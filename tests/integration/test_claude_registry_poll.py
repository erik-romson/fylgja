"""The Claude registry poll over a real temporary directory, a fake ``ps`` and a fake clock.

Covers the WP1.7 done condition: an in-place rewrite and an atomic replace are both seen although
the directory mtime does not change, plus added and removed rows, a reused pid, ``*.key`` files that
are never opened, the 60 s backstop, ``ignore_cwd``, failures and health.
"""

from __future__ import annotations

import asyncio
import builtins
import io
import json
import logging
import os
import sys
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest
from claude_support import T0, World, copy_fixture_rows, rewrite_in_place, row_data, write_row

from fylgja import paths
from fylgja.collectors.claude import adapter, parse
from fylgja.collectors.claude.collector import ClaudeCollector, ClaudeSettings, ignore_list
from fylgja.collectors.claude.registry import RegistryPoller
from fylgja.collectors.context import build_context
from fylgja.collectors.seams import FileInfo, FileSystem
from fylgja.models import (
    HealthState,
    SessionActivity,
    SessionActivitySnapshot,
    SessionPayload,
    SessionSnapshot,
)
from fylgja.testing.fakes import FakeClock, FakeFS, RecordingSink


def sessions(world: World) -> dict[str, SessionPayload]:
    """The sessions of the last emitted complete snapshot, by entity id."""
    snapshots = [r for r in world.sink.records if isinstance(r, SessionSnapshot)]
    assert snapshots, "nothing was emitted"
    return {item.entity_id: item.payload for item in snapshots[-1].items}


def activities(world: World) -> dict[str, SessionActivity]:
    snapshots = [r for r in world.sink.records if isinstance(r, SessionActivitySnapshot)]
    assert snapshots, "nothing was emitted"
    return {item.entity_id: item.payload for item in snapshots[-1].items}


def emissions(world: World) -> int:
    return sum(isinstance(r, SessionSnapshot) for r in world.sink.records)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path / "sessions")


# --------------------------------------------------------------------------------------------
# Writes: in place and atomic replace, directory mtime never a gate
# --------------------------------------------------------------------------------------------


async def test_an_in_place_rewrite_is_observed_though_the_directory_mtime_is_unchanged(
    world: World,
) -> None:
    path = world.add(100, "s-100", status="idle", name="old name")
    assert await world.collector.poll()
    assert sessions(world)["claude:s-100"].name == "old name"
    assert activities(world)["claude:s-100"].state == "idle"
    directory_before = world.registry.stat()
    inode_before = path.stat().st_ino

    rewrite_in_place(path, row_data(100, "s-100", status="busy", name="a new, longer name"))
    os.utime(world.registry, ns=(directory_before.st_atime_ns, directory_before.st_mtime_ns))

    assert path.stat().st_ino == inode_before, "the rewrite must keep the inode"
    assert world.registry.stat().st_mtime_ns == directory_before.st_mtime_ns
    await world.clock.advance(2)
    assert await world.collector.poll()
    assert sessions(world)["claude:s-100"].name == "a new, longer name"
    assert activities(world)["claude:s-100"].state == "working"


async def test_an_atomic_replace_is_observed_though_the_directory_mtime_is_set_back(
    world: World,
) -> None:
    path = world.add(100, "s-100", status="idle", name="old name")
    assert await world.collector.poll()
    directory_before = world.registry.stat()
    inode_before = path.stat().st_ino

    world.add(100, "s-100", status="busy", name="new name")
    os.utime(world.registry, ns=(directory_before.st_atime_ns, directory_before.st_mtime_ns))

    assert path.stat().st_ino != inode_before, "an atomic replace gives a new inode"
    assert world.registry.stat().st_mtime_ns == directory_before.st_mtime_ns
    await world.clock.advance(2)
    assert await world.collector.poll()
    assert sessions(world)["claude:s-100"].name == "new name"
    assert activities(world)["claude:s-100"].state == "working"


