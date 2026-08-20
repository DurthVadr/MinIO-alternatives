"""Fixtures and an outcome collector for the S3 conformance matrix.

A conformance run is not pass/fail in the usual sense: an unsupported feature is
a finding, not a broken test. Every test therefore records a status rather than
only raising, and the session writes the whole matrix to JSON at the end.

Statuses written into results/<profile_id>/conformance.json:

  supported              the AWS-documented behaviour was observed end to end.

  diverges               implemented, but observably different from AWS. The
                         behaviour is there and does something; a client written
                         against S3 may still need to know. Example: returning
                         1001 keys with IsTruncated=False for an unbounded list.

  accepted_not_enforced  the call was accepted and reported success while the
                         guarantee it implies was never evaluated or enforced.
                         Sometimes nothing happened at all -- a retention header
                         stored and ignored, SSE headers echoed over an object
                         kept in plaintext. Sometimes the work happened and only
                         the condition on it was skipped: a conditional write
                         that lands because the precondition was never read
                         belongs here too, and is the more dangerous shape,
                         because the caller sees a success that means something
                         weaker than they asked for.

  not_implemented        cleanly absent or refused -- a 501, or a subresource
                         that is not routed at all. The detail carries the S3
                         error code, the HTTP status and the server's message.

  not_exercisable        the behaviour could not be exercised here. Every cell
                         with this status carries a "reason" field, because two
                         different claims live under it and any rendering that
                         drops the prose would merge them:
                           conformant_refusal    the server refused for a reason
                                                 AWS refuses for too (MinIO
                                                 declining SSE-C without TLS).
                                                 An affirmative claim that the
                                                 system behaved correctly.
                           missing_prerequisite  the feature needs something
                                                 this deployment does not supply
                                                 (a key manager). No claim
                                                 either way about the feature.

  error                  no verdict was reached. NOT a claim about the system;
                         it means this suite hit something it did not model.

`accepted_not_enforced` is separated from `not_implemented` deliberately, and it
is the most useful distinction in the matrix. "This is missing, plan around it"
and "you believe you have a guarantee and you do not" are different problems for
a reader, and only the second one fails silently in production. Collapsing them
into one bucket would hide exactly the finding this suite exists to surface.

Every entry becomes a published assertion about a named open-source product, so
the fallback status for a test that fails without recording anything is
deliberately "error": deriving a verdict from any failure is how a harness bug
becomes a public claim. Tests record their own verdict on every behavioural
path; a bare assert in a test is reserved for harness invariants, where "error"
is the right answer.
"""
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import boto3
import pytest
from botocore.client import Config
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
STACK = str(ROOT / "bench" / "stack.sh")
HWPROFILE = str(ROOT / "bench" / "hwprofile.sh")
ENDPOINT = "http://127.0.0.1:19000"
ACCESS_KEY = "benchuser"
SECRET_KEY = "benchsecret0"
# The systems bench/stack.sh knows how to start. SYSTEMS is only the default
# ordering (SYSTEMS[0] is what a run without --system targets) and may be
# narrowed by the environment; KNOWN_SYSTEMS is what a --system value is
# validated against, so narrowing the default list never rejects a valid name.
KNOWN_SYSTEMS = ("minio", "silo", "rustfs", "seaweedfs")
SYSTEMS = os.environ.get("CONFORMANCE_SYSTEMS", ",".join(KNOWN_SYSTEMS)).split(",")

_MATRIX = {}
_SYSTEM = None
_PROFILE = None


# The two pytest suites in this repo both drive the same host port through
# bench/stack.sh, so they are run separately:
#
#   .venv/bin/pytest conformance/ --system <name>
#
# pytest_collection_modifyitems below keeps a bare `pytest` at the repo root
# working the way it did before this directory existed.


def pytest_addoption(parser):
    parser.addoption("--system", action="store", default=None,
                     help="run the matrix against a single system")
    # Calibration aid: the stack up/down cycle dominates the wall clock when a
    # single test is being re-run against an already-running system. The
    # recorded matrix is identical either way -- the guard below refuses to run
    # if the named system's container is not the one actually up, so a result
    # can never be filed under the wrong system.
    parser.addoption("--no-stack", action="store_true", default=False,
                     help="assume the system is already running; do not start or stop it")


