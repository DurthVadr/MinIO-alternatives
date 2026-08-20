import json
import re
from pathlib import Path

LOCK = Path(__file__).resolve().parents[1] / "images.lock"
COMPOSE_DIR = Path(__file__).resolve().parents[1] / "compose"
BENCH_DIR = Path(__file__).resolve().parents[1] / "bench"
EXPECTED_SYSTEMS = {"minio", "silo", "rustfs", "seaweedfs"}
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
PINNED_DIGEST_RE = re.compile(r"@(sha256:[0-9a-f]{64})")


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


def test_helper_debian_pinned_by_digest():
    # bench/warp/Dockerfile builds the load generator on this base. warp v1.6.1
    # ships a dynamically linked glibc binary, so the base image is part of what
    # the client actually is -- a floating tag could change the client's libc
    # halfway through a multi-hour study.
    debian = load()["helpers"]["debian"]
    assert DIGEST_RE.match(debian["digest"]), f"debian digest malformed: {debian['digest']}"


def test_warp_dockerfile_uses_the_locked_base():
    dockerfile = BENCH_DIR / "warp" / "Dockerfile"
    assert dockerfile.is_file(), f"{dockerfile} missing"
    text = dockerfile.read_text()
    digest = load()["helpers"]["debian"]["digest"]
    assert f"@{digest}" in text, (
        f"{dockerfile} does not build on the locked debian digest ({digest})"
    )
    # Every FROM must be pinned, not just the first one.
    for line in text.splitlines():
        if line.strip().upper().startswith("FROM "):
            assert "@sha256:" in line, f"unpinned base image: {line.strip()}"


def test_warp_dockerfile_verifies_the_release_checksum():
    # The whole reason warp is built from a release asset instead of pulled as
    # an image is that the asset can be pinned by content. A Dockerfile that
    # downloads without checking the checksum would throw that away.
    warp = load()["warp"]
    text = (BENCH_DIR / "warp" / "Dockerfile").read_text()
    assert warp["sha256"] in text, "Dockerfile does not carry the locked warp checksum"
    assert "sha256sum -c" in text, "Dockerfile does not verify the downloaded asset"
    assert warp["asset"] in text, "Dockerfile does not fetch the locked asset name"


def test_helper_curl_pinned_by_digest():
    # Ruling C: bench/stack.sh polls S3 endpoints with curlimages/curl. An
    # unpinned helper could change readiness detection mid-study with nothing
    # recording it, so it is locked the same way the four system images are.
    curl = load()["helpers"]["curl"]
    assert DIGEST_RE.match(curl["digest"]), f"curl digest malformed: {curl['digest']}"


def pinned_files():
    """Every file in the repo that pins an image by digest.

    Not just compose/: bench/stack.sh and bench/measure-usable-ratio.sh both
    hardcode the pinned curl helper, and bench/warp/Dockerfile pins the base
    image the load generator is built on. Globbing only compose/*.yaml left
    this test blind to most of the places a digest appears, so any of those
    files could drift from images.lock with nothing failing -- which is exactly
    the drift Ruling A exists to catch, and exactly what Ruling E (no unpinned
    helper image) depends on this test to enforce.
    """
    return (
        sorted(COMPOSE_DIR.glob("*.yaml"))
        + sorted(BENCH_DIR.glob("*.sh"))
        + sorted(BENCH_DIR.rglob("Dockerfile"))
    )


def test_pinned_digests_match_lock():
    # Ruling A: the digest lives in more than one place (images.lock, the
    # compose files, the bench scripts) and nothing else checks they agree.
    # Drift in either direction -- a file pinned to a digest images.lock does
    # not know about, or a file that has fallen behind a re-locked digest --
    # must fail loudly instead of silently benchmarking the wrong build.
    lock = load()
    known_digests = {e["digest"] for e in lock["images"].values()}
    known_digests |= {e["digest"] for e in lock.get("helpers", {}).values()}

    files = pinned_files()
    assert files, f"no compose files or bench scripts found under {COMPOSE_DIR} / {BENCH_DIR}"

    seen_any = False
    for path in files:
        text = path.read_text()
        for digest in PINNED_DIGEST_RE.findall(text):
            seen_any = True
            assert digest in known_digests, (
                f"{path.name} references {digest}, which is not in images.lock"
            )
    assert seen_any, "no digest-pinned references found at all -- the guard is inert"

    for name, entry in lock["images"].items():
        compose_path = COMPOSE_DIR / f"{name}.yaml"
        # A missing compose file for a locked system is drift too -- Ruling A
        # said either direction must fail, and silently skipping here means
        # renaming or deleting a compose file passes this test by omission.
        assert compose_path.is_file(), (
            f"images.lock has an entry for {name!r} but {compose_path} does "
            f"not exist"
        )
        text = compose_path.read_text()
        assert f"@{entry['digest']}" in text, (
            f"{compose_path.name} does not use the locked digest for {name} "
            f"({entry['digest']})"
        )
