"""Every read command against a fake HTTP server (WP1.10, A7).

The server is stdlib ``http.server`` on an ephemeral port in a thread. The commands run in process
through Typer's test runner, so coverage sees them; the port reaches them through ``config.toml``,
exactly as for a real user.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

import pytest
from http_support import LoopbackHTTPServer
from typer.testing import CliRunner

from fylgja import paths
from fylgja.cli import client, render
from fylgja.cli.app import app

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "cli"
NOW: dict[str, Any] = json.loads((FIXTURES / "now.json").read_text(encoding="utf-8"))
COLLECTORS: list[dict[str, Any]] = json.loads(
    (FIXTURES / "collectors.json").read_text(encoding="utf-8")
)

ROUTES: dict[str, Any] = {
    "/now": NOW,
    "/workspaces": NOW["workspaces"],
    "/workspaces/2": NOW["workspaces"][1],
    "/workspaces/1": NOW["workspaces"][0],
    "/sessions": NOW["sessions"],
    "/collectors": COLLECTORS,
}


class FakeDaemon:
    """Serves ``routes`` (path to JSON), and records the requests it got."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.delay = 0.0
        self.raw: dict[str, tuple[int, bytes]] = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
                outer.requests.append((self.path, dict(self.headers)))
                if outer.delay:
                    time.sleep(outer.delay)
                if self.path in outer.raw:
                    status, body = outer.raw[self.path]
                elif self.path in outer.routes:
                    status, body = 200, json.dumps(outer.routes[self.path]).encode()
                else:
                    status, body = 404, b'{"detail":"not found"}'
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

        self.server = LoopbackHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> FakeDaemon:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


def _configure_port(port: int) -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(f"[api]\nport = {port}\n", encoding="utf-8")


@pytest.fixture
def daemon() -> Iterator[FakeDaemon]:
    with FakeDaemon(ROUTES) as fake:
        _configure_port(fake.port)
        yield fake