@pytest.fixture(scope="session")
def system(request):
    """One system per pytest session. The matrix is filled one system at a time.

    The 3.9 GB Docker VM cannot hold two of these stacks at once, and running
    them sequentially also keeps each system's results free of any interference
    from a neighbour.
    """
    global _SYSTEM, _PROFILE
    # default= matters: pytest only honours pytest_addoption from an initial
    # conftest, so --system is not registered when someone runs a bare `pytest`
    # at the repo root. Without the default that raises ValueError instead of
    # falling back.
    name = request.config.getoption("--system", default=None) or SYSTEMS[0]
    if name not in KNOWN_SYSTEMS:
        pytest.fail(f"unknown system {name!r}; expected one of {list(KNOWN_SYSTEMS)}")

    # Resolve the hardware profile up front rather than at session end: it names
    # the results directory, and a bad PROFILE_ID should stop the run before it
    # measures anything, not after.
    _PROFILE = json.loads(subprocess.run(
        [HWPROFILE], capture_output=True, text=True, check=True).stdout)

    if request.config.getoption("--no-stack", default=False):
        running = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}", "--filter", f"name=^bench-{name}$"],
            capture_output=True, text=True, check=True).stdout.split()
        if not running:
            pytest.fail(f"--no-stack given but no container named bench-{name} is running")
        _SYSTEM = name
        yield name
        return

    subprocess.run([STACK, "up", name, "c1"], check=True)
    _SYSTEM = name
    try:
        yield name
    finally:
        subprocess.run([STACK, "down", name], check=True)


@pytest.fixture(scope="session")
def s3(system):
    """A path-style SigV4 client, configured exactly as the rest of the harness.

    Deliberately the same botocore configuration the smoke test and the
    benchmark use: a conformance verdict has to describe the system as this
    study drives it, not a client tuned per behaviour.
    """
    client = boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                      retries={"max_attempts": 2}),
    )
    last_exc = None
    for _ in range(30):
        try:
            client.list_buckets()
            break
        except Exception as exc:
            last_exc = exc
            time.sleep(1)
    else:
        # Returning an unusable client here would file a connection failure
        # against every behaviour in the matrix as if the system had rejected it.
        pytest.fail(f"{system}: S3 client never became ready: {last_exc!r}")
    return client


def _purge(s3, bucket):
    """Best-effort removal of a bucket and everything in it, versions included."""
    try:
        try:
            for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket):
                doomed = [{"Key": v["Key"], "VersionId": v["VersionId"]}
                          for v in page.get("Versions", []) + page.get("DeleteMarkers", [])]
                if doomed:
                    s3.delete_objects(Bucket=bucket, Delete={"Objects": doomed})
        except ClientError:
            # Not every system implements ListObjectVersions; fall back to the
            # unversioned listing rather than leaving the bucket populated.
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
                doomed = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                if doomed:
                    s3.delete_objects(Bucket=bucket, Delete={"Objects": doomed})
        s3.delete_bucket(Bucket=bucket)
    except Exception:
        pass  # teardown failures must not mask findings


@pytest.fixture
def new_bucket(s3):
    """Factory for throwaway buckets, all cleaned up after the test.

    A factory rather than a single bucket because the Object Lock group needs
    buckets created with ObjectLockEnabledForBucket, which cannot be turned on
    after the fact.
    """
    created = []

    def _make(prefix="conf", **kwargs):
        name = f"{prefix}-{uuid.uuid4().hex[:12]}"
        s3.create_bucket(Bucket=name, **kwargs)
        created.append(name)
        return name

    yield _make
    for name in reversed(created):
        _purge(s3, name)


@pytest.fixture
def bucket(new_bucket):
    return new_bucket()


@pytest.fixture
def record(request):
    """Record this test's verdict for the system under test.

    Called explicitly by every test on every behavioural path. `observed` holds
    the structured evidence (S3 error code, HTTP status, server message) so a
    reader can judge a verdict instead of taking it on trust.
    """
    def _record(status, detail="", observed=None, reason=None):
        assert status in ("supported", "diverges", "accepted_not_enforced",
                          "not_implemented", "not_exercisable", "error"), status
        # not_exercisable is the one status that can assert the system behaved
        # correctly, or merely that we could not look -- opposite claims. It is
        # never written without saying which.
        if status == "not_exercisable":
            assert reason in ("conformant_refusal", "missing_prerequisite"), (
                f"not_exercisable requires a reason, got {reason!r}")
        else:
            assert reason is None, f"{status} must not carry a reason ({reason!r})"
        # 1400, not a few hundred: the details that explain *why* a system
        # failed (the unrouted-subresource evidence, the SSE-C key probes and
        # their transport caveat) are the whole value of a cell, and truncating
        # one mid-sentence publishes an explanation nobody can follow. Details
        # are also written so the interpretation leads and the evidence follows,
        # so that a truncation here can only ever cost evidence, never meaning.
        entry = {"status": status, "detail": detail[:1400]}
        if reason:
            entry["reason"] = reason
        if observed:
            entry["observed"] = observed
        _MATRIX.setdefault(_SYSTEM, {})[request.node.name] = entry
    return _record


