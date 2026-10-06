"""``POST /sessions/{key}/message`` and ``fylgja session message`` (WP3.6, A6, §4.2).

The API runs through ASGI on the real app: disabled by default (409, nothing sent or recorded),
behind the mutation guard with the 64 KiB message limit, refusing any socket path and answering
with an honest receipt. The CLI runs in process against a stdlib HTTP server and asks before
sending unless ``--yes`` is given.
"""

from __future__ import annotations

import json
import socket
import sqlite3
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from api_support import BASE_URL, make_daemon
from claude_support import PsRunner
from message_support import (
    SESSION,
    FakeInbox,
    LineEncoder,
    World,
    short_tmp,
    socket_dir,
)
from typer.testing import CliRunner

from fylgja import paths
from fylgja.api.app import create_app
from fylgja.api.routes.messages import DISABLED_DETAIL
from fylgja.cli.app import app
from fylgja.core.daemon import Daemon
from fylgja.messaging.claude.receipts import MemoryReceipts, ReceiptState
from fylgja.messaging.service import MessageService, MessagingDisabled, WriterReceipts
from fylgja.testing.fakes import FakeClock

SECRET = "k7Qz-test-secret-9fA2xLmN0pRsTuVwXyZ_abcdefghij"
AUTH = {"Authorization": f"Bearer {SECRET}", "Content-Type": "application/json"}
KEY = f"claude:{SESSION}"
PATH = f"/sessions/{KEY}/message"
TEXT = "please run the tests again - FYLGJA-API-CANARY-31c9"


@pytest.fixture
def socks() -> Iterator[Path]:
    with short_tmp() as root:
        yield socket_dir(root)


@pytest.fixture
def world(isolated_env: Path, socks: Path) -> World:
    return World(isolated_env / "registry", socks)


def _http(daemon: Daemon) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(daemon, secret=SECRET)), base_url=BASE_URL
    )


def _receipt_rows() -> list[tuple[Any, ...]]:
    path = paths.database_file()
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
        return list(connection.execute("SELECT * FROM message_receipts"))


@pytest.fixture
async def default_daemon() -> AsyncIterator[Daemon]:
    instance = make_daemon()
    await instance.start()
    try:
        yield instance
    finally:
        await instance.stop()


# --- disabled by default --------------------------------------------------------------------------


async def test_messaging_is_disabled_by_default(default_daemon: Daemon, world: World) -> None:
    assert default_daemon.config.actions.message.enabled is False
    assert default_daemon.messages.enabled is False
    world.add()
    with FakeInbox(world.sock_path()) as inbox:
        async with _http(default_daemon) as http:
            response = await http.post(PATH, headers=AUTH, json={"text": TEXT})
        assert inbox.connections == 0
    assert response.status_code == 409
    assert response.json() == {"detail": DISABLED_DETAIL}
    assert TEXT not in response.text
    await default_daemon.writer.flush()
    assert _receipt_rows() == []


async def test_disabled_wins_over_every_body_problem(default_daemon: Daemon) -> None:
    async with _http(default_daemon) as http:
        for body in (b"not json", b"[]", b'{"text": "x", "socket_path": "/tmp/x"}'):
            response = await http.post(PATH, headers=AUTH, content=body)
            assert response.status_code == 409, body


# --- guarded --------------------------------------------------------------------------------------


async def test_the_message_route_is_behind_the_mutation_guard(default_daemon: Daemon) -> None:
    body = json.dumps({"text": TEXT}).encode()
    async with _http(default_daemon) as http:
        assert (await http.post(PATH, content=body)).status_code == 401
        wrong = {**AUTH, "Authorization": "Bearer nope"}
        assert (await http.post(PATH, headers=wrong, content=body)).status_code == 401
        foreign = {**AUTH, "Origin": "http://evil.example"}
        assert (await http.post(PATH, headers=foreign, content=body)).status_code == 403
        form = {**AUTH, "Content-Type": "text/plain"}
        assert (await http.post(PATH, headers=form, content=body)).status_code == 415
        huge = json.dumps({"text": "x" * (64 * 1024)}).encode()
        assert (await http.post(PATH, headers=AUTH, content=huge)).status_code == 413
        big = json.dumps({"text": "x" * (40 * 1024)}).encode()  # over 16 KiB, under 64 KiB
        assert (await http.post(PATH, headers=AUTH, content=big)).status_code == 409


