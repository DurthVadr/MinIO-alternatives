#!/usr/bin/env bash
# Emit a hardware/runtime profile as JSON on stdout.
# Every result file embeds this so no number ever travels without its context.
#
# Also guards against profile_id collisions: profile_id is a human-readable
# slug (e.g. "apple-m1-8gb-darwin") that two genuinely different machines can
# share if they have a similar CPU-name prefix and RAM bucket. Every run
# computes a fingerprint (sha256 of stable HOST identity fields only -- cpu
# model/cores, ram, os, arch; deliberately excludes VM CPU/RAM allocation,
# which Docker Desktop can change between runs) and, if
# results/<profile_id>/hardware-profile.json already exists from a prior run,
# checks the fingerprint against it. A mismatch means two different machines
# are about to share a results directory, so the run is aborted loudly
# instead of silently mixing their results. Two machines with identical
# hardware share a fingerprint by design -- fingerprint identifies a
# hardware class, not an individual box.
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

# profile_id becomes a directory name (results/<profile_id>/) and used to
# flow verbatim into a shell-interpolated heredoc, so it must be a strict
# slug. The derived cpu_slug above is already sanitized (every non
# [a-z0-9] run, including anything hostile in a devicetree-sourced
# cpu_model, collapses to a single hyphen); an operator-set PROFILE_ID is
# not filtered at all before this point, so it is the one path that can
# still carry shell metacharacters or path-traversal segments ("../"). This
# check rejects anything that is not a plain slug before it reaches
# anything else, closing both concerns with one gate regardless of source.
slug_re='^[a-z0-9]+(-[a-z0-9]+)*$'
if ! [[ "$profile_id" =~ $slug_re ]]; then
  echo "hwprofile: profile_id '${profile_id}' is not a valid slug." >&2
  echo "Expected lowercase letters, digits, and single hyphens between" >&2
  echo "segments (e.g. 'apple-m1-8gb-darwin'). If you set PROFILE_ID, use a" >&2
  echo "value in that shape." >&2
  exit 1
fi

docker_server="$(docker info --format '{{.ServerVersion}}')"
docker_client="$(docker version --format '{{.Client.Version}}')"
vm_cpus="$(docker info --format '{{.NCPU}}')"
vm_ram_bytes="$(docker info --format '{{.MemTotal}}')"
vm_arch="$(docker info --format '{{.Architecture}}')"
storage_driver="$(docker info --format '{{.Driver}}')"
captured_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Every dynamic value below crosses into Python through the environment,
# never through shell interpolation into the heredoc text, and the heredoc
# itself is quoted (<<'PYEOF') so bash performs NO expansion on it -- no
# $vars, no $(...), no backticks -- no matter what characters end up in
# cpu_model, os_version, docker's reported versions, or anything else.
# cpu_model in particular can come from a bootloader/hypervisor-supplied
# string on arm64 Linux (see the devicetree fallback above) with no
# character restrictions; profile_id is validated above but is passed the
# same safe way for consistency and defense in depth.
HWPROFILE_ROOT="$ROOT" \
HWPROFILE_OS="$os" \
HWPROFILE_OS_VERSION="$os_version" \
HWPROFILE_ARCH="$arch" \
HWPROFILE_CPU_MODEL="$cpu_model" \
HWPROFILE_CPU_CORES="$cpu_cores" \
HWPROFILE_RAM_BYTES="$ram_bytes" \
HWPROFILE_DOCKER_CLIENT="$docker_client" \
HWPROFILE_DOCKER_SERVER="$docker_server" \
HWPROFILE_VM_CPUS="$vm_cpus" \
HWPROFILE_VM_RAM_BYTES="$vm_ram_bytes" \
HWPROFILE_VM_ARCH="$vm_arch" \
HWPROFILE_STORAGE_DRIVER="$storage_driver" \
HWPROFILE_HOST_FREE_BYTES="$host_free_bytes" \
HWPROFILE_PROFILE_ID="$profile_id" \
HWPROFILE_CAPTURED_AT="$captured_at" \
python3 - <<'PYEOF'
import hashlib, json, os, sys
from pathlib import Path

