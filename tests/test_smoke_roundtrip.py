"""Every system must survive a put/get/delete roundtrip before it is benchmarked.

These run against the host-mapped port (19000). The benchmark itself talks over
the bridge network, but for a correctness smoke test the host port is simpler
and the extra latency does not matter.
"""
import subprocess
import time
from pathlib import Path

import boto3
import pytest
from botocore.client import Config
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
STACK = str(ROOT / "bench" / "stack.sh")
SYSTEMS = ["minio", "silo", "rustfs", "seaweedfs"]
ENDPOINT = "http://127.0.0.1:19000"


@pytest.fixture(params=SYSTEMS)
def system(request):
    name = request.param
    subprocess.run([STACK, "up", name, "c1"], check=True)
    try:
        yield name
    finally:
        subprocess.run([STACK, "down", name], check=True)


@pytest.fixture
def s3(system):
    client = boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id="benchuser",
        aws_secret_access_key="benchsecret0",
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    # The endpoint answers before it is fully ready on some systems; retry briefly.
    for _ in range(30):
        try:
            client.list_buckets()
            break
        except Exception:
            time.sleep(1)
    return client


def test_put_get_delete_roundtrip(s3, system):
    bucket = "smoke"
    payload = b"hello from the benchmark harness"
    key = "roundtrip/object.bin"

    s3.create_bucket(Bucket=bucket)
    s3.put_object(Bucket=bucket, Key=key, Body=payload)

    got = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    assert got == payload, f"{system}: content mismatch"

    listed = s3.list_objects_v2(Bucket=bucket, Prefix="roundtrip/")
    assert [o["Key"] for o in listed.get("Contents", [])] == [key]

    s3.delete_object(Bucket=bucket, Key=key)
    with pytest.raises(ClientError) as err:
        s3.get_object(Bucket=bucket, Key=key)
    assert err.value.response["Error"]["Code"] in ("NoSuchKey", "404")
