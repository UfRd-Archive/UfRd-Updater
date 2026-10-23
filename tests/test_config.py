from __future__ import annotations

import json
from pathlib import Path

import pytest

from ufrd_updater.cli import main
from ufrd_updater.config import ConfigError, load_config, validate_output_paths

BASE = """
version = 1

[settings]
output_dir = "dist"

[[target]]
id = "demo"

[[target.request]]
url = "https://example.invalid/api"

[[target.request.extract]]
var = "version"
type = "json"
query = "version"

[[target.arch]]
id = "x86_64-linux"
url = "https://example.invalid/{version}/app.tar.gz"
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "updater.toml"
    path.write_text(text, encoding="utf-8")
    return path


def parse(tmp_path: Path, text: str):
    return load_config(write(tmp_path, text))


def test_valid_config_and_list(tmp_path, capsys):
    config = parse(tmp_path, BASE)
    assert config.version == 1
    assert config.settings.output_dir == "dist"
    assert config.full_keys() == {"demo/x86_64-linux"}
    assert config.output_dir == tmp_path / "dist"
    validate_output_paths(config)

    assert main(["list", "--config", str(tmp_path / "updater.toml")]) == 0
    assert capsys.readouterr().out.strip() == "demo/x86_64-linux"


def test_validate_offline(tmp_path, capsys):
    path = write(tmp_path, BASE)
    assert main(["validate", "--config", str(path)]) == 0
    assert capsys.readouterr().out.strip() == "config OK"
    assert not (tmp_path / "dist").exists()


@pytest.mark.parametrize(
    ("anchor", "key"),
    [
        ("[settings]", "timeout"),
        ("[settings]", "download_timeout"),
        ("[settings]", "connect_timeout"),
        ('id = "demo"', "timeout"),
        ('query = "version"', "timeout"),
    ],
)
@pytest.mark.parametrize(
    "value",
    ["0", "-1", "nan", "inf", "-inf", "9" * 400],
    ids=["zero", "negative", "nan", "infinity", "negative-infinity", "overflow"],
)
def test_timeouts_require_finite_positive_numbers(tmp_path, anchor, key, value):
    text = BASE.replace(anchor, f"{anchor}\n{key} = {value}")
    with pytest.raises(ConfigError):
        parse(tmp_path, text)


def test_finite_integer_and_fractional_timeouts(tmp_path):
    text = (
        BASE.replace(
            "[settings]",
            "[settings]\ntimeout = 0.125\ndownload_timeout = 2\nconnect_timeout = 3.5",
        )
        .replace('id = "demo"', 'id = "demo"\ntimeout = 4')
        .replace('query = "version"', 'query = "version"\ntimeout = 5.25')
    )
    config = parse(tmp_path, text)
    assert config.settings.timeout == 0.125
    assert config.settings.download_timeout == 2.0
    assert config.settings.connect_timeout == 3.5
    assert config.targets[0].timeout == 4.0
    assert config.targets[0].requests[0].extract[0].timeout == 5.25


@pytest.mark.parametrize("number", ["nan", "inf", "-inf"])
@pytest.mark.parametrize(
    "json_body",
    [
        "{ value = NUMBER }",
        "{ items = [{ value = NUMBER }] }",
        "{ nested = { items = [NUMBER] } }",
    ],
)
def test_nonfinite_json_numbers_rejected_at_every_depth(tmp_path, number, json_body):
    body = json_body.replace("NUMBER", number)
    text = BASE.replace(
        'url = "https://example.invalid/api"',
        f'url = "https://example.invalid/api"\njson = {body}',
    )
    with pytest.raises(ConfigError):
        parse(tmp_path, text)


@pytest.mark.parametrize(
    "value",
    [
        "private-user-agent/caf\u00e9",
        "private-user-agent\r\nInjected: secret",
        "private-user-agent\x00",
        "private-user-agent\x1f",
        "private-user-agent\x7f",
        " private-user-agent",
        "private-user-agent\t",
    ],
    ids=["non-ascii", "crlf", "nul", "control", "del", "leading-space", "trailing-tab"],
)
def test_user_agent_requires_a_safe_ascii_header_value(tmp_path, value):
    text = BASE.replace(
        "[settings]", f"[settings]\nuser_agent = {json.dumps(value)}"
    )
    with pytest.raises(ConfigError) as error:
        parse(tmp_path, text)
    assert "private-user-agent" not in str(error.value)


@pytest.mark.parametrize(
    "value",
    ["updater/1.0", "updater/1.0 (compatible)", "updater/1.0\tclient"],
)
def test_user_agent_accepts_ascii_header_content(tmp_path, value):
    text = BASE.replace(
        "[settings]", f"[settings]\nuser_agent = {json.dumps(value)}"
    )
    assert parse(tmp_path, text).settings.user_agent == value


@pytest.mark.parametrize(
    "text",
    [
        BASE.replace("version = 1", "version = 1\nbogus = 2"),
        BASE.replace("[settings]", "[settings]\nbogus = 1"),
        BASE.replace('id = "demo"', 'id = "demo"\nbogus = 1'),
        BASE.replace('id = "x86_64-linux"', 'id = "x86_64-linux"\nbogus = 1'),
        BASE.replace('query = "version"', 'query = "version"\nbogus = 1'),
    ],
)
def test_unknown_keys_rejected(tmp_path, text):
    with pytest.raises(ConfigError, match="unknown key"):
        parse(tmp_path, text)


def test_duplicate_target_and_arch(tmp_path):
    doubled_target = BASE + """
