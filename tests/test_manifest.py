from __future__ import annotations

import json

import pytest

from ufrd_updater.config import ConfigError
from ufrd_updater.manifest import (
    changed_state,
    classify,
    decide,
    invalid_hash_entries,
    load_manifest,
    load_state,
    make_entry,
    merge_manifest,
    prune,
    sha256_text,
    state_text,
    unchanged_state,
)

from .helpers import abc_hashes, make_entry as resolved, make_manifest_entry


def test_new_entry_downloads_and_changes():
    entry = resolved()
    decision = decide(
        entry, None, None, archive_enabled=False, repository="", force=False
    )
    assert decision.need_download is True
    assert decision.definite_kind == "new"

    decision = classify(
        decision, abc_hashes(), archive_enabled=False, repository=""
    )
    assert decision.changed is True
    assert decision.kind == "new"


def test_version_bump_changes():
    entry = resolved(version="2.0", filename="app-2.0.tar.gz")
    baseline = make_manifest_entry()
    decision = decide(
        entry,
        baseline,
        {"source_url_sha256": sha256_text(entry.source_url), "archive_url": "", "archive_repository": ""},
        archive_enabled=False,
        repository="",
        force=False,
    )
    assert decision.need_download is True
    assert decision.definite_kind == "version"
    decision = classify(decision, abc_hashes(), archive_enabled=False, repository="")
    assert decision.changed is True
    assert decision.kind == "version"


def test_filename_change_counts_as_version_change():
    entry = resolved(filename="new-name.tar.gz")
    baseline = make_manifest_entry()
    decision = decide(
        entry,
        baseline,
        {"source_url_sha256": sha256_text(entry.source_url), "archive_url": "", "archive_repository": ""},
        archive_enabled=False,
        repository="",
        force=False,
    )
    assert decision.need_download is True
    assert decision.definite_kind == "version"


def test_same_version_and_url_skips_download():
    entry = resolved()
    baseline = make_manifest_entry()
    state = {
        "source_url_sha256": sha256_text(entry.source_url),
        "archive_url": "",
        "archive_repository": "",
    }
    decision = decide(
        entry, baseline, state, archive_enabled=False, repository="", force=False
    )
    assert decision.need_download is False
    assert decision.definite_kind is None


def test_changed_source_url_with_equal_hashes_stays_unchanged():
    entry = resolved(source_url="https://src.test/new-url.tar.gz")
    baseline = make_manifest_entry()
    state = {
        "source_url_sha256": sha256_text("https://src.test/old-url.tar.gz"),
        "archive_url": "",
        "archive_repository": "",
    }
    decision = decide(
        entry, baseline, state, archive_enabled=False, repository="", force=False
    )
    assert decision.need_download is True
    assert decision.definite_kind is None

    decision = classify(
        decision, abc_hashes(), archive_enabled=False, repository=""
    )
    assert decision.changed is False
    assert decision.kind is None
    assert decision.baseline["url"] == baseline["url"]

    state_record = unchanged_state(decision, source_url=entry.source_url)
    assert state_record["source_url_sha256"] == sha256_text(entry.source_url)
    assert state_record["archive_url"] == ""


def test_changed_source_url_with_different_hashes_changes():
    entry = resolved(source_url="https://src.test/new-url.tar.gz")
    baseline = make_manifest_entry()
    state = {
        "source_url_sha256": sha256_text("https://src.test/old-url.tar.gz"),
        "archive_url": "",
        "archive_repository": "",
    }
    decision = decide(
        entry, baseline, state, archive_enabled=False, repository="", force=False
    )
    different = dict(abc_hashes())
    different["sha256"] = "f" * 64
    different["sha256-sri"] = "sha256-" + "A" * 43 + "="
    decision = classify(decision, different, archive_enabled=False, repository="")
    assert decision.changed is True
    assert decision.kind == "content"


def test_archive_bootstrap_forces_release():
    entry = resolved()
    baseline = make_manifest_entry()
    decision = decide(
        entry,
        baseline,
        None,
        archive_enabled=True,
        repository="org/archive",
        force=False,
    )
    assert decision.archive_bootstrap is True
    assert decision.need_download is True
    assert decision.definite_kind == "archive"

    decision = classify(
        decision, abc_hashes(), archive_enabled=True, repository="org/archive"
    )
    assert decision.changed is True
    assert decision.kind == "archive"


