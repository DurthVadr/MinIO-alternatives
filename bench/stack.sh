#!/usr/bin/env bash
# Bring one system up in one config, wait until its S3 endpoint answers, tear it down.
# Usage: stack.sh up <system> <config> | down <system> | endpoint <system>
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_DIR="$ROOT/compose"
NETWORK="s3bench"
SYSTEMS="minio silo rustfs seaweedfs"
CONFIGS="c1 c2"

# Pinned to the digest recorded in images.lock under "helpers.curl" -- an
# unpinned helper image could silently change readiness detection partway
# through a multi-hour study.
CURL_IMAGE="curlimages/curl:8.11.1@sha256:012fd1b212b09992bad38e2f911cac0bfbcadd0f8060fb4558fc2d74f8435a4a"

ensure_network() {
  docker network inspect "$NETWORK" >/dev/null 2>&1 || docker network create "$NETWORK" >/dev/null
}

compose() {
  local system="$1"; shift
  docker compose -f "$COMPOSE_DIR/${system}.yaml" -p "bench-${system}" "$@"
}

wait_ready() {
  # Poll the S3 endpoint from inside the network. A container healthcheck is not
  # enough: a system can report healthy before its S3 route is serving.
  #
  # Ruling B: a single request per iteration, not two. The brief's original
  # version ran a -fsS check first (which fails on any non-2xx, including a
  # 403) and then a second request to accept 200 or 403. The first call was
  # dead weight -- a 403 from the S3 root is a normal response when
  # anonymous listing is denied, so it always fell through to the second
  # request anyway. One `docker run` per poll instead of two matters here:
  # up to 120s of 2s polling, across roughly 64 stack transitions in a full
  # benchmark run, is a lot of avoidable container-start overhead.
  local system="$1" deadline=$((SECONDS + 120)) code
  while (( SECONDS < deadline )); do
    code=$(docker run --rm --network "$NETWORK" \
      "$CURL_IMAGE" -sS -o /dev/null -w '%{http_code}' \
      "http://bench-${system}:9000/" 2>/dev/null || true)
    if [[ "$code" =~ ^(200|403)$ ]]; then
      return 0
    fi
    sleep 2
  done
  echo "timeout waiting for ${system} S3 endpoint" >&2
  compose "$system" logs --tail 60 >&2
  return 1
}

# Validate <system>/<config> before doing anything else. Task 6 calls this
# script roughly 64 times across an unattended multi-hour run: without this,
# a typo'd system or config name (or a missing argument, which `set -u`
# would otherwise report as an opaque "unbound variable") silently started
# nothing and then burned wait_ready's full 120s timeout before failing.
usage() {
  echo "usage: stack.sh {up <system> <config>|down <system>|endpoint <system>}" >&2
  echo "  <system>: one of: $SYSTEMS" >&2
  echo "  <config>: one of: $CONFIGS" >&2
  exit 2
}

require_system() {
  local system="${1:-}"
  [ -n "$system" ] || { echo "stack.sh: missing <system>" >&2; usage; }
  case " $SYSTEMS " in
    *" $system "*) ;;
    *) echo "stack.sh: unknown system '${system}' (expected one of: $SYSTEMS)" >&2; usage ;;
  esac
}

require_config() {
  local config="${1:-}"
  [ -n "$config" ] || { echo "stack.sh: missing <config>" >&2; usage; }
  case " $CONFIGS " in
    *" $config "*) ;;
    *) echo "stack.sh: unknown config '${config}' (expected one of: $CONFIGS)" >&2; usage ;;
  esac
}

case "${1:-}" in
  up)
    system="${2:-}"; config="${3:-}"
    require_system "$system"
    require_config "$config"
    ensure_network
    compose "$system" --profile "$config" up -d
    wait_ready "$system"
    ;;
  down)
    system="${2:-}"
    require_system "$system"
    compose "$system" --profile c1 --profile c2 down -v --remove-orphans
    ;;
  endpoint)
    system="${2:-}"
    require_system "$system"
    printf 'http://bench-%s:9000\n' "$system"
    ;;
  *)
    usage
    ;;
esac
