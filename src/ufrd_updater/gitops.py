"""Git commit/push support with narrow path-scoped commits."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .http import redact_text

LOG = logging.getLogger("ufrd-updater")

PUSH_HINT = (
    "check the workflow permissions: set `permissions: contents: write` and "
    "checkout with `persist-credentials: true`"
)


class GitError(Exception):
    """Raised when git preconditions or operations fail.

    ``commit`` is set when a commit was created but the subsequent push failed,
    so the caller can still export the new SHA.
    """

    def __init__(self, message: str, *, commit: str | None = None) -> None:
        super().__init__(message)
        self.commit = commit


@dataclass(frozen=True)
class CommitResult:
    commit: str | None
    pushed: bool


def _git(
    args: list[str],
    *,
    cwd: Path,
    environ: Mapping[str, str] | None = None,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ if environ is None else environ)
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            input=input_text,
        )
    except FileNotFoundError as exc:
        raise GitError("git is not installed or not on PATH") from exc
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise GitError(f"git {' '.join(args)} failed: {detail}")
    return completed


def resolve_branch(
    *,
    cwd: Path,
    environ: Mapping[str, str] | None = None,
) -> str:
    env = os.environ if environ is None else environ
    ref = env.get("GITHUB_REF", "")
    if ref.startswith("refs/heads/"):
        name = env.get("GITHUB_REF_NAME") or ref[len("refs/heads/") :]
        if name:
            return name
    completed = _git(["symbolic-ref", "--short", "HEAD"], cwd=cwd, environ=env, check=False)
    branch = completed.stdout.strip()
    if completed.returncode != 0 or not branch:
        raise GitError(
            "cannot push from a detached HEAD; check out a branch or set GITHUB_REF"
        )
    return branch


def check_preconditions(
    *,
    base_dir: Path,
    paths: Iterable[Path],
    push_mode: str,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Validate git availability, worktree membership and push branch."""

    if shutil.which("git") is None:
        raise GitError("git is not installed or not on PATH")
    env = os.environ if environ is None else environ
    completed = _git(["rev-parse", "--show-toplevel"], cwd=base_dir, environ=env, check=False)
    if completed.returncode != 0:
        raise GitError(f"'{base_dir}' is not inside a git worktree")
    toplevel = Path(completed.stdout.strip()).resolve()
    relative = _relative_paths(toplevel, paths)
    committable = _committable_paths(toplevel, relative, env, warn=False)
    may_push = push_mode == "true" or (
        push_mode == "auto" and env.get("GITHUB_ACTIONS") == "true"
    )
    if may_push and committable:
        resolve_branch(cwd=toplevel, environ=env)
    return str(toplevel)


def _relative_paths(toplevel: Path, paths: Iterable[Path]) -> list[str]:
    relative = []
    for path in paths:
        absolute = Path(path).resolve()
        try:
            relative.append(str(absolute.relative_to(toplevel)))
        except ValueError as exc:
            raise GitError(
                f"output path '{absolute}' is outside the git worktree '{toplevel}'"
            ) from exc
    return relative


def _committable_paths(
    toplevel: Path,
    relative: list[str],
    environ: Mapping[str, str],
    *,
    warn: bool,
) -> list[str]:
    if not relative:
        return []
    ignored = _git(
        ["check-ignore", "-z", "--stdin"],
        cwd=toplevel,
        environ=environ,
        check=False,
        input_text="\0".join(relative) + "\0",
    )
    if ignored.returncode not in (0, 1):
        detail = (ignored.stderr or ignored.stdout).strip()
        raise GitError(f"git could not determine which export paths are ignored: {detail}")
    ignored_paths = set(ignored.stdout.rstrip("\0").split("\0")) if ignored.stdout else set()
    if warn:
        for path in relative:
            if path in ignored_paths:
                LOG.warning("skipping ignored export: %s", redact_text(path))
    return [path for path in relative if path not in ignored_paths]


def commit_and_push(
    *,
    toplevel: Path,
    paths: Iterable[Path],
    message: str,
    user_name: str,
    user_email: str,
    push_mode: str,
    environ: Mapping[str, str] | None = None,
) -> CommitResult:
    """Commit only the given paths and optionally push the current branch."""

    env = dict(os.environ if environ is None else environ)
    root = Path(toplevel)
    relative = _committable_paths(
        root, _relative_paths(root, paths), env, warn=True
    )
    if not relative:
        return CommitResult(commit=None, pushed=False)

    tracked_output = _git(
        ["--literal-pathspecs", "ls-files", "-z", "--", *relative],
        cwd=root,
        environ=env,
    ).stdout
    tracked = set(tracked_output.rstrip("\0").split("\0")) if tracked_output else set()
    tracked_paths = [path for path in relative if path in tracked]
    new_paths = [path for path in relative if path not in tracked]
    if tracked_paths:
        _git(
            ["--literal-pathspecs", "add", "-u", "--", *tracked_paths],
            cwd=root,
            environ=env,
        )
    if new_paths:
        _git(
            ["--literal-pathspecs", "add", "-A", "--", *new_paths],
            cwd=root,
            environ=env,
        )
    diff = _git(
        ["--literal-pathspecs", "diff", "--cached", "--quiet", "--", *relative],
        cwd=root,
        environ=env,
        check=False,
    )
    if diff.returncode == 0:
        return CommitResult(commit=None, pushed=False)
    if diff.returncode != 1:
        detail = (diff.stderr or diff.stdout).strip()
        raise GitError(f"git diff --cached failed: {detail}")

    _git(
        [
            "--literal-pathspecs",
            "-c",
            f"user.name={user_name}",
            "-c",
            f"user.email={user_email}",
            "commit",
            "--only",
            "-m",
            message,
            "--",
            *relative,
        ],
        cwd=root,
        environ=env,
    )
    commit = _git(["rev-parse", "HEAD"], cwd=root, environ=env).stdout.strip()

    should_push = push_mode == "true" or (
        push_mode == "auto" and env.get("GITHUB_ACTIONS") == "true"
    )
    if not should_push:
        return CommitResult(commit=commit, pushed=False)

    branch = resolve_branch(cwd=root, environ=env)
    push_env = dict(env)
    push_env["GIT_TERMINAL_PROMPT"] = "0"
    pushed = _git(
        ["push", "origin", f"HEAD:refs/heads/{branch}"],
        cwd=root,
        environ=push_env,
        check=False,
    )
    if pushed.returncode != 0:
        detail = (pushed.stderr or pushed.stdout).strip()
        raise GitError(
            f"git push failed: {detail}; {PUSH_HINT}", commit=commit
        )
    return CommitResult(commit=commit, pushed=True)
