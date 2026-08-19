"""S3 conformance matrix.

Each test asserts AWS-documented behaviour against one system and records a
verdict with the evidence behind it. Every cell becomes a published assertion
about a named open-source product, so a verdict states what was observed -- the
S3 error code, the HTTP status, the server's message, or the state of the object
afterwards when nothing was raised at all.

The status vocabulary separates the failure modes that a reader has to act on
differently (see conformance/conftest.py for the full contract). The one that
earns its own name is `accepted_not_enforced`: a call that returns success while
doing nothing is not the same finding as a call that is cleanly absent. "This is
missing, plan around it" and "you believe you have a guarantee and you do not"
are different problems, and only the second one fails silently in production.

Three shapes are checked deliberately throughout:

* Silent success. A store that accepts a precondition it does not implement and
  overwrites anyway does not error, it loses data. Asserting "an error is
  raised" would miss exactly the case that matters most, so every conditional
  behaviour also inspects the object afterwards.
* Vacuous rejection. A store that refuses every conditional write would satisfy
  a test that only checks for a 412. Each rejection test is therefore paired
  with a test that the same conditional call succeeds when it should.
* Vacuous acceptance. The mirror image, and subtler: a store that ignores a
  precondition accepts the call it was supposed to accept, for the wrong
  reason. The tests whose expected outcome is success therefore probe a second
  time with a precondition that must fail, and record
  `accepted_not_enforced` when that one is accepted too -- otherwise the cell
  would read as conformance in a rendered table.

Bare asserts here guard harness invariants (was the header really sent?), where
"error" -- not a claim about the system -- is the right recorded outcome.
"""
import base64
import hashlib
import json as _json
import os as _os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError

BODY = b"conformance payload"
SECOND = b"second write - must not land"


@contextmanager
def capture_request(client, operation):
    """Capture the HTTP request botocore actually puts on the wire.

    The conditional-write group turns on parameters (IfNoneMatch, IfMatch) that
    an older or newer botocore can drop silently. A dropped parameter turns an
    unremarkable overwrite into a published "precondition ignored" claim about
    somebody's software, so the tests that depend on one assert the header was
    really sent before they record anything.

    Lives here rather than in conftest.py: `from conftest import ...` binds to
    whichever conftest module was imported first, so in a session that also
    collected tests/ it resolved to tests/conftest.py and this module failed to
    import outright.
    """
    seen = {}

    def _handler(request, **kwargs):
        seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
        seen["method"] = request.method
        seen["url"] = request.url

    event = f"before-send.s3.{operation}"
    client.meta.events.register(event, _handler)
    try:
        yield seen
    finally:
        client.meta.events.unregister(event, _handler)


# --------------------------------------------------------------------------
# Evidence helpers
# --------------------------------------------------------------------------

def _observed(err):
    """The publishable evidence behind a rejection."""
    response = getattr(err, "response", None) or {}
    error = response.get("Error", {}) or {}
    meta = response.get("ResponseMetadata", {}) or {}
    return {
        "s3_error_code": error.get("Code", ""),
        "http_status": meta.get("HTTPStatusCode"),
        "message": (error.get("Message") or "")[:400],
    }


def _fmt(obs):
    return (f"HTTP {obs.get('http_status')} {obs.get('s3_error_code')}"
            f"{': ' + obs['message'] if obs.get('message') else ''}")


def _status(resp):
    """The HTTP status a successful call actually came back with.

    Recorded rather than assumed: "the PUT returned 200" is evidence only if it
    was read off the response.
    """
    return (resp or {}).get("ResponseMetadata", {}).get("HTTPStatusCode")


def _try(fn, **kwargs):
    """Run the call under test. Returns (response, error); exactly one is None."""
    try:
        return fn(**kwargs), None
    except ClientError as err:
        return None, err


def _finding(record, status, detail, observed=None, reason=None):
    """Record a non-supported verdict and fail the test.

    Failing keeps the finding visible in pytest's own output -- a conformance
    run is expected to exit non-zero against any system that is missing a
    behaviour -- while the recorded status is what the matrix publishes.
    """
    record(status, detail, observed, reason)
    pytest.fail(f"{status}: {detail}")


def _require_header(sent, name, expected=None):
    """Assert botocore actually put the header on the wire.

    An SDK that drops a parameter it does not know produces a request with no
    precondition on it at all, which then succeeds -- indistinguishable, from
    the test's point of view, from a system that ignored the precondition. That
    is a false verdict waiting to be published, so it is checked directly.
    """
    headers = sent.get("headers")
    assert headers is not None, f"harness bug: no request was captured for {name}"
    got = headers.get(name)
    if isinstance(got, bytes):
        got = got.decode()
    assert got is not None, (
        f"harness bug: botocore did not send the {name} header, so this request "
        f"carried no precondition and any verdict from it would describe the SDK, "
        f"not the system (headers sent: {sorted(headers)})")
    if expected is not None:
        assert got == expected, f"harness bug: {name} was sent as {got!r}, expected {expected!r}"
    return got


def _put_many(s3, bucket, keys, body=b"x"):
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda k: s3.put_object(Bucket=bucket, Key=k, Body=body), keys))


def _http(url, data=None, method="GET"):
    """Raw HTTP against a presigned URL. Returns (status, body)."""
    request = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


# --------------------------------------------------------------------------
# Conditional writes - Iceberg and Delta Lake commit protocols depend on these
# --------------------------------------------------------------------------

def test_put_if_none_match_allows_first_write(s3, bucket, record):
    """If-None-Match: * must succeed when the key does not exist yet.

    Acceptance alone proves nothing -- a system that ignores the header accepts
    this write too, for the wrong reason. So the same conditional PUT is then
    repeated against the now-existing key: if that is accepted as well, the
    first acceptance carried no information and the cell says so rather than
    reading as conformance next to a failing overwrite row.
    """
    key = "cond/first-write"
    with capture_request(s3, "PutObject") as sent:
        _, err = _try(s3.put_object, Bucket=bucket, Key=key, Body=BODY, IfNoneMatch="*")
    _require_header(sent, "if-none-match", "*")
    if err is not None:
        obs = _observed(err)
        _finding(record, "not_implemented",
                 f"If-None-Match: * was rejected on a key that did not exist, so a first commit "
                 f"cannot be made conditionally: {_fmt(obs)}", obs)
    stored = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    if stored != BODY:
        _finding(record, "diverges",
                 f"the conditional first write reported success but the stored body is {stored!r}")

    # Discriminator: was the precondition evaluated, or merely ignored?
    again, again_err = _try(s3.put_object, Bucket=bucket, Key=key, Body=SECOND, IfNoneMatch="*")
    after = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    if again_err is None:
        _finding(record, "accepted_not_enforced",
                 f"the conditional write was accepted on a new key, but repeating the identical "
                 f"If-None-Match: * PUT against the now-existing key is accepted too "
                 f"(HTTP {_status(again)}, object is now {after!r}). The acceptance does not show "
                 f"the precondition was evaluated -- the header is being ignored, so this cell "
                 f"must not be read as conditional-write support.",
                 {"http_status": _status(again), "stored_after": after.decode(errors="replace")})
    record("supported",
           f"If-None-Match: * accepted on a new key and the body stored intact; the same PUT "
           f"against the existing key is then refused ({_fmt(_observed(again_err))}), so the "
           f"precondition was genuinely evaluated")


