from __future__ import annotations

import json
import os
import shlex
import signal
import socket
import subprocess
import sys

import httpx
import jmespath
import pytest

from ufrd_updater import http
from ufrd_updater.config import Extract, Settings
from ufrd_updater.extract import ExtractContext, ExtractError, run_extractors
from ufrd_updater.template import TemplateError, render

from .conftest import Response


def context(
    tmp_path,
    *,
    body: bytes = b"",
    url: str = "https://example.test/page",
    status: int = 200,
    headers: httpx.Headers | None = None,
    variables: dict | None = None,
    encoding: str | None = None,
) -> ExtractContext:
    return ExtractContext(
        body=body,
        url=url,
        status=status,
        headers=headers or httpx.Headers(),
        encoding=encoding,
        variables=variables or {},
        cwd=tmp_path,
        default_timeout=5.0,
        environ=dict(os.environ),
    )


def extract_one(item: Extract, ctx: ExtractContext) -> dict:
    return run_extractors([item], ctx)


def test_regex_group_all_index_and_flags(tmp_path):
    body = b"Version: 1.2.3\nBETA\nversion: 2.0.0"
    ctx = context(tmp_path, body=body)

    assert extract_one(
        Extract(var="v", type="regex", pattern=r"(?i)version: (\d+\.\d+\.\d+)", group=1),
        ctx,
    )["v"] == "1.2.3"

    assert extract_one(
        Extract(
            var="all",
            type="regex",
            pattern=r"(?i)version: (\d+\.\d+\.\d+)",
            group=1,
            all=True,
        ),
        ctx,
    )["all"] == ["1.2.3", "2.0.0"]

    assert extract_one(
        Extract(
            var="last",
            type="regex",
            pattern=r"(?i)version: (\d+\.\d+\.\d+)",
            group=1,
            all=True,
            index=-1,
        ),
        ctx,
    )["last"] == "2.0.0"

    assert extract_one(
        Extract(var="beta", type="regex", pattern=r"^BETA$", flags="m"),
        ctx,
    )["beta"] == "BETA"

    assert extract_one(
        Extract(
            var="minor",
            type="regex",
            pattern=r"(?P<major>\d+)\.(?P<minor>\d+)",
            group="minor",
        ),
        context(tmp_path, body=b"1.42"),
    )["minor"] == "42"


def test_regex_strip_and_optional_behavior(tmp_path):
    ctx = context(tmp_path, body=b" 42 ")
    assert extract_one(
        Extract(var="v", type="regex", pattern=r"(\s+42\s+)", group=1, strip=True),
        ctx,
    )["v"] == "42"

    with pytest.raises(ExtractError):
        extract_one(Extract(var="v", type="regex", pattern=r"nope"), ctx)

    result = extract_one(
        Extract(var="v", type="regex", pattern=r"nope", required=False),
        ctx,
    )
    assert "v" not in result

    result = extract_one(
        Extract(var="v", type="regex", pattern=r"nope", required=False, default="0"),
        ctx,
    )
    assert result["v"] == "0"

    result = extract_one(
        Extract(
            var="v",
            type="regex",
            pattern=r"nope",
            all=True,
            required=False,
            default=["0"],
        ),
        ctx,
    )
    assert result["v"] == ["0"]

    with pytest.raises(ExtractError):
        extract_one(Extract(var="v", type="regex", pattern=r"nope", all=True), ctx)

    result = extract_one(
        Extract(var="v", type="regex", pattern=r"42", index=5, required=False),
        ctx,
    )
    assert "v" not in result


def test_header_extraction_and_redirect_url(server, tmp_path):
    server.route(
        "/redirect",
        Response(status=302, headers={"Location": server.url("/final")}),
    )
    server.route("/final", Response(body=b"ok"))

    settings = Settings(retries=0, timeout=5.0, connect_timeout=5.0)
    with http.make_client(settings) as client:
        response = http.request(
            client,
            method="GET",
            url=server.url("/redirect"),
            follow_redirects=False,
            expect_status=(302,),
            timeout=http.metadata_timeout(settings),
            retries=0,
        )
        ctx = context(
            tmp_path,
            body=response.content,
            url=response.url,
            status=response.status,
            headers=response.headers,
        )
        location = extract_one(
            Extract(
                var="location",
                type="regex",
                from_="header",
                name="location",
                pattern=r"http://[^/]+/final",
            ),
            ctx,
        )["location"]
        assert location.endswith("/final")

        followed = http.request(
            client,
            method="GET",
            url=server.url("/redirect"),
            follow_redirects=True,
            timeout=http.metadata_timeout(settings),
            retries=0,
        )
        ctx = context(tmp_path, body=followed.content, url=followed.url)
        assert extract_one(
            Extract(
                var="final",
                type="regex",
                from_="url",
                pattern=r"/final$",
            ),
            ctx,
        )["final"] == "/final"


