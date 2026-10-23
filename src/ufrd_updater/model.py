"""Runtime records shared by resolution, downloads, manifests and releases."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass(frozen=True)
class RunContext:
    """Values captured once when the process starts."""

    datetime: datetime
    run_id: str
    run_number: str
    sha: str
    repository: str
    environ: Mapping[str, str] = field(repr=False, compare=False)

    @classmethod
    def create(cls, environ: Mapping[str, str] | None = None) -> "RunContext":
        env = os.environ if environ is None else environ
        now = datetime.now(UTC).replace(microsecond=0)
        sha = env.get("GITHUB_SHA", "")[:7] or "local"
        return cls(
            datetime=now,
            run_id=env.get("GITHUB_RUN_ID", "") or "local",
            run_number=env.get("GITHUB_RUN_NUMBER", "") or "0",
            sha=sha,
            repository=env.get("GITHUB_REPOSITORY", ""),
            environ=env,
        )

    def variables(self) -> dict[str, str]:
        return {
            "datetime": self.datetime.strftime("%Y%m%d-%H%M%S"),
            "date": self.datetime.strftime("%Y%m%d"),
            "time": self.datetime.strftime("%H%M%S"),
            "run_id": self.run_id,
            "run_number": self.run_number,
            "sha": self.sha,
            "repository": self.repository,
        }

    def iso_datetime(self) -> str:
        return self.datetime.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class ResolvedEntry:
    """A target/arch whose metadata was resolved successfully."""

    target: str
    arch: str
    version: str
    source_url: str
    filename: str
    headers: dict[str, str]
    download_headers: dict[str, str]
    retries: int

    @property
    def key(self) -> str:
        return f"{self.target}/{self.arch}"


@dataclass(frozen=True)
class Failure:
    target: str
    arch: str
    error: str

    def as_dict(self) -> dict[str, str]:
        return {"target": self.target, "arch": self.arch, "error": self.error}


@dataclass(frozen=True)
class DownloadResult:
    hashes: dict[str, str]
    size: int
    final_url: str


@dataclass
class Decision:
    """Change-detection state for one resolved entry."""

    entry: ResolvedEntry
    baseline: dict | None
    state: dict | None
    archive_bootstrap: bool
    definite_kind: str | None
    need_download: bool
    result: DownloadResult | None = None
    changed: bool = False
    kind: str | None = None
    failed: bool = False

    @property
    def key(self) -> str:
        return self.entry.key
