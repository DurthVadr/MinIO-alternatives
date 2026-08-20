#!/usr/bin/env bash
# Run one (system, config, profile, concurrency) measurement and emit a single
# self-contained JSON result plus the telemetry CSV it was derived from.
#
# The system must already be up via `bench/stack.sh up <system> <config>`.
#
# Usage: ROUND=1 run-workload.sh <system> <config> <profile-id> <concurrency>
# Env:
#   ROUND                round number (default 1)
#   BENCH_PYTHON         python interpreter with PyYAML (default: .venv, then python3)
#   BENCH_MIN_FREE_GIB   floor for the pre-run disk guard (default 10)
#   BENCH_RUN_TIMEOUT    watchdog for the measurement, seconds (default 900)
#
# Output (under results/<profile_id>/raw/, profile_id from bench/hwprofile.sh):
#   <stem>.json                 the result: hardware profile, run metadata,
#                               derived metrics, telemetry, status and checks
#   <stem>.telemetry.csv        per-container CPU/memory/IO samples, 1 Hz
#   <stem>.warp.log             warp's own console output
#   <stem>.benchdata.csv.zst    warp's per-request records (with --full)
# where <stem> = <system>__<config>__<profile>__c<concurrency>__r<round>.
#
# The raw warp analysis is deliberately NOT embedded in the result: it was 88%
# of the file and is fully re-derivable from the benchdata with
# `warp analyze --json`. The benchdata keeps a `.benchdata.` infix so that
# decompressing it cannot land on top of the result file.
#
# STATUS VOCABULARY, in decreasing order of trust. Task 7 must treat anything
# other than "ok" as needing a look:
#   ok                 every check passed
#   ok_with_warnings   the numbers are real but something is off -- most
#                      importantly, the warp client may have been the
#                      bottleneck, in which case the number measures warp
#   suspect            a fatal check failed; the run produced numbers that
#                      should not be published
#   failed             warp did not run, or its analysis is unreadable; the
#                      file carries NO metrics block at all
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SYSTEMS="minio silo rustfs seaweedfs"
CONFIGS="c1 c2"
NETWORK="s3bench"

usage() {
  echo "usage: ROUND=<n> run-workload.sh <system> <config> <profile-id> <concurrency>" >&2
  echo "  <system>:      one of: $SYSTEMS" >&2
  echo "  <config>:      one of: $CONFIGS" >&2
  echo "  <profile-id>:  an id from bench/workloads.yaml" >&2
  echo "  <concurrency>: positive integer" >&2
  exit 2
}

die() { echo "run-workload: $*" >&2; exit 1; }

system="${1:-}"; config="${2:-}"; profile_id="${3:-}"; concurrency="${4:-}"
round="${ROUND:-1}"

[ -n "$system" ] && [ -n "$config" ] && [ -n "$profile_id" ] && [ -n "$concurrency" ] || usage
case " $SYSTEMS " in *" $system "*) ;; *) echo "unknown system '$system'" >&2; usage ;; esac
case " $CONFIGS " in *" $config "*) ;; *) echo "unknown config '$config'" >&2; usage ;; esac
[[ "$concurrency" =~ ^[1-9][0-9]*$ ]] || { echo "concurrency must be a positive integer" >&2; usage; }
[[ "$round" =~ ^[1-9][0-9]*$ ]] || die "ROUND must be a positive integer (got '$round')"
# profile_id becomes part of a filename and a docker -v path.
[[ "$profile_id" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]] || die "profile id '$profile_id' is not a plain slug"

# ---------------------------------------------------------------------------
# Interpreter. Both the YAML parsing and bench/assemble_result.py need to run;
# only the former needs PyYAML, which the system python3 on a developer machine
# generally does not have (it is in requirements-dev.txt). Resolve it once,
# loudly, instead of failing four steps later with an ImportError in the middle
# of an unattended matrix run.
# ---------------------------------------------------------------------------
PY="${BENCH_PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"; else PY="python3"; fi
fi
command -v "$PY" >/dev/null 2>&1 || die "python interpreter '$PY' not found"
"$PY" -c 'import yaml' 2>/dev/null || die "'$PY' cannot import yaml. Install requirements-dev.txt (python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt) or set BENCH_PYTHON."

