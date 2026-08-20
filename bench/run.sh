#!/usr/bin/env bash
# Drive the whole benchmark matrix unattended: every system, in every config,
# across every workload profile, for every round, plus the concurrency sweep.
#
# Usage: run.sh [--quick] [--force] [--systems a,b] [--profiles a,b]
#
# ---------------------------------------------------------------------------
# ROUND-ROBIN ORDER
# ---------------------------------------------------------------------------
# Systems cycle within each round rather than each system running its whole
# matrix back to back. Running one system to completion before starting the
# next lets thermal throttling accumulate against whoever runs last; cycling
# spreads that penalty evenly, and taking the median across rounds removes most
# of what is left.
#
# ---------------------------------------------------------------------------
# WHAT THIS AGGREGATES ON: `status`, NEVER `$?`
# ---------------------------------------------------------------------------
# bench/run-workload.sh exits 0 for BOTH "ok" and "ok_with_warnings", by
# design, so that an unattended matrix keeps going past a run whose numbers are
# real but whose load generator may itself have been the bottleneck. A driver
# that branched on the exit code would therefore file a client-bottlenecked run
# as clean. This script never does that: after every measurement it reads the
# `status` field out of the result JSON and records that, together with the
# run's `failed_checks`. Handling, per bench/run-workload.sh's vocabulary:
#
#   ok                 record, continue
#   ok_with_warnings   record WITH its failed_checks, continue, list it in the
#                      summary -- these are not "ok" and must not be merged
#                      into the ok count anywhere
#   suspect            a fatal check failed; record, continue, flag loudly
#   failed             warp did not run or its analysis is unreadable; the file
#                      carries no metrics at all
#
# Two states exist that run-workload.sh cannot express because they happen
# before it produces a file, and this script names them rather than losing them:
#
#   no_result          run-workload.sh exited without writing a result JSON
#                      (its own preflight refused: disk floor, stack mismatch,
#                      missing image). Nothing was measured.
#   stack_failed       the system never came up in that config.
#
# ---------------------------------------------------------------------------
# TEARDOWN CADENCE
# ---------------------------------------------------------------------------
# The stack comes down after EVERY measurement, not just between systems. One
# `medium` run was measured consuming 4.07 GiB of host disk, and Docker Desktop
# reclaims the space asynchronously -- `docker system prune` during Task 5
# returned nothing at all. With ~30 GiB free and a 10 GiB floor only four or
# five runs fit before run-workload.sh's own guard starts refusing to start.
# So: down after each run, then poll until free space is back above the floor
# before the next one, with a bounded wait and an abort if it never recovers.
# The reclaim path that actually works is `stack.sh down -v` dropping the
# volumes and the Docker Desktop VM eventually TRIMming its sparse disk image;
# that is what the poll is waiting for. Image pruning is deliberately NOT part
# of it -- every system image here is referenced by digest and carries no tag,
# and no prune we could run frees host space in time to matter.
#
# ---------------------------------------------------------------------------
# RESUMABILITY
# ---------------------------------------------------------------------------
# A full matrix is hours long and this machine has already lost one long job to
# sleep. So the driver is idempotent: a combination whose result JSON already
# exists is skipped, its recorded status read off disk and folded into the
# summary, so an interrupted run resumes instead of restarting. `--force`
# re-runs everything, deleting the stale artifacts first so a re-run that dies
# before warp cannot leave the previous status behind to be misread as this
# one's. To re-queue one combination, delete its result JSON.
#
# ---------------------------------------------------------------------------
# SLEEP
# ---------------------------------------------------------------------------
# On macOS the whole run re-execs under `caffeinate -dimsu`, which holds the
# machine awake only for as long as this script runs and changes nothing about
# the user's power settings. Guarded so contributors on Linux are unaffected.
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$SELF_DIR/$(basename "${BASH_SOURCE[0]}")"
ROOT="$(cd "$SELF_DIR/.." && pwd)"

ALL_SYSTEMS="minio silo rustfs seaweedfs"
ALL_CONFIGS="c1 c2"
QUICK_PROFILE_COUNT=3

# Unit separator. Same reason bench/run-workload.sh splits its plan on a tab:
# a field separator that cannot occur inside a value, so an empty field stays
# an empty field instead of being swallowed by the default IFS.
RS=$'\x1f'

