from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ufrd_updater.config import ReleaseConfig
from ufrd_updater.release import (
    ReleaseError,
    build_release_document,
    dedupe_assets,
    files_text,
    plan_tags,
    render_archive_url,
    safe_asset_name,
    sanitize_tag,
    urls_text,
    validate_tags,
)

from .helpers import abc_hashes, fixed_run, make_decision, make_entry, make_tagged

DEFAULT_URL_TEMPLATE = "https://github.com/{repository}/releases/download/{tag}/{filename}"


def plan(
    decisions,
    tag_template=None,
    target_templates=None,
    repository="org/UfRd-Archive",
    environ=None,
):
    targets = {
        decision.entry.target: SimpleNamespace(
            tag_template=(target_templates or {}).get(decision.entry.target, tag_template)
        )
        for decision in decisions
    }
    return plan_tags(
        decisions,
        release_config=ReleaseConfig(tag_template=tag_template or "ufrd-{datetime}-{run_id}"),
        targets_by_id=targets,
        run=fixed_run(repository),
        repository=repository,
        archive_url_template=DEFAULT_URL_TEMPLATE,
        environ=environ,
    )


def test_default_tag_is_shared_per_run():
    first = make_decision(entry=make_entry(target="one"))
    second = make_decision(entry=make_entry(target="two"))
    tagged = plan([first, second])
    assert {item.tag for item in tagged.values()} == {"ufrd-20261023-153012-42"}
    assert all(item.raw_tag == "ufrd-20261023-153012-42" for item in tagged.values())
    assert all(
        item.archive_url.startswith(
            "https://github.com/org/UfRd-Archive/releases/download/ufrd-20261023-153012-42/"
        )
        for item in tagged.values()
    )


def test_per_target_template_yields_multiple_tags():
    first = make_decision(entry=make_entry(target="one", version="1.0", filename="a.tar.gz"))
    second = make_decision(entry=make_entry(target="two", version="2.0", filename="b.tar.gz"))
    tagged = plan(
        [first, second],
        tag_template="{id}-{version}",
        target_templates={"two": "{id}-v{version}"},
    )
    assert tagged[first.key].tag == "one-1.0"
    assert tagged[second.key].tag == "two-v2.0"


def test_sanitize_tag_rules():
    assert sanitize_tag("my release/1..0") == "my-release-1.0"
    assert sanitize_tag("release.lock") == "release-lock"
    assert sanitize_tag("a/b:c d") == "a-b-c-d"
    assert sanitize_tag("foo..bar") == "foo.bar"
    assert sanitize_tag("..--..") == ""
    assert len(sanitize_tag("a" * 250)) == 200
    assert sanitize_tag("  .-" + "x" * 10 + "-.  ") == "x" * 10


def test_truncating_a_tag_cannot_create_a_lock_suffix():
    tag = sanitize_tag("a" * 195 + ".lockx")
    assert tag == "a" * 195 + "-lock"
    assert len(tag) == 200
    assert not tag.endswith(".lock")


@pytest.mark.parametrize("filename", ["app-1.0.tar.gz", "A_b.2", "x" * 200])
def test_safe_asset_names_stay_unchanged(filename):
    assert safe_asset_name(filename) == filename


@pytest.mark.parametrize(
    "filename",
    [
        "app ü.tar.gz",
        ".hidden",
        "file.",
        ".",
        "..",
        "a" * 250,
        "a/b#label[1]*.zip",
        chr(0xD800) + ".bin",
    ],
)
def test_unsafe_asset_names_are_stable_bounded_ascii(filename):
    mapped = safe_asset_name(filename)
    assert mapped == safe_asset_name(filename)
    assert mapped != filename
    assert mapped.isascii()
    assert all(char.isalnum() or char in "._-" for char in mapped)
    assert not mapped.startswith(".")
    assert not mapped.endswith(".")
    assert 0 < len(mapped) <= 200
    assert safe_asset_name(mapped) == mapped


