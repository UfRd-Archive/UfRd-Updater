from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

import pytest

from ufrd_updater import cli as cli_module
from ufrd_updater.cli import main
from ufrd_updater.manifest import sha256_text

from .conftest import Response, parse_github_output
from .helpers import make_manifest_entry


def simple_config(base_url: str, *, path: str = "/file", filename: str | None = "file.bin") -> str:
    filename_line = f'filename = "{filename}"\n' if filename is not None else ""
    return f"""
version = 1
[settings]
output_dir = "dist"
retries = 0
timeout = 5.0
download_timeout = 5.0
connect_timeout = 5.0
[[target]]
id = "demo"
[[target.arch]]
id = "x86_64-linux"
version = "1.0"
url = "{base_url}{path}"
{filename_line}"""


def seed_baseline(output: Path, source_url: str, *, filename: str = "file.bin") -> None:
    output.mkdir(parents=True, exist_ok=True)
    entry = make_manifest_entry(url=source_url, filename=filename)
    manifest = {"demo": {"x86_64-linux": entry}}
    state = {
        "version": 1,
        "entries": {
            "demo/x86_64-linux": {
                "source_url_sha256": sha256_text(source_url),
                "archive_url": "",
                "archive_repository": "",
            }
        },
    }
    (output / "manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    (output / "state.json").write_text(json.dumps(state) + "\n", encoding="utf-8")


def test_failure_modes_preserve_baseline(server, config_file, tmp_path, monkeypatch):
    server.route("/file", Response(status=404, body=b"missing"))
    source = server.url("/file")
    output = tmp_path / "dist"
    seed_baseline(output, source)
    baseline_manifest = (output / "manifest.json").read_text(encoding="utf-8")
    baseline_state = (output / "state.json").read_text(encoding="utf-8")
    baseline_manifest_data = json.loads(baseline_manifest)
    baseline_state_data = json.loads(baseline_state)
    config = config_file(simple_config(server.base_url))

    github_output = tmp_path / "out1"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    assert main(["run", "--config", str(config), "--force", "--on-error", "continue"]) == 0
    outputs = parse_github_output(github_output)
    assert outputs["changed"] == "false"
    assert outputs["failed_count"] == "1"
    assert "HTTP 404" in json.loads(outputs["failed"])[0]["error"]
    assert json.loads((output / "manifest.json").read_text(encoding="utf-8")) == baseline_manifest_data
    assert json.loads((output / "state.json").read_text(encoding="utf-8")) == baseline_state_data
    assert "## Failures" in (output / "update.md").read_text(encoding="utf-8")

    second = tmp_path / "out2"
    monkeypatch.setenv("GITHUB_OUTPUT", str(second))
    assert main(["run", "--config", str(config), "--force", "--on-error", "fail"]) == 1
    assert parse_github_output(second)["failed_count"] == "1"

    rollback = tmp_path / "rollback"
    rollback.mkdir()
    (rollback / "manifest.json").write_text(baseline_manifest, encoding="utf-8")
    (rollback / "state.json").write_text(baseline_state, encoding="utf-8")
    rollback_config = config_file(
        simple_config(server.base_url).replace('output_dir = "dist"', 'output_dir = "rollback"')
    )
    rollback_output = tmp_path / "out3"
    monkeypatch.setenv("GITHUB_OUTPUT", str(rollback_output))
    assert main(["run", "--config", str(rollback_config), "--force", "--on-error", "rollback"]) == 1
    assert not rollback_output.exists()
    assert (rollback / "manifest.json").read_text(encoding="utf-8") == baseline_manifest
    assert not (rollback / "releases.json").exists()
    assert not (rollback / "update.md").exists()


def test_only_prune_preserves_unselected_targets(server, config_file, tmp_path):
    server.route("/keep", Response(body=b"abc"))
    output = tmp_path / "dist"
    output.mkdir()
    stale_entry = make_manifest_entry(url="https://stale.invalid/x", filename="x")
    skip_entry = make_manifest_entry(url="https://skip.invalid/y", filename="y")
    (output / "manifest.json").write_text(
        json.dumps(
            {
                "keep": {},
                "skip": {"aarch64-linux": skip_entry},
                "stale": {"x86_64-linux": stale_entry},
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
                    "skip/aarch64-linux": {
                        "source_url_sha256": "a",
                        "archive_url": "",
                        "archive_repository": "",
                    },
                    "stale/x86_64-linux": {
                        "source_url_sha256": "b",
                        "archive_url": "",
                        "archive_repository": "",
                    },
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
retries = 0
[[target]]
id = "keep"
[[target.arch]]
id = "x86_64-linux"
version = "1.0"
url = "{server.base_url}/keep"
filename = "keep.bin"
[[target]]
id = "skip"
[[target.arch]]
id = "aarch64-linux"
version = "1.0"
url = "https://skip.invalid/y"
filename = "y"
"""
    )
    assert main(["run", "--config", str(config), "--only", "keep", "--prune"]) == 0
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest) == {"keep", "skip"}
    assert manifest["skip"]["aarch64-linux"] == skip_entry
    assert "stale" not in manifest
    state = json.loads((output / "state.json").read_text(encoding="utf-8"))
    assert set(state["entries"]) == {"keep/x86_64-linux", "skip/aarch64-linux"}


def test_force_identical_bytes_is_changed(server, config_file, tmp_path, monkeypatch):
    server.route("/file", Response(body=b"abc"))
    config = config_file(simple_config(server.base_url))
    output = tmp_path / "dist"
    assert main(["run", "--config", str(config)]) == 0
    first = (output / "manifest.json").read_text(encoding="utf-8")

    github_output = tmp_path / "forced"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    assert main(["run", "--config", str(config), "--force"]) == 0
    outputs = parse_github_output(github_output)
    assert outputs["changed"] == "true"
    assert outputs["tag"] != ""
    assert server.count("/file") == 2
    assert (output / "manifest.json").read_text(encoding="utf-8") == first


def test_derived_filename_and_query_only_failure(server, config_file, tmp_path, monkeypatch):
    server.route("/app-1.0.tar.gz", Response(body=b"abc"))
    config = config_file(simple_config(server.base_url, path="/app-1.0.tar.gz", filename=None))
    assert main(["run", "--config", str(config)]) == 0
    manifest = json.loads((tmp_path / "dist" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["demo"]["x86_64-linux"]["filename"] == "app-1.0.tar.gz"

    server.route("/download", Response(body=b"abc"))
    query_config = config_file(
        simple_config(server.base_url, path="/?id=1", filename=None).replace(
            'output_dir = "dist"', 'output_dir = "query-dist"'
        )
    )
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "query-output"))
    assert main(["run", "--config", str(query_config), "--on-error", "continue"]) == 0
    text = (tmp_path / "query-dist" / "update.md").read_text(encoding="utf-8")
    assert "cannot derive a filename" in text
    assert not (tmp_path / "query-dist" / "files").exists()


pytestmark_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is required"
)


@pytestmark_git
def test_cli_commit_and_push(server, config_file, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "--initial-branch=main", str(bare)],
        check=True,
        cwd=tmp_path,
    )
    subprocess.run(["git", "init", "-q", "-b", "main"], check=True, cwd=repo)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], check=True, cwd=repo)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-q",
            "-m",
            "initial",
        ],
        check=True,
        cwd=repo,
    )
    subprocess.run(["git", "remote", "add", "origin", str(bare)], check=True, cwd=repo)

    server.route("/file", Response(body=b"abc"))
    config_path = repo / "updater.toml"
    config_path.write_text(simple_config(server.base_url), encoding="utf-8")
    github_output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))

    assert main(["run", "--config", str(config_path), "--commit", "--push"]) == 0
    outputs = parse_github_output(github_output)
    assert outputs["commit"]
    assert outputs["pushed"] == "true"

    tracked = subprocess.run(
        ["git", "ls-files", "dist"], check=True, capture_output=True, text=True, cwd=repo
    ).stdout.split()
    assert tracked == [
        "dist/fetchurl.nix",
        "dist/manifest.json",
        "dist/sources.nix",
        "dist/state.json",
    ]
    remote_head = subprocess.run(
        ["git", "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
        cwd=bare,
    ).stdout.strip()
    assert remote_head == outputs["commit"]


def test_step_summary_appended(server, config_file, tmp_path, monkeypatch):
    server.route("/file", Response(body=b"abc"))
    config = config_file(simple_config(server.base_url))
    summary = tmp_path / "summary.md"
    summary.write_text("prefix\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert main(["run", "--config", str(config)]) == 0
    text = summary.read_text(encoding="utf-8")
    assert text.startswith("prefix\n")
    assert "# UfRd update" in text


def test_verbose_logs_redact_urls_and_headers(server, config_file, tmp_path, caplog):
    server.route("/api", Response(body=b'{"version": "1.0"}'))
    server.route("/file", Response(body=b"abc"))
    config = config_file(
        f"""
version = 1
[settings]
output_dir = "dist"
retries = 0
[[target]]
id = "demo"
[[target.request]]
url = "{server.base_url}/api?token=secret"
headers = {{ Authorization = "Bearer supersecret" }}
[[target.request.extract]]
var = "version"
type = "json"
query = "version"
[[target.arch]]
id = "x86_64-linux"
url = "{server.base_url}/file"
filename = "file.bin"
"""
    )
    caplog.set_level(logging.DEBUG)
    assert main(["run", "--config", str(config), "--verbose"]) == 0
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "secret" not in text
    assert "supersecret" not in text
    assert "HTTP Request:" not in text


def test_archive_repository_env_fallback(server, config_file, tmp_path, monkeypatch):
    server.route("/file", Response(body=b"abc"))
    config = config_file(
        simple_config(server.base_url).replace(
            '[settings]', '[archive]\nenabled = true\n[settings]'
        )
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "org/EnvRepo")
    assert main(["run", "--config", str(config)]) == 0
    manifest = json.loads((tmp_path / "dist" / "manifest.json").read_text(encoding="utf-8"))
    url = manifest["demo"]["x86_64-linux"]["url"]
    assert url.startswith("https://github.com/org/EnvRepo/releases/download/")
    releases = json.loads((tmp_path / "dist" / "releases.json").read_text(encoding="utf-8"))
    assert releases["run"]["repository"] == "org/EnvRepo"


def test_malformed_baseline_fails_before_network(server, config_file, tmp_path):
    server.route("/file", Response(body=b"abc"))
    output = tmp_path / "dist"
    output.mkdir()
    (output / "manifest.json").write_text("{not json", encoding="utf-8")
    config = config_file(simple_config(server.base_url))
    assert main(["run", "--config", str(config)]) == 2
    assert server.count("/file") == 0
    assert (output / "manifest.json").read_text(encoding="utf-8") == "{not json"
    assert not (output / "state.json").exists()


@pytestmark_git
def test_push_failure_exports_local_commit(server, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], check=True, cwd=repo)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], check=True, cwd=repo)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-q",
            "-m",
            "initial",
        ],
        check=True,
        cwd=repo,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(tmp_path / "missing.git")],
        check=True,
        cwd=repo,
    )
    server.route("/file", Response(body=b"abc"))
    (repo / "updater.toml").write_text(simple_config(server.base_url), encoding="utf-8")
    github_output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))

    assert main(["run", "--config", str(repo / "updater.toml"), "--commit", "--push"]) == 1
    outputs = parse_github_output(github_output)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True, cwd=repo
    ).stdout.strip()
    assert outputs["commit"] == head
    assert outputs["pushed"] == "false"
    tracked = subprocess.run(
        ["git", "ls-files", "dist"], check=True, capture_output=True, text=True, cwd=repo
    ).stdout.split()
    assert tracked == [
        "dist/fetchurl.nix",
        "dist/manifest.json",
        "dist/sources.nix",
        "dist/state.json",
    ]


def output_snapshot(output: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(output)): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file() and ".tmp" not in path.relative_to(output).parts
    }


@pytest.mark.parametrize("failure_stage", ["prepare", "install"])
def test_output_batch_failure_restores_files_and_retry_updates(
    server, config_file, tmp_path, monkeypatch, failure_stage
):
    server.route("/old", Response(body=b"old bytes"))
    server.route("/new", Response(body=b"new bytes"))
    config = config_file(simple_config(server.base_url, path="/old"))
    assert main(["run", "--config", str(config)]) == 0
    output = tmp_path / "dist"
    before = output_snapshot(output)
    config = config_file(simple_config(server.base_url, path="/new"))
    github_output = tmp_path / "failed-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))

    with monkeypatch.context() as fault:
        if failure_stage == "prepare":
            original_write = cli_module.write_atomic

            def fail_prepare(path, text):
                if Path(path).name == "manifest.json":
                    raise OSError("injected write failure")
                return original_write(path, text)

            fault.setattr(cli_module, "write_atomic", fail_prepare)
        else:
            original_replace = cli_module.os.replace

            def fail_install(source, destination):
                source, destination = Path(source), Path(destination)
                if source.parent.name == ".exports" and destination == output / "manifest.json":
                    raise OSError("injected replacement failure")
                return original_replace(source, destination)

            fault.setattr(cli_module.os, "replace", fail_install)

        assert main(["run", "--config", str(config)]) == 1

    assert output_snapshot(output) == before
    assert not (output / ".tmp").exists()
    assert not github_output.exists()
    assert main(["run", "--config", str(config)]) == 0
    assert (output / "files/demo/x86_64-linux/file.bin").read_bytes() == b"new bytes"
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["demo"]["x86_64-linux"]["url"] == server.url("/new")
    assert server.count("/new") == 2


def test_failed_first_batch_leaves_no_exported_files(server, config_file, tmp_path, monkeypatch):
    server.route("/file", Response(body=b"new bytes"))
    config = config_file(simple_config(server.base_url))
    output = tmp_path / "dist"
    original_replace = cli_module.os.replace

    def fail_install(source, destination):
        source, destination = Path(source), Path(destination)
        if source.parent.name == ".exports" and destination == output / "state.json":
            raise OSError("injected replacement failure")
        return original_replace(source, destination)

    monkeypatch.setattr(cli_module.os, "replace", fail_install)
    assert main(["run", "--config", str(config)]) == 1
    assert output_snapshot(output) == {}
    assert not (output / ".tmp").exists()


def test_selected_corrupt_hashes_are_repaired(server, config_file, tmp_path, monkeypatch):
    server.route("/file", Response(body=b"abc"))
    config = config_file(simple_config(server.base_url))
    assert main(["run", "--config", str(config)]) == 0
    manifest_path = tmp_path / "dist/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_hashes = dict(manifest["demo"]["x86_64-linux"]["hashes"])
    manifest["demo"]["x86_64-linux"]["hashes"] = {}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    github_output = tmp_path / "repair-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))

    assert main(["run", "--config", str(config)]) == 0
    repaired = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert repaired["demo"]["x86_64-linux"]["hashes"] == expected_hashes
    assert server.count("/file") == 2
    assert parse_github_output(github_output)["changed"] == "true"
    assert 'hash = "";' not in (tmp_path / "dist/fetchurl.nix").read_text(encoding="utf-8")


def test_unselected_corrupt_hashes_stop_before_metadata(server, config_file, tmp_path):
    server.route("/api", Response(body=b"{}"))
    server.route("/file", Response(body=b"abc"))
    output = tmp_path / "dist"
    seed_baseline(output, server.url("/file"))
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    broken = make_manifest_entry()
    broken["hashes"] = {}
    manifest["skip"] = {"aarch64-linux": broken}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    before = output_snapshot(output)
    text = simple_config(server.base_url).replace(
        'id = "demo"\n',
        f'id = "demo"\n[[target.request]]\nurl = "{server.url("/api")}"\n',
    )
    text += f"""
[[target]]
id = "skip"
[[target.arch]]
id = "aarch64-linux"
version = "1.0"
url = "{server.url("/file")}"
filename = "skip.bin"
"""
    config = config_file(text)

    assert main(["run", "--config", str(config), "--only", "^demo$"]) == 2
    assert server.count("/api") == 0
    assert server.count("/file") == 0
    assert output_snapshot(output) == before


def test_failed_hash_repair_stops_exports_even_in_continue_mode(
    server, config_file, tmp_path, monkeypatch
):
    server.route("/file", Response(status=404))
    output = tmp_path / "dist"
    seed_baseline(output, server.url("/file"))
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["demo"]["x86_64-linux"]["hashes"] = {}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    before = output_snapshot(output)
    config = config_file(simple_config(server.base_url))
    github_output = tmp_path / "failed-repair-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))

    assert main(["run", "--config", str(config), "--on-error", "continue"]) == 1
    assert server.count("/file") == 1
    assert output_snapshot(output) == before
    assert not github_output.exists()


@pytest.mark.parametrize("invalid_url", ["http://host.invalid:bad/file", "http://["])
def test_invalid_url_is_an_isolated_safe_failure(
    server, config_file, tmp_path, monkeypatch, capsys, invalid_url
):
    server.route("/api", Response(body=b"{}"))
    server.route("/file", Response(body=b"abc"))
    text = simple_config(server.base_url).replace(
        'id = "demo"\n',
        f'id = "demo"\n[[target.request]]\nurl = "{server.url("/api")}"\n',
    )
    text = text.replace(server.url("/file"), invalid_url)
    text += f"""
[[target]]
id = "good"
[[target.arch]]
id = "x86_64-linux"
version = "1.0"
url = "{server.url("/file")}"
filename = "good.bin"
"""
    config = config_file(text)
    github_output = tmp_path / "isolated-url-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))

    assert main(["run", "--config", str(config), "--on-error", "continue"]) == 0
    outputs = parse_github_output(github_output)
    assert outputs["failed_count"] == "1"
    manifest = json.loads((tmp_path / "dist/manifest.json").read_text(encoding="utf-8"))
    assert set(manifest) == {"good"}
    assert server.count("/file") == 1
    assert "Traceback" not in capsys.readouterr().err


def test_request_and_download_header_overrides_are_case_insensitive(
    server, config_file
):
    received = {}

    def api(method, parsed, headers):
        received["metadata"] = headers.get_all("Authorization")
        return Response(body=b'{"version":"1"}')

    def download(method, parsed, headers):
        received["download"] = headers.get_all("Authorization")
        return Response(body=b"abc")

    server.route("/api", api)
    server.route("/file", download)
    config = config_file(f"""
version = 1
[settings]
retries = 0
[[target]]
id = "demo"
headers = {{ Authorization = "Bearer old" }}
[[target.request]]
url = "{server.url("/api")}"
headers = {{ authorization = "Bearer metadata" }}
[[target.request.extract]]
var = "version"
type = "json"
query = "version"
[[target.arch]]
id = "x86_64-linux"
url = "{server.url("/file")}"
filename = "file.bin"
headers = {{ AUTHORIZATION = "Bearer arch" }}
download_headers = {{ authorization = "Bearer download" }}
""")

    assert main(["run", "--config", str(config)]) == 0
    assert received == {
        "metadata": ["Bearer metadata"],
        "download": ["Bearer download"],
    }


@pytest.mark.parametrize("filename", ["../upstream.bin", ".app#1[2].zip"])
def test_upstream_filename_mapping_preserves_safe_local_paths(
    server, config_file, tmp_path, filename
):
    server.route("/api", Response(body=json.dumps({"filename": filename}).encode()))
    server.route("/file", Response(body=b"abc"))
    config = config_file(f"""
version = 1
[settings]
retries = 0
[archive]
enabled = true
repository = "org/archive"
[[target]]
id = "demo"
[[target.request]]
url = "{server.url("/api")}"
[[target.request.extract]]
var = "filename"
type = "json"
query = "filename"
[[target.arch]]
id = "x86_64-linux"
version = "1"
url = "{server.url("/file")}"
""")

    assert main(["run", "--config", str(config)]) == 0
    output = tmp_path / "dist"
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    entry = manifest["demo"]["x86_64-linux"]
    local = output / "files/demo/x86_64-linux" / entry["filename"]
    assert local.parent == output / "files/demo/x86_64-linux"
    assert local.read_bytes() == b"abc"
    release = json.loads((output / "releases.json").read_text(encoding="utf-8"))
    asset = release["releases"][0]["assets"][0]
    assert asset["path"] == str(local)
    assert entry["url"].endswith("/" + asset["asset_name"])
    if "/" in filename:
        assert entry["filename"] != filename
    else:
        assert entry["filename"] == filename
        assert asset["asset_name"] != filename
    assert main(["run", "--config", str(config)]) == 0
    assert server.count("/file") == 1


def test_rollback_failure_retains_original_asset_for_recovery(
    server, config_file, tmp_path, monkeypatch
):
    server.route("/old", Response(body=b"original bytes"))
    server.route("/new", Response(body=b"replacement bytes"))
    config = config_file(simple_config(server.base_url, path="/old"))
    assert main(["run", "--config", str(config)]) == 0
    output = tmp_path / "dist"
    asset = output / "files/demo/x86_64-linux/file.bin"
    config = config_file(simple_config(server.base_url, path="/new"))
    original_replace = cli_module.os.replace

    def fail_install_and_asset_restore(source, destination):
        source, destination = Path(source), Path(destination)
        if source.parent.name == ".exports" and destination == output / "manifest.json":
            raise OSError("injected install failure")
        if source.parent.name == ".backups" and destination == asset:
            raise OSError("injected restore failure")
        return original_replace(source, destination)

    monkeypatch.setattr(cli_module.os, "replace", fail_install_and_asset_restore)
    assert main(["run", "--config", str(config)]) == 1
    retained = list((output / ".tmp").glob("downloads-*/.backups/*"))
    assert any(path.read_bytes() == b"original bytes" for path in retained)


def test_prune_can_remove_corrupt_obsolete_entry(server, config_file, tmp_path):
    output = tmp_path / "dist"
    seed_baseline(output, server.url("/file"))
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    broken = make_manifest_entry()
    broken["hashes"] = {}
    manifest["obsolete"] = {"unused": broken}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    config = config_file(simple_config(server.base_url))

    assert main(["run", "--config", str(config), "--prune"]) == 0
    pruned = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(pruned) == {"demo"}
    assert pruned["demo"]["x86_64-linux"]["hashes"] == manifest["demo"]["x86_64-linux"]["hashes"]
    assert server.count("/file") == 0
