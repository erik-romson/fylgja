"""The Codex collector over a fake process runner and a real, temporary Codex directory.

``pgrep``, ``lsof`` and ``ps`` answer from the committed process fixtures; the state database is
built from the sanitized DDL and rows, and the rollouts are the committed ones.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from codex_support import (
    LSOF,
    PGREP,
    PROCESS_FIXTURES,
    PS,
    CodexHome,
    Rig,
    build_state_db,
    lsof_text,
    make_home,
    script_processes,
    thread_id,
)
from pydantic import BaseModel, ValidationError

from fylgja.collectors.codex import adapter, parse
from fylgja.collectors.codex.collector import CodexSettings
from fylgja.collectors.codex.plugin import plugin
from fylgja.models import (
    EntityKind,
    HealthState,
    ProcessRef,
    SessionActivity,
    SessionActivitySnapshot,
    SessionPayload,
    SessionSnapshot,
)
from fylgja.testing.fakes import FakeProcessRunner

KEY1 = f"codex:{thread_id(1)}"
KEY2 = f"codex:{thread_id(2)}"


def sessions(rig: Rig, index: int = -1) -> dict[str, SessionPayload]:
    snapshot = rig.sink.of_kind(EntityKind.SESSION)[index]
    assert isinstance(snapshot, SessionSnapshot)
    assert snapshot.complete
    return {item.entity_id: item.payload for item in snapshot.items}


def activities(rig: Rig, index: int = -1) -> dict[str, SessionActivity]:
    snapshot = rig.sink.of_kind(EntityKind.SESSION_ACTIVITY)[index]
    assert isinstance(snapshot, SessionActivitySnapshot)
    assert snapshot.complete
    return {item.entity_id: item.payload for item in snapshot.items}


@pytest.fixture
def codex(isolated_env: Path) -> CodexHome:
    return make_home(isolated_env)


@pytest.fixture
def rig(codex: CodexHome) -> Rig:
    rig = Rig(codex)
    script_processes(rig.runner, codex)
    return rig


# --------------------------------------------------------------------------------------------
# The join: pgrep, lsof and the database
# --------------------------------------------------------------------------------------------


async def test_every_process_with_an_open_rollout_becomes_a_session(rig: Rig) -> None:
    assert await rig.collector.poll()
    listed = sessions(rig)
    assert sorted(listed) == [KEY1, KEY2]

    first = listed[KEY1]
    assert first.kind == "codex"
    assert first.native_id == thread_id(1)
    assert first.pid == 41001
    assert first.cwd == "/work/example/api-service"
    assert first.branch == "main"
    assert first.model == "gpt-5-codex"
    assert first.version == "0.154.0"
    assert first.started_at is not None
    assert first.process_key is not None
    assert first.process_key.startswith("41001-")
    assert first.ancestry is not None
    assert [ref.pid for ref in first.ancestry] == [41001, 7010, 7000]
    assert first.ancestry[0] == ProcessRef(pid=41001, ppid=7010, comm="codex")

    states = activities(rig)
    assert states[KEY1].state == "idle"
    assert states[KEY1].title is None  # the title is only the start of the prompt
    assert states[KEY1].last_prompt == "Add pagination to the orders endpoint"
    assert states[KEY1].current_tool is None
    assert states[KEY2].state == "busy"
    assert states[KEY2].title == "Login test failure"
    assert states[KEY2].current_tool == "exec_command"
    assert states[KEY2].current_target == "pytest tests/test_login.py -x --lf"
    assert states[KEY2].branch == "feature/login"


async def test_the_commands_are_pgrep_then_lsof_then_ps(rig: Rig) -> None:
    await rig.collector.poll()
    pgrep, lsof, ps = rig.runner.calls
    assert pgrep[0] == PGREP
    assert pgrep[1] == "-x"
    assert pgrep[-1] == "codex"
    assert lsof[:3] == (LSOF, "-nP", "-a")
    assert "41001,41002" in lsof
    assert "-Fpfn" in lsof
    assert ps[0] == PS
    assert all(timeout == 5.0 for timeout in rig.runner.timeouts)


async def test_the_process_table_is_read_once_while_the_processes_live(rig: Rig) -> None:
    await rig.collector.poll()
    await rig.collector.poll()
    await rig.collector.poll()
    assert [call[0] for call in rig.runner.calls].count(PS) == 1
    assert [call[0] for call in rig.runner.calls].count(PGREP) == 3


async def test_no_process_gives_an_empty_complete_snapshot_once(codex: CodexHome) -> None:
    rig = Rig(codex)
    rig.runner.script(PGREP, returncode=1)  # pgrep exits 1 when nothing matches
    assert await rig.collector.poll()
    assert sessions(rig) == {}
    assert LSOF not in [call[0] for call in rig.runner.calls]
    assert not await rig.collector.poll()  # unchanged: nothing to say
    report = await rig.collector.doctor()
    assert report.state is HealthState.OK
    assert report.detail is not None
    assert "0 codex processes" in report.detail


async def test_a_process_that_ends_closes_its_session(codex: CodexHome) -> None:
    rig = Rig(codex)
    script_processes(rig.runner, codex)
    await rig.collector.poll()
    later = FakeProcessRunner()
    script_processes(later, codex, pids=(41001,))
    rig.swap_runner(later)
    await rig.clock.advance(1.0)
    assert await rig.collector.poll()
    assert sorted(sessions(rig)) == [KEY1]
    assert sorted(sessions(rig, -2)) == [KEY1, KEY2]


async def test_a_process_without_an_open_rollout_is_not_a_session(codex: CodexHome) -> None:
    rig = Rig(codex)
    rig.runner.script(PGREP, stdout="41001\n41002\n41003\n")
    extra = "p41003\nfcwd\nn/work/example/notes\nf0\nn/dev/ttys006\n"
    rig.runner.script(LSOF, stdout=lsof_text(codex) + extra)
    rig.runner.script(PS, stdout="")
    await rig.collector.poll()
    assert sorted(sessions(rig)) == [KEY1, KEY2]
    assert rig.collector.stats.without_rollout == 1
    assert rig.collector.stats.processes == 3


async def test_a_rollout_outside_the_codex_directory_is_never_read(
    codex: CodexHome, isolated_env: Path
) -> None:
    outside = isolated_env / "elsewhere"
    outside.mkdir()
    stray = outside / codex.rollout(1).name
    shutil.copy(PROCESS_FIXTURES.parent / "rollouts" / "idle.jsonl", stray)
    rig = Rig(codex)
    rig.runner.script(PGREP, stdout="41001\n")
    rig.runner.script(LSOF, stdout=f"p41001\nfcwd\nn/work\nf3\nn{stray}\n")
    rig.runner.script(PS, stdout="")
    await rig.collector.poll()
    assert sessions(rig) == {}
    assert rig.collector.stats.without_rollout == 1


async def test_a_symlinked_codex_directory_matches_the_real_path_lsof_reports(
    isolated_env: Path,
) -> None:
    real = make_home(isolated_env)
    link = isolated_env / "link-home"
    link.symlink_to(real.home)
    codex = CodexHome(link, link / "sessions" / "2026" / "10" / "06")
    rig = Rig(codex)
    rig.runner.script(PGREP, stdout="41001\n")
    rig.runner.script(LSOF, stdout=lsof_text(real, pids=(41001,)))
    rig.runner.script(PS, stdout="")
    await rig.collector.poll()
    assert sorted(sessions(rig)) == [KEY1]


async def test_the_main_thread_wins_over_its_sub_agent_in_one_process(codex: CodexHome) -> None:
    rig = Rig(codex)
    rig.runner.script(PGREP, stdout="41001\n")
    rig.runner.script(
        LSOF,
        stdout=(
            "p41001\nfcwd\nn/work/example/notes\n"
            f"f3\nn{codex.rollout(6)}\nf4\nn{codex.rollout(4)}\n"
        ),
    )
    rig.runner.script(PS, stdout="")
    await rig.collector.poll()
    assert sorted(sessions(rig)) == [f"codex:{thread_id(4)}"]  # 6 is a child of 4


async def test_without_a_sub_agent_edge_the_oldest_rollout_wins(codex: CodexHome) -> None:
    rig = Rig(codex)
    rig.runner.script(PGREP, stdout="41001\n")
    rig.runner.script(
        LSOF, stdout=f"p41001\nfcwd\nn/w\nf3\nn{codex.rollout(2)}\nf4\nn{codex.rollout(1)}\n"
    )
    rig.runner.script(PS, stdout="")
    await rig.collector.poll()
    assert sorted(sessions(rig)) == [KEY1]  # 09:00 is older than 09:10


async def test_one_thread_open_in_two_processes_is_one_session(codex: CodexHome) -> None:
    rig = Rig(codex)
    rig.runner.script(PGREP, stdout="41001\n41002\n")
    rig.runner.script(
        LSOF,
        stdout=(
            f"p41001\nfcwd\nn/w\nf3\nn{codex.rollout(1)}\n"
            f"p41002\nfcwd\nn/w\nf3\nn{codex.rollout(1)}\n"
        ),
    )
    rig.runner.script(PS, stdout="")
    await rig.collector.poll()
    listed = sessions(rig)
    assert list(listed) == [KEY1]
    assert listed[KEY1].pid == 41001


async def test_a_thread_missing_from_the_database_is_listed_from_rollout_and_process(
    isolated_env: Path,
) -> None:
    codex = make_home(isolated_env)
    connection = sqlite3.connect(codex.home / "state_5.sqlite")
    connection.execute("DELETE FROM threads WHERE id = ?", [thread_id(2)])
    connection.commit()
    connection.close()
    rig = Rig(codex)
    script_processes(rig.runner, codex)
    await rig.collector.poll()
    second = sessions(rig)[KEY2]
    assert second.cwd == "/work/example/web-app"  # from the rollout's session_meta
    assert second.branch is None
    assert activities(rig)[KEY2].state == "busy"
    assert rig.collector.stats.without_row == 1
    report = await rig.collector.doctor()
    assert report.state is HealthState.OK


# --------------------------------------------------------------------------------------------
# Busy and idle follow the rollout
# --------------------------------------------------------------------------------------------


async def test_a_state_change_shows_in_the_next_poll(rig: Rig, codex: CodexHome) -> None:
    await rig.collector.poll()
    assert activities(rig)[KEY2].state == "busy"
    with codex.rollout(2).open("a", encoding="utf-8") as handle:
        handle.write(
            '{"type":"response_item","payload":{"type":"function_call_output","call_id":"call_b"}}\n'
            '{"type":"event_msg","payload":{"type":"task_complete","turn_id":"turn-2",'
            '"last_agent_message":"Fixed it."}}\n'
        )
    await rig.clock.advance(2.0)
    assert await rig.collector.poll()
    now = activities(rig)[KEY2]
    assert now.state == "idle"
    assert now.current_tool is None
    assert now.last_reply == "Fixed it."


async def test_an_unchanged_rollout_is_not_read_again(rig: Rig, monkeypatch: Any) -> None:
    reads: list[str] = []
    original = adapter.read_rollout_tail

    def spy(fs: Any, path: Path | str, limit: int) -> Any:
        reads.append(Path(path).name)
        return original(fs, path, limit)

    monkeypatch.setattr("fylgja.collectors.codex.collector.read_rollout_tail", spy)
    await rig.collector.poll()
    await rig.collector.poll()
    assert len(reads) == 2  # one per session, none in the second poll


async def test_the_tail_is_widened_until_a_turn_event_shows(
    isolated_env: Path,
) -> None:
    codex = make_home(isolated_env, threads=(1,))
    path = codex.rollout(1)
    head = path.read_text()
    filler = '{"type":"response_item","payload":{"type":"reasoning","summary":[]}}\n' * 400
    path.write_text(head + filler)  # the turn events are now far from the end
    rig = Rig(codex, tail_bytes=1024, tail_max_bytes=1 << 20)
    script_processes(rig.runner, codex, pids=(41001,))
    await rig.collector.poll()
    assert activities(rig)[KEY1].state == "idle"
    # With a ceiling too low to reach a turn event the state is unknown, not guessed.
    capped = Rig(codex, tail_bytes=1024, tail_max_bytes=2048)
    script_processes(capped.runner, codex, pids=(41001,))
    await capped.collector.poll()
    assert activities(capped)[KEY1].state is None


async def test_an_unreadable_rollout_leaves_the_session_listed(rig: Rig, codex: CodexHome) -> None:
    codex.rollout(2).unlink()
    await rig.collector.poll()
    assert sorted(sessions(rig)) == [KEY1, KEY2]
    assert activities(rig)[KEY2].state is None
    assert activities(rig)[KEY2].title == "Login test failure"
    assert rig.collector.stats.unreadable_rollouts == 1


async def test_a_session_is_re_emitted_every_heartbeat_and_not_in_between(rig: Rig) -> None:
    assert await rig.collector.poll()
    await rig.clock.advance(10.0)
    assert not await rig.collector.poll()
    await rig.clock.advance(25.0)
    assert await rig.collector.poll()
    assert rig.collector.emissions == 2


# --------------------------------------------------------------------------------------------
# Schema detection
# --------------------------------------------------------------------------------------------


async def test_an_unknown_state_version_is_degraded_and_the_database_is_not_opened(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex = make_home(isolated_env, db=False)
    (codex.home / "state_9.sqlite").write_bytes(b"\x00 a format nobody revalidated")
    opened: list[object] = []
    monkeypatch.setattr(
        "fylgja.collectors.codex.collector.StateDb.open", lambda path: opened.append(path)
    )
    rig = Rig(codex)
    script_processes(rig.runner, codex)
    await rig.collector.poll()
    report = await rig.collector.doctor()
    assert report.state is HealthState.DEGRADED
    assert report.error_kind == "unknown_state_schema"
    assert report.detail is not None
    assert "state_9.sqlite" in report.detail
    assert "known: 5" in report.detail
    assert opened == []  # nothing guessed from the file
    # The sessions are still there, from the process and the rollout alone.
    listed = sessions(rig)
    assert sorted(listed) == [KEY1, KEY2]
    assert listed[KEY1].branch is None
    assert listed[KEY1].cwd == "/work/example/api-service"
    assert activities(rig)[KEY1].state == "idle"
    assert activities(rig)[KEY2].title is None


async def test_the_newest_state_version_wins_even_when_an_older_one_is_known(
    codex: CodexHome,
) -> None:
    (codex.home / "state_6.sqlite").write_bytes(b"")
    rig = Rig(codex)
    script_processes(rig.runner, codex)
    await rig.collector.poll()
    assert (await rig.collector.doctor()).error_kind == "unknown_state_schema"
    assert rig.collector.stats.state_db == "state_6.sqlite"


async def test_a_known_version_with_a_changed_threads_table_is_a_schema_mismatch(
    isolated_env: Path,
) -> None:
    codex = make_home(isolated_env, db=False)
    connection = sqlite3.connect(codex.home / "state_5.sqlite")
    connection.execute("CREATE TABLE threads (id TEXT, title TEXT)")
    connection.commit()
    connection.close()
    rig = Rig(codex)
    script_processes(rig.runner, codex)
    await rig.collector.poll()
    report = await rig.collector.doctor()
    assert report.state is HealthState.DEGRADED
    assert report.error_kind == "state_schema_mismatch"
    assert sorted(sessions(rig)) == [KEY1, KEY2]


async def test_a_corrupt_database_is_degraded_and_recovers(isolated_env: Path) -> None:
    codex = make_home(isolated_env, db=False)
    path = codex.home / "state_5.sqlite"
    path.write_bytes(b"corrupt" * 100)
    rig = Rig(codex)
    script_processes(rig.runner, codex)
    await rig.collector.poll()
    assert (await rig.collector.doctor()).error_kind == "state_db_unreadable"
    path.unlink()
    build_state_db(path, codex.rollout_dir)
    await rig.clock.advance(1.0)
    await rig.collector.poll()
    assert (await rig.collector.doctor()).state is HealthState.OK
    assert sessions(rig)[KEY1].branch == "main"


async def test_a_missing_codex_directory_is_degraded(isolated_env: Path) -> None:
    codex = CodexHome(isolated_env / "nowhere", isolated_env / "nowhere" / "sessions")
    rig = Rig(codex)
    rig.runner.script(PGREP, returncode=1)
    before = await rig.collector.doctor()  # before the first poll
    assert before.error_kind == "codex_home_missing"
    await rig.collector.poll()
    assert (await rig.collector.doctor()).error_kind == "codex_home_missing"


async def test_a_codex_directory_that_is_a_file_or_unreadable_is_degraded(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    afile = isolated_env / "afile"
    afile.write_text("x")
    rig = Rig(CodexHome(afile, afile))
    assert (await rig.collector.doctor()).error_kind == "codex_home_missing"

    for error, kind in (
        (PermissionError(13, "denied"), "codex_home_unreadable"),
        (OSError(5, "io"), "codex_home_error"),
    ):
        rig = Rig(CodexHome(isolated_env, isolated_env))

        def listdir(path: Path | str, raised: OSError = error) -> list[str]:
            raise raised

        monkeypatch.setattr(rig.collector._seams.fs, "listdir", listdir)
        assert (await rig.collector.doctor()).error_kind == kind


async def test_a_directory_without_a_state_database_is_degraded(isolated_env: Path) -> None:
    codex = make_home(isolated_env, db=False)
    rig = Rig(codex)
    script_processes(rig.runner, codex)
    await rig.collector.poll()
    assert (await rig.collector.doctor()).error_kind == "state_db_missing"
    assert sorted(sessions(rig)) == [KEY1, KEY2]


# --------------------------------------------------------------------------------------------
# Command failures
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("script", "kind"),
    [
        (None, "pgrep_missing"),  # no script at all: FileNotFoundError
        ({"timeout": True}, "pgrep_timeout"),
        ({"returncode": 2, "stderr": "bad option"}, "pgrep_failed"),
        ({"error": PermissionError(13, "no")}, "pgrep_not_executable"),
        ({"error": OSError(5, "io")}, "pgrep_failed"),
    ],
)
async def test_a_failing_pgrep_degrades_and_emits_nothing(
    codex: CodexHome, script: dict[str, Any] | None, kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    rig = Rig(codex)
    if script is not None:
        rig.runner.script(PGREP, **script)
    with caplog.at_level(logging.INFO, logger="fylgja.collector"):
        assert not await rig.collector.poll()
        assert not await rig.collector.poll()
    report = await rig.collector.doctor()
    assert report.state is HealthState.DEGRADED
    assert report.error_kind == kind
    assert rig.sink.records == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1  # logged once, not every poll


async def test_a_failing_lsof_degrades_and_keeps_the_last_state(rig: Rig, codex: CodexHome) -> None:
    await rig.collector.poll()
    count = len(rig.sink.records)
    broken = FakeProcessRunner()
    broken.script(PGREP, stdout="41001\n41002\n")  # lsof has no script: it is missing
    rig.swap_runner(broken)
    await rig.clock.advance(1.0)
    assert not await rig.collector.poll()
    report = await rig.collector.doctor()
    assert report.error_kind == "lsof_missing"
    assert len(rig.sink.records) == count


async def test_the_collector_recovers_when_the_command_returns(
    codex: CodexHome, caplog: pytest.LogCaptureFixture
) -> None:
    rig = Rig(codex)
    with caplog.at_level(logging.INFO, logger="fylgja.collector"):
        await rig.collector.poll()  # pgrep is missing
        assert (await rig.collector.doctor()).state is HealthState.DEGRADED
        script_processes(rig.runner, codex)
        await rig.clock.advance(1.0)
        assert await rig.collector.poll()
    assert (await rig.collector.doctor()).state is HealthState.OK
    assert any("recovered" in r.getMessage() for r in caplog.records)


async def test_a_failing_ps_costs_only_the_ancestry(codex: CodexHome) -> None:
    rig = Rig(codex)
    rig.runner.script(PGREP, stdout="41001\n")
    rig.runner.script(LSOF, stdout=lsof_text(codex, pids=(41001,)))
    rig.runner.script(PS, returncode=2)
    await rig.collector.poll()
    listed = sessions(rig)[KEY1]
    assert listed.ancestry is None
    assert listed.process_key is None
    assert (await rig.collector.doctor()).state is HealthState.OK
    # Once ps answers, the next poll fills the chain in.
    fixed = FakeProcessRunner()
    script_processes(fixed, codex, pids=(41001,))
    rig.swap_runner(fixed)
    await rig.clock.advance(1.0)
    assert await rig.collector.poll()
    assert sessions(rig)[KEY1].ancestry is not None


# --------------------------------------------------------------------------------------------
# The loop, logging and settings
# --------------------------------------------------------------------------------------------


async def test_run_polls_on_the_interval_and_stops_when_cancelled(rig: Rig) -> None:
    task = asyncio.create_task(rig.collector.run())
    for _ in range(7):
        await rig.clock.advance(1.0)
    task.cancel()
    await asyncio.wait({task})
    assert rig.clock.sleeps[:3] == [2.0, 2.0, 2.0]
    assert [c[0] for c in rig.runner.calls].count(PGREP) == 4  # t=0, 2, 4, 6
    assert rig.clock.pending == 0


async def test_an_unexpected_error_in_a_poll_is_logged_once_and_the_loop_goes_on(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = 0

    async def poll() -> bool:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom with a secret title")

    monkeypatch.setattr(rig.collector, "poll", poll)
    with caplog.at_level(logging.INFO, logger="fylgja.collector"):
        task = asyncio.create_task(rig.collector.run())
        for _ in range(7):
            await rig.clock.advance(1.0)
        report = await rig.collector.doctor()
        task.cancel()
        await asyncio.wait({task})
    assert calls >= 3
    assert report.state is HealthState.DEGRADED
    assert report.error_kind == "poll_error"
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "secret" not in caplog.text
    assert "RuntimeError" in errors[0].getMessage()


async def test_nothing_of_the_session_contents_is_logged(
    rig: Rig, codex: CodexHome, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        await rig.collector.poll()
        (codex.home / "state_7.sqlite").write_bytes(b"")
        await rig.collector.poll()
    for text in ("pagination", "login", "pytest", "api-service", thread_id(1)):
        assert text not in caplog.text


def test_settings_reject_unknown_keys_and_bad_values() -> None:
    assert CodexSettings().poll_interval_seconds == 2.0
    with pytest.raises(ValidationError):
        CodexSettings(unknown=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        CodexSettings(poll_interval_seconds=0)
    with pytest.raises(ValidationError):
        CodexSettings(process_name="")


class _Foreign(BaseModel):
    pass


def test_the_factory_builds_a_collector_over_the_default_home(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fylgja.collectors.codex.collector import CodexCollector
    from fylgja.collectors.context import build_context
    from fylgja.testing.fakes import FakeClock, RecordingSink

    context = build_context("codex", RecordingSink(), clock=FakeClock())
    built = plugin.factory(CodexSettings(), context)
    assert isinstance(built, CodexCollector)
    assert built._home == isolated_env / "home" / ".codex"
    with pytest.raises(TypeError):
        plugin.factory(_Foreign(), context)


def test_the_plugin_declares_only_session_capability() -> None:
    assert plugin.name == "codex"
    assert {c.value for c in plugin.capabilities} == {"sessions"}
    assert parse.SOURCE == plugin.name
