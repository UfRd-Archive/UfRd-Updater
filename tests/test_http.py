from __future__ import annotations

import gzip
import json
import tracemalloc
import zlib
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from ufrd_updater import http
from ufrd_updater.config import Settings

from .conftest import Response


def settings(**overrides) -> Settings:
    values = dict(
        timeout=5.0,
        download_timeout=5.0,
        connect_timeout=5.0,
        retries=0,
    )
    values.update(overrides)
    return Settings(**values)


class ResponseStream(httpx.SyncByteStream):
    def __init__(self, data: bytes, *, chunk_size: int = 65536, forbid_read: bool = False):
        self.data = data
        self.chunk_size = chunk_size
        self.forbid_read = forbid_read
        self.iterated = False
        self.closed = False

    def __iter__(self):
        self.iterated = True
        if self.forbid_read:
            pytest.fail("redirect response body was consumed")
        for start in range(0, len(self.data), self.chunk_size):
            yield self.data[start : start + self.chunk_size]

    def close(self):
        self.closed = True


def encode_body(body: bytes, codings: tuple[str, ...]) -> bytes:
    for coding in codings:
        if coding == "gzip":
            body = gzip.compress(body, mtime=0)
        elif coding == "deflate":
            body = zlib.compress(body)
        elif coding == "raw-deflate":
            encoder = zlib.compressobj(wbits=-zlib.MAX_WBITS)
            body = encoder.compress(body) + encoder.flush()
    return body


def test_redaction_removes_query_fragment_and_userinfo():
    assert (
        http.redact_url("https://user:pass@example.test:8443/a/b?token=secret#frag")
        == "https://example.test:8443/a/b"
    )
    assert http.redact_url("https://example.test/a") == "https://example.test/a"


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@example.test:not-a-port/a?token=hidden",
        "https://[broken/a?token=hidden",
        "https://[not-ipv6]/a?token=hidden",
    ],
)
def test_redaction_does_not_leak_malformed_url_credentials(url):
    redacted = http.redact_url(url)
    assert "secret" not in redacted
    assert "hidden" not in redacted
    assert "user:" not in redacted
    assert "?" not in redacted
    assert "#" not in redacted


def test_free_text_redaction_is_case_insensitive_and_consumes_query_suffix():
    result = http.redact_text(
        "failed (HTTPS://user:secret@example.test/a?token=hidden)still-secret#fragment)"
    )
    assert result == "failed (https://example.test/a"


def test_header_layers_override_each_item_case_insensitively():
    assert http.merge_headers(
        {"Authorization": "first", "X-Value": "one", "x-value": "two"},
        {"authorization": "second", "ACCEPT-ENCODING": "gzip"},
        {"Accept-Encoding": "identity"},
    ) == {
        "authorization": "second",
        "x-value": "two",
        "accept-encoding": "identity",
    }


def test_retry_after_parsing():
    assert http.parse_retry_after("7") == 7.0
    assert http.parse_retry_after("999") == 60.0
    assert http.parse_retry_after("not-a-date") is None
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=10), usegmt=True)
    value = http.parse_retry_after(future)
    assert value is not None and 0 <= value <= 10
    past = format_datetime(datetime.now(UTC) - timedelta(seconds=10), usegmt=True)
    assert http.parse_retry_after(past) == 0.0


@pytest.mark.parametrize("value", ["²", "٤", "７"])
def test_retry_after_rejects_non_ascii_digits(value):
    assert http.parse_retry_after(value) is None


def test_retry_after_bounds_numeric_conversion():
    assert http.parse_retry_after("9" * 10000) == 60.0
    assert http.parse_retry_after("0" * 10000 + "7") == 7.0
    assert http.parse_retry_after("0" * 10000) == 0.0


def test_retry_delay_backoff_and_cap(monkeypatch):
    monkeypatch.setattr(http.random, "uniform", lambda low, high: 0.0)
    assert http.retry_delay(0) == 1.0
    assert http.retry_delay(1) == 2.0
    assert http.retry_delay(10) == 10.0
    assert http.retry_delay(1024) == 10.0
    assert http.retry_delay(10**100) == 10.0
    assert http.retry_delay(0, 100.0) == 60.0