def test_put_if_none_match_rejects_overwrite(s3, bucket, record):
    """PutObject with If-None-Match: * must fail once the key exists.

    This is the single cell that decides whether an Iceberg or Delta Lake table
    can live on a store. The failure that matters is not an error, it is
    silence: a store that accepts the precondition and overwrites anyway lets
    two writers both believe they committed. Both silent shapes are separated
    here -- overwritten, and accepted-but-dropped.
    """
    key = "cond/create-once"
    s3.put_object(Bucket=bucket, Key=key, Body=BODY)  # unconditional first write
    with capture_request(s3, "PutObject") as sent:
        resp, err = _try(s3.put_object, Bucket=bucket, Key=key, Body=SECOND, IfNoneMatch="*")
    _require_header(sent, "if-none-match", "*")
    stored = s3.get_object(Bucket=bucket, Key=key)["Body"].read()

    if err is None:
        if stored == SECOND:
            _finding(record, "accepted_not_enforced",
                     f"precondition ignored: the second PUT returned HTTP {_status(resp)} and the "
                     f"object was overwritten (now {stored!r}). A lakehouse commit is lost here, "
                     f"silently -- the writer is told it succeeded.",
                     {"http_status": _status(resp),
                      "stored_after": stored.decode(errors="replace")})
        _finding(record, "accepted_not_enforced",
                 f"the second PUT returned HTTP {_status(resp)} but the object still holds "
                 f"{stored!r}. The write was dropped without an error, so both writers read "
                 f"their commit as won.",
                 {"http_status": _status(resp),
                  "stored_after": stored.decode(errors="replace")})

    obs = _observed(err)
    if stored != BODY:
        _finding(record, "diverges",
                 f"rejected with {_fmt(obs)} but the object was modified anyway "
                 f"(now {stored!r})", obs)
    if obs["http_status"] == 412:
        record("supported",
               f"412 {obs['s3_error_code'] or 'PreconditionFailed'} on an existing key; "
               f"object left unchanged", obs)
        return
    _finding(record, "diverges",
             f"the overwrite was rejected and the object left unchanged, but not with the "
             f"documented 412 PreconditionFailed: {_fmt(obs)}", obs)


def test_put_if_match_on_current_etag_succeeds(s3, bucket, record):
    """If-Match with the object's current ETag must be allowed through.

    Same discriminator as the If-None-Match first-write test: a system that
    ignores If-Match also accepts this, so a stale ETag is tried afterwards to
    tell an evaluated precondition from an ignored one.
    """
    key = "cond/if-match-current"
    stale = s3.put_object(Bucket=bucket, Key=key, Body=b"original")["ETag"]
    etag = s3.put_object(Bucket=bucket, Key=key, Body=BODY)["ETag"]
    with capture_request(s3, "PutObject") as sent:
        _, err = _try(s3.put_object, Bucket=bucket, Key=key, Body=SECOND, IfMatch=etag)
    _require_header(sent, "if-match", etag)
    if err is not None:
        obs = _observed(err)
        _finding(record, "not_implemented",
                 f"If-Match with the object's own current ETag ({etag}) was rejected, so "
                 f"compare-and-swap updates are not possible: {_fmt(obs)}", obs)
    stored = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    if stored != SECOND:
        _finding(record, "diverges",
                 f"If-Match on the current ETag reported success but the object still holds "
                 f"{stored!r}")

    # Discriminator: an ETag the object has not had for two writes must not pass.
    again, again_err = _try(s3.put_object, Bucket=bucket, Key=key, Body=b"third", IfMatch=stale)
    if again_err is None:
        after = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        _finding(record, "accepted_not_enforced",
                 f"the update was accepted with the current ETag, but a two-generations-stale "
                 f"ETag ({stale}) is accepted just as readily (HTTP {_status(again)}, object now "
                 f"{after!r}). The header is being ignored, so this cell is not evidence of "
                 f"compare-and-swap support.",
                 {"http_status": _status(again), "stored_after": after.decode(errors="replace")})
    record("supported",
           f"If-Match on the current ETag accepted and applied, while a stale ETag on the same "
           f"key is refused ({_fmt(_observed(again_err))})")


def test_put_if_match_on_stale_etag_is_rejected(s3, bucket, record):
    """If-Match must fail once the object has moved on from that ETag.

    The ETag used is the object's real previous ETag rather than a fabricated
    one, so a system cannot be credited for rejecting a malformed value.
    """
    key = "cond/if-match-stale"
    stale = s3.put_object(Bucket=bucket, Key=key, Body=BODY)["ETag"]
    s3.put_object(Bucket=bucket, Key=key, Body=b"updated")
    with capture_request(s3, "PutObject") as sent:
        resp, err = _try(s3.put_object, Bucket=bucket, Key=key, Body=SECOND, IfMatch=stale)
    _require_header(sent, "if-match", stale)
    stored = s3.get_object(Bucket=bucket, Key=key)["Body"].read()

    if err is None:
        if stored == SECOND:
            _finding(record, "accepted_not_enforced",
                     f"stale If-Match ({stale}) accepted: the PUT returned HTTP {_status(resp)} "
                     f"and the write landed on top of a version the client had never seen "
                     f"(object now {stored!r}). A read-modify-write loop loses updates here "
                     f"without ever seeing an error.",
                     {"http_status": _status(resp),
                      "stored_after": stored.decode(errors="replace")})
        _finding(record, "accepted_not_enforced",
                 f"stale If-Match returned HTTP {_status(resp)} but the write was dropped; "
                 f"object still {stored!r}",
                 {"http_status": _status(resp),
                  "stored_after": stored.decode(errors="replace")})

    obs = _observed(err)
    if stored != b"updated":
        _finding(record, "diverges",
                 f"rejected with {_fmt(obs)} but the object was modified anyway "
                 f"(now {stored!r})", obs)
    if obs["http_status"] == 412:
        record("supported",
               f"412 {obs['s3_error_code'] or 'PreconditionFailed'} on a stale ETag", obs)
        return
    _finding(record, "diverges",
             f"stale If-Match rejected, but not with the documented 412 PreconditionFailed: "
             f"{_fmt(obs)}", obs)


def test_get_if_none_match_returns_304(s3, bucket, record):
    """A conditional GET on an unmodified object must return 304, not the body."""
    key = "cond/get-304"
    etag = s3.put_object(Bucket=bucket, Key=key, Body=BODY)["ETag"]
    with capture_request(s3, "GetObject") as sent:
        resp, err = _try(s3.get_object, Bucket=bucket, Key=key, IfNoneMatch=etag)
    _require_header(sent, "if-none-match", etag)
    if err is None:
        body = resp["Body"].read()
        _finding(record, "accepted_not_enforced",
                 f"If-None-Match on GET ignored: HTTP {_status(resp)} with a {len(body)}-byte "
                 f"body for an object whose ETag matched",
                 {"http_status": _status(resp), "body_bytes": len(body)})
    obs = _observed(err)
    if obs["http_status"] == 304:
        record("supported", "304 Not Modified for a matching ETag", obs)
        return
    _finding(record, "diverges",
             f"the conditional GET did not return the body, but answered {_fmt(obs)} rather than "
             f"304 Not Modified", obs)


def test_get_if_match_on_stale_etag_is_rejected(s3, bucket, record):
    """GET with If-Match on an ETag the object no longer has must fail with 412."""
    key = "cond/get-if-match"
    stale = s3.put_object(Bucket=bucket, Key=key, Body=BODY)["ETag"]
    s3.put_object(Bucket=bucket, Key=key, Body=b"updated")
    with capture_request(s3, "GetObject") as sent:
        resp, err = _try(s3.get_object, Bucket=bucket, Key=key, IfMatch=stale)
    _require_header(sent, "if-match", stale)
    if err is None:
        body = resp["Body"].read()
        _finding(record, "accepted_not_enforced",
                 f"If-Match on GET ignored: a stale ETag still returned HTTP {_status(resp)} and "
                 f"{len(body)} bytes",
                 {"http_status": _status(resp), "body_bytes": len(body)})
    obs = _observed(err)
    if obs["http_status"] == 412:
        record("supported",
               f"412 {obs['s3_error_code'] or 'PreconditionFailed'} on a stale ETag", obs)
        return
    _finding(record, "diverges",
             f"the conditional GET was rejected, but not with the documented 412: {_fmt(obs)}",
             obs)


