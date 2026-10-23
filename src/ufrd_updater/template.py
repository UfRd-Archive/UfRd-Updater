"""Strict ``str.format``-style templates.

Only simple ``{name}`` fields and ``{env:NAME}`` lookups are supported.
Conversions, format specs and attribute/index access are rejected so that a
typo is a hard error instead of a surprising lookup.
"""

from __future__ import annotations

import os
import re
import string
from collections.abc import Mapping
from typing import Any

VARIABLE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class TemplateError(Exception):
    """Raised for invalid templates or unresolved variables."""


class _Field:
    __slots__ = ("literal", "name")

    def __init__(self, literal: str, name: str | None) -> None:
        self.literal = literal
        self.name = name


def parse_template(text: str) -> list[_Field]:
    """Parse a template, validating every field expression.

    Raises :class:`TemplateError` for malformed templates, conversions, format
    specs, dotted/indexed field names and empty environment names.
    """

    fields: list[_Field] = []
    try:
        parsed = list(string.Formatter().parse(text))
    except ValueError as exc:
        raise TemplateError(f"invalid template: {exc}") from exc

    for literal, field_name, format_spec, conversion in parsed:
        if literal:
            fields.append(_Field(literal, None))
        if field_name is None:
            continue
        if field_name == "":
            raise TemplateError("invalid template: empty field name")
        if conversion:
            raise TemplateError(
                f"invalid template: conversion !{conversion} is not supported"
            )
        if field_name == "env":
            env_name = format_spec
            if not env_name:
                raise TemplateError("invalid template: empty environment variable name")
            if not VARIABLE_NAME_RE.match(env_name):
                raise TemplateError(
                    f"invalid template: invalid environment variable name '{env_name}'"
                )
            fields.append(_Field(literal, f"env:{env_name}"))
            continue
        if format_spec:
            raise TemplateError(
                f"invalid template: format spec ':{format_spec}' is not supported"
            )
        if field_name.startswith("env:"):
            env_name = field_name[4:]
            if not env_name:
                raise TemplateError("invalid template: empty environment variable name")
        elif not VARIABLE_NAME_RE.match(field_name):
            raise TemplateError(
                f"invalid template: unsupported field name '{field_name}' "
                "(attribute/index access is not supported)"
            )
        fields.append(_Field(literal, field_name))
    return fields


def template_fields(text: str) -> list[str]:
    """Return the variable names referenced by a template."""

    return [field.name for field in parse_template(text) if field.name is not None]


def render(
    text: str,
    variables: Mapping[str, Any],
    *,
    context: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Render a template with simple variable substitution.

    Missing variables raise :class:`TemplateError` listing available names but
    never their values. Non-scalar values are rejected with a hint that explains
    how to narrow them.
    """

    env = os.environ if environ is None else environ
    out: list[str] = []
    for field in parse_template(text):
        if field.name is None:
            out.append(field.literal)
            continue
        name = field.name
        if name.startswith("env:"):
            env_name = name[4:]
            if env_name not in env:
                raise TemplateError(
                    f"environment variable '{env_name}' is not set"
                    + _context_suffix(context)
                )
            out.append(env[env_name])
            continue
        if name not in variables:
            available = ", ".join(sorted(variables)) or "none"
            raise TemplateError(
                f"missing variable '{name}'"
                + _context_suffix(context)
                + f" (available: {available})"
            )
        value = variables[name]
        if isinstance(value, str):
            out.append(value)
            continue
        if isinstance(value, list):
            raise TemplateError(
                f"variable '{name}' is a list, not a scalar; use an extractor "
                "index or a command extractor to select one item"
                + _context_suffix(context)
            )
        raise TemplateError(
            f"variable '{name}' is a {type(value).__name__}, not a scalar string"
            + _context_suffix(context)
        )
    return "".join(out)


def render_json(
    value: Any,
    variables: Mapping[str, Any],
    *,
    context: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Any:
    """Recursively render string values inside a JSON-compatible value."""

    if isinstance(value, str):
        return render(value, variables, context=context, environ=environ)
    if isinstance(value, list):
        return [
            render_json(item, variables, context=context, environ=environ)
            for item in value
        ]
    if isinstance(value, dict):
        return {
            key: render_json(item, variables, context=context, environ=environ)
            for key, item in value.items()
        }
    return value


def _context_suffix(context: str | None) -> str:
    return f" in {context}" if context else ""