# ---------------------------------------------------------------------------
# Hardware profile. bench/hwprofile.sh is the single source of profile_id and
# it enforces the collision guard; never reimplement either.
# ---------------------------------------------------------------------------
hw="$("$ROOT/bench/hwprofile.sh")" || die "hwprofile.sh failed"
pid="$(BENCH_HW="$hw" "$PY" -c 'import json,os; print(json.loads(os.environ["BENCH_HW"])["profile_id"])')"
[ -n "$pid" ] || die "could not read profile_id from the hardware profile"

raw="$ROOT/results/$pid/raw"
mkdir -p "$raw"

stem="${system}__${config}__${profile_id}__c${concurrency}__r${round}"
telemetry_csv="$raw/$stem.telemetry.csv"
warp_log="$raw/$stem.warp.log"
analysis_json="$raw/$stem.warp-analysis.json"
result_json="$raw/$stem.json"
benchdata_stem="$stem.benchdata"

# ---------------------------------------------------------------------------
# Resolve the profile out of workloads.yaml. Values come back one per line as
# <key>\t<value>; the generator refuses to emit a value containing a tab or a
# newline, so this cannot silently mis-split.
# ---------------------------------------------------------------------------
plan_file="$(mktemp "${TMPDIR:-/tmp}/warp-plan.XXXXXX")"
trap 'rm -f "$plan_file"' EXIT
BENCH_WORKLOADS="$ROOT/bench/workloads.yaml" BENCH_PROFILE="$profile_id" \
  "$PY" - > "$plan_file" <<'PY'
import os, sys, yaml

cfg = yaml.safe_load(open(os.environ["BENCH_WORKLOADS"]))
want = os.environ["BENCH_PROFILE"]
profiles = {p["id"]: p for p in cfg["profiles"]}
if want not in profiles:
    sys.exit("run-workload: no profile %r in workloads.yaml (have: %s)"
             % (want, ", ".join(sorted(profiles))))
p = profiles[want]
d = cfg["defaults"]
w = cfg["warp"]
c = cfg["credentials"]

out = [
    ("op", p["op"]),
    ("duration_seconds", p.get("duration_seconds", d["duration_seconds"])),
    ("bucket", p.get("bucket", d["bucket"])),
    ("write_gib_estimate", p.get("write_gib_estimate", d["write_gib_estimate"])),
    ("image", w["image"]),
    ("client_cpus", w["client"]["cpus"]),
    ("client_memory", w["client"]["memory"]),
    ("client_container", w["client"]["container_name"]),
    ("access_key", c["access_key"]),
    ("secret_key", c["secret_key"]),
]
out += [("arg", a) for a in w.get("common_args", [])]
out += [("arg", a) for a in p.get("args", [])]

for key, value in out:
    value = str(value)
    if "\t" in value or "\n" in value:
        sys.exit("run-workload: value for %r contains a tab or newline: %r" % (key, value))
    print(key + "\t" + value)
PY

op=""; duration_seconds=""; bucket=""; image=""; write_gib_estimate=""
client_cpus=""; client_memory=""; client_container=""
access_key=""; secret_key=""
warp_args=()
while IFS=$'\t' read -r key value; do
  case "$key" in
    op)                 op="$value" ;;
    duration_seconds)   duration_seconds="$value" ;;
    bucket)             bucket="$value" ;;
    write_gib_estimate) write_gib_estimate="$value" ;;
    image)              image="$value" ;;
    client_cpus)        client_cpus="$value" ;;
    client_memory)      client_memory="$value" ;;
    client_container)   client_container="$value" ;;
    access_key)         access_key="$value" ;;
    secret_key)         secret_key="$value" ;;
    arg)                warp_args+=("$value") ;;
    "")                 ;;
    *)                  die "unexpected plan key '$key'" ;;
  esac
done < "$plan_file"
[ -n "$op" ] && [ -n "$duration_seconds" ] && [ -n "$image" ] || die "incomplete plan for profile '$profile_id'"

image_id="$(docker image inspect --format '{{.Id}}' "$image" 2>/dev/null || true)"
[ -n "$image_id" ] || die "image '$image' not found. Build it: docker build -t $image bench/warp"