@pytest.fixture
def down_port() -> int:
    """A port nothing listens on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    _configure_port(port)
    return port


runner = CliRunner()


def _golden(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


COMMANDS: list[tuple[list[str], str, Any]] = [
    (["now"], "/now", NOW),
    (["ws"], "/workspaces", NOW["workspaces"]),
    (["sessions"], "/sessions", NOW["sessions"]),
    (["collectors", "list"], "/collectors", COLLECTORS),
]


@pytest.mark.parametrize(("args", "path", "payload"), COMMANDS)
def test_json_prints_the_daemons_json(
    daemon: FakeDaemon, args: list[str], path: str, payload: Any
) -> None:
    result = runner.invoke(app, [*args, "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == payload
    assert daemon.requests[0][0] == path


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["now"], render.render_now(NOW, 80)),
        (["ws"], render.render_workspaces(NOW["workspaces"], 80)),
        (["sessions"], render.render_sessions(NOW["sessions"], 80)),
        (["collectors", "list"], render.render_collectors(COLLECTORS, 80)),
        (["ws", "2"], render.render_workspace(NOW["workspaces"][1], NOW["sessions"], 80)),
    ],
)
def test_tables_are_the_rendering_of_the_payload(
    daemon: FakeDaemon, monkeypatch: pytest.MonkeyPatch, args: list[str], expected: str
) -> None:
    monkeypatch.setenv("COLUMNS", "80")
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert result.stdout == expected + "\n"


def test_now_prints_the_workspace_and_session_table(
    daemon: FakeDaemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "100")
    result = runner.invoke(app, ["now"])
    assert result.stdout == _golden("now-100.txt")


def test_ws_with_a_name_asks_for_that_workspace(daemon: FakeDaemon) -> None:
    result = runner.invoke(app, ["ws", "2", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == NOW["workspaces"][1]
    assert [path for path, _ in daemon.requests] == ["/workspaces/2"]


def test_ws_without_sessions_makes_one_request(daemon: FakeDaemon) -> None:
    result = runner.invoke(app, ["ws", "1"])
    assert result.exit_code == 0
    assert [path for path, _ in daemon.requests] == ["/workspaces/1"]


def test_ws_with_sessions_also_reads_them(daemon: FakeDaemon) -> None:
    runner.invoke(app, ["ws", "2"])
    assert [path for path, _ in daemon.requests] == ["/workspaces/2", "/sessions"]


def test_an_unknown_workspace_exits_1(daemon: FakeDaemon) -> None:
    result = runner.invoke(app, ["ws", "nope"])
    assert result.exit_code == client.EXIT_API_ERROR
    assert "404" in result.stderr


def test_a_workspace_name_is_percent_encoded(daemon: FakeDaemon) -> None:
    runner.invoke(app, ["ws", "a/b c"])
    assert daemon.requests[0][0] == "/workspaces/a%2Fb%20c"


def test_the_request_is_a_plain_loopback_get_without_credentials(daemon: FakeDaemon) -> None:
    runner.invoke(app, ["sessions"])
    headers = {k.lower(): v for k, v in daemon.requests[0][1].items()}
    assert headers["host"] == f"127.0.0.1:{daemon.port}"
    assert headers["accept"] == "application/json"
    assert "authorization" not in headers


@pytest.mark.parametrize(("args", "_path", "_payload"), COMMANDS)
@pytest.mark.parametrize("as_json", [False, True])
def test_a_daemon_that_is_down_exits_3_with_a_clear_message(
    down_port: int, args: list[str], _path: str, _payload: Any, as_json: bool
) -> None:
    result = runner.invoke(app, [*args, *(["--json"] if as_json else [])])
    assert result.exit_code == client.EXIT_DAEMON_DOWN == 3
    assert "daemon not running" in result.stderr
    assert f"http://127.0.0.1:{down_port}" in result.stderr
    assert "fylgja serve" in result.stderr
    assert result.stdout == ""


def test_a_daemon_that_does_not_answer_in_time_counts_as_down(
    daemon: FakeDaemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon.delay = 1.0
    monkeypatch.setattr(client, "TIMEOUT", 0.2)
    result = runner.invoke(app, ["now"])
    assert result.exit_code == 3
    assert "timed out" in result.stderr


def test_an_error_status_exits_1(daemon: FakeDaemon) -> None:
    daemon.raw["/sessions"] = (500, b"boom")
    result = runner.invoke(app, ["sessions"])
    assert result.exit_code == 1
    assert "500" in result.stderr


def test_an_answer_that_is_not_json_exits_1(daemon: FakeDaemon) -> None:
    daemon.raw["/sessions"] = (200, b"<html>")
    result = runner.invoke(app, ["sessions"])
    assert result.exit_code == 1
    assert "not JSON" in result.stderr


def test_an_invalid_config_exits_2_naming_the_file() -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text("[api]\nport = 'no'\n", encoding="utf-8")
    result = runner.invoke(app, ["now"])
    assert result.exit_code == 2
    assert "config.toml" in result.stderr


def test_the_default_port_is_7319_without_a_config_file() -> None:
    assert client.base_url() == "http://127.0.0.1:7319"


def test_a_connection_that_closes_without_an_answer_counts_as_down() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def hang_up() -> None:
            conn, _ = listener.accept()
            conn.close()

        thread = threading.Thread(target=hang_up, daemon=True)
        thread.start()
        with pytest.raises(client.DaemonDown):
            client.get_json("/now", url=f"http://127.0.0.1:{port}", timeout=2)
        thread.join(5)


def test_no_command_prints_a_traceback_when_the_daemon_is_down(down_port: int) -> None:
    result = runner.invoke(app, ["collectors", "list"])
    assert "Traceback" not in result.output


def test_bare_fylgja_prints_the_help() -> None:
    result = runner.invoke(app, [])
    assert "serve" in result.output
    assert "collectors" in result.output