def test_archive_bootstrap_conditions():
    entry = resolved()
    archived = "https://github.com/org/archive/releases/download/t/app.tar.gz"
    baseline = make_manifest_entry(url=archived)

    matching = {
        "source_url_sha256": sha256_text(entry.source_url),
        "archive_url": archived,
        "archive_repository": "org/archive",
    }
    decision = decide(
        entry, baseline, matching, archive_enabled=True, repository="org/archive", force=False
    )
    assert decision.archive_bootstrap is False
    assert decision.need_download is False

    wrong_repo = dict(matching, archive_repository="org/other")
    assert decide(
        entry, baseline, wrong_repo, archive_enabled=True, repository="org/archive", force=False
    ).archive_bootstrap is True

    wrong_url = dict(matching, archive_url="https://example.test/old")
    assert decide(
        entry, baseline, wrong_url, archive_enabled=True, repository="org/archive", force=False
    ).archive_bootstrap is True

    assert decide(
        entry, baseline, matching, archive_enabled=False, repository="org/archive", force=False
    ).archive_bootstrap is False


def test_force_wins_even_with_identical_hashes():
    entry = resolved()
    baseline = make_manifest_entry()
    state = {
        "source_url_sha256": sha256_text(entry.source_url),
        "archive_url": "",
        "archive_repository": "",
    }
    decision = decide(
        entry, baseline, state, archive_enabled=False, repository="", force=True
    )
    assert decision.definite_kind == "forced"
    decision = classify(decision, abc_hashes(), archive_enabled=False, repository="")
    assert decision.changed is True
    assert decision.kind == "forced"


def test_merge_preserves_baseline_on_failure_and_unknown_entries():
    baseline = {
        "demo": {"x86_64-linux": make_manifest_entry()},
        "gone": {"aarch64-linux": make_manifest_entry()},
    }
    merged = merge_manifest(baseline, {})
    assert merged == baseline
    assert merged is not baseline
    assert merged["demo"] is not baseline["demo"]

    merged = merge_manifest(baseline, {"demo/x86_64-linux": make_entry(
        version="9", url="https://x", filename="x", hashes=abc_hashes()
    )})
    assert merged["demo"]["x86_64-linux"]["version"] == "9"
    assert merged["gone"] == baseline["gone"]


def test_prune_keeps_unselected_config_keys():
    manifest = {
        "demo": {"x86_64-linux": make_manifest_entry()},
        "skip": {"aarch64-linux": make_manifest_entry()},
        "stale": {"x86_64-linux": make_manifest_entry()},
    }
    state = {
        "demo/x86_64-linux": {"source_url_sha256": "a", "archive_url": "", "archive_repository": ""},
        "skip/aarch64-linux": {"source_url_sha256": "b", "archive_url": "", "archive_repository": ""},
        "stale/x86_64-linux": {"source_url_sha256": "c", "archive_url": "", "archive_repository": ""},
    }
    pruned, pruned_state = prune(
        manifest, state, {"demo/x86_64-linux", "skip/aarch64-linux"}
    )
    assert set(pruned) == {"demo", "skip"}
    assert set(pruned_state) == {"demo/x86_64-linux", "skip/aarch64-linux"}


def test_state_text_is_sorted_and_versioned():
    state = {
        "z/z": {"source_url_sha256": "2", "archive_url": "", "archive_repository": ""},
        "a/a": {"source_url_sha256": "1", "archive_url": "u", "archive_repository": "r"},
    }
    text = state_text(state)
    assert text.endswith("\n")
    assert list(json.loads(text)["entries"]) == ["a/a", "z/z"]
    assert json.loads(text)["version"] == 1
    assert changed_state("https://x", "https://a", "r") == {
        "source_url_sha256": sha256_text("https://x"),
        "archive_url": "https://a",
        "archive_repository": "r",
    }


def test_load_missing_files(tmp_path):
    assert load_manifest(tmp_path) == {}
    assert load_state(tmp_path) == {}


