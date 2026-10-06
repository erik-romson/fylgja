"""The OpenCode collector over a fixture database and a scripted process table (WP3.2).

``pgrep``, ``lsof`` and ``ps`` answer from the committed ``process/`` fixtures through a
:class:`FakeProcessRunner`; ``opencode.db`` is built from the sanitized DDL and rows. No OpenCode
is needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from inbox_support import write_envelope
from opencode_support import (
    CANARY,
    FIXTURES,
    LSOF,
    PGREP,
    PS,
    Rig,
    build_db,
    lsof_cwds,
    script_processes,
    session_id,
)

from fylgja import paths
from fylgja.collectors.base import Capability
from fylgja.collectors.opencode import adapter, plugin
from fylgja.collectors.opencode.collector import OpenCodeCollector, OpenCodeSettings
from fylgja.models import (
    EntityKind,
    HealthState,
    HookEvent,
    SessionActivity,
    SessionActivitySnapshot,
    SessionSnapshot,
)
from fylgja.testing.fakes import FakeProcessRunner

ENVELOPE_GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "envelope"


async def wait_until(condition: Callable[[], object], seconds: float = 5.0) -> None:
    """Wait in real time: the inbox reads its files on worker threads."""
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return build_db(tmp_path / "opencode.db")


def _last(rig: Rig, kind: type[SessionSnapshot] | type[SessionActivitySnapshot]) -> object:
    return next(r for r in reversed(rig.sink.records) if isinstance(r, kind))


def _sessions(rig: Rig) -> dict[str, object]:
    snapshot = _last(rig, SessionSnapshot)
    assert isinstance(snapshot, SessionSnapshot)
    return {item.entity_id: item.payload for item in snapshot.items}


def _activities(rig: Rig) -> dict[str, SessionActivity]:
    snapshot = _last(rig, SessionActivitySnapshot)
    assert isinstance(snapshot, SessionActivitySnapshot)
    return {item.entity_id: item.payload for item in snapshot.items}


def _key(number: int) -> str:
    return f"opencode:{session_id(number)}"


# --------------------------------------------------------------------------------------------
# The join and the state
# --------------------------------------------------------------------------------------------


async def test_a_poll_lists_each_process_with_its_newest_session(db: Path) -> None:
    rig = Rig(db)
    script_processes(rig.runner)
    assert await rig.collector.poll() is True
    sessions = _sessions(rig)
    assert set(sessions) == {_key(1), _key(2)}
    first = sessions[_key(1)]
    assert (first.pid, first.cwd, first.kind, first.model) == (  # type: ignore[attr-defined]
        51001,
        "/work/example/api-service",
        "opencode",
        "example-model",
    )
    assert first.process_key.startswith("51001-")  # type: ignore[attr-defined]
    assert [p.comm for p in first.ancestry][-1].endswith("idea")  # type: ignore[attr-defined]
    assert sessions[_key(2)].pid == 51002  # type: ignore[attr-defined]


async def test_busy_and_idle_come_from_the_assistant_message(db: Path) -> None:
    rig = Rig(db)
    script_processes(rig.runner)
    await rig.collector.poll()
    activities = _activities(rig)
    busy = activities[_key(1)]
    assert busy.state == "busy"
    assert (busy.current_tool, busy.current_target) == ("bash", "uv run pytest tests/unit -x")
    assert busy.title == "Add pagination to the orders endpoint"
    idle = activities[_key(2)]
    assert idle.state == "idle"
    assert idle.current_tool is None
    assert idle.title == "Fix the login redirect"


async def test_the_todo_table_is_exposed_as_the_sessions_own_list(db: Path) -> None:
    rig = Rig(db)
    script_processes(rig.runner)
    await rig.collector.poll()
    busy = _activities(rig)[_key(1)]
    assert busy.open_task == "Write the pagination tests"
    assert busy.facts == {
        "tasks": [
            {"status": "completed", "content": "Add the page and limit parameters"},
            {"status": "in_progress", "content": "Write the pagination tests"},
            {"status": "pending", "content": "Update the API documentation"},
        ]
    }
    assert _activities(rig)[_key(2)].facts is None


async def test_archived_and_sub_agent_sessions_are_never_chosen(db: Path) -> None:
    rig = Rig(db)
    script_processes(rig.runner, pids=(51001,))
    await rig.collector.poll()
    assert set(_sessions(rig)) == {_key(1)}


async def test_two_processes_in_one_directory_take_the_two_newest_sessions(db: Path) -> None:
    rig = Rig(db)
    rig.runner.script(PGREP, stdout="51001\n51002\n")
    rig.runner.script(
        LSOF,
        stdout=lsof_cwds(
            {51001: "/work/example/api-service"} | {51002: "/work/example/api-service/"}
        ),
    )
    rig.runner.script(PS, stdout=(FIXTURES / "process" / "ps-table.txt").read_text())
    await rig.collector.poll()
    chosen = {key: payload.pid for key, payload in _sessions(rig).items()}  # type: ignore[attr-defined]
    # Session 1 is newest; session 3 is the only other candidate (4 is archived, 5 a child). The
    # older process (51001) gets the older session.
    assert chosen == {_key(3): 51001, _key(1): 51002}


async def test_a_stale_turn_and_an_errored_turn_are_idle(db: Path) -> None:
    rig = Rig(db)
    rig.runner.script(PGREP, stdout="51001\n51002\n")
    rig.runner.script(
        LSOF, stdout=lsof_cwds({51001: "/work/example/docs", 51002: "/work/example/errored"})
    )
    rig.runner.script(PS, stdout="")
    await rig.collector.poll()
    activities = _activities(rig)
    assert activities[_key(6)].state == "idle"
    assert activities[_key(6)].current_tool is None  # the running part of a dead turn
    assert activities[_key(8)].state == "idle"
    # Without the stale limit the old row would read as a turn in flight.
    wide = Rig(db, runner=rig.runner, busy_stale_seconds=10 * 86400)
    await wide.collector.poll()
    assert _activities(wide)[_key(6)].state == "busy"
    assert _activities(wide)[_key(6)].current_tool == "bash"


async def test_a_placeholder_title_and_no_messages_is_idle_without_a_title(db: Path) -> None:
    rig = Rig(db)
    rig.runner.script(PGREP, stdout="51001\n")
    rig.runner.script(LSOF, stdout=lsof_cwds({51001: "/work/example/scratch"}))
    rig.runner.script(PS, stdout="")
    await rig.collector.poll()
    activity = _activities(rig)[_key(7)]
    assert activity == SessionActivity(state="idle")


async def test_a_process_without_a_session_row_is_counted_and_not_listed(db: Path) -> None:
    rig = Rig(db)
    rig.runner.script(PGREP, stdout="51001\n51002\n")
    rig.runner.script(
        LSOF, stdout=lsof_cwds({51001: "/work/example/api-service", 51002: "/work/nowhere"})
    )
    rig.runner.script(PS, stdout="")
    await rig.collector.poll()
    assert set(_sessions(rig)) == {_key(1)}
    report = await rig.collector.doctor()
    assert report.state is HealthState.OK
    assert report.detail is not None
    assert "1 without a session row" in report.detail


async def test_a_process_that_exited_before_lsof_is_skipped(db: Path) -> None:
    rig = Rig(db)
    rig.runner.script(PGREP, stdout="51001\n51002\n")
    rig.runner.script(LSOF, stdout=lsof_cwds({51002: "/work/example/web-app"}))
    rig.runner.script(PS, stdout="")
    await rig.collector.poll()
    assert set(_sessions(rig)) == {_key(2)}


async def test_no_text_of_a_message_part_or_tool_input_reaches_a_record(db: Path) -> None:
    rig = Rig(db)
    script_processes(rig.runner)
    await rig.collector.poll()
    dumped = json.dumps([record.model_dump(mode="json") for record in rig.sink.records])
    assert CANARY not in dumped
    assert "second line" not in dumped


# --------------------------------------------------------------------------------------------
# Emission
# --------------------------------------------------------------------------------------------


async def test_an_unchanged_poll_emits_only_on_the_heartbeat(db: Path) -> None:
    rig = Rig(db, heartbeat_seconds=30.0)
    script_processes(rig.runner)
    assert await rig.collector.poll() is True
    await rig.clock.advance(5.0)
    assert await rig.collector.poll() is False
    assert rig.collector.emissions == 1
    await rig.clock.advance(30.0)
    assert await rig.collector.poll() is True
    assert rig.collector.emissions == 2


async def test_a_state_change_is_emitted_at_once(db: Path) -> None:
    rig = Rig(db)
    script_processes(rig.runner)
    await rig.collector.poll()
    connection = sqlite3.connect(db)
    connection.execute(
        "UPDATE message SET data = json_set(data, '$.time.completed', time_created + 5)"
        " WHERE id = 'msg_0000000104'"
    )
    connection.commit()
    connection.close()
    await rig.clock.advance(1.0)
    assert await rig.collector.poll() is True
    assert _activities(rig)[_key(1)].state == "idle"
    assert _activities(rig)[_key(1)].current_tool is None


async def test_a_session_whose_process_ended_leaves_the_complete_snapshot(db: Path) -> None:
    rig = Rig(db)
    script_processes(rig.runner)
    await rig.collector.poll()
    ended = FakeProcessRunner()
    ended.script(PGREP, returncode=1)
    rig.runner = ended
    rig.collector._seams = adapter.Seams(ended, adapter.LocalFileSystem())  # noqa: SLF001
    assert await rig.collector.poll() is True
    snapshot = rig.sink.of_kind(EntityKind.SESSION)[-1]
    assert isinstance(snapshot, SessionSnapshot)
    assert snapshot.complete
    assert snapshot.items == ()


async def test_ps_is_asked_only_for_new_processes(db: Path) -> None:
    rig = Rig(db)
    script_processes(rig.runner)
    await rig.collector.poll()
    await rig.clock.advance(31.0)
    await rig.collector.poll()
    assert sum(1 for call in rig.runner.calls if call[0] == PS) == 1


async def test_a_failing_ps_costs_only_the_process_key(db: Path) -> None:
    rig = Rig(db)
    rig.runner.script(PGREP, stdout="51001\n")
    rig.runner.script(LSOF, stdout=lsof_cwds({51001: "/work/example/api-service"}))
    rig.runner.script(PS, returncode=3)
    assert await rig.collector.poll() is True
    payload = _sessions(rig)[_key(1)]
    assert payload.process_key is None  # type: ignore[attr-defined]
    assert payload.ancestry is None  # type: ignore[attr-defined]


# --------------------------------------------------------------------------------------------
# Broken sources
# --------------------------------------------------------------------------------------------


async def test_a_missing_database_is_degraded_and_keeps_the_last_state(db: Path) -> None:
    rig = Rig(db)
    script_processes(rig.runner)
    await rig.collector.poll()
    emitted = len(rig.sink.records)
    os.unlink(db)
    await rig.clock.advance(31.0)
    assert await rig.collector.poll() is False
    assert len(rig.sink.records) == emitted  # nothing was ended by a read that failed
    report = await rig.collector.doctor()
    assert report.state is HealthState.DEGRADED
    assert report.error_kind == "db_missing"


async def test_a_missing_database_with_no_process_still_reports_degraded(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "absent" / "opencode.db")
    rig.runner.script(PGREP, returncode=1)
    report = await rig.collector.doctor()
    assert (report.state, report.error_kind) == (HealthState.DEGRADED, "db_missing")
    assert await rig.collector.poll() is True  # no process: the empty snapshot is still true
    assert (await rig.collector.doctor()).error_kind == "db_missing"
    assert not (tmp_path / "absent").exists()


async def test_a_corrupt_database_is_degraded(tmp_path: Path) -> None:
    path = tmp_path / "opencode.db"
    path.write_bytes(b"corrupt" * 100)
    rig = Rig(path)
    script_processes(rig.runner)
    assert await rig.collector.poll() is False
    report = await rig.collector.doctor()
    assert (report.state, report.error_kind) == (HealthState.DEGRADED, "db_unreadable")
    assert report.detail is not None
    assert "corrupt" not in report.detail


async def test_a_changed_schema_is_degraded_and_never_guessed(tmp_path: Path) -> None:
    ddl = (FIXTURES / "opencode.schema.sql").read_text(encoding="utf-8")
    path = tmp_path / "opencode.db"
    connection = sqlite3.connect(path)
    connection.executescript(ddl.replace("`time_archived` integer,", ""))
    connection.close()
    rig = Rig(path)
    script_processes(rig.runner)
    assert await rig.collector.poll() is False
    report = await rig.collector.doctor()
    assert (report.state, report.error_kind) == (HealthState.DEGRADED, "schema_mismatch")
    assert report.detail is not None
    assert "session.time_archived" in report.detail
    assert rig.sink.records == []


async def test_recovery_clears_the_degraded_state(
    db: Path, caplog: pytest.LogCaptureFixture
) -> None:
    moved = db.with_name("moved.db")
    os.rename(db, moved)
    rig = Rig(db)
    script_processes(rig.runner)
    with caplog.at_level(logging.INFO):
        await rig.collector.poll()
        await rig.collector.poll()
        os.rename(moved, db)
        assert await rig.collector.poll() is True
    assert (await rig.collector.doctor()).state is HealthState.OK
    messages = [r.getMessage() for r in caplog.records if "opencode" in r.getMessage()]
    assert messages.count("opencode database: db_missing") == 1  # logged once, not per poll
    assert "opencode database recovered" in messages


@pytest.mark.parametrize(
    ("script", "kind"),
    [
        ({"error": FileNotFoundError("pgrep")}, "pgrep_missing"),
        ({"returncode": 2}, "pgrep_failed"),
        ({"timeout": True}, "pgrep_timeout"),
        ({"error": PermissionError("no")}, "pgrep_not_executable"),
    ],
)
async def test_a_failing_pgrep_is_degraded(db: Path, script: dict[str, object], kind: str) -> None:
    rig = Rig(db)
    rig.runner.script(PGREP, **script)  # type: ignore[arg-type]
    assert await rig.collector.poll() is False
    report = await rig.collector.doctor()
    assert (report.state, report.error_kind) == (HealthState.DEGRADED, kind)
    assert rig.sink.records == []


async def test_a_failing_lsof_is_degraded(db: Path) -> None:
    rig = Rig(db)
    rig.runner.script(PGREP, stdout="51001\n")
    rig.runner.script(LSOF, returncode=2, stderr="lsof: bad option")
    assert await rig.collector.poll() is False
    assert (await rig.collector.doctor()).error_kind == "lsof_failed"


# --------------------------------------------------------------------------------------------
# The optional TypeScript plugin's inbox
# --------------------------------------------------------------------------------------------


def _spool_golden(number: int = 0) -> str:
    """The TypeScript plugin's golden envelope as a spool file in the OpenCode inbox."""
    envelope = json.loads((ENVELOPE_GOLDEN / "opencode-session-status.json").read_text())
    envelope["id"] = f"7a2b9c1d-3e4f-4a5b-8c6d-0e1f2a3b{number:04d}"
    envelope["received_at_ns"] += number
    payload = json.loads(envelope["payload_text"])
    payload["session_id"] = session_id(1)
    envelope["payload_text"] = json.dumps(payload)
    path = write_envelope(paths.inbox_dir("opencode"), envelope)
    return path.name