# --------------------------------------------------------------------------
# Versioning
# --------------------------------------------------------------------------

def _versioning_unavailable(s3, bucket, record, err):
    """Record why versioning could not be enabled, having probed the cause.

    The bare error can be actively misleading. A gateway that does not route the
    ?versioning subresource at all falls through to whatever handler owns
    `PUT /<bucket>`, and answers BucketAlreadyExists -- which reads like a
    harness bug rather than a missing feature. So the cause is established here
    rather than inferred: if the identical call against an unused bucket name
    leaves a bucket behind, the subresource is not routed and the request became
    a CreateBucket.
    """
    obs = _observed(err)
    probe, probe_err = _try(s3.get_bucket_versioning, Bucket=bucket)
    if probe_err is None:
        read_side = (f"GET ?versioning on the same bucket answers HTTP {_status(probe)} with "
                     f"status {probe.get('Status')!r}")
    else:
        read_side = f"GET ?versioning also fails: {_fmt(_observed(probe_err))}"
    obs["get_bucket_versioning"] = read_side

    spare = f"probe-{uuid.uuid4().hex[:12]}"
    spare_resp, spare_err = _try(s3.put_bucket_versioning, Bucket=spare,
                                 VersioningConfiguration={"Status": "Enabled"})
    _, absent = _try(s3.head_bucket, Bucket=spare)
    if absent is None:
        outcome = ("success" if spare_err is None
                   else _fmt(_observed(spare_err)))
        cause = (f" The ?versioning subresource is not routed at all: the identical call against "
                 f"the unused name {spare!r} answered {outcome} and left a bucket of that name "
                 f"behind, so the request falls through to the CreateBucket handler. That is what "
                 f"the error above actually is -- a bucket-creation error, not a statement about "
                 f"versioning.")
        obs["subresource_routed"] = False
        _try(s3.delete_bucket, Bucket=spare)
    else:
        cause = (" The identical call against an unused bucket name created no bucket, so the "
                 "subresource is routed and the refusal is about versioning itself.")
        obs["subresource_routed"] = True

    _finding(record, "not_implemented",
             f"bucket versioning, which this behaviour requires, could not be enabled: "
             f"{_fmt(obs)}. {read_side}.{cause}", obs)


def _enable_versioning(s3, bucket, record):
    """Turn versioning on, or record why this behaviour cannot be reached."""
    _, err = _try(s3.put_bucket_versioning, Bucket=bucket,
                  VersioningConfiguration={"Status": "Enabled"})
    if err is not None:
        _versioning_unavailable(s3, bucket, record, err)
    resp, err = _try(s3.get_bucket_versioning, Bucket=bucket)
    if err is not None:
        obs = _observed(err)
        _finding(record, "diverges",
                 f"PutBucketVersioning was accepted but GetBucketVersioning failed, so versioning "
                 f"cannot be confirmed on: {_fmt(obs)}", obs)
    status = resp.get("Status")
    if status != "Enabled":
        _finding(record, "accepted_not_enforced",
                 f"PutBucketVersioning returned success but the bucket reports versioning status "
                 f"{status!r}; the setting was accepted and ignored",
                 {"versioning_status": status})


def test_versioning_can_be_enabled(s3, bucket, record):
    """PutBucketVersioning must take effect, not merely be accepted."""
    _enable_versioning(s3, bucket, record)
    record("supported", "versioning enabled and read back as Enabled")


def test_versioning_keeps_prior_versions(s3, bucket, record):
    """Two writes to one key must leave two independently addressable versions."""
    _enable_versioning(s3, bucket, record)
    key = "ver/object"
    s3.put_object(Bucket=bucket, Key=key, Body=b"v1")
    s3.put_object(Bucket=bucket, Key=key, Body=b"v2")
    resp, err = _try(s3.list_object_versions, Bucket=bucket, Prefix=key)
    if err is not None:
        obs = _observed(err)
        _finding(record, "not_implemented", f"ListObjectVersions failed: {_fmt(obs)}", obs)
    versions = resp.get("Versions", [])
    ids = {v.get("VersionId") for v in versions}
    if len(versions) < 2:
        _finding(record, "accepted_not_enforced",
                 f"versioning reports Enabled, but only {len(versions)} version(s) are retained "
                 f"after two writes to the same key; the earlier version is gone",
                 {"versions_listed": len(versions)})
    if len(ids) < 2:
        _finding(record, "diverges",
                 f"{len(versions)} versions were listed but they share {len(ids)} version id(s) "
                 f"({sorted(str(i) for i in ids)}), so they are not separately addressable",
                 {"versions_listed": len(versions), "distinct_version_ids": len(ids)})
    record("supported", f"{len(versions)} versions retained with distinct version ids")


def test_versioned_get_returns_the_older_version(s3, bucket, record):
    """An overwritten version must still be readable by its version id.

    Listing version ids proves nothing on its own; this is what shows the older
    bytes actually survived the overwrite.
    """
    _enable_versioning(s3, bucket, record)
    key = "ver/readback"
    first = s3.put_object(Bucket=bucket, Key=key, Body=b"v1").get("VersionId")
    s3.put_object(Bucket=bucket, Key=key, Body=b"v2")
    if not first:
        resp, err = _try(s3.list_object_versions, Bucket=bucket, Prefix=key)
        if err is not None:
            obs = _observed(err)
            _finding(record, "not_implemented",
                     f"no VersionId on the PUT response and ListObjectVersions failed: "
                     f"{_fmt(obs)}", obs)
        older = [v for v in resp.get("Versions", []) if not v.get("IsLatest")]
        if not older:
            _finding(record, "accepted_not_enforced",
                     "versioning reports Enabled, but the PUT returned no VersionId and no "
                     "non-current version is listed, so the overwritten version cannot be "
                     "addressed at all")
        first = older[0]["VersionId"]
    resp, err = _try(s3.get_object, Bucket=bucket, Key=key, VersionId=first)
    if err is not None:
        obs = _observed(err)
        _finding(record, "accepted_not_enforced",
                 f"the overwritten version {first} is listed but could not be read back: "
                 f"{_fmt(obs)}", obs)
    body = resp["Body"].read()
    if body != b"v1":
        _finding(record, "diverges",
                 f"reading version {first} returned {body!r}, not the bytes that version held")
    record("supported", "an overwritten version reads back byte-identical by version id")


def test_delete_creates_a_delete_marker(s3, bucket, record):
    """A delete in a versioned bucket must be a marker, not a destruction."""
    _enable_versioning(s3, bucket, record)
    key = "ver/marker"
    s3.put_object(Bucket=bucket, Key=key, Body=b"v1")
    s3.delete_object(Bucket=bucket, Key=key)
    resp, err = _try(s3.list_object_versions, Bucket=bucket, Prefix=key)
    if err is not None:
        obs = _observed(err)
        _finding(record, "not_implemented", f"ListObjectVersions failed: {_fmt(obs)}", obs)
    markers = resp.get("DeleteMarkers", [])
    versions = resp.get("Versions", [])
    if not markers:
        _finding(record, "accepted_not_enforced",
                 f"versioning reports Enabled, but no delete marker was created; "
                 f"{len(versions)} version(s) remain listed, so the delete was applied "
                 f"destructively rather than as a marker",
                 {"delete_markers": 0, "versions_listed": len(versions)})
    if not versions:
        _finding(record, "diverges",
                 "a delete marker was created but the underlying version is no longer listed, "
                 "so the delete is not reversible")
    record("supported",
           f"delete marker created; {len(versions)} underlying version(s) still listed")


