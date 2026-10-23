from __future__ import annotations

import gzip
import zlib
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from ufrd_updater import http
from ufrd_updater.config import Settings
from ufrd_updater.download import DownloadTask, Downloader
from ufrd_updater.model import ResolvedEntry

from .conftest import Response
from .helpers import abc_hashes, make_entry


def make_task(entry: ResolvedEntry, root: Path) -> DownloadTask:
    return DownloadTask(
        entry=entry,
        staging_path=root
        / "stage"
        / entry.target
        / entry.arch
        / entry.filename,
    )


def run_download(tasks, *, concurrency=2, retries=0):
    settings = Settings(
        concurrency=concurrency,
        retries=retries,
        timeout=5.0,
        download_timeout=5.0,
        connect_timeout=5.0,
    )
    with http.make_client(settings) as client:
        return Downloader(client, settings).run(tasks)


def test_download_streams_and_hashes(server, tmp_path):
    server.route("/app.tar.gz", Response(body=b"abc"))
    entry = make_entry(source_url=server.url("/app.tar.gz"))
    task = make_task(entry, tmp_path)

    batch = run_download([task])

    assert batch.failures == {}
    result = batch.results[entry.key]
    assert result.hashes == abc_hashes()
    assert result.size == 3
    assert task.staging_path.read_bytes() == b"abc"


def test_retry_500_then_success(server, tmp_path, no_sleep):
    attempts = {"count": 0}

    def route(method, parsed, headers):
        attempts["count"] += 1
        if attempts["count"] < 3:
            return Response(status=500, body=b"boom")
        return Response(body=b"abc")

    server.route("/retry", route)
    entry = replace(make_entry(source_url=server.url("/retry")), retries=2)
    batch = run_download([make_task(entry, tmp_path)])

    assert batch.failures == {}
    assert attempts["count"] == 3
    assert len(no_sleep) == 2


def test_404_is_not_retried(server, tmp_path, no_sleep):
    server.route("/missing", Response(status=404, body=b"nope"))
    entry = replace(make_entry(source_url=server.url("/missing")), retries=3)
    task = make_task(entry, tmp_path)

    batch = run_download([task])

    assert entry.key in batch.failures
    assert "HTTP 404" in batch.failures[entry.key]
    assert server.count("/missing") == 1
    assert no_sleep == []
    assert not task.staging_path.exists()
    assert not task.staging_path.with_name(task.staging_path.name + ".part").exists()


def test_redirect_is_followed(server, tmp_path):
    server.route(
        "/redir",
        Response(status=302, headers={"Location": server.url("/file")}),
    )
    server.route("/file", Response(body=b"abc"))
    entry = make_entry(source_url=server.url("/redir"))
    batch = run_download([make_task(entry, tmp_path)])

    assert batch.failures == {}
    assert batch.results[entry.key].final_url == server.url("/file")
    assert batch.results[entry.key].hashes == abc_hashes()


def test_content_length_mismatch_retries(server, tmp_path, no_sleep):
    server.route(
        "/short",
        Response(
            body=b"abc",
            headers={"Content-Length": "5", "Connection": "close"},
        ),
    )
    entry = replace(make_entry(source_url=server.url("/short")), retries=1)
    task = make_task(entry, tmp_path)

    batch = run_download([task])

    assert entry.key in batch.failures
    assert server.count("/short") == 2
    assert len(no_sleep) == 1
    assert not task.staging_path.exists()


def test_duplicate_url_and_filename_downloads_once(server, tmp_path):
    server.route("/shared.tar.gz", Response(body=b"abc"))
    url = server.url("/shared.tar.gz")
    first = make_entry(target="one", arch="x86_64-linux", source_url=url, filename="shared.tar.gz")
    second = make_entry(target="two", arch="aarch64-linux", source_url=url, filename="shared.tar.gz")
    first_task = make_task(first, tmp_path)
    second_task = make_task(second, tmp_path)

    batch = run_download([first_task, second_task])

    assert batch.failures == {}
    assert server.count("/shared.tar.gz") == 1
    assert set(batch.results) == {first.key, second.key}
    assert first_task.staging_path.read_bytes() == b"abc"
    assert second_task.staging_path.read_bytes() == b"abc"


