# MinIO alternatives, measured

MinIO's community edition shipped its last release in October 2025 and the
repository was archived in April 2026. Plenty of projects now describe
themselves as the replacement. Very few of the comparisons in circulation say
which benchmark tool they used, which object sizes they tried, or what
durability setting they measured under — so the numbers cannot be checked, and
a number you cannot check is not evidence.

This repository is the comparison with all of that written down. Four
S3-compatible object stores, on one machine, under one budget, with every
result stamped by the hardware that produced it and every image pinned by
digest.

| System | What it is | Pinned image |
| --- | --- | --- |
| MinIO | The last community release, as the baseline | `alpine/minio:RELEASE.2025-10-15T17-29-55Z` |
| Silo | A fork of MinIO by Pigsty, with the console restored | `pgsty/silo:latest` |
| RustFS | A Rust reimplementation, Apache-2.0, release candidate | `rustfs/rustfs:rc` |
| SeaweedFS | Mature Go project, replication rather than erasure coding | `chrislusf/seaweedfs:v3.33` |

Digests are in [images.lock](images.lock). `bench/lock-images.sh` re-resolves
them and exits non-zero if a tag has been repointed underneath us.

Ceph RGW and Apache Ozone were deliberately left out. Both are built to run
across many nodes; on a single VM with under 4 GiB of RAM, what gets measured
is the constraint, not the architecture.

## What the study found

Full tables, with every cell traceable to a file under `results/`, are in
**[RESULTS.md](RESULTS.md)**. The short version:

### Conformance separates them more than speed does

29 S3 behaviours, four systems, five possible outcomes per cell. MinIO, Silo
and RustFS support essentially all of it. SeaweedFS accepts nine calls it does
not enforce — it returns success while the guarantee the call implies is never
applied.

The one to know about is conditional write. Iceberg and Delta Lake commit
protocols depend on `If-None-Match: *` failing when the object already exists.
SeaweedFS returns HTTP 200 and overwrites. Two writers both believe they
committed, and one of them silently did not.

Object Lock behaves the same way: an object under legal hold deletes without
error, and a delete succeeds with 119 seconds still on its retention clock. In
SSE-C the encryption headers are inert — the object is stored in plaintext and
reads back with the wrong key.

Separately, and worth checking before you deploy anything: the SeaweedFS S3
gateway silently ignores `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`. A
gateway configured with that widely copied pattern accepts *any* signature —
deliberately wrong credentials were accepted in testing. Identities only take
effect when loaded from a separate `-s3.config` file. The evidence, including
the log line the gateway emits while doing this, is in
[compose/seaweedfs.yaml](compose/seaweedfs.yaml) under `CORRECTION 2`.

### Durability costs storage, and the trade is measurable

Every system was configured to survive the loss of exactly one device, then had
a device actually destroyed and the object read back and compared byte for
byte. All four survived. What differs is what that guarantee costs:

| Mechanism | Usable share of raw capacity |
| --- | ---: |
| Erasure coding, 4 drives, parity 1 (MinIO, Silo, RustFS) | ~0.75 |
| Replication, 2 copies (SeaweedFS) | 0.50 |

On 10 TB of raw disk that is 7.5 TB against 5 TB, for the same tolerance to a
single failure.

Past the redundancy limit, none of the four fails cleanly in the first moments
after the fault. The shapes differ and they matter if your client retries: a
wrong error code, a `Content-Length` that does not match the body, an error
body that is not S3 XML.

### Performance is preliminary and labelled as such

The design called for three interleaved rounds and a median. What exists is one
round, and the data contains direct evidence that this is not enough: on the
20 MiB read profile, both Silo and RustFS measured *faster* in their redundant
configuration than in their single-device one. Erasure coding cannot make a
read cheaper, so something other than the storage configuration is moving those
numbers.

The tables are published anyway, with the caveat attached, because the
harness — not the leaderboard — is the point of this repository. Two results in
particular should not be quoted until they have been repeated: Silo sitting at
roughly half of MinIO despite sharing a codebase, and RustFS coming in well
under its own published claim of 2.3× MinIO on small objects.

SeaweedFS has no number at all for the 20 MiB profile: it is killed out of
memory during the upload phase under the study's 2 GB per-system budget, in
both configurations. A diagnostic run at 4 GB completed and peaked at 3.34 GiB.
That is a memory-footprint finding, not a read-performance one, and RESULTS.md
keeps it in its own section so it cannot be misread as the latter.

## Running it

Requires Docker, and a Python 3 with PyYAML available. On the reference
machine 29 measured runs took 1.7 hours end to end, so the full 192-run matrix
is most of a day.

```sh
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

bench/lock-images.sh                  # verify pinned digests still resolve
.venv/bin/python -m pytest tests -q   # the harness's own tests

bench/run.sh --dry-run                # what the full matrix would run
bench/run.sh --quick                  # one round, three profiles
bench/run.sh                          # the full matrix

bench/make_results.py                 # regenerate RESULTS.md
```

`bench/run.sh` is resumable. A combination that already has a result file is
skipped, so an interrupted matrix continues instead of restarting. Pass
`--force` to re-measure.

The conformance and durability matrices are separate suites, because both write
published artefacts and neither should be re-recorded by an ordinary `pytest`:

```sh
.venv/bin/python -m pytest conformance -q      # writes results/<profile>/conformance.json
BENCH_WRITE_RESULTS=1 .venv/bin/python -m pytest tests/test_durability.py -q
```

Individual stacks can be driven by hand:

```sh
bench/stack.sh up minio c2      # c1 = single device, c2 = redundant
bench/stack.sh endpoint minio
bench/stack.sh down minio
```

## Layout

```
bench/          the measurement harness
  run.sh          orchestrator: round-robin over the matrix, resumable
  run-workload.sh one measured run, end to end
  assemble_result.py  turns warp output + telemetry into one result JSON
  make_results.py     turns results/ into RESULTS.md
  stack.sh        bring one system up or down
  hwprofile.sh    the hardware profile stamped into every result
  workloads.yaml  the workload matrix, with the reasoning for each choice
compose/        one Compose stack per system, c1 and c2 profiles
conformance/    the 29-behaviour S3 conformance suite
tests/          tests for the harness, plus the durability fault injection
results/        one directory per hardware profile
images.lock     every image this repository runs, by digest
RESULTS.md      generated from results/ — do not edit by hand
METHODOLOGY.md  why the measurement is shaped the way it is
CONTRIBUTING.md how to add results from your own hardware
```

## Status

Complete and hardware-independent: the conformance matrix and the durability
matrix.

Partial: the performance matrix covers 29 of 192 planned runs, at one round
instead of three. See the caveat above and in RESULTS.md before quoting any of
it.

Results were produced on a single Apple M1 with 8 GB of RAM. That is a real
limitation and the reason every result carries a hardware fingerprint. Runs
from other machines are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).
