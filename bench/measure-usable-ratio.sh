#!/usr/bin/env bash
# Measure how much physical disk a known amount of logical data costs, in c2.
#
# Usage:
#   measure-usable-ratio.sh <system>                  # full cycle: up, measure, down
#   measure-usable-ratio.sh --measure-only <system>   # measure against an already-running c2 stack
#
# Emits one JSON object on stdout. tests/test_durability.py calls the
# --measure-only form while it already has the stack up, so the harness never
# pays for a second stack cycle just to weigh the volumes.
#
# Why this number matters: erasure coding and replication cannot both be held
# equal on fault tolerance AND on storage cost. This harness holds fault
# tolerance equal (every c2 config survives one device loss) and MEASURES the
# storage cost, which is what makes the comparison honest instead of a claim.
#
# Ruling E: the brief reached for an `amazon/aws-cli` container to write the
# blob. That image is not in images.lock, and every image this harness runs is
# digest-pinned -- adding an unpinned one would contradict that and break
# tests/test_images_lock.py. The project's own .venv (boto3 is already a
# dependency) talks to the host-mapped endpoint instead. No new images.
#
# Ruling F: `docker system df -v` does not report per-volume Size on every
# storage driver, so a ratio derived from it can silently come out as a
# division by zero. Each volume is weighed directly with `du -sb` inside a
# container built from an image already in images.lock.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The same pinned helper bench/stack.sh already uses (images.lock ->
# helpers.curl). It only has to provide a shell and `du`; weighing a system's
# volumes with that system's OWN image would make the measurement depend on
# the thing being measured.
HELPER_IMAGE="curlimages/curl:8.11.1@sha256:012fd1b212b09992bad38e2f911cac0bfbcadd0f8060fb4558fc2d74f8435a4a"

LOGICAL_BYTES=$((256 * 1024 * 1024))
ENDPOINT="http://127.0.0.1:19000"
BUCKET="ratio"
KEY="blob.bin"

measure_only=false
if [ "${1:-}" = "--measure-only" ]; then
  measure_only=true
  shift
fi
system="${1:-}"
if [ -z "$system" ]; then
  echo "usage: measure-usable-ratio.sh [--measure-only] <system>" >&2
  exit 2
fi

baseline_breakdown="$(mktemp)"
after_breakdown="$(mktemp)"
stack_is_ours=false

cleanup() {
  rm -f "$baseline_breakdown" "$after_breakdown"
  if [ "$stack_is_ours" = true ]; then
    "$ROOT/bench/stack.sh" down "$system" >&2
  fi
}
trap cleanup EXIT

# Volumes are named <compose project>_<volume>, and bench/stack.sh uses
# `-p bench-<system>`. Weighing every volume of the project (including
# SeaweedFS's filer-metadata volume) rather than a hand-listed subset keeps
# the number honest: whatever the system chose to write, it counts.
volumes_of() {
  docker volume ls -q --filter "name=^bench-${system}_" | sort
}

# Writes "<volume>\t<bytes>" lines into $1 and echoes the total.
#
# `docker run -v <name>:/v` CREATES <name> when it does not exist, so a typo or
# an already-torn-down stack would weigh a freshly created empty volume as 0
# bytes and leave it behind -- a silent zero that becomes a nonsense ratio.
# Every volume is confirmed to exist first.
weigh_volumes() {
  local out="$1" total=0 vol size
  : >"$out"
  for vol in $(volumes_of); do
    docker volume inspect "$vol" >/dev/null 2>&1 || {
      echo "measure-usable-ratio: volume ${vol} vanished mid-measurement" >&2
      return 1
    }
    size=$(docker run --rm -v "${vol}:/v:ro" --entrypoint sh "$HELPER_IMAGE" \
      -c 'du -sb /v | cut -f1')
    total=$((total + size))
    printf '%s\t%s\n' "$vol" "$size" >>"$out"
  done
  echo "$total"
}

if [ "$measure_only" = false ]; then
  "$ROOT/bench/stack.sh" up "$system" c2 >&2
  stack_is_ours=true
fi

