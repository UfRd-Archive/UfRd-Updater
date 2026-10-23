"""Release tags, grouping, archive URLs and release-plan documents."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx

from .config import ReleaseConfig
from .model import Decision, RunContext
from .template import TemplateError, render

LOG = logging.getLogger("ufrd-updater")

TAG_ILLEGAL = re.compile(r"[^A-Za-z0-9._-]")
TAG_MAX_LENGTH = 200
ASSET_ILLEGAL = re.compile(r"[^A-Za-z0-9._-]+")
ASSET_MAX_LENGTH = 200


class ReleaseError(Exception):
    """Raised for tag, filename or archive URL problems."""


@dataclass
class TaggedEntry:
    decision: Decision
    raw_tag: str
    tag: str
    archive_url: str | None

    @property
    def entry(self):
        return self.decision.entry

    @property
    def asset_name(self) -> str:
        return safe_asset_name(self.entry.filename)

    @property
    def hashes(self) -> dict[str, str]:
        assert self.decision.result is not None
        return self.decision.result.hashes


def safe_asset_name(filename: str) -> str:
    """Map a local filename to a stable, literal GitHub upload name."""

    if (
        filename
        and len(filename) <= ASSET_MAX_LENGTH
        and ASSET_ILLEGAL.search(filename) is None
        and not filename.startswith(".")
        and not filename.endswith(".")
    ):
        return filename
    digest = hashlib.sha256(filename.encode("utf-8", errors="surrogatepass")).hexdigest()[:12]
    suffix = f"-{digest}"
    stem = ASSET_ILLEGAL.sub("-", filename).strip(".-")
    stem = stem[: ASSET_MAX_LENGTH - len(suffix)].rstrip(".-") or "asset"
    return stem + suffix


def sanitize_tag(raw: str) -> str:
    """Make a rendered tag safe for a git ref and a release name."""

    tag = TAG_ILLEGAL.sub("-", raw)
    tag = re.sub(r"-+", "-", tag)
    while ".." in tag:
        tag = tag.replace("..", ".")
    tag = tag.strip(".-")
    tag = tag[:TAG_MAX_LENGTH].strip(".-")
    if tag.endswith(".lock"):
        tag = tag[: -len(".lock")] + "-lock"
    return tag


def tag_scope(run: RunContext, decision: Decision, repository: str) -> dict[str, str]:
    entry = decision.entry
    scope = run.variables()
    scope.update(
        {
            "repository": repository,
            "id": entry.target,
            "arch": entry.arch,
            "version": entry.version,
            "filename": entry.filename,
        }
    )
    return scope


def plan_tags(
    decisions: list[Decision],
    *,
    release_config: ReleaseConfig,
    targets_by_id: dict,
    run: RunContext,
    repository: str,
    archive_url_template: str,
    environ: dict[str, str] | None = None,
) -> dict[str, TaggedEntry]:
    """Render and sanitize tags for the given (changed) decisions."""

    tagged: dict[str, TaggedEntry] = {}
    for decision in decisions:
        target = targets_by_id[decision.entry.target]
        template = target.tag_template or release_config.tag_template
        scope = tag_scope(run, decision, repository)
        try:
            raw = render(
                template,
                scope,
                context=f"tag template for {decision.key}",
                environ=environ,
            )
        except TemplateError as exc:
            raise ReleaseError(str(exc)) from exc
        tag = sanitize_tag(raw)
        if not tag:
            raise ReleaseError(
                f"tag template for {decision.key} produced an empty tag"
            )
        if tag != raw:
            LOG.warning("sanitized release tag for %s", decision.key)
        archive_url = None
        if repository:
            archive_url = render_archive_url(
                decision.entry.filename,
                tag,
                repository,
                template=archive_url_template,
                environ=environ,
            )
        tagged[decision.key] = TaggedEntry(
            decision=decision,
            raw_tag=raw,
            tag=tag,
            archive_url=archive_url,
        )
    return tagged


def render_archive_url(
    filename: str,
    tag: str,
    repository: str,
    *,
    template: str,
    environ: dict[str, str] | None = None,
) -> str:
    variables = {
        "repository": repository,
        "tag": tag,
        "filename": quote(filename, safe=""),
    }
    try:
        parsed = httpx.URL(render(template, variables, context="archive URL", environ=environ))
        if parsed.host == "github.com":
            variables["filename"] = safe_asset_name(filename)
            parsed = httpx.URL(
                render(template, variables, context="archive URL", environ=environ)
            )
    except TemplateError as exc:
        raise ReleaseError(str(exc)) from exc
    except (httpx.InvalidURL, TypeError, ValueError) as exc:
        raise ReleaseError("archive URL template produced an invalid URL") from exc
    if parsed.scheme not in ("http", "https"):
        raise ReleaseError("archive URL must use http or https")
    if not parsed.host:
        raise ReleaseError("archive URL must include a host")
    return str(parsed)


def validate_tags(
    tagged: list[TaggedEntry],
    *,
    pre_download: bool,
) -> None:
    """Validate rendered tags and, after download, mapped asset identities."""

    raw_by_tag: dict[str, set[str]] = {}
    for item in tagged:
        raw_by_tag.setdefault(item.tag, set()).add(item.raw_tag)
    for raw_values in raw_by_tag.values():
        if len(raw_values) > 1:
            raise ReleaseError(
                "different rendered tags sanitize to the same release tag; "
                "change the tag templates to avoid collisions"
            )

    if pre_download:
        return
    by_group: dict[tuple[str, str], list[TaggedEntry]] = {}
    for item in tagged:
        by_group.setdefault((item.tag, item.asset_name), []).append(item)
    for items in by_group.values():
        if len(items) < 2:
            continue
        names = ", ".join(f"{item.entry.target}/{item.entry.arch}" for item in items)
        hashes = [item.hashes for item in items]
        if any(value != hashes[0] for value in hashes[1:]):
            raise ReleaseError(
                f"a release has multiple different assets with the same upload name ({names}); "
                "give each target/arch a distinct filename, for example "
                "{id}-{arch}-{filename}"
            )


def dedupe_assets(tagged: list[TaggedEntry]) -> list[TaggedEntry]:
    """Drop duplicate tag+upload-name assets whose full hashes match."""

    seen: dict[tuple[str, str], dict[str, str]] = {}
    output: list[TaggedEntry] = []
    for item in sorted(tagged, key=lambda value: (value.entry.target, value.entry.arch)):
        key = (item.tag, item.asset_name)
        hashes = item.hashes
        if key in seen:
            if seen[key] == hashes:
                continue
            raise ReleaseError(
                "a release has different assets with the same upload name"
            )
        seen[key] = hashes
        output.append(item)
    return output


def final_path(output_dir: Path, decision: Decision) -> Path:
    entry = decision.entry
    return output_dir.absolute() / "files" / entry.target / entry.arch / entry.filename


def build_release_document(
    tagged: list[TaggedEntry],
    *,
    run: RunContext,
    repository: str,
    archive_enabled: bool,
    output_dir: Path,
) -> dict:
    groups: dict[str, list[TaggedEntry]] = {}
    for item in tagged:
        groups.setdefault(item.tag, []).append(item)

    releases = []
    for tag in sorted(groups):
        items = sorted(
            dedupe_assets(groups[tag]),
            key=lambda value: (value.entry.target, value.entry.arch),
        )
        assets = []
        for item in items:
            entry = item.entry
            hashes = item.hashes
            assets.append(
                {
                    "target": entry.target,
                    "arch": entry.arch,
                    "version": entry.version,
                    "filename": entry.filename,
                    "asset_name": item.asset_name,
                    "path": str(final_path(output_dir, item.decision)),
                    "url": item.archive_url,
                    "sha256": hashes["sha256"],
                    "sha256_sri": hashes["sha256-sri"],
                }
            )
        releases.append({"tag": tag, "assets": assets})

    tags = sorted(groups)
    document = {
        "version": 1,
        "run": {
            "datetime": run.iso_datetime(),
            "tag": tags[0] if len(tags) == 1 else None,
            "tags": tags,
            "archive": archive_enabled,
            "repository": repository,
        },
        "releases": releases,
    }
    return document


def releases_text(document: dict) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def urls_text(tagged: dict[str, TaggedEntry], *, archive_enabled: bool) -> str:
    urls = []
    for key in sorted(tagged):
        item = tagged[key]
        if archive_enabled:
            if item.archive_url is None:
                raise ReleaseError(
                    f"archive is enabled but no archive URL is known for {key}"
                )
            urls.append(item.archive_url)
        else:
            urls.append(item.entry.source_url)
    return "".join(f"{url}\n" for url in urls)


def files_text(tagged: dict[str, TaggedEntry], output_dir: Path) -> str:
    paths = sorted(
        {
            str(final_path(output_dir, item.decision))
            for item in tagged.values()
        }
    )
    return "".join(f"{path}\n" for path in paths)


