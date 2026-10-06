"""The norn collector's file scan: the last ``.history`` line wins, the depth limit, mtime gating
and the process match, over the sanitized run files and fakes."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest
from norn_support import (
    FILES_NOW,
    Rig,
    by_suffix,
    copy_case,
    lsof_cwds,
    ps_table,
)

from fylgja.collectors.codex.adapter import CommandFailure
from fylgja.collectors.context import build_context
from fylgja.collectors.norn import adapter, parse, state
from fylgja.collectors.norn.collector import NornCollector, NornSettings
from fylgja.models import HealthState, SessionActivitySnapshot
from fylgja.testing.fakes import FakeClock, FakeFS, FakeProcessRunner, RecordingSink

NOW = FILES_NOW.timestamp()


def walk(
    fs: object,
    roots: list[str],
    *,
    depth: int = 3,
    age: float = 21600.0,
    extra: list[str] | None = None,
):  # type: ignore[no-untyped-def]
    return adapter.find_state_files(
        fs,  # type: ignore[arg-type]
        roots,
        extra or [],
        max_depth=depth,
        min_mtime=NOW - age,
    )


def norn_files(fs: FakeFS, directory: str, stem: str = "plan", *, mtime: float = NOW - 10) -> None:
    fs.add_file(f"{directory}/{stem}.history", b'{"run_id": 1}\n', mtime=mtime)
    fs.add_file(f"{directory}/{stem}.checkpoint", b"{}", mtime=mtime)


# --- the walk ---------------------------------------------------------------------------------


def test_the_walk_finds_history_files_to_the_depth_limit() -> None:
    fs = FakeFS()
    norn_files(fs, "/root")
    norn_files(fs, "/root/a")
    norn_files(fs, "/root/a/b")
    norn_files(fs, "/root/a/b/c")
    norn_files(fs, "/root/a/b/c/d")

    def found(depth: int) -> list[str]:
        return sorted(f.directory for f in walk(fs, ["/root"], depth=depth).files)

    assert found(0) == ["/root"]
    assert found(1) == ["/root", "/root/a"]
    assert found(3) == ["/root", "/root/a", "/root/a/b", "/root/a/b/c"]


def test_a_history_file_needs_a_checkpoint_or_a_runs_directory_next_to_it() -> None:
    fs = FakeFS()
    fs.add_file("/root/zsh.history", b"ls\n", mtime=NOW)  # somebody else's history file
    fs.add_file("/root/ok.history", b"{}\n", mtime=NOW)
    fs.add_dir("/root/ok.runs")
    fs.add_file("/root/.history", b"x", mtime=NOW)
    assert [f.stem for f in walk(fs, ["/root"]).files] == ["ok"]


def test_hidden_skipped_and_runs_directories_are_not_entered() -> None:
    fs = FakeFS()
    for directory in ("/root/.hidden", "/root/node_modules/x", "/root/p.runs/1", "/root/Library"):
        norn_files(fs, directory)
    norn_files(fs, "/root/real")
    assert [f.directory for f in walk(fs, ["/root"]).files] == ["/root/real"]


def test_files_older_than_the_age_limit_are_neither_listed_nor_read() -> None:
    fs = FakeFS()
    norn_files(fs, "/root/young", mtime=NOW - 100)
    norn_files(fs, "/root/old", mtime=NOW - 21601)
    result = walk(fs, ["/root"], age=21600)
    assert [f.directory for f in result.files] == ["/root/young"]
    assert not any(str(p).endswith(".history") for p in fs.reads)


def test_known_directories_are_listed_without_descending() -> None:
    fs = FakeFS()
    norn_files(fs, "/elsewhere/project")
    norn_files(fs, "/elsewhere/project/sub")
    result = walk(fs, [], extra=["/elsewhere/project", "/missing"])
    assert [f.directory for f in result.files] == ["/elsewhere/project"]
    assert result.roots_found == 0


def test_missing_and_unreadable_roots_are_counted() -> None:
    fs = FakeFS()
    norn_files(fs, "/root/a")
    fs.add_dir("/root/locked")
    fs.fail("/root/locked", PermissionError("denied"))
    fs.add_dir("/root/gone")
    result = walk(fs, ["/root", "/nowhere"])
    assert result.roots_found == 1
    assert result.unreadable == 1
    assert [f.directory for f in result.files] == ["/root/a"]


def test_the_walk_stops_at_the_entry_budget() -> None:
    fs = FakeFS()
    for number in range(30):
        fs.add_dir(f"/root/d{number:02d}")
    result = adapter.find_state_files(fs, ["/root"], [], max_depth=2, min_mtime=0, max_entries=10)
    assert result.truncated is True


def test_the_walk_over_real_files_gates_on_mtime(tmp_path: Path) -> None:
    case = copy_case(tmp_path, "finished", age=60)
    old = copy_case(tmp_path, "waiting", age=30 * 3600)
    result = walk(adapter.LocalFileSystem(), [str(tmp_path)])
    assert [Path(f.directory) for f in result.files] == [case]
    assert old.exists()


# --- reading the files ------------------------------------------------------------------------


def _history_file(tmp_path: Path, lines: list[str]) -> adapter.StateFile:
    path = tmp_path / "big.history"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    info = path.stat()
    return adapter.StateFile(str(tmp_path), "big", str(path), info.st_mtime_ns, info.st_size)


def _line(run_id: int, padding: int = 0, **extra: object) -> str:
    import json

    return json.dumps(
        {"run_id": run_id, "in_progress": True, "pad": "x" * padding, "stage_log": [], **extra}
    )


def test_only_the_tail_of_a_big_history_file_is_read(tmp_path: Path) -> None:
    lines = [_line(1, padding=900_000), _line(2, padding=900_000)] + [_line(3), _line(4)]
    file = _history_file(tmp_path, lines)
    assert file.size > adapter.HISTORY_TAIL_BYTES
    records = adapter.read_history(adapter.LocalFileSystem(), file)
    assert sorted(records) == [3, 4]  # the big lines before the tail are never parsed


def test_the_tail_grows_until_it_holds_a_whole_line(tmp_path: Path) -> None:
    tail = adapter.HISTORY_TAIL_BYTES
    file = _history_file(tmp_path, [_line(1, padding=3 * tail), _line(2, padding=tail * 3 // 2)])
    records = adapter.read_history(adapter.LocalFileSystem(), file)
    assert sorted(records) == [2]


def test_a_file_without_range_reads_falls_back_to_slicing(tmp_path: Path) -> None:
    fs = FakeFS({tmp_path / "x": "a\nb\nc\n"})
    data, complete = adapter.read_tail(fs, str(tmp_path / "x"), 6, 3)
    assert (data, complete) == (b"b\nc\n"[-3:], False)
    assert adapter.read_tail(fs, str(tmp_path / "x"), 6, 100) == (b"a\nb\nc\n", True)


def test_run_directories_are_the_newest_numbered_ones() -> None:
    fs = FakeFS()
    for name in ("1", "2", "10", "x", "9"):
        fs.add_dir(f"/root/p.runs/{name}")
    file = adapter.StateFile("/root", "p", "/root/p.history", 0, 0)
    assert [n for n, _ in adapter.run_dirs(fs, file, newest=3)] == ["10", "9", "2"]
    assert (
        adapter.run_dirs(fs, adapter.StateFile("/none", "p", "/none/p.history", 0, 0), newest=3)
        == []
    )


def test_run_header_and_events_tail_tolerate_missing_and_large_files() -> None:
    fs = FakeFS()
    fs.add_file("/r/1/run.json", b"x" * (adapter.RUN_JSON_MAX_BYTES + 1))
    assert adapter.read_run_header(fs, "/r/1") is None
    assert adapter.read_run_header(fs, "/r/2") is None
    assert adapter.read_events_tail(fs, "/r/2") is None
    fs.add_file("/r/3/events.jsonl", b'{"type": "RunFinished", "success": true}\n')
    tail = adapter.read_events_tail(fs, "/r/3")
    assert tail is not None
    assert tail.outcome == "passed"


# --- the scan through the collector -----------------------------------------------------------


async def test_the_last_history_line_of_a_run_decides_its_state(tmp_path: Path) -> None:
    copy_case(tmp_path, "finished")
    r = Rig(roots=[str(tmp_path)], start=FILES_NOW)
    r.script_processes()
    await r.poll()
    assert list(r.states().values()) == ["passed"]
    snapshot = [x for x in r.sink.records if isinstance(x, SessionActivitySnapshot)][-1]
    (item,) = snapshot.items
    assert item.payload.stage is not None
    assert item.payload.stage.name == "plan summary"
    assert r.collector.stats.file_runs == 1


async def test_a_finished_run_in_the_files_leaves_the_listing_after_the_window(
    tmp_path: Path,
) -> None:
    copy_case(tmp_path, "finished", age=60)
    r = Rig(roots=[str(tmp_path)], start=FILES_NOW, finished_seconds=900)
    r.script_processes()
    await r.poll()
    assert list(r.states().values()) == ["passed"]
    await r.clock.advance(15 * 60)  # the run finished 2 minutes before FILES_NOW
    await r.poll()
    assert r.states() == {}


async def test_failed_and_passed_runs_of_norns_own_files(tmp_path: Path) -> None:
    failed = copy_case(tmp_path, "failed", at=FILES_NOW.replace(month=9, day=23, hour=14, minute=0))
    passed = copy_case(tmp_path, "passed", at=FILES_NOW.replace(hour=8, minute=45), age=30)
    r = Rig(roots=[str(failed), str(passed)], start=FILES_NOW.replace(hour=8, minute=45))
    r.script_processes()
    await r.poll()
    assert list(r.states().values()) == ["passed"]  # the failed run is from September
    r2 = Rig(roots=[str(failed)], start=FILES_NOW.replace(month=9, day=23, hour=14, minute=0))
    r2.script_processes()
    await r2.poll()
    assert list(r2.states().values()) == ["failed"]


async def test_a_run_waiting_in_the_files_shows_why(tmp_path: Path) -> None:
    copy_case(tmp_path, "waiting")
    r = Rig(roots=[str(tmp_path)], start=FILES_NOW, alive={4100})
    argv = "/Users/user/projects/erik.romson/norn/main/norn/__main__.py ui"
    r.script_processes(
        ps_table((4100, f"/usr/bin/python3 {argv}")),
        cwds={4100: "/Users/user/projects/erik-romson/fylgja/main"},
    )
    await r.poll()
    assert list(r.states().values()) == ["waiting_input"]


async def test_the_newest_unfinished_run_takes_the_process(tmp_path: Path) -> None:
    """One ``norn ui`` process hosts runs one after another: only the newest can be running."""
    older = state.Run(key="a", argv=("/n/__main__.py", "ui"), cwd="/w", started_at=FILES_NOW)
    newer = state.Run(
        key="b",
        argv=("/n/__main__.py", "ui"),
        cwd="/w",
        started_at=FILES_NOW + timedelta(hours=1),
    )
    process = state.ScanProcess(5, ("python", "/n/__main__.py", "ui"), "/w")
    assert state.match_processes([older, newer], [process]) == {"b": 5}
    two = state.ScanProcess(6, ("python", "/n/__main__.py", "ui"), "/w")
    assert state.match_processes([older, newer], [process, two]) == {"b": 5, "a": 6}


def test_argv_matching_compares_the_script_by_name_and_the_arguments_in_order() -> None:
    run = ("/a/b/__main__.py", "run", "pipe", "--resume")
    assert state.argv_matches(run, ("python", "/x/__main__.py", "run", "pipe", "--resume"))
    assert state.argv_matches(run, ("python", "/x/__main__.py", "run", "pipe", "--resume", "-v"))
    assert not state.argv_matches(run, ("python", "/x/__main__.py", "run", "pipe"))
    assert not state.argv_matches(run, ("python", "/x/other.py", "run", "pipe", "--resume"))
    assert not state.argv_matches(run, ("python", "/x/__main__.py", "run", "other", "--resume"))
    assert not state.argv_matches((), ("python",))
    assert state.argv_matches(("norn",), ("/usr/bin/norn", "x"))


def test_a_module_launch_matches_the_main_file_of_the_run() -> None:
    """``python3 -m norn ui`` is how norn runs: ``run.json`` has the path of ``__main__.py``."""
    run = ("/n/norn/__main__.py", "run", "implement_features_v2", "plan")
    assert state.argv_matches(
        run, ("python3", "-m", "norn", "run", "implement_features_v2", "plan")
    )
    assert state.argv_matches(("/n/norn/__main__.py", "ui"), ("python3", "-m", "norn", "ui"))
    assert not state.argv_matches(("/n/norn/__main__.py", "ui"), ("python3", "norn", "ui"))
    assert not state.argv_matches(("/n/norn/__main__.py", "ui"), ("python3", "-m", "norn", "run"))
    assert not state.argv_matches(("/n/other.py", "ui"), ("python3", "-m", "norn", "ui"))


def test_directories_compare_through_the_private_prefix() -> None:
    run = state.Run(key="a", argv=("n",), cwd="/var/folders/x", started_at=FILES_NOW)
    inside = state.ScanProcess(1, ("n",), "/private/var/folders/x")
    assert state.match_processes([run], [inside]) == {"a": 1}
    unknown = state.ScanProcess(2, ("n",), None)
    assert state.match_processes([run], [unknown]) == {"a": 2}
    assert (
        state.match_processes([state.Run(key="b", outcome="passed", argv=("n",))], [unknown]) == {}
    )
    assert state.match_processes([state.Run(key="c")], [unknown]) == {}


def test_ps_lines_are_parsed_to_pids_and_arguments() -> None:
    text = "  1 /sbin/launchd\n  22 python /a b\nbad line\n   \n 33\n"
    assert adapter.parse_ps_args(text) == [(1, ("/sbin/launchd",)), (22, ("python", "/a", "b"))]


async def test_candidates_are_matched_by_argument_vector_and_get_a_directory() -> None:
    runner = FakeProcessRunner()
    runner.script("/bin/ps", stdout=ps_table((1, "/sbin/launchd"), (9, "py /n/__main__.py ui")))
    runner.script("/usr/sbin/lsof", stdout=lsof_cwds({9: "/w"}))
    found = await adapter.list_candidates(
        runner,
        ps="/bin/ps",
        lsof="/usr/sbin/lsof",
        wanted=[("/n/__main__.py", "ui")],
        timeout=5,
    )
    assert found == [state.ScanProcess(9, ("py", "/n/__main__.py", "ui"), "/w")]
    assert runner.calls[1][0] == "/usr/sbin/lsof"
    assert (
        await adapter.list_candidates(
            runner, ps="/bin/ps", lsof="/usr/sbin/lsof", wanted=[("nothing",)], timeout=5
        )
        == []
    )


@pytest.mark.parametrize(
    ("kwargs", "kind"),
    [
        ({"error": FileNotFoundError()}, "ps_missing"),
        ({"error": PermissionError()}, "ps_not_executable"),
        ({"timeout": True}, "ps_timeout"),
        ({"error": OSError("x")}, "ps_failed"),
        ({"returncode": 2}, "ps_failed"),
    ],
)
async def test_a_failing_ps_is_a_command_failure_with_a_kind(
    kwargs: dict[str, object], kind: str
) -> None:
    runner = FakeProcessRunner()
    runner.script("/bin/ps", **kwargs)  # type: ignore[arg-type]
    with pytest.raises(CommandFailure) as caught:
        await adapter.list_candidates(
            runner, ps="/bin/ps", lsof="/usr/sbin/lsof", wanted=[("x",)], timeout=5
        )
    assert caught.value.kind == kind


def test_pid_liveness_of_this_process_and_of_nothing() -> None:
    assert adapter.pid_alive(os.getpid()) is True
    assert adapter.pid_alive(0) is False
    assert adapter.pid_alive(-5) is False
    assert adapter.pid_alive(2**22 + 12345) is False  # above any pid macOS hands out


async def test_a_state_file_is_not_read_again_while_it_is_unchanged() -> None:
    from norn_support import FILES

    finished = FILES / "finished"
    fs = FakeFS()
    for source in finished.rglob("*"):
        if source.is_file():
            fs.add_file(
                "/root/" + str(source.relative_to(finished)), source.read_bytes(), mtime=NOW - 60
            )
    clock = FakeClock(FILES_NOW)
    runner = FakeProcessRunner()
    sink = RecordingSink()
    collector = NornCollector(
        NornSettings(roots=["/root"], scan_interval_seconds=5, inbox_watch="poll"),
        build_context("norn", sink, clock=clock),
        seams=adapter.Seams(runner, fs, pid_alive=lambda pid: False),
    )
    await collector.poll()
    history = [p for p in fs.reads if str(p).endswith(".history")]
    assert len(history) == 1
    await clock.advance(6)
    await collector.poll()
    assert len([p for p in fs.reads if str(p).endswith(".history")]) == 1
    fs.replace_file(
        "/root/plan_with_review.history",
        fs.read_bytes("/root/plan_with_review.history") + b"\n",
        mtime=NOW,
    )
    await clock.advance(6)
    await collector.poll()
    assert len([p for p in fs.reads if str(p).endswith(".history")]) == 3  # one is the line above


async def test_missing_roots_degrade_and_recover(tmp_path: Path) -> None:
    r = Rig(roots=[str(tmp_path / "later")], start=FILES_NOW, scan_interval_seconds=5)
    r.script_processes()
    await r.poll()
    report = await r.collector.doctor()
    assert (report.state, report.error_kind) == (HealthState.DEGRADED, "roots_missing")
    copy_case(tmp_path / "later", "finished") if (tmp_path / "later").mkdir() is None else None
    await r.clock.advance(6)
    await r.poll()
    assert (await r.collector.doctor()).state is HealthState.OK
    assert by_suffix(r.states(), ":1") == "passed"


async def test_unreadable_state_directories_are_reported(tmp_path: Path) -> None:
    fs = FakeFS()
    norn_files(fs, "/root/a")
    fs.add_dir("/root/locked")
    fs.fail("/root/locked", PermissionError("denied"))
    clock = FakeClock(FILES_NOW)
    collector = NornCollector(
        NornSettings(roots=["/root"], inbox_watch="poll"),
        build_context("norn", RecordingSink(), clock=clock),
        seams=adapter.Seams(FakeProcessRunner(), fs, pid_alive=lambda pid: False),
    )
    await collector.poll()
    report = await collector.doctor()
    assert (report.state, report.error_kind) == (HealthState.DEGRADED, "state_unreadable")


async def test_known_directories_of_plugin_runs_are_scanned_outside_the_roots(
    tmp_path: Path,
) -> None:
    from norn_support import INBOX_NOW, deliver

    root = tmp_path / "elsewhere"
    root.mkdir()
    r = Rig(roots=[], start=INBOX_NOW, alive={51234}, scan_interval_seconds=5)
    r.script_processes()
    deliver(r.inbox_dir, "lifecycle", stop=2)
    await r.drain()
    run = next(iter(r.collector._plugin.values()))
    assert run.cwd == "/Users/user/projects/example"
    assert r.collector._known_directories() == ["/Users/user/projects/example"]


def test_default_seams_are_the_real_ones() -> None:
    seams = adapter.default_seams()
    assert seams.pid_alive is adapter.pid_alive
    assert isinstance(seams.fs, adapter.LocalFileSystem)


def test_a_history_record_without_a_header_is_named_by_its_file(tmp_path: Path) -> None:
    record = parse.HistoryRecord(
        run_id=4,
        timestamp=FILES_NOW,
        success=None,
        in_progress=True,
        failed_stage=None,
        provider=None,
        session_id=None,
        stages=(),
    )
    run = state.run_from_files("norn:p@a:4", record, None, None, stem="p")
    assert (run.pipeline, run.cwd, run.argv, run.finished) == ("p", None, (), False)
    done = state.run_from_files(
        "norn:p@a:4",
        parse.HistoryRecord(4, FILES_NOW, True, False, None, None, None, ()),
        None,
        None,
        stem="p",
    )
    assert (done.outcome, done.finished_at) == ("passed", FILES_NOW)
    failed = state.run_from_files(
        "k",
        parse.HistoryRecord(4, FILES_NOW, False, False, None, None, None, ()),
        None,
        None,
        stem="p",
    )
    assert failed.outcome == "failed"