MIN_FREE_GIB="${BENCH_MIN_FREE_GIB:-10}"
DISK_WAIT_SECONDS="${BENCH_DISK_WAIT_SECONDS:-600}"
DISK_POLL_SECONDS="${BENCH_DISK_POLL_SECONDS:-15}"

usage() {
  cat >&2 <<'USAGE'
usage: run.sh [--quick] [--force] [--systems a,b,...] [--profiles a,b,...]

  --quick       one round, the first three workload profiles, no sweep.
  --force       re-run combinations that already have a result file.
  --dry-run     print the matrix this invocation would run, and stop.
  --systems     restrict to these systems (comma separated).
  --profiles    restrict to these workload profiles (comma separated).
  -h, --help    this message.

Environment:
  BENCH_MIN_FREE_GIB       host free-space floor before each run (default 10)
  BENCH_DISK_WAIT_SECONDS  how long to wait for space to come back (default 600)
  BENCH_DISK_POLL_SECONDS  poll interval for that wait (default 15)
  BENCH_RUN_TIMEOUT        per-measurement watchdog, passed through (default 900)
  BENCH_PYTHON             python interpreter with PyYAML
  BENCH_NO_CAFFEINATE=1    do not re-exec under caffeinate on macOS
USAGE
  exit "${1:-2}"
}

die() { echo "run.sh: $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Arguments. Unknown flags are rejected rather than ignored: a driver that
# silently accepted `--quik` would start a four-hour matrix when a twenty-five
# minute smoke test was asked for.
# ---------------------------------------------------------------------------
QUICK=0
FORCE=0
DRY_RUN=0
SYSTEMS_SEL=""
PROFILES_SEL=""
# Kept because the argument loop consumes "$@" and the caffeinate re-exec below
# has to replay the invocation verbatim.
ORIG_ARGV=(${1+"$@"})
while [ "$#" -gt 0 ]; do
  case "$1" in
    --quick)      QUICK=1 ;;
    --force)      FORCE=1 ;;
    --dry-run)    DRY_RUN=1 ;;
    --systems)    shift; [ "$#" -gt 0 ] || { echo "run.sh: --systems needs a value" >&2; usage; }; SYSTEMS_SEL="$1" ;;
    --systems=*)  SYSTEMS_SEL="${1#*=}" ;;
    --profiles)   shift; [ "$#" -gt 0 ] || { echo "run.sh: --profiles needs a value" >&2; usage; }; PROFILES_SEL="$1" ;;
    --profiles=*) PROFILES_SEL="${1#*=}" ;;
    -h|--help)    usage 0 ;;
    *)            echo "run.sh: unknown argument '$1'" >&2; usage ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# Sleep protection. `pmset` on this machine sleeps the system after 1 minute on
# both AC and battery, which would strand a multi-hour run. Rather than change
# the user's settings, hold an assertion for exactly the lifetime of this
# process. The env guard makes the re-exec idempotent.
#
# What the flags actually buy: -i (no idle sleep) and -s (no system sleep on AC)
# are the ones that last for the whole run. -d only keeps the display on and -u
# declares user activity for a default of five seconds, so neither is load
# bearing; they are kept because they are harmless and make the intent obvious.
#
# KNOWN LIMIT: none of this survives closing the lid. A clamshell sleep will
# still strand the run -- the matrix resumes on the next invocation, but the
# in-flight measurement is lost. Leave the lid open.
# ---------------------------------------------------------------------------
CAFFEINATED="${BENCH_CAFFEINATED:-0}"
if [ "$CAFFEINATED" != "1" ] && [ -z "${BENCH_NO_CAFFEINATE:-}" ] \
   && [ "$(uname -s)" = "Darwin" ] && command -v caffeinate >/dev/null 2>&1; then
  export BENCH_CAFFEINATED=1
  exec caffeinate -dimsu "$SELF" ${ORIG_ARGV[@]+"${ORIG_ARGV[@]}"}
fi

# ---------------------------------------------------------------------------
# Interpreter, resolved exactly the way bench/run-workload.sh resolves it and
# then handed down through the environment, so the driver and the measurement
# cannot disagree about which python is in play.
# ---------------------------------------------------------------------------
PY="${BENCH_PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"; else PY="python3"; fi
fi
command -v "$PY" >/dev/null 2>&1 || die "python interpreter '$PY' not found"
"$PY" -c 'import yaml' 2>/dev/null || die "'$PY' cannot import yaml. Install requirements-dev.txt (python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt) or set BENCH_PYTHON."
export BENCH_PYTHON="$PY"

