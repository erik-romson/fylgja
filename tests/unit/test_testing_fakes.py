"""The public fakes in ``fylgja.testing`` behave as documented."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fylgja.collectors.base import Clock, RecordSink
from fylgja.collectors.seams import FileSystem, LineStream, PathResolver, ProcessRunner
from fylgja.models import EntityKind, PresenceEvent, PresencePayload, TodoEvent, TodoPayload
from fylgja.testing import fixtures
from fylgja.testing.fakes import (
    FakeClock,
    FakeFS,
    FakeLineStream,
    FakeProcessRunner,
    RecordingSink,
    settle,
)

# --- clock ----------------------------------------------------------------------------


def test_clock_satisfies_the_protocol_and_starts_at_the_epoch() -> None:
    clock = FakeClock()
    assert isinstance(clock, Clock)
    assert clock.now() == datetime(2026, 1, 1, tzinfo=UTC)
    assert clock.monotonic() == 0.0


def test_clock_rejects_a_naive_start() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FakeClock(datetime(2026, 1, 1))  # noqa: DTZ001


async def test_sleep_blocks_until_time_is_advanced() -> None:
    clock = FakeClock(datetime(2030, 5, 1, tzinfo=UTC))
    woke: list[float] = []

    async def sleeper() -> None:
        await clock.sleep(10)
        woke.append(clock.monotonic())

    task = asyncio.create_task(sleeper())
    await settle()
    assert clock.pending == 1
    await clock.advance(9.9)
    assert woke == []
    await clock.advance(0.1)
    assert woke == [10.0]
    assert clock.now() == datetime(2030, 5, 1, tzinfo=UTC) + timedelta(seconds=10)
    assert clock.sleeps == [10]
    await task
    assert clock.pending == 0


async def test_sleepers_wake_in_deadline_order_and_time_follows_each() -> None:
    clock = FakeClock()
    order: list[tuple[str, float]] = []

    async def sleeper(name: str, seconds: float) -> None:
        await clock.sleep(seconds)
        order.append((name, clock.monotonic()))

    tasks = [
        asyncio.create_task(sleeper("late", 5)),
        asyncio.create_task(sleeper("early", 2)),
    ]
    await clock.advance(10)
    assert order == [("early", 2.0), ("late", 5.0)]
    assert clock.monotonic() == 10.0
    await asyncio.gather(*tasks)


async def test_non_positive_sleep_returns_at_once_and_advance_never_goes_back() -> None:
    clock = FakeClock()
    await clock.sleep(0)
    await clock.sleep(-1)
    await clock.advance(5)
    await clock.advance_to(1)
    assert clock.monotonic() == 5.0


async def test_a_cancelled_sleeper_does_not_block_advance() -> None:
    clock = FakeClock()
    task = asyncio.create_task(clock.sleep(3))
    await settle()
    task.cancel()
    await asyncio.wait({task})
    await clock.advance(5)
    assert clock.pending == 0


# --- process runner -------------------------------------------------------------------


def test_runner_satisfies_the_protocol() -> None:
    assert isinstance(FakeProcessRunner(), ProcessRunner)


async def test_runner_records_argv_stdin_and_timeout_and_returns_the_script() -> None:
    runner = FakeProcessRunner()
    runner.script("aerospace", "list-windows", stdout="a\nb\n", stderr=b"warn", returncode=0)
    result = await runner.run(["aerospace", "list-windows", "--all"], timeout=3.0, stdin=b"in")
    assert (result.returncode, result.stdout, result.stderr) == (0, b"a\nb\n", b"warn")
    assert result.argv == ("aerospace", "list-windows", "--all")
    assert runner.calls == [("aerospace", "list-windows", "--all")]
    assert runner.stdin == [b"in"]
    assert runner.timeouts == [3.0]
    assert runner.active == 0


async def test_runner_returns_non_zero_exit_codes_as_results() -> None:
    runner = FakeProcessRunner()
    runner.script("gh", returncode=4, stderr="boom")
    result = await runner.run(["gh", "pr", "view"])
    assert (result.returncode, result.stderr) == (4, b"boom")


async def test_the_longest_matching_prefix_wins() -> None:
    runner = FakeProcessRunner()
    runner.script("git", stdout="generic")
    runner.script("git", "status", stdout="status")
    assert (await runner.run(["git", "status", "-s"])).stdout == b"status"
    assert (await runner.run(["git", "log"])).stdout == b"generic"


async def test_an_unscripted_command_is_missing() -> None:
    runner = FakeProcessRunner()
    with pytest.raises(FileNotFoundError):
        await runner.run(["nothing"])
    assert runner.calls == [("nothing",)]


async def test_scripted_timeout_and_error() -> None:
    runner = FakeProcessRunner()
    runner.script("slow", timeout=True)
    runner.script("denied", error=PermissionError("no"))
    with pytest.raises(TimeoutError):
        await runner.run(["slow"], timeout=1)
    with pytest.raises(PermissionError):
        await runner.run(["denied"])
    assert runner.active == 0


async def test_times_limits_a_script() -> None:
    runner = FakeProcessRunner()
    runner.script("once", stdout="x", times=1)
    assert (await runner.run(["once"])).stdout == b"x"
    with pytest.raises(FileNotFoundError):
        await runner.run(["once"])


async def test_a_hanging_command_counts_as_active_until_cancelled() -> None:
    runner = FakeProcessRunner()
    runner.script("hang", hang=True)
    task = asyncio.create_task(runner.run(["hang"]))
    await settle()
    assert runner.active == 1
    task.cancel()
    await asyncio.wait({task})
    assert runner.active == 0


def test_runner_rejects_an_empty_script_and_an_empty_argv() -> None:
    runner = FakeProcessRunner()
    with pytest.raises(ValueError, match="argv prefix"):
        runner.script()
    with pytest.raises(ValueError, match="empty argv"):
        asyncio.run(runner.run([]))


# --- filesystem -----------------------------------------------------------------------


def test_fs_satisfies_the_protocol() -> None:
    assert isinstance(FakeFS(), FileSystem)


def test_fs_reads_files_and_lists_directories() -> None:
    fs = FakeFS({"/a/b/one.txt": "hello", Path("/a/two.bin"): b"\x00\x01"})
    fs.add_file("/a/b/three.txt", b"abc", mtime=12.5)
    fs.add_dir("/empty/dir")
    assert fs.read_text("/a/b/one.txt") == "hello"
    assert fs.read_bytes(Path("/a/two.bin")) == b"\x00\x01"
    assert fs.listdir("/a") == ["b", "two.bin"]
    assert fs.listdir("/a/b") == ["one.txt", "three.txt"]
    assert fs.listdir("/") == ["a", "empty"]
    assert fs.exists("/a/b")
    assert fs.is_dir("/a/b")
    assert not fs.is_dir("/a/two.bin")
    assert not fs.exists("/nope")
    info = fs.stat("/a/b/three.txt")
    assert (info.size, info.mtime, info.is_dir) == (3, 12.5, False)
    assert fs.stat("/a").is_dir
    assert [str(p) for p in fs.reads] == ["/a/b/one.txt", "/a/two.bin"]


def test_fs_errors() -> None:
    fs = FakeFS({"/f": "x"})
    with pytest.raises(FileNotFoundError):
        fs.read_bytes("/missing")
    with pytest.raises(FileNotFoundError):
        fs.stat("/missing")
    with pytest.raises(FileNotFoundError):
        fs.listdir("/missing")
    with pytest.raises(FileNotFoundError):
        fs.unlink("/missing")
    with pytest.raises(IsADirectoryError):
        fs.read_bytes("/")


def test_fs_injected_failures_and_healing() -> None:
    fs = FakeFS({"/secret": "x", "/d/f": "y"})
    fs.fail("/secret", PermissionError("denied"))
    fs.fail("/d", PermissionError("denied"))
    with pytest.raises(PermissionError):
        fs.read_text("/secret")
    with pytest.raises(PermissionError):
        fs.stat("/secret")
    with pytest.raises(PermissionError):
        fs.listdir("/d")
    with pytest.raises(PermissionError):
        fs.unlink("/secret")
    fs.heal("/secret")
    assert fs.read_text("/secret") == "x"


def test_fs_unlink_removes_the_file() -> None:
    fs = FakeFS({"/inbox/1.json": "{}"})
    fs.unlink("/inbox/1.json")
    assert not fs.exists("/inbox/1.json")
    assert fs.listdir("/inbox") == []


def test_fs_realpath_follows_links_by_prefix_and_normalizes() -> None:
    fs = FakeFS()
    assert isinstance(fs, PathResolver)
    assert fs.realpath("/repo/docs/../a.md") == "/repo/a.md"
    fs.links["/repo/link.md"] = "/home/x/.ssh/id"
    fs.links["/repo/dir"] = "/elsewhere"
    fs.links["/repo/dir/deep"] = "/deeper"
    assert fs.realpath("/repo/link.md") == "/home/x/.ssh/id"
    assert fs.realpath("/repo/dir/a.md") == "/elsewhere/a.md"
    assert fs.realpath("/repo/dir/deep/a.md") == "/deeper/a.md"  # the longest prefix wins
    assert fs.realpath("/repo/directory/a.md") == "/repo/directory/a.md"  # whole components only


def test_real_filesystems_resolve_paths(tmp_path: Path) -> None:
    from fylgja.collectors.codex.adapter import LocalFileSystem as CodexFileSystem
    from fylgja.core.system import LocalFileSystem

    target = tmp_path / "real"
    target.mkdir()
    (tmp_path / "link").symlink_to(target)
    for fs in (LocalFileSystem(), CodexFileSystem()):
        assert isinstance(fs, PathResolver)
        assert fs.realpath(tmp_path / "link" / "x.md") == str(target.resolve() / "x.md")


def test_real_filesystems_read_only_regular_files_and_never_block(tmp_path: Path) -> None:
    from fylgja.collectors.codex.adapter import LocalFileSystem as CodexFileSystem
    from fylgja.core.system import LocalFileSystem

    (tmp_path / "file").write_bytes(b"text")
    os.mkfifo(tmp_path / "fifo")  # nobody writes to it: a blocking open would hang
    for fs in (LocalFileSystem(), CodexFileSystem()):
        assert fs.read_bytes(tmp_path / "file") == b"text"
        with pytest.raises(OSError, match="not a regular file"):
            fs.read_bytes(tmp_path / "fifo")
        with pytest.raises(IsADirectoryError):
            fs.read_bytes(tmp_path)


# --- line stream ----------------------------------------------------------------------


def test_line_stream_satisfies_the_protocol() -> None:
    assert isinstance(FakeLineStream(), LineStream)


async def test_line_stream_returns_whole_lines_then_empty_at_eof() -> None:
    stream = FakeLineStream("one\ntwo\n", eof=True)
    assert await stream.readline() == b"one\n"
    assert await stream.readline() == b"two\n"
    assert await stream.readline() == b""
    assert stream.at_eof
    assert await stream.readline() == b""


async def test_line_stream_waits_for_a_torn_line_to_complete() -> None:
    stream = FakeLineStream()
    task = asyncio.create_task(stream.readline())
    stream.feed(b'{"a":')
    await settle()
    assert not task.done()
    stream.feed(b"1}\nnext")
    assert await task == b'{"a":1}\n'
    stream.feed_eof()
    assert await stream.readline() == b"next"
    assert await stream.readline() == b""


async def test_line_stream_blocks_until_eof_and_iterates() -> None:
    stream = FakeLineStream(b"a\n")
    stream.feed_line("b")
    collected: list[bytes] = []

    async def consume() -> None:
        async for line in stream:
            collected.append(line)

    task = asyncio.create_task(consume())
    await settle()
    assert collected == [b"a\n", b"b\n"]
    assert not task.done()
    stream.feed(b"tail")
    stream.feed_eof()
    await task
    assert collected == [b"a\n", b"b\n", b"tail"]


def test_line_stream_refuses_data_after_eof() -> None:
    stream = FakeLineStream(eof=True)
    with pytest.raises(ValueError, match="already ended"):
        stream.feed("x")


# --- sink -----------------------------------------------------------------------------


def _presence() -> PresenceEvent:
    return PresenceEvent(
        source="s",
        entity_id="user",
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        payload=PresencePayload(state="active"),
    )


def _todo() -> TodoEvent:
    return TodoEvent(
        source="t",
        entity_id="1",
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        payload=TodoPayload(text="x"),
    )


async def test_sink_records_emits_and_resolves_commits() -> None:
    sink = RecordingSink()
    assert isinstance(sink, RecordSink)
    first, second = _presence(), _todo()
    sink.emit(first)
    await sink.emit_committed(second)
    assert sink.records == [first, second]
    assert sink.committed == [second]
    assert sink.of_kind(EntityKind.TODO) == [second]
    assert sink.of_source("s") == [first]
    sink.clear()
    assert sink.records == []
    assert sink.committed == []


async def test_sink_commit_error_leaves_the_record_uncommitted() -> None:
    sink = RecordingSink(commit_error=RuntimeError("disk full"))
    with pytest.raises(RuntimeError, match="disk full"):
        await sink.emit_committed(_presence())
    assert sink.records == []
    assert sink.committed == []


# --- fixtures -------------------------------------------------------------------------


def test_fixture_loaders_read_committed_fixtures() -> None:
    names = fixtures.list_fixtures("ratatosk/probe-session")
    assert names == [
        "ratatosk/probe-session/hook-events.ndjson",
        "ratatosk/probe-session/transcript.jsonl",
    ]
    events = fixtures.load_ndjson("ratatosk/probe-session/hook-events.ndjson")
    assert events
    assert all(isinstance(e, dict) for e in events)
    assert fixtures.load_json("lidskjalv/builds/runs-empty.json") is not None
    assert fixtures.load_text("lidskjalv/transcripts/README.md")
    assert fixtures.load_bytes("lidskjalv/builds/runs-pass.json").startswith(b"{")
    assert fixtures.fixture_path("ratatosk").is_dir()
    assert fixtures.list_fixtures("lidskjalv/builds/runs-pass.json") == [
        "lidskjalv/builds/runs-pass.json"
    ]
    assert "ratatosk/probe-session/transcript.jsonl" in fixtures.list_fixtures()


@pytest.mark.parametrize("name", ["", "/etc/passwd", "../x", "a/../../x"])
def test_fixture_names_cannot_escape(name: str) -> None:
    with pytest.raises(ValueError, match="invalid fixture name"):
        fixtures.fixture_path(name)


def test_unknown_fixture_is_reported() -> None:
    with pytest.raises(fixtures.FixtureNotFound):
        fixtures.fixture_path("nope/missing.json")


def test_fixtures_dir_follows_the_environment_and_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = tmp_path / "custom"
    custom.mkdir()
    (custom / "x.json").write_text('{"k": 1}', encoding="utf-8")
    monkeypatch.setenv(fixtures.FIXTURES_ENV, str(custom))
    assert fixtures.fixtures_dir() == custom
    assert fixtures.load_json("x.json") == {"k": 1}

    monkeypatch.delenv(fixtures.FIXTURES_ENV)
    nested = tmp_path / "repo" / "tests" / "fixtures"
    nested.mkdir(parents=True)
    deep = tmp_path / "repo" / "src" / "pkg"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    assert fixtures.fixtures_dir().resolve() == nested.resolve()

    monkeypatch.chdir(tmp_path / "custom")
    with pytest.raises(fixtures.FixtureNotFound):
        fixtures.fixtures_dir()