[[target]]
id = "demo"
[[target.arch]]
id = "aarch64-linux"
version = "{version}"
url = "https://example.invalid/{version}/arm.tar.gz"
"""
    with pytest.raises(ConfigError, match="duplicate target"):
        parse(tmp_path, doubled_target)

    doubled_arch = BASE + """
[[target.arch]]
id = "x86_64-linux"
version = "{version}"
url = "https://example.invalid/{version}/two.tar.gz"
"""
    with pytest.raises(ConfigError, match="duplicate arch"):
        parse(tmp_path, doubled_arch)


def test_missing_version_and_wrong_type(tmp_path):
    with pytest.raises(ConfigError, match="version is required"):
        parse(tmp_path, BASE.replace("version = 1\n", ""))
    with pytest.raises(ConfigError, match="must be 1"):
        parse(tmp_path, BASE.replace("version = 1", "version = 2"))
    with pytest.raises(ConfigError, match="integer"):
        parse(tmp_path, BASE.replace("version = 1", "version = true"))


def test_target_and_arch_requirements(tmp_path):
    with pytest.raises(ConfigError, match="at least one"):
        parse(tmp_path, "version = 1\n")
    with pytest.raises(ConfigError, match="arch must contain"):
        parse(
            tmp_path,
            BASE.split("[[target.arch]]")[0].replace(
                'url = "https://example.invalid/api"', 'url = "x"'
            ),
        )
    with pytest.raises(ConfigError, match="url is required"):
        parse(tmp_path, BASE.replace('url = "https://example.invalid/api"\n', ""))
    with pytest.raises(ConfigError, match="url is required"):
        parse(
            tmp_path,
            BASE.replace(
                'url = "https://example.invalid/{version}/app.tar.gz"\n', ""
            ),
        )


def test_missing_version_variable(tmp_path):
    text = BASE.replace(
        """
