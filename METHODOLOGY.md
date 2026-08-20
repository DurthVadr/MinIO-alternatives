# Methodology

This document exists so you can decide how much to trust [RESULTS.md](RESULTS.md)
without reading the harness. It covers what is being compared, what was held
equal and what deliberately was not, and where the measurement is weak.

Everything below is implemented in this repository. Where a decision has a
longer argument attached, that argument lives in a comment next to the code
that implements it — `bench/workloads.yaml` in particular is written to be read.

## 1. What is being compared

Four S3-compatible object stores, each in its default single-node deployment,
each pinned by image digest in [images.lock](images.lock):

- **MinIO** — the last community release, as the baseline everything else is
  being considered against.
- **Silo** — a fork of MinIO. Not really an alternative in the usual sense: the
  S3 API, environment variables, metrics and HTTP paths are identical, and
  swapping the image name over the same data directory works. It is in this
  study as a differential measurement — does the fork cost anything? — rather
  than as an independent candidate.
- **RustFS** — a Rust reimplementation, release candidate at the time of
  measurement.
- **SeaweedFS** — a mature Go project whose redundancy model is replication
  rather than erasure coding, which is what makes the durability axis below a
  real decision rather than a formality.

Ceph RGW and Apache Ozone are out of scope. Both are designed to run across
several nodes, and a single VM with under 4 GiB of RAM would measure that
constraint rather than the architecture. Excluding them is a scope decision,
not a judgement.

Each system runs in two configurations:

- **c1** — a single device, no redundancy. The upper bound on what the software
  can do when it is not paying for durability.
- **c2** — configured to survive the loss of exactly one device. For MinIO,
  Silo and RustFS this is four drives with parity 1; for SeaweedFS it is two
  replicas.

## 2. Fairness controls

### The resource budget is per system, not per container

Each system gets 6 CPUs and 2048 MB of memory **in total**, across however many
containers its architecture needs.

This matters because the architectures differ. SeaweedFS in its replicated
configuration runs five processes — a master, a filer, volume servers, an S3
gateway. MinIO runs one. Giving every container 2048 MB would have handed
SeaweedFS 10 GB against MinIO's 2 GB.

The cost of a multi-process design is a real property of that design. Exempting
it from the budget would not be neutrality, it would be a subsidy.

The consequence is visible in the results and is stated there: SeaweedFS cannot
complete the 20 MiB profile inside 2 GB. A diagnostic run at 4 GB completed and
peaked at 3.34 GiB. RESULTS.md keeps that in a separate Diagnostics section
precisely so it cannot be quoted as a performance number, because it ran under
conditions no other system was given.

### The load generator runs inside the VM, and is bounded

