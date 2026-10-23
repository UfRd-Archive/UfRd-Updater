"""Extractor execution.

A request's extractors run sequentially against a staging variable map. If any
extractor in the request fails, the caller discards the staging results and the
previous scope is left untouched.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import jmespath
from bs4 import BeautifulSoup

from .config import Extract

FLAGS = {
    "i": re.IGNORECASE,
    "m": re.MULTILINE,
    "s": re.DOTALL,
    "x": re.VERBOSE,
}


class ExtractError(Exception):
    """Raised when an extractor fails."""


@dataclass
class ExtractContext:
    body: bytes
    url: str
    status: int
    headers: httpx.Headers
    encoding: str | None
    variables: dict[str, Any]
    cwd: Path
    default_timeout: float
    environ: Mapping[str, str]


def run_extractors(
    extractors: tuple[Extract, ...] | list[Extract],
    context: ExtractContext,
) -> dict[str, Any]:
    """Run extractors sequentially, returning a copy of the variable scope."""

    staging = dict(context.variables)
    for extractor in extractors:
        try:
            found, value = _run_one(extractor, context, staging)
        except ExtractError as exc:
            raise ExtractError(f"extractor '{extractor.var}': {exc}") from exc
        if found:
            staging[extractor.var] = value
        elif extractor.default is not None:
            staging[extractor.var] = extractor.default
        elif extractor.required:
            raise ExtractError(
                f"extractor '{extractor.var}' produced no result"
            )
    return staging


def _run_one(
    extractor: Extract,
    context: ExtractContext,
    staging: dict[str, Any],
) -> tuple[bool, Any]:
    if extractor.type == "command":
        return _run_command(extractor, context, staging)

    if extractor.type == "json":
        return _run_json(extractor, context)
    if extractor.type == "html":
        return _run_html(extractor, context)

    pattern = _compile_regex(extractor)

    if extractor.from_ == "body":
        return _regex_values(
            extractor, pattern, _decode_body(context), strip_default=False
        )
    if extractor.from_ == "url":
        return _regex_values(extractor, pattern, context.url, strip_default=False)
    if extractor.from_ == "var":
        assert extractor.source is not None
        if extractor.source not in staging:
            return False, None
        value = staging[extractor.source]
        if not isinstance(value, str):
            raise ExtractError(
                f"source variable '{extractor.source}' is a "
                f"{type(value).__name__}, not a scalar string"
            )
        return _regex_values(extractor, pattern, value, strip_default=False)
    if extractor.from_ == "header":
        assert extractor.name is not None
        values = context.headers.get_list(extractor.name)
        texts = values if (extractor.all or extractor.index is not None) else values[:1]
        matches: list[str] = []
        for text in texts:
            matches.extend(_find_matches(extractor, pattern, text))
        return _select(extractor, matches, strip_default=True)
    raise ExtractError(f"unsupported from = {extractor.from_!r}")  # pragma: no cover


def _compile_regex(extractor: Extract) -> re.Pattern[str]:
    flags = 0
    for flag in extractor.flags:
        flags |= FLAGS[flag]
    try:
        return re.compile(extractor.pattern or "", flags)
    except re.error as exc:  # config validation should catch this
        raise ExtractError(f"invalid regex: {exc}") from exc


def _find_matches(
    extractor: Extract,
    pattern: re.Pattern[str],
    text: str,
) -> list[str]:
    matches: list[str] = []
    for match in pattern.finditer(text):
        try:
            value = match.group(extractor.group)
        except (IndexError, re.error) as exc:
            raise ExtractError(f"invalid regex group {extractor.group!r}: {exc}") from exc
        if value is not None:
            matches.append(value)
    return matches


def _regex_values(
    extractor: Extract,
    pattern: re.Pattern[str],
    text: str,
    *,
    strip_default: bool,
) -> tuple[bool, Any]:
    return _select(
        extractor,
        _find_matches(extractor, pattern, text),
        strip_default=strip_default,
    )


def _select(
    extractor: Extract,
    values: list[str],
    *,
    strip_default: bool,
) -> tuple[bool, Any]:
    strip = strip_default if extractor.strip is None else extractor.strip
    if extractor.index is not None:
        try:
            value: Any = values[extractor.index]
        except IndexError:
            return False, None
        return True, _apply_strip(value, strip)
    if not values:
        return False, None
    if extractor.all:
        return True, [_apply_strip(item, strip) for item in values]
    return True, _apply_strip(values[0], strip)


def _apply_strip(value: Any, strip: bool) -> Any:
    if not strip:
        return value
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return [_apply_strip(item, strip) for item in value]
    return value


def _decode_body(context: ExtractContext) -> str:
    encoding = context.encoding or "utf-8"
    try:
        return context.body.decode(encoding, errors="replace")
    except LookupError as exc:
        raise ExtractError(f"unknown body encoding '{encoding}'") from exc


def _run_json(extractor: Extract, context: ExtractContext) -> tuple[bool, Any]:
    try:
        data = json.loads(context.body)
    except (ValueError, RecursionError) as exc:
        raise ExtractError("response body is not valid JSON") from exc
    try:
        result = jmespath.search(extractor.query or "", data)
    except (
        jmespath.exceptions.JMESPathError,
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
    ) as exc:
        raise ExtractError("could not evaluate JMESPath query") from exc
    if result is None:
        return False, None
    value = result
    if extractor.index is not None:
        if not isinstance(result, list):
            raise ExtractError("index requires a list result from the JSON query")
        try:
            value = result[extractor.index]
        except IndexError:
            return False, None
    if value is None:
        return False, None
    strip = bool(extractor.strip)
    return True, _json_scalar(value, strip)


def _json_scalar(value: Any, strip: bool) -> Any:
    """Convert top-level JSON scalars to strings; keep lists/dicts intact."""

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip() if strip else value
    # Lists and objects remain Python values so command extractors receive
    # their original structure through UFRD_VARS.
    return value


def _run_html(extractor: Extract, context: ExtractContext) -> tuple[bool, Any]:
    try:
        soup = BeautifulSoup(
            context.body,
            "html.parser",
            from_encoding=context.encoding,
        )
    except Exception as exc:  # BeautifulSoup raises several unrelated types
        raise ExtractError(f"could not parse HTML body: {exc}") from exc
    try:
        nodes = soup.select(extractor.selector or "")
    except Exception as exc:  # soupsieve raises its own hierarchy
        raise ExtractError(f"invalid CSS selector: {exc}") from exc
    strip = True if extractor.strip is None else extractor.strip
    values: list[str] = []
    if extractor.attr:
        for node in nodes:
            if not node.has_attr(extractor.attr):
                continue
            raw = node.get(extractor.attr)
            if isinstance(raw, list):
                raw = " ".join(str(item) for item in raw)
            text = str(raw)
            values.append(text.strip() if strip else text)
    else:
        for node in nodes:
            text = node.get_text(strip=strip)
            values.append(text)
    return _select(extractor, values, strip_default=False)


def _run_command(
    extractor: Extract,
    context: ExtractContext,
    staging: dict[str, Any],
) -> tuple[bool, Any]:
    environment = dict(context.environ)
    environment["UFRD_BODY"] = _decode_body(context)
    environment["UFRD_URL"] = context.url
    environment["UFRD_STATUS"] = str(context.status)
    environment["UFRD_HEADERS"] = json.dumps(
        {key: value for key, value in context.headers.multi_items()},
        ensure_ascii=False,
    )
    environment["UFRD_VARS"] = json.dumps(staging, ensure_ascii=False, default=str)
    for name, value in staging.items():
        if isinstance(value, (str, int, float, bool)):
            environment[f"UFRD_VAR_{name.upper()}"] = _scalar_to_env(value)

    timeout = extractor.timeout if extractor.timeout is not None else context.default_timeout
    try:
        with subprocess.Popen(
            ["sh", "-c", extractor.command or ""],
            cwd=str(context.cwd),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        ) as process:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except BaseException:
                # Stop the shell's process group before the caller can continue
                # or roll back the failed request.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                raise
    except FileNotFoundError as exc:
        raise ExtractError("POSIX shell 'sh' is not available") from exc
    except subprocess.TimeoutExpired as exc:
        raise ExtractError(f"command timed out after {timeout:g}s") from exc
    except (OSError, ValueError) as exc:
        # E2BIG for oversized environment strings, embedded NUL bytes, and
        # other process-spawn failures are per-extractor errors, not crashes.
        raise ExtractError("could not run command") from exc
    if process.returncode != 0:
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        detail = f": {stderr_text[:500]}" if stderr_text else ""
        raise ExtractError(f"command exited with status {process.returncode}{detail}")
    output = stdout.decode("utf-8", errors="replace")
    strip = True if extractor.strip is None else extractor.strip
    return True, output.strip() if strip else output


def _scalar_to_env(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