# --------------------------------------------------------------------------
# Object Lock / WORM
# --------------------------------------------------------------------------

def test_object_lock_bucket_can_be_created(s3, new_bucket, record):
    """Object Lock has to be enabled at bucket creation; it cannot be added later."""
    try:
        name = new_bucket("lock", ObjectLockEnabledForBucket=True)
    except ClientError as err:
        obs = _observed(err)
        _finding(record, "not_implemented",
                 f"CreateBucket with ObjectLockEnabledForBucket was rejected: {_fmt(obs)}", obs)
    resp, err = _try(s3.get_object_lock_configuration, Bucket=name)
    if err is not None:
        obs = _observed(err)
        _finding(record, "accepted_not_enforced",
                 f"CreateBucket accepted ObjectLockEnabledForBucket but "
                 f"GetObjectLockConfiguration then fails, so object lock is not in effect on the "
                 f"bucket it claimed to create: {_fmt(obs)}", obs)
    configuration = resp.get("ObjectLockConfiguration", {})
    if configuration.get("ObjectLockEnabled") != "Enabled":
        _finding(record, "accepted_not_enforced",
                 f"CreateBucket accepted ObjectLockEnabledForBucket and "
                 f"GetObjectLockConfiguration answers HTTP {_status(resp)}, but the configuration "
                 f"it returns is {configuration!r}. The header was accepted and ignored: the "
                 f"bucket has no object lock on it.",
                 {"http_status": _status(resp),
                  "object_lock_configuration": str(configuration)})
    record("supported", "object lock enabled at bucket creation and reported back as Enabled")


def test_object_lock_retention_blocks_deletion(s3, new_bucket, record):
    """A COMPLIANCE-retained object must not be deletable, by anyone, until it expires.

    When a system returns no version id the delete is attempted against the key
    anyway. "Retention could not be checked" is not a finding; "the object was
    accepted with COMPLIANCE retention and then deleted" is.

    The retained object outlives the test on a system that does enforce it:
    COMPLIANCE retention cannot be shortened or bypassed, which is the point of
    it, so the bucket is left for the stack teardown (which removes the volumes).
    """
    try:
        name = new_bucket("lock", ObjectLockEnabledForBucket=True)
    except ClientError as err:
        obs = _observed(err)
        _finding(record, "not_implemented",
                 f"object lock buckets cannot be created, so retention cannot be enforced: "
                 f"{_fmt(obs)}", obs)
    key = "worm/retained"
    until = datetime.now(timezone.utc) + timedelta(minutes=2)
    resp, err = _try(s3.put_object, Bucket=name, Key=key, Body=BODY,
                     ObjectLockMode="COMPLIANCE", ObjectLockRetainUntilDate=until)
    if err is not None:
        obs = _observed(err)
        _finding(record, "not_implemented",
                 f"PutObject with COMPLIANCE retention was rejected: {_fmt(obs)}", obs)

    target = {"Bucket": name, "Key": key}
    version = resp.get("VersionId")
    if not version:
        listed, list_err = _try(s3.list_object_versions, Bucket=name, Prefix=key)
        candidates = [] if list_err else listed.get("Versions", [])
        version = candidates[0]["VersionId"] if candidates else None
    if version:
        target["VersionId"] = version
    unversioned = "" if version else (
        " The object also has no version id -- PutObject returned none and none is listed -- "
        "so there is no immutable version behind the key at all.")

    _, err = _try(s3.delete_object, **target)
    _, head_err = _try(s3.head_object, **target)
    if err is None:
        _finding(record, "accepted_not_enforced",
                 f"the retention headers were accepted, then ignored: DeleteObject returned "
                 f"success while the retention still had "
                 f"~{int((until - datetime.now(timezone.utc)).total_seconds())}s to run, and the "
                 f"object is {'still readable' if head_err is None else 'gone'} afterwards. A "
                 f"caller who set COMPLIANCE retention believes the object is immutable and it "
                 f"is not.{unversioned}",
                 {"deleted": True, "still_readable": head_err is None, "version_id": version})
    obs = _observed(err)
    if head_err is not None:
        _finding(record, "diverges",
                 f"DeleteObject was rejected ({_fmt(obs)}) but the object is not readable "
                 f"either: {_fmt(_observed(head_err))}", obs)
    if not version:
        _finding(record, "diverges",
                 f"the delete was refused ({_fmt(obs)}) and the object is still readable, but the "
                 f"bucket keeps no versions, so retention guards the current key rather than an "
                 f"immutable version as AWS does.{unversioned}", obs)
    record("supported",
           f"the retained version could not be deleted ({_fmt(obs)}) and is still readable", obs)


def test_object_lock_legal_hold_blocks_deletion(s3, new_bucket, record):
    """A legal hold must block deletion independently of any retention period.

    As with retention, a system that returns no version id still gets the delete
    attempted against the key: an inert hold is a finding, an unfinished test is
    not.
    """
    try:
        name = new_bucket("lock", ObjectLockEnabledForBucket=True)
    except ClientError as err:
        obs = _observed(err)
        _finding(record, "not_implemented",
                 f"object lock buckets cannot be created, so legal hold cannot be applied: "
                 f"{_fmt(obs)}", obs)
    key = "worm/legal-hold"
    resp, err = _try(s3.put_object, Bucket=name, Key=key, Body=BODY,
                     ObjectLockLegalHoldStatus="ON")
    if err is not None:
        obs = _observed(err)
        _finding(record, "not_implemented",
                 f"PutObject with a legal hold was rejected: {_fmt(obs)}", obs)
    target = {"Bucket": name, "Key": key}
    version = resp.get("VersionId")
    if version:
        target["VersionId"] = version
    unversioned = "" if version else (
        " The object also carries no version id, so there is no version behind the hold.")
    try:
        status, status_err = _try(s3.get_object_legal_hold, **target)
        held = None if status_err is not None else status.get("LegalHold", {}).get("Status")
        _, err = _try(s3.delete_object, **target)
        _, head_err = _try(s3.head_object, **target)
        if err is None:
            _finding(record, "accepted_not_enforced",
                     f"the legal hold was accepted, then ignored: an object put with "
                     f"ObjectLockLegalHoldStatus=ON was deleted without error, it is "
                     f"{'still readable' if head_err is None else 'gone'} afterwards, and "
                     f"GetObjectLegalHold reports {held!r}.{unversioned}",
                     {"deleted": True, "legal_hold_read_back": held,
                      "still_readable": head_err is None, "version_id": version})
        obs = _observed(err)
        if status_err is not None:
            _finding(record, "diverges",
                     f"the delete was refused ({_fmt(obs)}) but GetObjectLegalHold fails, so the "
                     f"hold cannot be read back: {_fmt(_observed(status_err))}", obs)
        if held != "ON":
            _finding(record, "diverges",
                     f"the delete was refused ({_fmt(obs)}) but the legal hold reads back as "
                     f"{held!r} rather than ON", obs)
        record("supported",
               f"an object under legal hold could not be deleted ({_fmt(obs)}) and the hold "
               f"reads back as ON", obs)
    finally:
        # Release the hold so the bucket can be cleaned up. Unlike COMPLIANCE
        # retention a legal hold is removable, so there is no reason to leak it.
        _try(s3.put_object_legal_hold, LegalHold={"Status": "OFF"}, **target)