# ---------------------------------------------------------------------------
# Disk guard, scaled to what this profile actually writes.
#
# The measured data lands in Docker volumes inside the Docker Desktop VM, whose
# virtual disk cheerfully reports 200 GB free while actually being a sparse
# image on a host filesystem with 25 GB left -- so the VM's figure gives false
# comfort and the host's is the one that can run out. Tearing the stack down
# does not return the space immediately either (Docker Desktop reclaims on its
# own schedule), so free space lags reality partway through a matrix.
#
# One flat floor is not enough: `bigdata-put` writes several GiB in 30s, so a
# run can clear a 10 GiB floor and still fill the disk mid-flight. The figure
# comes from bench/disk_budget.py, which is the ONLY place the formula lives --
# bench/run.sh waits for the same number before starting this script, and when
# the two disagreed a transient dip became a permanent lost cell instead of a
# pause. The same figure is handed to the assembler, which re-checks it after
# the run so the next one is not started into a disk that has not been
# reclaimed.
# ---------------------------------------------------------------------------
min_free_gib="${BENCH_MIN_FREE_GIB:-10}"
required_gib="$("$PY" "$ROOT/bench/disk_budget.py" "$ROOT/bench/workloads.yaml" \
  "$profile_id" "$config" "$min_free_gib")" \
  || die "could not compute the disk requirement for '$profile_id' on $config"
host_free_bytes() { echo $(( $(df -k "$ROOT" | awk 'NR==2 {print $4}') * 1024 )); }
disk_required=$(( required_gib * 1024 * 1024 * 1024 ))
disk_before="$(host_free_bytes)"
if [ "$disk_before" -lt "$disk_required" ]; then
  die "only $(( disk_before / 1024 / 1024 / 1024 )) GiB free on the host; profile '$profile_id' on $config needs ${required_gib} GiB. Tear down stacks, wait for Docker Desktop to reclaim, or lower BENCH_MIN_FREE_GIB."
fi

# ---------------------------------------------------------------------------
# Assert the running stack is the one we are about to label the results with,
# and that nothing else is competing for the same 8 CPUs.
#
# Nothing else in the pipeline checks either. `stack.sh up minio c1` followed
# by `run-workload.sh minio c2 ...` would produce a file named c2 holding a c1
# measurement, and no reader could ever tell. And a stray second bench-* stack
# left running by an interrupted session -- which has already happened once in
# this project -- would contaminate every subsequent run undetected.
#
# `docker compose --profile <config> config` names exactly the containers that
# config is supposed to have: one for minio/silo/rustfs in either config, but
# bench-seaweedfs plus bench-swfs-vol0..3 for seaweedfs c2.
# ---------------------------------------------------------------------------
expected_containers="$(
  docker compose -f "$ROOT/compose/${system}.yaml" -p "bench-${system}" --profile "$config" \
    config --format json 2>/dev/null \
  | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(" ".join(sorted(str(s.get("container_name","")) for s in d["services"].values())))'
)" || die "could not read the compose config for $system/$config"
[ -n "$expected_containers" ] || die "compose config for $system/$config named no containers"

running_containers="$(
  docker ps --filter "label=com.docker.compose.project=bench-${system}" --format '{{.Names}}' \
  | sort | tr '\n' ' ' | sed 's/ $//'
)"
if [ "$running_containers" != "$expected_containers" ]; then
  die "stack mismatch for ${system}/${config}: expected containers [$expected_containers] but [${running_containers:-none}] are running. Run: ./bench/stack.sh up $system $config"
fi

foreign_projects="$(
  docker ps --format '{{.Label "com.docker.compose.project"}}' \
  | grep '^bench-' | sort -u | grep -v "^bench-${system}\$" | tr '\n' ' ' | sed 's/ $//' || true
)"
[ -z "$foreign_projects" ] || die "another benchmark stack is running (compose project(s): $foreign_projects). It competes for the same CPUs and would contaminate this measurement. Tear it down first."

# The per-system budget is a claim until it is read back off the running
# containers. It is also the yardstick the telemetry needs: any CPU sample
# above it is physically impossible and therefore a measurement artefact.
cpu_budget_pct="$(
  docker inspect --format '{{.HostConfig.NanoCpus}}' $(docker ps --filter "label=com.docker.compose.project=bench-${system}" --format '{{.ID}}') 2>/dev/null \
  | awk '{ total += $1 } END { if (total > 0) printf "%.0f", total / 10000000; else print "" }'
)"

endpoint="bench-${system}:9000"

# ---------------------------------------------------------------------------
# Measure.
# ---------------------------------------------------------------------------
# Containers left behind by an interrupted run would make `--name` collide.
docker rm -f "$client_container" "${client_container}-analyze" >/dev/null 2>&1 || true

