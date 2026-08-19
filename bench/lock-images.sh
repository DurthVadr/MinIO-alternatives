#!/usr/bin/env bash
# Re-resolve arm64 digests from the registry and diff against images.lock.
# Exit 1 on any drift. This is how we detect a tag being re-pointed upstream.
#
# Covers both the four system images ("images") and the pinned helper images
# ("helpers", e.g. curlimages/curl used by bench/stack.sh to poll S3 endpoints
# for readiness) -- an unpinned helper could change readiness detection
# partway through a study with nothing recording it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK="$ROOT/images.lock"
drift=0

resolve_arm64_digest() {
  local repo="$1" tag="$2" token manifest
  token=$(curl -fsS "https://auth.docker.io/token?service=registry.docker.io&scope=repository:${repo}:pull" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])')
  manifest=$(curl -fsS \
    -H "Authorization: Bearer ${token}" \
    -H "Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json" \
    "https://registry-1.docker.io/v2/${repo}/manifests/${tag}")
  printf '%s' "$manifest" | python3 -c '
import sys, json
index = json.load(sys.stdin)
for m in index.get("manifests", []):
    p = m.get("platform", {})
    if p.get("os") == "linux" and p.get("architecture") == "arm64":
        print(m["digest"]); break
else:
    sys.exit("no linux/arm64 manifest in index")
'
}

# check_group <lock-key>: walk every entry under images.lock[<lock-key>],
# re-resolve it, and diff against the locked digest.
check_group() {
  local group="$1"
  while IFS=$'\t' read -r name ref locked; do
    repo="${ref%:*}"; tag="${ref##*:}"
    actual=$(resolve_arm64_digest "$repo" "$tag")
    if [[ "$actual" == "$locked" ]]; then
      printf 'ok    %-10s %s\n' "$name" "$locked"
    else
      printf 'DRIFT %-10s locked=%s actual=%s\n' "$name" "$locked" "$actual"
      drift=1
    fi
  done < <(python3 -c '
import json, sys
lock = json.load(open(sys.argv[1]))
for name, e in lock.get(sys.argv[2], {}).items():
    print(name + "\t" + e["ref"] + "\t" + e["digest"])
' "$LOCK" "$group")
}

check_group images
check_group helpers

exit "$drift"