# --------------------------------------------------------------------------
# Multipart
# --------------------------------------------------------------------------

PART = b"x" * (5 * 1024 * 1024)  # 5 MiB, the S3 minimum for non-final parts
UNDERSIZED = b"y" * (1024 * 1024)  # 1 MiB, below that minimum


def test_multipart_upload_completes(s3, bucket, record):
    key = "mpu/basic"
    upload = s3.create_multipart_upload(Bucket=bucket, Key=key)["UploadId"]
    parts = []
    for number in (1, 2):
        etag = s3.upload_part(Bucket=bucket, Key=key, UploadId=upload,
                              PartNumber=number, Body=PART)["ETag"]
        parts.append({"ETag": etag, "PartNumber": number})
    _, err = _try(s3.complete_multipart_upload, Bucket=bucket, Key=key, UploadId=upload,
                  MultipartUpload={"Parts": parts})
    if err is not None:
        obs = _observed(err)
        _finding(record, "not_implemented",
                 f"CompleteMultipartUpload failed for a two-part 10 MiB upload: {_fmt(obs)}", obs)
    head, err = _try(s3.head_object, Bucket=bucket, Key=key)
    if err is not None:
        obs = _observed(err)
        _finding(record, "diverges",
                 f"the upload completed but the object cannot be headed: {_fmt(obs)}", obs)
    if head["ContentLength"] != 2 * len(PART):
        _finding(record, "diverges",
                 f"completed object is {head['ContentLength']} bytes, expected {2 * len(PART)}",
                 {"content_length": head["ContentLength"]})
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    if body != PART + PART:
        _finding(record, "diverges",
                 f"completed object has the right length but the wrong content "
                 f"({len(body)} bytes read back)")
    record("supported", "two-part 10 MiB upload completed and reassembled byte-identical")


def test_multipart_accepts_out_of_order_parts(s3, bucket, record):
    """Parts may be uploaded in any order; only the completion manifest orders them."""
    key = "mpu/out-of-order"
    upload = s3.create_multipart_upload(Bucket=bucket, Key=key)["UploadId"]
    tail = s3.upload_part(Bucket=bucket, Key=key, UploadId=upload, PartNumber=2,
                          Body=PART + b"tail")["ETag"]
    head_etag = s3.upload_part(Bucket=bucket, Key=key, UploadId=upload, PartNumber=1,
                               Body=PART)["ETag"]
    _, err = _try(s3.complete_multipart_upload, Bucket=bucket, Key=key, UploadId=upload,
                  MultipartUpload={"Parts": [{"ETag": head_etag, "PartNumber": 1},
                                             {"ETag": tail, "PartNumber": 2}]})
    if err is not None:
        obs = _observed(err)
        _finding(record, "not_implemented",
                 f"completing an upload whose parts arrived out of order failed: {_fmt(obs)}", obs)
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    if not body.endswith(b"tail") or len(body) != 2 * len(PART) + 4:
        _finding(record, "diverges",
                 f"out-of-order parts were accepted but reassembled wrongly: {len(body)} bytes, "
                 f"ends with {body[-8:]!r}", {"content_length": len(body)})
    record("supported", "parts uploaded 2-then-1 reassembled in manifest order")


def test_multipart_abort_releases_the_upload(s3, bucket, record):
    key = "mpu/abort"
    upload = s3.create_multipart_upload(Bucket=bucket, Key=key)["UploadId"]
    s3.upload_part(Bucket=bucket, Key=key, UploadId=upload, PartNumber=1, Body=PART)
    _, err = _try(s3.abort_multipart_upload, Bucket=bucket, Key=key, UploadId=upload)
    if err is not None:
        obs = _observed(err)
        _finding(record, "not_implemented", f"AbortMultipartUpload failed: {_fmt(obs)}", obs)
    listed, err = _try(s3.list_multipart_uploads, Bucket=bucket)
    if err is not None:
        obs = _observed(err)
        _finding(record, "diverges",
                 f"the abort was accepted but ListMultipartUploads failed, so it cannot be "
                 f"confirmed: {_fmt(obs)}", obs)
    active = [u["UploadId"] for u in listed.get("Uploads", [])]
    if upload in active:
        _finding(record, "accepted_not_enforced",
                 f"AbortMultipartUpload returned success but the upload is still listed as "
                 f"active, so its parts are still occupying space",
                 {"uploads_listed": len(active)})
    # The abort must also have stopped the object from existing.
    _, err = _try(s3.head_object, Bucket=bucket, Key=key)
    if err is None:
        _finding(record, "diverges",
                 "the upload was aborted but an object exists at the key anyway")
    record("supported", "aborted upload is no longer listed and left no object behind")


def test_multipart_rejects_undersized_part(s3, bucket, record):
    """A non-final part below 5 MiB must be refused at completion.

    AWS answers EntityTooSmall. A store that accepts it instead writes an object
    AWS would have refused, so anything produced that way is not portable back
    to S3 -- and the client is never told.
    """
    key = "mpu/undersized"
    upload = s3.create_multipart_upload(Bucket=bucket, Key=key)["UploadId"]
    parts = []
    for number in (1, 2):
        etag = s3.upload_part(Bucket=bucket, Key=key, UploadId=upload,
                              PartNumber=number, Body=UNDERSIZED)["ETag"]
        parts.append({"ETag": etag, "PartNumber": number})
    resp, err = _try(s3.complete_multipart_upload, Bucket=bucket, Key=key, UploadId=upload,
                     MultipartUpload={"Parts": parts})
    if err is None:
        head, _ = _try(s3.head_object, Bucket=bucket, Key=key)
        _finding(record, "diverges",
                 f"an upload whose first part was 1 MiB completed without error "
                 f"({head['ContentLength'] if head else '?'} bytes written, HTTP "
                 f"{_status(resp)}); AWS rejects this with EntityTooSmall, so the part-size "
                 f"minimum is not enforced and such an object is not reproducible on S3",
                 {"http_status": _status(resp),
                  "content_length": head["ContentLength"] if head else None})
    obs = _observed(err)
    if obs["s3_error_code"] == "EntityTooSmall":
        record("supported", f"EntityTooSmall on a 1 MiB non-final part ({_fmt(obs)})", obs)
        return
    _finding(record, "diverges",
             f"the undersized part was rejected, but not with the documented EntityTooSmall: "
             f"{_fmt(obs)}", obs)


# --------------------------------------------------------------------------
# Listing - the lakehouse pain point
# --------------------------------------------------------------------------