telemetry_pid=""
watchdog_pid=""

# Stop a `( sleep N; ... ) &` watchdog completely.
#
# `kill $!` reaches only the subshell, which is blocked in `wait`; the `sleep`
# it forked is reparented to init and keeps every file descriptor it inherited
# -- including this script's stdout -- open for the rest of the timeout. That
# is invisible when stdout is a terminal and fatal when it is a pipe: the
# orchestrator (bench/run.sh) reads this script's output, and its reader would
# block for the full BENCH_RUN_TIMEOUT after every single measurement, turning
# a 90-second run into a 15-minute one. Take the child down by pid, after the
# subshell is dead so it cannot reach its `docker rm -f`.
stop_watchdog() {
  local child
  [ -n "$watchdog_pid" ] || return 0
  child="$(pgrep -P "$watchdog_pid" 2>/dev/null || true)"
  kill "$watchdog_pid" 2>/dev/null || true
  if [ -n "$child" ]; then kill $child 2>/dev/null || true; fi
  wait "$watchdog_pid" 2>/dev/null || true
  watchdog_pid=""
}

cleanup() {
  [ -n "$telemetry_pid" ] && kill "$telemetry_pid" 2>/dev/null || true
  stop_watchdog
  docker rm -f "$client_container" "${client_container}-analyze" >/dev/null 2>&1 || true
  rm -f "$plan_file"
}
trap cleanup EXIT

TELEMETRY_CLIENT_CONTAINER="$client_container" BENCH_PYTHON="$PY" \
  "$ROOT/bench/telemetry.sh" "$system" "$telemetry_csv" &
telemetry_pid=$!

# Watchdog. Task 6 runs this script ~192 times unattended; a system that wedges
# mid-benchmark would otherwise stall the whole night on one `docker run` with
# no timeout. Removing the client container makes warp exit non-zero, which
# lands as "status": "failed" and lets the matrix move on. The budget has to
# cover warp's un-timed prepare phase (uploading --objects objects) as well as
# --duration, hence the generous default.
run_timeout="${BENCH_RUN_TIMEOUT:-900}"
( sleep "$run_timeout"; docker rm -f "$client_container" >/dev/null 2>&1 ) &
watchdog_pid=$!

started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# warp runs as a container on the s3bench bridge network, never from the host:
# on Docker Desktop, host->container traffic crosses a gvisor userspace network
# stack, so a host-side client would benchmark Docker Desktop's networking
# rather than the storage engine.
#
# CORRECTIONS to the brief's warp invocation, all from `warp <op> --help` in
# the pinned v1.6.1 image:
#   --warp-client=   dropped. It means "connect to these warp clients and run
#                    the benchmark there" (distributed mode). Passing it empty
#                    was a guess; there is no reason to name the flag at all
#                    for a single local client.
#   --noclear=false  dropped. --noclear is a boolean whose default is already
#                    "clear the bucket before and after", which is what
#                    isolates consecutive runs. Naming it to request the
#                    default only creates a way to get it backwards.
#   --duration       carries the whole run; there is no --warmup flag in warp
#                    and --analyze.skip is inert on the data warp writes (see
#                    the comment on `defaults` in bench/workloads.yaml).
# --full is in workloads.yaml's common_args: without it warp only ever stores
# pre-aggregated 10-second windows, and true latency quantiles are then
# permanently unrecoverable for the whole matrix.
set +e
docker run --rm --name "$client_container" --network "$NETWORK" \
  --cpus "$client_cpus" --memory "$client_memory" \
  -v "$raw:/results" \
  "$image" \
  "$op" \
  --host="$endpoint" \
  --access-key="$access_key" \
  --secret-key="$secret_key" \
  --bucket="$bucket" \
  --concurrent="$concurrency" \
  --duration="${duration_seconds}s" \
  --benchdata="/results/$benchdata_stem" \
  ${warp_args[@]+"${warp_args[@]}"} \
  >"$warp_log" 2>&1
warp_exit=$?
set -e

ended_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
stop_watchdog
kill "$telemetry_pid" 2>/dev/null || true
wait "$telemetry_pid" 2>/dev/null || true
telemetry_pid=""
disk_after="$(host_free_bytes)"

