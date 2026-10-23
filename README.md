# UfRd-Updater

[English](README.md) | [简体中文](README.zh-CN.md)

[![CI](https://github.com/UfRd-Archive/UfRd-Updater/actions/workflows/ci.yml/badge.svg)](https://github.com/UfRd-Archive/UfRd-Updater/actions/workflows/ci.yml)

UfRd-Updater keeps a Nix package archive current. You describe where versions
and downloads live in one TOML file; the updater resolves them, downloads only
what changed, hashes every artifact, and writes a deterministic manifest plus
Nix expressions, a release plan, and a human-readable summary.

It runs as a standalone CLI and as a reusable GitHub Action.

## Quick start

```bash
uv tool install .
ufrd-updater validate --config updater.toml
ufrd-updater list --config updater.toml
ufrd-updater run --config updater.toml
```

`run` is the default command, so `ufrd-updater --config updater.toml` does the
same thing.

A minimal configuration:

```toml
version = 1

[[target]]
id = "example-app"

[[target.request]]
url = "https://example.com/api/latest"

[[target.request.extract]]
var = "version"
type = "json"
query = "version"

[[target.arch]]
id = "x86_64-linux"
url = "https://example.com/releases/{version}/app-linux-x64.tar.gz"
filename = "app-{version}-linux-x64.tar.gz"
```

Every path inside the configuration resolves relative to the TOML file, not to
your shell. The default output directory is `dist/` next to it.

## What a run does

1. Parses and validates the whole configuration. Unknown keys and bad types stop
   the run before any network request.
2. Runs target requests once, then each arch's requests, collecting variables
   with regex, JSON, HTML, or command extractors.
3. Renders each arch's version, download URL, filename, and headers.
4. Compares against the previous manifest and state. Entries whose version,
   filename, and source URL are unchanged skip the download.
5. Downloads candidates in parallel, hashes them in one pass, and keeps the
   previous entry when the bytes did not change.
6. Writes the new manifest, Nix files, state, release plan, URL list, and
   summary as one batch.
7. Optionally commits the four exported files and pushes.

## CLI

| Flag | Meaning |
| --- | --- |
| `--config PATH` | Configuration file. Default: `updater.toml`. |
| `--only REGEX` | Run targets whose id matches the regex. |
| `--force` | Treat every selected entry as changed: download, release, and rewrite its archive URL. |
| `--prune` | Drop manifest and state entries that are absent from the full configuration. Targets excluded by `--only` stay. |
| `--on-error MODE` | Override `settings.on_error`: `fail`, `continue`, or `rollback`. |
| `--commit` / `--no-commit` | Override `git.commit`. |
| `--push` / `--no-push` | Override `git.push`. `--push` means `true` and requires commit. |
| `--verbose` | Debug logging. URLs and header values stay redacted. |

Exit codes:

| Code | Meaning |
| --- | --- |
| `0` | Success. `continue` mode returns `0` for ordinary per-entry failures. |
| `1` | Per-entry failure in `fail` mode, failed hash repair, commit/push/output failure, or a triggered `rollback`. |
| `2` | Configuration or usage error. Nothing was downloaded or committed. |

`validate` parses the configuration and checks the output directory, without
network access. `list` prints every configured target/arch pair.

## Configuration

The file starts with `version = 1`. Unknown keys are errors. Four optional
tables control global behavior.

### `[settings]`

| Key | Default | Meaning |
| --- | --- | --- |
| `output_dir` | `"dist"` | Output directory, relative to the configuration file. |
| `concurrency` | `4` | Parallel download workers, at least 1. |
| `retries` | `3` | Retries per request and download, at least 0. |
| `timeout` | `30.0` | Metadata read timeout in seconds. |
| `download_timeout` | `120.0` | Download read timeout in seconds. |
| `connect_timeout` | `10.0` | Connection timeout in seconds. |
| `user_agent` | `"ufrd-updater/0.1.0"` | Default `User-Agent`. Must be an ASCII HTTP header value. |
| `on_error` | `"fail"` | `fail`, `continue`, or `rollback`. |

All timeouts are finite positive numbers.

### `[git]`

| Key | Default | Meaning |
| --- | --- | --- |
| `commit` | `false` | Commit the exported data after a run. |
| `push` | `"auto"` | `auto`, `true`, or `false`. |
| `message` | `"chore(ufrd): update sources {datetime}"` | Commit message template. |
| `user_name` | `"github-actions[bot]"` | Commit author name. |
| `user_email` | `"41898282+github-actions[bot]@users.noreply.github.com"` | Commit author email. |

`auto` pushes inside GitHub Actions when `GITHUB_ACTIONS=true`. `true` pushes
everywhere when commit is enabled. A configuration with `push = true` and
`commit = false` is rejected; pass `--commit` or enable commit. The updater
passes the identity with `-c` and leaves global Git configuration alone.

### `[release]`

| Key | Default | Meaning |
| --- | --- | --- |
| `tag_template` | `"ufrd-{datetime}-{run_id}"` | Tag template for changed assets. A target can override it. |

### `[archive]`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Point the manifest and `fetchurl.nix` at release assets. |
| `repository` | `""` | `owner/repo`. Falls back to `GITHUB_REPOSITORY`. |
| `url_template` | `"https://github.com/{repository}/releases/download/{tag}/{filename}"` | Archive asset URL. |

`enabled = true` requires a repository whenever at least one selected entry is
new or changed.

### `[[target]]`

```toml
[[target]]
id = "example-app"
description = "optional documentation only"
headers = { Referer = "https://example.com/" }
download_headers = { Authorization = "Bearer {env:ARCHIVE_TOKEN}" }
timeout = 30.0
retries = 3
tag_template = "example-app-{version}"
vars = { channel = "stable" }
```

Each target needs at least one `[[target.arch]]`. Target ids are unique.

### Requests and extractors

A target-level request runs once. An arch-level request runs for each arch.
Variables extracted by a request enter the target/arch scope; `vars` and
extracted values share one namespace, and later writes win.

```toml
[[target.request]]
url = "https://example.com/api/latest"
method = "GET"                # GET, POST, or HEAD
headers = { Accept = "application/json" }
follow_redirects = true
expect_status = [200]         # a single int or a list; default: any 2xx
# body = 'literal or {templated}'
# json = { arch = "x86_64" }  # body and json are mutually exclusive
encoding = "utf-8"            # text/command body decode override

[[target.request.extract]]
var = "version"
type = "json"
query = "latest.version"
```

Extractor fields:

| Field | Meaning |
| --- | --- |
| `var` | Variable name to write. Required. |
| `type` | Required: `regex`, `json`, `html`, or `command`. |
| `from` | Input for regex: `body` (default), `header`, `url`, or `var`. |
| `source` | Source variable for `from = "var"`. |
| `name` | Header name for `from = "header"`. |
| `pattern`, `group`, `flags` | Regex pattern, capture group, and flags (`i`, `m`, `s`, `x`). |
| `query` | JMESPath expression for JSON. |
| `selector`, `attr` | CSS selector and optional attribute for HTML. |
| `all`, `index` | Collect every match, or select one by index. |
| `strip` | Trim string results. HTML defaults to true; regex defaults depend on the source. |
| `required` | Fail the request when nothing was found and no default exists. |
| `default` | Value used when nothing was found. |
| `command`, `timeout` | Shell command for `command` extractors, and its timeout. |

Extractors run in order against a staging map. If one fails, the request's new
variables are discarded and the previous scope stays intact. A JSON `null` is
"no result", including when selected with `index`.

`command` runs `sh -c` in the configuration directory with `stdin` closed and
receives `UFRD_BODY`, `UFRD_URL`, `UFRD_STATUS`, `UFRD_HEADERS`, `UFRD_VARS`,
and `UFRD_VAR_<NAME>` for scalar variables. A timeout terminates the whole
process group, not just the outer shell.

### Arches

```toml
[[target.arch]]
id = "x86_64-linux"
version = "{version}"          # defaults to the extracted "version"
url = "https://example.com/{version}/app-linux-x64.tar.gz"
filename = "app-{version}-x64.tar.gz"  # defaults to var "filename", then URL basename
headers = { }
download_headers = { }
vars = { }
```

`id` is normally a Nix system string. The URL is required. Resolution order is
`vars`, arch requests, `version`, URL, then filename. An arch request therefore
feeds `version`; a static `arch.version` does not feed an earlier arch request.
Put a known version in `vars` when a request needs it.

Header names are case-insensitive. Metadata requests merge target headers,
arch headers, then request headers. Downloads merge target headers, arch
headers, target `download_headers`, then arch `download_headers`, and finish
with `Accept-Encoding: identity`.

Common headers follow the artifact URL. If an upstream controls that URL, it
controls where those headers go. Keep metadata-only credentials in the request
that needs them, and reserve `download_headers` for trusted destinations.
Cross-origin redirects drop `Authorization` and cookies. Other custom secret
headers travel through redirects, so the destination must be trusted.

### Templates

`url`, header values, `body`, strings inside `json`, `vars`, `version`,
`filename`, `tag_template`, `archive.url_template`, and `git.message` use
`{name}` interpolation. `{{` and `}}` produce literal braces. `{env:NAME}` reads
an environment variable. Unknown names fail the relevant operation and list the
available names.

Built-ins: `{id}`, `{arch}` (arch scopes), and for tags `{datetime}` in
`YYYYMMDD-HHMMSS` UTC, `{date}`, `{time}`, `{run_id}`, `{run_number}`, `{sha}`
(the first 7 characters of `GITHUB_SHA`, or `local` outside Actions),
`{repository}`, `{version}`, and `{filename}`. Commit messages also accept
`{tags}`, `{tag}`, and `{changed_count}`.

Locally safe filenames stay as they are. Path separators, control characters,
invalid Unicode, and names longer than the filesystem limit are replaced with a
deterministic safe filename inside the target/arch directory. An empty filename
still needs an explicit name or a URL basename.

## Outputs

```text
dist/
  manifest.json   public source database (committed)
  sources.nix     pure data mirroring manifest.json (committed)
  fetchurl.nix    { fetchurl }: { ... } (committed)
  state.json      internal fingerprints (committed)
  releases.json   this run's release plan
  urls.txt        manifest URLs of changed entries
  update.md       human-readable summary
  files/<target>/<arch>/<filename>
```

`manifest.json` is keyed by target and then arch. Each entry holds `version`,
`url`, `filename`, and twelve hashes: md5, sha1, sha256, and sha512, each in
base16, Nix base32 (`-nix32`), and SRI (`-sri`). JSON uses two-space indentation
and a trailing newline.

`state.json` stores `source_url_sha256`, `archive_url`, and
`archive_repository` per entry. It lets unchanged entries skip downloads.

Change detection compares version and filename first, then the resolved source
URL hash. A candidate whose twelve hashes match the baseline stays unchanged.
Enabling archive, switching its repository, or creating archive state for an
existing entry marks that entry for release even when its bytes match, so the
manifest and `fetchurl.nix` pick up the new archive URLs. Disabling archive
leaves existing archive URLs in place; run `--force` to rewrite them back to
source URLs. `--force` refreshes every selected entry. Entries missing from the
configuration stay in the manifest unless you pass `--prune`; `--only` never
prunes unselected targets.

The updater validates the full twelve-hash set. If a selected entry has missing,
malformed, or inconsistent hashes, that entry is downloaded again even when its
version and URL match. A corrupt unselected entry stops the run before any
network access, because keeping it would export invalid data. Corrupt obsolete
entries removed by `--prune` are simply dropped. When a repair fails, no exports
are installed and the run exits `1`, including in `continue` mode.

All output files and changed assets are prepared first, then installed together.
If a replacement fails, the previous batch is restored, including assets that
were already replaced. If restoring also fails, the error prints the
`output_dir/.tmp/` directory holding the previous files. Keep that directory for
recovery.

Every path in `GITHUB_OUTPUT`, every release asset `path`, and the `files` list
is absolute. Nested configuration directories and nested `output_dir` values
work without extra path arithmetic in workflows.

## Release tags and asset names

Changed assets are grouped by a release tag from the target or global template.
Invalid tag characters become `-`, consecutive `-` collapse, `..` collapses to
`.`, leading and trailing dots and dashes are trimmed, and the tag is limited to
200 characters. A tag that ends in `.lock` gets a `-lock` suffix after
truncation, so it stays a valid Git ref.

Each release asset has three names:

| Field | Meaning |
| --- | --- |
| `filename` | The local artifact name and the name recorded in the manifest. |
| `asset_name` | The GitHub asset name. Safe names are unchanged; unsafe names become bounded ASCII with a deterministic digest suffix. |
| `path` | Absolute local path to the artifact. |

GitHub archive URLs, upload collisions, and publication use `asset_name`.
Custom non-GitHub URL templates keep the original filename. Two assets with the
same tag and upload name must have identical bytes; different content is an
error. Identical bytes from different source URLs are deduplicated after
download.

`releases.json` looks like this:

```json
{
  "version": 1,
  "run": {
    "datetime": "2026-10-24T03:17:00Z",
    "tag": "ufrd-20261024-031700-1234",
    "tags": ["ufrd-20261024-031700-1234"],
    "archive": true,
    "repository": "UfRd-Archive/archive"
  },
  "releases": [
    {
      "tag": "ufrd-20261024-031700-1234",
      "assets": [
        {
          "target": "example-app",
          "arch": "x86_64-linux",
          "version": "2.4.1",
          "filename": "app-2.4.1-linux-x64.tar.gz",
          "asset_name": "app-2.4.1-linux-x64.tar.gz",
          "path": "/home/runner/work/archive/dist/files/example-app/x86_64-linux/app-2.4.1-linux-x64.tar.gz",
          "url": "https://github.com/UfRd-Archive/archive/releases/download/ufrd-20261024-031700-1234/app-2.4.1-linux-x64.tar.gz",
          "sha256": "…",
          "sha256_sri": "sha256-…"
        }
      ]
    }
  ]
}
```

## Publishing

The updater writes the plan; your workflow publishes it. `examples/workflow.yml`
contains a complete publisher that:

- reads the plan from the current action output, so an old plan is never reused;
- verifies every local SHA256 before touching the API;
- publishes to `run.repository`, including a repository different from the
  checkout;
- pages through existing assets, reuses a same-name asset only when its SHA256
  matches, and rejects conflicting content before uploading anything;
- creates a new release as a draft, uploads assets by streaming literal paths,
  then publishes the draft;
- leaves an existing draft as a draft and fails clearly when an immutable release
  is missing assets;
- never deletes or overwrites an asset.

Publishing to another repository needs a token with access to that repository.
The default `github.token` only covers the repository running the workflow.

The workflow in `examples/workflow.yml` needs:

```yaml
permissions:
  contents: write
```

and a checkout with persisted credentials when the updater commits its output.

## GitHub Action

```yaml
- uses: actions/checkout@v4
  with:
    persist-credentials: true

- uses: UfRd-Archive/UfRd-Updater@main
  id: ufrd
  with:
    config: updater.toml
    # only: "example-app"
    # force: "true"
    # prune: "true"
    # on_error: continue
```

Inputs:

| Input | Default | Meaning |
| --- | --- | --- |
| `config` | `updater.toml` | Configuration path in the workspace. |
| `only` | `""` | Target id regex. |
| `force` | `"false"` | Pass `--force`. |
| `prune` | `"false"` | Pass `--prune`. |
| `on_error` | `""` | Override `settings.on_error`. |
| `commit` | `""` | `true` or `false`; empty keeps the configuration value. |
| `push` | `""` | `true` or `false`; empty keeps the configuration value. `true` requires commit. |
| `verbose` | `"false"` | Enable debug logging. |

Outputs: `changed`, `tag`, `tags`, `manifest`, `sources`, `fetchurl`, `state`,
`releases`, `urls`, `summary`, `files`, `failed`, `failed_count`, `commit`, and
`pushed`.

`releases` is empty when the run did not export a batch, which covers
configuration errors, rollbacks, and failed hash repairs. `update.md` is
appended to `GITHUB_STEP_SUMMARY` when that variable is set.

## Git commits

With `git.commit = true` or `--commit`, the updater considers four files:
`manifest.json`, `sources.nix`, `fetchurl.nix`, and `state.json`.

Paths are literal, not glob patterns, and unrelated staged changes stay staged.
Ignored exports are reported and skipped; the remaining exports are committed.
A file that is already tracked stays committable even if a later ignore rule
matches its directory. If all four are ignored, or the exports have no diff,
commit and push are skipped and the run still succeeds. Track all four files to
keep the archive snapshot complete.

The commit happens after exports, so the committed manifest references a
release that the workflow publishes afterwards. For strict
publish-before-commit, set `commit = false`, publish, then commit the four
files.

## Development

```bash
uv sync --frozen
uv run pytest -q
uv run ufrd-updater validate --config examples/updater.toml
nix flake check
nix run . -- --version
```

The test suite uses a local HTTP server and temporary Git repositories. It does
not touch external services.

## Limits

- Upstream pages that require JavaScript need an API endpoint or a `command`
  extractor.
- `command` extractors need a POSIX `sh`.
- Version comparison is exact string equality. There is no semver ordering and
  no downgrade detection.
- Downloads are not compared against an upstream checksum. The recorded hashes
  describe the bytes the updater received.
- Signed or expiring source URLs change on every run, so the entry downloads
  every time. Identical bytes still produce no release.
- Changing only `archive.url_template` does not rewrite unchanged entries. Run
  with `--force`.
- HTTP metadata decoding supports gzip, zlib/raw deflate, and identity. Other
  content encodings fail explicitly.