def test_list_objects_v2_paginates_with_continuation_token(s3, bucket, record):
    _put_many(s3, bucket, [f"page/{i:04d}" for i in range(120)])
    first, err = _try(s3.list_objects_v2, Bucket=bucket, Prefix="page/", MaxKeys=50)
    if err is not None:
        obs = _observed(err)
        _finding(record, "not_implemented",
                 f"ListObjectsV2 with MaxKeys failed: {_fmt(obs)}", obs)
    got = first.get("Contents", [])
    if len(got) != 50 or not first.get("IsTruncated"):
        _finding(record, "diverges",
                 f"MaxKeys=50 over 120 keys returned {len(got)} key(s) with "
                 f"IsTruncated={first.get('IsTruncated')!r}; the page limit is not honoured",
                 {"keys_returned": len(got), "is_truncated": first.get("IsTruncated")})
    token = first.get("NextContinuationToken")
    if not token:
        _finding(record, "not_implemented",
                 "the listing was truncated but carried no NextContinuationToken, so the rest "
                 "of the keys are unreachable")
    second, err = _try(s3.list_objects_v2, Bucket=bucket, Prefix="page/", MaxKeys=50,
                       ContinuationToken=token)
    if err is not None:
        obs = _observed(err)
        _finding(record, "not_implemented",
                 f"the continuation token was refused: {_fmt(obs)}", obs)
    page2 = second.get("Contents", [])
    if len(page2) != 50:
        _finding(record, "diverges",
                 f"the second page returned {len(page2)} key(s), expected 50",
                 {"keys_returned": len(page2)})
    if page2[0]["Key"] <= got[-1]["Key"]:
        _finding(record, "diverges",
                 f"the second page restarts at {page2[0]['Key']!r}, which is not after the first "
                 f"page's last key {got[-1]['Key']!r}; keys are not in lexicographic order "
                 f"across pages")
    record("supported",
           "continuation token honoured; pages are 50 keys and lexicographically ordered")


def test_list_objects_v2_caps_at_1000_keys(s3, bucket, record):
    """An unbounded listing must stop at 1000 keys and say it is truncated.

    Table formats page through prefixes with tens of thousands of files. A
    system that returns a different number without IsTruncated is not
    necessarily losing data, but it is not the contract clients are written
    against.
    """
    keys = [f"many/{i:04d}" for i in range(1001)]
    _put_many(s3, bucket, keys)
    page, err = _try(s3.list_objects_v2, Bucket=bucket, Prefix="many/")
    if err is not None:
        obs = _observed(err)
        _finding(record, "not_implemented",
                 f"ListObjectsV2 failed over 1001 keys: {_fmt(obs)}", obs)
    got = page.get("Contents", [])
    truncated = page.get("IsTruncated")
    if len(got) != 1000 or not truncated:
        _finding(record, "diverges",
                 f"an unbounded listing over 1001 keys returned {len(got)} key(s) with "
                 f"IsTruncated={truncated!r}; AWS returns exactly 1000 and marks it truncated",
                 {"keys_returned": len(got), "is_truncated": truncated})
    seen = set()
    for chunk in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="many/"):
        seen.update(o["Key"] for o in chunk.get("Contents", []))
    if seen != set(keys):
        _finding(record, "diverges",
                 f"paginating to the end yielded {len(seen)} distinct keys out of {len(keys)} "
                 f"written; {len(set(keys) - seen)} are unreachable through pagination",
                 {"keys_written": len(keys), "keys_reachable": len(seen)})
    record("supported",
           "1000-key page cap with IsTruncated, and all 1001 keys reachable by paging")


def test_list_objects_v2_delimiter_yields_common_prefixes(s3, bucket, record):
    for key in ("a/1", "a/2", "b/1", "top"):
        s3.put_object(Bucket=bucket, Key=key, Body=b"x")
    listing, err = _try(s3.list_objects_v2, Bucket=bucket, Delimiter="/")
    if err is not None:
        obs = _observed(err)
        _finding(record, "not_implemented",
                 f"ListObjectsV2 with a delimiter failed: {_fmt(obs)}", obs)
    prefixes = sorted(p["Prefix"] for p in listing.get("CommonPrefixes", []))
    contents = sorted(o["Key"] for o in listing.get("Contents", []))
    if prefixes != ["a/", "b/"]:
        _finding(record, "diverges",
                 f"delimiter rollup returned CommonPrefixes {prefixes}, expected ['a/', 'b/']",
                 {"common_prefixes": prefixes, "contents": contents})
    if contents != ["top"]:
        _finding(record, "diverges",
                 f"CommonPrefixes were correct but the same listing also returned {contents} as "
                 f"keys, expected only ['top']",
                 {"common_prefixes": prefixes, "contents": contents})
    record("supported", "delimiter rollup returns a/ and b/ with only the top-level key inline")


# --------------------------------------------------------------------------
# Presigned URLs, copy, range, tagging, encryption, policy
# --------------------------------------------------------------------------

def test_presigned_get_url_works(s3, bucket, record):
    key = "presign/object"
    s3.put_object(Bucket=bucket, Key=key, Body=BODY)
    url = s3.generate_presigned_url("get_object",
                                    Params={"Bucket": bucket, "Key": key}, ExpiresIn=300)
    status, body = _http(url)
    if status != 200:
        _finding(record, "not_implemented",
                 f"a presigned GET was refused: HTTP {status}, body {body[:200]!r}",
                 {"http_status": status, "body": body[:200].decode(errors="replace")})
    if body != BODY:
        _finding(record, "diverges",
                 f"the presigned GET succeeded but returned {body[:80]!r}")
    record("supported", "presigned GET honoured and returned the object bytes")


def test_presigned_put_url_works(s3, bucket, record):
    key = "presign/upload"
    url = s3.generate_presigned_url("put_object",
                                    Params={"Bucket": bucket, "Key": key}, ExpiresIn=300)
    status, body = _http(url, data=BODY, method="PUT")
    if status not in (200, 204):
        _finding(record, "not_implemented",
                 f"a presigned PUT was refused: HTTP {status}, body {body[:200]!r}",
                 {"http_status": status, "body": body[:200].decode(errors="replace")})
    stored, err = _try(s3.get_object, Bucket=bucket, Key=key)
    if err is not None:
        obs = _observed(err)
        _finding(record, "diverges",
                 f"the presigned PUT returned HTTP {status} but the object is not readable "
                 f"afterwards: {_fmt(obs)}", obs)
    content = stored["Body"].read()
    if content != BODY:
        _finding(record, "diverges",
                 f"the presigned PUT stored {content[:80]!r} instead of the sent body")
    record("supported", "presigned PUT honoured and the body stored intact")


def test_presigned_url_expiry_is_enforced(s3, bucket, record):
    """A presigned URL must stop working when it expires.

    Fetched once while valid first: without that, a URL that never worked at all
    would be indistinguishable from an expiry that was enforced.
    """
    key = "presign/expiring"
    s3.put_object(Bucket=bucket, Key=key, Body=BODY)
    url = s3.generate_presigned_url("get_object",
                                    Params={"Bucket": bucket, "Key": key}, ExpiresIn=2)
    status, body = _http(url)
    if status != 200:
        # No verdict is reachable: this says nothing about expiry either way, and
        # presigned_get_url_works is the cell that carries that finding.
        _finding(record, "error",
                 f"the presigned URL did not work even while valid (HTTP {status}), so expiry "
                 f"could not be judged from it -- see test_presigned_get_url_works",
                 {"http_status": status})
    time.sleep(6)
    status, body = _http(url)
    if status == 200:
        _finding(record, "accepted_not_enforced",
                 f"a URL presigned for 2 seconds still returned HTTP 200 and {len(body)} bytes "
                 f"6 seconds later; the expiry in the signature is accepted and not enforced",
                 {"http_status": status, "body_bytes": len(body)})
    record("supported",
           f"the URL served while valid and answered HTTP {status} after expiry",
           {"http_status_after_expiry": status,
            "body_after_expiry": body[:200].decode(errors="replace")})


def test_copy_object_preserves_content(s3, bucket, record):
    s3.put_object(Bucket=bucket, Key="copy/src", Body=BODY)
    _, err = _try(s3.copy_object, Bucket=bucket, Key="copy/dst",
                  CopySource={"Bucket": bucket, "Key": "copy/src"})
    if err is not None:
        obs = _observed(err)
        _finding(record, "not_implemented", f"CopyObject failed: {_fmt(obs)}", obs)
    copied, err = _try(s3.get_object, Bucket=bucket, Key="copy/dst")
    if err is not None:
        obs = _observed(err)
        _finding(record, "accepted_not_enforced",
                 f"CopyObject returned success but the destination does not exist: {_fmt(obs)}",
                 obs)
    body = copied["Body"].read()
    if body != BODY:
        _finding(record, "diverges", f"the copy holds {body!r}, not the source bytes")
    record("supported", "server-side copy produced a byte-identical object")