# --- enabled --------------------------------------------------------------------------------------


@pytest.fixture
async def enabled_daemon(isolated_env: Path) -> AsyncIterator[tuple[Daemon, PsRunner]]:
    """The production wiring, enabled, over a test registry directory and a fake ``ps``."""
    runner = PsRunner()
    registry = isolated_env / "registry"
    instance = make_daemon(
        user_config={
            "actions": {"message": {"enabled": True}},
            "collectors": {"claude": {"registry_dir": str(registry)}},
        },
        runner=runner,
    )
    await instance.start()
    try:
        yield instance, runner
    finally:
        await instance.stop()


async def test_the_production_service_resolves_and_records_through_the_writer(
    enabled_daemon: tuple[Daemon, PsRunner], world: World
) -> None:
    daemon, runner = enabled_daemon
    assert daemon.messages.enabled is True
    async with _http(daemon) as http:
        missing = await http.post(PATH, headers=AUTH, json={"text": TEXT})
        world.add()  # a live row, its socket in a test directory: not one of Claude's roots
        runner.starts.update(world.starts.table)
        with FakeInbox(world.sock_path()) as inbox:
            outside = await http.post(PATH, headers=AUTH, json={"text": TEXT})
            assert inbox.connections == 0
    assert missing.status_code == outside.status_code == 200
    assert (missing.json()["state"], missing.json()["error_kind"]) == ("unreachable", "no_live_row")
    assert (outside.json()["state"], outside.json()["error_kind"]) == (
        "refused-locally",
        "socket_outside_allowed_roots",
    )
    assert runner.calls  # liveness came from ps
    await daemon.writer.flush()
    rows = _receipt_rows()
    assert [(row[1], row[3], row[6]) for row in rows] == [
        (KEY, "unreachable", "no_live_row"),
        (KEY, "refused-locally", "socket_outside_allowed_roots"),
    ]
    assert TEXT not in json.dumps(rows)
    assert TEXT not in missing.text + outside.text


async def test_a_submitted_message_answers_with_its_receipt(
    enabled_daemon: tuple[Daemon, PsRunner], world: World
) -> None:
    daemon, _ = enabled_daemon
    daemon.messages = MessageService(
        enabled=True,
        max_chars=8000,
        messengers={"claude": world.messenger(LineEncoder())},
        receipts=WriterReceipts(daemon.writer),
        clock=daemon.clock,
    )
    world.add()
    with FakeInbox(world.sock_path()) as inbox:
        async with _http(daemon) as http:
            response = await http.post(PATH, headers=AUTH, json={"text": TEXT})
        inbox.wait()
    assert response.status_code == 200
    receipt = response.json()
    assert receipt["state"] == "submitted"
    assert receipt["session_key"] == KEY
    assert "not delivery" in receipt["detail"]
    assert receipt["bytes"] == len(inbox.received[0])
    assert receipt["message_id"] in inbox.text
    assert TEXT in inbox.text  # the target got the text ...
    assert TEXT not in response.text  # ... the answer and the store did not
    await daemon.writer.flush()
    assert [row[0] for row in _receipt_rows()] == [receipt["message_id"]]
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("body", "status"),
    [
        (b"{not json", 400),
        (b"[]", 422),
        (b'"text"', 422),
        (b"{}", 422),
        (b'{"text": 5}', 422),
        (b'{"text": null}', 422),
        (b'{"text": "hi", "priority": "high"}', 422),
        (b'{"text": "hi", "socket_path": "/tmp/x.sock"}', 400),
        (b'{"text": "hi", "messagingSocketPath": "/tmp/x.sock"}', 400),
    ],
)
async def test_the_body_must_be_exactly_a_text(
    enabled_daemon: tuple[Daemon, PsRunner], body: bytes, status: int
) -> None:
    daemon, runner = enabled_daemon
    async with _http(daemon) as http:
        response = await http.post(PATH, headers=AUTH, content=body)
    assert response.status_code == status
    assert runner.calls == []  # nothing was resolved
    await daemon.writer.flush()
    assert _receipt_rows() == []