def test_asset_name_normalization_distinguishes_similar_filenames():
    assert safe_asset_name("a b.bin") != safe_asset_name("a?b.bin")
    assert safe_asset_name("a b.bin") != safe_asset_name("a-b.bin")


def test_sanitized_tag_collision_errors():
    first = make_decision(entry=make_entry(target="one"))
    second = make_decision(entry=make_entry(target="two"))
    tagged = plan(
        [first, second],
        tag_template="a b",
        target_templates={"two": "a/b"},
    )
    with pytest.raises(ReleaseError, match="sanitize to the same release tag"):
        validate_tags(list(tagged.values()), pre_download=True)


def test_tag_diagnostics_never_include_rendered_environment_values(caplog):
    first = make_decision(entry=make_entry(target="one"))
    second = make_decision(entry=make_entry(target="two"))
    raw = "https://user:private-token@example.test/x?token=environment-secret"
    tagged = plan(
        [first, second],
        tag_template="{env:PRIVATE_TAG}",
        target_templates={"two": "{env:PRIVATE_TAG}/"},
        environ={"PRIVATE_TAG": raw},
    )
    with pytest.raises(ReleaseError) as caught:
        validate_tags(list(tagged.values()), pre_download=True)
    diagnostics = caplog.text + str(caught.value)
    assert "sanitized release tag" in diagnostics
    assert raw not in diagnostics
    assert "private-token" not in diagnostics
    assert "environment-secret" not in diagnostics


def test_empty_tag_diagnostic_does_not_include_raw_value():
    with pytest.raises(ReleaseError, match="empty tag") as caught:
        plan(
            [make_decision()],
            tag_template="{env:PRIVATE_TAG}",
            environ={"PRIVATE_TAG": "☃"},
        )
    assert "☃" not in str(caught.value)


def test_duplicate_filenames_are_compared_after_download():
    first = make_decision(entry=make_entry(target="one", filename="same.bin", source_url="https://a"))
    second = make_decision(entry=make_entry(target="two", filename="same.bin", source_url="https://b"))
    tagged = plan([first, second])
    validate_tags(list(tagged.values()), pre_download=True)
    validate_tags(list(tagged.values()), pre_download=False)

    same_source = make_decision(entry=make_entry(target="one", filename="same.bin", source_url="https://a"))
    same_source_two = make_decision(
        entry=make_entry(target="two", filename="same.bin", source_url="https://a")
    )
    validate_tags(list(plan([same_source, same_source_two]).values()), pre_download=True)

    different_hashes = make_decision(
        entry=make_entry(target="two", filename="same.bin"),
        hashes=dict(abc_hashes(), sha256="f" * 64),
    )
    with pytest.raises(ReleaseError, match="different assets"):
        validate_tags(list(plan([first, different_hashes]).values()), pre_download=False)


def test_archive_url_encoding_and_validation():
    url = render_archive_url(
        "app ü.tar.gz",
        "tag",
        "org/repo",
        template=DEFAULT_URL_TEMPLATE,
    )
    assert url == (
        "https://github.com/org/repo/releases/download/tag/"
        + safe_asset_name("app ü.tar.gz")
    )
    with pytest.raises(ReleaseError, match="http or https"):
        render_archive_url("f", "t", "r", template="ftp://x/{filename}")


def test_custom_github_archive_template_uses_mapped_asset_name():
    filename = "app ü.tar.gz"
    assert render_archive_url(
        filename,
        "tag",
        "org/repo",
        template="https://{env:ARCHIVE_HOST}/{repository}/releases/download/{tag}/{filename}?download=1",
        environ={"ARCHIVE_HOST": "GITHUB.COM"},
    ) == (
        "https://github.com/org/repo/releases/download/tag/"
        + safe_asset_name(filename)
        + "?download=1"
    )


