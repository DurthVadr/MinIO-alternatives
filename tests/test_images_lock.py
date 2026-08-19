import json
import re
from pathlib import Path

LOCK = Path(__file__).resolve().parents[1] / "images.lock"
COMPOSE_DIR = Path(__file__).resolve().parents[1] / "compose"
EXPECTED_SYSTEMS = {"minio", "silo", "rustfs", "seaweedfs"}
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
COMPOSE_DIGEST_RE = re.compile(r"@(sha256:[0-9a-f]{64})")


def load():
    return json.loads(LOCK.read_text())


def test_lock_file_exists():
    assert LOCK.is_file(), f"{LOCK} missing"


def test_all_systems_present():
    assert set(load()["images"]) == EXPECTED_SYSTEMS


def test_every_image_pinned_by_digest():
    for name, entry in load()["images"].items():
        assert DIGEST_RE.match(entry["digest"]), f"{name} digest malformed: {entry['digest']}"


def test_every_image_has_a_note():
    for name, entry in load()["images"].items():
        assert entry.get("note"), f"{name} missing provenance note"


def test_warp_pinned_by_checksum():
    warp = load()["warp"]
    assert re.match(r"^[0-9a-f]{64}$", warp["sha256"])
    assert warp["url"].endswith(warp["asset"])


def test_platform_is_arm64():
    assert load()["platform"] == "linux/arm64"


def test_helper_curl_pinned_by_digest():
    # Ruling C: bench/stack.sh polls S3 endpoints with curlimages/curl. An
    # unpinned helper could change readiness detection mid-study with nothing
    # recording it, so it is locked the same way the four system images are.
    curl = load()["helpers"]["curl"]
    assert DIGEST_RE.match(curl["digest"]), f"curl digest malformed: {curl['digest']}"


def test_compose_digests_match_lock():
    # Ruling A: the digest lives in two places (images.lock and the compose
    # files) and nothing else checks they agree. Drift in either direction --
    # a compose file pinned to a digest images.lock does not know about, or a
    # system image whose compose file has fallen behind a re-locked digest --
    # must fail loudly instead of silently benchmarking the wrong build.
    lock = load()
    known_digests = {e["digest"] for e in lock["images"].values()}
    known_digests |= {e["digest"] for e in lock.get("helpers", {}).values()}

    compose_files = sorted(COMPOSE_DIR.glob("*.yaml"))
    assert compose_files, f"no compose files found under {COMPOSE_DIR}"

    for path in compose_files:
        text = path.read_text()
        for digest in COMPOSE_DIGEST_RE.findall(text):
            assert digest in known_digests, (
                f"{path.name} references {digest}, which is not in images.lock"
            )

    for name, entry in lock["images"].items():
        compose_path = COMPOSE_DIR / f"{name}.yaml"
        if not compose_path.is_file():
            continue
        text = compose_path.read_text()
        assert f"@{entry['digest']}" in text, (
            f"{compose_path.name} does not use the locked digest for {name} "
            f"({entry['digest']})"
        )