async def test_a_rewrite_that_keeps_inode_and_size_is_seen_through_its_mtime(world: World) -> None:
    path = world.add(100, "s-100", status="idle")
    await world.collector.poll()
    before = path.stat()
    rewrite_in_place(path, row_data(100, "s-100", status="busy"))  # same length as "idle"
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 5_000_000_000))
    assert path.stat().st_size == before.st_size
    await world.clock.advance(2)
    assert await world.collector.poll()
    assert activities(world)["claude:s-100"].state == "working"


async def test_the_backstop_reparses_a_rewrite_that_changed_no_stat_value(world: World) -> None:
    path = world.add(100, "s-100", status="idle")
    await world.collector.poll()
    before = path.stat()
    rewrite_in_place(path, row_data(100, "s-100", status="busy"))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))  # inode, mtime and size all equal
    assert (path.stat().st_ino, path.stat().st_mtime_ns, path.stat().st_size) == (
        before.st_ino,
        before.st_mtime_ns,
        before.st_size,
    )

    for _ in range(29):  # 58 s of 2 s polls: every stat signature is unchanged
        await world.clock.advance(2)
        assert not await world.collector.poll() or activities(world)["claude:s-100"].state == "idle"
    assert activities(world)["claude:s-100"].state == "idle"

    await world.clock.advance(2)  # the 60 s full reparse
    assert await world.collector.poll()
    assert activities(world)["claude:s-100"].state == "working"


async def test_a_poll_reads_only_changed_files_between_full_reparses(world: World) -> None:
    for pid in (100, 101, 102):
        world.add(pid, f"s-{pid}")
    poller = RegistryPoller(adapter.LocalFileSystem(), world.registry)
    first = poller.poll(0.0)
    assert (first.reads, first.full_reparse) == (3, True)
    assert first.added == {100, 101, 102}
    quiet = poller.poll(2.0)
    assert (quiet.reads, quiet.full_reparse, quiet.added, quiet.changed) == (0, False, set(), set())
    world.add(101, "s-101", status="busy")
    changed = poller.poll(4.0)
    assert (changed.reads, changed.changed) == (1, {101})
    full = poller.poll(60.0)
    assert (full.reads, full.full_reparse, full.changed) == (3, True, set())
    assert poller.total_reads == 3 + 1 + 3


# --------------------------------------------------------------------------------------------
# Added, removed, stale and filtered rows
# --------------------------------------------------------------------------------------------


async def test_added_and_removed_rows_open_and_close_sessions(world: World) -> None:
    world.add(100, "s-100")
    assert await world.collector.poll()
    assert set(sessions(world)) == {"claude:s-100"}

    world.add(101, "s-101")
    await world.clock.advance(2)
    assert await world.collector.poll()
    assert set(sessions(world)) == {"claude:s-100", "claude:s-101"}

    (world.registry / "100.json").unlink()
    await world.clock.advance(2)
    assert await world.collector.poll()
    assert set(sessions(world)) == {"claude:s-101"}, "a complete snapshot closes the removed row"
    snapshot = [r for r in world.sink.records if isinstance(r, SessionSnapshot)][-1]
    assert snapshot.complete


async def test_a_session_payload_carries_the_registry_fields(world: World) -> None:
    world.add(100, "s-100", version="2.1.284", entrypoint="sdk-py", bridgeSessionId="session_abc")
    await world.collector.poll()
    payload = sessions(world)["claude:s-100"]
    assert (payload.kind, payload.native_id, payload.pid) == ("claude", "s-100", 100)
    assert payload.version == "2.1.284"
    assert payload.entrypoint == "sdk-py"
    assert payload.peer_protocol == 1
    assert payload.remote_url == "https://claude.ai/code/session_abc"
    assert payload.process_key == f"100-{int(T0)}"
    assert payload.predecessor_key is None
    assert payload.started_at is not None


async def test_a_stale_row_whose_pid_was_reused_is_not_live(world: World) -> None:
    world.add(100, "s-live")
    world.add(101, "s-reused")
    world.ps.starts[101] = T0 + 3600  # another process now has pid 101
    world.add(102, "s-dead")
    del world.ps.starts[102]  # nobody has pid 102
    assert await world.collector.poll()
    assert set(sessions(world)) == {"claude:s-live"}
    assert world.collector.stats.reused == 1
    assert world.collector.stats.dead == 1
    assert world.collector.stats.live == 1


