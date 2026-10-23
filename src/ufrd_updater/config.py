"""Configuration schema and strict TOML validation.

Every default documented in the project plan lives here. Unknown keys at any
level are errors so that typos fail fast. Runtime ownership of network and file
writes is deliberately absent: this module is safe for ``validate``.
"""

from __future__ import annotations

import codecs
import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jmespath
import soupsieve

from .template import TemplateError, template_fields

TARGET_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
VARIABLE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

TAG_VARIABLES = {
    "datetime",
    "date",
    "time",
    "run_id",
    "run_number",
    "sha",
    "repository",
    "id",
    "arch",
    "version",
    "filename",
}
GIT_VARIABLES = {"datetime", "date", "time", "tags", "tag", "changed_count"}
ARCHIVE_VARIABLES = {"repository", "tag", "filename"}

METHODS = ("GET", "POST", "HEAD")
EXTRACT_TYPES = ("regex", "json", "html", "command")
EXTRACT_FROM = ("body", "header", "url", "var")
ON_ERROR = ("fail", "continue", "rollback")
PUSH_MODES = ("auto", "true", "false")

_MISSING = object()


class ConfigError(Exception):
    """Raised for any configuration or data-shape error (exit code 2)."""


@dataclass(frozen=True)
class Settings:
    output_dir: str = "dist"
    concurrency: int = 4
    retries: int = 3
    timeout: float = 30.0
    download_timeout: float = 120.0
    connect_timeout: float = 10.0
    user_agent: str = "ufrd-updater/0.1.0"
    on_error: str = "fail"


@dataclass(frozen=True)
class GitConfig:
    commit: bool = False
    push: str = "auto"
    message: str = "chore(ufrd): update sources {datetime}"
    user_name: str = "github-actions[bot]"
    user_email: str = "41898282+github-actions[bot]@users.noreply.github.com"


@dataclass(frozen=True)
class ReleaseConfig:
    tag_template: str = "ufrd-{datetime}-{run_id}"


@dataclass(frozen=True)
class ArchiveConfig:
    enabled: bool = False
    repository: str = ""
    url_template: str = "https://github.com/{repository}/releases/download/{tag}/{filename}"


@dataclass(frozen=True)
class Extract:
    var: str
    type: str
    from_: str = "body"
    source: str | None = None
    name: str | None = None
    pattern: str | None = None
    group: int | str = 0
    flags: str = ""
    query: str | None = None
    selector: str | None = None
    attr: str | None = None
    all: bool = False
    index: int | None = None
    strip: bool | None = None
    required: bool = True
    default: str | None = None
    command: str | None = None
    timeout: float | None = None


@dataclass(frozen=True)
class Request:
    url: str
    id: str | None = None
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    follow_redirects: bool = True
    expect_status: tuple[int, ...] | None = None
    body: str | None = None
    json: Any = None
    encoding: str | None = None
    extract: tuple[Extract, ...] = ()


@dataclass(frozen=True)
class Arch:
    id: str
    url: str
    version: str | None = None
    filename: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    download_headers: dict[str, str] = field(default_factory=dict)
    vars: dict[str, str] = field(default_factory=dict)
    requests: tuple[Request, ...] = ()


@dataclass(frozen=True)
class Target:
    id: str
    description: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    download_headers: dict[str, str] = field(default_factory=dict)
    timeout: float | None = None
    retries: int | None = None
    tag_template: str | None = None
    vars: dict[str, str] = field(default_factory=dict)
    requests: tuple[Request, ...] = ()
    arch: tuple[Arch, ...] = ()


@dataclass(frozen=True)
class Config:
    path: Path
    base_dir: Path
    version: int
    settings: Settings
    git: GitConfig
    release: ReleaseConfig
    archive: ArchiveConfig
    targets: tuple[Target, ...]

    @property
    def output_dir(self) -> Path:
        output = Path(self.settings.output_dir)
        return output if output.is_absolute() else self.base_dir / output

    def full_keys(self) -> set[str]:
        return {f"{target.id}/{arch.id}" for target in self.targets for arch in target.arch}

    def selected_targets(self, only: re.Pattern[str] | None) -> tuple[Target, ...]:
        if only is None:
            return self.targets
        return tuple(target for target in self.targets if only.search(target.id))


def _type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "table"
    return type(value).__name__


