"""S3 PUT with SigV4 over http.client, path-style addressing (AWS and LocalStack).

S3 rejects chunked transfer-encoding, so the file is read into memory for its
Content-Length. The body goes out in slices with on_bytes after each; urllib
sends it whole with no hook, so a 40 MB upload sat at 0% then jumped to done.
"""

from __future__ import annotations

import collections.abc
import dataclasses
import datetime
import http.client
from pathlib import Path
from urllib.parse import quote, urlsplit

from . import sigv4
from .api import SSL_CONTEXT, FatalError, TransientError
from .mirisupload import CreateAssetResult
from .version import USER_AGENT


@dataclasses.dataclass(frozen=True)
class S3Target:
    endpoint_url: str
    region: str
    access_key_id: str
    secret_key: str
    session_token: str
    bucket: str


def parse_s3_upload_path(upload_path: str) -> tuple[str, str]:
    rest = upload_path.removeprefix("s3://")
    bucket, _, prefix = rest.partition("/")
    return bucket, prefix.rstrip("/")


def join_key(prefix: str, rel: str) -> str:
    p = prefix.strip("/")
    r = rel.lstrip("/")
    return f"{p}/{r}" if p else r


def target_from_asset(result: CreateAssetResult) -> tuple[S3Target, str]:
    bucket, prefix = parse_s3_upload_path(result.upload_path)
    target = S3Target(
        endpoint_url=result.endpoint_url.rstrip("/"),
        region=result.region,
        access_key_id=result.access_key_id,
        secret_key=result.secret_key,
        session_token=result.session_token,
        bucket=bucket,
    )
    return target, prefix


# One progress report per slice. A slice is handed to the kernel, not yet
# acknowledged by S3, so the bar runs a little ahead of the wire; near enough.
_SLICE = 1 << 20


def _connection(parts, timeout: float) -> http.client.HTTPConnection:
    if parts.scheme == "https":
        return http.client.HTTPSConnection(
            parts.hostname, parts.port, timeout=timeout, context=SSL_CONTEXT
        )
    return http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)


def put_file(
    target: S3Target,
    key: str,
    file_path: Path,
    *,
    on_bytes: collections.abc.Callable[[int], None],
    timeout: float = 60.0,
) -> None:
    payload = file_path.read_bytes()
    total = len(payload)
    payload_hash = sigv4.sha256_hex(payload)
    quoted_key = quote(key, safe="/-_.~")
    url = f"{target.endpoint_url}/{target.bucket}/{quoted_key}"
    amz_date = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")

    headers = sigv4.sign(
        method="PUT",
        url=url,
        headers={"x-amz-security-token": target.session_token},
        payload_hash=payload_hash,
        access_key=target.access_key_id,
        secret_key=target.secret_key,
        region=target.region,
        amz_date=amz_date,
    )
    headers["Content-Length"] = str(total)
    headers["User-Agent"] = USER_AGENT

    parts = urlsplit(url)
    connection = _connection(parts, timeout)
    try:
        # Host is already in the signed headers; a second one breaks the signature.
        connection.putrequest("PUT", parts.path or "/", skip_host=True, skip_accept_encoding=True)
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.endheaders()
        sent = 0
        while sent < total:
            end = min(sent + _SLICE, total)
            connection.send(payload[sent:end])
            sent = end
            on_bytes(sent)
        response = connection.getresponse()
        status = response.status
        body = response.read()[:240].decode("utf-8", errors="replace")
    except (OSError, http.client.HTTPException) as e:
        # Timeouts, TLS failures and dropped connections are transient, like a 5xx.
        raise TransientError(f"PUT {url} failed: {e}") from e
    finally:
        connection.close()
    if status >= 500:
        raise TransientError(f"PUT {url} returned {status}: {body}")
    if status >= 300:
        raise FatalError(f"PUT {url} returned {status}: {body}")
