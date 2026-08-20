#!/usr/bin/env bash
# Does SeaweedFS complete the bigdata-get workload if you give it more memory?
#
# Usage: diagnose-seaweedfs-memory.sh [memory-limit]     (default 4096m)
#
# WHY THIS IS NOT A BENCHMARK
# ---------------------------
# SeaweedFS is the only system in the study that cannot finish `bigdata-get`:
# `weed server` is OOM-killed (exit 137) partway through warp's PREPARE phase,
# uploading the 200 x 20 MiB dataset, in both configurations. That leaves a dead
# cell in the matrix and a reader with no way to tell "too slow" from "would not
# run", which are very different things to know about a storage system.
#
# This script answers the one question that turns the dead cell into a usable
# fact: how much memory does it need? It runs the same profile against the same
# pinned image with a DIFFERENT memory budget, so its numbers are not comparable
# with anything in results/<profile>/raw/ and must never be aggregated with
# them. That is why the output goes to results/<profile>/diagnostics/ instead,
# carries "kind": "diagnostic", and records the budget it ran under next to the
# 2048m the matrix uses.
#
# The throughput it reports is worthless as a comparison -- a system given twice
# the memory of its competitors is not being compared with them. The only
# publishable output is the pass/fail and the budget it needed.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYSTEM="seaweedfs"
CONFIG="c1"
PROFILE="bigdata-get"
CONCURRENCY=32
NETWORK="s3bench"
CONTAINER="bench-seaweedfs"
MEM_LIMIT="${1:-4096m}"

die() { echo "diagnose: $*" >&2; exit 1; }

[[ "$MEM_LIMIT" =~ ^[0-9]+[bkmgBKMG]?$ ]] || die "memory limit '$MEM_LIMIT' is not a docker size"

PY="${BENCH_PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"; else PY="python3"; fi
fi
"$PY" -c 'import yaml' 2>/dev/null || die "'$PY' cannot import yaml"

hw="$("$ROOT/bench/hwprofile.sh")" || die "hwprofile.sh failed"
pid="$(BENCH_HW="$hw" "$PY" -c 'import json,os; print(json.loads(os.environ["BENCH_HW"])["profile_id"])')"
out_dir="$ROOT/results/$pid/diagnostics"
mkdir -p "$out_dir"
stem="seaweedfs-memory-${MEM_LIMIT}"
warp_log="$out_dir/$stem.warp.log"
telemetry_csv="$out_dir/$stem.telemetry.csv"
out_json="$out_dir/$stem.json"

# The workload comes out of the same file the matrix uses, so the diagnostic
# cannot drift away from the profile it is diagnosing.
plan="$(BENCH_WORKLOADS="$ROOT/bench/workloads.yaml" BENCH_PROFILE="$PROFILE" "$PY" - <<'PY'
import os, sys, yaml
cfg = yaml.safe_load(open(os.environ["BENCH_WORKLOADS"]))
p = {x["id"]: x for x in cfg["profiles"]}[os.environ["BENCH_PROFILE"]]
d, w, c = cfg["defaults"], cfg["warp"], cfg["credentials"]
out = [("op", p["op"]),
       ("duration_seconds", p.get("duration_seconds", d["duration_seconds"])),
       ("bucket", p.get("bucket", d["bucket"])),
       ("image", w["image"]),
       ("client_cpus", w["client"]["cpus"]),
       ("client_memory", w["client"]["memory"]),
       ("access_key", c["access_key"]),
       ("secret_key", c["secret_key"])]
out += [("arg", a) for a in w.get("common_args", [])]
out += [("arg", a) for a in p.get("args", [])]
sys.stdout.write("".join(k + "\x1f" + str(v) + "\n" for k, v in out))
PY
)" || die "could not read profile '$PROFILE' from workloads.yaml"

op=""; duration_seconds=""; bucket=""; image=""; client_cpus=""; client_memory=""
access_key=""; secret_key=""; warp_args=()
while IFS=$'\x1f' read -r key value; do
  case "$key" in
    op) op="$value" ;; duration_seconds) duration_seconds="$value" ;;
    bucket) bucket="$value" ;; image) image="$value" ;;
    client_cpus) client_cpus="$value" ;; client_memory) client_memory="$value" ;;
    access_key) access_key="$value" ;; secret_key) secret_key="$value" ;;
    arg) warp_args[${#warp_args[@]}]="$value" ;;
    "") ;;
    *) die "unexpected plan key '$key'" ;;
  esac
done <<< "$plan"

echo "=== seaweedfs memory diagnostic: $PROFILE at $MEM_LIMIT (matrix budget: 2048m) ==="
"$ROOT/bench/stack.sh" up "$SYSTEM" "$CONFIG"