hw="$("$ROOT/bench/hwprofile.sh")" || die "hwprofile.sh failed"
PROFILE_ID="$(BENCH_HW="$hw" "$PY" -c 'import json,os; print(json.loads(os.environ["BENCH_HW"])["profile_id"])')"
[ -n "$PROFILE_ID" ] || die "could not read profile_id from the hardware profile"
RAW="$ROOT/results/$PROFILE_ID/raw"
mkdir -p "$RAW"
LOG="$ROOT/results/$PROFILE_ID/run.log"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Host free space, in GiB, of the filesystem holding this repository -- the one
# that can actually run out. The Docker Desktop VM reports its own virtual disk
# as nearly empty while the sparse image backing it fills the host.
free_gib() { df -k "$ROOT" | awk 'NR==2 { printf "%d", $4 / 1048576 }'; }

reclaim() {
  # Build cache only. See the TEARDOWN CADENCE note: the system images are
  # digest-referenced and untagged, so image pruning is not worth the risk for
  # space it cannot free in time anyway.
  docker builder prune -f >/dev/null 2>&1 || true
}

# GiB that must be free before (profile, config) may start. bench/disk_budget.py
# is the only place this formula lives; bench/run-workload.sh refuses below the
# same number. They must not diverge -- when the driver waited on a flat floor
# while the measurement refused on a profile-scaled one, the driver never waited
# for the space the job actually needed, so a transient dip became a permanent
# no_result instead of a pause, and which cells were lost depended on the order
# they happened to run in.
job_required_gib() {
  "$PY" "$ROOT/bench/disk_budget.py" "$ROOT/bench/workloads.yaml" "$1" "$2" "$MIN_FREE_GIB"
}

# wait_for_disk <required-gib> <label>
wait_for_disk() {
  local required="$1" label="$2" free waited=0
  free="$(free_gib)"
  if [ "$free" -ge "$required" ]; then
    echo "   disk: ${free} GiB free, ${label} needs ${required} GiB"
    return 0
  fi
  echo "   disk: only ${free} GiB free, ${label} needs ${required} GiB -- waiting up to ${DISK_WAIT_SECONDS}s for Docker Desktop to reclaim"
  reclaim
  while [ "$waited" -lt "$DISK_WAIT_SECONDS" ]; do
    sleep "$DISK_POLL_SECONDS"
    waited=$(( waited + DISK_POLL_SECONDS ))
    free="$(free_gib)"
    echo "   disk: ${free} GiB free after ${waited}s (need ${required} GiB)"
    if [ "$free" -ge "$required" ]; then return 0; fi
  done
  return 1
}

# status + failed_checks of one result JSON, as "<status><RS><csv of checks>".
read_result_status() {
  BENCH_RESULT="$1" "$PY" - <<'PY'
import json, os, sys
try:
    d = json.load(open(os.environ["BENCH_RESULT"]))
except Exception as exc:
    sys.stdout.write("unreadable\x1f%s" % exc.__class__.__name__)
else:
    checks = d.get("failed_checks") or []
    sys.stdout.write("%s\x1f%s" % (d.get("status") or "no_status_field",
                                   ",".join(str(c) for c in checks)))
PY
}

records=()
n_ok=0; n_warn=0; n_suspect=0; n_failed=0; n_noresult=0; n_stackfail=0; n_other=0
n_skipped=0; n_attempted=0
measured_seconds=0
started_epoch=0
aborted=""

count_status() {
  case "$1" in
    ok)               n_ok=$(( n_ok + 1 )) ;;
    ok_with_warnings) n_warn=$(( n_warn + 1 )) ;;
    suspect)          n_suspect=$(( n_suspect + 1 )) ;;
    failed)           n_failed=$(( n_failed + 1 )) ;;
    no_result)        n_noresult=$(( n_noresult + 1 )) ;;
    stack_failed)     n_stackfail=$(( n_stackfail + 1 )) ;;
    *)                n_other=$(( n_other + 1 )) ;;
  esac
}

