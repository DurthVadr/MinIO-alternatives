#!/usr/bin/env bash
# Emit a hardware/runtime profile as JSON on stdout.
# Every result file embeds this so no number ever travels without its context.
#
# Also guards against profile_id collisions: profile_id is a human-readable
# slug (e.g. "apple-m1-8gb-darwin") that two genuinely different machines can
# share if they have a similar CPU-name prefix and RAM bucket. Every run
# computes a fingerprint (sha256 of stable hardware/runtime fields) and, if
# results/<profile_id>/hardware-profile.json already exists from a prior run,
# checks the fingerprint against it. A mismatch means two different machines
# are about to share a results directory, so the run is aborted loudly
# instead of silently mixing their results.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

case "$(uname -s)" in
  Darwin)
    os="darwin"
    os_version="$(uname -r)"
    cpu_model="$(sysctl -n machdep.cpu.brand_string)"
    cpu_cores="$(sysctl -n hw.ncpu)"
    ram_bytes="$(sysctl -n hw.memsize)"
    host_free_bytes=$(( $(df -k "$ROOT" | awk 'NR==2 {print $4}') * 1024 ))
    ;;
  Linux)
    os="linux"
    os_version="$(uname -r)"
    cpu_cores="$(nproc)"
    ram_bytes=$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo) * 1024 ))
    host_free_bytes=$(( $(df -k "$ROOT" | awk 'NR==2 {print $4}') * 1024 ))

    # cpu_model: try increasingly indirect sources until one is non-empty.
    # "model name"/"Model" are frequently absent from /proc/cpuinfo on arm64
    # Linux (Graviton, Ampere, most cloud arm64 VMs) -- exactly the machine
    # class this project targets -- so falling straight to "unknown" would
    # silently degrade the hardware-provenance stamp on the systems that
    # matter most.
    cpu_model="$(awk -F': ' '/^model name/ {print $2; exit}' /proc/cpuinfo 2>/dev/null || true)"
    [ -n "$cpu_model" ] || cpu_model="$(awk -F': ' '/^Model/ {print $2; exit}' /proc/cpuinfo 2>/dev/null || true)"
    [ -n "$cpu_model" ] || cpu_model="$(lscpu 2>/dev/null | awk -F': *' '/^Model name:/ {print $2; exit}' || true)"
    if [ -z "$cpu_model" ] && [ -r /sys/firmware/devicetree/base/model ]; then
      cpu_model="$(tr -d '\0' < /sys/firmware/devicetree/base/model 2>/dev/null || true)"
    fi
    [ -n "$cpu_model" ] || cpu_model="unknown"
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

python3 - "$ROOT" <<PYEOF
import hashlib, json, sys
from pathlib import Path

root = Path(sys.argv[1])
lock = json.load(open(root / "images.lock"))

host = {
    "os": "${os}",
    "os_version": "${os_version}",
    "arch": "${arch}",
    "cpu_model": "${cpu_model}",
    "cpu_cores": ${cpu_cores},
    "ram_bytes": ${ram_bytes},
}
runtime = {
    "kind": "docker",
    "client_version": "${docker_client}",
    "server_version": "${docker_server}",
    "vm_cpus": ${vm_cpus},
    "vm_ram_bytes": ${vm_ram_bytes},
    "vm_arch": "${vm_arch}",
    "storage_driver": "${storage_driver}",
}

# fingerprint: a stable hardware identity independent of the human-readable
# slug. Same machine -> same fingerprint, every time.
canonical = "|".join([
    host["cpu_model"], str(host["cpu_cores"]), str(host["ram_bytes"]),
    host["os"], host["arch"], str(runtime["vm_cpus"]), str(runtime["vm_ram_bytes"]),
])
fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]

profile_id = "${profile_id}"
profile = {
    "schema": 1,
    "profile_id": profile_id,
    "fingerprint": fingerprint,
    "captured_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    "host": host,
    "container_runtime": runtime,
    "disk": {"host_free_bytes": ${host_free_bytes}},
    "images": {k: v["digest"] for k, v in lock["images"].items()},
    "warp": lock["warp"]["version"],
}
text = json.dumps(profile, indent=2)

# Collision guard: runs before every measurement, so this is the right place
# to catch two different machines about to share a results directory.
results_dir = root / "results" / profile_id
guard_file = results_dir / "hardware-profile.json"
if guard_file.exists():
    recorded_fingerprint = json.loads(guard_file.read_text()).get("fingerprint")
    if recorded_fingerprint != fingerprint:
        sys.exit(
            f"hwprofile: profile_id '{profile_id}' was already recorded with "
            f"fingerprint {recorded_fingerprint}, but this machine computes "
            f"{fingerprint}. Two different machines cannot share a profile_id -- "
            f"set PROFILE_ID=<something-distinct> and re-run."
        )
    # Fingerprints match: leave the recorded file untouched.
else:
    results_dir.mkdir(parents=True, exist_ok=True)
    guard_file.write_text(text + "\n")

print(text)
PYEOF