env = os.environ
root = Path(env["HWPROFILE_ROOT"])
lock = json.load(open(root / "images.lock"))

host = {
    "os": env["HWPROFILE_OS"],
    "os_version": env["HWPROFILE_OS_VERSION"],
    "arch": env["HWPROFILE_ARCH"],
    "cpu_model": env["HWPROFILE_CPU_MODEL"],
    "cpu_cores": int(env["HWPROFILE_CPU_CORES"]),
    "ram_bytes": int(env["HWPROFILE_RAM_BYTES"]),
}
runtime = {
    "kind": "docker",
    "client_version": env["HWPROFILE_DOCKER_CLIENT"],
    "server_version": env["HWPROFILE_DOCKER_SERVER"],
    "vm_cpus": int(env["HWPROFILE_VM_CPUS"]),
    "vm_ram_bytes": int(env["HWPROFILE_VM_RAM_BYTES"]),
    "vm_arch": env["HWPROFILE_VM_ARCH"],
    "storage_driver": env["HWPROFILE_STORAGE_DRIVER"],
}

# fingerprint: stable HOST identity only, independent of the human-readable
# slug. Same machine -> same fingerprint, every time. vm_cpus/vm_ram_bytes
# are deliberately excluded: they come from "docker info", and on Docker
# Desktop the VM's allocated RAM can change between invocations (Resource
# Saver reclaims idle VM RAM), which would make the SAME machine compute a
# different fingerprint on a later run and trip the guard against its own
# earlier record. vm_cpus/vm_ram_bytes are still recorded below under
# container_runtime -- useful provenance, just not machine identity.
canonical = "|".join([
    host["cpu_model"], str(host["cpu_cores"]), str(host["ram_bytes"]),
    host["os"], host["arch"],
])
fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]

profile_id = env["HWPROFILE_PROFILE_ID"]
profile = {
    "schema": 1,
    "profile_id": profile_id,
    "fingerprint": fingerprint,
    "captured_at": env["HWPROFILE_CAPTURED_AT"],
    "host": host,
    "container_runtime": runtime,
    "disk": {"host_free_bytes": int(env["HWPROFILE_HOST_FREE_BYTES"])},
    "images": {k: v["digest"] for k, v in lock["images"].items()},
    "warp": lock["warp"]["version"],
}
text = json.dumps(profile, indent=2)

# Collision guard: runs before every measurement, so this is the right place
# to catch two different machines about to share a results directory. Task 6
# calls this before every single measurement in an unattended multi-hour
# run, so failure modes here must be either silent-safe or a clean one-line
# message -- never a traceback that bricks the rest of the run.
results_dir = root / "results" / profile_id
guard_file = results_dir / "hardware-profile.json"


def write_guard_file():
    try:
        results_dir.mkdir(parents=True, exist_ok=True)
        guard_file.write_text(text + "\n")
    except OSError as exc:
        # The benchmark itself needs to write into results/ too, so if this
        # fails there is nothing useful left to do but stop cleanly.
        sys.exit(f"hwprofile: cannot write {guard_file}: {exc}")


if guard_file.exists():
    try:
        recorded = json.loads(guard_file.read_text())
        if not isinstance(recorded, dict):
            raise ValueError("not a JSON object")
        recorded_fingerprint = recorded.get("fingerprint")
    except (json.JSONDecodeError, UnicodeDecodeError, OSError, ValueError) as exc:
        # A corrupt record protects nothing -- there is no fingerprint in it
        # to guard against -- so replace it rather than block every future
        # run on this profile_id until someone finds and deletes it by hand.
        print(
            f"hwprofile: {guard_file} is unreadable ({exc}); replacing it "
            f"with the current profile.",
            file=sys.stderr,
        )
        write_guard_file()
    else:
        if recorded_fingerprint != fingerprint:
            sys.exit(
                f"hwprofile: profile_id '{profile_id}' was already recorded with "
                f"fingerprint {recorded_fingerprint}, but this machine computes "
                f"{fingerprint}. Two different machines cannot share a profile_id "
                f"-- set PROFILE_ID=<something-distinct> and re-run."
            )
        # Fingerprints match: leave the recorded file untouched.
else:
    write_guard_file()

print(text)
PYEOF