async def test_the_inbox_is_the_opencode_one_and_an_absent_one_is_fine(db: Path) -> None:
    rig = Rig(db)
    assert rig.collector.inbox.directory == paths.inbox_dir("opencode")
    assert not rig.collector.inbox.directory.exists()
    script_processes(rig.runner)
    task = asyncio.create_task(rig.collector.run())
    try:
        await rig.clock.advance(3.0)
        assert (await rig.collector.doctor()).state is HealthState.OK
    finally:
        task.cancel()
        await asyncio.wait({task}, timeout=5)
    assert not rig.collector.inbox.directory.exists()  # the reader never creates it


async def test_a_plugin_envelope_is_drained_and_wakes_the_poll(db: Path) -> None:
    rig = Rig(db, poll_interval_seconds=60.0, inbox_poll_seconds=1.0)
    script_processes(rig.runner)
    task = asyncio.create_task(rig.collector.run())
    try:
        await rig.clock.advance(0.5)
        assert rig.collector.emissions == 1  # the first poll
        # The state changes in the database; only the inbox can say so sooner than 60 s.
        connection = sqlite3.connect(db)
        connection.execute(
            "UPDATE message SET data = json_set(data, '$.time.completed', time_created + 5)"
            " WHERE id = 'msg_0000000104'"
        )
        connection.commit()
        connection.close()
        name = _spool_golden()
        await rig.clock.advance(1.0)
        await wait_until(lambda: rig.collector.emissions == 2)
        assert _activities(rig)[_key(1)].state == "idle"
    finally:
        task.cancel()
        await asyncio.wait({task}, timeout=5)
    events = [r for r in rig.sink.committed if isinstance(r, HookEvent)]
    assert len(events) == 1
    event = events[0]
    assert (event.source, event.entity_id) == ("opencode", _key(1))
    assert (event.payload.event, event.payload.reason, event.payload.cwd) == (
        "session.status",
        "busy",
        "/work/example/api-service",
    )
    assert not (paths.inbox_dir("opencode") / name).exists()  # delete-after-commit