def test_header_all_values(tmp_path):
    headers = httpx.Headers(
        [
            ("set-cookie", " a=1 "),
            ("set-cookie", " b=2 "),
            ("x-one", "only"),
        ]
    )
    ctx = context(tmp_path, headers=headers)
    assert extract_one(
        Extract(
            var="cookies",
            type="regex",
            from_="header",
            name="Set-Cookie",
            pattern=r"([ab])=\d",
            group=1,
            all=True,
        ),
        ctx,
    )["cookies"] == ["a", "b"]
    assert extract_one(
        Extract(
            var="first",
            type="regex",
            from_="header",
            name="set-cookie",
            pattern=r"([ab])=\d",
            group=1,
        ),
        ctx,
    )["first"] == "a"


def test_from_var(tmp_path):
    ctx = context(tmp_path, variables={"source": "version=9.9.9"})
    assert extract_one(
        Extract(
            var="v",
            type="regex",
            from_="var",
            source="source",
            pattern=r"version=(\d+\.\d+\.\d+)",
            group=1,
        ),
        ctx,
    )["v"] == "9.9.9"

    ctx = context(tmp_path, variables={"source": ["x"]})
    with pytest.raises(ExtractError, match="not a scalar"):
        extract_one(
            Extract(var="v", type="regex", from_="var", source="source", pattern="x"),
            ctx,
        )


def test_json_scalars_filters_arrays_and_errors(tmp_path):
    body = json.dumps(
        {
            "latest": {"version": "3.4.5"},
            "items": [
                {"arch": "x64", "version": "1"},
                {"arch": "arm", "version": "2"},
            ],
            "flag": True,
            "n": 7,
            "empty": None,
        }
    ).encode()
    ctx = context(tmp_path, body=body)

    assert extract_one(
        Extract(var="v", type="json", query="latest.version"), ctx
    )["v"] == "3.4.5"
    assert extract_one(Extract(var="f", type="json", query="flag"), ctx)["f"] == "true"
    assert extract_one(Extract(var="n", type="json", query="n"), ctx)["n"] == "7"

    filtered = extract_one(
        Extract(var="versions", type="json", query="items[?arch=='x64'].version"), ctx
    )["versions"]
    assert filtered == ["1"]
    assert extract_one(
        Extract(
            var="one",
            type="json",
            query="items[?arch=='x64'].version",
            index=0,
        ),
        ctx,
    )["one"] == "1"

    optional = extract_one(
        Extract(var="missing", type="json", query="empty", required=False), ctx
    )
    assert "missing" not in optional

    with pytest.raises(ExtractError, match="index requires a list"):
        extract_one(
            Extract(var="bad", type="json", query="latest.version", index=0), ctx
        )
    with pytest.raises(ExtractError, match="not valid JSON"):
        extract_one(
            Extract(var="bad", type="json", query="x"),
            context(tmp_path, body=b"{not json"),
        )

    list_value = filtered
    with pytest.raises(TemplateError, match="use an extractor index"):
        render("{versions}", {"versions": list_value})


def test_json_lists_and_objects_keep_python_values(tmp_path):
    body = json.dumps({"items": [1, True, {"n": 2}], "obj": {"n": 2}}).encode()
    ctx = context(tmp_path, body=body)

    items = extract_one(Extract(var="items", type="json", query="items"), ctx)["items"]
    assert items == [1, True, {"n": 2}]
    assert isinstance(items[2], dict)

    selected = extract_one(
        Extract(var="item", type="json", query="items", index=2), ctx
    )["item"]
    assert selected == {"n": 2}

    obj = extract_one(Extract(var="obj", type="json", query="obj"), ctx)["obj"]
    assert obj == {"n": 2}