The benchmark client is [warp](https://github.com/minio/warp) v1.6.1, built
from the release asset whose SHA256 is recorded in `images.lock` — there is no
published image for that version.

It runs **inside** the Docker VM, on the same network as the system under test.
On Docker Desktop, traffic between the host and the VM crosses a userspace
network layer; measuring from the host would have measured that layer.

It is also **capped at 2.0 CPUs and 1024 MB**, outside the systems' 6 + 2048 MB
budget. An unbounded client would take whatever CPU the server left idle, which
makes the client's speed a function of the server's — a fast system would hand
its own client more CPU than a slow one, and the comparison would partly be
measuring that. 6 for the system plus 2 for the client exactly partitions the
VM's 8 vCPUs.

The risk of capping it is the obvious one: the client becomes the bottleneck
and client numbers get published as storage numbers. That is not left to trust.
`bench/telemetry.sh` samples the warp container as `role=client`, every result
records its CPU saturation, and a `client_not_saturated` check fires when it
gets close to its limit. See §5 for what happens to a run that trips it.

### Ordering and thermal effects

Systems cycle within each round rather than each system running its whole
matrix back to back. Running one system to completion before starting the next
lets thermal throttling accumulate against whoever runs last. The design calls
for three rounds and a median across them.

**This control is currently unexercised**: what exists is one round. The
evidence that it matters is in the data itself — on the 20 MiB read profile,
two systems measured faster in their redundant configuration than in their
single-device one, which erasure coding cannot cause. RESULTS.md says so at the
top of its performance section.

### Everything is pinned

Every image is pinned by `sha256:` digest rather than by tag, including the
helper images (`curl` for readiness polling, `debian` as the warp base). A
floating tag repointed mid-study would change what was measured with nothing
recording it. `bench/lock-images.sh` re-resolves all of them and exits non-zero
on drift.

### Every result carries the hardware that produced it

`bench/hwprofile.sh` collects CPU model, core count, RAM, OS, architecture,
container runtime versions and VM allocation, and derives a stable
`profile_id` and fingerprint. Every result file embeds it, and results live
under `results/<profile_id>/`.

The fingerprint deliberately excludes the VM's CPU and RAM allocation. Docker
Desktop's Resource Saver changes those between invocations, so including them
would make the same machine fingerprint differently mid-matrix.

## 3. Durability: which axis is held equal

This is the most consequential decision in the study.

Erasure coding and replication cannot be equalised on two axes at once. Hold
storage efficiency equal and fault tolerance drifts; hold fault tolerance equal
and storage efficiency drifts. There is no configuration where both match.

**Fault tolerance is held equal. Storage efficiency is measured and reported as
a result.**

The reasoning: fault tolerance is an operational requirement — you either
survive losing a device or you do not — and storage efficiency is what meeting
that requirement costs. Holding the requirement constant and measuring the cost
answers the question a team actually has. The reverse would have equalised the
cost and let the guarantee float, which answers nothing.

So every c2 configuration survives the loss of exactly one device, and the
usable-capacity column in RESULTS.md is a finding rather than a control.

### Durability is proved, not read off a config file

For each system: write an object, **actually destroy a device**, read the
object back and compare it byte for byte.

Then the negative control — destroy one device *more* than the configuration
tolerates and require the read to fail. Without it, a passing durability test
says nothing about whether the test is capable of failing.

After the unrecoverable fault, reads are sampled on a fixed schedule: t=0 (the
moment the fault-injection command returns), then 0.25, 0.5, 1, 2, 4 and 8
seconds. Each sample is two requests — a raw signed GET for the wire-level
facts, and a boto3 GET immediately after for the SDK's view — so a compound
label like `HTTP 200 / ResponseStreamingError` describes two requests taken
moments apart, not one contradictory response. The counts in RESULTS.md
describe that schedule, not observed frequencies.

### Configuration caveats travel with the data

RustFS is the only one of the four that refuses to start in erasure mode when
its drives share a physical device, which every c2 configuration here does. It
had to be started with `RUSTFS_UNSAFE_BYPASS_DISK_CHECK=true`.

That bypass *equalises* the comparison rather than relaxing it: MinIO reaches
the same condition and only warns, and SeaweedFS has no such check. The
alternatives were to drop RustFS from c2, or to give it four real devices the
others did not get.

The caveat is recorded inside `durability.json`, not only in prose, so it
cannot be separated from the number it qualifies. It is also a finding in its
own right, and RESULTS.md reports it in both directions: RustFS is the
youngest of the four and the only one that treats this as an error.

## 4. Conformance: five outcomes, not pass and fail

29 S3 behaviours, chosen for what breaks in production rather than for
coverage: conditional writes, Object Lock, versioning, multipart, SSE, presigned
URLs, ListObjectsV2 pagination.

Each cell gets one of five statuses:

| Status | Meaning |
| --- | --- |
| `supported` | Behaves the way AWS documents it. |
| `diverges` | Implemented, but observably different. |
| `accepted_not_enforced` | Accepts the call and returns success without enforcing the guarantee it implies. |
| `not_implemented` | Absent, and says so. |
| `not_exercisable` | The system behaves correctly; this harness cannot exercise the behaviour. |

`accepted_not_enforced` is separated from `not_implemented` deliberately, and
it is the most useful distinction the matrix makes. *This feature is missing,
plan around it* and *you believe you have a guarantee and you do not* are
different problems, and only the second one fails silently in production.

`not_exercisable` carries a mandatory `reason`, because two different claims
live under it: `conformant_refusal` (the server refused for the same reason AWS
would — an affirmative statement that it behaved correctly) and
`missing_prerequisite` (the feature needs something this deployment does not
supply, such as a key manager — no claim either way).

Rules that keep the matrix honest:

- **No per-system score.** The categories are not commensurable, so a total
  rewards a system for accepting a request it should refuse. This is not
  hypothetical: at one point in this study RustFS ranked above MinIO because it
  accepted SSE-C over plain HTTP, which AWS rejects — MinIO was being penalised
  for being right. The fix was to reclassify the cell and never publish a count
  per system.
- **A test that fails without recording a verdict records `error`, not a
  status.** Deriving a verdict from any failure is how a harness bug becomes a
  public claim about a named product.
- **Prerequisite refusals are system-agnostic.** The SSE-C-over-HTTP rule is
  evaluated from the endpoint scheme, not from which system is being tested, so
  it cannot quietly become a per-system exemption.

## 5. What counts as a clean run

Every measurement writes a `status`, decided by 14 checks:

| Status | Meaning |
| --- | --- |
| `ok` | Every check passed. |
| `ok_with_warnings` | Real numbers, but at least one non-fatal check failed. |
| `suspect` | A fatal check failed. |
| `failed` | warp did not run, or its analysis is unreadable. There are no metrics. |

The important property is that **any** failing check, including warning-level
ones, takes a run out of `ok`. An earlier version filtered on severity, and a
run where the benchmark client was itself the bottleneck was published as
clean — the check had fired, and the status ignored it. A consumer filtering on
`status == "ok"` would have published a number that measured the load
generator, most likely on the 4 KiB profile, which is exactly where the
headline small-object claims are.

The orchestrator never branches on the exit code for this. `run-workload.sh`
exits 0 for both `ok` and `ok_with_warnings` so an unattended matrix keeps
going; the driver reads the `status` field out of the result JSON instead.

## 6. How latency is computed

warp reports latency in 10-second windows. **Those windows cannot be averaged.**

Merging them unweighted inflated one run's median by 3.8× — the final window
held 0.4% of the run's requests but took 14.3% of the weight in the mean. The
corrected median was 109.1 ms against 411.4 ms.

Two rules follow:

1. Window merges are weighted by request count.
2. **Percentiles are not merged at all.** The mean of p99s is not a p99. Every
   percentile in RESULTS.md is computed from warp's per-request records, which
   is why `--full` is in the common arguments for every run. That decision has
   to be made before the matrix runs: without `--full`, warp never stores
   per-request latencies and no later analysis can recover them.

The weighted implementation was validated against those per-request records:
the weighted mean matched the ground truth to six decimal places.

Latency in RESULTS.md is end-to-end request duration for the measured
operation. Time-to-first-byte is recorded too, but it is only meaningful on
reads, and publishing a TTFB percentile for a PUT-dominated profile would be a
number with no interpretation.

## 7. What is deliberately not measured

- **Multi-node behaviour.** Everything here is single-node. Rebalancing,
  rebuild time after a device replacement, and cross-node consistency are all
  out of scope, and they are where the differences between these systems are
  likely to be largest.
- **Anything above the S3 API.** Console usability, IAM depth, lifecycle rules,
  notification targets, tiering.
- **Cost, licensing and governance.** They matter for a migration decision, and
  none of them is a measurement.
- **Long-run stability.** The longest measured window here is 60 seconds.
  Nothing in this repository says anything about a week of production traffic.

## 8. Known threats to validity

Stated plainly, because a methodology document that only lists its strengths is
an advertisement.

1. **One round, not three.** The performance matrix has no repetition, so
   ordering and warm-up effects are uncontrolled — and, as noted in §2, the
   data shows they are large enough to reverse a comparison. Treat every
   performance number as preliminary.
2. **One machine.** An Apple M1 with 8 GB of RAM, running Docker Desktop with a
   VM that has under 4 GiB. Absolute values belong to this hardware. This is why
   every result carries a fingerprint and why `CONTRIBUTING.md` invites runs
   from other machines.
3. **Four drives on one physical disk.** Every c2 configuration puts its
   "devices" on the same host disk, so device-loss is simulated at the
   filesystem level and the drives share one I/O queue. The fault injection is
   real; the physical independence is not.
4. **A 2 GB budget is small.** It is enough to differentiate these systems on
   this hardware, and it is small enough that at least one of them cannot
   complete a profile inside it. A production deployment would not run this way.
5. **Default configurations.** No tuning was applied to any system. That is
   deliberate and it is even-handed, but a system whose defaults are
   conservative is being measured at a disadvantage it would not have in
   practice.
6. **A conformance suite is only as good as its behaviours.** 29 is not
   exhaustive. A cell that is not in the matrix is not a claim that the
   behaviour works.
