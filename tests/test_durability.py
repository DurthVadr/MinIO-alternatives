"""Prove the durability claim instead of asserting it.

Every c2 config in this study claims the same thing: it survives the loss of
one device. That claim is what makes the storage-efficiency comparison
meaningful -- same fault tolerance, different storage cost -- so it is
established by destroying a device rather than by reading a config file.

Per system: bring c2 up, measure what 256 MiB of incompressible data actually
costs on disk, write a probe object, destroy one device that demonstrably
holds part of that object, and require the object to read back byte-identical.
Then destroy one device MORE than the configuration tolerates and require the
read to fail: without that second step, a green result would not distinguish
"reconstructed from redundancy" from "the fault injection did nothing".

That past-limit read is sampled several times, over the wire as well as
through the SDK, and every distinct outcome is recorded with its count. Two
reasons, both learned the hard way. A system can answer the same fault more
than one way depending on where its healing has got to, so one error string
published as characteristic behaviour is a claim the evidence does not
support. And an SDK-only record loses the HTTP status entirely when a server
answers with a malformed error response -- which is how a 500 with an
overstated Content-Length once got written up as a 200 with a truncated body.

Observations land in results/<profile_id>/durability.json (see conftest.py),
which the report consumes.

Nothing here is taken from configuration alone. The redundancy mechanism is
read back from each running server and cross-checked against what the compose
file asked for, so a setting that silently fails to take effect -- the exact
failure mode this harness has already hit twice -- surfaces as a test failure
instead of as a benchmark of something other than what the report claims.
"""
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import boto3
import pytest
import yaml
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.client import Config
from botocore.credentials import Credentials
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
STACK = str(ROOT / "bench" / "stack.sh")
MEASURE = str(ROOT / "bench" / "measure-usable-ratio.sh")
COMPOSE_DIR = ROOT / "compose"
ENDPOINT = "http://127.0.0.1:19000"
ACCESS_KEY = "benchuser"
SECRET_KEY = "benchsecret0"
BUCKET = "durability"
KEY = "probe.bin"

# 8 MiB of incompressible data. Size matters: it is well past MinIO's and
# RustFS's inline-metadata threshold, so every drive holds a real part file
# (verified: /data3/durability/probe.bin/<uuid>/part.1) and wiping a drive
# therefore destroys object data rather than only a metadata copy. Randomness
# matters too -- SeaweedFS compresses on write, and a repeating payload came
# back from its filer marked "is_compressed": true.
PAYLOAD = os.urandom(8 * 1024 * 1024)

# Every system that offers a single-device-loss configuration. RustFS is in
# the list: its rc build does erasure-code across four drives with a
# configurable parity (see the c2 comment block in compose/rustfs.yaml).
C2_SYSTEMS = ["minio", "silo", "rustfs", "seaweedfs"]

# How a device dies differs by architecture. MinIO, Silo and RustFS spread one
# process over four mounted volumes, so a device is a drive and it dies by
# having its contents destroyed -- the state a swapped-in blank disk is in.
# SeaweedFS runs four independent volume servers, so a device is a server and
# it dies by being stopped.
EC_SYSTEMS = ("minio", "silo", "rustfs")
CONTAINER = {
    "minio": "bench-minio",
    "silo": "bench-silo",
    "rustfs": "bench-rustfs",
    "seaweedfs": "bench-seaweedfs",  # master + filer + S3 gateway
}
# The c2 service inside each compose file, for reading back what was asked for.
C2_SERVICE = {
    "minio": "minio-c2",
    "silo": "silo-c2",
    "rustfs": "rustfs-c2",
    "seaweedfs": "swfs-master",
}
VICTIM_DRIVE = "/data3"
EXTRA_VICTIM_DRIVES = ("/data1", "/data2")

# How long a post-fault read may take before the loss is called permanent.
# Generous on purpose: a slow read after a device dies is a healing-latency
# result, not a data-loss result, and the two must not be confused. The
# elapsed time is recorded either way.
READ_DEADLINE_S = 60
# How many times the read is repeated after redundancy has been deliberately
# exceeded. One sample is not behaviour: a system can answer the same fault
# several different ways depending on where its healing has got to, and
# recording one draw as though it were characteristic is how a single error
# string turns into a published claim about a named product.
PAST_LIMIT_SAMPLES = 5
PAST_LIMIT_INTERVAL_S = 2


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------
def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            # One attempt per call: this test does its own timed retrying, and
            # botocore's internal retries would distort how long a post-fault
            # read actually took.
            retries={"max_attempts": 1, "mode": "standard"},
        ),
    )