@pytest.mark.parametrize(
    "name,content",
    [
        ("manifest.json", "{not json"),
        ("manifest.json", "[]"),
        ("manifest.json", '{"demo": []}'),
        ("manifest.json", '{"demo": {"x86_64-linux": {"version": 1}}}'),
        ("state.json", "[]"),
        ("state.json", '{"version": 2, "entries": {}}'),
        ("state.json", '{"version": true, "entries": {}}'),
        ("state.json", '{"version": 1.0, "entries": {}}'),
        ("state.json", '{"version": 1, "entries": []}'),
        ("state.json", '{"version": 1, "entries": {"a/b": {"source_url_sha256": 1}}}'),
    ],
)
def test_malformed_data_is_fatal(tmp_path, name, content):
    (tmp_path / name).write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError):
        if name.startswith("manifest"):
            load_manifest(tmp_path)
        else:
            load_state(tmp_path)


def test_valid_manifest_and_state_load_unchanged(tmp_path):
    manifest = {
        "demo": {"x86_64-linux": make_manifest_entry()},
        "other": {"aarch64-linux": make_manifest_entry(version="2")},
    }
    state = {
        "demo/x86_64-linux": {
            "source_url_sha256": sha256_text("https://src.test/app-1.0.tar.gz"),
            "archive_url": "",
            "archive_repository": "",
        }
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "state.json").write_text(state_text(state), encoding="utf-8")

    loaded = load_manifest(tmp_path)

    assert loaded == manifest
    assert invalid_hash_entries(loaded) == set()
    assert load_state(tmp_path) == state


@pytest.mark.parametrize(
    "hashes",
    [
        None,
        {},
        [],
        "not-a-map",
        0,
        False,
        {key: "" for key in abc_hashes()},
        {key: value for key, value in abc_hashes().items() if key != "sha256"},
        {**abc_hashes(), "unexpected": "value"},
        {**abc_hashes(), "sha256": 1},
        {**abc_hashes(), "sha256": "invalid"},
        {**abc_hashes(), "sha256": "0" * 64},
        {**abc_hashes(), "sha256-nix32": "0" * 52},
        {**abc_hashes(), "sha256-sri": "sha256-" + "A" * 43 + "="},
    ],
)
def test_corrupt_hashes_are_retained_and_flagged_for_repair(tmp_path, hashes):
    broken = make_manifest_entry()
    broken["hashes"] = hashes
    manifest = {
        "demo": {"x86_64-linux": broken, "aarch64-linux": make_manifest_entry()},
        "other": {"x86_64-linux": make_manifest_entry()},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    loaded = load_manifest(tmp_path)

    assert loaded == manifest
    assert invalid_hash_entries(loaded) == {"demo/x86_64-linux"}


def test_missing_hashes_are_retained_and_flagged_for_repair(tmp_path):
    entry = make_manifest_entry()
    del entry["hashes"]
    manifest = {"demo": {"x86_64-linux": entry}}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    loaded = load_manifest(tmp_path)

    assert loaded == manifest
    assert "hashes" not in loaded["demo"]["x86_64-linux"]
    assert invalid_hash_entries(loaded) == {"demo/x86_64-linux"}


@pytest.mark.parametrize("field", ["version", "url", "filename"])
@pytest.mark.parametrize("value", [None, 1, [], {}])
def test_wrong_typed_structural_entry_fields_remain_fatal(tmp_path, field, value):
    entry = make_manifest_entry()
    entry[field] = value
    (tmp_path / "manifest.json").write_text(
        json.dumps({"demo": {"x86_64-linux": entry}}), encoding="utf-8"
    )

    with pytest.raises(ConfigError, match=f"'{field}' must be a string"):
        load_manifest(tmp_path)


@pytest.mark.parametrize("field", ["version", "url", "filename"])
def test_missing_structural_entry_fields_remain_fatal(tmp_path, field):
    entry = make_manifest_entry()
    del entry[field]
    (tmp_path / "manifest.json").write_text(
        json.dumps({"demo": {"x86_64-linux": entry}}), encoding="utf-8"
    )

    with pytest.raises(ConfigError, match=f"'{field}' must be a string"):
        load_manifest(tmp_path)


@pytest.mark.parametrize("loader,name", [(load_manifest, "manifest.json"), (load_state, "state.json")])
def test_non_utf8_baseline_is_a_configuration_error(tmp_path, loader, name):
    (tmp_path / name).write_bytes(b"\xff")

    with pytest.raises(ConfigError, match="invalid"):
        loader(tmp_path)