async def test_a_row_that_becomes_stale_closes_its_session(world: World) -> None:
    world.add(100, "s-100")
    await world.collector.poll()
    assert set(sessions(world)) == {"claude:s-100"}
    world.ps.starts[100] = T0 + 99  # the process exited and the pid was reused
    await world.clock.advance(2)
    assert await world.collector.poll()
    assert sessions(world) == {}


async def test_ps_is_asked_once_per_poll_for_every_candidate_pid(world: World) -> None:
    for pid in (100, 101, 102):
        world.add(pid, f"s-{pid}")
    await world.collector.poll()
    assert world.ps.calls == [("/bin/ps", "-o", "pid=,lstart=", "-p", "100,101,102")]


async def test_no_ps_call_is_made_when_there_is_no_row(world: World) -> None:
    assert await world.collector.poll()
    assert world.ps.calls == []
    assert sessions(world) == {}


async def test_ignore_cwd_drops_probe_and_summarizer_sessions(tmp_path: Path) -> None:
    summarizer = paths.summarizer_dir()
    settings = ClaudeSettings()
    ignore = ignore_list(settings)
    assert ignore == ("CodexBar/ClaudeProbe", str(summarizer))
    world = World(tmp_path / "sessions", ignore_cwd=ignore)
    world.add(100, "s-keep", cwd="/work/project")
    world.add(101, "s-probe", cwd="/Users/u/Library/Caches/CodexBar/ClaudeProbe")
    world.add(102, "s-summary", cwd=str(summarizer))
    world.add(103, "s-nested", cwd=str(summarizer / "job-1"))
    await world.collector.poll()
    assert set(sessions(world)) == {"claude:s-keep"}
    assert world.collector.stats.ignored == 3
    assert world.ps.calls[0][-1] == "100", "ignored rows are not even looked up"


async def test_ignore_cwd_comes_from_the_settings(tmp_path: Path) -> None:
    settings = ClaudeSettings(ignore_cwd=["scratch/tmp"])
    world = World(tmp_path / "sessions", settings=settings, ignore_cwd=ignore_list(settings))
    world.add(100, "s-a", cwd="/x/scratch/tmp/y")
    world.add(101, "s-b", cwd="/Users/u/CodexBar/ClaudeProbe")
    await world.collector.poll()
    assert set(sessions(world)) == {"claude:s-b"}


# --------------------------------------------------------------------------------------------
# Key files and other names are never opened
# --------------------------------------------------------------------------------------------