def test_failed_download_cleans_staging(server, tmp_path):
    server.route("/broken", Response(status=503, body=b"nope"))
    entry = make_entry(source_url=server.url("/broken"))
    task = make_task(entry, tmp_path)

    batch = run_download([task])

    assert entry.key in batch.failures
    assert not task.staging_path.exists()
    assert not task.staging_path.parent.exists() or not any(
        task.staging_path.parent.iterdir()
    )


def test_shared_url_with_different_effective_headers_keeps_distinct_bytes(server, tmp_path):
    def route(method, parsed, headers):
        return Response(body=headers["X-Variant"].encode("ascii"))

    server.route("/variant", route)
    url = server.url("/variant")
    first = replace(
        make_entry(target="one", source_url=url, filename="shared.bin"),
        headers={"X-Variant": "one"},
    )
    second = replace(
        make_entry(target="two", source_url=url, filename="shared.bin"),
        download_headers={"x-variant": "two"},
    )
    first_task = make_task(first, tmp_path)
    second_task = make_task(second, tmp_path)

    batch = run_download([first_task, second_task], concurrency=1)

    assert batch.failures == {}
    assert server.count("/variant") == 2
    assert first_task.staging_path.read_bytes() == b"one"
    assert second_task.staging_path.read_bytes() == b"two"
    assert batch.results[first.key].hashes != batch.results[second.key].hashes


def test_equivalent_case_insensitive_effective_headers_share_download(server, tmp_path):
    def route(method, parsed, headers):
        return Response(body=headers["X-Variant"].encode("ascii"))

    server.route("/equivalent", route)
    url = server.url("/equivalent")
    first = replace(
        make_entry(target="one", source_url=url, filename="shared.bin"),
        headers={"X-Variant": "chosen", "X-Other": "value"},
    )
    second = replace(
        make_entry(target="two", source_url=url, filename="shared.bin"),
        headers={"x-other": "value", "x-variant": "overridden"},
        download_headers={"X-VARIANT": "chosen"},
    )
    first_task = make_task(first, tmp_path)
    second_task = make_task(second, tmp_path)

    batch = run_download([first_task, second_task], concurrency=1)

    assert batch.failures == {}
    assert server.count("/equivalent") == 1
    assert first_task.staging_path.read_bytes() == b"chosen"
    assert second_task.staging_path.read_bytes() == b"chosen"


def test_download_header_layers_override_case_and_force_identity(server, tmp_path):
    received = {}

    def route(method, parsed, headers):
        received["authorization"] = headers.get_all("Authorization")
        received["accept-encoding"] = headers.get_all("Accept-Encoding")
        return Response(body=b"abc")

    server.route("/headers", route)
    entry = replace(
        make_entry(source_url=server.url("/headers")),
        headers={"Authorization": "Bearer common", "ACCEPT-ENCODING": "gzip"},
        download_headers={
            "authorization": "Bearer download",
            "accept-encoding": "deflate",
        },
    )

    batch = run_download([make_task(entry, tmp_path)])

    assert batch.failures == {}
    assert received == {
        "authorization": ["Bearer download"],
        "accept-encoding": ["identity"],
    }


def test_shared_url_with_different_retry_policies_isolated(server, tmp_path, no_sleep):
    attempts = {"count": 0}

    def route(method, parsed, headers):
        attempts["count"] += 1
        if attempts["count"] <= 2:
            return Response(status=503, body=b"retry")
        return Response(body=b"abc")

    server.route("/retry-policies", route)
    url = server.url("/retry-policies")
    first = make_entry(target="one", source_url=url, filename="shared.bin")
    second = replace(
        make_entry(target="two", source_url=url, filename="shared.bin"), retries=1
    )
    first_task = make_task(first, tmp_path)
    second_task = make_task(second, tmp_path)

    batch = run_download([first_task, second_task], concurrency=1)

    assert set(batch.failures) == {first.key}
    assert set(batch.results) == {second.key}
    assert server.count("/retry-policies") == 3
    assert len(no_sleep) == 1
    assert not first_task.staging_path.exists()
    assert second_task.staging_path.read_bytes() == b"abc"