# record <status> <label> <source> <detail>
record() {
  records[${#records[@]}]="$1$RS$2$RS$3$RS$4"
  count_status "$1"
}

summary() {
  local total_planned="${1:-0}" rec st label src detail elapsed mean
  elapsed=$(( $(date +%s) - started_epoch ))
  echo
  echo "==========================================================================="
  echo "=== summary: profile=$PROFILE_ID quick=$QUICK finished=$(now_utc)"
  echo "==========================================================================="
  printf 'wall clock          : %02d:%02d:%02d\n' $(( elapsed / 3600 )) $(( elapsed % 3600 / 60 )) $(( elapsed % 60 ))
  echo "combinations planned: $total_planned"
  echo "  measured this run : $n_attempted"
  echo "  already present   : $n_skipped (skipped; --force re-runs them)"
  if [ "$n_attempted" -gt 0 ]; then
    mean=$(( measured_seconds / n_attempted ))
    printf 'mean seconds/run    : %s (over %s measured runs, stack up+down included)\n' "$mean" "$n_attempted"
  fi
  echo
  echo "status counts across all planned combinations:"
  printf '  %-18s %s\n' "ok"                 "$n_ok"
  printf '  %-18s %s\n' "ok_with_warnings"   "$n_warn"
  printf '  %-18s %s\n' "suspect"            "$n_suspect"
  printf '  %-18s %s\n' "failed"             "$n_failed"
  printf '  %-18s %s\n' "no_result"          "$n_noresult"
  printf '  %-18s %s\n' "stack_failed"       "$n_stackfail"
  [ "$n_other" -gt 0 ] && printf '  %-18s %s\n' "other"  "$n_other"
  echo
  [ -n "$aborted" ] && echo "ABORTED: $aborted"
  if [ "${#records[@]}" -eq 0 ]; then
    echo "nothing was recorded."
  elif [ $(( n_warn + n_suspect + n_failed + n_noresult + n_stackfail + n_other )) -eq 0 ]; then
    echo "every recorded combination reached status=ok."
  else
    echo "not ok -- every combination below needs a human before anything is published:"
    for rec in ${records[@]+"${records[@]}"}; do
      IFS="$RS" read -r st label src detail <<< "$rec"
      [ "$st" = "ok" ] && continue
      printf '  %-17s %-34s %-12s %s\n' "$st" "$label" "($src)" "$detail"
    done
  fi
  echo
  echo "raw results : $RAW/"
  echo "run log     : $LOG"
}

# ---------------------------------------------------------------------------
# Read the plan out of bench/workloads.yaml.
#
# One <key><US><value> line per value, never several fields on one line: a
# single space-separated print with a bare `read` collapses adjacent empty
# fields, so in --quick mode -- where the sweep fields are empty -- the
# concurrency would land in the sweep-profile variable and the driver would
# sweep a profile called "32".
# ---------------------------------------------------------------------------
plan_file="$(mktemp "${TMPDIR:-/tmp}/bench-plan.XXXXXX")"
trap 'rm -f "$plan_file"' EXIT
BENCH_WORKLOADS="$ROOT/bench/workloads.yaml" BENCH_QUICK="$QUICK" \
BENCH_QUICK_COUNT="$QUICK_PROFILE_COUNT" "$PY" - > "$plan_file" <<'PY'
import os, sys, yaml

cfg = yaml.safe_load(open(os.environ["BENCH_WORKLOADS"]))
quick = os.environ["BENCH_QUICK"] == "1"
# Every profile is emitted, always. --quick's truncation to the first few is
# applied by the caller and ONLY when --profiles was not given: --quick means
# "one round, no sweep, a short default set", and an explicit --profiles is a
# deliberate choice that must be able to name any profile in the file. Without
# that split there is no way to smoke-test the profiles --quick leaves out
# except by running three full rounds of them.
ids = [p["id"] for p in cfg["profiles"]]

out = [("rounds", 1 if quick else cfg["rounds"]),
       ("default_concurrency", cfg["defaults"]["concurrency"]),
       ("warp_image", cfg["warp"]["image"]),
       ("sweep_profile", "" if quick else cfg["sweep"]["profile"])]
out += [("profile", i) for i in ids]
out.append(("quick_profile_count", os.environ["BENCH_QUICK_COUNT"]))
if not quick:
    out += [("sweep_concurrency", c) for c in cfg["sweep"]["concurrency"]]

for key, value in out:
    value = str(value)
    if "\x1f" in value or "\n" in value:
        sys.exit("run.sh: value for %r contains a separator: %r" % (key, value))
    sys.stdout.write(key + "\x1f" + value + "\n")
PY

rounds=""; default_concurrency=""; warp_image=""; sweep_profile=""; quick_profile_count=""
profiles=()
sweep_concurrency=()
while IFS="$RS" read -r key value; do
  case "$key" in
    rounds)              rounds="$value" ;;
    default_concurrency) default_concurrency="$value" ;;
    warp_image)          warp_image="$value" ;;
    sweep_profile)       sweep_profile="$value" ;;
    profile)             profiles[${#profiles[@]}]="$value" ;;
    quick_profile_count) quick_profile_count="$value" ;;
    sweep_concurrency)   sweep_concurrency[${#sweep_concurrency[@]}]="$value" ;;
    "")                  ;;
    *)                   die "unexpected plan key '$key'" ;;
  esac