[[target.request.extract]]
var = "version"
type = "json"
query = "version"
""",
        "",
    )
    with pytest.raises(ConfigError, match="no version template"):
        parse(tmp_path, text)


def test_bad_regex_jmespath_css(tmp_path):
    with pytest.raises(ConfigError, match="pattern is invalid"):
        parse(
            tmp_path,
            BASE.replace(
                'query = "version"', 'query = "version"\npattern = "("'
            ).replace("type = \"json\"", "type = \"regex\""),
        )
    with pytest.raises(ConfigError, match="query is invalid"):
        parse(tmp_path, BASE.replace('query = "version"', 'query = "["'))
    with pytest.raises(ConfigError, match="selector is invalid"):
        parse(
            tmp_path,
            BASE.replace(
                'query = "version"',
                'query = "version"\nselector = "a[["',
            ).replace("type = \"json\"", "type = \"html\""),
        )


def test_bad_enums_and_extract_combinations(tmp_path):
    with pytest.raises(ConfigError, match="method"):
        parse(tmp_path, BASE.replace('url = "https://example.invalid/api"', 'url = "https://example.invalid/api"\nmethod = "PUT"'))
    with pytest.raises(ConfigError, match="on_error"):
        parse(tmp_path, BASE.replace("[settings]", '[settings]\non_error = "ignore"'))
    with pytest.raises(ConfigError, match="type must be one of"):
        parse(tmp_path, BASE.replace('type = "json"', 'type = "xml"'))
    with pytest.raises(ConfigError, match="from must be one of"):
        parse(tmp_path, BASE.replace('type = "json"', 'type = "json"\nfrom = "cookie"'))
    with pytest.raises(ConfigError, match="name is required"):
        parse(
            tmp_path,
            BASE.replace(
                'query = "version"', 'query = "version"\npattern = "x"'
            ).replace('type = "json"', 'type = "regex"\nfrom = "header"'),
        )
    with pytest.raises(ConfigError, match="source is required"):
        parse(
            tmp_path,
            BASE.replace(
                'query = "version"', 'query = "version"\npattern = "x"'
            ).replace('type = "json"', 'type = "regex"\nfrom = "var"'),
        )
    with pytest.raises(ConfigError, match="attr is only valid"):
        parse(
            tmp_path,
            BASE.replace(
                'query = "version"', 'query = "version"\nattr = "href"\npattern = "x"'
            ).replace('type = "json"', 'type = "regex"'),
        )
    with pytest.raises(ConfigError, match="all is not allowed"):
        parse(tmp_path, BASE.replace('type = "json"', 'type = "json"\nall = true'))


def test_body_and_json_are_exclusive(tmp_path):
    with pytest.raises(ConfigError, match="mutually exclusive"):
        parse(
            tmp_path,
            BASE.replace(
                'url = "https://example.invalid/api"',
                'url = "https://example.invalid/api"\nbody = "x"\njson = { a = "b" }',
            ),
        )


def test_template_validation(tmp_path):
    with pytest.raises(ConfigError, match="not allowed here"):
        parse(
            tmp_path,
            BASE.replace(
                'url = "https://example.invalid/{version}/app.tar.gz"',
                'url = "https://example.invalid/app.tar.gz"\nfilename = "{url}"',
            ),
        )
    with pytest.raises(ConfigError, match="unknown template variable"):
        parse(
            tmp_path,
            BASE.replace(
                "[settings]", '[release]\ntag_template = "{bogus}"\n[settings]'
            ),
        )
    with pytest.raises(ConfigError, match="unknown template variable"):
        parse(
            tmp_path,
            BASE.replace(
                "[settings]", '[git]\nmessage = "{bogus}"\n[settings]'
            ),
        )
    with pytest.raises(ConfigError, match="unknown template variable"):
        parse(
            tmp_path,
            BASE.replace(
                "[settings]",
                '[archive]\nurl_template = "https://x/{bogus}"\n[settings]',
            ),
        )
    with pytest.raises(ConfigError, match="invalid template"):
        parse(
            tmp_path,
            BASE.replace(
                'url = "https://example.invalid/api"',
                'url = "https://example.invalid/api/{unclosed"',
            ),
        )


def test_push_true_requires_commit(tmp_path):
    text = BASE.replace("[settings]", '[git]\ncommit = false\npush = true\n[settings]')
    path = write(tmp_path, text)
    assert main(["run", "--config", str(path)]) == 2
    assert main(["run", "--config", str(path), "--push"]) == 2


@pytest.mark.parametrize("output_dir", ["dist", "dist/nested/output"])
def test_output_path_structure(tmp_path, output_dir):
    text = BASE.replace('output_dir = "dist"', f'output_dir = "{output_dir}"')
    config = parse(tmp_path, text)
    (tmp_path / "dist").write_text("file", encoding="utf-8")
    with pytest.raises(ConfigError):
        validate_output_paths(config)


def test_output_path_allows_missing_descendants_of_directory(tmp_path):
    (tmp_path / "existing").mkdir()
    config = parse(
        tmp_path,
        BASE.replace('output_dir = "dist"', 'output_dir = "existing/nested/output"'),
    )
    validate_output_paths(config)
    assert not config.output_dir.exists()


def test_output_path_nul_is_a_safe_configuration_error(tmp_path, capsys):
    config = parse(
        tmp_path,
        BASE.replace(
            'output_dir = "dist"', 'output_dir = "private-output\\u0000/nested"'
        ),
    )
    with pytest.raises(ConfigError) as error:
        validate_output_paths(config)
    assert "private-output" not in str(error.value)
    assert main(["validate", "--config", str(config.path)]) == 2
    assert "private-output" not in capsys.readouterr().err


@pytest.mark.parametrize("destination_exists", [False, True])
def test_output_path_rejects_nondirectory_symlink_ancestor(tmp_path, destination_exists):
    destination = tmp_path / "destination"
    if destination_exists:
        destination.write_text("file", encoding="utf-8")
    (tmp_path / "blocked").symlink_to(destination)
    config = parse(
        tmp_path,
        BASE.replace('output_dir = "dist"', 'output_dir = "blocked/nested/output"'),
    )
    with pytest.raises(ConfigError):
        validate_output_paths(config)


def test_bad_toml_and_missing_file(tmp_path):
    path = write(tmp_path, "version = [")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(path)
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "nope.toml")


def test_invalid_config_encoding_is_a_configuration_error(tmp_path, capsys):
    path = tmp_path / "updater.toml"
    path.write_bytes(b"version = 1\n# invalid UTF-8: \xff\n")
    with pytest.raises(ConfigError):
        load_config(path)
    assert main(["validate", "--config", str(path)]) == 2
    assert capsys.readouterr().err


def test_body_and_json_template_validation(tmp_path):
    with pytest.raises(ConfigError, match="invalid template"):
        parse(
            tmp_path,
            BASE.replace(
                'url = "https://example.invalid/api"',
                'url = "https://example.invalid/api"\nbody = "hello {unclosed"',
            ),
        )
    with pytest.raises(ConfigError, match="JSON bodies may only contain"):
        parse(
            tmp_path,
            BASE.replace(
                'url = "https://example.invalid/api"',
                'url = "https://example.invalid/api"\njson = { when = 1979-05-27T07:32:00Z }',
            ),
        )


def test_empty_artifact_fields_rejected(tmp_path):
    with pytest.raises(ConfigError, match="filename must not be empty"):
        parse(
            tmp_path,
            BASE.replace(
                'url = "https://example.invalid/{version}/app.tar.gz"',
                'url = "https://example.invalid/{version}/app.tar.gz"\nfilename = ""',
            ),
        )
    with pytest.raises(ConfigError, match="tag_template must not be empty"):
        parse(
            tmp_path,
            BASE.replace(
                "[settings]", '[release]\ntag_template = ""\n[settings]'
            ),
        )