async def test_a_key_file_created_at_runtime_is_never_opened(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.add(100, "s-100")
    key = world.registry / "100.0123456789abcdef.key"
    key.write_text("not a real key", encoding="utf-8")
    (world.registry / "100.json.bak").write_text("{}", encoding="utf-8")
    (world.registry / "notes.txt").write_text("x", encoding="utf-8")
    (world.registry / "abc.json").write_text("{}", encoding="utf-8")

    opened: list[str] = []
    real_open = builtins.open
    real_io_open = io.open

    def spy(file: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(os.fspath(file) if isinstance(file, str | os.PathLike) else repr(file))
        return real_open(file, *args, **kwargs)

    def io_spy(file: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(os.fspath(file) if isinstance(file, str | os.PathLike) else repr(file))
        return real_io_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    monkeypatch.setattr(io, "open", io_spy)
    for _ in range(31):  # past the 60 s full reparse
        await world.collector.poll()
        await world.clock.advance(2)
    monkeypatch.undo()

    assert opened, "the spy saw no open at all"
    assert all(path.endswith("100.json") for path in opened), opened
    assert not any(path.endswith(".key") for path in opened)
    assert world.collector.stats.non_row_files == 4
    assert set(sessions(world)) == {"claude:s-100"}


async def test_the_doctor_counts_other_files_and_touches_no_key_file(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.add(100, "s-100")
    (world.registry / "100.ffff.key").write_text("k", encoding="utf-8")
    await world.collector.poll()
    opened: list[str] = []
    real_open = builtins.open

    def spy(file: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(os.fspath(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    report = await world.collector.doctor()
    monkeypatch.undo()
    assert report.state is HealthState.OK
    assert "1 other files left unopened" in (report.detail or "")
    assert not any(path.endswith(".key") for path in opened)


# --------------------------------------------------------------------------------------------
# Emission cadence and the loop
# --------------------------------------------------------------------------------------------


async def test_unchanged_polls_emit_nothing_until_the_heartbeat(world: World) -> None:
    world.add(100, "s-100")
    assert await world.collector.poll()
    for _ in range(14):
        await world.clock.advance(2)
        assert not await world.collector.poll()
    assert emissions(world) == 1
    await world.clock.advance(2)  # 30 s since the last emission
    assert await world.collector.poll()
    assert emissions(world) == 2
    assert world.collector.emissions == 2


async def test_sessions_and_activity_are_emitted_together(world: World) -> None:
    world.add(100, "s-100", status="waiting", waitingFor="input needed")
    await world.collector.poll()
    kinds = [type(r).__name__ for r in world.sink.records]
    assert kinds == ["SessionSnapshot", "SessionActivitySnapshot"]
    activity = activities(world)["claude:s-100"]
    assert (activity.state, activity.waiting_reason) == ("waiting", "input needed")


async def test_the_loop_polls_every_two_seconds_and_stops_when_cancelled(world: World) -> None:
    world.add(100, "s-100")
    task = asyncio.create_task(world.collector.run())
    try:
        await world.clock.advance(7)
        assert world.clock.sleeps == [2.0, 2.0, 2.0, 2.0]
        assert len(world.ps.calls) == 4
        world.add(101, "s-101")
        await world.clock.advance(2)
        assert set(sessions(world)) == {"claude:s-100", "claude:s-101"}
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    assert world.clock.pending == 0


async def test_a_torn_row_keeps_the_previous_session_until_the_file_is_whole(
    world: World,
) -> None:
    path = world.add(100, "s-100", name="whole")
    await world.collector.poll()
    path.write_text('{"pid": 100, "sessionId": "s-100", "cw', encoding="utf-8")  # mid-rewrite
    await world.clock.advance(2)
    assert not await world.collector.poll()
    assert world.collector.stats.rejected == 1
    assert set(sessions(world)) == {"claude:s-100"}, "a torn read must not close the session"

    rewrite_in_place(path, row_data(100, "s-100", name="healed"))
    await world.clock.advance(2)
    assert await world.collector.poll()
    assert sessions(world)["claude:s-100"].name == "healed"
    assert world.collector.stats.rejected == 0


async def test_a_new_file_that_is_not_a_row_is_counted_not_emitted(world: World) -> None:
    world.add(100, "s-100")
    (world.registry / "101.json").write_text("not json", encoding="utf-8")
    (world.registry / "102.json").write_text('{"pid": 999}', encoding="utf-8")
    await world.collector.poll()
    assert set(sessions(world)) == {"claude:s-100"}
    assert world.collector.stats.rejected == 2
    assert world.collector.stats.files == 3


async def test_fixture_rows_all_come_alive_with_their_proc_start(world: World) -> None:
    copy_fixture_rows(world.registry)
    for path in sorted(world.registry.glob("*.json")):
        row = parse.parse_row(path.read_bytes(), file_pid=int(path.stem))
        assert isinstance(row, parse.RegistryRow)
        started = (
            row.proc_start.replace(tzinfo=UTC).timestamp()
            if row.proc_start is not None
            else (row.started_at_ms or 0) / 1000 - 5
        )
        world.ps.starts[row.pid] = started
    await world.collector.poll()
    live = sessions(world)
    assert len(live) == 5
    assert sorted(p.version or "" for p in live.values()) == ["2.1.284"] + ["2.1.289"] * 4
    assert world.collector.stats.versions == ("2.1.284", "2.1.289")
    assert {p.entrypoint for p in live.values()} == {"cli", "sdk-py"}
    assert sum(p.remote_url is not None for p in live.values()) == 1
    assert {a.state for a in activities(world).values()} == {"idle", "waiting", "working"}


# --------------------------------------------------------------------------------------------
# Failures and health
# --------------------------------------------------------------------------------------------


async def test_a_missing_registry_degrades_without_crashing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    world = World(tmp_path / "sessions")
    (tmp_path / "sessions").rmdir()
    caplog.set_level(logging.INFO)
    for _ in range(3):
        assert not await world.collector.poll()
    report = await world.collector.doctor()
    assert (report.state, report.error_kind) == (HealthState.DEGRADED, "registry_missing")
    assert world.sink.records == []
    assert len([r for r in caplog.records if "registry failed" in r.getMessage()]) == 1

    (tmp_path / "sessions").mkdir()
    world.add(100, "s-100")
    assert await world.collector.poll()
    assert (await world.collector.doctor()).state is HealthState.OK
    assert any("registry recovered" in r.getMessage() for r in caplog.records)


async def test_a_registry_that_is_a_file_degrades(tmp_path: Path) -> None:
    world = World(tmp_path / "sessions")
    (tmp_path / "sessions").rmdir()
    (tmp_path / "sessions").write_text("x", encoding="utf-8")
    assert not await world.collector.poll()
    assert (await world.collector.doctor()).error_kind == "registry_missing"


@pytest.mark.skipif(sys.platform != "darwin" or os.geteuid() == 0, reason="needs chmod to deny")
async def test_an_unreadable_registry_degrades(tmp_path: Path) -> None:
    world = World(tmp_path / "sessions")
    world.registry.chmod(0o000)
    try:
        assert not await world.collector.poll()
        assert (await world.collector.doctor()).error_kind == "registry_unreadable"
    finally:
        world.registry.chmod(0o700)


async def test_another_listing_error_degrades_as_registry_error(tmp_path: Path) -> None:
    class Failing(adapter.LocalFileSystem):
        def listdir(self, path: Path | str) -> list[str]:
            raise OSError(5, "input/output error")

    world = World(tmp_path / "sessions")
    world.collector = ClaudeCollector(
        world.settings,
        build_context("claude", world.sink, clock=world.clock),
        registry_dir=world.registry,
        seams=adapter.Seams(world.ps, Failing()),
        ignore_cwd=(),
    )
    assert not await world.collector.poll()
    assert (await world.collector.doctor()).error_kind == "registry_error"


@pytest.mark.parametrize(
    ("setup", "kind"),
    [
        (lambda ps: setattr(ps, "error", FileNotFoundError("ps")), "ps_missing"),
        (lambda ps: setattr(ps, "error", PermissionError("ps")), "ps_not_executable"),
        (lambda ps: setattr(ps, "error", TimeoutError("ps")), "ps_timeout"),
        (lambda ps: setattr(ps, "error", OSError(12, "no memory")), "ps_failed"),
        (lambda ps: setattr(ps, "returncode", 2), "ps_failed"),
    ],
)
async def test_a_failing_ps_degrades_and_keeps_the_last_sessions(
    world: World, setup: Callable[[Any], None], kind: str
) -> None:
    world.add(100, "s-100")
    await world.collector.poll()
    assert set(sessions(world)) == {"claude:s-100"}
    count = emissions(world)

    setup(world.ps)
    await world.clock.advance(2)
    assert not await world.collector.poll()
    assert emissions(world) == count, "a failing ps must not close live sessions"
    report = await world.collector.doctor()
    assert (report.state, report.error_kind) == (HealthState.DEGRADED, kind)

    world.ps.error = None
    world.ps.returncode = None
    await world.clock.advance(2)
    await world.collector.poll()
    assert (await world.collector.doctor()).state is HealthState.OK


async def test_ps_output_that_does_not_parse_means_dead_rows(world: World) -> None:
    world.add(100, "s-100")
    world.ps.stdout = b"garbage\n"
    world.ps.returncode = 0
    await world.collector.poll()
    assert sessions(world) == {}
    assert world.collector.stats.dead == 1


async def test_a_directory_of_unusable_rows_is_degraded(world: World) -> None:
    (world.registry / "100.json").write_text("nope", encoding="utf-8")
    (world.registry / "101.json").write_text("[]", encoding="utf-8")
    await world.collector.poll()
    report = await world.collector.doctor()
    assert (report.state, report.error_kind) == (HealthState.DEGRADED, "unparseable_rows")


async def test_some_unusable_rows_beside_good_ones_stay_ok(world: World) -> None:
    world.add(100, "s-100", version="2.1.284")
    world.add(101, "s-101", version="2.1.289")
    (world.registry / "102.json").write_text("nope", encoding="utf-8")
    await world.collector.poll()
    report = await world.collector.doctor()
    assert report.state is HealthState.OK
    assert report.detail is not None
    assert "2 live of 2 rows" in report.detail
    assert "versions 2.1.284, 2.1.289" in report.detail
    assert "1 rejected" in report.detail


async def test_the_doctor_before_any_poll_checks_the_directory(tmp_path: Path) -> None:
    world = World(tmp_path / "sessions")
    report = await world.collector.doctor()
    assert report.state is HealthState.OK
    assert world.ps.calls == [], "the doctor does not run ps"
    assert world.sink.records == []

    (tmp_path / "sessions").rmdir()
    assert (await world.collector.doctor()).error_kind == "registry_missing"


# --------------------------------------------------------------------------------------------
# Races while listing and statting
# --------------------------------------------------------------------------------------------


class _StatRaces(adapter.LocalFileSystem):
    def __init__(self) -> None:
        self.errors: dict[str, OSError] = {}

    def stat(self, path: Path | str) -> FileInfo:
        error = self.errors.get(Path(path).name)
        if error is not None:
            raise error
        return super().stat(path)


async def test_a_file_that_vanishes_between_listing_and_stat_is_treated_as_removed(
    tmp_path: Path,
) -> None:
    fs = _StatRaces()
    directory = tmp_path / "sessions"
    directory.mkdir()
    write_row(directory, row_data(100, "s-100"))
    poller = RegistryPoller(fs, directory)
    assert set(poller.poll(0.0).rows) == {100}
    fs.errors["100.json"] = FileNotFoundError("gone")
    result = poller.poll(2.0)
    assert result.rows == {}
    fs.errors.clear()
    assert set(poller.poll(4.0).rows) == {100}


async def test_a_file_that_cannot_be_statted_is_rejected_and_the_old_row_is_kept(
    tmp_path: Path,
) -> None:
    fs = _StatRaces()
    directory = tmp_path / "sessions"
    directory.mkdir()
    write_row(directory, row_data(100, "s-100"))
    poller = RegistryPoller(fs, directory)
    poller.poll(0.0)
    fs.errors["100.json"] = PermissionError("denied")
    result = poller.poll(2.0)
    assert result.rejected == {100: "unreadable"}
    assert set(result.rows) == {100}


async def test_a_file_that_cannot_be_read_is_rejected(tmp_path: Path) -> None:
    class Unreadable(adapter.LocalFileSystem):
        def __init__(self) -> None:
            self.error: OSError | None = None

        def read_bytes(self, path: Path | str) -> bytes:
            if self.error is not None:
                raise self.error
            return super().read_bytes(path)

    fs = Unreadable()
    directory = tmp_path / "sessions"
    directory.mkdir()
    write_row(directory, row_data(100, "s-100"))
    fs.error = PermissionError("denied")
    poller = RegistryPoller(fs, directory)
    assert poller.poll(0.0).rejected == {100: "unreadable"}
    fs.error = FileNotFoundError("gone")
    assert poller.poll(2.0).rejected == {100: "vanished"}
    fs.error = None
    result = poller.poll(4.0)
    assert (result.rejected, set(result.rows), result.added) == ({}, {100}, {100})


async def test_the_poller_works_over_the_in_memory_filesystem() -> None:
    fs = FakeFS()
    directory = Path("/home/u/.claude/sessions")
    first = row_data(100, "s-100", status="idle")
    fs.add_file(directory / "100.json", json.dumps(first), mtime=1.0)
    fs.add_file(directory / "100.abc.key", "secret", mtime=1.0)
    poller = RegistryPoller(fs, directory)
    assert set(poller.poll(0.0).rows) == {100}
    assert [str(p) for p in fs.reads] == ["/home/u/.claude/sessions/100.json"]

    second = row_data(100, "s-100", status="busy")
    fs.add_file(directory / "100.json", json.dumps(second), mtime=2.0)  # in place
    result = poller.poll(2.0)
    assert result.changed == {100}
    fs.replace_file(directory / "100.json", json.dumps(first), mtime=2.0)  # new inode
    assert poller.poll(4.0).changed == {100}


# --------------------------------------------------------------------------------------------
# Plugin and adapter
# --------------------------------------------------------------------------------------------


async def test_the_plugin_factory_reads_the_default_registry_under_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fylgja.collectors.claude import plugin

    registry = Path(os.environ["HOME"]) / ".claude" / "sessions"
    registry.mkdir(parents=True)
    write_row(registry, row_data(100, "s-100"))
    seen: list[int] = []

    def fake_seams() -> adapter.Seams:
        class Runner(adapter.SubprocessRunner):
            async def run(self, argv: Any, **kwargs: Any) -> Any:
                seen.append(len(argv))
                return await super().run([sys.executable, "-c", "pass"], **kwargs)

        return adapter.Seams(Runner(), adapter.LocalFileSystem())

    monkeypatch.setattr(adapter, "default_seams", fake_seams)
    sink = RecordingSink()
    collector = plugin.factory(
        plugin.config_model(), build_context("claude", sink, clock=FakeClock())
    )
    report = await collector.doctor()
    assert report.state is HealthState.OK
    assert adapter.default_registry_dir() == registry


async def test_the_plugin_factory_refuses_foreign_settings() -> None:
    from pydantic import BaseModel

    from fylgja.collectors.claude import plugin

    class Foreign(BaseModel):
        pass

    with pytest.raises(TypeError):
        plugin.factory(Foreign(), build_context("claude", RecordingSink()))


async def test_the_plugin_factory_uses_a_configured_registry_dir(tmp_path: Path) -> None:
    from fylgja.collectors.claude import plugin

    collector = plugin.factory(
        ClaudeSettings(registry_dir=tmp_path / "elsewhere"),
        build_context("claude", RecordingSink(), clock=FakeClock()),
    )
    assert (await collector.doctor()).error_kind == "registry_missing"


def test_the_local_filesystem_reads_stats_and_lists(tmp_path: Path) -> None:
    fs: FileSystem = adapter.LocalFileSystem()
    (tmp_path / "b.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    assert fs.listdir(tmp_path) == ["a.txt", "b.txt"]
    assert fs.read_bytes(tmp_path / "b.txt") == b"hello"
    assert fs.read_text(tmp_path / "b.txt") == "hello"
    assert fs.exists(tmp_path / "b.txt")
    assert not fs.exists(tmp_path / "nope")
    assert fs.is_dir(tmp_path)
    assert not fs.is_dir(tmp_path / "b.txt")
    info = fs.stat(tmp_path / "b.txt")
    real = (tmp_path / "b.txt").stat()
    assert (info.size, info.ino, info.mtime_ns, info.is_dir) == (
        5,
        real.st_ino,
        real.st_mtime_ns,
        False,
    )
    assert fs.stat(tmp_path).is_dir
    fs.unlink(tmp_path / "a.txt")
    assert not fs.exists(tmp_path / "a.txt")


async def test_the_subprocess_runner_runs_commands() -> None:
    runner = adapter.SubprocessRunner()
    result = await runner.run([sys.executable, "-c", "print('hi')"])
    assert (result.returncode, result.stdout.strip()) == (0, b"hi")
    echoed = await runner.run(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"], stdin=b"abc"
    )
    assert echoed.stdout == b"abc"
    failed = await runner.run([sys.executable, "-c", "raise SystemExit(3)"])
    assert failed.returncode == 3
    with pytest.raises(TimeoutError):
        await runner.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.3)
    with pytest.raises(FileNotFoundError):
        await runner.run(["/nonexistent/definitely-not-here"])


async def test_ps_start_times_with_the_real_ps_see_this_process() -> None:
    if sys.platform != "darwin":
        pytest.skip("macOS ps")
    starts = await adapter.ps_start_times(
        adapter.SubprocessRunner(), "/bin/ps", [os.getpid(), 99990], timeout=5
    )
    assert set(starts) == {os.getpid()}
    import time

    assert abs(starts[os.getpid()] - time.time()) < 3600