def test_metadata_retries_and_expect_status(server, no_sleep):
    attempts = {"count": 0}

    def route(method, parsed, headers):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return Response(status=500, body=b"boom")
        return Response(status=201, body=b"ok")

    server.route("/meta", route)
    with http.make_client(settings()) as client:
        response = http.request(
            client,
            method="GET",
            url=server.url("/meta"),
            expect_status=(201,),
            timeout=http.metadata_timeout(settings()),
            retries=1,
        )
    assert response.status == 201
    assert response.content == b"ok"
    assert attempts["count"] == 2
    assert len(no_sleep) == 1


def test_expected_retryable_status_is_accepted_after_retries(server, no_sleep):
    server.route("/always-500", Response(status=500, body=b"boom"))
    with http.make_client(settings()) as client:
        response = http.request(
            client,
            method="GET",
            url=server.url("/always-500"),
            expect_status=(500,),
            timeout=http.metadata_timeout(settings()),
            retries=1,
        )
    assert response.status == 500
    assert response.content == b"boom"
    assert server.count("/always-500") == 2
    assert len(no_sleep) == 1

    with http.make_client(settings()) as client:
        with pytest.raises(http.RequestError, match="HTTP 500"):
            http.request(
                client,
                method="GET",
                url=server.url("/always-500"),
                timeout=http.metadata_timeout(settings()),
                retries=1,
            )
    assert server.count("/always-500") == 4


def test_unexpected_status_is_not_retried(server, no_sleep):
    server.route("/meta", Response(status=200, body=b"ok"))
    with http.make_client(settings()) as client:
        with pytest.raises(http.RequestError, match="HTTP 200"):
            http.request(
                client,
                method="GET",
                url=server.url("/meta"),
                expect_status=(201,),
                timeout=http.metadata_timeout(settings()),
                retries=3,
            )
    assert server.count("/meta") == 1
    assert no_sleep == []


def test_metadata_body_cap(server):
    server.route("/large", Response(body=b"x" * 64))
    with http.make_client(settings()) as client:
        with pytest.raises(http.RequestError, match="exceeds 32"):
            http.request(
                client,
                method="GET",
                url=server.url("/large"),
                timeout=http.metadata_timeout(settings()),
                retries=0,
                max_bytes=32,
            )


def test_error_message_redacts_query(server):
    server.route("/secret", Response(status=404, body=b"no"))
    with http.make_client(settings()) as client:
        with pytest.raises(http.RequestError) as error:
            http.request(
                client,
                method="GET",
                url=server.url("/secret?token=hunter2"),
                timeout=http.metadata_timeout(settings()),
                retries=0,
            )
    message = str(error.value)
    assert "hunter2" not in message
    assert "http://127.0.0.1" in message


def test_json_body_sets_content_type(server):
    server.route("/json", Response(body=b"{}"))
    with http.make_client(settings()) as client:
        http.request(
            client,
            method="POST",
            url=server.url("/json"),
            json_body={"answer": 42},
            timeout=http.metadata_timeout(settings()),
            retries=0,
        )
    request = server.requests[-1]
    assert httpx.Headers(request.headers)["Content-Type"] == "application/json"
    assert json.loads(request.body) == {"answer": 42}


def test_json_content_type_override(server):
    server.route("/json", Response(body=b"{}"))
    with http.make_client(settings()) as client:
        http.request(
            client,
            method="POST",
            url=server.url("/json"),
            headers={"Content-Type": "application/vnd.ufrd+json"},
            json_body={"answer": 42},
            timeout=http.metadata_timeout(settings()),
            retries=0,
        )
    request = server.requests[-1]
    assert httpx.Headers(request.headers)["Content-Type"] == "application/vnd.ufrd+json"
    assert json.loads(request.body) == {"answer": 42}


def test_client_default_user_agent(server):
    server.route("/ua", Response(body=b"ok"))
    custom = settings(user_agent="ufrd-test/9")
    with http.make_client(custom) as client:
        http.request(
            client,
            method="GET",
            url=server.url("/ua"),
            timeout=http.metadata_timeout(custom),
            retries=0,
        )
    header = server.requests[-1].headers.get("User-Agent")
    assert header == "ufrd-test/9"