def docker_exec(container, script):
    """Run a shell snippet inside a container, returning (rc, stdout)."""
    proc = subprocess.run(
        ["docker", "exec", container, "sh", "-c", script],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout.strip()


def path_bytes(container, path):
    """Apparent size of a path inside a container, in bytes.

    Returns None when the path does not exist -- never 0, and never a silent 0
    for a measurement that failed. The two used to be indistinguishable, which
    put a failed measurement on the pass side of the post-wipe check ("the
    object's bytes are gone") for the wrong reason. A measurement that cannot
    be made at all now raises instead of quietly answering zero.
    """
    rc, out = docker_exec(
        container, f"if [ -e {path} ]; then du -sb {path} | cut -f1; else echo MISSING; fi"
    )
    assert rc == 0, f"could not measure {path} in {container} (rc={rc})"
    if out == "MISSING":
        return None
    assert out.isdigit(), f"unparseable du output for {path} in {container}: {out!r}"
    return int(out)


def admin_backend():
    """The running server's own account of its redundancy scheme.

    MinIO, Silo and RustFS all serve MinIO's admin API; RustFS wraps the same
    document in an "info" key. Requests are signed with botocore's S3 SigV4
    signer specifically -- the plain SigV4Auth signer is rejected with 403,
    because the admin API insists on the x-amz-content-sha256 header that only
    the S3 variant adds.
    """
    url = ENDPOINT + "/minio/admin/v3/info"
    signed = AWSRequest(method="GET", url=url, data=b"")
    S3SigV4Auth(Credentials(ACCESS_KEY, SECRET_KEY), "s3", "us-east-1").add_auth(signed)
    request = urllib.request.Request(url, headers=dict(signed.headers))
    with urllib.request.urlopen(request, timeout=20) as response:
        document = json.loads(response.read())
    return document.get("info", document)["backend"]


def wget_json(container, url):
    """Fetch JSON from inside a container.

    SeaweedFS's master and filer ports are deliberately not published to the
    host (only the S3 port is), so its topology is read from in there.
    """
    rc, out = docker_exec(
        container, f"wget -qO- --header='Accept: application/json' '{url}'"
    )
    assert rc == 0 and out, f"no response from {url} (rc={rc})"
    return json.loads(out)


def c2_service(system):
    document = yaml.safe_load((COMPOSE_DIR / f"{system}.yaml").read_text())
    return document["services"][C2_SERVICE[system]]


def configured_parity(system):
    """The EC:<parity> the compose file asks for, whatever the env var is called."""
    environment = c2_service(system)["environment"]
    value = next(v for k, v in environment.items() if k.endswith("STORAGE_CLASS_STANDARD"))
    scheme, _, parity = value.partition(":")
    assert scheme == "EC", f"{system}: unexpected storage class scheme {value!r}"
    return int(parity)


def configured_replication(system="seaweedfs"):
    """The replication code the compose file asks SeaweedFS's master for."""
    command = " ".join(c2_service(system)["command"].split())
    match = re.search(r"-master\.defaultReplication=(\d{3})", command)
    assert match, f"{system}: no -master.defaultReplication in the c2 command"
    return match.group(1)


def sdk_read_once(s3):
    """One read through boto3 -- what an S3 SDK user would see.

    Returns a dict rather than a bare string so the HTTP status survives when
    the SDK has one. It often does not: a malformed error response surfaces as
    a transport error with no status and no S3 code at all, which is precisely
    the shape that got SeaweedFS's behaviour misdescribed once already.
    """
    try:
        body = s3.get_object(Bucket=BUCKET, Key=KEY)["Body"].read()
    except ClientError as exc:
        error = exc.response.get("Error", {})
        return {
            "readable": False,
            "sdk_outcome": (
                f"ClientError ({error.get('Code')}): {error.get('Message')}"
            ),
            "sdk_http_status": exc.response.get("ResponseMetadata", {}).get(
                "HTTPStatusCode"
            ),
        }
    except Exception as exc:  # noqa: BLE001 - any failure to read is the result
        return {
            "readable": False,
            "sdk_outcome": f"{type(exc).__name__}: {exc}",
            "sdk_http_status": None,
        }
    if body == PAYLOAD:
        return {
            "readable": True,
            "sdk_outcome": "object read back byte-identical",
            "sdk_http_status": 200,
        }
    return {
        "readable": False,
        "sdk_outcome": f"content mismatch: {len(body)} of {len(PAYLOAD)} bytes",
        "sdk_http_status": 200,
    }


# Non-default settings a system needed before its c2 config would run at all.
# These ride along in durability.json rather than living only in a report,
# because durability.json is the only artifact the report generator reads: a
# caveat that depends on a later task remembering a paragraph is a caveat that
# will eventually go missing. Keyed by the compose env var that carries it, so
# removing the setting removes the disclosure with it.
CONFIG_CAVEATS = {
    ("rustfs", "RUSTFS_UNSAFE_BYPASS_DISK_CHECK"): {
        "setting": "RUSTFS_UNSAFE_BYPASS_DISK_CHECK=true",
        "why": (
            "RustFS refuses to start in erasure mode when its drives share a "
            "physical device, which every c2 config in this study does -- four "
            "drives on one host disk."
        ),
        "refusal_message": (
            "local erasure endpoints must use distinct physical disks; detected "
            "shared devices [vda => /data0, /data1, /data2, /data3] ... Set "
            "RUSTFS_UNSAFE_BYPASS_DISK_CHECK=true only for local testing or CI "
            "to bypass this safety check"
        ),
        "peer_behaviour": (
            "MinIO and Silo meet the identical shared-disk condition and only "
            "warn ('Host local has more than 1 drives of set. A host failure "
            "will result in data becoming unavailable.'); SeaweedFS has no "
            "equivalent check at all. The bypass equalises the comparison "
            "rather than relaxing it for RustFS -- and RustFS is the only one "
            "of the four that treats the condition as an error."
        ),
        "also_required": (
            "user: root -- the image runs as uid 10001 and only /data is "
            "pre-owned by it, so fresh /data0../data3 mounts are root-owned "
            "and the server dies with 'Io error: Permission denied (os error "
            "13)'. compose/minio.yaml needs the same workaround."
        ),
    },
}


def configuration_caveats(system):
    """Caveats that apply to this system's c2 config, derived from the compose
    file so a caveat cannot outlive the setting that caused it."""
    environment = c2_service(system).get("environment") or {}
    return [
        caveat
        for (caveat_system, variable), caveat in CONFIG_CAVEATS.items()
        if caveat_system == system and variable in environment
    ]


def read_probe(s3, deadline_s):
    """Read the probe object, retrying until deadline_s has elapsed.

    Returns (ok, seconds, last_error). ok is True only if the bytes came back
    identical: a short or corrupt body is a failure, not a success. Both
    outcomes are results -- neither is treated as an error here.
    """
    start = time.monotonic()
    last_error = None
    while True:
        attempt = sdk_read_once(s3)
        if attempt["readable"]:
            return True, time.monotonic() - start, None
        last_error = attempt["sdk_outcome"]
        if time.monotonic() - start >= deadline_s:
            return False, time.monotonic() - start, last_error
        time.sleep(2)


def raw_probe():
    """Fetch the probe object over plain HTTP and record what the wire said.

    The SDK view is not enough evidence on its own. When a server answers a
    failed read with a malformed response -- a status the SDK never surfaces,
    a Content-Length that overstates the body, a non-XML error payload --
    boto3 reports only a transport error, and reading intent into that is
    guesswork. This records the status line, the Content-Length claimed, and
    how many bytes actually arrived, so the artifact cannot be misread.
    """
    url = f"{ENDPOINT}/{BUCKET}/{KEY}"
    signed = AWSRequest(method="GET", url=url, data=b"")
    S3SigV4Auth(Credentials(ACCESS_KEY, SECRET_KEY), "s3", "us-east-1").add_auth(signed)
    request = urllib.request.Request(url, headers=dict(signed.headers))
    sample = {
        "http_status": None,
        "content_length_header": None,
        "content_type_header": None,
        "body_bytes_received": None,
        "body_excerpt": None,
        "wire_error": None,
    }
    try:
        response = urllib.request.urlopen(request, timeout=30)
    except urllib.error.HTTPError as exc:
        response = exc  # a non-2xx is still a real response, headers and all
    except Exception as exc:  # noqa: BLE001
        sample["wire_error"] = f"{type(exc).__name__}: {exc}"
        return sample
    with response:
        sample["http_status"] = response.status
        sample["content_length_header"] = response.headers.get("Content-Length")
        sample["content_type_header"] = response.headers.get("Content-Type")
        try:
            body = response.read()
        except Exception as exc:  # noqa: BLE001 - a truncated body is the finding
            sample["wire_error"] = f"{type(exc).__name__}: {exc}"
            body = getattr(exc, "partial", b"")
        sample["body_bytes_received"] = len(body)
        if body != PAYLOAD:
            sample["body_excerpt"] = body[:200].decode("utf-8", "replace")
    return sample


def outcome_key(sample):
    """A short, countable label for one past-limit read."""
    if sample["readable"]:
        return "object read back byte-identical"
    sdk = sample["sdk_outcome"] or ""
    match = re.search(r"\(([A-Za-z]+)\)", sdk)
    code = match.group(1) if match else (sdk.split(":")[0] or "unknown")
    status = sample["http_status"]
    return f"HTTP {status if status is not None else 'none'} / {code}"


def sample_past_limit_reads(s3, samples=PAST_LIMIT_SAMPLES):
    """Read the object repeatedly after redundancy has been exceeded.

    Each sample is two requests -- one raw, one through boto3 -- so the wire's
    account and the SDK's account of roughly the same moment are both on
    record. Several samples because a system can answer the same fault more
    than one way depending on where its healing has got to; every distinct
    outcome is returned with its count, rather than one draw standing in for
    the system's behaviour.
    """
    observed = []
    for index in range(samples):
        wire = raw_probe()
        observed.append({**wire, **sdk_read_once(s3)})
        if index + 1 < samples:
            time.sleep(PAST_LIMIT_INTERVAL_S)
    counts = {}
    for sample in observed:
        key = outcome_key(sample)
        counts[key] = counts.get(key, 0) + 1
    return observed, counts


# --------------------------------------------------------------------------
# observing what redundancy is actually in effect
# --------------------------------------------------------------------------
def ec_mechanism(system):
    backend = admin_backend()
    parity = backend["standardSCParity"]
    drives = backend["totalDrivesPerSet"][0]
    asked_for = configured_parity(system)
    assert backend["backendType"] == "Erasure", f"{system}: backend is {backend['backendType']}"
    assert drives == 4, f"{system}: {drives} drives per set, expected 4"
    assert backend["offlineDisks"] == 0, (
        f"{system}: {backend['offlineDisks']} drives already offline before any fault"
    )
    assert parity == asked_for, (
        f"{system}: compose asks for EC:{asked_for} but the server reports parity "
        f"{parity} -- the storage class did not take effect"
    )
    assert parity >= 1, f"{system}: parity {parity} tolerates no drive loss"
    return f"erasure coding, {drives} drives, parity {parity}"


def seaweed_volume_servers():
    topology = wget_json(CONTAINER["seaweedfs"], "http://127.0.0.1:9333/dir/status")
    nodes = [
        node["Url"].split(":")[0]
        for datacenter in topology["Topology"]["DataCenters"]
        for rack in datacenter["Racks"]
        for node in rack["DataNodes"]
    ]
    layouts = {layout["replication"] for layout in topology["Topology"]["Layouts"]}
    return nodes, layouts


def seaweed_object_replicas():
    """Map every volume holding the probe object to the volume servers that
    hold a copy of it -- read from the filer and the master, never assumed."""
    container = CONTAINER["seaweedfs"]
    metadata = wget_json(
        container, f"http://127.0.0.1:8888/buckets/{BUCKET}/{KEY}?metadata=true"
    )
    volume_ids = sorted({chunk["fid"]["volume_id"] for chunk in metadata.get("chunks") or []})
    assert volume_ids, "seaweedfs: the filer reports no chunks for the probe object"
    replicas = {}
    for volume_id in volume_ids:
        lookup = wget_json(container, f"http://127.0.0.1:9333/dir/lookup?volumeId={volume_id}")
        replicas[volume_id] = sorted(
            location["url"].split(":")[0] for location in lookup["locations"]
        )
    return replicas


def seaweed_mechanism(replicas):
    code = configured_replication()
    # SeaweedFS replication xyz: copies on other data centres, other racks,
    # other servers in the same rack. Any volume therefore exists 1+x+y+z times.
    expected_copies = 1 + sum(int(digit) for digit in code)
    nodes, layouts = seaweed_volume_servers()
    assert layouts == {code}, (
        f"seaweedfs: compose asks for replication {code} but the master reports "
        f"layouts {sorted(layouts)} -- the flag did not take effect"
    )
    for volume_id, servers in replicas.items():
        assert len(servers) == expected_copies, (
            f"seaweedfs: volume {volume_id} has {len(servers)} copies ({servers}), "
            f"expected {expected_copies} for replication {code}"
        )
    return f"replication {code}, {expected_copies} copies across {len(nodes)} volume servers"


# --------------------------------------------------------------------------
# fault injection
# --------------------------------------------------------------------------
def kill_one_device(system, replicas):
    """Destroy exactly one device that demonstrably holds part of the probe
    object. Returns (description, evidence)."""
    container = CONTAINER[system]
    if system in EC_SYSTEMS:
        # Confirm the drive really holds this object's data before destroying
        # it: otherwise "survived" could just mean nothing was lost.
        object_before = path_bytes(container, f"{VICTIM_DRIVE}/{BUCKET}")
        drive_before = path_bytes(container, VICTIM_DRIVE)
        assert object_before is not None and object_before > 0, (
            f"{system}: {VICTIM_DRIVE} holds no data for bucket {BUCKET} "
            f"(measured {object_before!r}), so destroying it would not be a "
            f"fault at all"
        )
        # Contents only, dotfiles included (.minio.sys / .rustfs.sys and the
        # format marker live there): the mount survives with nothing on it,
        # which is exactly what a swapped-in blank disk looks like.
        docker_exec(
            container,
            f"rm -rf {VICTIM_DRIVE}/..?* {VICTIM_DRIVE}/.[!.]* {VICTIM_DRIVE}/* 2>/dev/null; "
            f"exit 0",
        )
        # None here is the expected outcome: the bucket's whole tree is gone
        # from this drive. A real 0 would do as well; anything else has not
        # been destroyed.
        object_after = path_bytes(container, f"{VICTIM_DRIVE}/{BUCKET}")
        drive_after = path_bytes(container, VICTIM_DRIVE)
        assert object_after in (None, 0), (
            f"{system}: {VICTIM_DRIVE} still holds {object_after} bytes of "
            f"{BUCKET} (was {object_before}); the device was not destroyed"
        )
        assert drive_after is not None and drive_after < drive_before, (
            f"{system}: {VICTIM_DRIVE} measures {drive_after!r} after the wipe "
            f"(was {drive_before}); the device was not destroyed"
        )
        return (
            f"{VICTIM_DRIVE} wiped (1 of 4 drives)",
            {
                "object_bytes_on_device_before_kill": object_before,
                "object_bytes_on_device_after_kill": object_after,
                "object_path_after_kill": "absent" if object_after is None else "present",
                "device_bytes_before_kill": drive_before,
                "device_bytes_after_kill": drive_after,
            },
        )

    # SeaweedFS: stop a volume server that actually holds a copy, choosing the
    # one holding the most of this object's volumes. Picking a server blindly
    # (say, always vol3) would often stop a server holding nothing of this
    # object -- verified: an 8 MiB probe landed entirely on volume 4, hosted
    # by vol1 and vol2, so stopping vol3 would have been a no-op dressed up as
    # a fault.
    servers = {server for holders in replicas.values() for server in holders}
    victim = max(
        servers, key=lambda server: (sum(server in h for h in replicas.values()), server)
    )
    # Count the fleet BEFORE the kill: the master drops a stopped node from its
    # topology, so asking afterwards would report one server fewer than the
    # cluster actually has and understate the fleet the loss happened in.
    total_servers = len(seaweed_volume_servers()[0])
    subprocess.run(["docker", "stop", victim], check=True, capture_output=True)
    held = [volume for volume, holders in replicas.items() if victim in holders]
    return (
        f"{victim} stopped (1 of {total_servers} volume servers, holding "
        f"{len(held)} of the object's {len(replicas)} volumes)",
        {
            "stopped_server": victim,
            "volumes_held_by_victim": held,
            "object_replicas": {str(volume): holders for volume, holders in replicas.items()},
        },
    )


def exceed_redundancy(system, replicas, evidence):
    """Destroy one device more than the configuration can tolerate.

    This is the negative control: after it, the object MUST be unreadable. If
    it is not, the fault injection is not destroying data and the result above
    means nothing.
    """
    container = CONTAINER[system]
    if system in EC_SYSTEMS:
        drives = " ".join(EXTRA_VICTIM_DRIVES)
        docker_exec(
            container,
            f"for d in {drives}; do rm -rf $d/..?* $d/.[!.]* $d/* 2>/dev/null; done; exit 0",
        )
        # Two drives short of four with parity 1 leaves 2 shards where 3 are
        # needed -- unreadable whether or not the first drive has healed yet.
        return f"{', '.join(EXTRA_VICTIM_DRIVES)} wiped as well (parity 1 cannot cover 2 lost drives)"

    # Take out the remaining holders of a volume the first fault already hit,
    # so one volume of the object has no live copy anywhere.
    victim = evidence["stopped_server"]
    volume_id = evidence["volumes_held_by_victim"][0]
    for server in replicas[volume_id]:
        subprocess.run(["docker", "stop", server], check=False, capture_output=True)
    return (
        f"every server holding volume {volume_id} stopped "
        f"({', '.join(replicas[volume_id])}, including {victim})"
    )


# --------------------------------------------------------------------------
# the test
# --------------------------------------------------------------------------
@pytest.mark.parametrize("system", C2_SYSTEMS)
def test_survives_single_device_loss(system, durability_results):
    try:
        # Inside the try: a stack that fails halfway up still has containers
        # and volumes to remove, and leaving them behind makes the NEXT
        # system's `up` collide on the container name.
        subprocess.run([STACK, "up", system, "c2"], check=True)

        s3 = s3_client()
        last_error = None
        for _ in range(30):
            try:
                s3.list_buckets()
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(1)
        else:
            pytest.fail(f"{system}: S3 client never became ready: {last_error!r}")

        # Storage efficiency, measured on the pristine stack before anything is
        # broken. Same boot: bringing every system up a second time just to
        # weigh its volumes would double the slowest part of the run.
        measured = subprocess.run(
            [MEASURE, "--measure-only", system], capture_output=True, text=True
        )
        assert measured.returncode == 0, (
            f"{system}: measure-usable-ratio.sh failed "
            f"(rc={measured.returncode}): {measured.stderr.strip()[-2000:]}"
        )
        storage = json.loads(measured.stdout)
        usable_ratio = storage["usable_ratio"]
        assert isinstance(usable_ratio, float) and usable_ratio > 0, (
            f"{system}: usable_ratio came out as {usable_ratio!r}"
        )

        s3.create_bucket(Bucket=BUCKET)
        s3.put_object(Bucket=BUCKET, Key=KEY, Body=PAYLOAD)
        readable, _, error = read_probe(s3, deadline_s=0)
        assert readable, f"{system}: probe object unreadable before any fault ({error})"

        replicas = seaweed_object_replicas() if system == "seaweedfs" else {}
        mechanism = (
            seaweed_mechanism(replicas) if system == "seaweedfs" else ec_mechanism(system)
        )

        device_killed, evidence = kill_one_device(system, replicas)
        tolerated, seconds, error = read_probe(s3, deadline_s=READ_DEADLINE_S)

        # Record before asserting: a system that loses data is a legitimate
        # result and has to reach the report, not just the pytest output.
        durability_results[system] = {
            "fault_tolerated": tolerated,
            "device_killed": device_killed,
            "mechanism": mechanism,
            "usable_ratio": usable_ratio,
            # The ratio was measured with ONE object of this size, so it
            # captures stripe and replica overhead but not per-object
            # metadata -- erasure systems write an xl.meta on all four drives
            # for every object, so many small objects cost more than this
            # figure implies. Recorded next to the number so the report can
            # say which it is showing instead of implying a general figure.
            "usable_ratio_object_bytes": storage["logical_bytes"],
            "usable_ratio_object_count": storage["object_count"],
            "configuration_caveats": configuration_caveats(system),
            "evidence": {
                "payload_bytes": len(PAYLOAD),
                "read_after_fault_seconds": round(seconds, 2),
                "read_after_fault_error": error,
                "storage": storage,
                **evidence,
            },
        }
        assert tolerated, (
            f"{system}: the object was lost after {device_killed} ({error}) -- "
            f"{mechanism} did not hold"
        )

        action = exceed_redundancy(system, replicas, evidence)
        samples, distinct_outcomes = sample_past_limit_reads(s3)
        still_readable = any(sample["readable"] for sample in samples)
        durability_results[system]["evidence"].update({
            "redundancy_limit_action": action,
            "redundancy_limit_read_failed": not still_readable,
            "redundancy_limit_samples": samples,
            "redundancy_limit_distinct_outcomes": distinct_outcomes,
        })
        assert not still_readable, (
            f"{system}: the object is still readable after {action}. The fault "
            f"injection is not destroying data, so the durability result above "
            f"proves nothing."
        )
    finally:
        subprocess.run([STACK, "down", system], check=True)
