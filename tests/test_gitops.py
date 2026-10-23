from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ufrd_updater.gitops import (
    GitError,
    check_preconditions,
    commit_and_push,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")

EXPORTS = ("manifest.json", "sources.nix", "fetchurl.nix", "state.json")


def git(args: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=check
    )


def init_repo(path: Path) -> Path:
    path.mkdir()
    git(["init", "-q", "-b", "main"], path)
    (path / "README.md").write_text("base\n", encoding="utf-8")
    git(["add", "README.md"], path)
    git(
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-q",
            "-m",
            "initial",
        ],
        path,
    )
    return path


def write_exports(repo: Path) -> list[Path]:
    output = repo / "dist"
    output.mkdir(exist_ok=True)
    paths = []
    for name in EXPORTS:
        path = output / name
        path.write_text(f"{name}\n", encoding="utf-8")
        paths.append(path)
    return paths


def test_commit_only_export_paths_leaves_unrelated_staged(tmp_path):
    repo = init_repo(tmp_path / "repo")
    paths = write_exports(repo)
    unrelated = repo / "unrelated.txt"
    unrelated.write_text("keep me staged\n", encoding="utf-8")
    git(["add", "unrelated.txt"], repo)

    toplevel = check_preconditions(
        base_dir=repo, paths=paths, push_mode="false"
    )
    assert Path(toplevel) == repo.resolve()

    result = commit_and_push(
        toplevel=Path(toplevel),
        paths=paths,
        message="chore(ufrd): update sources 20261023-153012",
        user_name="github-actions[bot]",
        user_email="41898282+github-actions[bot]@users.noreply.github.com",
        push_mode="false",
    )
    assert result.commit is not None
    assert result.pushed is False

    committed = git(["show", "--name-only", "--format=", "HEAD"], repo).stdout.split()
    assert committed == sorted(f"dist/{name}" for name in EXPORTS)

    staged = git(["diff", "--cached", "--name-only"], repo).stdout.split()
    assert staged == ["unrelated.txt"]

    author = git(["log", "-1", "--format=%an <%ae>"], repo).stdout.strip()
    assert author == (
        "github-actions[bot] "
        "<41898282+github-actions[bot]@users.noreply.github.com>"
    )

    again = commit_and_push(
        toplevel=Path(toplevel),
        paths=paths,
        message="chore: no changes",
        user_name="github-actions[bot]",
        user_email="bot@example.test",
        push_mode="false",
    )
    assert again.commit is None
    assert again.pushed is False


def test_push_to_local_bare_remote(tmp_path):
    bare = tmp_path / "remote.git"
    git(["init", "-q", "--bare", "--initial-branch=main", str(bare)], tmp_path)
    repo = init_repo(tmp_path / "repo")
    git(["remote", "add", "origin", str(bare)], repo)
    paths = write_exports(repo)

    toplevel = check_preconditions(base_dir=repo, paths=paths, push_mode="true")
    result = commit_and_push(
        toplevel=Path(toplevel),
        paths=paths,
        message="chore: update",
        user_name="bot",
        user_email="bot@example.test",
        push_mode="true",
    )
    assert result.commit is not None
    assert result.pushed is True

    remote_head = git(["rev-parse", "refs/heads/main"], bare).stdout.strip()
    assert remote_head == result.commit


def test_auto_does_not_push_outside_actions(tmp_path):
    bare = tmp_path / "remote.git"
    git(["init", "-q", "--bare", "--initial-branch=main", str(bare)], tmp_path)
    repo = init_repo(tmp_path / "repo")
    git(["remote", "add", "origin", str(bare)], repo)
    paths = write_exports(repo)
    environ = dict(os.environ)
    environ.pop("GITHUB_ACTIONS", None)

    toplevel = check_preconditions(
        base_dir=repo, paths=paths, push_mode="auto", environ=environ
    )
    result = commit_and_push(
        toplevel=Path(toplevel),
        paths=paths,
        message="chore: update",
        user_name="bot",
        user_email="bot@example.test",
        push_mode="auto",
        environ=environ,
    )
    assert result.commit is not None
    assert result.pushed is False
    assert git(["rev-parse", "--verify", "refs/heads/main"], bare, check=False).returncode != 0