if [ -z "$(volumes_of)" ]; then
  echo "measure-usable-ratio: no volumes for project bench-${system}; is the c2 stack up?" >&2
  exit 1
fi

# Baseline first: a fresh stack is not empty (MinIO/RustFS write .minio.sys /
# .rustfs.sys, SeaweedFS writes filer metadata), and that fixed overhead is not
# part of what a byte of user data costs. usable_ratio is therefore the
# MARGINAL cost -- logical bytes over the physical bytes this write added --
# which is the number the EC-vs-replication comparison is actually about. The
# gross figure is reported next to it so nothing is hidden.
baseline="$(weigh_volumes "$baseline_breakdown")"

# Incompressible payload: SeaweedFS compresses on write by default (verified --
# a repeating-pattern object came back from its filer with
# "is_compressed": true), which would flatter it against systems that do not.
# Random data measures storage layout, not zlib.
MUR_ENDPOINT="$ENDPOINT" MUR_BUCKET="$BUCKET" MUR_KEY="$KEY" \
MUR_BYTES="$LOGICAL_BYTES" "$ROOT/.venv/bin/python" - <<'PYEOF' >&2
import os

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

env = os.environ
size = int(env["MUR_BYTES"])
s3 = boto3.client(
    "s3",
    endpoint_url=env["MUR_ENDPOINT"],
    aws_access_key_id="benchuser",
    aws_secret_access_key="benchsecret0",
    region_name="us-east-1",
    config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
)
try:
    s3.create_bucket(Bucket=env["MUR_BUCKET"])
except ClientError as exc:
    if exc.response["Error"]["Code"] not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
        raise
s3.put_object(Bucket=env["MUR_BUCKET"], Key=env["MUR_KEY"], Body=os.urandom(size))

# Read it back before weighing anything: a ratio computed over a write that
# did not fully land would be a fabrication.
body = s3.get_object(Bucket=env["MUR_BUCKET"], Key=env["MUR_KEY"])["Body"]
read = 0
while chunk := body.read(1 << 20):
    read += len(chunk)
if read != size:
    raise SystemExit(f"read back {read} of {size} bytes")
print(f"wrote and verified {size} bytes at {env['MUR_BUCKET']}/{env['MUR_KEY']}")
PYEOF

# Systems acknowledge a write before every byte has settled on disk (SeaweedFS's
# volume servers in particular finish behind the S3 response). Weighing too
# early undercounts physical bytes, which would overstate efficiency.
sleep 5

after="$(weigh_volumes "$after_breakdown")"

MUR_SYSTEM="$system" MUR_LOGICAL="$LOGICAL_BYTES" MUR_BASELINE="$baseline" \
MUR_AFTER="$after" MUR_BASE_BREAKDOWN="$baseline_breakdown" \
MUR_AFTER_BREAKDOWN="$after_breakdown" "$ROOT/.venv/bin/python" - <<'PYEOF'
import json
import os

env = os.environ


def breakdown(path):
    out = {}
    with open(path) as fh:
        for line in fh:
            name, size = line.rstrip("\n").split("\t")
            out[name] = int(size)
    return out


logical = int(env["MUR_LOGICAL"])
baseline = int(env["MUR_BASELINE"])
after = int(env["MUR_AFTER"])
delta = after - baseline
print(json.dumps({
    "system": env["MUR_SYSTEM"],
    "logical_bytes": logical,
    "physical_bytes_baseline": baseline,
    "physical_bytes_after": after,
    "physical_bytes_delta": delta,
    # Marginal: what a byte of user data cost. This is usable_ratio.
    "usable_ratio": round(logical / delta, 4) if delta > 0 else None,
    # Gross: includes the system's fixed on-disk overhead, so the marginal
    # figure cannot be mistaken for the whole story.
    "usable_ratio_gross": round(logical / after, 4) if after > 0 else None,
    "per_volume_baseline": breakdown(env["MUR_BASE_BREAKDOWN"]),
    "per_volume_after": breakdown(env["MUR_AFTER_BREAKDOWN"]),
    "method": (
        "du -sb over every bench-<system>_* docker volume, before and after "
        "one incompressible object"
    ),
}, indent=2))
PYEOF