# ---------------------------------------------------------------------------
# Locate the benchdata warp actually wrote.
#
# CORRECTION: the brief assumed "<benchdata>.csv.zst". v1.6.1 writes
# "<benchdata>.json.zst" for its default aggregated output and .csv.zst only
# with --full. Rather than hardcode either, accept whichever exists and fail
# loudly if that is neither or both -- a future warp changing the extension
# again must not turn into a silent "no results".
# ---------------------------------------------------------------------------
benchdata=""
if [ "$warp_exit" -eq 0 ]; then
  # Counted, not word-split: $raw contains the repository path, which may well
  # contain a space (it does on the machine this was developed on).
  found_count=0
  found_names=""
  for ext in csv.zst json.zst; do
    if [ -f "$raw/$benchdata_stem.$ext" ]; then
      found_count=$(( found_count + 1 ))
      benchdata="$raw/$benchdata_stem.$ext"
      found_names="$found_names $benchdata_stem.$ext"
    fi
  done
  if [ "$found_count" -eq 0 ]; then
    echo "run-workload: warp exited 0 but wrote no benchdata for $stem" >&2
    warp_exit=90
  elif [ "$found_count" -gt 1 ]; then
    echo "run-workload: ambiguous benchdata for $stem:$found_names" >&2
    benchdata=""
    warp_exit=91
  fi
fi

analyze_exit=0
if [ "$warp_exit" -eq 0 ]; then
  # The measurement watchdog is already gone by here, and `warp analyze` on a
  # --full benchdata is real work, so it gets its own.
  ( sleep "$run_timeout"; docker rm -f "${client_container}-analyze" >/dev/null 2>&1 ) &
  watchdog_pid=$!
  set +e
  docker run --rm --name "${client_container}-analyze" -v "$raw:/results" "$image" \
    analyze --json "/results/$(basename "$benchdata")" >"$analysis_json" 2>>"$warp_log"
  analyze_exit=$?
  set -e
  stop_watchdog
fi

# ---------------------------------------------------------------------------
# Assemble the result. Every value crosses into Python through argv or the
# environment; bench/assemble_result.py is a real module so that the window
# merge and the checks can be unit-tested (tests/test_assemble_result.py).
# ---------------------------------------------------------------------------
set +e
BENCH_HW="$hw" \
BENCH_SYSTEM="$system" \
BENCH_CONFIG="$config" \
BENCH_PROFILE="$profile_id" \
BENCH_OP="$op" \
BENCH_CONCURRENCY="$concurrency" \
BENCH_ROUND="$round" \
BENCH_DURATION="$duration_seconds" \
BENCH_BUCKET="$bucket" \
BENCH_ENDPOINT="$endpoint" \
BENCH_IMAGE="$image" \
BENCH_IMAGE_ID="$image_id" \
BENCH_ARGS="$(printf '%s\n' ${warp_args[@]+"${warp_args[@]}"})" \
BENCH_CLIENT_CPUS="$client_cpus" \
BENCH_CLIENT_MEMORY="$client_memory" \
BENCH_CLIENT_CONTAINER="$client_container" \
BENCH_EXPECTED_CONTAINERS="$expected_containers" \
BENCH_CPU_BUDGET_PCT="$cpu_budget_pct" \
BENCH_WARP_EXIT="$warp_exit" \
BENCH_ANALYZE_EXIT="$analyze_exit" \
BENCH_STARTED_AT="$started_at" \
BENCH_ENDED_AT="$ended_at" \
BENCH_DISK_BEFORE="$disk_before" \
BENCH_DISK_AFTER="$disk_after" \
BENCH_DISK_REQUIRED="$disk_required" \
BENCH_BENCHDATA_BYTES="$([ -n "$benchdata" ] && wc -c < "$benchdata" | tr -d ' ' || echo 0)" \
BENCH_BENCHDATA_SHA256="$([ -n "$benchdata" ] && shasum -a 256 "$benchdata" | awk '{print $1}' || echo '')" \
"$PY" "$ROOT/bench/assemble_result.py" \
  "$analysis_json" "$telemetry_csv" "${benchdata:--}" "$result_json"
assemble_exit=$?
set -e

# The analysis is re-derivable from the benchdata; the standalone copy would
# only be a second, divergeable source of the same bytes. Kept on failure,
# where it is the evidence.
if [ "$assemble_exit" -eq 0 ]; then
  rm -f "$analysis_json"
fi

exit "$assemble_exit"
