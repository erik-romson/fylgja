"""``fylgja session focus`` (D6, R19): focus a session, or open the navigator page instead.

The real Typer app runs in process against a stdlib HTTP stub. The page opener is recorded, never
run.
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import urllib.parse
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler
from typing import Any

import pytest
from http_support import LoopbackHTTPServer
from typer.testing import CliRunner

from fylgja import paths
from fylgja.cli import app as cli_app
from fylgja.cli.app import EXIT_CONFIG, app

SECRET = "k7Qz-test-secret-9fA2xLmN0pRsTuVwXyZ_abcdefghij"
KEY = "claude:a"
FOCUSED = {"focused": "tab", "via": "cmux-cli"}
runner = CliRunner()


class Stub:
    """A daemon that answers ``POST /focus/session/<key>`` and ``GET /sessions`` as scripted."""

    def __init__(self) -> None:
        self.sessions: list[dict[str, Any]] = [{"key": KEY}]
        self.answer: tuple[int, dict[str, Any]] = (200, FOCUSED)
        self.posts: list[tuple[str, dict[str, str]]] = []
        self.gets: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status: int, body: Any) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
                outer.gets.append(self.path)
                if self.path == "/sessions":
                    self._send(200, outer.sessions)
                else:
                    self._send(404, {"detail": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                self.rfile.read(int(self.headers.get("content-length", "0")))
                outer.posts.append((self.path, dict(self.headers)))
                self._send(*outer.answer)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

        self.server = LoopbackHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> Stub:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    @property
    def page(self) -> str:
        return f"http://127.0.0.1:{self.port}/#recall"

    def posted_keys(self) -> list[str]:
        return [urllib.parse.unquote(path.rsplit("/", 1)[-1]) for path, _ in self.posts]


def _configure(port: int) -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(f"[api]\nport = {port}\n", encoding="utf-8")
    paths.api_secret_file().write_text(SECRET + "\n", encoding="utf-8")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


@pytest.fixture
def stub() -> Iterator[Stub]:
    with Stub() as server:
        _configure(server.port)
        yield server


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    urls: list[str] = []
    monkeypatch.setattr(cli_app, "_open_page", urls.append)
    return urls


def test_a_focused_session_does_not_open_the_page(stub: Stub, opened: list[str]) -> None:
    """Scenario 1."""
    result = runner.invoke(app, ["session", "focus", KEY, "--else-open"])
    assert result.exit_code == 0, result.output
    assert "focused tab via cmux-cli" in result.output
    assert len(stub.posts) == 1
    assert stub.posts[0][0].startswith("/focus/session/")
    assert stub.posted_keys() == [KEY]
    assert stub.posts[0][1]["Authorization"] == f"Bearer {SECRET}"
    assert opened == []


def test_no_window_opens_the_page(stub: Stub, opened: list[str]) -> None:
    """Scenario 2: 409."""
    stub.answer = (409, {"detail": "no window"})
    result = runner.invoke(app, ["session", "focus", KEY, "--else-open"])
    assert result.exit_code == 0, result.output
    assert opened == [stub.page]

    opened.clear()
    result = runner.invoke(app, ["session", "focus", KEY])
    assert result.exit_code == 1
    assert opened == []


@pytest.mark.parametrize("status", [404, 502])
def test_an_api_error_opens_the_page(stub: Stub, opened: list[str], status: int) -> None:
    """Scenario 3."""
    stub.answer = (status, {"detail": "no"})
    result = runner.invoke(app, ["session", "focus", KEY, "--else-open"])
    assert result.exit_code == 0, result.output
    assert opened == [stub.page]

    opened.clear()
    result = runner.invoke(app, ["session", "focus", KEY])
    assert result.exit_code == 1
    assert opened == []


def test_a_daemon_that_is_down_opens_the_page(isolated_env: object, opened: list[str]) -> None:
    """Scenario 4."""
    port = _free_port()
    _configure(port)
    result = runner.invoke(app, ["session", "focus", KEY, "--else-open"])
    assert result.exit_code == 0, result.output
    assert opened == [f"http://127.0.0.1:{port}/#recall"]

    opened.clear()
    result = runner.invoke(app, ["session", "focus", KEY])
    assert result.exit_code == 3
    assert opened == []


def test_an_empty_key_opens_the_page_without_a_request(stub: Stub, opened: list[str]) -> None:
    """Scenario 5."""
    result = runner.invoke(app, ["session", "focus", "", "--else-open"])
    assert result.exit_code == 0, result.output
    assert stub.posts == []
    assert stub.gets == []
    assert opened == [stub.page]


def test_json_prints_the_daemons_answer(stub: Stub, opened: list[str]) -> None:
    """Scenario 6."""
    result = runner.invoke(app, ["session", "focus", KEY, "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == FOCUSED
    assert opened == []


def test_the_page_is_opened_with_usr_bin_open(stub: Stub, monkeypatch: pytest.MonkeyPatch) -> None:
    """Scenario 7: ``_open_page`` itself, with ``subprocess.run`` replaced."""
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    cli_app._open_page(stub.page)
    assert len(calls) == 1
    assert calls[0][0] == ["/usr/bin/open", stub.page]
    assert calls[0][1]["timeout"] == 5


def test_an_unknown_native_id(stub: Stub, opened: list[str]) -> None:
    """Scenario 8."""
    stub.sessions = [{"key": "claude:xyz"}]
    result = runner.invoke(app, ["session", "focus", "abc", "--else-open"])
    assert result.exit_code == 0, result.output
    assert stub.posts == []
    assert opened == [stub.page]

    opened.clear()
    result = runner.invoke(app, ["session", "focus", "abc"])
    assert result.exit_code == 1
    assert "no live session matches 'abc'" in result.output
    assert opened == []


def test_an_ambiguous_prefix(stub: Stub, opened: list[str]) -> None:
    """Scenario 9."""
    stub.sessions = [{"key": "claude:ab1"}, {"key": "codex:ab2"}]
    result = runner.invoke(app, ["session", "focus", "ab", "--else-open"])
    assert result.exit_code == 0, result.output
    assert stub.posts == []
    assert opened == [stub.page]

    opened.clear()
    result = runner.invoke(app, ["session", "focus", "ab"])
    assert result.exit_code == 1
    assert "matches 2 sessions" in result.output
    assert opened == []


def test_a_daemon_down_during_the_lookup(isolated_env: object, opened: list[str]) -> None:
    """Scenario 10."""
    port = _free_port()
    _configure(port)
    result = runner.invoke(app, ["session", "focus", "abc", "--else-open"])
    assert result.exit_code == 0, result.output
    assert opened == [f"http://127.0.0.1:{port}/#recall"]

    opened.clear()
    result = runner.invoke(app, ["session", "focus", "abc"])
    assert result.exit_code == 3
    assert opened == []


@pytest.mark.parametrize("given", [KEY, "abc"])
def test_a_broken_config(isolated_env: object, opened: list[str], given: str) -> None:
    """Scenario 11: the page is on the default port; without the flag, the config exit code."""
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text("[api\nport = \n", encoding="utf-8")
    paths.api_secret_file().write_text(SECRET + "\n", encoding="utf-8")
    result = runner.invoke(app, ["session", "focus", given, "--else-open"])
    assert result.exit_code == 0, result.output
    assert opened == ["http://127.0.0.1:7319/#recall"]

    opened.clear()
    result = runner.invoke(app, ["session", "focus", given])
    assert result.exit_code == EXIT_CONFIG
    assert opened == []


def test_a_unique_native_prefix_is_resolved(stub: Stub, opened: list[str]) -> None:
    """Scenario 12."""
    stub.sessions = [{"key": "claude:abc123"}]
    result = runner.invoke(app, ["session", "focus", "abc", "--else-open"])
    assert result.exit_code == 0, result.output
    assert stub.posted_keys() == ["claude:abc123"]
    assert opened == []
