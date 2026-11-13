"""Minimal AWS Signature Version 4 for S3 header auth, since Painter has no boto3.

Validated against AWS's published PUT Object example (see tests). The canonical
URI is the URL path as is: S3 wants single encoding and no normalization.
"""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from urllib.parse import urlsplit

_CHUNK = 1024 * 1024


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file_hex(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sign(
    *,
    method: str,
    url: str,
    headers: dict,
    payload_hash: str,
    access_key: str,
    secret_key: str,
    region: str,
    service: str = "s3",
    amz_date: str,
) -> dict:
    parts = urlsplit(url)
    date_stamp = amz_date[:8]

    all_headers = dict(headers)
    all_headers["host"] = parts.netloc
    all_headers["x-amz-date"] = amz_date
    all_headers["x-amz-content-sha256"] = payload_hash

    lowered = {k.lower(): str(v).strip() for k, v in all_headers.items()}
    signed_names = sorted(lowered)
    canonical_headers = "".join(f"{k}:{lowered[k]}\n" for k in signed_names)
    signed_headers = ";".join(signed_names)

    canonical_request = "\n".join(
        [
            method,
            parts.path or "/",
            parts.query,  # already-encoded query string, empty for our PUTs
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )

    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, sha256_hex(canonical_request.encode())]
    )

    k_date = _hmac(f"AWS4{secret_key}".encode(), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    k_signing = _hmac(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()

    all_headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return all_headers