def test_range_get_returns_the_requested_slice(s3, bucket, record):
    s3.put_object(Bucket=bucket, Key="range/object", Body=b"0123456789")
    resp, err = _try(s3.get_object, Bucket=bucket, Key="range/object", Range="bytes=2-5")
    if err is not None:
        obs = _observed(err)
        _finding(record, "not_implemented", f"a ranged GET was rejected: {_fmt(obs)}", obs)
    body = resp["Body"].read()
    status = _status(resp)
    if body != b"2345":
        _finding(record, "accepted_not_enforced",
                 f"bytes=2-5 returned {body!r} (HTTP {status}); the Range header was accepted "
                 f"and ignored, so a client reading a slice gets the whole object",
                 {"http_status": status, "body": body.decode(errors="replace")})
    if status != 206:
        _finding(record, "diverges",
                 f"the correct slice came back but with HTTP {status}, not 206 Partial Content",
                 {"http_status": status})
    record("supported", "bytes=2-5 returned the four requested bytes with HTTP 206")


def test_object_tagging_roundtrip(s3, bucket, record):
    key = "tag/object"
    s3.put_object(Bucket=bucket, Key=key, Body=BODY)
    _, err = _try(s3.put_object_tagging, Bucket=bucket, Key=key,
                  Tagging={"TagSet": [{"Key": "layer", "Value": "bronze"}]})
    if err is not None:
        obs = _observed(err)
        _finding(record, "not_implemented", f"PutObjectTagging failed: {_fmt(obs)}", obs)
    resp, err = _try(s3.get_object_tagging, Bucket=bucket, Key=key)
    if err is not None:
        obs = _observed(err)
        _finding(record, "diverges",
                 f"PutObjectTagging was accepted but GetObjectTagging failed: {_fmt(obs)}", obs)
    tags = resp.get("TagSet", [])
    if not tags:
        _finding(record, "accepted_not_enforced",
                 "PutObjectTagging returned success but the object carries no tags afterwards",
                 {"tag_set": tags})
    if tags != [{"Key": "layer", "Value": "bronze"}]:
        _finding(record, "diverges",
                 f"the tag set read back as {tags}, not the one written", {"tag_set": tags})
    record("supported", "object tag set written and read back unchanged")


# A refusal that names a deployment prerequisite is a different finding from one
# that says the feature does not exist. But the two kinds of prerequisite are
# also different claims about the product, so they are separated into a
# machine-readable reason rather than left for a reader to infer from prose --
# any rendering that drops the detail text would otherwise collapse them:
#
#   conformant_refusal    the server refused for a reason AWS refuses for too.
#                         MinIO declining SSE-C without TLS is correct behaviour,
#                         and this harness is plain HTTP by design.
#   missing_prerequisite  the feature needs something this deployment does not
#                         provide, such as a key manager. Says nothing either way
#                         about whether the feature works once it is provided.
#
# Matching is on whole documented phrases, never on bare scheme tokens, and any
# URL is stripped from the message first. Under this vocabulary
# `not_exercisable` is an affirmative claim that the system behaved correctly,
# so a server that merely cites a docs link (".. see https://../tls-setup")
# must not have a genuine gap silently promoted into one. Precision is chosen
# over recall deliberately: an unmatched prerequisite is recorded as
# `not_implemented`, which understates rather than overstates.
_URL_RE = re.compile(r"https?://\S+")

_CONFORMANT_REFUSAL_PHRASES = (
    "must be made over a secure connection",
    "over a secure connection",
    "requires a secure connection",
    "requires tls",
    "requires https",
    "must use https",
    "only over https",
)

_MISSING_PREREQUISITE_PHRASES = (
    "kms is not configured",
    "kms not configured",
    "kms is not enabled",
    "key management service is not configured",
    "master_key",
)


def _prerequisite_reason(code, message):
    """Classify a refusal.

    Returns "conformant_refusal", "missing_prerequisite", or None when the
    refusal is a real gap rather than a property of this deployment.
    """
    haystack = _URL_RE.sub(" ", f"{code} {message}").lower()
    if any(phrase in haystack for phrase in _CONFORMANT_REFUSAL_PHRASES):
        return "conformant_refusal"
    if any(phrase in haystack for phrase in _MISSING_PREREQUISITE_PHRASES):
        return "missing_prerequisite"
    return None


def _encryption_finding(record, err, feature, note):
    obs = _observed(err)
    reason = _prerequisite_reason(obs["s3_error_code"], obs["message"])
    if reason is not None:
        _finding(record, "not_exercisable",
                 f"{feature} was refused for a deployment prerequisite this harness does not "
                 f"provide to any system: {_fmt(obs)}. {note}", obs, reason=reason)
    _finding(record, "not_implemented", f"{feature} was rejected: {_fmt(obs)}", obs)


def test_sse_s3_encryption_is_accepted(s3, bucket, record):
    key = "sse/object"
    _, err = _try(s3.put_object, Bucket=bucket, Key=key, Body=BODY,
                  ServerSideEncryption="AES256")
    if err is not None:
        _encryption_finding(
            record, err, "SSE-S3 (ServerSideEncryption=AES256)",
            "The stacks run every system in its default single-node configuration with no key "
            "manager wired up, so this says nothing about whether the feature exists once one is.")
    head, err = _try(s3.head_object, Bucket=bucket, Key=key)
    if err is not None:
        obs = _observed(err)
        _finding(record, "diverges",
                 f"the encrypted PUT was accepted but the object cannot be headed: {_fmt(obs)}",
                 obs)
    reported = head.get("ServerSideEncryption")
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    if body != BODY:
        _finding(record, "diverges",
                 f"the object does not read back intact after an SSE-S3 PUT ({len(body)} bytes)")
    if reported != "AES256":
        _finding(record, "accepted_not_enforced",
                 f"the PUT was accepted and the body round-trips, but the object reports "
                 f"ServerSideEncryption={reported!r}. The encryption request was accepted and "
                 f"ignored, so a caller who asked for encryption at rest did not get it and was "
                 f"not told.",
                 {"server_side_encryption": reported})
    record("supported", "SSE-S3 applied, reported on the object, and the body round-trips")