@pytest.mark.parametrize(
    "codings",
    [
        ("gzip",),
        ("deflate",),
        ("raw-deflate",),
        ("gzip", "deflate"),
        ("deflate", "gzip"),
        ("gzip", "gzip"),
        ("identity", "gzip", "deflate", "identity"),
    ],
)
@pytest.mark.parametrize("chunk_size", [1, 7, 65536])
def test_streaming_decoders_handle_boundaries_and_stacked_encodings(codings, chunk_size):
    body = bytes(range(256)) * 1025
    encoded = encode_body(body, codings)
    stream = ResponseStream(encoded, chunk_size=chunk_size)
    encoding = ", ".join("deflate" if value == "raw-deflate" else value for value in codings)

    def transport(request):
        return httpx.Response(200, headers={"Content-Encoding": encoding}, stream=stream)

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        with http.stream_response(
            client,
            "GET",
            "https://example.test/asset",
            timeout=http.metadata_timeout(settings()),
        ) as response:
            chunks = list(http.iter_response_bytes(response))
    assert b"".join(chunks) == body
    assert max(map(len, chunks)) <= 65536
    assert stream.closed


def test_stacked_compression_cap_precedes_large_decoded_allocation():
    encoded = encode_body(b"x" * (8 * 1024 * 1024), ("gzip", "gzip"))
    stream = ResponseStream(encoded)

    def transport(request):
        return httpx.Response(
            200, headers={"Content-Encoding": "gzip, gzip"}, stream=stream
        )

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        tracemalloc.start()
        try:
            with pytest.raises(http.RequestError, match="exceeds 4096"):
                http.request(
                    client,
                    method="GET",
                    url="https://example.test/bomb?token=hidden",
                    timeout=http.metadata_timeout(settings()),
                    retries=0,
                    max_bytes=4096,
                )
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    assert peak < 2 * 1024 * 1024
    assert stream.closed


@pytest.mark.parametrize("max_bytes", [0, 4096])
def test_decoded_body_limit_is_exact(max_bytes):
    body = b"x" * max_bytes
    stream = ResponseStream(gzip.compress(body, mtime=0), chunk_size=1)

    def transport(request):
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=stream)

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        response = http.request(
            client,
            method="GET",
            url="https://example.test/exact",
            timeout=http.metadata_timeout(settings()),
            retries=0,
            max_bytes=max_bytes,
        )
    assert response.content == body


@pytest.mark.parametrize(
    ("codings", "encoded", "message"),
    [
        (("gzip",), b"invalid gzip secret", "invalid compressed"),
        (("deflate",), b"\x07\xffsecret", "invalid compressed"),
        (("gzip",), encode_body(b"body", ("gzip",))[:-1], "truncated compressed"),
        (("deflate",), encode_body(b"body", ("deflate",))[:-1], "truncated compressed"),
        (
            ("deflate",),
            encode_body(b"body", ("raw-deflate",))[:-1],
            "truncated compressed",
        ),
        (
            ("gzip", "deflate"),
            zlib.compress(gzip.compress(b"body", mtime=0)[:-8]),
            "truncated compressed",
        ),
        (
            ("gzip", "deflate"),
            encode_body(b"body", ("gzip", "deflate"))[:-1],
            "truncated compressed",
        ),
        (
            ("gzip",),
            gzip.compress(b"body", mtime=0) + b"secret trailing garbage",
            "invalid compressed",
        ),
    ],
)
def test_malformed_and_truncated_compressed_responses_are_safe(codings, encoded, message):
    stream = ResponseStream(encoded, chunk_size=1)

    def transport(request):
        return httpx.Response(
            200, headers={"Content-Encoding": ", ".join(codings)}, stream=stream
        )

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(http.RequestError, match=message) as error:
            http.request(
                client,
                method="GET",
                url="https://example.test/bad?token=hidden",
                timeout=http.metadata_timeout(settings()),
                retries=0,
            )
    assert "secret" not in str(error.value)
    assert "hidden" not in str(error.value)
    assert stream.closed


def test_gzip_checksum_is_validated():
    encoded = bytearray(gzip.compress(b"body", mtime=0))
    encoded[-8] ^= 1
    stream = ResponseStream(bytes(encoded), chunk_size=1)

    def transport(request):
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=stream)

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(http.RequestError, match="invalid compressed"):
            http.request(
                client,
                method="GET",
                url="https://example.test/bad",
                timeout=http.metadata_timeout(settings()),
                retries=0,
            )