def test_custom_non_github_archive_keeps_original_filename_semantics():
    assert render_archive_url(
        "app ü.tar.gz",
        "tag",
        "org/repo",
        template="https://archive.example.test/{tag}/{filename}",
    ) == "https://archive.example.test/tag/app%20%C3%BC.tar.gz"


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("https:/releases/release-1/file.bin?token=private-token", "include a host"),
        ("https://example.test:bad/file.bin?token=private-token", "invalid URL"),
        ("ftp://example.test/file.bin?token=private-token", "http or https"),
    ],
)
def test_invalid_archive_urls_fail_without_exposing_environment_values(url, message):
    with pytest.raises(ReleaseError, match=message) as caught:
        render_archive_url(
            "file.bin",
            "tag",
            "org/repo",
            template="{env:ARCHIVE}",
            environ={"ARCHIVE": url},
        )
    assert "private-token" not in str(caught.value)
    assert url not in str(caught.value)


def test_release_document_contents_and_paths(tmp_path):
    first = make_decision(
        entry=make_entry(target="alpha", arch="x86_64-linux", filename="a.tar.gz"),
        kind="new",
    )
    second = make_decision(
        entry=make_entry(target="beta", arch="aarch64-darwin", filename="b.tar.gz"),
        kind="content",
    )
    tagged = plan([second, first])
    document = build_release_document(
        list(tagged.values()),
        run=fixed_run(),
        repository="org/UfRd-Archive",
        archive_enabled=True,
        output_dir=tmp_path / "dist",
    )
    assert document["version"] == 1
    assert document["run"]["datetime"] == "2026-10-23T15:30:12Z"
    assert document["run"]["tag"] == "ufrd-20261023-153012-42"
    assert document["run"]["tags"] == ["ufrd-20261023-153012-42"]
    assert document["run"]["archive"] is True
    assert document["run"]["repository"] == "org/UfRd-Archive"
    assert len(document["releases"]) == 1
    assets = document["releases"][0]["assets"]
    assert [(asset["target"], asset["arch"]) for asset in assets] == [
        ("alpha", "x86_64-linux"),
        ("beta", "aarch64-darwin"),
    ]
    assert assets[0]["path"] == str(
        tmp_path / "dist/files/alpha/x86_64-linux/a.tar.gz"
    )
    assert assets[0]["filename"] == "a.tar.gz"
    assert assets[0]["asset_name"] == "a.tar.gz"
    assert assets[0]["url"].endswith("/a.tar.gz")
    assert assets[0]["sha256"] == abc_hashes()["sha256"]
    assert assets[0]["sha256_sri"] == abc_hashes()["sha256-sri"]


def test_release_plan_maps_asset_name_independently_from_local_filename(tmp_path):
    filename = "app ü.tar.gz"
    decision = make_decision(entry=make_entry(filename=filename))
    tagged = plan([decision])
    item = tagged[decision.key]
    assert item.asset_name == safe_asset_name(filename)
    document = build_release_document(
        [item],
        run=fixed_run(),
        repository="org/UfRd-Archive",
        archive_enabled=True,
        output_dir=tmp_path / "dist",
    )
    asset = document["releases"][0]["assets"][0]
    assert asset["filename"] == filename
    assert asset["asset_name"] == item.asset_name
    assert asset["path"] == str(
        tmp_path / "dist/files/demo/x86_64-linux" / filename
    )
    assert asset["url"].endswith("/" + item.asset_name)


def test_mapped_upload_identity_controls_collisions_and_deduplication():
    filename = "app ü.tar.gz"
    first = make_decision(entry=make_entry(target="alpha", filename=filename))
    second = make_decision(
        entry=make_entry(
            target="beta",
            filename=safe_asset_name(filename),
            source_url="https://mirror.test/same-content",
        )
    )
    tagged = list(plan([first, second]).values())
    assert tagged[0].asset_name == tagged[1].asset_name
    assert tagged[0].archive_url == tagged[1].archive_url
    validate_tags(tagged, pre_download=True)
    validate_tags(tagged, pre_download=False)
    assert dedupe_assets(tagged) == [tagged[0]]
    conflicting = make_decision(
        entry=second.entry,
        hashes=dict(abc_hashes(), sha256="f" * 64),
    )
    conflicts = list(plan([first, conflicting]).values())
    validate_tags(conflicts, pre_download=True)
    with pytest.raises(ReleaseError, match="different assets"):
        validate_tags(conflicts, pre_download=False)
    with pytest.raises(ReleaseError, match="different assets"):
        dedupe_assets(conflicts)


