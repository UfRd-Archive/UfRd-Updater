"""Command-line entry point and run orchestration."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import uuid
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from . import __version__, http
from .actions import ActionsError, append_summary, write_outputs
from .config import (
    Arch,
    Config,
    ConfigError,
    Request,
    Target,
    load_config,
    validate_output_paths,
)
from .download import DownloadBatch, Downloader, DownloadTask
from .extract import ExtractContext, ExtractError, run_extractors
from .gitops import CommitResult, GitError, check_preconditions, commit_and_push
from .manifest import (
    FETCHURL_NAME,
    MANIFEST_NAME,
    RELEASES_NAME,
    SOURCES_NAME,
    STATE_NAME,
    UPDATE_NAME,
    URLS_NAME,
    baseline_entry,
    changed_state,
    classify,
    decide,
    invalid_hash_entries,
    load_manifest,
    load_state,
    make_entry,
    merge_manifest,
    prune,
    state_text,
    unchanged_state,
)
from .model import Decision, Failure, ResolvedEntry, RunContext
from .release import (
    ReleaseError,
    build_release_document,
    files_text,
    final_path,
    plan_tags,
    releases_text,
    urls_text,
    safe_asset_name,
    validate_tags,
)
from .render import (
    render_fetchurl_nix,
    render_manifest,
    render_sources_nix,
    render_update_md,
    write_atomic,
)
from .template import TemplateError, render, render_json

LOG = logging.getLogger("ufrd-updater")

EXPORT_NAMES = (MANIFEST_NAME, SOURCES_NAME, FETCHURL_NAME, STATE_NAME)


class ResolutionError(Exception):
    """A metadata resolution failure for one target/arch."""


class OutputError(Exception):
    """A failed output transaction, optionally retaining recovery files."""

    def __init__(self, message: str, *, recovery_dir: Path | None = None) -> None:
        super().__init__(message)
        self.recovery_dir = recovery_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ufrd-updater",
        description="Config-driven artifact updater for UfRd-Archive.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="resolve, download and update")
    run_parser.add_argument("--config", default="updater.toml")
    run_parser.add_argument("--only", default=None, metavar="REGEX")
    run_parser.add_argument("--force", action="store_true")
    run_parser.add_argument("--prune", action="store_true")
    run_parser.add_argument(
        "--on-error", choices=("fail", "continue", "rollback"), default=None
    )
    run_parser.add_argument("--commit", dest="commit", action="store_true", default=None)
    run_parser.add_argument("--no-commit", dest="commit", action="store_false")
    run_parser.add_argument("--push", dest="push", action="store_true", default=None)
    run_parser.add_argument("--no-push", dest="push", action="store_false")
    run_parser.add_argument("--verbose", action="store_true")

    validate_parser = subparsers.add_parser("validate", help="validate configuration offline")
    validate_parser.add_argument("--config", default="updater.toml")

    list_parser = subparsers.add_parser("list", help="list configured target/arch pairs")
    list_parser.add_argument("--config", default="updater.toml")

    return parser


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] in ("-h", "--help", "--version"):
        pass
    elif not raw or raw[0] not in ("run", "validate", "list"):
        raw = ["run", *raw]

    parser = build_parser()
    args = parser.parse_args(raw)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    # httpx logs full request URLs at INFO, including query strings that may
    # carry signed tokens. Our own request logs are redacted; silence theirs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    run_context = RunContext.create()
    try:
        if args.command == "validate":
            return cmd_validate(args)
        if args.command == "list":
            return cmd_list(args)
        return cmd_run(args, run_context)
    except ConfigError as exc:
        print(f"ufrd-updater: config error: {http.redact_text(str(exc))}", file=sys.stderr)
        return 2
    except (GitError, ActionsError, ReleaseError) as exc:
        print(f"ufrd-updater: error: {http.redact_text(str(exc))}", file=sys.stderr)
        return 1


def cmd_validate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    validate_output_paths(config)
    print("config OK")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    for target in config.targets:
        for arch in target.arch:
            print(f"{target.id}/{arch.id}")
    return 0


def _effective_options(config: Config, args: argparse.Namespace) -> tuple[str, bool, str]:
    on_error = args.on_error or config.settings.on_error
    commit = config.git.commit if args.commit is None else bool(args.commit)
    if args.push is True:
        push_mode = "true"
    elif args.push is False:
        push_mode = "false"
    else:
        push_mode = config.git.push
    if not commit and push_mode == "true":
        raise ConfigError(
            "git.push = true requires git.commit = true (or --commit); refusing to push "
            "without committing"
        )
    return on_error, commit, push_mode


def _resolve_metadata(
    config: Config,
    run: RunContext,
    selected: tuple[Target, ...],
    client: httpx.Client,
) -> tuple[list[ResolvedEntry], list[Failure]]:
    entries: list[ResolvedEntry] = []
    failures: list[Failure] = []
    for target in selected:
        target_entries, target_failures = _resolve_target(config, run, target, client)
        entries.extend(target_entries)
        failures.extend(target_failures)
    return entries, failures


def _resolve_target(
    config: Config,
    run: RunContext,
    target: Target,
    client: httpx.Client,
) -> tuple[list[ResolvedEntry], list[Failure]]:
    scope = run.variables()
    scope["id"] = target.id
    last_final_url: str | None = None
    try:
        for name, template in target.vars.items():
            scope[name] = render(
                template,
                scope,
                context=f"target '{target.id}' vars.{name}",
                environ=run.environ,
            )
        for request in target.requests:
            last_final_url, scope = _execute_request(
                config,
                run,
                target,
                request,
                scope,
                extra_headers={},
                client=client,
            )
    except (TemplateError, ResolutionError, http.RequestError, ExtractError) as exc:
        message = f"target request failed: {exc}"
        return [], [Failure(target.id, arch.id, message) for arch in target.arch]

    entries: list[ResolvedEntry] = []
    failures: list[Failure] = []
    for arch in target.arch:
        try:
            entries.append(
                _resolve_arch(config, run, target, arch, scope, last_final_url, client)
            )
        except (TemplateError, ResolutionError, http.RequestError, ExtractError) as exc:
            failures.append(Failure(target.id, arch.id, str(exc)))
    return entries, failures


def _execute_request(
    config: Config,
    run: RunContext,
    target: Target,
    request: Request,
    scope: dict,
    *,
    extra_headers: dict[str, str],
    client: httpx.Client,
) -> tuple[str, dict]:
    label = f"request '{request.id}'" if request.id else f"request {request.method}"
    url = render(
        request.url,
        scope,
        context=f"target '{target.id}' {label} URL",
        environ=run.environ,
    )
    normalized = _normalize_url(url, f"target '{target.id}' {label} URL")

    headers: dict[str, str] = {}
    for source in (target.headers, extra_headers, request.headers):
        rendered_headers = {
            name: render(
                template,
                scope,
                context=f"header '{name}' for target '{target.id}'",
                environ=run.environ,
            )
            for name, template in source.items()
        }
        headers = http.merge_headers(headers, rendered_headers)

    content: str | None = None
    json_body = None
    if request.body is not None:
        content = render(
            request.body,
            scope,
            context=f"target '{target.id}' {label} body",
            environ=run.environ,
        )
    if request.json is not None:
        json_body = render_json(
            request.json,
            scope,
            context=f"target '{target.id}' {label} json",
            environ=run.environ,
        )

    LOG.debug("%s %s", request.method, http.redact_url(normalized))
    response = http.request(
        client,
        method=request.method,
        url=normalized,
        headers=headers,
        content=content,
        json_body=json_body,
        follow_redirects=request.follow_redirects,
        expect_status=request.expect_status,
        timeout=http.metadata_timeout(config.settings, target.timeout),
        retries=target.retries if target.retries is not None else config.settings.retries,
    )
    context = ExtractContext(
        body=response.content,
        url=response.url,
        status=response.status,
        headers=response.headers,
        encoding=request.encoding,
        variables=scope,
        cwd=config.base_dir,
        default_timeout=config.settings.timeout,
        environ=run.environ,
    )
    new_scope = run_extractors(request.extract, context)
    return response.url, new_scope


def _resolve_arch(
    config: Config,
    run: RunContext,
    target: Target,
    arch: Arch,
    base_scope: dict,
    last_final_url: str | None,
    client: httpx.Client,
) -> ResolvedEntry:
    scope = dict(base_scope)
    scope["arch"] = arch.id
    for name, template in arch.vars.items():
        scope[name] = render(
            template,
            scope,
            context=f"target '{target.id}' arch '{arch.id}' vars.{name}",
            environ=run.environ,
        )

    final_url = last_final_url
    for request in arch.requests:
        final_url, scope = _execute_request(
            config,
            run,
            target,
            request,
            scope,
            extra_headers=arch.headers,
            client=client,
        )

    if arch.version is not None:
        raw_version = render(
            arch.version,
            scope,
            context=f"target '{target.id}' arch '{arch.id}' version",
            environ=run.environ,
        )
    else:
        if "version" not in scope:
            raise ResolutionError(
                f"target '{target.id}' arch '{arch.id}': no version template and no "
                "'version' variable was extracted or configured"
            )
        raw_version = _require_scalar(scope, "version", target.id, arch.id)
    version = raw_version.strip()
    _validate_version(version, target.id, arch.id)
    scope["version"] = version

    rendered_url = render(
        arch.url,
        scope,
        context=f"target '{target.id}' arch '{arch.id}' URL",
        environ=run.environ,
    )
    if final_url is not None:
        try:
            joined = urljoin(final_url, rendered_url)
        except (ValueError, UnicodeError) as exc:
            raise ResolutionError(
                f"target '{target.id}' arch '{arch.id}': invalid download URL"
            ) from exc
    else:
        joined = rendered_url
        if not _is_absolute(joined):
            raise ResolutionError(
                f"target '{target.id}' arch '{arch.id}': download URL is relative "
                "and no request established a base URL"
            )
    resolved_url = _normalize_url(
        joined, f"target '{target.id}' arch '{arch.id}' URL"
    )

    if arch.filename is not None:
        filename = render(
            arch.filename,
            scope,
            context=f"target '{target.id}' arch '{arch.id}' filename",
            environ=run.environ,
        )
    elif "filename" in scope:
        filename = _require_scalar(scope, "filename", target.id, arch.id)
    else:
        path = urlsplit(resolved_url).path
        filename = unquote(path.rsplit("/", 1)[-1])
        if not filename:
            raise ResolutionError(
                f"target '{target.id}' arch '{arch.id}': cannot derive a filename from "
                f"'{http.redact_url(resolved_url)}'; set arch.filename or extract 'filename'"
            )
    filename = _normalize_filename(filename, target.id, arch.id)

    headers: dict[str, str] = {}
    for source in (target.headers, arch.headers):
        rendered_headers = {
            name: render(
                template,
                scope,
                context=f"download header '{name}' for target '{target.id}'",
                environ=run.environ,
            )
            for name, template in source.items()
        }
        headers = http.merge_headers(headers, rendered_headers)
    download_headers: dict[str, str] = {}
    for source in (target.download_headers, arch.download_headers):
        rendered_headers = {
            name: render(
                template,
                scope,
                context=f"download header '{name}' for target '{target.id}'",
                environ=run.environ,
            )
            for name, template in source.items()
        }
        download_headers = http.merge_headers(download_headers, rendered_headers)

    return ResolvedEntry(
        target=target.id,
        arch=arch.id,
        version=version,
        source_url=resolved_url,
        filename=filename,
        headers=headers,
        download_headers=download_headers,
        retries=target.retries if target.retries is not None else config.settings.retries,
    )


def _require_scalar(scope: dict, name: str, target_id: str, arch_id: str) -> str:
    value = scope[name]
    if isinstance(value, str):
        return value
    hint = "use an index or a command extractor" if isinstance(value, list) else "a scalar string is required"
    raise ResolutionError(
        f"target '{target_id}' arch '{arch_id}': variable '{name}' is not a scalar "
        f"({hint}; got {type(value).__name__})"
    )


def _is_absolute(value: str) -> bool:
    try:
        parsed = httpx.URL(value)
        return bool(parsed.scheme and parsed.host)
    except (TypeError, ValueError, UnicodeError, httpx.InvalidURL):
        return False


def _normalize_url(value: str, label: str) -> str:
    try:
        parsed = httpx.URL(value)
    except (TypeError, ValueError, UnicodeError, httpx.InvalidURL) as exc:
        raise ResolutionError(f"{label} is not a valid absolute http(s) URL") from exc
    if parsed.scheme not in ("http", "https") or not parsed.host:
        raise ResolutionError(
            f"{label} must be an absolute http(s) URL, got '{http.redact_url(value)}'"
        )
    return str(parsed)


def _validate_version(version: str, target_id: str, arch_id: str) -> None:
    if not version:
        raise ResolutionError(
            f"target '{target_id}' arch '{arch_id}': version is empty"
        )
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in version):
        raise ResolutionError(
            f"target '{target_id}' arch '{arch_id}': version contains control characters"
        )
    try:
        version.encode("utf-8")
    except UnicodeError as exc:
        raise ResolutionError(
            f"target '{target_id}' arch '{arch_id}': version is not valid Unicode"
        ) from exc


def _normalize_filename(filename: str, target_id: str, arch_id: str) -> str:
    if not filename:
        raise ResolutionError(
            f"target '{target_id}' arch '{arch_id}': filename is empty"
        )
    try:
        filename.encode("utf-8")
        valid_unicode = True
    except UnicodeError:
        valid_unicode = False
    invalid = (
        not valid_unicode
        or filename in (".", "..")
        or len(os.fsencode(filename)) > 255
        or any(
            character in "/\\\x00"
            or ord(character) < 0x20
            or ord(character) == 0x7F
            for character in filename
        )
    )
    if invalid:
        LOG.warning("normalizing unsafe filename for %s/%s", target_id, arch_id)
        return safe_asset_name(filename)
    return filename


def _print_failures(failures: list[Failure]) -> None:
    for failure in failures:
        print(
            f"FAILED {failure.target}/{failure.arch}: {http.redact_text(failure.error)}",
            file=sys.stderr,
        )


def cmd_run(args: argparse.Namespace, run: RunContext) -> int:
    config = load_config(args.config)
    validate_output_paths(config)
    on_error, commit_enabled, push_mode = _effective_options(config, args)

    only = None
    if args.only:
        try:
            only = re.compile(args.only)
        except re.error as exc:
            raise ConfigError(f"--only is not a valid regular expression: {exc}") from exc
    selected = config.selected_targets(only)

    output_dir = config.output_dir
    baseline_manifest = load_manifest(output_dir)
    baseline_state = load_state(output_dir)
    repair_keys = invalid_hash_entries(baseline_manifest)
    if args.prune:
        repair_keys &= config.full_keys()
    selected_keys = {
        f"{target.id}/{arch.id}" for target in selected for arch in target.arch
    }
    unselected_repairs = repair_keys - selected_keys
    if unselected_repairs:
        raise ConfigError(
            "manifest has invalid hashes for unselected entries: "
            + ", ".join(sorted(unselected_repairs))
            + "; select these entries to repair them before exporting"
        )

    export_paths = [output_dir / name for name in EXPORT_NAMES]
    toplevel: str | None = None
    if commit_enabled:
        toplevel = check_preconditions(
            base_dir=config.base_dir,
            paths=export_paths,
            push_mode=push_mode,
            environ=run.environ,
        )

    repository = config.archive.repository or run.repository
    failures: list[Failure] = []
    changed_count = 0
    commit_result = CommitResult(commit=None, pushed=False)
    action_error = False
    update_markdown = ""

    with http.make_client(config.settings) as client:
        entries, metadata_failures = _resolve_metadata(config, run, selected, client)
        failures.extend(metadata_failures)
        if (on_error == "rollback" and failures) or (
            repair_keys - {entry.key for entry in entries}
        ):
            _print_failures(failures)
            return 1

        decisions = []
        for entry in entries:
            decision = decide(
                entry,
                baseline_entry(baseline_manifest, entry.key),
                baseline_state.get(entry.key),
                archive_enabled=config.archive.enabled,
                repository=repository,
                force=bool(args.force) or entry.key in repair_keys,
            )
            decisions.append(decision)

        definite = [decision for decision in decisions if decision.definite_kind]
        if config.archive.enabled and not repository and definite:
            raise ConfigError(
                "archive.enabled requires archive.repository or GITHUB_REPOSITORY; "
                "refusing to run with changed entries that need archive URLs"
            )

        tagged_pre = plan_tags(
            definite,
            release_config=config.release,
            targets_by_id={target.id: target for target in config.targets},
            run=run,
            repository=repository,
            archive_url_template=config.archive.url_template,
            environ=run.environ,
        )
        validate_tags(list(tagged_pre.values()), pre_download=True)

        staging_root = output_dir / ".tmp" / f"downloads-{uuid.uuid4().hex}"
        keep_staging = False
        try:
            tasks = [
                DownloadTask(
                    entry=decision.entry,
                    staging_path=staging_root
                    / decision.entry.target
                    / decision.entry.arch
                    / decision.entry.filename,
                )
                for decision in decisions
                if decision.need_download
            ]
            if tasks:
                batch = Downloader(client, config.settings).run(tasks)
            else:
                batch = DownloadBatch(results={}, failures={})

            failed_keys = set(batch.failures)
            for key, error in batch.failures.items():
                target_id, arch_id = key.split("/", 1)
                failures.append(Failure(target_id, arch_id, error))
            for decision in decisions:
                if decision.key in failed_keys:
                    decision.failed = True
                    continue
                if decision.need_download:
                    result = batch.results.get(decision.key)
                    if result is None:
                        decision.failed = True
                        failures.append(
                            Failure(
                                decision.entry.target,
                                decision.entry.arch,
                                "download did not produce a result",
                            )
                        )
                        continue
                    decision.result = result
                    classify(
                        decision,
                        result.hashes,
                        archive_enabled=config.archive.enabled,
                        repository=repository,
                    )
                    if not decision.changed:
                        _unlink(staging_root / decision.entry.target / decision.entry.arch / decision.entry.filename)

            changed = [
                decision
                for decision in decisions
                if decision.changed and not decision.failed
            ]
            changed_count = len(changed)

            repaired_keys = {
                decision.key
                for decision in changed
                if decision.key in repair_keys and decision.result is not None
            }
            if (on_error == "rollback" and failures) or (repair_keys - repaired_keys):
                _print_failures(failures)
                return 1

            tagged = plan_tags(
                changed,
                release_config=config.release,
                targets_by_id={target.id: target for target in config.targets},
                run=run,
                repository=repository,
                archive_url_template=config.archive.url_template,
                environ=run.environ,
            )
            validate_tags(list(tagged.values()), pre_download=False)

            updates: dict[str, dict] = {}
            state_updates: dict[str, dict[str, str]] = {}
            for decision in decisions:
                if decision.failed:
                    continue
                if decision.changed:
                    item = tagged[decision.key]
                    manifest_url = item.archive_url if config.archive.enabled else decision.entry.source_url
                    if config.archive.enabled and not manifest_url:
                        raise ReleaseError(
                            f"archive is enabled but no archive URL is available for "
                            f"{decision.key}"
                        )
                    assert decision.result is not None
                    updates[decision.key] = make_entry(
                        version=decision.entry.version,
                        url=manifest_url,
                        filename=decision.entry.filename,
                        hashes=decision.result.hashes,
                    )
                    state_updates[decision.key] = changed_state(
                        decision.entry.source_url,
                        item.archive_url if config.archive.enabled else None,
                        repository if config.archive.enabled else None,
                    )
                else:
                    state_updates[decision.key] = unchanged_state(
                        decision, source_url=decision.entry.source_url
                    )

            manifest = merge_manifest(baseline_manifest, updates)
            state_entries = dict(baseline_state)
            state_entries.update(state_updates)
            if args.prune:
                manifest, state_entries = prune(
                    manifest, state_entries, config.full_keys()
                )

            release_document = build_release_document(
                list(tagged.values()),
                run=run,
                repository=repository,
                archive_enabled=config.archive.enabled,
                output_dir=output_dir,
            )
            update_markdown = render_update_md(
                run=run,
                tagged=tagged,
                failures=failures,
                archive_enabled=config.archive.enabled,
                repository=repository,
            )

            try:
                documents = {
                    SOURCES_NAME: render_sources_nix(manifest),
                    FETCHURL_NAME: render_fetchurl_nix(manifest),
                    RELEASES_NAME: releases_text(release_document),
                    URLS_NAME: urls_text(tagged, archive_enabled=config.archive.enabled),
                    UPDATE_NAME: update_markdown,
                    STATE_NAME: state_text(state_entries),
                    MANIFEST_NAME: render_manifest(manifest),
                }
                _install_outputs(output_dir, staging_root, changed, documents)
            except OutputError as exc:
                keep_staging = exc.recovery_dir is not None
                print(f"ufrd-updater: error: {http.redact_text(str(exc))}", file=sys.stderr)
                return 1
            except (OSError, UnicodeError) as exc:
                print(
                    f"ufrd-updater: error: could not prepare output files: "
                    f"{http.redact_text(str(exc))}",
                    file=sys.stderr,
                )
                return 1
        finally:
            if not keep_staging:
                shutil.rmtree(staging_root, ignore_errors=True)
                try:
                    staging_root.parent.rmdir()
                except OSError:
                    pass

    if not changed_count and not failures:
        print("No updates.")
    _print_failures(failures)

    if commit_enabled:
        try:
            message = render(
                config.git.message,
                _message_scope(run, tagged),
                context="git.message",
                environ=run.environ,
            )
        except TemplateError as exc:
            print(
                f"ufrd-updater: error: could not render commit message: "
                f"{http.redact_text(str(exc))}",
                file=sys.stderr,
            )
            message = None
            action_error = True
        if message is not None and toplevel is not None:
            try:
                commit_result = commit_and_push(
                    toplevel=Path(toplevel),
                    paths=export_paths,
                    message=message,
                    user_name=config.git.user_name,
                    user_email=config.git.user_email,
                    push_mode=push_mode,
                    environ=run.environ,
                )
            except GitError as exc:
                print(f"ufrd-updater: error: {http.redact_text(str(exc))}", file=sys.stderr)
                action_error = True
                if exc.commit:
                    commit_result = CommitResult(commit=exc.commit, pushed=False)

    outputs = _outputs(
        run=run,
        tagged=tagged,
        changed_count=changed_count,
        failures=failures,
        output_dir=output_dir,
        commit_result=commit_result,
    )
    try:
        write_outputs(outputs, environ=run.environ)
    except ActionsError as exc:
        print(f"ufrd-updater: error: {http.redact_text(str(exc))}", file=sys.stderr)
        return 1
    if update_markdown and not append_summary(update_markdown, environ=run.environ):
        LOG.warning("could not append to GITHUB_STEP_SUMMARY; continuing")

    if failures and on_error == "fail":
        return 1
    if action_error:
        return 1
    return 0


def _install_outputs(
    output_dir: Path,
    staging_root: Path,
    changed: list[Decision],
    documents: dict[str, str],
) -> None:
    exports = staging_root / ".exports"
    for name, text in documents.items():
        write_atomic(exports / name, text)
    replacements = [
        (
            staging_root / decision.entry.target / decision.entry.arch / decision.entry.filename,
            final_path(output_dir, decision),
        )
        for decision in changed
    ]
    replacements.extend((exports / name, output_dir / name) for name in documents)
    backups = staging_root / ".backups"
    backups.mkdir(parents=True, exist_ok=True)
    journal: list[tuple[Path, Path | None]] = []
    try:
        for _, destination in replacements:
            if destination.is_dir():
                raise OSError(f"output file path is a directory: {destination}")
            destination.parent.mkdir(parents=True, exist_ok=True)
        for index, (source, destination) in enumerate(replacements):
            backup = backups / str(index) if (
                destination.exists() or destination.is_symlink()
            ) else None
            journal.append((destination, backup))
            if backup is not None:
                os.replace(destination, backup)
            os.replace(source, destination)
    except BaseException as exc:
        rollback_errors: list[str] = []
        for destination, backup in reversed(journal):
            try:
                if backup is None:
                    destination.unlink(missing_ok=True)
                elif backup.exists() or backup.is_symlink():
                    os.replace(backup, destination)
            except OSError as rollback_error:
                rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise OutputError(
                f"output replacement failed and rollback could not finish; "
                f"recovery files are retained at {staging_root}: "
                + "; ".join(rollback_errors),
                recovery_dir=staging_root,
            ) from exc
        if isinstance(exc, (OSError, UnicodeError)):
            raise OutputError(
                f"output replacement failed; previous files restored: {exc}"
            ) from exc
        raise


def _message_scope(run: RunContext, tagged: dict) -> dict[str, str]:
    tags = sorted({item.tag for item in tagged.values()})
    scope = run.variables()
    scope["tags"] = ", ".join(tags)
    scope["tag"] = tags[0] if len(tags) == 1 else ""
    scope["changed_count"] = str(len(tagged))
    return scope


def _outputs(
    *,
    run: RunContext,
    tagged: dict,
    changed_count: int,
    failures: list[Failure],
    output_dir: Path,
    commit_result: CommitResult,
) -> dict[str, str]:
    tags = sorted({item.tag for item in tagged.values()})
    files = files_text(tagged, output_dir)
    failed = [
        {
            "target": failure.target,
            "arch": failure.arch,
            "error": http.redact_text(failure.error),
        }
        for failure in sorted(failures, key=lambda item: (item.target, item.arch))
    ]
    return {
        "changed": "true" if changed_count else "false",
        "tag": tags[0] if len(tags) == 1 else "",
        "tags": json.dumps(tags),
        "manifest": str((output_dir / MANIFEST_NAME).resolve()),
        "sources": str((output_dir / SOURCES_NAME).resolve()),
        "fetchurl": str((output_dir / FETCHURL_NAME).resolve()),
        "state": str((output_dir / STATE_NAME).resolve()),
        "releases": str((output_dir / RELEASES_NAME).resolve()),
        "urls": str((output_dir / URLS_NAME).resolve()),
        "summary": str((output_dir / UPDATE_NAME).resolve()),
        "files": files,
        "failed": json.dumps(failed, ensure_ascii=False),
        "failed_count": str(len(failed)),
        "commit": commit_result.commit or "",
        "pushed": "true" if commit_result.pushed else "false",
    }


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
