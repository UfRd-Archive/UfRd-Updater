"""GitHub Actions output-file and step-summary helpers."""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping


class ActionsError(Exception):
    """Raised when GITHUB_OUTPUT cannot be written (exit code 1)."""


def _delimiter(value: str) -> str:
    while True:
        delimiter = f"UFRD_EOF_{uuid.uuid4().hex}"
        if all(line != delimiter for line in value.splitlines()):
            return delimiter


def write_outputs(
    outputs: Mapping[str, str],
    *,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Append key/value pairs to GITHUB_OUTPUT using heredoc syntax."""

    env = os.environ if environ is None else environ
    path = env.get("GITHUB_OUTPUT")
    if not path:
        return
    chunks: list[str] = []
    for key, raw_value in outputs.items():
        value = str(raw_value)
        delimiter = _delimiter(value)
        chunks.append(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")
    try:
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write("".join(chunks))
    except OSError as exc:
        raise ActionsError(f"could not write GITHUB_OUTPUT '{path}': {exc}") from exc


def append_summary(
    markdown: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Append Markdown to GITHUB_STEP_SUMMARY; failures only warn."""

    env = os.environ if environ is None else environ
    path = env.get("GITHUB_STEP_SUMMARY")
    if not path:
        return True
    try:
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(markdown)
            if not markdown.endswith("\n"):
                handle.write("\n")
    except OSError:
        return False
    return True