def test_detached_head_with_push_errors(tmp_path):
    repo = init_repo(tmp_path / "repo")
    paths = write_exports(repo)
    git(["checkout", "-q", "--detach"], repo)
    with pytest.raises(GitError, match="detached HEAD"):
        check_preconditions(base_dir=repo, paths=paths, push_mode="true")


def test_output_outside_worktree_errors(tmp_path):
    repo = init_repo(tmp_path / "repo")
    with pytest.raises(GitError, match="outside the git worktree"):
        check_preconditions(
            base_dir=repo,
            paths=[tmp_path / "elsewhere" / "manifest.json"],
            push_mode="false",
        )


def test_push_failure_preserves_local_commit(tmp_path):
    repo = init_repo(tmp_path / "repo")
    git(["remote", "add", "origin", str(tmp_path / "missing" / "remote.git")], repo)
    paths = write_exports(repo)
    toplevel = check_preconditions(base_dir=repo, paths=paths, push_mode="true")
    with pytest.raises(GitError) as error:
        commit_and_push(
            toplevel=Path(toplevel),
            paths=paths,
            message="chore: update",
            user_name="bot",
            user_email="bot@example.test",
            push_mode="true",
        )
    assert "permissions: contents: write" in str(error.value)
    assert "persist-credentials: true" in str(error.value)
    head = git(["rev-parse", "HEAD"], repo).stdout.strip()
    assert error.value.commit == head


def test_export_paths_are_literal_not_glob_patterns(tmp_path):
    repo = init_repo(tmp_path / "repo")
    wanted = repo / "out[1]" / "manifest.json"
    unrelated = repo / "out1" / "manifest.json"
    for path in (wanted, unrelated):
        path.parent.mkdir()
        path.write_text(path.parent.name, encoding="utf-8")

    result = commit_and_push(
        toplevel=repo,
        paths=[wanted],
        message="literal export",
        user_name="bot",
        user_email="bot@example.test",
        push_mode="false",
    )

    assert result.commit is not None
    assert git(["show", "--name-only", "--format=", "HEAD"], repo).stdout.splitlines() == [
        "out[1]/manifest.json"
    ]
    assert git(["ls-files", "--", "out1/manifest.json"], repo).stdout == ""


def test_ignored_exports_are_skipped_but_tracked_exports_commit(tmp_path, caplog):
    repo = init_repo(tmp_path / "repo")
    paths = write_exports(repo)
    git(["add", "dist/manifest.json"], repo)
    git(
        ["-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "track manifest"],
        repo,
    )
    (repo / ".gitignore").write_text("dist/\n", encoding="utf-8")
    paths[0].write_text("updated manifest\n", encoding="utf-8")
    caplog.set_level(logging.WARNING)

    result = commit_and_push(
        toplevel=repo,
        paths=paths,
        message="update committable exports",
        user_name="bot",
        user_email="bot@example.test",
        push_mode="false",
    )

    assert result.commit is not None
    assert git(["show", "--name-only", "--format=", "HEAD"], repo).stdout.splitlines() == [
        "dist/manifest.json"
    ]
    for path in paths[1:]:
        assert any(
            record.levelno == logging.WARNING and path.name in record.getMessage()
            for record in caplog.records
        )
        assert git(["ls-files", "--", str(path.relative_to(repo))], repo).stdout == ""


def test_all_ignored_exports_skip_commit_and_push_even_when_detached(tmp_path):
    repo = init_repo(tmp_path / "repo")
    paths = write_exports(repo)
    (repo / ".gitignore").write_text("dist/\n", encoding="utf-8")
    before = git(["rev-parse", "HEAD"], repo).stdout
    git(["checkout", "-q", "--detach"], repo)

    toplevel = check_preconditions(base_dir=repo, paths=paths, push_mode="true")
    result = commit_and_push(
        toplevel=Path(toplevel),
        paths=paths,
        message="nothing committable",
        user_name="bot",
        user_email="bot@example.test",
        push_mode="true",
    )

    assert result.commit is None
    assert result.pushed is False
    assert git(["rev-parse", "HEAD"], repo).stdout == before
    assert git(["ls-files", "dist"], repo).stdout == ""