async def test_a_foreign_envelope_in_the_inbox_is_quarantined(db: Path) -> None:
    rig = Rig(db)
    script_processes(rig.runner)
    inbox = paths.inbox_dir("opencode")
    envelope = json.loads((ENVELOPE_GOLDEN / "opencode-session-status.json").read_text())
    envelope["source"] = "claude"  # an envelope of another source in this inbox
    write_envelope(inbox, envelope)
    task = asyncio.create_task(rig.collector.run())
    try:
        await rig.clock.advance(3.0)
        await wait_until(lambda: (inbox / "quarantine").is_dir())
    finally:
        task.cancel()
        await asyncio.wait({task}, timeout=5)
    assert [p.name for p in (inbox / "quarantine").iterdir()]
    assert not [r for r in rig.sink.records if isinstance(r, HookEvent)]


# --------------------------------------------------------------------------------------------
# The loop, the settings and the plugin descriptor
# --------------------------------------------------------------------------------------------


async def test_an_unexpected_error_is_logged_once_and_the_loop_goes_on(
    db: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rig = Rig(db)
    script_processes(rig.runner)
    calls = 0
    original = rig.collector.poll

    async def flaky() -> bool:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise RuntimeError("boom with a secret title")
        return await original()

    rig.collector.poll = flaky  # type: ignore[method-assign]
    task = asyncio.create_task(rig.collector.run())
    try:
        with caplog.at_level(logging.ERROR):
            await rig.clock.advance(10.0)
    finally:
        task.cancel()
        await asyncio.wait({task}, timeout=5)
    assert calls >= 3
    assert rig.collector.emissions >= 1
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert errors == ["opencode poll loop failed: RuntimeError"]


def test_the_database_is_opened_read_only(db: Path) -> None:
    before = db.stat().st_mtime_ns
    content = db.read_bytes()
    with adapter.OpenCodeDb.open(db) as database:
        database.check_schema()
        database.todos(session_id(1))
    assert db.stat().st_mtime_ns == before
    assert db.read_bytes() == content
    assert sorted(p.name for p in db.parent.iterdir()) == ["opencode.db"]


def test_settings_reject_unknown_keys_and_bad_values() -> None:
    with pytest.raises(ValueError, match="bogus"):
        OpenCodeSettings(bogus=1)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="poll_interval_seconds"):
        OpenCodeSettings(poll_interval_seconds=0)
    assert OpenCodeSettings().process_name == "opencode"


def test_the_plugin_descriptor_declares_sessions_and_todos(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert plugin.name == "opencode"
    assert plugin.capabilities == frozenset({Capability.SESSIONS, Capability.TODOS})
    assert plugin.config_model is OpenCodeSettings
    from fylgja.collectors.context import build_context
    from fylgja.testing.fakes import FakeClock, RecordingSink

    context = build_context("opencode", RecordingSink(), clock=FakeClock())
    built = plugin.factory(OpenCodeSettings(db_path=db), context)
    assert isinstance(built, OpenCodeCollector)
    monkeypatch.setenv("XDG_DATA_HOME", str(db.parent))
    default = plugin.factory(OpenCodeSettings(), context)
    assert isinstance(default, OpenCodeCollector)
    with pytest.raises(TypeError):
        plugin.factory(shutil, context)  # type: ignore[arg-type]
