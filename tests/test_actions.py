from __future__ import annotations

import json

import pytest

from ufrd_updater.actions import ActionsError, append_summary, write_outputs
from ufrd_updater.cli import _outputs
from ufrd_updater.model import Failure
from ufrd_updater.gitops import CommitResult

from .conftest import parse_github_output
from .helpers import fixed_run, make_decision, make_entry, make_tagged


def test_write_outputs_uses_heredocs(tmp_path, monkeypatch):
    path = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(path))
    outputs = {
        "changed": "true",
        "tag": "",
        "tags": '["a", "b"]',
        "files": "dist/a.tar.gz\ndist/b.tar.gz\n",
        "failed": "[]",
        "failed_count": "0",
        "commit": "",
        "pushed": "false",
    }
    write_outputs(outputs)
    assert parse_github_output(path) == outputs


def test_write_outputs_without_env_is_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    write_outputs({"changed": "false"})
    assert list(tmp_path.iterdir()) == []


def test_write_outputs_failure_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path))
    with pytest.raises(ActionsError):
        write_outputs({"changed": "true"})


def test_outputs_contract_for_multiple_tags(tmp_path):
    first = make_tagged(
        decision=make_decision(entry=make_entry(target="alpha", filename="a.tar.gz")),
        tag="tag-one",
    )
    second = make_tagged(
        decision=make_decision(entry=make_entry(target="beta", filename="b.tar.gz")),
        tag="tag-two",
    )
    tagged = {first.entry.key: first, second.entry.key: second}
    failures = [Failure("demo", "aarch64-linux", "boom https://example.test/x?token=secret")]
    outputs = _outputs(
        run=fixed_run(),
        tagged=tagged,
        changed_count=2,
        failures=failures,
        output_dir=tmp_path / "dist",
        commit_result=CommitResult(commit="deadbeef", pushed=True),
    )
    assert outputs["changed"] == "true"
    assert outputs["tag"] == ""
    assert json.loads(outputs["tags"]) == ["tag-one", "tag-two"]
    assert outputs["manifest"] == str(tmp_path / "dist/manifest.json")
    assert outputs["sources"] == str(tmp_path / "dist/sources.nix")
    assert outputs["fetchurl"] == str(tmp_path / "dist/fetchurl.nix")
    assert outputs["state"] == str(tmp_path / "dist/state.json")
    assert outputs["releases"] == str(tmp_path / "dist/releases.json")
    assert outputs["urls"] == str(tmp_path / "dist/urls.txt")
    assert outputs["summary"] == str(tmp_path / "dist/update.md")
    assert outputs["files"] == (
        f"{tmp_path}/dist/files/alpha/x86_64-linux/a.tar.gz\n"
        f"{tmp_path}/dist/files/beta/x86_64-linux/b.tar.gz\n"
    )
    assert json.loads(outputs["failed"]) == [
        {"target": "demo", "arch": "aarch64-linux", "error": "boom https://example.test/x"}
    ]
    assert outputs["failed_count"] == "1"
    assert outputs["commit"] == "deadbeef"
    assert outputs["pushed"] == "true"


def test_summary_appends(tmp_path, monkeypatch):
    path = tmp_path / "summary.md"
    path.write_text("existing\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))
    assert append_summary("## New\n\ncontent\n") is True
    text = path.read_text(encoding="utf-8")
    assert text.startswith("existing\n")
    assert text == "existing\n## New\n\ncontent\n"


def test_summary_failure_only_warns(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path))
    assert append_summary("text") is False
