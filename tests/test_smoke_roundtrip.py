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


def _client(access_key, secret_key):
    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


@pytest.fixture
def s3(system):
    client = _client("benchuser", "benchsecret0")
    # The endpoint answers before it is fully ready on some systems; retry briefly.
    last_exc = None
    for _ in range(30):
        try:
            client.list_buckets()
            break
        except Exception as exc:
            last_exc = exc
            time.sleep(1)
    else:
        pytest.fail(f"{system}: S3 client never became ready: {last_exc!r}")
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

    # Auth must actually be enforced, not just accepted when correct. This is
    # the regression guard for the SeaweedFS finding from this task's initial
    # implementation: env-var credentials (AWS_ACCESS_KEY_ID/SECRET_ACCESS_KEY)
    # were silently ignored by the S3 gateway, which served every request --
    # including unsigned ones -- anonymously. Only a `-s3.config` identity
    # file actually wires up enforcement. Without this assertion, deleting
    # that fix leaves all other tests green while the harness benchmarks a
    # wide-open endpoint.
    #
    # create_bucket is not a valid probe: SeaweedFS returns
    # BucketAlreadyExists/409 for it even under correct auth (existence still
    # leaks pre-auth there), so it can't distinguish "rejected" from
    # "succeeded, bucket already existed". get_object/list_objects_v2 against
    # a real, already-written key/bucket do reject wrong credentials
    # consistently -- verified empirically against all four systems: every
    # one returns InvalidAccessKeyId / HTTP 403.
    bad = _client("wronguser", "wrongsecret0")
    with pytest.raises(ClientError) as err:
        bad.get_object(Bucket=bucket, Key=key)
    code = err.value.response["Error"]["Code"]
    status = err.value.response["ResponseMetadata"]["HTTPStatusCode"]
    assert code == "InvalidAccessKeyId" and status == 403, (
        f"{system}: wrong credentials were not rejected (code={code}, status={status})"
    )

    s3.delete_object(Bucket=bucket, Key=key)
    with pytest.raises(ClientError) as err:
        s3.get_object(Bucket=bucket, Key=key)
    assert err.value.response["Error"]["Code"] in ("NoSuchKey", "404")
