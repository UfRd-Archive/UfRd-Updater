"""HTTP plumbing: one client per run, bounded metadata bodies, retries."""

from __future__ import annotations

import json
import random
import re
import time
import zlib
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .config import Settings

MAX_METADATA_BYTES = 32 * 1024 * 1024
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
MAX_RETRY_AFTER = 60.0
MAX_BACKOFF = 10.0
RESPONSE_CHUNK_BYTES = 64 * 1024


class RequestError(Exception):
    """A failed HTTP request with a redacted message."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    url: str
    headers: httpx.Headers
    content: bytes


def make_client(settings: Settings) -> httpx.Client:
    timeout = httpx.Timeout(settings.timeout, connect=settings.connect_timeout)
    return httpx.Client(
        timeout=timeout,
        headers={"User-Agent": settings.user_agent},
    )


def metadata_timeout(settings: Settings, override: float | None = None) -> httpx.Timeout:
    return httpx.Timeout(
        override if override is not None else settings.timeout,
        connect=settings.connect_timeout,
        write=settings.connect_timeout,
        pool=settings.connect_timeout,
    )


def download_timeout(settings: Settings) -> httpx.Timeout:
    return httpx.Timeout(
        settings.download_timeout,
        connect=settings.connect_timeout,
        write=settings.connect_timeout,
        pool=settings.connect_timeout,
    )


URL_IN_TEXT = re.compile(r"https?://\S+", re.IGNORECASE)


def merge_headers(*layers: Mapping[str, str]) -> dict[str, str]:
    """Merge header layers, overriding each name case-insensitively."""

    return {name.lower(): value for layer in layers for name, value in layer.items()}


def redact_url(url: str) -> str:
    """Drop query, fragment and userinfo, failing closed for malformed URLs."""

    try:
        parsed = httpx.URL(url)
        return str(
            parsed.copy_with(
                query=None,
                fragment=None,
                username=None,
                password=None,
            )
        )
    except (httpx.InvalidURL, TypeError, ValueError, UnicodeError):
        return "<invalid URL>"


def redact_text(text: str) -> str:
    """Redact every http(s) URL embedded in free-form text."""

    return URL_IN_TEXT.sub(lambda match: redact_url(match.group(0)), text)


def status_expected(status: int, expect_status: tuple[int, ...] | None) -> bool:
    if expect_status is not None:
        return status in expect_status
    return 200 <= status < 300


def is_retryable_status(status: int) -> bool:
    return status in RETRYABLE_STATUSES


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    if value.isascii() and value.isdecimal():
        digits = value.lstrip("0")
        # The cap is two digits; never convert an arbitrarily long integer.
        if len(digits) > 2:
            return MAX_RETRY_AFTER
        return min(float(digits or "0"), MAX_RETRY_AFTER)
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    delta = (moment - datetime.now(UTC)).total_seconds()
    return max(0.0, min(delta, MAX_RETRY_AFTER))


def retry_delay(attempt: int, retry_after: float | None = None) -> float:
    """Backoff for a zero-based attempt index (1s, 2s, 4s, ... capped)."""

    if retry_after is not None:
        return min(retry_after, MAX_RETRY_AFTER)
    base = min(2.0 ** min(attempt, 4), MAX_BACKOFF)
    return base + random.uniform(0.0, 0.25)


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def safe_exception(exc: Exception, url: str) -> str:
    """Describe failures without echoing protocol data or request credentials."""

    if isinstance(exc, httpx.InvalidURL):
        return "InvalidURL: invalid URL"
    if isinstance(exc, httpx.TimeoutException):
        return "TimeoutException: request timed out"
    if isinstance(exc, httpx.TooManyRedirects):
        return "TooManyRedirects: redirect limit exceeded"
    if isinstance(exc, httpx.UnsupportedProtocol):
        return "UnsupportedProtocol: unsupported URL protocol"
    if isinstance(exc, httpx.ProtocolError):
        return "ProtocolError: invalid HTTP protocol data"
    if isinstance(exc, httpx.DecodingError):
        return "DecodingError: invalid response encoding"
    if isinstance(exc, httpx.TransportError):
        return "TransportError: HTTP transport failed"
    if isinstance(exc, httpx.HTTPError):
        return "HTTPError: HTTP request failed"
    if isinstance(exc, UnicodeError):
        return "UnicodeError: invalid request text"
    if isinstance(exc, ValueError):
        return "ValueError: invalid request value"
    return "Exception: request failed"


@contextmanager
def stream_response(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    content: bytes | str | None = None,
    follow_redirects: bool = True,
    timeout: httpx.Timeout,
) -> Iterator[httpx.Response]:
    """Stream using HTTPX redirect requests, never reading redirect bodies."""

    response: httpx.Response | None = None
    auth_flow = None
    try:
        try:
            outgoing = client.build_request(
                method,
                url,
                headers=merge_headers(headers or {}),
                content=content,
                timeout=timeout,
            )
            auth = client.auth
            if auth is None:
                username, password = outgoing.url.username, outgoing.url.password
                auth = (
                    httpx.BasicAuth(username, password)
                    if username or password
                    else httpx.Auth()
                )
            auth_flow = auth.sync_auth_flow(outgoing)
            outgoing = next(auth_flow)
        except (httpx.InvalidURL, UnicodeError, ValueError) as exc:
            raise RequestError(
                f"{method} {redact_url(url)} failed: {safe_exception(exc, url)}"
            ) from exc

        history: list[httpx.Response] = []
        # Authentication runs around the entire redirect chain, as in HTTPX.
        # Applying client auth anew at each hop would restore stripped credentials.
        no_auth = httpx.Auth()
        while True:
            if len(history) > client.max_redirects:
                raise httpx.TooManyRedirects(
                    "Exceeded maximum allowed redirects.", request=outgoing
                )
            try:
                response = client.send(
                    outgoing, stream=True, follow_redirects=False, auth=no_auth
                )
            except (httpx.InvalidURL, UnicodeError, ValueError) as exc:
                raise RequestError(
                    f"{method} {redact_url(url)} failed: {safe_exception(exc, url)}"
                ) from exc
            response.history = list(history)
            if follow_redirects and response.next_request is not None:
                outgoing = response.next_request
                history.append(response)
                response.close()
                continue
            try:
                outgoing = auth_flow.send(response)
            except StopIteration:
                break
            except (httpx.InvalidURL, UnicodeError, ValueError) as exc:
                raise RequestError(
                    f"{method} {redact_url(url)} failed: {safe_exception(exc, url)}"
                ) from exc
            history.append(response)
            response.close()
        yield response
    finally:
        if response is not None:
            response.close()
        if auth_flow is not None:
            auth_flow.close()


def _decode_chunks(
    chunks: Iterable[bytes], coding: str, chunk_size: int
) -> Iterator[bytes]:
    """Bound every zlib call, including intermediate stacked representations."""

    decoder = zlib.decompressobj(zlib.MAX_WBITS | 16) if coding == "gzip" else None
    prefix = b""
    for data in chunks:
        if not data:
            continue
        if decoder is None:
            # Let zlib inspect a complete two-byte header before falling back
            # to raw deflate, even when transport chunks split that header.
            needed = 2 - len(prefix)
            prefix += data[:needed]
            data = data[needed:]
            if len(prefix) < 2:
                continue
            decoder = zlib.decompressobj()
            try:
                decoder.decompress(prefix, max_length=chunk_size)
            except zlib.error:
                decoder = zlib.decompressobj(-zlib.MAX_WBITS)
                data = prefix + data
        elif decoder.eof:
            if coding != "gzip":
                raise RequestError("invalid compressed HTTP response body")
            decoder = zlib.decompressobj(zlib.MAX_WBITS | 16)

        while True:
            decoded = decoder.decompress(data, max_length=chunk_size)
            if decoded:
                yield decoded
            if decoder.eof:
                trailing = decoder.unused_data
                if not trailing:
                    break
                if coding != "gzip":
                    raise RequestError("invalid compressed HTTP response body")
                # gzip permits concatenated members. Each member is bounded too.
                decoder = zlib.decompressobj(zlib.MAX_WBITS | 16)
                data = trailing
            elif decoder.unconsumed_tail:
                data = decoder.unconsumed_tail
            elif len(decoded) == chunk_size:
                # A full output buffer can leave output pending without input.
                data = b""
            else:
                break
    # No unbounded flush: reaching zlib EOF guarantees all output and checksums
    # were processed by the bounded calls above.
    if decoder is None or not decoder.eof:
        raise RequestError("truncated compressed HTTP response body")


def iter_response_bytes(
    response: httpx.Response, *, max_bytes: int | None = None
) -> Iterator[bytes]:
    """Yield bounded decoded chunks, optionally capping the final body size."""

    chunk_size = RESPONSE_CHUNK_BYTES
    if max_bytes is not None:
        chunk_size = min(chunk_size, max(1, max_bytes + 1))
    chunks: Iterable[bytes] = (
        data[start : start + chunk_size]
        for data in response.iter_raw()
        for start in range(0, len(data), chunk_size)
    )
    # HEAD and bodyless status codes describe a representation but do not send it.
    if response.request.method != "HEAD" and response.status_code not in (204, 304):
        codings = [
            value.strip().lower()
            for value in response.headers.get("content-encoding", "").split(",")
            if value.strip()
        ]
        if any(coding not in ("gzip", "deflate", "identity") for coding in codings):
            raise RequestError("unsupported HTTP response Content-Encoding")
        for coding in reversed(codings):
            if coding != "identity":
                chunks = _decode_chunks(chunks, coding, chunk_size)
    total = 0
    try:
        for chunk in chunks:
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise RequestError(
                    f"HTTP response body exceeds {max_bytes} bytes for "
                    f"{redact_url(str(response.url))}"
                )
            yield chunk
    except zlib.error as exc:
        raise RequestError("invalid compressed HTTP response body") from exc


def request(
    client: httpx.Client,
    *,
    method: str,
    url: str,
    headers: Mapping[str, str] | None = None,
    content: bytes | str | None = None,
    json_body: Any = None,
    follow_redirects: bool = True,
    expect_status: tuple[int, ...] | None = None,
    timeout: httpx.Timeout,
    retries: int,
    max_bytes: int = MAX_METADATA_BYTES,
) -> HttpResponse:
    """Perform a request with bounded body reads and the shared retry policy."""

    effective_headers = merge_headers(headers or {})
    if json_body is not None:
        if content is not None:
            raise RequestError("internal error: content and json_body are mutually exclusive")
        try:
            content = json.dumps(json_body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
            raise RequestError("invalid HTTP request JSON body") from exc
        effective_headers.setdefault("content-type", "application/json")

    attempts = retries + 1
    last_exception: Exception | None = None
    last_status: int | None = None

    for attempt in range(attempts):
        retry_after: float | None = None
        retry = False
        try:
            with stream_response(
                client,
                method,
                url,
                headers=effective_headers,
                content=content,
                follow_redirects=follow_redirects,
                timeout=timeout,
            ) as response:
                status = response.status_code
                last_status = status
                if is_retryable_status(status) and attempt < attempts - 1:
                    retry_after = parse_retry_after(response.headers.get("retry-after"))
                    retry = True
                else:
                    # expect_status is evaluated after retries, so a caller may
                    # explicitly accept a retryable status such as 500 or 429.
                    body = b"".join(iter_response_bytes(response, max_bytes=max_bytes))
                    if not status_expected(status, expect_status):
                        raise RequestError(
                            f"{method} {redact_url(url)} returned HTTP {status}"
                        )
                    return HttpResponse(
                        status=status,
                        url=str(response.url),
                        headers=response.headers.copy(),
                        content=body,
                    )
        except RequestError:
            raise
        except httpx.TransportError as exc:
            last_exception = exc
            if attempt >= attempts - 1:
                raise RequestError(
                    f"{method} {redact_url(url)} failed: {safe_exception(exc, url)}"
                ) from exc
        except httpx.HTTPError as exc:
            raise RequestError(
                f"{method} {redact_url(url)} failed: {safe_exception(exc, url)}"
            ) from exc

        if retry:
            sleep(retry_delay(attempt, retry_after))
            continue

        if last_exception is not None:
            sleep(retry_delay(attempt))

    raise RequestError(
        f"{method} {redact_url(url)} failed"
        + (f" after HTTP {last_status}" if last_status is not None else "")
    )