async def test_a_service_switched_off_mid_request_answers_409(
    enabled_daemon: tuple[Daemon, PsRunner],
) -> None:
    daemon, _ = enabled_daemon

    class Racing(MessageService):
        async def send(self, session_key: str, text: str) -> Any:
            raise MessagingDisabled

    daemon.messages = Racing(
        enabled=True, max_chars=10, messengers={}, receipts=MemoryReceipts(), clock=FakeClock()
    )
    async with _http(daemon) as http:
        response = await http.post(PATH, headers=AUTH, json={"text": "x"})
    assert response.status_code == 409


async def test_a_kind_without_an_adapter_is_unsupported(
    enabled_daemon: tuple[Daemon, PsRunner],
) -> None:
    daemon, _ = enabled_daemon
    async with _http(daemon) as http:
        response = await http.post("/sessions/codex:t-1/message", headers=AUTH, json={"text": "x"})
    assert (response.json()["state"], response.json()["error_kind"]) == (
        ReceiptState.UNSUPPORTED.value,
        "no_message_adapter",
    )


# --- the CLI --------------------------------------------------------------------------------------


class FakeDaemon:
    """Answers ``GET /sessions`` and ``POST /sessions/<key>/message`` and records the requests."""

    def __init__(self) -> None:
        self.sessions: list[dict[str, Any]] = [{"key": KEY}]
        self.answer: tuple[int, dict[str, Any]] = (200, _receipt("submitted"))
        self.posts: list[tuple[str, dict[str, str], bytes]] = []
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
                if self.path == "/sessions":
                    self._send(200, outer.sessions)
                else:
                    self._send(404, {"detail": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length", "0"))
                outer.posts.append((self.path, dict(self.headers), self.rfile.read(length)))
                self._send(*outer.answer)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
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


def _receipt(state: str, error_kind: str | None = None) -> dict[str, Any]:
    return {
        "message_id": "6f1d2c3b-0000-4000-8000-000000000001",
        "session_key": KEY,
        "state": state,
        "created_at": "2026-10-06T12:00:00.000000Z",
        "state_at": "2026-10-06T12:00:00.000000Z",
        "bytes": 120 if state == "submitted" else 0,
        "error_kind": error_kind,
        "detail": f"the {state} sentence",
    }


def _configure(port: int) -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(f"[api]\nport = {port}\n", encoding="utf-8")
    paths.api_secret_file().write_text(SECRET + "\n", encoding="utf-8")


@pytest.fixture
def fake() -> Iterator[FakeDaemon]:
    with FakeDaemon() as server:
        _configure(server.port)
        yield server


runner = CliRunner()


def test_the_cli_asks_first_and_sends_nothing_when_declined(fake: FakeDaemon) -> None:
    result = runner.invoke(app, ["session", "message", KEY, TEXT], input="n\n")
    assert result.exit_code == 1
    assert f"Send {len(TEXT)} characters to {KEY}?" in result.output
    assert "not sent" in result.output
    assert fake.posts == []


def test_the_cli_sends_once_after_yes(fake: FakeDaemon) -> None:
    result = runner.invoke(app, ["session", "message", KEY, TEXT], input="y\n")
    assert result.exit_code == 0, result.output
    assert "submitted: the submitted sentence" in result.output
    assert len(fake.posts) == 1
    path, headers, body = fake.posts[0]
    assert path == f"/sessions/{KEY.replace(':', '%3A')}/message"
    assert json.loads(body) == {"text": TEXT}
    assert headers["Authorization"] == f"Bearer {SECRET}"
    assert headers["Content-Type"] == "application/json"
    assert "Origin" not in headers
    assert SECRET not in result.output


def test_yes_skips_the_question(fake: FakeDaemon) -> None:
    result = runner.invoke(app, ["session", "message", "--yes", KEY, TEXT])
    assert result.exit_code == 0, result.output
    assert "Send " not in result.output
    assert len(fake.posts) == 1
    short = runner.invoke(app, ["session", "message", "-y", KEY, TEXT, "--json"])
    assert short.exit_code == 0
    assert json.loads(short.output)["state"] == "submitted"


@pytest.mark.parametrize("state", ["unsupported", "unreachable", "refused-locally", "unknown"])
def test_anything_but_submitted_exits_one(fake: FakeDaemon, state: str) -> None:
    fake.answer = (200, _receipt(state, "no_supported_frame"))
    result = runner.invoke(app, ["session", "message", "--yes", KEY, TEXT])
    assert result.exit_code == 1
    assert result.output.startswith(f"{state}: ")
    assert len(fake.posts) == 1  # never retried by the CLI either


def test_a_disabled_daemon_says_so(fake: FakeDaemon) -> None:
    fake.answer = (409, {"detail": DISABLED_DETAIL})
    result = runner.invoke(app, ["session", "message", "--yes", KEY, TEXT])
    assert result.exit_code == 1
    assert "409" in result.output
    assert "messaging is disabled" in result.output
    assert "nothing was sent" in result.output


def test_a_bare_or_prefix_id_is_resolved_against_live_sessions(fake: FakeDaemon) -> None:
    fake.sessions = [
        {"key": KEY},
        {"key": "codex:t-9", "children": [{"key": "claude:abcdef99-0000"}]},
        {"key": "claude:abcdef12-0000"},
    ]
    exact = runner.invoke(app, ["session", "message", "--yes", SESSION, "hi"])
    assert exact.exit_code == 0, exact.output
    child = runner.invoke(app, ["session", "message", "--yes", "abcdef99", "hi"])
    assert child.exit_code == 0, child.output
    assert [path for path, _, _ in fake.posts] == [
        f"/sessions/{KEY.replace(':', '%3A')}/message",
        "/sessions/claude%3Aabcdef99-0000/message",
    ]
    ambiguous = runner.invoke(app, ["session", "message", "--yes", "abcdef", "hi"])
    assert ambiguous.exit_code == 1
    assert "matches 2 sessions" in ambiguous.output
    none = runner.invoke(app, ["session", "message", "--yes", "zzz", "hi"])
    assert none.exit_code == 1
    assert "no live session matches" in none.output
    assert len(fake.posts) == 2


def test_the_daemon_down_exits_three_and_sends_nothing() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    _configure(port)
    result = runner.invoke(app, ["session", "message", "--yes", KEY, TEXT])
    assert result.exit_code == 3
    assert "nothing was sent" in result.output


def test_an_answer_that_is_not_a_receipt_exits_one(fake: FakeDaemon) -> None:
    fake.answer = (200, ["not", "a", "receipt"])  # type: ignore[assignment]
    result = runner.invoke(app, ["session", "message", "--yes", KEY, TEXT])
    assert result.exit_code == 1
    assert "not a receipt" in result.output


def test_without_a_secret_the_cli_explains_and_sends_nothing(fake: FakeDaemon) -> None:
    paths.api_secret_file().unlink()
    result = runner.invoke(app, ["session", "message", "--yes", KEY, TEXT])
    assert result.exit_code == 1
    assert "fylgja install secret" in result.output
    assert fake.posts == []


def test_session_message_help_names_the_rules() -> None:
    result = runner.invoke(app, ["session", "message", "--help"])
    assert result.exit_code == 0
    assert "--yes" in result.output
    assert "retried" in result.output  # the help panel may wrap the sentence anywhere
