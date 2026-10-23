"""Concurrent downloads with staging, deduplication and one-pass hashing."""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import http
from .config import Settings
from .hashes import hashes_from_hashers, new_hashers
from .model import DownloadResult, Failure, ResolvedEntry


class DownloadError(Exception):
    """Raised when a download cannot be completed."""


@dataclass(frozen=True)
class DownloadTask:
    entry: ResolvedEntry
    staging_path: Path


@dataclass
class DownloadBatch:
    results: dict[str, DownloadResult]
    failures: dict[str, str]

    @property
    def failure_records(self) -> list[Failure]:
        records: list[Failure] = []
        for key, error in sorted(self.failures.items()):
            target, arch = key.split("/", 1)
            records.append(Failure(target=target, arch=arch, error=error))
        return records


class _Retry(Exception):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class Downloader:
    def __init__(self, client: httpx.Client, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    def run(self, tasks: Iterable[DownloadTask]) -> DownloadBatch:
        tasks = list(tasks)
        groups: dict[
            tuple[str, str, tuple[tuple[str, str], ...], int], list[DownloadTask]
        ] = {}
        for task in tasks:
            headers = http.merge_headers(
                task.entry.headers,
                task.entry.download_headers,
                {"Accept-Encoding": "identity"},
            )
            identity = (
                task.entry.source_url,
                task.entry.filename,
                tuple(sorted(headers.items())),
                task.entry.retries,
            )
            groups.setdefault(identity, []).append(task)

        results: dict[str, DownloadResult] = {}
        failures: dict[str, str] = {}
        workers = max(1, min(self.settings.concurrency, len(groups) or 1))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._download_group, group, dict(identity[2])): group
                for identity, group in groups.items()
            }
            for future, group in futures.items():
                try:
                    group_results, group_failures = future.result()
                except Exception as exc:  # defensive: a worker bug must not kill the run
                    error = (
                        "download failed unexpectedly: "
                        f"{http.safe_exception(exc, group[0].entry.source_url)}"
                    )
                    for task in group:
                        failures[task.entry.key] = error
                    continue
                results.update(group_results)
                failures.update(group_failures)
        return DownloadBatch(results=results, failures=failures)

    def _download_group(
        self,
        group: list[DownloadTask],
        headers: dict[str, str],
    ) -> tuple[dict[str, DownloadResult], dict[str, str]]:
        primary = group[0]
        results: dict[str, DownloadResult] = {}
        failures: dict[str, str] = {}
        try:
            result = self._download_one(primary.entry, primary.staging_path, headers)
        except Exception as exc:
            message = (
                str(exc)
                if isinstance(exc, DownloadError)
                else (
                    f"GET {http.redact_url(primary.entry.source_url)} failed: "
                    f"{http.safe_exception(exc, primary.entry.source_url)}"
                )
            )
            for task in group:
                failures[task.entry.key] = message
            return results, failures

        results[primary.entry.key] = result
        for task in group[1:]:
            try:
                self._materialize(primary.staging_path, task.staging_path)
            except OSError as exc:
                failures[task.entry.key] = (
                    f"could not stage duplicate download at {task.staging_path}: "
                    f"{http.safe_exception(exc, task.entry.source_url)}"
                )
                continue
            results[task.entry.key] = result
        return results, failures

    def _download_one(
        self, entry: ResolvedEntry, path: Path, headers: dict[str, str]
    ) -> DownloadResult:
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(f".ufrd-{uuid.uuid4().hex}.part")
        timeout = http.download_timeout(self.settings)
        attempts = entry.retries + 1
        last_error: str | None = None

        for attempt in range(attempts):
            retry_after: float | None = None
            final_url = entry.source_url
            hashers = new_hashers()
            written = 0
            try:
                with http.stream_response(
                    self.client,
                    "GET",
                    entry.source_url,
                    headers=headers,
                    follow_redirects=True,
                    timeout=timeout,
                ) as response:
                    final_url = str(response.url)
                    status = response.status_code
                    if http.is_retryable_status(status):
                        if attempt < attempts - 1:
                            retry_after = http.parse_retry_after(
                                response.headers.get("retry-after")
                            )
                            raise _Retry(
                                f"GET {http.redact_url(entry.source_url)} returned HTTP {status}",
                                retry_after,
                            )
                        raise DownloadError(
                            f"GET {http.redact_url(entry.source_url)} returned HTTP {status}"
                        )
                    if not 200 <= status < 300:
                        raise DownloadError(
                            f"GET {http.redact_url(entry.source_url)} returned HTTP {status}"
                        )
                    expected_length = _content_length(response)
                    with open(partial, "wb") as handle:
                        for chunk in http.iter_response_bytes(response):
                            handle.write(chunk)
                            for hasher in hashers.values():
                                hasher.update(chunk)
                            written += len(chunk)
                if expected_length is not None and written != expected_length:
                    raise _Retry(
                        f"Content-Length mismatch for {http.redact_url(entry.source_url)}: "
                        f"expected {expected_length}, received {written}"
                    )
                os.replace(partial, path)
                return DownloadResult(
                    hashes=hashes_from_hashers(hashers),
                    size=written,
                    final_url=final_url,
                )
            except _Retry as exc:
                last_error = str(exc)
                _cleanup(partial, path)
                if attempt >= attempts - 1:
                    break
                http.sleep(http.retry_delay(attempt, exc.retry_after))
            except httpx.TransportError as exc:
                last_error = (
                    f"GET {http.redact_url(entry.source_url)} failed: "
                    f"{http.safe_exception(exc, entry.source_url)}"
                )
                _cleanup(partial, path)
                if attempt >= attempts - 1:
                    break
                http.sleep(http.retry_delay(attempt))
            except (DownloadError, http.RequestError, OSError) as exc:
                _cleanup(partial, path)
                if isinstance(exc, DownloadError):
                    raise
                if isinstance(exc, http.RequestError):
                    raise DownloadError(str(exc)) from exc
                raise DownloadError(
                    f"could not write {path}: {http.safe_exception(exc, entry.source_url)}"
                ) from exc
            except Exception as exc:
                _cleanup(partial, path)
                raise DownloadError(
                    f"GET {http.redact_url(entry.source_url)} failed: "
                    f"{http.safe_exception(exc, entry.source_url)}"
                ) from exc

        raise DownloadError(
            last_error
            or f"GET {http.redact_url(entry.source_url)} failed after {attempts} attempt(s)"
        )

    @staticmethod
    def _materialize(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            destination.unlink()
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)


def _content_length(response: httpx.Response) -> int | None:
    encoding = (response.headers.get("content-encoding") or "").strip().lower()
    if encoding and encoding != "identity":
        return None
    value = response.headers.get("content-length")
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _cleanup(*paths: Path) -> None:
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
