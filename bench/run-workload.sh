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
#   BENCH_MIN_FREE_GIB   abort if the host has less free disk than this (default 10)
#
# Output (under results/<profile_id>/raw/, profile_id from bench/hwprofile.sh):
#   <stem>.json            the result: hardware profile, run metadata, derived
#                          metrics, the full raw warp analysis, telemetry
#   <stem>.telemetry.csv   per-container CPU/memory/IO samples, 1 Hz
#   <stem>.warp.log        warp's own console output
#   <stem>.json.zst        warp's raw benchdata, so the analysis can be redone
# where <stem> = <system>__<config>__<profile>__c<concurrency>__r<round>
#
# FAILURE BEHAVIOUR. A run that did not measure anything must never look like
# one that did. If warp fails, or the analysis cannot be read, the result file
# is written with "status": "failed" and NO "metrics" key at all, and the
# script exits non-zero. If warp succeeds but a sanity check fails (operation
# errors, a truncated measurement window, telemetry that missed containers),
# the status is "suspect" and the failed checks are named in the file. Only
# "status": "ok" means every check passed. Task 7 must filter on that field.
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
# Interpreter. The YAML parsing and the result assembly both need PyYAML, which
# the system python3 on a developer machine generally does not have (it is in
# requirements-dev.txt). Resolve it once, loudly, instead of failing four steps
# later with an ImportError in the middle of an unattended matrix run.
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

# ---------------------------------------------------------------------------
# Disk guard. The measured data lands in Docker volumes inside the Docker
# Desktop VM, whose virtual disk cheerfully reports 200 GB free while actually
# being a sparse image on a host filesystem with 25 GB left -- so the VM's own
# figure gives false comfort and the host's is the one that can actually run
# out. Measured on this machine: one 60s `medium` run consumed 4-5 GiB of host
# disk, and tearing the stack down does not return it immediately; Docker
# Desktop reclaims it on its own schedule, minutes later. Free space can
# therefore lag several GiB behind reality partway through a matrix, which is
# why this aborts before starting rather than filling the disk mid-benchmark.
# ---------------------------------------------------------------------------
min_free_gib="${BENCH_MIN_FREE_GIB:-10}"
host_free_bytes() { echo $(( $(df -k "$ROOT" | awk 'NR==2 {print $4}') * 1024 )); }
disk_before="$(host_free_bytes)"
if [ "$disk_before" -lt $(( min_free_gib * 1024 * 1024 * 1024 )) ]; then
  die "only $(( disk_before / 1024 / 1024 / 1024 )) GiB free on the host, need ${min_free_gib} GiB. Free space (docker system prune -f) or lower BENCH_MIN_FREE_GIB."
fi

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

op=""; duration_seconds=""; bucket=""; image=""
client_cpus=""; client_memory=""; client_container=""
access_key=""; secret_key=""
warp_args=()
while IFS=$'\t' read -r key value; do
  case "$key" in
    op)               op="$value" ;;
    duration_seconds) duration_seconds="$value" ;;
    bucket)           bucket="$value" ;;
    image)            image="$value" ;;
    client_cpus)      client_cpus="$value" ;;
    client_memory)    client_memory="$value" ;;
    client_container) client_container="$value" ;;
    access_key)       access_key="$value" ;;
    secret_key)       secret_key="$value" ;;
    arg)              warp_args+=("$value") ;;
    "")               ;;
    *)                die "unexpected plan key '$key'" ;;
  esac
done < "$plan_file"
[ -n "$op" ] && [ -n "$duration_seconds" ] && [ -n "$image" ] || die "incomplete plan for profile '$profile_id'"

docker image inspect "$image" >/dev/null 2>&1 \
  || die "image '$image' not found. Build it: docker build -t $image bench/warp"

# ---------------------------------------------------------------------------
# Assert the running stack is the one we are about to label the results with.
#
# Nothing else in the pipeline checks this: `stack.sh up minio c1` followed by
# `run-workload.sh minio c2 ...` would happily produce a file named c2 holding
# a c1 measurement, and no reader could ever tell. `docker compose --profile
# <config> config` names exactly the containers that config is supposed to have
# -- one for minio/silo/rustfs in either config, but bench-seaweedfs plus
# bench-swfs-vol0..3 for seaweedfs c2 -- so comparing that set against what is
# actually running catches both a mislabelled config and a partially started
# stack.
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

