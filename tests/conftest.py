from __future__ import annotations

import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class RecordedRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes = b""


class Server:
    def __init__(self) -> None:
        self.routes: dict[str, object] = {}
        self.requests: list[RecordedRequest] = []
        self._lock = threading.Lock()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.owner = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def route(self, path: str, response: Response | object) -> None:
        self.routes[path] = response

    def count(self, path: str) -> int:
        with self._lock:
            return sum(1 for request in self.requests if request.path == path)

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:  # noqa: D401
        return

    def _serve(self) -> None:
        owner: Server = self.server.owner  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        with owner._lock:
            owner.requests.append(
                RecordedRequest(self.command, self.path, dict(self.headers), body)
            )
        parsed = urlsplit(self.path)
        route = owner.routes.get(parsed.path)
        if route is None:
            response = Response(status=404, body=b"not found")
        elif callable(route):
            response = route(self.command, parsed, self.headers)
        else:
            response = route
        assert isinstance(response, Response)
        self.send_response(response.status)
        headers = dict(response.headers)
        headers.setdefault("Content-Length", str(len(response.body)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response.body)
            self.wfile.flush()

    do_GET = _serve
    do_POST = _serve
    do_HEAD = _serve


@pytest.fixture
def server():
    instance = Server()
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name in (
        "GITHUB_OUTPUT",
        "GITHUB_STEP_SUMMARY",
        "GITHUB_ACTIONS",
        "GITHUB_REF",
        "GITHUB_REF_NAME",
        "GITHUB_SHA",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_NUMBER",
        "GITHUB_REPOSITORY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)


@pytest.fixture
def no_sleep(monkeypatch):
    """Record retry sleeps instead of actually waiting."""

    from ufrd_updater import http as http_module

    delays: list[float] = []
    monkeypatch.setattr(http_module, "sleep", delays.append)
    return delays


@pytest.fixture
def config_file(tmp_path: Path):
    def write(text: str) -> Path:
        path = tmp_path / "updater.toml"
        path.write_text(text, encoding="utf-8")
        return path

    return write


def parse_github_output(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    outputs: dict[str, str] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if "<<" not in line:
            index += 1
            continue
        key, delimiter = line.split("<<", 1)
        index += 1
        value_lines: list[str] = []
        while index < len(lines) and lines[index] != delimiter:
            value_lines.append(lines[index])
            index += 1
        outputs[key] = "\n".join(value_lines)
        index += 1
    return outputs