done < "$plan_file"
rm -f "$plan_file"
trap - EXIT

[ -n "$rounds" ] && [ -n "$default_concurrency" ] && [ "${#profiles[@]}" -gt 0 ] \
  || die "could not read a usable plan out of bench/workloads.yaml"

# ---------------------------------------------------------------------------
# Apply --systems / --profiles, validating against what actually exists.
# ---------------------------------------------------------------------------
systems=()
if [ -n "$SYSTEMS_SEL" ]; then
  IFS=',' read -r -a want_systems <<< "$SYSTEMS_SEL"
  for s in ${want_systems[@]+"${want_systems[@]}"}; do
    case " $ALL_SYSTEMS " in
      *" $s "*) systems[${#systems[@]}]="$s" ;;
      *) die "unknown system '$s' (expected one of: $ALL_SYSTEMS)" ;;
    esac
  done
else
  for s in $ALL_SYSTEMS; do systems[${#systems[@]}]="$s"; done
fi

if [ -n "$PROFILES_SEL" ]; then
  all_profiles=" ${profiles[*]} "
  IFS=',' read -r -a want_profiles <<< "$PROFILES_SEL"
  selected=()
  for p in ${want_profiles[@]+"${want_profiles[@]}"}; do
    case "$all_profiles" in
      *" $p "*) selected[${#selected[@]}]="$p" ;;
      *) die "unknown profile '$p' (workloads.yaml has: ${profiles[*]})" ;;
    esac
  done
  profiles=(${selected[@]+"${selected[@]}"})
elif [ "$QUICK" = "1" ]; then
  # --quick's default short set: the first few profiles, in file order.
  selected=()
  for p in ${profiles[@]+"${profiles[@]}"}; do
    [ "${#selected[@]}" -lt "$quick_profile_count" ] || break
    selected[${#selected[@]}]="$p"
  done
  profiles=(${selected[@]+"${selected[@]}"})
fi

if [ -n "$PROFILES_SEL" ]; then
  # The sweep runs on one profile; if that profile was filtered out, so is the sweep.
  case " ${profiles[*]} " in
    *" $sweep_profile "*) ;;
    *) sweep_profile="" ;;
  esac
fi

# ---------------------------------------------------------------------------
# Build the job list, round-robin: round -> system -> config -> profile.
# ---------------------------------------------------------------------------
jobs=()
for round in $(seq 1 "$rounds"); do
  for system in ${systems[@]+"${systems[@]}"}; do
    for config in $ALL_CONFIGS; do
      for profile in ${profiles[@]+"${profiles[@]}"}; do
        jobs[${#jobs[@]}]="$round$RS$system$RS$config$RS$profile$RS$default_concurrency"
      done
      if [ -n "$sweep_profile" ]; then
        for c in ${sweep_concurrency[@]+"${sweep_concurrency[@]}"}; do
          jobs[${#jobs[@]}]="$round$RS$system$RS$config$RS$sweep_profile$RS$c"
        done
      fi
    done
  done
done
total_planned="${#jobs[@]}"

# ---------------------------------------------------------------------------
# Everything from here is teed into the run log. The pipeline form is used
# rather than `exec > >(tee ...)` because a process substitution can be torn
# down before it has flushed, which loses precisely the end-of-run summary a
# human comes back to read.
# ---------------------------------------------------------------------------
main() {
  local i=0 rec round system config profile conc stem result label
  local st detail out rc t0 t1 required failed_cell="" cell=""

  started_epoch="$(date +%s)"
  trap 'summary "$total_planned"' EXIT
  trap 'aborted="interrupted by signal"; echo; echo "!! interrupted -- tearing down"; teardown_all; exit 130' INT TERM

  echo "==========================================================================="
  echo "=== benchmark matrix: profile=$PROFILE_ID quick=$QUICK started=$(now_utc)"
  echo "==========================================================================="
  echo "repo         : $ROOT"
  echo "git          : $(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo 'not a git checkout')$(git -C "$ROOT" diff --quiet 2>/dev/null || echo ' (dirty)')"
  echo "systems      : ${systems[*]}"
  echo "configs      : $ALL_CONFIGS"
  echo "profiles     : ${profiles[*]}"
  echo "rounds       : $rounds"
  if [ -n "$sweep_profile" ]; then
    echo "sweep        : $sweep_profile at concurrency ${sweep_concurrency[*]}"
  else
    echo "sweep        : none"
  fi
  echo "concurrency  : $default_concurrency"
  echo "combinations : $total_planned"
  echo "force        : $FORCE"
  echo "disk         : each run waits for its own requirement (profile estimate + ${MIN_FREE_GIB} GiB floor), up to ${DISK_WAIT_SECONDS}s; currently $(free_gib) GiB free"
  if [ "$CAFFEINATED" = "1" ]; then
    echo "sleep        : holding caffeinate -dimsu for the lifetime of this run"
  else
    echo "sleep        : NOT holding a sleep assertion -- the machine may sleep mid-run"
  fi
  echo "python       : $PY"
  echo

  if [ "$DRY_RUN" = "1" ]; then
    echo "-- dry run: nothing is started, no result is written"
    for rec in ${jobs[@]+"${jobs[@]}"}; do
      IFS="$RS" read -r round system config profile conc <<< "$rec"
      i=$(( i + 1 ))
      stem="${system}__${config}__${profile}__c${conc}__r${round}"
      label="$system $config $profile c$conc r$round"
      if [ -e "$RAW/$stem.json" ] && [ "$FORCE" != "1" ]; then
        IFS="$RS" read -r st detail <<< "$(read_result_status "$RAW/$stem.json")"
        printf '[%d/%d] %-34s already done (status=%s)\n' "$i" "$total_planned" "$label" "$st"
      else
        printf '[%d/%d] %-34s would run\n' "$i" "$total_planned" "$label"
      fi
    done
    trap - EXIT
    echo
    echo "dry run only -- $total_planned combinations planned."
    exit 0
  fi

  docker image inspect "$warp_image" >/dev/null 2>&1 \
    || die "warp image '$warp_image' is missing. Build it: docker build -t $warp_image bench/warp"

  # A stack left running by an interrupted session competes for the same CPUs
  # and would contaminate every measurement; run-workload.sh refuses to run
  # while one exists. Start from nothing.
  echo "-- tearing down any stack left over from an earlier session"
  teardown_all
  echo

  for rec in ${jobs[@]+"${jobs[@]}"}; do
    IFS="$RS" read -r round system config profile conc <<< "$rec"
    i=$(( i + 1 ))
    stem="${system}__${config}__${profile}__c${conc}__r${round}"
    result="$RAW/$stem.json"
    label="$system $config $profile c$conc r$round"

    if [ -e "$result" ]; then
      if [ "$FORCE" = "1" ]; then
        echo "[$i/$total_planned] $label -- --force: discarding the existing result and re-running"
        rm -f "$RAW/$stem".*
      else
        IFS="$RS" read -r st detail <<< "$(read_result_status "$result")"
        echo "[$i/$total_planned] $label -- already done (status=$st), skipping"
        n_skipped=$(( n_skipped + 1 ))
        record "$st" "$label" "existing" "$detail"
        continue
      fi
    fi

    cell="$round/$system/$config"
    if [ "$cell" = "$failed_cell" ]; then
      echo "[$i/$total_planned] $label -- $system did not come up in $config this round, skipping"
      record "stack_failed" "$label" "skipped" "stack did not start earlier in this round"
      continue
    fi

    echo "[$i/$total_planned] $label -- $(now_utc)"
    required="$(job_required_gib "$profile" "$config")" \
      || die "could not compute the disk requirement for $profile on $config"
    if ! wait_for_disk "$required" "$profile/$config"; then
      aborted="host free space never reached the ${required} GiB that $profile on $config needs (waited ${DISK_WAIT_SECONDS}s, $(free_gib) GiB free). Free space and re-run; completed combinations are skipped automatically."
      echo "!! $aborted" >&2
      record "no_result" "$label" "aborted" "aborted before this run: needed ${required} GiB"
      teardown_all
      exit 1
    fi

    t0="$(date +%s)"
    if ! "$ROOT/bench/stack.sh" up "$system" "$config"; then
      echo "!! $system did not come up in $config -- recording the rest of the cell as stack_failed"
      "$ROOT/bench/stack.sh" down "$system" || true
      failed_cell="$cell"
      record "stack_failed" "$label" "attempted" "stack.sh up failed"
      continue
    fi

    # Captured to a file and replayed, not piped. A measurement forks
    # background helpers (telemetry, watchdogs); any one of them leaking an
    # inherited descriptor into a pipe would block this driver until it exited
    # -- up to BENCH_RUN_TIMEOUT per run, i.e. days across a full matrix.
    # bench/run-workload.sh had exactly that leak and it is fixed there too,
    # but an unattended driver should not be one stray descriptor away from
    # stalling. A file cannot stall.
    out="$(mktemp "${TMPDIR:-/tmp}/bench-run.XXXXXX")"
    set +e
    ROUND="$round" "$ROOT/bench/run-workload.sh" "$system" "$config" "$profile" "$conc" \
      >"$out" 2>&1
    rc=$?
    set -e
    cat "$out"

    "$ROOT/bench/stack.sh" down "$system" || true
    t1="$(date +%s)"
    n_attempted=$(( n_attempted + 1 ))
    measured_seconds=$(( measured_seconds + t1 - t0 ))

    # THE POINT OF THIS SCRIPT: the status comes off the result file, not $rc.
    # run-workload.sh exits 0 for ok AND for ok_with_warnings.
    if [ -e "$result" ]; then
      IFS="$RS" read -r st detail <<< "$(read_result_status "$result")"
    else
      st="no_result"
      detail="$(grep -E '^(run-workload|assemble_result):' "$out" | tail -1 || true)"
      [ -n "$detail" ] || detail="$(grep -v '^[[:space:]]*$' "$out" | tail -1 || true)"
      [ -n "$detail" ] || detail="run-workload.sh exited $rc with no output"
    fi
    rm -f "$out"

    record "$st" "$label" "measured" "$detail"
    case "$st" in
      ok)               echo "   -> status=ok  exit=$rc  $(( t1 - t0 ))s" ;;
      ok_with_warnings) echo "   -> status=ok_with_warnings  exit=$rc  $(( t1 - t0 ))s  failed_checks: $detail" ;;
      suspect)          echo "!! -> status=SUSPECT  exit=$rc  $(( t1 - t0 ))s  failed_checks: $detail -- do not publish this run" ;;
      failed)           echo "!! -> status=FAILED  exit=$rc  $(( t1 - t0 ))s  no metrics were produced  ($detail)" ;;
      no_result)        echo "!! -> NO RESULT FILE  exit=$rc  $(( t1 - t0 ))s  $detail" ;;
      *)                echo "!! -> status=$st  exit=$rc  $(( t1 - t0 ))s  $detail" ;;
    esac
    echo "   disk after teardown: $(free_gib) GiB free"
  done

  echo
  echo "=== all $total_planned combinations processed $(now_utc) ==="
  # Explicit: bash runs a pipeline subshell's EXIT trap when it `exit`s and NOT
  # when it falls off the end, and that trap is what prints the summary.
  exit 0
}

teardown_all() {
  local s
  for s in $ALL_SYSTEMS; do
    "$ROOT/bench/stack.sh" down "$s" >/dev/null 2>&1 || true
  done
}

# A dry run starts nothing and measures nothing, so it has no business
# appending to the committed run log; it also needs no subshell, and main's
# explicit `exit` then ends the script directly.
if [ "$DRY_RUN" = "1" ]; then
  main
  exit $?
fi
main 2>&1 | tee -a "$LOG"
exit "${PIPESTATUS[0]}"