def test_concatenated_gzip_members_are_decoded():
    stream = ResponseStream(
        gzip.compress(b"first", mtime=0) + gzip.compress(b"second", mtime=0), chunk_size=1
    )

    def transport(request):
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=stream)

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        response = http.request(
            client,
            method="GET",
            url="https://example.test/members",
            timeout=http.metadata_timeout(settings()),
            retries=0,
        )
    assert response.content == b"firstsecond"


def test_unsupported_encoding_fails_before_body_read():
    stream = ResponseStream(b"body", forbid_read=True)

    def transport(request):
        return httpx.Response(
            200, headers={"Content-Encoding": "gzip, unsupported-secret"}, stream=stream
        )

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(http.RequestError, match="unsupported") as error:
            http.request(
                client,
                method="GET",
                url="https://example.test/body",
                timeout=http.metadata_timeout(settings()),
                retries=0,
            )
    assert "secret" not in str(error.value)
    assert not stream.iterated
    assert stream.closed


@pytest.mark.parametrize(
    ("status", "method", "redirected_method"),
    [
        (301, "POST", "GET"),
        (301, "PUT", "PUT"),
        (302, "PUT", "GET"),
        (303, "PATCH", "GET"),
        (303, "HEAD", "HEAD"),
        (307, "POST", "POST"),
        (308, "PUT", "PUT"),
    ],
)
def test_redirects_close_without_reading_and_preserve_httpx_semantics(
    status, method, redirected_method
):
    redirect = ResponseStream(b"unbounded redirect", forbid_read=True)
    final = ResponseStream(b"ok")
    outgoing = []

    def transport(request):
        outgoing.append(request)
        if request.url.path == "/start":
            return httpx.Response(
                status,
                headers={"Location": "/final", "Set-Cookie": "session=new; Path=/"},
                stream=redirect,
            )
        return httpx.Response(200, stream=final)

    with httpx.Client(
        transport=httpx.MockTransport(transport),
        auth=("user", "password"),
        cookies={"initial": "cookie"},
    ) as client:
        with http.stream_response(
            client,
            method,
            "https://example.test/start",
            content=b"body",
            timeout=http.metadata_timeout(settings()),
        ) as response:
            assert response.status_code == 200
            assert [item.status_code for item in response.history] == [status]
            assert b"".join(http.iter_response_bytes(response)) == b"ok"
    assert len(outgoing) == 2
    assert outgoing[1].method == redirected_method
    assert outgoing[1].content == (b"body" if method == redirected_method else b"")
    assert outgoing[1].headers["Authorization"] == outgoing[0].headers["Authorization"]
    assert "initial=cookie" in outgoing[1].headers["Cookie"]
    assert "session=new" in outgoing[1].headers["Cookie"]
    assert not redirect.iterated
    assert redirect.closed and final.closed


@pytest.mark.parametrize(
    ("destination", "authorized"),
    [
        ("/final", True),
        ("https://example.test/final", True),
        ("https://other.test/final", False),
        ("http://example.test:8080/final", False),
    ],
)
def test_redirects_do_not_reapply_client_auth_across_origins(destination, authorized):
    redirect = ResponseStream(b"unread", forbid_read=True)
    outgoing = []

    def transport(request):
        outgoing.append(request)
        if request.url.path == "/start":
            return httpx.Response(
                302,
                headers={
                    "Location": destination,
                    "Set-Cookie": "host_session=private; Path=/",
                },
                stream=redirect,
            )
        return httpx.Response(200, stream=ResponseStream(b"ok"))

    with httpx.Client(
        transport=httpx.MockTransport(transport), auth=("user", "password")
    ) as client:
        response = http.request(
            client,
            method="GET",
            url="http://example.test/start",
            timeout=http.metadata_timeout(settings()),
            retries=0,
        )
    assert response.content == b"ok"
    assert "Authorization" in outgoing[0].headers
    assert ("Authorization" in outgoing[1].headers) is authorized
    if outgoing[1].url.host == "other.test":
        assert "Cookie" not in outgoing[1].headers
    assert not redirect.iterated
    assert redirect.closed