cleanup() {
  [ -n "${telemetry_pid:-}" ] && kill "$telemetry_pid" 2>/dev/null || true
  docker rm -f bench-warp >/dev/null 2>&1 || true
  "$ROOT/bench/stack.sh" down "$SYSTEM" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Raise the ceiling on the running container. Changing compose would change the
# budget the matrix runs under, which is the one thing that must not move.
docker update --memory "$MEM_LIMIT" --memory-swap "$MEM_LIMIT" "$CONTAINER" >/dev/null \
  || die "docker update could not raise the memory limit"
applied="$(docker inspect --format '{{.HostConfig.Memory}}' "$CONTAINER")"
baseline="$(BENCH_ROOT="$ROOT" "$PY" -c '
import os, yaml
cfg = yaml.safe_load(open(os.path.join(os.environ["BENCH_ROOT"], "compose", "seaweedfs.yaml")))
print(cfg["services"]["seaweedfs"]["mem_limit"])')"
echo "-- memory limit now ${applied} bytes (compose declares $baseline)"
[ "$applied" -gt 0 ] || die "memory limit did not take effect"

TELEMETRY_CLIENT_CONTAINER=bench-warp BENCH_PYTHON="$PY" \
  "$ROOT/bench/telemetry.sh" "$SYSTEM" "$telemetry_csv" &
telemetry_pid=$!

started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
set +e
docker run --rm --name bench-warp --network "$NETWORK" \
  --cpus "$client_cpus" --memory "$client_memory" \
  "$image" "$op" \
  --host="${CONTAINER}:9000" --access-key="$access_key" --secret-key="$secret_key" \
  --bucket="$bucket" --concurrent="$CONCURRENCY" --duration="${duration_seconds}s" \
  ${warp_args[@]+"${warp_args[@]}"} >"$warp_log" 2>&1
warp_exit=$?
set -e
ended_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
kill "$telemetry_pid" 2>/dev/null || true; wait "$telemetry_pid" 2>/dev/null || true
telemetry_pid=""

state="$(docker inspect --format '{{.State.Status}} {{.State.OOMKilled}} {{.State.ExitCode}} {{.RestartCount}}' "$CONTAINER" 2>/dev/null || echo "gone true - -")"
read -r c_status c_oom c_exit c_restarts <<< "$state"
echo "-- warp exit $warp_exit; container status=$c_status oom_killed=$c_oom exit=$c_exit"

BENCH_HW="$hw" BENCH_OUT="$out_json" BENCH_TELEMETRY="$telemetry_csv" \
BENCH_WARP_LOG="$warp_log" BENCH_MEM_APPLIED="$applied" BENCH_MEM_BASELINE="$baseline" \
BENCH_MEM_REQUESTED="$MEM_LIMIT" BENCH_WARP_EXIT="$warp_exit" BENCH_OOM="$c_oom" \
BENCH_EXIT_CODE="$c_exit" BENCH_STATUS="$c_status" BENCH_STARTED="$started_at" \
BENCH_ENDED="$ended_at" BENCH_PROFILE_NAME="$PROFILE" BENCH_CONCURRENCY="$CONCURRENCY" \
"$PY" - <<'PY'
import csv, json, os

env = os.environ
peak = {}
try:
    with open(env["BENCH_TELEMETRY"]) as handle:
        for row in csv.DictReader(handle):
            name = row.get("container")
            try:
                mem = int(row["mem_bytes"])
            except (KeyError, TypeError, ValueError):
                continue
            if name and mem > peak.get(name, 0):
                peak[name] = mem
except OSError:
    pass

warp_exit = int(env["BENCH_WARP_EXIT"])
oom = env["BENCH_OOM"] == "true"
doc = {
    "kind": "diagnostic",
    "not_a_benchmark_result": True,
    "why": ("SeaweedFS is OOM-killed during warp's prepare phase for the "
            "bigdata-get profile under the study's 2048m per-system budget, in "
            "both configurations, so that cell of the matrix has no number. "
            "This run repeats the same profile against the same pinned image "
            "with a DIFFERENT memory budget to establish what it needs. It ran "
            "under conditions no other system was given and must never be "
            "compared with, or aggregated into, results/<profile>/raw/."),
    "system": "seaweedfs",
    "config": "c1",
    "profile": env["BENCH_PROFILE_NAME"],
    "concurrency": int(env["BENCH_CONCURRENCY"]),
    "memory_budget": {
        "matrix_budget": env["BENCH_MEM_BASELINE"],
        "diagnostic_requested": env["BENCH_MEM_REQUESTED"],
        "diagnostic_applied_bytes": int(env["BENCH_MEM_APPLIED"]),
    },
    "outcome": {
        "completed": warp_exit == 0 and not oom,
        "warp_exit": warp_exit,
        "container_status": env["BENCH_STATUS"],
        "container_oom_killed": oom,
        "container_exit_code": env["BENCH_EXIT_CODE"],
    },
    "peak_memory_bytes": peak,
    "started_at": env["BENCH_STARTED"],
    "ended_at": env["BENCH_ENDED"],
    "hardware_profile": json.loads(env["BENCH_HW"]),
    "artifacts": {
        "warp_log": os.path.basename(env["BENCH_WARP_LOG"]),
        "telemetry_csv": os.path.basename(env["BENCH_TELEMETRY"]),
    },
}
with open(env["BENCH_OUT"], "w") as handle:
    json.dump(doc, handle, indent=2, sort_keys=True)
    handle.write("\n")
print("%s  completed=%s oom_killed=%s peak=%s"
      % (env["BENCH_OUT"], doc["outcome"]["completed"], oom,
         ", ".join("%s %.0f MiB" % (k, v / 1048576) for k, v in sorted(peak.items()))))
PY