@pytest.mark.parametrize(
    ("query", "index"),
    [("items[0]", None), ("items", 0), ("items", -1)],
)
@pytest.mark.parametrize(
    ("required", "default"),
    [(False, None), (True, None), (False, "fallback"), (True, "fallback")],
)
def test_json_selected_null_has_equivalent_missing_result_behavior(
    tmp_path, query, index, required, default
):
    ctx = context(tmp_path, body=b'{"items":[null]}', variables={"keep": "original"})
    item = Extract(
        var="value",
        type="json",
        query=query,
        index=index,
        required=required,
        default=default,
    )
    if required and default is None:
        with pytest.raises(ExtractError):
            extract_one(item, ctx)
    else:
        expected = {"keep": "original"}
        if default is not None:
            expected["value"] = default
        assert extract_one(item, ctx) == expected
    assert ctx.variables == {"keep": "original"}


@pytest.mark.parametrize(
    "body",
    [b'{"private":"private-response-payload",invalid}', b'{"private":"\xff"}'],
    ids=["malformed", "invalid-encoding"],
)
def test_invalid_json_errors_do_not_echo_response_data(tmp_path, body):
    ctx = context(tmp_path, body=body, variables={"keep": "original"})
    with pytest.raises(ExtractError) as error:
        extract_one(Extract(var="value", type="json", query="private"), ctx)
    assert "private-response-payload" not in str(error.value)
    assert "0xff" not in str(error.value)
    assert ctx.variables == {"keep": "original"}


def test_json_nesting_limit_is_an_extractor_error(tmp_path):
    depth = 100_000
    ctx = context(tmp_path, body=b"[" * depth + b"0" + b"]" * depth)
    with pytest.raises(ExtractError) as error:
        extract_one(Extract(var="value", type="json", query="@"), ctx)
    assert isinstance(error.value.__cause__.__cause__, RecursionError)
    assert ctx.variables == {}


def test_json_integer_conversion_limit_is_an_extractor_error(tmp_path):
    previous_limit = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(640)
        ctx = context(tmp_path, body=b'{"n":' + b"9" * 641 + b"}")
        with pytest.raises(ExtractError) as error:
            extract_one(Extract(var="value", type="json", query="n"), ctx)
        assert isinstance(error.value.__cause__.__cause__, ValueError)
        assert ctx.variables == {}
    finally:
        sys.set_int_max_str_digits(previous_limit)


@pytest.mark.parametrize(
    ("query", "value", "failure"),
    [
        ("contains(private, n)", 1, TypeError),
        (
            "ceil(n)",
            {"secret": "private-response-payload"},
            jmespath.exceptions.JMESPathError,
        ),
        ("ceil(n)", float("nan"), ValueError),
        ("floor(n)", float("inf"), OverflowError),
    ],
    ids=["contains-type", "query-type", "nonfinite-value", "numeric-overflow"],
)
def test_json_query_data_errors_are_safe_and_isolated(tmp_path, query, value, failure):
    body = json.dumps({"private": "private-response-payload", "n": value}).encode()
    ctx = context(tmp_path, body=body, variables={"keep": "original"})
    extractors = [
        Extract(var="staged", type="regex", pattern="private-response-payload"),
        Extract(var="value", type="json", query=query),
    ]
    with pytest.raises(ExtractError) as error:
        run_extractors(extractors, ctx)
    assert isinstance(error.value.__cause__.__cause__, failure)
    assert "private-response-payload" not in str(error.value)
    assert ctx.variables == {"keep": "original"}