def test_authentication_flow_sees_final_redirect_response():
    class ChallengeAuth(httpx.Auth):
        def auth_flow(self, request):
            response = yield request
            assert response.status_code == 401
            request.headers["Authorization"] = "Bearer challenge"
            yield request

    redirects = []
    outgoing = []

    def transport(request):
        outgoing.append(request)
        if request.url.path == "/start":
            stream = ResponseStream(b"unread", forbid_read=True)
            redirects.append(stream)
            return httpx.Response(302, headers={"Location": "/final"}, stream=stream)
        status = 200 if "Authorization" in request.headers else 401
        return httpx.Response(status, stream=ResponseStream(b"ok"))

    with httpx.Client(
        transport=httpx.MockTransport(transport), auth=ChallengeAuth()
    ) as client:
        response = http.request(
            client,
            method="GET",
            url="https://example.test/start",
            timeout=http.metadata_timeout(settings()),
            retries=0,
        )
    assert response.status == 200
    assert [request.url.path for request in outgoing] == ["/start", "/final"] * 2
    assert all(stream.closed and not stream.iterated for stream in redirects)


def test_redirect_limit_is_preserved_without_consuming_bodies():
    streams = []

    def transport(request):
        stream = ResponseStream(b"unread", forbid_read=True)
        streams.append(stream)
        return httpx.Response(302, headers={"Location": "/loop"}, stream=stream)

    with httpx.Client(
        transport=httpx.MockTransport(transport), max_redirects=2
    ) as client:
        with pytest.raises(http.RequestError, match="redirect limit exceeded"):
            http.request(
                client,
                method="GET",
                url="https://example.test/loop?token=hidden",
                timeout=http.metadata_timeout(settings()),
                retries=0,
            )
    assert len(streams) == 3
    assert all(stream.closed and not stream.iterated for stream in streams)


def test_redirect_following_can_be_disabled():
    outgoing = []
    stream = ResponseStream(b"redirect")

    def transport(request):
        outgoing.append(request)
        return httpx.Response(302, headers={"Location": "/final"}, stream=stream)

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        with http.stream_response(
            client,
            "GET",
            "https://example.test/start",
            follow_redirects=False,
            timeout=http.metadata_timeout(settings()),
        ) as response:
            assert response.status_code == 302
            assert response.next_request.url.path == "/final"
            assert b"".join(http.iter_response_bytes(response)) == b"redirect"
    assert len(outgoing) == 1
    assert stream.closed


def test_response_obs_text_header_bytes_are_preserved(server):
    server.route("/headers", Response(body=b"ok", headers={"X-Note": "caf\xe9"}))
    with http.make_client(settings()) as client:
        response = http.request(
            client,
            method="GET",
            url=server.url("/headers"),
            timeout=http.metadata_timeout(settings()),
            retries=0,
        )
    assert response.headers["X-Note"] == "caf\xe9"
    assert (b"X-Note", b"caf\xe9") in response.headers.raw


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer very-secret\n"},
        {"X-Note": "private-caf\xe9"},
    ],
)
def test_invalid_request_headers_fail_safely(server, headers):
    server.route("/headers", Response(body=b"ok"))
    with http.make_client(settings()) as client:
        with pytest.raises(http.RequestError) as error:
            http.request(
                client,
                method="GET",
                url=server.url("/headers?token=hidden"),
                headers=headers,
                timeout=http.metadata_timeout(settings()),
                retries=0,
            )
    message = str(error.value)
    assert "failed:" in message
    assert "very-secret" not in message
    assert "private-caf" not in message
    assert "hidden" not in message
    assert server.requests == []


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), "\ud800"])
def test_invalid_json_request_values_fail_before_http(server, value):
    server.route("/json", Response(body=b"ok"))
    with http.make_client(settings()) as client:
        with pytest.raises(http.RequestError, match="invalid HTTP request JSON"):
            http.request(
                client,
                method="POST",
                url=server.url("/json"),
                json_body={"nested": [value]},
                timeout=http.metadata_timeout(settings()),
                retries=0,
            )
    assert server.count("/json") == 0


def test_invalid_request_url_is_a_safe_request_error():
    def transport(request):
        pytest.fail("invalid URL reached transport")

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(http.RequestError) as error:
            http.request(
                client,
                method="GET",
                url="https://user:secret@example.test:bad/a?token=hidden",
                timeout=http.metadata_timeout(settings()),
                retries=0,
            )
    assert "<invalid URL>" in str(error.value)
    assert "secret" not in str(error.value)
    assert "hidden" not in str(error.value)