def _table(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must be a table, got {_type_name(value)}")
    return value


def _unknown_keys(table: dict[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"{path}: unknown key(s): {', '.join(unknown)}")


def _string(
    table: dict[str, Any],
    key: str,
    path: str,
    *,
    default: Any = _MISSING,
    allow_empty: bool = False,
    required: bool = False,
) -> str | None:
    label = f"{path}.{key}"
    if key not in table:
        if required:
            raise ConfigError(f"{label} is required")
        if default is _MISSING:
            return None
        return default
    value = table[key]
    if not isinstance(value, str):
        raise ConfigError(f"{label} must be a string, got {_type_name(value)}")
    if not allow_empty and value == "":
        raise ConfigError(f"{label} must not be empty")
    return value


def _boolean(table: dict[str, Any], key: str, path: str, default: Any = _MISSING) -> bool:
    label = f"{path}.{key}"
    if key not in table:
        if default is _MISSING:
            raise ConfigError(f"{label} is required")
        return default
    value = table[key]
    if not isinstance(value, bool):
        raise ConfigError(f"{label} must be a boolean, got {_type_name(value)}")
    return value


def _integer(
    table: dict[str, Any],
    key: str,
    path: str,
    *,
    default: Any = _MISSING,
    minimum: int | None = None,
) -> int:
    label = f"{path}.{key}"
    if key not in table:
        if default is _MISSING:
            raise ConfigError(f"{label} is required")
        return default
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{label} must be an integer, got {_type_name(value)}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{label} must be >= {minimum}")
    return value


def _number(
    table: dict[str, Any],
    key: str,
    path: str,
    *,
    default: Any = _MISSING,
    positive: bool = False,
) -> float:
    label = f"{path}.{key}"
    if key not in table:
        if default is _MISSING:
            raise ConfigError(f"{label} is required")
        return default
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{label} must be a number, got {_type_name(value)}")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ConfigError(f"{label} must be finite") from exc
    if not math.isfinite(result):
        raise ConfigError(f"{label} must be finite")
    if positive and result <= 0:
        raise ConfigError(f"{label} must be > 0")
    return result


def _string_map(
    table: dict[str, Any],
    key: str,
    path: str,
    *,
    templated: bool = False,
) -> dict[str, str]:
    if key not in table:
        return {}
    label = f"{path}.{key}"
    value = table[key]
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be a table, got {_type_name(value)}")
    result: dict[str, str] = {}
    for map_key, map_value in value.items():
        if not isinstance(map_key, str) or not map_key:
            raise ConfigError(f"{label} keys must be non-empty strings")
        if not isinstance(map_value, str):
            raise ConfigError(
                f"{label}.{map_key} must be a string, got {_type_name(map_value)}"
            )
        if templated:
            _validate_template(map_value, f"{label}.{map_key}")
        result[map_key] = map_value
    return result


def _validate_template(
    text: str,
    path: str,
    *,
    allowed: set[str] | None = None,
    forbidden: set[str] | None = None,
) -> None:
    try:
        fields = template_fields(text)
    except TemplateError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    for name in fields:
        if name.startswith("env:"):
            if not name[4:]:
                raise ConfigError(f"{path}: empty environment variable name")
            continue
        if allowed is not None and name not in allowed:
            options = ", ".join(sorted(allowed))
            raise ConfigError(
                f"{path}: unknown template variable '{{{name}}}'; allowed: {options}"
            )
        if forbidden and name in forbidden:
            raise ConfigError(f"{path}: template variable '{{{name}}}' is not allowed here")


def _validate_json_value(value: Any, path: str) -> None:
    if isinstance(value, str):
        _validate_template(value, path)
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigError(f"{path} must be finite")
    if isinstance(value, bool) or isinstance(value, (int, float)) or value is None:
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_json_value(item, f"{path}.{key}")
        return
    raise ConfigError(
        f"{path} contains a value of type {_type_name(value)}; JSON bodies may only "
        "contain strings, numbers, booleans, null, arrays and tables"
    )


def _extract_vars(requests: tuple[Request, ...]) -> set[str]:
    return {extract.var for request in requests for extract in request.extract}


def _parse_extract(data: Any, path: str) -> Extract:
    table = _table(data, path)
    _unknown_keys(
        table,
        {
            "var",
            "type",
            "from",
            "source",
            "name",
            "pattern",
            "group",
            "flags",
            "query",
            "selector",
            "attr",
            "all",
            "index",
            "strip",
            "required",
            "default",
            "command",
            "timeout",
        },
        path,
    )
    var = _string(table, "var", path, required=True)
    assert var is not None
    if not VARIABLE_NAME_RE.match(var):
        raise ConfigError(
            f"{path}.var must match [A-Za-z_][A-Za-z0-9_]*, got '{var}'"
        )
    kind = _string(table, "type", path, required=True)
    assert kind is not None
    if kind not in EXTRACT_TYPES:
        raise ConfigError(f"{path}.type must be one of {', '.join(EXTRACT_TYPES)}")
    from_ = _string(table, "from", path, default="body")
    assert from_ is not None
    if from_ not in EXTRACT_FROM:
        raise ConfigError(f"{path}.from must be one of {', '.join(EXTRACT_FROM)}")

    def optional(key: str) -> str | None:
        return _string(table, key, path, allow_empty=True)

    source = optional("source")
    name = optional("name")
    pattern = optional("pattern")
    query = optional("query")
    selector = optional("selector")
    attr = optional("attr")
    default = optional("default")
    command = optional("command")

    group_raw = table.get("group", 0)
    if isinstance(group_raw, bool) or not isinstance(group_raw, (int, str)):
        raise ConfigError(f"{path}.group must be an integer or string")
    group: int | str = group_raw
    if isinstance(group, str) and not group:
        raise ConfigError(f"{path}.group must not be empty")

    flags = _string(table, "flags", path, default="") or ""
    for flag in flags:
        if flag not in "imsx":
            raise ConfigError(f"{path}.flags only supports i, m, s, x")

    all_values = _boolean(table, "all", path, default=False)
    index = (
        _integer(table, "index", path)
        if "index" in table
        else None
    )
    strip = _boolean(table, "strip", path) if "strip" in table else None
    required = _boolean(table, "required", path, default=True)
    timeout = (
        _number(table, "timeout", path, positive=True) if "timeout" in table else None
    )

    if kind == "regex":
        if not pattern:
            raise ConfigError(f"{path}.pattern is required for type = \"regex\"")
        flag_value = 0
        for flag, value in (("i", re.IGNORECASE), ("m", re.MULTILINE), ("s", re.DOTALL), ("x", re.VERBOSE)):
            if flag in flags:
                flag_value |= value
        try:
            re.compile(pattern, flag_value)
        except re.error as exc:
            raise ConfigError(f"{path}.pattern is invalid: {exc}") from exc
        if from_ == "header" and not name:
            raise ConfigError(f"{path}.name is required when from = \"header\"")
        if from_ == "var" and not source:
            raise ConfigError(f"{path}.source is required when from = \"var\"")
    elif kind == "json":
        if from_ != "body":
            raise ConfigError(f"{path}.from must be \"body\" for type = \"json\"")
        if not query:
            raise ConfigError(f"{path}.query is required for type = \"json\"")
        try:
            jmespath.compile(query)
        except jmespath.exceptions.JMESPathError as exc:
            raise ConfigError(f"{path}.query is invalid: {exc}") from exc
        if all_values:
            raise ConfigError(f"{path}.all is not allowed for type = \"json\"")
    elif kind == "html":
        if from_ != "body":
            raise ConfigError(f"{path}.from must be \"body\" for type = \"html\"")
        if not selector:
            raise ConfigError(f"{path}.selector is required for type = \"html\"")
        try:
            soupsieve.compile(selector)
        except soupsieve.SelectorSyntaxError as exc:
            raise ConfigError(f"{path}.selector is invalid: {exc}") from exc
    elif kind == "command":
        if not command:
            raise ConfigError(f"{path}.command is required for type = \"command\"")

    if attr and kind != "html":
        raise ConfigError(f"{path}.attr is only valid for type = \"html\"")

    return Extract(
        var=var,
        type=kind,
        from_=from_,
        source=source,
        name=name,
        pattern=pattern,
        group=group,
        flags=flags,
        query=query,
        selector=selector,
        attr=attr,
        all=all_values,
        index=index,
        strip=strip,
        required=required,
        default=default,
        command=command,
        timeout=timeout,
    )


def _parse_request(data: Any, path: str) -> Request:
    table = _table(data, path)
    _unknown_keys(
        table,
        {
            "id",
            "url",
            "method",
            "headers",
            "follow_redirects",
            "expect_status",
            "body",
            "json",
            "encoding",
            "extract",
        },
        path,
    )
    request_id = _string(table, "id", path, allow_empty=True)
    url = _string(table, "url", path, required=True)
    assert url is not None
    _validate_template(url, f"{path}.url")

    method = (_string(table, "method", path, default="GET") or "GET").upper()
    if method not in METHODS:
        raise ConfigError(f"{path}.method must be one of {', '.join(METHODS)}")

    headers = _string_map(table, "headers", path, templated=True)

    follow_redirects = _boolean(table, "follow_redirects", path, default=True)

    expect_status: tuple[int, ...] | None = None
    if "expect_status" in table:
        raw = table["expect_status"]
        if isinstance(raw, bool):
            raise ConfigError(f"{path}.expect_status must be an integer or array")
        if isinstance(raw, int):
            values = [raw]
        elif isinstance(raw, list):
            values = []
            for item in raw:
                if isinstance(item, bool) or not isinstance(item, int):
                    raise ConfigError(f"{path}.expect_status entries must be integers")
                values.append(item)
            if not values:
                raise ConfigError(f"{path}.expect_status must not be empty")
        else:
            raise ConfigError(f"{path}.expect_status must be an integer or array")
        for value in values:
            if value < 100 or value > 599:
                raise ConfigError(f"{path}.expect_status values must be valid HTTP statuses")
        expect_status = tuple(values)

    body = _string(table, "body", path, allow_empty=True)
    if body is not None:
        _validate_template(body, f"{path}.body")
    if "json" in table and body is not None:
        raise ConfigError(f"{path}: body and json are mutually exclusive")
    json_body = None
    if "json" in table:
        json_body = table["json"]
        if not isinstance(json_body, dict):
            raise ConfigError(f"{path}.json must be a table")
        _validate_json_value(json_body, f"{path}.json")

    encoding = _string(table, "encoding", path, allow_empty=True)
    if encoding:
        try:
            codecs.lookup(encoding)
        except LookupError as exc:
            raise ConfigError(f"{path}.encoding is not a known codec: {encoding}") from exc

    extract: list[Extract] = []
    if "extract" in table:
        raw_extract = table["extract"]
        if not isinstance(raw_extract, list):
            raise ConfigError(f"{path}.extract must be an array of tables")
        for index, item in enumerate(raw_extract):
            extract.append(_parse_extract(item, f"{path}.extract[{index}]"))

    return Request(
        url=url,
        id=request_id,
        method=method,
        headers=headers,
        follow_redirects=follow_redirects,
        expect_status=expect_status,
        body=body,
        json=json_body,
        encoding=encoding,
        extract=tuple(extract),
    )


def _parse_requests(table: dict[str, Any], path: str) -> tuple[Request, ...]:
    if "request" not in table:
        return ()
    raw = table["request"]
    if not isinstance(raw, list):
        raise ConfigError(f"{path}.request must be an array of tables")
    return tuple(
        _parse_request(item, f"{path}.request[{index}]")
        for index, item in enumerate(raw)
    )


def _parse_arch(data: Any, path: str) -> Arch:
    table = _table(data, path)
    _unknown_keys(
        table,
        {
            "id",
            "version",
            "url",
            "filename",
            "headers",
            "download_headers",
            "vars",
            "request",
        },
        path,
    )
    arch_id = _string(table, "id", path, required=True)
    assert arch_id is not None
    if not TARGET_ID_RE.match(arch_id):
        raise ConfigError(
            f"{path}.id must match [A-Za-z0-9][A-Za-z0-9._-]*, got '{arch_id}'"
        )
    version = _string(table, "version", path)
    if version is not None:
        _validate_template(version, f"{path}.version")
    url = _string(table, "url", path, required=True)
    assert url is not None
    _validate_template(url, f"{path}.url")
    filename = _string(table, "filename", path)
    if filename is not None:
        _validate_template(filename, f"{path}.filename", forbidden={"url"})
    headers = _string_map(table, "headers", path, templated=True)
    download_headers = _string_map(table, "download_headers", path, templated=True)
    vars_ = _string_map(table, "vars", path, templated=True)
    requests = _parse_requests(table, path)
    return Arch(
        id=arch_id,
        url=url,
        version=version,
        filename=filename,
        headers=headers,
        download_headers=download_headers,
        vars=vars_,
        requests=requests,
    )


def _parse_target(data: Any, path: str) -> Target:
    table = _table(data, path)
    _unknown_keys(
        table,
        {
            "id",
            "description",
            "headers",
            "download_headers",
            "timeout",
            "retries",
            "tag_template",
            "vars",
            "request",
            "arch",
        },
        path,
    )
    target_id = _string(table, "id", path, required=True)
    assert target_id is not None
    if not TARGET_ID_RE.match(target_id):
        raise ConfigError(
            f"{path}.id must match [A-Za-z0-9][A-Za-z0-9._-]*, got '{target_id}'"
        )
    description = _string(table, "description", path, default="", allow_empty=True) or ""
    headers = _string_map(table, "headers", path, templated=True)
    download_headers = _string_map(table, "download_headers", path, templated=True)
    timeout = _number(table, "timeout", path, positive=True) if "timeout" in table else None
    retries = _integer(table, "retries", path, minimum=0) if "retries" in table else None
    tag_template = _string(table, "tag_template", path)
    if tag_template is not None:
        _validate_template(tag_template, f"{path}.tag_template", allowed=TAG_VARIABLES)
    vars_ = _string_map(table, "vars", path, templated=True)
    requests = _parse_requests(table, path)

    if "arch" not in table:
        raise ConfigError(f"{path}.arch must contain at least one [[{path}.arch]] table")
    raw_arch = table["arch"]
    if not isinstance(raw_arch, list):
        raise ConfigError(f"{path}.arch must be an array of tables")
    arches: list[Arch] = []
    for index, item in enumerate(raw_arch):
        arches.append(_parse_arch(item, f"{path}.arch[{index}]"))
    if not arches:
        raise ConfigError(f"{path}.arch must contain at least one arch")

    arch_ids = [arch.id for arch in arches]
    duplicates = sorted({arch_id for arch_id in arch_ids if arch_ids.count(arch_id) > 1})
    if duplicates:
        raise ConfigError(f"{path}.arch: duplicate arch id(s): {', '.join(duplicates)}")

    provided = _extract_vars(requests)
    for arch in arches:
        provided_arch = _extract_vars(arch.requests)
        if (
            arch.version is None
            and "version" not in vars_
            and "version" not in arch.vars
            and "version" not in provided
            and "version" not in provided_arch
        ):
            raise ConfigError(
                f"{path}.arch: arch '{arch.id}' has no version template and no "
                "'version' variable is provided by vars or extractors"
            )

    return Target(
        id=target_id,
        description=description,
        headers=headers,
        download_headers=download_headers,
        timeout=timeout,
        retries=retries,
        tag_template=tag_template,
        vars=vars_,
        requests=requests,
        arch=tuple(arches),
    )


def _parse_settings(data: Any, path: str) -> Settings:
    table = _table(data, path)
    _unknown_keys(
        table,
        {
            "output_dir",
            "concurrency",
            "retries",
            "timeout",
            "download_timeout",
            "connect_timeout",
            "user_agent",
            "on_error",
        },
        path,
    )
    output_dir = _string(table, "output_dir", path, default="dist")
    assert output_dir is not None
    concurrency = _integer(table, "concurrency", path, default=4, minimum=1)
    retries = _integer(table, "retries", path, default=3, minimum=0)
    timeout = _number(table, "timeout", path, default=30.0, positive=True)
    download_timeout = _number(table, "download_timeout", path, default=120.0, positive=True)
    connect_timeout = _number(table, "connect_timeout", path, default=10.0, positive=True)
    user_agent = _string(table, "user_agent", path, default="ufrd-updater/0.1.0")
    assert user_agent is not None
    if re.fullmatch(r"[!-~](?:[ \t!-~]*[!-~])?", user_agent) is None:
        raise ConfigError(
            f"{path}.user_agent must be a valid ASCII HTTP header value"
        )
    on_error = _string(table, "on_error", path, default="fail")
    assert on_error is not None
    if on_error not in ON_ERROR:
        raise ConfigError(f"{path}.on_error must be one of {', '.join(ON_ERROR)}")
    return Settings(
        output_dir=output_dir,
        concurrency=concurrency,
        retries=retries,
        timeout=timeout,
        download_timeout=download_timeout,
        connect_timeout=connect_timeout,
        user_agent=user_agent,
        on_error=on_error,
    )


def _parse_git(data: Any, path: str) -> GitConfig:
    table = _table(data, path)
    _unknown_keys(table, {"commit", "push", "message", "user_name", "user_email"}, path)
    commit = _boolean(table, "commit", path, default=False)
    push_raw = table.get("push", "auto")
    if isinstance(push_raw, bool):
        push = "true" if push_raw else "false"
    elif isinstance(push_raw, str) and push_raw in PUSH_MODES:
        push = push_raw
    else:
        raise ConfigError(f"{path}.push must be \"auto\", true or false")
    message = _string(table, "message", path, default="chore(ufrd): update sources {datetime}")
    assert message is not None
    _validate_template(message, f"{path}.message", allowed=GIT_VARIABLES)
    user_name = _string(table, "user_name", path, default="github-actions[bot]")
    assert user_name is not None
    user_email = _string(
        table,
        "user_email",
        path,
        default="41898282+github-actions[bot]@users.noreply.github.com",
    )
    assert user_email is not None
    return GitConfig(
        commit=commit,
        push=push,
        message=message,
        user_name=user_name,
        user_email=user_email,
    )


def _parse_release(data: Any, path: str) -> ReleaseConfig:
    table = _table(data, path)
    _unknown_keys(table, {"tag_template"}, path)
    tag_template = _string(table, "tag_template", path, default="ufrd-{datetime}-{run_id}")
    assert tag_template is not None
    _validate_template(tag_template, f"{path}.tag_template", allowed=TAG_VARIABLES)
    return ReleaseConfig(tag_template=tag_template)


def _parse_archive(data: Any, path: str) -> ArchiveConfig:
    table = _table(data, path)
    _unknown_keys(table, {"enabled", "repository", "url_template"}, path)
    enabled = _boolean(table, "enabled", path, default=False)
    repository = _string(table, "repository", path, default="", allow_empty=True) or ""
    url_template = _string(
        table,
        "url_template",
        path,
        default="https://github.com/{repository}/releases/download/{tag}/{filename}",
    )
    assert url_template is not None
    _validate_template(url_template, f"{path}.url_template", allowed=ARCHIVE_VARIABLES)
    return ArchiveConfig(enabled=enabled, repository=repository, url_template=url_template)


def parse_config(data: Any, path: Path) -> Config:
    """Validate an already-parsed TOML mapping."""

    table = _table(data, "config")
    _unknown_keys(table, {"version", "settings", "git", "release", "archive", "target"}, "config")
    if "version" not in table:
        raise ConfigError("config.version is required")
    raw_version = table["version"]
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        raise ConfigError("config.version must be the integer 1")
    if raw_version != 1:
        raise ConfigError(f"config.version must be 1, got {raw_version}")
    version = raw_version

    settings = _parse_settings(table.get("settings", {}), "settings")
    git = _parse_git(table.get("git", {}), "git")
    release = _parse_release(table.get("release", {}), "release")
    archive = _parse_archive(table.get("archive", {}), "archive")

    if "target" not in table:
        raise ConfigError("config needs at least one [[target]] table")
    raw_targets = table["target"]
    if not isinstance(raw_targets, list):
        raise ConfigError("config.target must be an array of tables")
    targets = [
        _parse_target(item, f"target[{index}]")
        for index, item in enumerate(raw_targets)
    ]
    target_ids = [target.id for target in targets]
    duplicates = sorted({target_id for target_id in target_ids if target_ids.count(target_id) > 1})
    if duplicates:
        raise ConfigError(f"config.target: duplicate target id(s): {', '.join(duplicates)}")

    resolved_path = Path(path).resolve()
    return Config(
        path=resolved_path,
        base_dir=resolved_path.parent,
        version=version,
        settings=settings,
        git=git,
        release=release,
        archive=archive,
        targets=tuple(targets),
    )


def load_config(path: str | os.PathLike[str] = "updater.toml") -> Config:
    """Load and validate a configuration file."""

    config_path = Path(path)
    try:
        text = config_path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise ConfigError(
            f"cannot read config file '{config_path}': invalid UTF-8"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"cannot read config file '{config_path}': {exc}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in '{config_path}': {exc}") from exc
    return parse_config(data, config_path)


def validate_output_paths(config: Config) -> None:
    """Offline structural checks for the output directory."""

    if "\x00" in config.settings.output_dir:
        raise ConfigError("settings.output_dir contains a NUL character")
    try:
        output = config.output_dir
        ancestor = output
        while not ancestor.exists() and not ancestor.is_symlink():
            ancestor = ancestor.parent
        if not ancestor.is_dir():
            raise ConfigError(
                f"output_dir '{output}' cannot use '{ancestor}': not a directory"
            )
    except (OSError, ValueError) as exc:
        raise ConfigError("cannot inspect output_dir") from exc
