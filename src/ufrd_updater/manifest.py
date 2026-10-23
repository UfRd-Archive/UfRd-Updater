"""Baseline manifest/state handling and change detection."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .config import ConfigError
from .hashes import hashes_equal, hashes_valid
from .model import Decision, ResolvedEntry

MANIFEST_NAME = "manifest.json"
STATE_NAME = "state.json"
RELEASES_NAME = "releases.json"
URLS_NAME = "urls.txt"
UPDATE_NAME = "update.md"
SOURCES_NAME = "sources.nix"
FETCHURL_NAME = "fetchurl.nix"

Manifest = dict[str, dict[str, dict[str, Any]]]
StateEntries = dict[str, dict[str, str]]


def load_manifest(output_dir: Path) -> Manifest:
    """Load structural metadata while retaining corrupt hashes for explicit repair."""

    path = output_dir / MANIFEST_NAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"invalid {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"invalid {path}: top level must be an object")
    manifest: Manifest = {}
    for target, arches in data.items():
        if not isinstance(target, str) or not isinstance(arches, dict):
            raise ConfigError(f"invalid {path}: expected target objects")
        manifest[target] = {}
        for arch, entry in arches.items():
            if not isinstance(arch, str) or not isinstance(entry, dict):
                raise ConfigError(
                    f"invalid {path}: expected arch objects under '{target}'"
                )
            _validate_manifest_entry(entry, f"{path}: {target}/{arch}")
            manifest[target][arch] = entry
    return manifest


def _validate_manifest_entry(entry: dict[str, Any], label: str) -> None:
    for field in ("version", "url", "filename"):
        if not isinstance(entry.get(field), str):
            raise ConfigError(f"{label}: '{field}' must be a string")


def invalid_hash_entries(manifest: Manifest) -> set[str]:
    """Return entry keys whose hashes need repair before any export."""

    return {
        f"{target}/{arch}"
        for target, arches in manifest.items()
        for arch, entry in arches.items()
        if not hashes_valid(entry.get("hashes"))
    }


def load_state(output_dir: Path) -> StateEntries:
    path = output_dir / STATE_NAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"invalid {path}: {exc}") from exc
    if (
        not isinstance(data, dict)
        or isinstance(data.get("version"), bool)
        or not isinstance(data.get("version"), int)
        or data.get("version") != 1
    ):
        raise ConfigError(f"invalid {path}: expected version 1")
    entries = data.get("entries")
    if not isinstance(entries, dict):
        raise ConfigError(f"invalid {path}: 'entries' must be an object")
    normalized: StateEntries = {}
    for key, record in entries.items():
        if not isinstance(key, str) or not isinstance(record, dict):
            raise ConfigError(f"invalid {path}: expected entry objects")
        source = record.get("source_url_sha256")
        if not isinstance(source, str):
            raise ConfigError(f"invalid {path}: {key}: source_url_sha256 must be a string")
        archive_url = record.get("archive_url", "")
        archive_repository = record.get("archive_repository", "")
        if not isinstance(archive_url, str) or not isinstance(archive_repository, str):
            raise ConfigError(f"invalid {path}: {key}: archive fields must be strings")
        normalized[key] = {
            "source_url_sha256": source,
            "archive_url": archive_url,
            "archive_repository": archive_repository,
        }
    return normalized


def state_text(entries: StateEntries) -> str:
    document = {
        "version": 1,
        "entries": {
            key: {
                "source_url_sha256": entries[key]["source_url_sha256"],
                "archive_url": entries[key].get("archive_url", ""),
                "archive_repository": entries[key].get("archive_repository", ""),
            }
            for key in sorted(entries)
        },
    }
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def baseline_entry(manifest: Manifest, key: str) -> dict[str, Any] | None:
    target, arch = key.split("/", 1)
    return manifest.get(target, {}).get(arch)


def decide(
    entry: ResolvedEntry,
    baseline: dict[str, Any] | None,
    state: dict[str, str] | None,
    *,
    archive_enabled: bool,
    repository: str,
    force: bool,
) -> Decision:
    """Apply the pre-download half of the change-detection rules."""

    archive_bootstrap = bool(
        archive_enabled
        and baseline is not None
        and (
            state is None
            or not state.get("archive_url")
            or state.get("archive_url") != baseline.get("url")
            or state.get("archive_repository", "") != repository
        )
    )

    if force:
        return Decision(entry, baseline, state, archive_bootstrap, "forced", True)
    if baseline is None:
        return Decision(entry, baseline, state, archive_bootstrap, "new", True)
    if baseline["version"] != entry.version or baseline["filename"] != entry.filename:
        return Decision(entry, baseline, state, archive_bootstrap, "version", True)

    definite_kind = "archive" if archive_bootstrap else None
    need_download = definite_kind is not None
    if state is not None and state["source_url_sha256"] != sha256_text(entry.source_url):
        need_download = True
    return Decision(entry, baseline, state, archive_bootstrap, definite_kind, need_download)


def classify(
    decision: Decision,
    hashes: dict[str, str],
    *,
    archive_enabled: bool,
    repository: str,
) -> Decision:
    """Apply the post-download half of the change-detection rules."""

    baseline = decision.baseline
    if decision.definite_kind in ("forced", "new", "version"):
        decision.changed = True
        decision.kind = decision.definite_kind
        return decision
    if decision.archive_bootstrap:
        decision.changed = True
        decision.kind = "archive"
        return decision
    archived = bool(
        not archive_enabled
        or (
            decision.state is not None
            and decision.state.get("archive_url") == (baseline or {}).get("url")
            and decision.state.get("archive_repository", "") == repository
        )
    )
    if baseline is not None and hashes_equal(baseline["hashes"], hashes) and archived:
        decision.changed = False
        decision.kind = None
        return decision
    decision.changed = True
    decision.kind = "content"
    return decision


def unchanged_state(decision: Decision, *, source_url: str) -> dict[str, str]:
    state = decision.state or {}
    return {
        "source_url_sha256": sha256_text(source_url),
        "archive_url": state.get("archive_url", ""),
        "archive_repository": state.get("archive_repository", ""),
    }


def changed_state(
    source_url: str,
    archive_url: str | None,
    repository: str | None,
) -> dict[str, str]:
    return {
        "source_url_sha256": sha256_text(source_url),
        "archive_url": archive_url or "",
        "archive_repository": repository or "",
    }


def make_entry(
    *,
    version: str,
    url: str,
    filename: str,
    hashes: dict[str, str],
) -> dict[str, Any]:
    return {
        "version": version,
        "url": url,
        "filename": filename,
        "hashes": dict(hashes),
    }


def merge_manifest(
    baseline: Manifest,
    updates: dict[str, dict[str, Any]],
) -> Manifest:
    """Return a nested manifest with updates applied; unrelated data is kept."""

    merged: Manifest = {}
    for target, arches in baseline.items():
        merged[target] = {arch: dict(entry) for arch, entry in arches.items()}
    for key, entry in updates.items():
        target, arch = key.split("/", 1)
        merged.setdefault(target, {})[arch] = entry
    return merged


def prune(
    manifest: Manifest,
    entries: StateEntries,
    keep_keys: set[str],
) -> tuple[Manifest, StateEntries]:
    manifest_out: Manifest = {}
    for target, arches in manifest.items():
        kept = {
            arch: entry
            for arch, entry in arches.items()
            if f"{target}/{arch}" in keep_keys
        }
        if kept:
            manifest_out[target] = kept
    state_out = {key: value for key, value in entries.items() if key in keep_keys}
    return manifest_out, state_out
