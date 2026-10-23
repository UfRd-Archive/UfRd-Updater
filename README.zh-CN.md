# UfRd-Updater

[English](README.md) | [简体中文](README.zh-CN.md)

[![CI](https://github.com/UfRd-Archive/UfRd-Updater/actions/workflows/ci.yml/badge.svg)](https://github.com/UfRd-Archive/UfRd-Updater/actions/workflows/ci.yml)

UfRd-Updater 用来维护 Nix 软件包归档。你只需在一个 TOML 文件里描述版本和下载地址在哪里，更新器就会解析它们、只下载发生变化的内容、为每个产物计算哈希，并写出确定性的 manifest、Nix 表达式、发布计划和一份便于阅读的摘要。

它既能作为独立 CLI 运行，也能作为可复用的 GitHub Action 使用。

## 快速开始

```bash
uv tool install .
ufrd-updater validate --config updater.toml
ufrd-updater list --config updater.toml
ufrd-updater run --config updater.toml
```

`run` 是默认子命令，所以 `ufrd-updater --config updater.toml` 与上面最后一条命令等价。

一份最小配置：

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

配置文件里的所有路径都相对于 TOML 文件所在目录解析，与你的 shell 当前目录无关。默认输出目录是它旁边的 `dist/`。

## 一次运行会做什么

1. 解析并校验整份配置。未知键和类型错误会在任何网络请求之前终止运行。
2. 先执行目标级请求，再执行各架构的请求，通过正则、JSON、HTML 或命令提取器收集变量。
3. 渲染每个架构的版本、下载地址、文件名和请求头。
4. 与上一轮的 manifest 和 state 比较。版本、文件名和源地址都没变的条目直接跳过下载。
5. 并行下载候选产物，一遍完成哈希计算；字节没有变化时保留上一轮条目。
6. 把新的 manifest、Nix 文件、state、发布计划、URL 列表和摘要作为一个批次写入。
7. 按需提交四个导出文件，并推送。

## 命令行

| 参数 | 含义 |
| --- | --- |
| `--config PATH` | 配置文件，默认 `updater.toml`。 |
| `--only REGEX` | 只运行 id 匹配该正则的目标。 |
| `--force` | 把每个选中条目都视为已变化：重新下载、重新发布，并重写归档地址。 |
| `--prune` | 删除完整配置中不存在的 manifest 和 state 条目。被 `--only` 排除的目标会保留。 |
| `--on-error MODE` | 覆盖 `settings.on_error`：`fail`、`continue` 或 `rollback`。 |
| `--commit` / `--no-commit` | 覆盖 `git.commit`。 |
| `--push` / `--no-push` | 覆盖 `git.push`。`--push` 等同于 `true`，并且需要启用提交。 |
| `--verbose` | 输出调试日志。URL 和请求头值保持脱敏。 |

退出码：

| 退出码 | 含义 |
| --- | --- |
| `0` | 成功。`continue` 模式下，普通的条目失败也返回 `0`。 |
| `1` | `fail` 模式下的条目失败、哈希修复失败、提交／推送／输出失败，或触发了 `rollback`。 |
| `2` | 配置或用法错误。没有下载，也没有提交。 |

`validate` 只解析配置并检查输出目录，不访问网络。`list` 打印所有配置好的目标／架构组合。

## 配置

文件以 `version = 1` 开头。未知键会直接报错。四个可选表控制全局行为。

### `[settings]`

| 键 | 默认值 | 含义 |
| --- | --- | --- |
| `output_dir` | `"dist"` | 输出目录，相对于配置文件。 |
| `concurrency` | `4` | 并行下载数，至少为 1。 |
| `retries` | `3` | 每次请求和下载的重试次数，至少为 0。 |
| `timeout` | `30.0` | 元数据读取超时，单位为秒。 |
| `download_timeout` | `120.0` | 下载读取超时，单位为秒。 |
| `connect_timeout` | `10.0` | 连接超时，单位为秒。 |
| `user_agent` | `"ufrd-updater/0.1.0"` | 默认 `User-Agent`，必须是合法的 ASCII HTTP 请求头值。 |
| `on_error` | `"fail"` | `fail`、`continue` 或 `rollback`。 |

所有超时都必须是有限的正数。

### `[git]`

| 键 | 默认值 | 含义 |
| --- | --- | --- |
| `commit` | `false` | 运行结束后提交导出的数据。 |
| `push` | `"auto"` | `auto`、`true` 或 `false`。 |
| `message` | `"chore(ufrd): update sources {datetime}"` | 提交信息模板。 |
| `user_name` | `"github-actions[bot]"` | 提交作者名。 |
| `user_email` | `"41898282+github-actions[bot]@users.noreply.github.com"` | 提交作者邮箱。 |

`auto` 在 GitHub Actions 中（`GITHUB_ACTIONS=true`）推送，其他环境不推送。`true` 在启用提交时于任何环境推送。`push = true` 与 `commit = false` 同时出现会报配置错误；传入 `--commit` 或启用提交即可。`false` 从不推送。更新器通过 `-c` 传入作者身份，不修改全局 Git 配置。

### `[release]`

| 键 | 默认值 | 含义 |
| --- | --- | --- |
| `tag_template` | `"ufrd-{datetime}-{run_id}"` | 变化资产的 tag 模板。目标可以覆盖它。 |

### `[archive]`

| 键 | 默认值 | 含义 |
| --- | --- | --- |
| `enabled` | `false` | 让 manifest 和 `fetchurl.nix` 指向 Release 资产。 |
| `repository` | `""` | `owner/repo`，回退到 `GITHUB_REPOSITORY`。 |
| `url_template` | `"https://github.com/{repository}/releases/download/{tag}/{filename}"` | 归档资产地址。 |

只要至少有一个选中的条目是新增或变化的，`enabled = true` 就必须有仓库。

### `[[target]]`

```toml
[[target]]
id = "example-app"
description = "可选的说明文字"
headers = { Referer = "https://example.com/" }
download_headers = { Authorization = "Bearer {env:ARCHIVE_TOKEN}" }
timeout = 30.0
retries = 3
tag_template = "example-app-{version}"
vars = { channel = "stable" }
```

每个目标至少需要一个 `[[target.arch]]`。目标 id 不能重复。

### 请求与提取器

目标级请求只执行一次，架构级请求为每个架构执行一次。请求提取出的变量会进入目标／架构作用域；`vars` 和提取结果共用同一个命名空间，后写入的值覆盖先写入的值。

```toml
[[target.request]]
url = "https://example.com/api/latest"
method = "GET"                # GET、POST 或 HEAD
headers = { Accept = "application/json" }
follow_redirects = true
expect_status = [200]         # 单个整数或列表；默认接受任意 2xx
# body = '字面量或 {模板}'
# json = { arch = "x86_64" }  # body 与 json 互斥
encoding = "utf-8"            # 文本／命令请求体解码覆盖

[[target.request.extract]]
var = "version"
type = "json"
query = "latest.version"
```

提取器字段：

| 字段 | 含义 |
| --- | --- |
| `var` | 要写入的变量名。必填。 |
| `type` | 必填：`regex`、`json`、`html` 或 `command`。 |
| `from` | 正则的输入来源：`body`（默认）、`header`、`url` 或 `var`。 |
| `source` | `from = "var"` 时的来源变量。 |
| `name` | `from = "header"` 时的请求头名称。 |
| `pattern`、`group`、`flags` | 正则表达式、捕获组和标志（`i`、`m`、`s`、`x`）。 |
| `query` | JSON 的 JMESPath 表达式。 |
| `selector`、`attr` | HTML 的 CSS 选择器和可选属性。 |
| `all`、`index` | 收集全部匹配，或按索引选择一个。 |
| `strip` | 去掉字符串结果两端的空白。HTML 默认开启；正则的默认值取决于来源。 |
| `required` | 没有结果且没有默认值时让请求失败。 |
| `default` | 没有结果时使用的值。 |
| `command`、`timeout` | `command` 提取器执行的命令及其超时。 |

提取器按顺序在暂存映射上运行。任意一个失败，该请求新增的变量全部丢弃，之前的作用域保持不变。JSON 的 `null` 视为「没有结果」，通过 `index` 选中的 `null` 也一样。

`command` 在配置文件所在目录执行 `sh -c`，stdin 关闭，并接收 `UFRD_BODY`、`UFRD_URL`、`UFRD_STATUS`、`UFRD_HEADERS`、`UFRD_VARS`，以及每个标量变量对应的 `UFRD_VAR_<NAME>`。超时会终止整个进程组，而不只是外层 shell。

### 架构

```toml
[[target.arch]]
id = "x86_64-linux"
version = "{version}"          # 默认使用提取出的 "version"
url = "https://example.com/{version}/app-linux-x64.tar.gz"
filename = "app-{version}-x64.tar.gz"  # 默认使用变量 "filename"，再回退到 URL 文件名
headers = { }
download_headers = { }
vars = { }
```

`id` 通常使用 Nix system 字符串。URL 必填。解析顺序是 `vars`、架构请求、`version`、URL、文件名。因此架构请求可以提供 `version`，而静态的 `arch.version` 不会提供给更早执行的架构请求。请求需要预先知道的版本时，把它写进 `vars`。

请求头名称不区分大小写。元数据请求依次合并目标请求头、架构请求头、请求自身的请求头。下载依次合并目标请求头、架构请求头、目标 `download_headers`、架构 `download_headers`，最后设置 `Accept-Encoding: identity`。

通用请求头会跟着产物地址走。上游控制产物地址时，也就控制了这些请求头的去向。只用于元数据的凭据放在需要它的那个请求里；`download_headers` 留给可信的目标地址。跨源重定向会移除 `Authorization` 和 Cookie，其他自定义秘密请求头会继续传递，所以目标地址必须可信。

### 模板

`url`、请求头值、`body`、`json` 里的字符串、`vars`、`version`、`filename`、`tag_template`、`archive.url_template` 和 `git.message` 都支持 `{name}` 插值。`{{` 和 `}}` 产生字面花括号。`{env:NAME}` 读取环境变量。未知名称会让对应操作失败，并列出可用的名称。

内置变量：`{id}`、`{arch}`（架构作用域），以及 tag 可用的 `{datetime}`（UTC，格式 `YYYYMMDD-HHMMSS`）、`{date}`、`{time}`、`{run_id}`、`{run_number}`、`{sha}`（`GITHUB_SHA` 的前 7 个字符；Actions 之外为 `local`）、`{repository}`、`{version}`、`{filename}`。提交信息还接受 `{tags}`、`{tag}` 和 `{changed_count}`。

本地安全的文件名保持原样。路径分隔符、控制字符、非法 Unicode 和超过文件系统长度限制的名称，会被替换成目标／架构目录内的确定性安全名称。空文件名仍然需要显式名称或可用的 URL 文件名。

## 输出

```text
dist/
  manifest.json   公开的来源数据库（提交）
  sources.nix     镜像 manifest.json 的纯数据（提交）
  fetchurl.nix    { fetchurl }: { ... }（提交）
  state.json      内部指纹（提交）
  releases.json   本次运行的发布计划
  urls.txt        变化条目的 manifest URL
  update.md       人类可读摘要
  files/<target>/<arch>/<filename>
```

`manifest.json` 先按目标、再按架构索引。每个条目包含 `version`、`url`、`filename` 和十二个哈希：md5、sha1、sha256、sha512，各提供 base16、Nix base32（`-nix32`）和 SRI（`-sri`）三种形式。JSON 使用两空格缩进，结尾带换行。

`state.json` 为每个条目保存 `source_url_sha256`、`archive_url` 和 `archive_repository`，用于让未变化的条目跳过下载。

变更检测先比较版本和文件名，再比较解析后的源地址哈希。候选产物十二个哈希与基线一致时保持未变化。启用归档、切换归档仓库，或为已有条目建立归档状态时，即使字节一致，该条目也会进入发布流程，让 manifest 和 `fetchurl.nix` 获得新的归档地址。关闭归档不会改写已有的归档地址；需要把它们改回源地址时运行 `--force`。`--force` 刷新所有选中条目。配置中消失的条目默认保留在 manifest 里，除非传入 `--prune`；`--only` 从不删除未选中的目标。

更新器校验完整的十二个哈希。选中的条目缺哈希、格式错误或彼此不一致时，即使版本和 URL 没变，也会重新下载修复。未选中且仍会写入输出的损坏条目会在访问网络之前终止运行，因为保留它就会导出无效数据。被 `--prune` 删除的过期损坏条目直接丢弃。修复失败时不会安装任何导出文件，运行返回 `1`，`continue` 模式也一样。

所有输出文件和变化资产先准备好，再一起安装。替换失败时恢复上一批文件，包括已经替换的资产。恢复本身也失败时，错误信息会打印保存上一批文件的 `output_dir/.tmp/` 目录，请保留该目录用于恢复。

`GITHUB_OUTPUT` 中的每个路径、每个 Release 资产的 `path`，以及 `files` 列表都是绝对路径。嵌套的配置目录和嵌套的 `output_dir` 可以直接在工作流里使用，不需要额外拼路径。

## Release tag 与资产名

变化资产按目标或全局模板渲染出的 Release tag 分组。非法 tag 字符变成 `-`，连续的 `-` 合并，`..` 变成 `.`，首尾的点和短横线去掉，长度限制为 200 个字符。截断后如果以 `.lock` 结尾，会补上 `-lock` 后缀，保持合法的 Git ref。

每个 Release 资产有三个名称：

| 字段 | 含义 |
| --- | --- |
| `filename` | 本地产物名称，也是写入 manifest 的名称。 |
| `asset_name` | GitHub 资产名。安全名称保持不变；不安全名称变成有长度上限的 ASCII 名称，并带确定性摘要后缀。 |
| `path` | 产物的本地绝对路径。 |

GitHub 归档地址、上传冲突检测和发布都使用 `asset_name`。自定义的非 GitHub URL 模板保留原文件名。同一个 tag 下上传名相同的两个资产必须字节一致；内容不同就是错误。不同源地址的相同字节在下载后去重。

`releases.json` 的结构：

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

## 发布

更新器负责写出计划，工作流负责发布。`examples/workflow.yml` 里有一份完整的发布脚本，它会：

- 从本次 Action 的输出读取计划，不会复用旧计划；
- 在访问 API 之前校验每个本地 SHA256；
- 发布到 `run.repository`，包括与当前 checkout 不同的仓库；
- 分页读取已有资产，同名资产的 SHA256 一致时复用，内容冲突时在上传前拒绝；
- 新建 Release 时先创建草稿，按字面路径流式上传资产，再发布草稿；
- 已有草稿保持草稿状态；不可变 Release 缺少资产时明确失败；
- 从不删除或覆盖资产。

发布到另一个仓库需要能访问该仓库的 token。默认的 `github.token` 只覆盖运行工作流的仓库。

`examples/workflow.yml` 需要：

```yaml
permissions:
  contents: write
```

更新器需要提交输出时，checkout 还要保留凭据。

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

输入：

| 输入 | 默认值 | 含义 |
| --- | --- | --- |
| `config` | `updater.toml` | 工作区内的配置路径。 |
| `only` | `""` | 目标 id 正则。 |
| `force` | `"false"` | 传入 `--force`。 |
| `prune` | `"false"` | 传入 `--prune`。 |
| `on_error` | `""` | 覆盖 `settings.on_error`。 |
| `commit` | `""` | `true` 或 `false`；留空时使用配置值。 |
| `push` | `""` | `true` 或 `false`；留空时使用配置值。`true` 需要启用提交。 |
| `verbose` | `"false"` | 开启调试日志。 |

输出：`changed`、`tag`、`tags`、`manifest`、`sources`、`fetchurl`、`state`、`releases`、`urls`、`summary`、`files`、`failed`、`failed_count`、`commit`、`pushed`。

运行没有导出批次时，`releases` 为空。配置错误、回滚和哈希修复失败都属于这种情况。设置了 `GITHUB_STEP_SUMMARY` 时，`update.md` 会追加到步骤摘要。

## Git 提交

设置 `git.commit = true` 或传入 `--commit` 后，更新器只考虑四个文件：`manifest.json`、`sources.nix`、`fetchurl.nix` 和 `state.json`。

路径按字面匹配，不当作 glob，无关的暂存内容保持原样。被忽略的导出文件会给出警告并跳过，其余导出文件照常提交。文件已经被跟踪时，即使后来新增的忽略规则匹配它所在的目录，它仍然可以提交。四个文件都被忽略，或者导出内容没有差异时，跳过提交和推送，运行仍然成功。建议四个文件一起跟踪，让归档快照保持完整。

提交发生在导出之后，所以提交的 manifest 会先于工作流发布 Release。需要严格先发布后提交时，设置 `commit = false`，完成发布后再提交这四个文件。

## 开发

```bash
uv sync --frozen
uv run pytest -q
uv run ufrd-updater validate --config examples/updater.toml
nix flake check
nix run . -- --version
```

测试套件使用本地 HTTP 服务器和临时 Git 仓库，不访问外部服务。

## 限制

- 需要 JavaScript 渲染的上游页面，请使用 API 端点或 `command` 提取器。
- `command` 提取器需要 POSIX `sh`。
- 版本比较是精确字符串相等，没有 semver 排序，也没有降级检测。
- 下载不会与上游校验和比对。记录的哈希描述的是更新器实际收到的字节。
- 带签名或会过期的源地址每次运行都会变化，因此该条目每次都会下载。字节相同就不会产生 Release。
- 只修改 `archive.url_template` 不会重写未变化的条目，请配合 `--force` 运行。
- HTTP 元数据解码支持 gzip、zlib／raw deflate 和 identity。其他内容编码会明确失败。