def test_html_text_attr_all_and_defaults(tmp_path):
    body = (
        b'<div><a class="dl" href="/a.tar.gz"> A </a>'
        b'<a class="dl" href="/b.tar.gz">B</a>'
        b'<span class="v"> 1.2 </span></div>'
    )
    ctx = context(tmp_path, body=body)

    assert extract_one(
        Extract(var="hrefs", type="html", selector="a.dl", attr="href", all=True), ctx
    )["hrefs"] == ["/a.tar.gz", "/b.tar.gz"]
    assert extract_one(
        Extract(var="texts", type="html", selector="a.dl", all=True), ctx
    )["texts"] == ["A", "B"]
    assert extract_one(
        Extract(var="version", type="html", selector="span.v"), ctx
    )["version"] == "1.2"
    assert extract_one(
        Extract(var="second", type="html", selector="a.dl", attr="href", index=1), ctx
    )["second"] == "/b.tar.gz"

    optional = extract_one(
        Extract(
            var="missing",
            type="html",
            selector="a.dl",
            attr="data-nope",
            required=False,
        ),
        ctx,
    )
    assert "missing" not in optional

    with pytest.raises(ExtractError):
        extract_one(
            Extract(var="bad", type="html", selector="a.dl", attr="data-nope"), ctx
        )


def test_command_environment_and_failures(tmp_path):
    ctx = context(
        tmp_path,
        body=b"hello",
        status=201,
        variables={"channel": "stable", "items": ["a", "b"]},
    )
    result = extract_one(
        Extract(
            var="out",
            type="command",
            command='printf "%s|%s|%s" "$UFRD_STATUS" "$UFRD_BODY" "$UFRD_VAR_CHANNEL"',
        ),
        ctx,
    )
    assert result["out"] == "201|hello|stable"

    result = extract_one(
        Extract(var="vars", type="command", command='printf "%s" "$UFRD_VARS"'),
        ctx,
    )
    assert json.loads(result["vars"]) == {"channel": "stable", "items": ["a", "b"]}

    with pytest.raises(ExtractError) as error:
        extract_one(
            Extract(var="bad", type="command", command="exit 3"), ctx
        )
    assert "3" in str(error.value)


def test_command_timeout_stops_descendants_before_they_can_act(tmp_path, monkeypatch):
    marker = tmp_path / "descendant-acted"
    child = (
        "import socket, sys; from pathlib import Path; "
        "connection = socket.create_connection(('127.0.0.1', int(sys.argv[1])), 30); "
        "connection.sendall(b'R'); "
        "instruction = connection.recv(1); "
        "instruction == b'G' and Path(sys.argv[2]).write_text('acted'); "
        "instruction == b'G' and connection.sendall(b'A')"
    )
    real_communicate = subprocess.Popen.communicate
    connection = None
    processes = []

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(10)
        port = listener.getsockname()[1]

        def communicate_after_descendant_is_ready(process, *args, **kwargs):
            nonlocal connection
            processes.append(process)
            connection, _ = listener.accept()
            connection.settimeout(10)
            assert connection.recv(1) == b"R"
            return real_communicate(process, *args, **kwargs)

        monkeypatch.setattr(
            subprocess.Popen, "communicate", communicate_after_descendant_is_ready
        )
        command = shlex.join(
            [sys.executable, "-c", child, str(port), str(marker)]
        ) + " & wait"
        try:
            with pytest.raises(ExtractError) as error:
                extract_one(
                    Extract(var="out", type="command", command=command, timeout=0.05),
                    context(tmp_path),
                )
            assert isinstance(error.value.__cause__.__cause__, subprocess.TimeoutExpired)
            assert processes[0].returncode is not None
            assert connection is not None
            try:
                connection.sendall(b"G")
            except (BrokenPipeError, ConnectionResetError):
                pass
            try:
                response = connection.recv(1)
            except ConnectionResetError:
                response = b""
            assert response == b""
            assert not marker.exists()
        finally:
            for process in processes:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if connection is not None:
                connection.close()


def test_request_level_staging_discards_on_failure(tmp_path):
    ctx = context(tmp_path, body=b"one=1 two=2")
    extractors = [
        Extract(var="one", type="regex", pattern=r"one=(\d)", group=1),
        Extract(var="boom", type="regex", pattern=r"nope"),
    ]
    with pytest.raises(ExtractError):
        run_extractors(extractors, ctx)
    assert ctx.variables == {}


def test_command_environment_limits_become_extractor_errors(tmp_path):
    large = context(tmp_path, body=b"x" * (140 * 1024))
    with pytest.raises(ExtractError):
        extract_one(Extract(var="out", type="command", command="true"), large)

    nul = context(tmp_path, body=b"a\x00b")
    with pytest.raises(ExtractError):
        extract_one(Extract(var="out", type="command", command="true"), nul)
