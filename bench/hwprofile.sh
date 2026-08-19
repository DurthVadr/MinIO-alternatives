#!/usr/bin/env bash
# Emit a hardware/runtime profile as JSON on stdout.
# Every result file embeds this so no number ever travels without its context.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

case "$(uname -s)" in
  Darwin)
    os="darwin"
    os_version="$(uname -r)"
    cpu_model="$(sysctl -n machdep.cpu.brand_string)"
    cpu_cores="$(sysctl -n hw.ncpu)"
    ram_bytes="$(sysctl -n hw.memsize)"
    host_free_bytes=$(( $(df -k /System/Volumes/Data | awk 'NR==2 {print $4}') * 1024 ))
    ;;
  Linux)
    os="linux"
    os_version="$(uname -r)"
    cpu_model="$(awk -F': ' '/model name|Model/ {print $2; exit}' /proc/cpuinfo)"
    [ -n "$cpu_model" ] || cpu_model="unknown"
    cpu_cores="$(nproc)"
    ram_bytes=$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo) * 1024 ))
    host_free_bytes=$(( $(df -k / | awk 'NR==2 {print $4}') * 1024 ))
    ;;
  *) echo "unsupported OS: $(uname -s)" >&2; exit 1 ;;
esac

arch="$(uname -m)"

# profile_id names the results directory. Slug form: <cpu>-<ram>gb-<os>
ram_gb=$(( ram_bytes / 1024 / 1024 / 1024 ))
cpu_slug="$(printf '%s' "$cpu_model" \
  | tr '[:upper:]' '[:lower:]' \
  | sed -E 's/\(r\)|\(tm\)//g; s/[^a-z0-9]+/-/g; s/^-+|-+$//g' \
  | cut -c1-24 | sed -E 's/-+$//')"
profile_id="${PROFILE_ID:-${cpu_slug}-${ram_gb}gb-${os}}"

docker_server="$(docker info --format '{{.ServerVersion}}')"
docker_client="$(docker version --format '{{.Client.Version}}')"
vm_cpus="$(docker info --format '{{.NCPU}}')"
vm_ram_bytes="$(docker info --format '{{.MemTotal}}')"
vm_arch="$(docker info --format '{{.Architecture}}')"
storage_driver="$(docker info --format '{{.Driver}}')"

python3 - "$ROOT/images.lock" <<PYEOF
import json, sys
lock = json.load(open(sys.argv[1]))
print(json.dumps({
    "schema": 1,
    "profile_id": "${profile_id}",
    "captured_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    "host": {
        "os": "${os}",
        "os_version": "${os_version}",
        "arch": "${arch}",
        "cpu_model": "${cpu_model}",
        "cpu_cores": ${cpu_cores},
        "ram_bytes": ${ram_bytes},
    },
    "container_runtime": {
        "kind": "docker",
        "client_version": "${docker_client}",
        "server_version": "${docker_server}",
        "vm_cpus": ${vm_cpus},
        "vm_ram_bytes": ${vm_ram_bytes},
        "vm_arch": "${vm_arch}",
        "storage_driver": "${storage_driver}",
    },
    "disk": {"host_free_bytes": ${host_free_bytes}},
    "images": {k: v["digest"] for k, v in lock["images"].items()},
    "warp": lock["warp"]["version"],
}, indent=2))
PYEOF
