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
import urllib.request
from pathlib import Path

import boto3
import pytest
import yaml
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.client import Config
from botocore.credentials import Credentials

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
# How long the read keeps being retried after redundancy has been deliberately
# exceeded, before the object is called genuinely unreadable.
UNREADABLE_DEADLINE_S = 10


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
    """Apparent size of a path inside a container; 0 if it does not exist."""
    rc, out = docker_exec(container, f"du -sb {path} 2>/dev/null | cut -f1")
    return int(out) if rc == 0 and out.isdigit() else 0


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


def read_probe(s3, deadline_s):
    """Read the probe object, retrying until deadline_s has elapsed.

    Returns (ok, seconds, last_error). ok is True only if the bytes came back
    identical: a short or corrupt body is a failure, not a success. Both
    outcomes are results -- neither is treated as an error here.
    """
    start = time.monotonic()
    last_error = None
    while True:
        try:
            body = s3.get_object(Bucket=BUCKET, Key=KEY)["Body"].read()
            if body == PAYLOAD:
                return True, time.monotonic() - start, None
            last_error = f"content mismatch: {len(body)} of {len(PAYLOAD)} bytes"
        except Exception as exc:  # noqa: BLE001 - any failure to read is the result
            last_error = f"{type(exc).__name__}: {exc}"
        if time.monotonic() - start >= deadline_s:
            return False, time.monotonic() - start, last_error
        time.sleep(2)


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
        before = path_bytes(container, f"{VICTIM_DRIVE}/{BUCKET}")
        assert before > 0, (
            f"{system}: {VICTIM_DRIVE} holds no data for bucket {BUCKET}, so "
            f"destroying it would not be a fault at all"
        )
        # Contents only, dotfiles included (.minio.sys / .rustfs.sys and the
        # format marker live there): the mount survives with nothing on it,
        # which is exactly what a swapped-in blank disk looks like.
        docker_exec(
            container,
            f"rm -rf {VICTIM_DRIVE}/..?* {VICTIM_DRIVE}/.[!.]* {VICTIM_DRIVE}/* 2>/dev/null; "
            f"exit 0",
        )
        after = path_bytes(container, VICTIM_DRIVE)
        assert after < before, (
            f"{system}: {VICTIM_DRIVE} still holds {after} bytes after the wipe "
            f"(was {before}); the device was not destroyed"
        )
        return (
            f"{VICTIM_DRIVE} wiped (1 of 4 drives)",
            {"device_bytes_before_kill": before, "device_bytes_after_kill": after},
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
    subprocess.run([STACK, "up", system, "c2"], check=True)
    try:
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
        storage = json.loads(
            subprocess.run(
                [MEASURE, "--measure-only", system],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )
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
        still_readable, _, unreadable_error = read_probe(
            s3, deadline_s=UNREADABLE_DEADLINE_S
        )
        durability_results[system]["evidence"].update({
            "redundancy_limit_action": action,
            "redundancy_limit_read_failed": not still_readable,
            "redundancy_limit_error": unreadable_error,
        })
        assert not still_readable, (
            f"{system}: the object is still readable after {action}. The fault "
            f"injection is not destroying data, so the durability result above "
            f"proves nothing."
        )
    finally:
        subprocess.run([STACK, "down", system], check=True)
