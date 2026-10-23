from __future__ import annotations

import json
from pathlib import Path

from ufrd_updater.cli import main
from ufrd_updater.hashes import hashes_from_digests
from ufrd_updater.manifest import sha256_text

from .conftest import Response, parse_github_output


def _abc_hashes() -> dict[str, str]:
    import hashlib

    return hashes_from_digests(
        {algorithm: hashlib.new(algorithm, b"abc").digest() for algorithm in ("md5", "sha1", "sha256", "sha512")}
    )


def test_e2e_two_arches_one_changed(server, config_file, tmp_path, monkeypatch):
    server.route("/api", Response(body=json.dumps({"version": "1.0"}).encode()))
    server.route("/files/app-1.0.tar.gz", Response(body=b"abc"))
    server.route("/files/app-1.0-arm.tar.gz", Response(body=b"abc"))

    output = tmp_path / "dist"
    output.mkdir()
    archive_url = (
        "https://github.com/org/UfRd-Archive/releases/download/old-tag/app-1.0-arm.tar.gz"
    )
    source_url = server.url("/files/app-1.0-arm.tar.gz")
    hashes = _abc_hashes()
    (output / "manifest.json").write_text(
        json.dumps(
            {
                "demo": {
                    "aarch64-linux": {
                        "version": "1.0",
                        "url": archive_url,
                        "filename": "app-1.0-arm.tar.gz",
                        "hashes": hashes,
                    }
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (output / "state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {
                    "demo/aarch64-linux": {
                        "source_url_sha256": sha256_text(source_url),
                        "archive_url": archive_url,
                        "archive_repository": "org/UfRd-Archive",
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    config = config_file(
        f"""
version = 1

[settings]
output_dir = "dist"
concurrency = 2
retries = 0
timeout = 5.0
download_timeout = 5.0
connect_timeout = 5.0

[release]
tag_template = "ufrd-{{datetime}}-{{run_id}}"

[archive]
enabled = true
repository = "org/UfRd-Archive"

[[target]]
id = "demo"

[[target.request]]
url = "{server.base_url}/api"

[[target.request.extract]]
var = "version"
type = "json"
query = "version"

[[target.arch]]
id = "x86_64-linux"
url = "{server.base_url}/files/app-{{version}}.tar.gz"
filename = "app-{{version}}.tar.gz"

[[target.arch]]
id = "aarch64-linux"
url = "{server.base_url}/files/app-{{version}}-arm.tar.gz"
filename = "app-{{version}}-arm.tar.gz"
"""
    )
    github_output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))

    assert main(["run", "--config", str(config)]) == 0
    outputs = parse_github_output(github_output)
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest) == {"demo"}
    assert set(manifest["demo"]) == {"x86_64-linux", "aarch64-linux"}
    changed = manifest["demo"]["x86_64-linux"]
    assert changed["version"] == "1.0"
    assert changed["filename"] == "app-1.0.tar.gz"
    assert changed["hashes"] == hashes
    assert changed["url"].startswith(
        "https://github.com/org/UfRd-Archive/releases/download/"
    )
    assert changed["url"].endswith("/app-1.0.tar.gz")
    assert manifest["demo"]["aarch64-linux"]["url"] == archive_url

    file_path = output / "files" / "demo" / "x86_64-linux" / "app-1.0.tar.gz"
    assert file_path.read_bytes() == b"abc"
    assert not (output / "files" / "demo" / "aarch64-linux").exists()

    state = json.loads((output / "state.json").read_text(encoding="utf-8"))
    assert state["version"] == 1
    assert state["entries"]["demo/x86_64-linux"]["source_url_sha256"] == sha256_text(
        server.url("/files/app-1.0.tar.gz")
    )
    assert state["entries"]["demo/x86_64-linux"]["archive_repository"] == "org/UfRd-Archive"
    assert state["entries"]["demo/x86_64-linux"]["archive_url"] == changed["url"]

    releases = json.loads((output / "releases.json").read_text(encoding="utf-8"))
    assert releases["version"] == 1
    assert len(releases["run"]["tags"]) == 1
    assert releases["run"]["tag"] == releases["run"]["tags"][0]
    assert releases["run"]["archive"] is True
    assert releases["run"]["repository"] == "org/UfRd-Archive"
    assert len(releases["releases"]) == 1
    assets = releases["releases"][0]["assets"]
    assert len(assets) == 1
    assert assets[0]["target"] == "demo"
    assert assets[0]["arch"] == "x86_64-linux"
    assert assets[0]["sha256"] == hashes["sha256"]
    assert assets[0]["sha256_sri"] == hashes["sha256-sri"]
    assert assets[0]["url"] == changed["url"]
    assert assets[0]["path"] == str(file_path)
    assert assets[0]["asset_name"] == changed["filename"]

    urls = (output / "urls.txt").read_text(encoding="utf-8").splitlines()
    assert urls == [changed["url"]]
    assert (output / "update.md").read_text(encoding="utf-8").count("| demo |") == 1

    assert outputs["changed"] == "true"
    assert outputs["tag"] == releases["run"]["tag"]
    assert json.loads(outputs["tags"]) == releases["run"]["tags"]
    assert outputs["manifest"].endswith("dist/manifest.json")
    assert outputs["files"] == f"{file_path}\n"
    assert outputs["failed"] == "[]"
    assert outputs["failed_count"] == "0"
    assert outputs["commit"] == ""
    assert outputs["pushed"] == "false"

    downloads = server.count("/files/app-1.0.tar.gz") + server.count(
        "/files/app-1.0-arm.tar.gz"
    )
    assert downloads == 1

    second_output = tmp_path / "github_output_2"
    monkeypatch.setenv("GITHUB_OUTPUT", str(second_output))
    assert main(["run", "--config", str(config)]) == 0
    outputs = parse_github_output(second_output)
    assert outputs["changed"] == "false"
    assert outputs["tag"] == ""
    assert json.loads(outputs["tags"]) == []
    assert outputs["files"] == ""
    assert server.count("/files/app-1.0.tar.gz") + server.count(
        "/files/app-1.0-arm.tar.gz"
    ) == 1
    second_manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert second_manifest == manifest
    releases = json.loads((output / "releases.json").read_text(encoding="utf-8"))
    assert releases["releases"] == []
    assert releases["run"]["tag"] is None
    assert (output / "urls.txt").read_text(encoding="utf-8") == ""
    assert "No updates." in (output / "update.md").read_text(encoding="utf-8")


def test_e2e_archive_requires_repository(server, config_file, tmp_path):
    server.route("/api", Response(body=b'{"version": "1.0"}'))
    server.route("/app.tar.gz", Response(body=b"abc"))
    config = config_file(
        f"""
version = 1
[settings]
output_dir = "dist"
retries = 0
[archive]
enabled = true
[[target]]
id = "demo"
[[target.request]]
url = "{server.base_url}/api"
[[target.request.extract]]
var = "version"
type = "json"
query = "version"
[[target.arch]]
id = "x86_64-linux"
url = "{server.base_url}/app.tar.gz"
"""
    )
    assert main(["run", "--config", str(config)]) == 2
    assert not (tmp_path / "dist" / "manifest.json").exists()
    assert server.count("/app.tar.gz") == 0
