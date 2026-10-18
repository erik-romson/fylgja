"""Canary secrets never reach Fylgja's files (``PLAN-final-plan.md`` §4.5, A3, ``PLAN.md`` §4.3.1).

A canary ``CLAUDE_CODE_MESSAGING_TOKEN`` and ``CMUX_SOCKET_CAPABILITY`` sit in the hook's
environment, next to every allowlisted variable. No file the hook writes may contain them.

The inbox drain (step 19) extends this to the full §4.5 rule: the real ``fylgja hook`` runs with the
canaries in its environment, the daemon's Claude collector drains the inbox into SQLite, and the
canaries appear in **no log, spool file, database row or API response**. Messaging (step 31) adds
its own checks. Use :data:`CANARIES`, :func:`canary_env` and :func:`assert_no_canary`.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest
from api_support import Dist, EntryPoint, client, config, no_hosts
from claude_support import PsRunner
from hook_support import all_bytes_under, clean_env, envelope_files, feed, run_hook
from message_support import PID, SESSION, FakeInbox, LineEncoder, World, short_tmp, socket_dir

from fylgja import hook, paths
from fylgja.cli.doctor import check_messaging
from fylgja.collectors.claude import adapter as claude_adapter
from fylgja.collectors.claude import plugin as claude_plugin
from fylgja.collectors.claude.adapter import Seams
from fylgja.core.daemon import Daemon
from fylgja.core.system import LocalFileSystem
from fylgja.messaging.claude.receipts import ReceiptState
from fylgja.messaging.claude.resolve import Resolver
from fylgja.messaging.service import MessageService, WriterReceipts
from fylgja.testing.fakes import FakeFS

CANARIES = {
    "CLAUDE_CODE_MESSAGING_TOKEN": "canary-messaging-token-7f3a91c2",
    "CMUX_SOCKET_CAPABILITY": "canary-cmux-capability-5be0d4e8",
}

ALLOWLISTED = {
    "CLAUDE_CODE_SESSION_ID": "00000000-0000-4000-8000-000000004101",
    "CLAUDE_PID": "4101",
    "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/cc-socks/4101.sock",
    "CMUX_WORKSPACE_ID": "ws-1",
    "CMUX_SURFACE_ID": "surface-1",
    "CMUX_TAB_ID": "tab-1",
    "CMUX_PANEL_ID": "panel-1",
    "ITERM_SESSION_ID": "w0t0p0:iterm-1",
    "TERM_SESSION_ID": "term-1",
    "TERM_PROGRAM": "ghostty",
    "CLAUDE_CODE_BRIDGE_SESSION_ID": "session_bridge-1",
}


def canary_env() -> dict[str, str]:
    """A clean subprocess environment with every allowlisted variable and both canaries."""
    return clean_env(**ALLOWLISTED, **CANARIES)


def fylgja_roots() -> list[Path]:
    return [paths.data_dir(), paths.state_dir(), paths.config_dir()]


def assert_no_canary(*blobs: tuple[str, bytes]) -> None:
    """No blob contains a canary value or a fragment of one."""
    needles = [value.encode() for value in CANARIES.values()]
    needles += [value[-12:].encode() for value in CANARIES.values()]
    hits = [name for name, blob in blobs for needle in needles if needle in blob]
    assert hits == []


def _files() -> list[tuple[str, bytes]]:
    found = all_bytes_under(*fylgja_roots())
    assert found, "the hook wrote nothing, so the check would prove nothing"
    return [(str(path), data) for path, data in found]


PAYLOADS = {
    "event": ("Stop", b'{"session_id":"abc","hook_event_name":"Stop"}'),
    "statusline": ("StatusLine", b'{"session_id":"abc","cost":{"total_cost_usd":1}}'),
    "oversize-json": ("PostToolUse", json.dumps({"tool_response": "r" * 300_000}).encode()),
    "oversize-garbage": ("PostToolUse", b"g" * 300_000),
}


@pytest.mark.parametrize("case", sorted(PAYLOADS))
def test_canaries_never_reach_a_spool_file_in_process(case: str) -> None:
    event, stdin = PAYLOADS[case]
    read_fd = feed(stdin)
    try:
        environ = {**ALLOWLISTED, **CANARIES}
        target = hook.run([event], stdin_fd=read_fd, environ=environ)
    finally:
        os.close(read_fd)
    assert target is not None
    envelope = json.loads(target.read_bytes())
    assert envelope["env"] == ALLOWLISTED
    assert envelope["truncated"] is case.startswith("oversize")
    assert_no_canary(*_files())


@pytest.mark.parametrize("case", sorted(PAYLOADS))
def test_canaries_never_reach_a_spool_file_from_the_real_process(case: str) -> None:
    event, stdin = PAYLOADS[case]
    result = run_hook(event, stdin=stdin, env=canary_env())
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")
    written = envelope_files() + sorted(paths.statusline_dir().glob("*.json"))
    assert len(written) == 1
    envelope = json.loads(written[0].read_bytes())
    assert envelope["env"] == ALLOWLISTED
    assert set(envelope["env"]).isdisjoint(CANARIES)
    assert_no_canary(*_files())


def test_dropped_events_leave_no_canary_either() -> None:
    inbox = paths.inbox_dir("claude")
    inbox.mkdir(parents=True, mode=0o700)
    (inbox / hook.BACKLOG_STAMP).write_bytes(b"full\n")
    result = run_hook("Stop", stdin=b"{}", env=canary_env())
    assert result.returncode == 0
    assert envelope_files() == []
    assert_no_canary(*_files())


def test_the_check_itself_finds_a_planted_canary(isolated_env: Path) -> None:
    planted = paths.data_dir() / "planted.json"
    planted.write_text(json.dumps({"token": CANARIES["CLAUDE_CODE_MESSAGING_TOKEN"]}))
    with pytest.raises(AssertionError):
        assert_no_canary(*_files())


# -- the full rule: hook, drain, database, API and logs -----------------------------------------

BODY_SECRET = "tool-body-secret-0d9a41b7"
"""A secret in a tool input and response: the spool keeps it until commit, the database never."""


def _payload(event: str, **extra: object) -> bytes:
    return json.dumps(
        {"session_id": "abc", "hook_event_name": event, "cwd": "/w", **extra}
    ).encode()


def _db_dump() -> list[tuple[str, bytes]]:
    """Every table of the database as text (the file is also scanned byte for byte)."""
    path = paths.database_file()
    if not path.exists():
        return []
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
        tables = [
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        return [
            (f"row of {table}", json.dumps(row, default=str).encode())
            for table in tables
            for row in connection.execute(f'SELECT * FROM "{table}"')  # noqa: S608 - own schema
        ]


def _event_count() -> int:
    path = paths.database_file()
    if not path.exists():
        return 0
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
            # The state machine adds derived session and activity events (WP2.3); the drain's own
            # records are the hook events and the statusline's usage rows.
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM events WHERE entity_kind IN ('hook_event', 'usage')"
                ).fetchone()[0]
            )
    except sqlite3.Error:
        return 0


async def test_canaries_reach_no_log_spool_file_database_row_or_api_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    events = [
        ("SessionStart", _payload("SessionStart", source="startup")),
        (
            "PostToolUse",
            _payload(
                "PostToolUse",
                tool_name="Edit",
                tool_input={"file_path": "/w/a.py", "new_string": BODY_SECRET},
                tool_response={"output": BODY_SECRET},
            ),
        ),
        ("UserPromptSubmit", _payload("UserPromptSubmit", prompt="please fix the build")),
        ("Stop", _payload("Stop")),
        (
            "StatusLine",
            b'{"session_id":"abc","cost":{"total_cost_usd":1.5},"context_window":{"used_percentage":12}}',
        ),
    ]
    for name, stdin in events:
        result = run_hook(name, stdin=stdin, env=canary_env())
        assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")
    spooled = envelope_files()
    assert len(spooled) == 4
    assert any(BODY_SECRET.encode() in path.read_bytes() for path in spooled), (
        "the spool keeps the raw payload until commit"
    )
    assert_no_canary(*_files())

    registry = paths.data_dir().parent / "registry"
    registry.mkdir()
    entry = EntryPoint("claude", "fylgja.collectors.claude:plugin", Dist("fylgja"), claude_plugin)
    daemon = Daemon(
        config({"collectors": {"claude": {"enabled": True, "registry_dir": str(registry)}}}),
        entry_points=lambda: [entry],
        fs=FakeFS(),
        host_loader=no_hosts,
        home="/home/tester",
    )
    await daemon.start()
    try:
        deadline = time.monotonic() + 20
        while (_event_count() < 5 or envelope_files()) and time.monotonic() < deadline:  # noqa: ASYNC110
            await asyncio.sleep(0.05)
        assert envelope_files() == [], "the inbox was not drained"
        assert _event_count() == 5, "hook events and the statusline reading did not reach SQLite"

        responses: list[tuple[str, bytes]] = []
        async with client(daemon) as http:
            for route in (
                "/now",
                "/sessions",
                "/workspaces",
                "/collectors",
                "/health",
                "/view/attention",
                "/view/overview",
                "/items",
                "/rankers",
                "/journal/threads",
                "/inspect/collectors",
                "/inspect/pipeline",
                "/inspect/explain",
                "/summaries/folder/0123456789abcdef",
                "/",
            ):
                reply = await http.get(route)
                responses.append((f"GET {route}", reply.content))
    finally:
        await daemon.stop()

    log_text = "\n".join(f"{r.name} {r.getMessage()}" for r in caplog.records).encode()
    assert_no_canary(("captured log", log_text), *responses)
    assert_no_canary(*_files())  # the files now include the database, its WAL and the quarantine
    assert_no_canary(*_db_dump())
    secret = BODY_SECRET.encode()
    assert not any(secret in blob for _, blob in [*_files(), *_db_dump(), *responses])
    assert secret not in log_text


# -- messaging (step 31): the token and the key files never meet the message path ------------------

MESSAGE_TEXT = "please check the deploy (message body 8e21)"


class _KeyGuardFS(LocalFileSystem):
    """The resolver's filesystem: fails the test on any attempt to read a ``*.key`` file."""

    def __init__(self) -> None:
        self.read: list[str] = []

    def read_bytes(self, path: Path | str) -> bytes:
        name = Path(path).name
        assert not name.endswith(".key"), "a *.key file was opened"
        self.read.append(name)
        return super().read_bytes(path)