# The per-system budget is a claim until it is read back off the running
# containers. It is also the yardstick the telemetry needs: any CPU sample
# above it is physically impossible and therefore a measurement artefact, and
# the result file should say how many there were rather than quietly average
# them in.
cpu_budget_pct="$(
  docker inspect --format '{{.HostConfig.NanoCpus}}' $(docker ps --filter "label=com.docker.compose.project=bench-${system}" --format '{{.ID}}') 2>/dev/null \
  | awk '{ total += $1 } END { if (total > 0) printf "%.0f", total / 10000000; else print "" }'
)"

endpoint="bench-${system}:9000"

# ---------------------------------------------------------------------------
# Measure.
# ---------------------------------------------------------------------------
# A container left behind by an interrupted run would make `--name` collide.
docker rm -f "$client_container" >/dev/null 2>&1 || true

telemetry_pid=""
watchdog_pid=""
cleanup() {
  [ -n "$telemetry_pid" ] && kill "$telemetry_pid" 2>/dev/null || true
  [ -n "$watchdog_pid" ] && kill "$watchdog_pid" 2>/dev/null || true
  docker rm -f "$client_container" >/dev/null 2>&1 || true
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
#                    "clear the bucket before and after", which is what isolates
#                    consecutive runs. Naming it to request the default only
#                    creates a way to get it backwards.
#   --duration       carries the whole run; there is no --warmup flag in warp
#                    and --analyze.skip is inert on the data warp writes (see
#                    the comment on `defaults` in bench/workloads.yaml).
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
  --benchdata="/results/$stem" \
  ${warp_args[@]+"${warp_args[@]}"} \
  >"$warp_log" 2>&1
warp_exit=$?
set -e

ended_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
kill "$watchdog_pid" 2>/dev/null || true
wait "$watchdog_pid" 2>/dev/null || true
watchdog_pid=""
kill "$telemetry_pid" 2>/dev/null || true
wait "$telemetry_pid" 2>/dev/null || true
telemetry_pid=""
disk_after="$(host_free_bytes)"

# ---------------------------------------------------------------------------
# Locate the benchdata warp actually wrote.
#
# CORRECTION: the brief assumed "<benchdata>.csv.zst". v1.6.1 writes
# "<benchdata>.json.zst" for its default aggregated output and only writes
# .csv.zst when --full is given. Rather than hardcode either, accept whichever
# exists and fail loudly if that is neither or both -- a future warp changing
# the extension again must not turn into a silent "no results".
# ---------------------------------------------------------------------------
benchdata=""
if [ "$warp_exit" -eq 0 ]; then
  # Counted, not word-split: $raw contains the repository path, which may well
  # contain a space (it does on the machine this was developed on), so anything
  # that relies on splitting a list of paths on whitespace is wrong here.
  found_count=0
  found_names=""
  for ext in json.zst csv.zst; do
    if [ -f "$raw/$stem.$ext" ]; then
      found_count=$(( found_count + 1 ))
      benchdata="$raw/$stem.$ext"
      found_names="$found_names $stem.$ext"
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
  set +e
  docker run --rm -v "$raw:/results" "$image" \
    analyze --json "/results/$(basename "$benchdata")" >"$analysis_json" 2>>"$warp_log"
  analyze_exit=$?
  set -e
fi

# ---------------------------------------------------------------------------
# Assemble the result. Every value crosses into Python through argv or the
# environment; the heredoc is quoted so bash expands nothing inside it.
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
BENCH_BENCHDATA="$(basename "${benchdata:-}")" \
"$PY" - "$analysis_json" "$telemetry_csv" "$result_json" <<'PY'
"""Assemble one benchmark result from warp's analysis and the telemetry CSV."""
import calendar
import csv
import datetime
import json
import os
import sys

analysis_path, telemetry_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
env = os.environ

warp_exit = int(env["BENCH_WARP_EXIT"])
analyze_exit = int(env["BENCH_ANALYZE_EXIT"])
duration_requested = int(env["BENCH_DURATION"])
client_cpu_limit_pct = float(env["BENCH_CLIENT_CPUS"]) * 100.0
expected_containers = sorted(env["BENCH_EXPECTED_CONTAINERS"].split())
try:
    server_cpu_budget_pct = float(env.get("BENCH_CPU_BUDGET_PCT") or "")
except ValueError:
    server_cpu_budget_pct = None

run = {
    "system": env["BENCH_SYSTEM"],
    "config": env["BENCH_CONFIG"],
    "profile": env["BENCH_PROFILE"],
    "concurrency": int(env["BENCH_CONCURRENCY"]),
    "round": int(env["BENCH_ROUND"]),
    "warp_op": env["BENCH_OP"],
    "warp_args": [a for a in env["BENCH_ARGS"].split("\n") if a],
    "warp_image": env["BENCH_IMAGE"],
    "duration_requested_seconds": duration_requested,
    "bucket": env["BENCH_BUCKET"],
    "endpoint": env["BENCH_ENDPOINT"],
    "client": {
        "container": env["BENCH_CLIENT_CONTAINER"],
        "cpus": env["BENCH_CLIENT_CPUS"],
        "memory": env["BENCH_CLIENT_MEMORY"],
        "on_network": "s3bench",
    },
    "expected_containers": expected_containers,
    "server_cpu_budget_pct": server_cpu_budget_pct,
    "started_at": env["BENCH_STARTED_AT"],
    "ended_at": env["BENCH_ENDED_AT"],
    "benchdata_file": env["BENCH_BENCHDATA"] or None,
    "host_disk_free_bytes_before": int(env["BENCH_DISK_BEFORE"]),
    "host_disk_free_bytes_after": int(env["BENCH_DISK_AFTER"]),
}

checks = []


def check(name, ok, severity, detail):
    checks.append({"name": name, "ok": bool(ok), "severity": severity, "detail": detail})
    return bool(ok)


def mean(values):
    return sum(values) / len(values) if values else None


def parse_rfc3339_epoch(value):
    """Seconds-resolution epoch from warp's RFC3339 stamps.

    warp emits nanosecond fractions ("...T05:07:51.28434372Z"), and the number
    of fractional digits varies between fields, which datetime.fromisoformat
    has historically been picky about. Telemetry is sampled once a second, so
    dropping the fraction costs nothing and cannot raise.
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1]
    text = text.split(".", 1)[0]
    try:
        parsed = datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return calendar.timegm(parsed.timetuple())


analysis = None
metrics = None
if warp_exit == 0 and analyze_exit == 0:
    try:
        with open(analysis_path) as fh:
            analysis = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        analysis = None
        run["analysis_error"] = str(exc)

# The telemetry collector starts before warp and stops after it, so its samples
# also cover warp's prepare phase (which uploads every --objects object) and its
# cleanup. Averaging CPU over all of that would answer "what did this container
# cost while warp was attached to it", not "what did it cost to serve the
# workload we are publishing numbers for" -- and the two differ a lot: the
# verification run spent 17 of its 86 seconds preparing 2 GiB of objects before
# the measured window even opened. Restrict the aggregates to the window warp
# says it measured.
window_start = window_end = None
window_source = "whole run (no warp analysis to bound it)"
if analysis is not None:
    window_start = parse_rfc3339_epoch((analysis.get("total") or {}).get("start_time"))
    window_end = parse_rfc3339_epoch((analysis.get("total") or {}).get("end_time"))
    if window_start is not None and window_end is not None and window_end > window_start:
        window_source = "warp total.start_time..total.end_time"
    else:
        window_start = window_end = None


# --- telemetry -------------------------------------------------------------
# CPU percentages are summed across a system's containers at each sample
# instant before being averaged, because the 6 CPU / 2048m budget is per
# SYSTEM: SeaweedFS config-2 spreads it over five containers and MinIO puts it
# all in one, so a per-container mean would not be comparable between them.
def load_telemetry(path, start, end):
    per_container = {}
    per_instant = {}
    rows = 0
    kept = 0
    try:
        with open(path, newline="") as fh:
            for row in csv.DictReader(fh):
                rows += 1
                role = row.get("role") or ""
                name = row.get("container") or ""
                epoch = row.get("epoch") or ""
                if not role or not name:
                    continue
                if start is not None:
                    try:
                        when = int(epoch)
                    except (TypeError, ValueError):
                        continue
                    if when < start or when > end:
                        continue
                kept += 1
                slot = per_container.setdefault(
                    (role, name), {"samples": 0, "cpu": [], "mem": [], "mem_limit": None}
                )
                slot["samples"] += 1
                try:
                    cpu = float(row["cpu_pct"])
                except (TypeError, ValueError, KeyError):
                    cpu = None
                else:
                    slot["cpu"].append(cpu)
                try:
                    memv = int(row["mem_bytes"])
                except (TypeError, ValueError, KeyError):
                    memv = None
                else:
                    slot["mem"].append(memv)
                # max, not last: the final sample of an exiting container
                # reports a limit of 0, which would otherwise overwrite the
                # real limit and make the result look unbounded.
                try:
                    limit = int(row["mem_limit_bytes"])
                except (TypeError, ValueError, KeyError):
                    limit = None
                if limit:
                    slot["mem_limit"] = max(slot["mem_limit"] or 0, limit)
                # Keyed by container inside the instant, not summed straight
                # in. The point of the instant bucket is to add up a system's
                # containers at one moment -- SeaweedFS config-2 spreads the
                # 6 CPU / 2048m budget over five of them -- but the collector
                # can occasionally land two samples of the SAME container in
                # one integer second, and adding those together doubles it.
                # That is not hypothetical: it produced a 1195% mean against a
                # 600% cgroup cap and a 2484 MiB peak against a 2048 MiB limit
                # from a CSV whose own maximum was 615% and 2042 MiB.
                inst = per_instant.setdefault((role, epoch), {})
                inst[name] = {"cpu": cpu, "mem": memv}
    except OSError:
        return None, rows, kept
    return (per_container, per_instant), rows, kept


loaded, telemetry_rows, telemetry_kept = load_telemetry(telemetry_path, window_start, window_end)
telemetry = {
    "csv": os.path.basename(telemetry_path),
    "rows_total": telemetry_rows,
    "rows_in_window": telemetry_kept,
    "window": {
        "source": window_source,
        "start_epoch": window_start,
        "end_epoch": window_end,
    },
}
observed_server = []
if loaded is None:
    telemetry["error"] = "telemetry CSV could not be read"
else:
    per_container, per_instant = loaded
    for role in ("server", "client"):
        instants = [v for (r, _), v in per_instant.items() if r == role]
        cpus = [
            sum(c["cpu"] for c in inst.values() if c["cpu"] is not None)
            for inst in instants
            if any(c["cpu"] is not None for c in inst.values())
        ]
        mems = [
            sum(c["mem"] for c in inst.values() if c["mem"] is not None)
            for inst in instants
            if any(c["mem"] is not None for c in inst.values())
        ]
        containers = {}
        for (r, name), slot in sorted(per_container.items()):
            if r != role:
                continue
            containers[name] = {
                "samples": slot["samples"],
                "cpu_pct_mean": mean(slot["cpu"]),
                "cpu_pct_max": max(slot["cpu"]) if slot["cpu"] else None,
                "mem_bytes_mean": mean(slot["mem"]),
                "mem_bytes_peak": max(slot["mem"]) if slot["mem"] else None,
                "mem_limit_bytes": slot["mem_limit"],
            }
        block = {
            "samples": len(instants),
            "containers": sorted(containers),
            "cpu_pct_sum_mean": mean(cpus),
            "cpu_pct_sum_max": max(cpus) if cpus else None,
            "mem_bytes_sum_mean": mean(mems),
            "mem_bytes_sum_peak": max(mems) if mems else None,
            "per_container": containers,
        }
        if role == "server" and server_cpu_budget_pct:
            # docker's own per-sample CPU accounting still overshoots
            # occasionally even when derived from the cumulative counter (the
            # daemon can serve a stats payload whose "read" stamp is closer to
            # the previous one than the counters it carries). Those samples are
            # left in the CSV and counted here rather than silently averaged
            # in, so a reader can judge the mean for themselves. The cgroup
            # cannot burst: cpu.max has no burst budget configured.
            block["cpu_pct_budget"] = server_cpu_budget_pct
            over = [c for c in cpus if c > server_cpu_budget_pct]
            block["cpu_pct_samples_over_budget"] = len(over)
            block["cpu_pct_fraction_over_budget"] = (
                len(over) / len(cpus) if cpus else None
            )
            block["cpu_pct_max_over_budget_ratio"] = (
                max(cpus) / server_cpu_budget_pct if cpus else None
            )
        if role == "client":
            block["cpu_pct_limit"] = client_cpu_limit_pct
            block["cpu_saturation_mean"] = (
                block["cpu_pct_sum_mean"] / client_cpu_limit_pct
                if block["cpu_pct_sum_mean"] is not None and client_cpu_limit_pct
                else None
            )
        else:
            observed_server = sorted(containers)
        telemetry[role] = block

# --- warp analysis ---------------------------------------------------------
# Schema notes for anyone reading this later (warp v1.6.1, analysis "v": 2):
#   total.throughput.{bytes,objects,ops,measure_duration_millis}
#   total.throughput.segmented.{median_bps,median_ops,fastest_bps,slowest_bps,...}
#   by_op_type.<OP>.throughput.*                        same shape, per operation
#   by_op_type.<OP>.requests_by_client.<CLIENT>[]       10-second windows, each
#     .single_sized_requests.{dur_avg_millis,dur_median_millis,dur_90_millis,
#                             dur_99_millis,fastest_millis,slowest_millis}
#     .single_sized_requests.first_byte.{average_millis,fastest_millis,
#         p25_millis,median_millis,p75_millis,p90_millis,p99_millis,
#         slowest_millis,std_dev_millis}
#     .multi_sized_requests.by_size[].first_byte.{...}  when sizes vary
# There is NO pre-merged latency figure anywhere in the JSON. warp's own
# printed report merges the windows with a plain UNWEIGHTED mean of each
# percentile (min of fastest, max of slowest) -- verified numerically against a
# real run's console output to the digit -- so that is what merge_windows does,
# which keeps these numbers reproducible by running warp by hand.
PERCENTILE_KEYS = (
    "average_millis", "p25_millis", "median_millis", "p75_millis",
    "p90_millis", "p99_millis", "std_dev_millis",
)
DURATION_KEYS = (
    "dur_avg_millis", "dur_median_millis", "dur_90_millis", "dur_99_millis",
    "std_dev_millis",
)


def merge_windows(blocks, keys):
    if not blocks:
        return None
    out = {}
    for key in keys:
        vals = [b[key] for b in blocks if isinstance(b.get(key), (int, float))]
        out[key] = mean(vals)
    fastest = [b["fastest_millis"] for b in blocks if isinstance(b.get("fastest_millis"), (int, float))]
    slowest = [b["slowest_millis"] for b in blocks if isinstance(b.get("slowest_millis"), (int, float))]
    out["fastest_millis"] = min(fastest) if fastest else None
    out["slowest_millis"] = max(slowest) if slowest else None
    out["windows"] = len(blocks)
    return out


def rate(value, millis):
    if not isinstance(value, (int, float)) or not millis:
        return None
    return value * 1000.0 / millis


def throughput_metrics(block):
    th = block.get("throughput") or {}
    millis = th.get("measure_duration_millis") or 0
    seg = th.get("segmented") or {}
    return {
        "measure_duration_seconds": millis / 1000.0 if millis else None,
        "requests": block.get("total_requests"),
        "objects": block.get("total_objects"),
        "bytes": block.get("total_bytes"),
        "errors": block.get("total_errors"),
        "bytes_per_sec_avg": rate(th.get("bytes"), millis),
        "obj_per_sec_avg": rate(th.get("objects"), millis),
        "bytes_per_sec_median": seg.get("median_bps"),
        "bytes_per_sec_fastest": seg.get("fastest_bps"),
        "bytes_per_sec_slowest": seg.get("slowest_bps"),
        "obj_per_sec_median": seg.get("median_ops"),
        "obj_per_sec_fastest": seg.get("fastest_ops"),
        "obj_per_sec_slowest": seg.get("slowest_ops"),
        "segments": len(seg.get("segments") or []),
    }


def latency_metrics(block):
    ttfb_blocks, dur_blocks, sized = [], [], set()
    for windows in (block.get("requests_by_client") or {}).values():
        for window in windows or []:
            single = window.get("single_sized_requests")
            multi = window.get("multi_sized_requests")
            if single:
                sized.add("single")
                dur_blocks.append(single)
                if single.get("first_byte"):
                    ttfb_blocks.append(single["first_byte"])
            elif multi:
                sized.add("multi")
                for by_size in multi.get("by_size") or []:
                    if by_size.get("first_byte"):
                        ttfb_blocks.append(by_size["first_byte"])
    return {
        "ttfb_millis": merge_windows(ttfb_blocks, PERCENTILE_KEYS),
        "request_millis": merge_windows(dur_blocks, DURATION_KEYS),
        "object_sizing": ("mixed" if len(sized) > 1 else (sized.pop() if sized else None)),
        "merge_rule": "unweighted mean across warp's aggregation windows; matches warp's own printed report",
    }


if analysis is not None:
    total = analysis.get("total") or {}
    by_op = analysis.get("by_op_type") or {}
    metrics = {
        "analysis_schema_version": analysis.get("v"),
        "total": throughput_metrics(total),
        "by_op": {},
    }
    for op_name, block in sorted(by_op.items()):
        entry = throughput_metrics(block)
        entry.update(latency_metrics(block))
        metrics["by_op"][op_name] = entry

# --- checks ----------------------------------------------------------------
check("warp_exit_zero", warp_exit == 0, "fatal", "warp exit code %d" % warp_exit)
check("analysis_parsed", analysis is not None, "fatal",
      "warp analyze --json produced a readable document" if analysis is not None
      else "no readable analysis (analyze exit %d)" % analyze_exit)

if metrics is not None:
    measured = metrics["total"]["measure_duration_seconds"] or 0.0
    errors = metrics["total"]["errors"]
    check("no_operation_errors", errors == 0, "fatal", "warp reported %s operation error(s)" % errors)
    check("measured_window_plausible", measured >= 0.4 * duration_requested, "fatal",
          "measured %.1fs of a requested %ds window (warp trims its own ramp-up/down)"
          % (measured, duration_requested))
    check("work_was_done", (metrics["total"]["requests"] or 0) > 0, "fatal",
          "%s requests recorded" % metrics["total"]["requests"])

server = telemetry.get("server") or {}
client = telemetry.get("client") or {}
check("telemetry_has_samples", (server.get("samples") or 0) > 0, "fatal",
      "%s server telemetry sample instants" % server.get("samples"))
check("telemetry_containers_complete", observed_server == expected_containers, "fatal",
      "sampled %s; expected %s" % (observed_server or "nothing", expected_containers))
check("telemetry_client_sampled", (client.get("samples") or 0) > 0, "warn",
      "%s client telemetry sample instants" % client.get("samples"))

# A cgroup with no burst budget cannot exceed its quota, so anything above it
# is measurement error. A couple of percent either side is ordinary interval
# jitter and is tolerated; a sample half again over the cap is not jitter, it
# means the aggregation is wrong (an earlier version of this script summed two
# samples of the same container landing in the same second, which showed up
# here as exactly that).
over_fraction = server.get("cpu_pct_fraction_over_budget")
over_ratio = server.get("cpu_pct_max_over_budget_ratio")
check("telemetry_cpu_plausible",
      (over_fraction is None or over_fraction <= 0.25)
      and (over_ratio is None or over_ratio <= 1.25),
      "warn",
      "%s of server CPU samples exceeded the %s%% cgroup budget; worst sample was %s of it"
      % ("unknown fraction" if over_fraction is None else ("%.0f%%" % (over_fraction * 100)),
         server.get("cpu_pct_budget"),
         "unknown" if over_ratio is None else ("%.2fx" % over_ratio)))

saturation = client.get("cpu_saturation_mean")
check("client_not_saturated", saturation is None or saturation < 0.90, "warn",
      "warp averaged %s of its %.0f%% CPU budget; above ~0.90 the number is the client's, not the server's"
      % ("unknown" if saturation is None else ("%.2f" % saturation), client_cpu_limit_pct))

fatal_failures = [c["name"] for c in checks if c["severity"] == "fatal" and not c["ok"]]
if warp_exit != 0 or analysis is None:
    status = "failed"
elif fatal_failures:
    status = "suspect"
else:
    status = "ok"

result = {
    "schema": 1,
    "status": status,
    "failed_checks": fatal_failures,
    "hardware_profile": json.loads(env["BENCH_HW"]),
    "run": run,
    "checks": checks,
    "telemetry": telemetry,
}
# No "metrics" key at all on a failed run: an absent number cannot be mistaken
# for a measured one, whereas a zero or a null inside a populated metrics block
# can.
if metrics is not None:
    result["metrics"] = metrics
    result["warp"] = analysis

with open(out_path, "w") as fh:
    json.dump(result, fh, indent=2, sort_keys=False)
    fh.write("\n")

print("%s  status=%s" % (out_path, status))
for c in checks:
    if not c["ok"]:
        print("  %-8s %s: %s" % (c["severity"].upper(), c["name"], c["detail"]), file=sys.stderr)
sys.exit(0 if status == "ok" else 1)
PY
merge_exit=$?
set -e

# The analysis is embedded in the result file; the standalone copy would only
# be a second, divergeable source of the same bytes. It is kept on failure,
# where it is the evidence.
if [ "$merge_exit" -eq 0 ]; then
  rm -f "$analysis_json"
fi

exit "$merge_exit"
