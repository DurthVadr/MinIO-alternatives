import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bench" / "hwprofile.sh"


def profile():
    out = subprocess.run([str(SCRIPT)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_script_is_executable():
    assert SCRIPT.is_file() and SCRIPT.stat().st_mode & 0o111


def test_emits_valid_json_with_schema_version():
    assert profile()["schema"] == 1


def test_profile_id_is_a_slug():
    pid = profile()["profile_id"]
    assert re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", pid), f"not a slug: {pid}"


def test_host_block_is_populated():
    host = profile()["host"]
    assert host["arch"] in {"arm64", "aarch64", "x86_64", "amd64"}
    assert host["cpu_cores"] >= 1
    assert host["ram_bytes"] > 1_000_000_000
    assert host["cpu_model"]


def test_runtime_block_reports_vm_resources():
    rt = profile()["container_runtime"]
    assert rt["server_version"]
    assert rt["vm_cpus"] >= 1
    assert rt["vm_ram_bytes"] > 0


def test_disk_block_reports_host_free_space():
    assert profile()["disk"]["host_free_bytes"] > 0


def test_images_are_embedded_from_lock():
    assert set(profile()["images"]) == {"minio", "silo", "rustfs", "seaweedfs"}


def test_captured_at_is_iso8601_utc():
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", profile()["captured_at"])