def test_sse_c_encryption_roundtrip(s3, bucket, record):
    """SSE-C must encrypt under the caller's key, refuse reads that lack it, and
    be offered only over a secure transport.

    Four observations, not one. The object round-trips with the right key; a read
    carrying no key is refused; a read carrying a *different* valid 32-byte key is
    refused; and the right key presented with a mismatched key MD5 is refused,
    which is what shows the key material is actually bound to the object rather
    than the headers being waved through. Without them a system that ignores the
    SSE-C headers entirely and stores plaintext records as supported. The refusals
    must also be SSE refusals -- a 400 or 403 from the server -- rather than any
    failure at all, or a transport fault would read as enforcement.

    The transport is part of the contract here, not general policy. AWS refuses
    SSE-C over anything but HTTPS *specifically because the customer key travels
    in a request header*, so a system that accepts the key in cleartext is
    observably different from AWS on the SSE-C contract even when its crypto is
    correct. That is a `diverges`, and the detail keeps the data-path evidence
    attached so the demotion cannot be misread as an encryption defect.
    """
    key_bytes = _os.urandom(32)
    b64 = base64.b64encode(key_bytes).decode()
    md5 = base64.b64encode(hashlib.md5(key_bytes).digest()).decode()
    other_bytes = _os.urandom(32)
    other_b64 = base64.b64encode(other_bytes).decode()
    other_md5 = base64.b64encode(hashlib.md5(other_bytes).digest()).decode()
    okey = "ssec/object"

    put, err = _try(s3.put_object, Bucket=bucket, Key=okey, Body=BODY,
                    SSECustomerAlgorithm="AES256", SSECustomerKey=b64, SSECustomerKeyMD5=md5)
    if err is not None:
        _encryption_finding(
            record, err, "SSE-C (customer-provided key)",
            "AWS S3 refuses SSE-C over plain HTTP as well, so refusing it here is conformant "
            "behaviour that this harness's plain-HTTP endpoint simply cannot exercise -- read "
            "this cell as not-exercisable, not as a missing feature.")
    echoed = put.get("SSECustomerAlgorithm")

    resp, err = _try(s3.get_object, Bucket=bucket, Key=okey, SSECustomerAlgorithm="AES256",
                     SSECustomerKey=b64, SSECustomerKeyMD5=md5)
    if err is not None:
        obs = _observed(err)
        _finding(record, "diverges",
                 f"the SSE-C PUT was accepted but the object cannot be read back with the same "
                 f"key: {_fmt(obs)}", obs)
    body = resp["Body"].read()
    if body != BODY:
        _finding(record, "diverges", f"the SSE-C object read back as {body[:80]!r}")

    # An encrypted object must not be readable without the key, nor under a
    # different one. Whether the unkeyed read hands back the plaintext is the
    # difference between "the key is not required" and "the object was never
    # encrypted", so it is observed rather than inferred.
    naked, naked_err = _try(s3.get_object, Bucket=bucket, Key=okey)
    if naked_err is None:
        leaked = naked["Body"].read()
        _finding(record, "accepted_not_enforced",
                 f"the SSE-C headers are inert: the PUT echoed SSECustomerAlgorithm={echoed!r}, "
                 f"and a GET carrying no key at all returns HTTP {_status(naked)} and "
                 + (f"the plaintext ({leaked!r}). The object is stored unencrypted while the "
                    f"caller believes it is encrypted under a key only they hold."
                    if leaked == BODY else
                    f"{len(leaked)} bytes of other content, so the key is not required to read it."),
                 {"http_status": _status(naked), "returned_plaintext": leaked == BODY,
                  "sse_customer_algorithm_echoed": echoed,
                  "body": leaked[:120].decode(errors="replace")})
    naked_obs = _observed(naked_err)

    wrong, wrong_err = _try(s3.get_object, Bucket=bucket, Key=okey,
                            SSECustomerAlgorithm="AES256", SSECustomerKey=other_b64,
                            SSECustomerKeyMD5=other_md5)
    if wrong_err is None:
        leaked = wrong["Body"].read()
        _finding(record, "accepted_not_enforced",
                 f"a different, unrelated 32-byte customer key reads the object back: HTTP "
                 f"{_status(wrong)} and "
                 + (f"the plaintext ({leaked!r}). The supplied key is not being used to encrypt "
                    f"anything."
                    if leaked == BODY else
                    f"{len(leaked)} bytes of other content."),
                 {"http_status": _status(wrong), "returned_plaintext": leaked == BODY,
                  "body": leaked[:120].decode(errors="replace")})
    wrong_obs = _observed(wrong_err)

    # The right key with the wrong key-MD5: the two are checked against each
    # other, or they are not being checked at all.
    mismatched, mismatched_err = _try(s3.get_object, Bucket=bucket, Key=okey,
                                      SSECustomerAlgorithm="AES256", SSECustomerKey=b64,
                                      SSECustomerKeyMD5=other_md5)
    if mismatched_err is None:
        _finding(record, "accepted_not_enforced",
                 f"the correct key presented with another key's MD5 is accepted (HTTP "
                 f"{_status(mismatched)}), so SSECustomerKeyMD5 is not validated against the key",
                 {"http_status": _status(mismatched)})
    mismatched_obs = _observed(mismatched_err)

    for label, obs in (("no key", naked_obs), ("a different 32-byte key", wrong_obs),
                       ("the correct key and a mismatched key MD5", mismatched_obs)):
        if obs["http_status"] not in (400, 403):
            _finding(record, "diverges",
                     f"the read with {label} was refused, but with {_fmt(obs)} rather than the "
                     f"400 or 403 AWS answers -- the refusal does not look like an SSE-C "
                     f"rejection", obs)

    data_path = (f"the data path is correct: the object round-trips under the customer key (PUT "
                 f"echoed SSECustomerAlgorithm={echoed!r}), a read with no key is refused "
                 f"({_fmt(naked_obs)}), a read with a different 32-byte key is refused "
                 f"({_fmt(wrong_obs)}), and the correct key with a mismatched key MD5 is refused "
                 f"({_fmt(mismatched_obs)})")
    evidence = {"no_key": naked_obs, "wrong_key": wrong_obs,
                "mismatched_key_md5": mismatched_obs,
                "sse_customer_algorithm_echoed": echoed}

    scheme = urllib.parse.urlparse(s3.meta.endpoint_url).scheme
    if scheme != "https":
        evidence["endpoint_scheme"] = scheme
        # The framing leads, and the evidence follows it. If this detail is ever
        # truncated or skimmed, the half a reader must not lose is that the
        # encryption itself is sound.
        _finding(record, "diverges",
                 f"a transport-contract divergence, not an encryption defect. The request was "
                 f"accepted over plain {scheme}, which means the 32-byte customer key travelled "
                 f"in cleartext in the x-amz-server-side-encryption-customer-key header. AWS S3 "
                 f"refuses SSE-C over anything but HTTPS, and that requirement belongs to the "
                 f"SSE-C contract rather than to general transport policy, precisely because the "
                 f"key rides in a header. Otherwise {data_path}.",
                 evidence)
    evidence["endpoint_scheme"] = scheme
    record("supported", f"{data_path}; and it is served over https, as the SSE-C contract "
                        f"requires", evidence)


def test_bucket_policy_can_be_set_and_read(s3, bucket, record):
    policy = _json.dumps({
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"AWS": ["*"]},
                       "Action": ["s3:GetObject"],
                       "Resource": [f"arn:aws:s3:::{bucket}/public/*"]}],
    })
    _, err = _try(s3.put_bucket_policy, Bucket=bucket, Policy=policy)
    if err is not None:
        obs = _observed(err)
        _finding(record, "not_implemented", f"PutBucketPolicy was rejected: {_fmt(obs)}", obs)
    resp, err = _try(s3.get_bucket_policy, Bucket=bucket)
    if err is not None:
        obs = _observed(err)
        _finding(record, "accepted_not_enforced",
                 f"PutBucketPolicy was accepted but GetBucketPolicy then fails, so the policy "
                 f"was not stored: {_fmt(obs)}", obs)
    try:
        back = _json.loads(resp["Policy"])
    except (ValueError, KeyError, TypeError):
        _finding(record, "diverges",
                 f"GetBucketPolicy returned something that is not a JSON policy: "
                 f"{str(resp)[:200]!r}")
    statements = back.get("Statement", [])
    actions = statements[0].get("Action") if statements else None
    if actions not in ("s3:GetObject", ["s3:GetObject"]):
        _finding(record, "diverges",
                 f"the policy read back with Action={actions!r}, not the one written",
                 {"statement": statements[:1]})
    record("supported", "bucket policy stored and read back with the same statement")
