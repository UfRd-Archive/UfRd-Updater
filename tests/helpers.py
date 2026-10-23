from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from ufrd_updater.hashes import hashes_from_digests
from ufrd_updater.model import Decision, DownloadResult, ResolvedEntry, RunContext
from ufrd_updater.release import TaggedEntry


def abc_hashes() -> dict[str, str]:
    return hashes_from_digests(
        {
            algorithm: hashlib.new(algorithm, b"abc").digest()
            for algorithm in ("md5", "sha1", "sha256", "sha512")
        }
    )


def make_entry(
    *,
    target: str = "demo",
    arch: str = "x86_64-linux",
    version: str = "1.0",
    source_url: str = "https://src.test/app-1.0.tar.gz",
    filename: str = "app-1.0.tar.gz",
) -> ResolvedEntry:
    return ResolvedEntry(
        target=target,
        arch=arch,
        version=version,
        source_url=source_url,
        filename=filename,
        headers={},
        download_headers={},
        retries=0,
    )


def make_manifest_entry(
    *,
    version: str = "1.0",
    url: str = "https://src.test/app-1.0.tar.gz",
    filename: str = "app-1.0.tar.gz",
    hashes: dict[str, str] | None = None,
) -> dict:
    return {
        "version": version,
        "url": url,
        "filename": filename,
        "hashes": dict(hashes or abc_hashes()),
    }


def make_decision(
    *,
    entry: ResolvedEntry | None = None,
    baseline: dict | None = None,
    state: dict | None = None,
    definite_kind: str | None = "new",
    need_download: bool = True,
    hashes: dict[str, str] | None = None,
    changed: bool = True,
    kind: str | None = "new",
    archive_bootstrap: bool = False,
) -> Decision:
    resolved = entry or make_entry()
    result = DownloadResult(
        hashes=dict(hashes or abc_hashes()),
        size=3,
        final_url=resolved.source_url,
    )
    return Decision(
        entry=resolved,
        baseline=baseline,
        state=state,
        archive_bootstrap=archive_bootstrap,
        definite_kind=definite_kind,
        need_download=need_download,
        result=result,
        changed=changed,
        kind=kind,
    )


def make_tagged(
    *,
    decision: Decision | None = None,
    tag: str = "ufrd-20261023-153012-42",
    raw_tag: str | None = None,
    archive_url: str | None = None,
) -> TaggedEntry:
    chosen = decision or make_decision()
    return TaggedEntry(
        decision=chosen,
        raw_tag=raw_tag if raw_tag is not None else tag,
        tag=tag,
        archive_url=archive_url,
    )


def fixed_run(repository: str = "org/UfRd-Archive") -> RunContext:
    return RunContext(
        datetime=datetime(2026, 10, 23, 15, 30, 12, tzinfo=UTC),
        run_id="42",
        run_number="7",
        sha="abc1234",
        repository=repository,
        environ={},
    )