def _message_world(registry: Path, socks: Path) -> tuple[World, _KeyGuardFS]:
    world = World(registry, socks)
    guard = _KeyGuardFS()
    world.resolver = Resolver(
        registry, fs=guard, start_time=world.starts, roots=(str(socks),), uid=os.getuid()
    )
    return world, guard


async def test_no_messaging_token_reaches_the_collector_spool_database_logs_api_or_socket(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon runs with the canary token in its own environment and a ``*.key`` file beside
    the registry row. The Claude collector reads the row, the message goes out over a fake socket,
    and the canary is in no log, file, database row, API answer or byte on the socket."""
    caplog.set_level(logging.DEBUG)
    for name, value in CANARIES.items():
        monkeypatch.setenv(name, value)
    token = CANARIES["CLAUDE_CODE_MESSAGING_TOKEN"]
    registry = paths.data_dir().parent / "registry"
    with short_tmp() as root:
        socks = socket_dir(root)
        world, guard = _message_world(registry, socks)
        row = world.add()
        (registry / f"{PID}.9c1f.key").write_text(token, encoding="utf-8")
        ps = PsRunner()
        ps.starts.update(world.starts.table)
        monkeypatch.setattr(
            claude_adapter, "default_seams", lambda: Seams(ps, claude_adapter.LocalFileSystem())
        )
        entry = EntryPoint(
            "claude", "fylgja.collectors.claude:plugin", Dist("fylgja"), claude_plugin
        )
        daemon = Daemon(
            config(
                {
                    "actions": {"message": {"enabled": True}},
                    "collectors": {"claude": {"enabled": True, "registry_dir": str(registry)}},
                }
            ),
            entry_points=lambda: [entry],
            fs=FakeFS(),
            host_loader=no_hosts,
            home="/home/tester",
        )
        await daemon.start()
        try:
            daemon.messages = MessageService(
                enabled=True,
                max_chars=8000,
                messengers={"claude": world.messenger(LineEncoder())},
                receipts=WriterReceipts(daemon.writer),
                clock=daemon.clock,
            )
            deadline = time.monotonic() + 20
            while not daemon.view.sessions and time.monotonic() < deadline:  # noqa: ASYNC110
                await asyncio.sleep(0.05)
            assert daemon.view.sessions, "the Claude collector did not report the session"
            responses: list[tuple[str, bytes]] = []
            with FakeInbox(world.sock_path()) as inbox:
                async with client(daemon) as http:
                    secret_headers = {"Content-Type": "application/json"}
                    reply = await http.post(
                        f"/sessions/claude:{SESSION}/message",
                        headers=secret_headers,
                        json={"text": MESSAGE_TEXT},
                    )
                    responses.append(("POST unauthenticated", reply.content))
                    receipt = await daemon.messages.send(f"claude:{SESSION}", MESSAGE_TEXT)
                    for route in ("/now", "/sessions", f"/sessions/claude:{SESSION}"):
                        responses.append((f"GET {route}", (await http.get(route)).content))
                inbox.wait()
            await daemon.writer.flush()
        finally:
            await daemon.stop()
        doctor_checks = await check_messaging(
            daemon.config,
            runner=ps,
            registry=registry,
            roots=(str(socks),),
            managed_settings=registry / "no-managed.json",
        )
    assert receipt.state is ReceiptState.SUBMITTED
    assert row.exists()
    assert guard.read
    assert all(name.endswith(".json") for name in guard.read)
    sent = b"".join(inbox.received)
    assert MESSAGE_TEXT.encode() in sent  # the socket got the message ...
    assert b'"auth"' not in sent  # ... and no auth line
    log_text = "\n".join(f"{r.name} {r.getMessage()}" for r in caplog.records).encode()
    doctor_text = "\n".join(c.detail for c in doctor_checks).encode()
    assert_no_canary(
        ("socket bytes", sent),
        ("captured log", log_text),
        ("doctor", doctor_text),
        *responses,
    )
    files = [(name, data) for name, data in _files() if not name.endswith(".key")]
    assert_no_canary(*files, *_db_dump())
    # the text body went to the socket only: no receipt, row, log or answer holds it
    body = MESSAGE_TEXT.encode()
    assert not any(body in blob for _, blob in [*files, *_db_dump(), *responses])
    assert body not in log_text


_MESSAGING_SOURCES = Path(__file__).resolve().parents[2] / "src" / "fylgja" / "messaging"


def _code_strings_and_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    strings = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    }
    names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    names |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    return strings, names


@pytest.mark.parametrize("source", sorted(_MESSAGING_SOURCES.rglob("*.py")), ids=lambda p: p.name)
def test_the_messaging_code_never_touches_the_token_or_an_environment(source: Path) -> None:
    strings, names = _code_strings_and_names(ast.parse(source.read_text(encoding="utf-8")))
    assert not {"environ", "getenv", "putenv", "environb"} & names
    assert not [s for s in strings if "MESSAGING_TOKEN" in s or s.endswith(".key")]
    assert not [s for s in strings if '"auth"' in s or s == "auth"]