def pytest_collection_modifyitems(config, items):
    """Refuse to fill the matrix from inside a mixed session.

    This suite holds one system up for the whole session; tests/ starts and
    stops all four in turn. Both bind host port 19000, so a bare `pytest` at the
    repo root that collected both would deadlock on the port rather than
    produce results. Skipping with a visible reason is better than either
    failing halfway through or silently recording a system's matrix while
    another system was actually answering.
    """
    here = Path(__file__).resolve().parent
    mine = [item for item in items if here in Path(item.path).resolve().parents]
    if not mine or len(mine) == len(items):
        return
    skip = pytest.mark.skip(
        reason="run the conformance matrix on its own: pytest conformance/ --system <name>")
    for item in mine:
        item.add_marker(skip)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if _SYSTEM is None:
        return
    entry = _MATRIX.setdefault(_SYSTEM, {})
    if item.name in entry:
        return  # the test recorded its own verdict

    # A fixture or setup failure used to record nothing at all, which dropped
    # the cell from the matrix entirely -- indistinguishable, once rendered,
    # from a system that was never run. It is not a verdict about the system,
    # so it is an error cell, but it has to be a cell.
    if report.when in ("setup", "teardown"):
        if report.failed:
            entry[item.name] = {
                "status": "error",
                "detail": f"{report.when} failed, no verdict reached: "
                          + str(report.longrepr).splitlines()[-1][:300],
            }
        return
    if report.passed:
        # Every test is meant to record explicitly; a silent pass means one of
        # them has a path that reaches the end without a verdict.
        entry[item.name] = {"status": "error",
                            "detail": "test passed without recording a verdict (suite bug)"}
        return
    if report.skipped:
        return
    entry[item.name] = {
        "status": "error",
        "detail": "unmodelled failure, no verdict recorded: "
                  + str(report.longrepr).splitlines()[-1][:300],
    }


def pytest_terminal_summary(terminalreporter):
    if not _MATRIX:
        return
    terminalreporter.section("conformance matrix")
    if terminalreporter.config.getoption("keyword", default="") or \
            terminalreporter.config.getoption("markexpr", default=""):
        terminalreporter.write_line(
            "NOTE: this session was filtered, so conformance.json now mixes these cells with "
            "the rest of a previous run. Re-run the whole file before publishing.")
    for name, tests in _MATRIX.items():
        counts = {}
        for entry in tests.values():
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        terminalreporter.write_line(
            f"{name}: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        for test_id, entry in sorted(tests.items()):
            if entry["status"] != "supported":
                terminalreporter.write_line(
                    f"  {entry['status']:<12} {test_id}: {entry['detail']}")


def pytest_sessionfinish(session, exitstatus):
    if not _MATRIX or _PROFILE is None:
        return
    out_dir = ROOT / "results" / _PROFILE["profile_id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "conformance.json"

    # Merge into the existing matrix, not over the whole file: the four systems
    # are filled in by four separate pytest sessions, so reading the file's
    # top-level object and updating that would nest the previous session's
    # results (and its hardware_profile) underneath "matrix".
    merged = {}
    if path.exists():
        try:
            previous = json.loads(path.read_text())
            if isinstance(previous, dict) and isinstance(previous.get("matrix"), dict):
                merged.update(previous["matrix"])
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            merged = {}  # a corrupt file protects nothing
    # Merge per cell, not per system. `pytest conformance/ -k sse` is a normal
    # way to re-check one behaviour, and replacing a system's whole cell dict
    # with the filtered subset would silently truncate the published artifact --
    # the missing cells leave nothing behind to show they were ever there.
    for name, cells in _MATRIX.items():
        merged.setdefault(name, {}).update(cells)
    path.write_text(json.dumps(
        {"schema": 1, "hardware_profile": _PROFILE, "matrix": merged},
        indent=2, sort_keys=True) + "\n")