def test_relative_output_directory_exports_absolute_asset_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    decision = make_decision(entry=make_entry(filename="app.tar.gz"))
    tagged = plan([decision])
    output_dir = Path("nested/dist")
    document = build_release_document(
        list(tagged.values()),
        run=fixed_run(),
        repository="org/UfRd-Archive",
        archive_enabled=True,
        output_dir=output_dir,
    )
    expected = str(tmp_path / "nested/dist/files/demo/x86_64-linux/app.tar.gz")
    assert document["releases"][0]["assets"][0]["path"] == expected
    assert files_text(tagged, output_dir) == expected + "\n"


def test_identical_duplicate_assets_are_deduped(tmp_path):
    first = make_decision(
        entry=make_entry(
            target="alpha", filename="same.bin", source_url="https://one.test/same.bin"
        )
    )
    second = make_decision(
        entry=make_entry(
            target="beta", filename="same.bin", source_url="https://two.test/same.bin"
        )
    )
    tagged = plan([first, second])
    validate_tags(list(tagged.values()), pre_download=True)
    validate_tags(list(tagged.values()), pre_download=False)
    document = build_release_document(
        list(tagged.values()),
        run=fixed_run(),
        repository="org/UfRd-Archive",
        archive_enabled=False,
        output_dir=tmp_path / "dist",
    )
    assets = document["releases"][0]["assets"]
    assert len(assets) == 1
    assert assets[0]["target"] == "alpha"


def test_multiple_tags_run_tag_is_null(tmp_path):
    first = make_decision(entry=make_entry(target="one"))
    second = make_decision(entry=make_entry(target="two"))
    tagged = plan([first, second], tag_template="{id}")
    document = build_release_document(
        list(tagged.values()),
        run=fixed_run(),
        repository="org/UfRd-Archive",
        archive_enabled=False,
        output_dir=tmp_path / "dist",
    )
    assert document["run"]["tag"] is None
    assert document["run"]["tags"] == ["one", "two"]
    assert [release["tag"] for release in document["releases"]] == ["one", "two"]
    assert document["releases"][0]["assets"][0]["url"].startswith(
        "https://github.com/org/UfRd-Archive/releases/download/one/"
    )


def test_urls_files_and_empty_archive(tmp_path):
    first = make_decision(entry=make_entry(target="alpha", filename="a.tar.gz"))
    tagged = plan([first])
    archive_url = tagged[first.key].archive_url
    assert urls_text(tagged, archive_enabled=True) == f"{archive_url}\n"
    assert urls_text(tagged, archive_enabled=False) == f"{first.entry.source_url}\n"
    assert files_text(tagged, tmp_path / "dist") == (
        str(tmp_path / "dist/files/alpha/x86_64-linux/a.tar.gz") + "\n"
    )

    empty = make_tagged(archive_url=None)
    with pytest.raises(ReleaseError, match="no archive URL"):
        urls_text({empty.entry.key: empty}, archive_enabled=True)


def test_no_repository_means_null_asset_url(tmp_path):
    first = make_decision(entry=make_entry())
    tagged = plan([first], repository="")
    assert tagged[first.key].archive_url is None
    document = build_release_document(
        list(tagged.values()),
        run=fixed_run(repository=""),
        repository="",
        archive_enabled=False,
        output_dir=tmp_path / "dist",
    )
    assert document["run"]["repository"] == ""
    assert document["releases"][0]["assets"][0]["url"] is None
