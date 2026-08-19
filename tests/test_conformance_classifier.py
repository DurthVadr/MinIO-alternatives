"""Unit tests for the conformance suite's prerequisite classifier.

This lives under tests/ rather than conformance/ on purpose. It exercises the
harness, not a running object store: it needs no Docker, and a test collected
under conformance/ that records no verdict would be written into the published
matrix as an extra cell.

What it guards: `not_exercisable` is an affirmative claim that a system behaved
correctly, so promoting a refusal into it must never happen by accident. The
classifier used to match a bare "https" substring, which any server citing a
documentation URL in an error message would have tripped -- silently turning a
real gap into a statement that the product is fine.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "conformance"))
from test_s3_conformance import _prerequisite_reason  # noqa: E402


# Verbatim messages captured from the four systems under test.
MINIO_SSE_C = ("InvalidRequest", "Requests specifying Server Side Encryption with Customer "
                                 "provided keys must be made over a secure connection.")
MINIO_SSE_S3 = ("NotImplemented", "Server side encryption specified but KMS is not configured "
                                  "(KMS not configured for a server side encrypted objects)")
RUSTFS_SSE_S3 = ("InvalidRequest", "SSE-S3 requires RUSTFS_SSE_S3_MASTER_KEY to be set to a "
                                   "base64-encoded 32-byte key when KMS is not configured.")


@pytest.mark.parametrize("code,message,expected", [
    (*MINIO_SSE_C, "conformant_refusal"),
    (*MINIO_SSE_S3, "missing_prerequisite"),
    (*RUSTFS_SSE_S3, "missing_prerequisite"),
])
def test_real_prerequisite_refusals_are_classified(code, message, expected):
    assert _prerequisite_reason(code, message) == expected


@pytest.mark.parametrize("code,message", [
    # The reported false-positive vector: a genuine gap whose message merely
    # links to documentation. Both the scheme and the word "tls" appear, and
    # neither may promote the verdict.
    ("NotImplemented", "This feature is not supported. See "
                       "https://example.com/docs/tls-setup for details."),
    ("NotImplemented", "Not supported; see https://docs.example.com/kms-and-https"),
    ("NotImplemented", "A header you provided implies functionality that is not implemented"),
    ("MethodNotAllowed", "The specified method is not allowed against this resource."),
    ("AccessDenied", "Access Denied."),
    ("", ""),
])
def test_real_gaps_are_not_promoted(code, message):
    assert _prerequisite_reason(code, message) is None, (
        "a genuine gap was promoted to not_exercisable, which publishes it as correct behaviour")


def test_urls_are_stripped_before_matching():
    """A phrase that only appears inside a URL must not count as a match."""
    assert _prerequisite_reason(
        "NotImplemented",
        "unsupported: https://example.com/must-be-made-over-a-secure-connection") is None
