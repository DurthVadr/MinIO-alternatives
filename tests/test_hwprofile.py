import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bench" / "hwprofile.sh"
RESULTS_ROOT = ROOT / "results"


def _remove_results_subdir(name):
    """Remove results/<name> and, if that leaves results/ empty, remove it
    too -- results/ is deliberately untracked (raw provenance with no
    measurements yet is misleading in shared history), so tests must not
    leave even an empty directory behind for git to notice later.
    """
    shutil.rmtree(RESULTS_ROOT / name, ignore_errors=True)
    try:
        RESULTS_ROOT.rmdir()
    except OSError:
        pass  # missing, non-empty (another test's subdir), or a race -- fine either way


def profile(profile_id=None):
    """Run hwprofile.sh and return the parsed profile.

    Every invocation is a side effect of the collision guard: it creates or
    checks results/<profile_id>/hardware-profile.json. This helper cleans up
    whatever directory it touched -- the real machine's derived profile_id
    if no override is given, or the caller's override -- leaving the
    working tree exactly as it found it. Tests that need the real
    derivation (e.g. the slug shape) call this with no argument and still
    get genuine derived data; they just don't leave a file behind
    afterward.
    """
    env = dict(os.environ)
    if profile_id is not None:
        env["PROFILE_ID"] = profile_id
    out = subprocess.run(
        [str(SCRIPT)], capture_output=True, text=True, check=True, env=env
    ).stdout
    data = json.loads(out)
    _remove_results_subdir(data["profile_id"])
    return data


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


def test_fingerprint_is_present_and_12_hex_chars():
    fp = profile()["fingerprint"]
    assert re.match(r"^[0-9a-f]{12}$", fp), f"not a 12-hex-char fingerprint: {fp}"


def test_fingerprint_is_stable_across_invocations():
    # Same machine, two separate subprocess runs -> identical fingerprint.
    assert profile()["fingerprint"] == profile()["fingerprint"]


def test_collision_guard_rejects_mismatched_fingerprint():
    # A profile_id that has never been used before, so this test cannot
    # collide with the real machine profile recorded under results/.
    test_profile_id = f"test-guard-{uuid.uuid4().hex[:8]}"
    results_dir = ROOT / "results" / test_profile_id
    guard_file = results_dir / "hardware-profile.json"
    try:
        results_dir.mkdir(parents=True)
        guard_file.write_text(json.dumps({"fingerprint": "deadbeef0000"}))

        result = subprocess.run(
            [str(SCRIPT)],
            capture_output=True,
            text=True,
            env={**os.environ, "PROFILE_ID": test_profile_id},
        )

        assert result.returncode != 0
        assert "fingerprint" in result.stderr.lower()
        assert test_profile_id in result.stderr
    finally:
        _remove_results_subdir(test_profile_id)


def test_collision_guard_replaces_corrupt_record():
    # A corrupt/unparseable record protects nothing, so the guard should
    # warn and replace it rather than block every future run -- unlike a
    # genuine fingerprint mismatch, this must not abort.
    test_profile_id = f"test-guard-corrupt-{uuid.uuid4().hex[:8]}"
    results_dir = ROOT / "results" / test_profile_id
    guard_file = results_dir / "hardware-profile.json"
    try:
        results_dir.mkdir(parents=True)
        guard_file.write_text("{not valid json")

        result = subprocess.run(
            [str(SCRIPT)],
            capture_output=True,
            text=True,
            env={**os.environ, "PROFILE_ID": test_profile_id},
        )

        assert result.returncode == 0, result.stderr
        assert "unreadable" in result.stderr.lower()

        replaced = json.loads(guard_file.read_text())
        assert replaced["profile_id"] == test_profile_id
        assert re.match(r"^[0-9a-f]{12}$", replaced["fingerprint"])
    finally:
        _remove_results_subdir(test_profile_id)


def test_profile_id_rejects_shell_metacharacters():
    # PROFILE_ID is an operator override (documented in CONTRIBUTING.md for
    # resolving a derived-id collision) that bypasses the cpu_slug sanitizer
    # entirely. hwprofile.sh used to interpolate profile_id -- and several
    # other values -- directly into an UNQUOTED heredoc, so a value like
    # this could previously run as shell commands or break the emitted
    # JSON. This project chose rejection over pass-through: profile_id must
    # be a plain slug, validated in bash before Python (or Docker) is ever
    # invoked. Assert no command runs (the literal backtick/$()/quote/
    # backslash never get shell-evaluated) and no results/ directory for
    # this value is created.
    hostile = 'a`id`$(whoami)"\\d'
    result = subprocess.run(
        [str(SCRIPT)],
        capture_output=True,
        text=True,
        env={**os.environ, "PROFILE_ID": hostile},
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "slug" in result.stderr.lower()
    # The raw value should appear verbatim in the error, proving it was
    # never evaluated (an executed `id`/$(whoami) would replace itself).
    assert hostile in result.stderr
    # Rejection happens in bash before results_dir is ever touched, so no
    # directory for this value should exist -- regardless of whatever other
    # tests may have left behind under results/ (an empty parent dir from a
    # sibling test's own cleanup is not this test's concern).
    assert not (ROOT / "results" / hostile).exists()