@pytest.mark.parametrize("credential", ["Bearer PRIVATE_MARKER\n", "Bearer PRIVATE_MARKERé"])
def test_invalid_outgoing_header_fails_safely_without_aborting_other_downloads(
    server, tmp_path, credential
):
    server.route("/safe", Response(body=b"abc"))
    bad = replace(
        make_entry(target="bad", source_url=server.url("/safe")),
        headers={"Authorization": credential},
    )
    good = make_entry(target="good", source_url=server.url("/safe"))
    bad_task = make_task(bad, tmp_path)
    good_task = make_task(good, tmp_path)

    batch = run_download([bad_task, good_task], concurrency=1)

    assert set(batch.failures) == {bad.key}
    assert "PRIVATE_MARKER" not in batch.failures[bad.key]
    assert set(batch.results) == {good.key}
    assert good_task.staging_path.read_bytes() == b"abc"
    assert not bad_task.staging_path.exists()
    assert not any(bad_task.staging_path.parent.iterdir())


def test_download_closes_redirect_body_without_reading(tmp_path):
    closed = []

    class RedirectBody(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError("redirect body must not be read")

        def close(self):
            closed.append(True)

    def handle(request):
        if request.url.path == "/redirect":
            return httpx.Response(
                302,
                headers={"Location": "/file"},
                stream=RedirectBody(),
            )
        return httpx.Response(200, stream=httpx.ByteStream(b"abc"))

    entry = make_entry(source_url="https://download.test/redirect")
    task = make_task(entry, tmp_path)
    settings = Settings(concurrency=1)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        batch = Downloader(client, settings).run([task])

    assert batch.failures == {}
    assert closed == [True]
    assert task.staging_path.read_bytes() == b"abc"
    assert batch.results[entry.key].final_url == "https://download.test/file"


@pytest.mark.parametrize(
    "coding,body",
    [
        ("gzip", gzip.compress(b"abc")),
        ("deflate", zlib.compress(b"abc")),
        ("gzip, deflate", zlib.compress(gzip.compress(b"abc"))),
    ],
)
def test_download_hashes_decoded_compressed_bytes(server, tmp_path, coding, body):
    server.route("/compressed", Response(body=body, headers={"Content-Encoding": coding}))
    entry = make_entry(source_url=server.url("/compressed"))
    task = make_task(entry, tmp_path)

    batch = run_download([task])

    assert batch.failures == {}
    assert task.staging_path.read_bytes() == b"abc"
    assert batch.results[entry.key].hashes == abc_hashes()
    assert batch.results[entry.key].size == 3


@pytest.mark.parametrize(
    "coding,body",
    [
        ("gzip", gzip.compress(b"abc")[:-1]),
        ("deflate", zlib.compress(b"abc")[:-1]),
        ("gzip", b"PRIVATE_BODY_MARKER"),
        ("unknown-PRIVATE_HEADER_MARKER", b"PRIVATE_BODY_MARKER"),
    ],
)
def test_invalid_compressed_download_fails_safely_and_removes_partial(
    server, tmp_path, coding, body
):
    server.route("/invalid", Response(body=body, headers={"Content-Encoding": coding}))
    entry = make_entry(source_url=server.url("/invalid"))
    task = make_task(entry, tmp_path)

    batch = run_download([task])

    assert set(batch.failures) == {entry.key}
    assert "PRIVATE_BODY_MARKER" not in batch.failures[entry.key]
    assert "PRIVATE_HEADER_MARKER" not in batch.failures[entry.key]
    assert not task.staging_path.exists()
    assert not any(task.staging_path.parent.iterdir())


@pytest.mark.parametrize("filename", ["a" * 255, "é" * 127 + "a"])
def test_maximum_local_filename_uses_short_partial_name(server, tmp_path, filename):
    server.route("/long-name", Response(body=b"abc"))
    entry = make_entry(source_url=server.url("/long-name"), filename=filename)
    task = make_task(entry, tmp_path)

    batch = run_download([task])

    assert batch.failures == {}
    assert task.staging_path.read_bytes() == b"abc"
    assert set(task.staging_path.parent.iterdir()) == {task.staging_path}


def test_maximum_local_filename_cleans_short_partial_after_stream_failure(server, tmp_path):
    server.route(
        "/long-name-short-body",
        Response(
            body=b"abc",
            headers={"Content-Length": "5", "Connection": "close"},
        ),
    )
    entry = make_entry(
        source_url=server.url("/long-name-short-body"),
        filename="a" * 255,
    )
    task = make_task(entry, tmp_path)

    batch = run_download([task])

    assert set(batch.failures) == {entry.key}
    assert not task.staging_path.exists()
    assert not any(task.staging_path.parent.iterdir())
