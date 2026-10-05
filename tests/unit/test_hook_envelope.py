"""``fylgja.hook`` in-process: envelope, A3 allowlist, size caps, atomic write, inbox checks,
backlog stamp, ``dropped.count``, StatusLine (A4) and the summary guard.

These tests call the plain functions; the fd redirect and the alarm of :func:`fylgja.hook.main`
are replaced by fakes, because they would silence pytest or fight pytest-timeout's own alarm.
``tests/integration/test_hook_ingress.py`` runs the real process.
"""

from __future__ import annotations

import json
import os
import re
import signal
import stat
import time
import uuid
from collections.abc import Iterator
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest
from hook_support import feed

from fylgja import hook, paths

NOW_NS = 1_791_231_759_255_399_000
SECOND_NS = 1_000_000_000

ALLOWED_ENV = {
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


@pytest.fixture
def stdin_pipe() -> Iterator[tuple[int, int]]:
    """A pipe ``(read_fd, write_fd)``; whatever is still open is closed afterwards."""
    read_fd, write_fd = os.pipe()
    opened = {read_fd, write_fd}
    yield read_fd, write_fd
    for fd in opened:
        try:
            os.close(fd)
        except OSError:
            pass


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _envelope(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "source": "claude",
        "event": "Stop",
        "payload_text": '{"session_id":"abc"}',
        "truncated": False,
        "environ": {},
        "received_at_ns": NOW_NS,
    }
    values.update(overrides)
    return hook.build_envelope(**values)


# --- arguments, guard, environment -------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], ("claude", None, False)),
        (["Stop"], ("claude", "Stop", False)),
        (["--source", "codex", "Stop"], ("codex", "Stop", False)),
        (["Stop", "--source=opencode"], ("opencode", "Stop", False)),
        (["--future-flag", "Stop", "extra"], ("claude", "Stop", False)),
        (["--source"], ("", None, False)),
        (["--help"], ("claude", None, True)),
        (["-h", "Stop"], ("claude", "Stop", True)),
        (["E" * 100], ("claude", "E" * hook.EVENT_ARG_MAX_CHARS, False)),
    ],
)
def test_parse_args(argv: list[str], expected: tuple[str, str | None, bool]) -> None:
    assert hook.parse_args(argv) == expected


@pytest.mark.parametrize(
    ("environ", "active"),
    [
        ({}, False),
        ({"FYLGJA_SUMMARY": "1"}, True),
        ({"LIDSKJALV_TRACK_SUMMARY": "1"}, True),
        ({"FYLGJA_SUMMARY": "", "LIDSKJALV_TRACK_SUMMARY": ""}, False),
        ({"OTHER": "1"}, False),
    ],
)
def test_summary_guard(environ: dict[str, str], active: bool) -> None:
    assert hook.summary_guard_active(environ) is active


def test_env_copies_exactly_the_a3_allowlist() -> None:
    environ = {
        **ALLOWED_ENV,
        "CLAUDE_CODE_MESSAGING_TOKEN": "secret-token",
        "CMUX_SOCKET_CAPABILITY": "secret-capability",
        "HOME": "/Users/user",
        "PATH": "/usr/bin",
        "CLAUDECODE": "1",
    }
    assert hook.capture_env(environ) == ALLOWED_ENV
    assert list(hook.ENV_ALLOWLIST) == list(ALLOWED_ENV)


def test_env_values_are_length_capped_and_empty_values_skipped() -> None:
    captured = hook.capture_env({"TERM_PROGRAM": "x" * 1000, "CMUX_TAB_ID": ""})
    assert captured == {"TERM_PROGRAM": "x" * hook.ENV_VALUE_MAX_CHARS}


# --- reading stdin -----------------------------------------------------------------------------


def test_read_stdin_reads_to_end_of_file() -> None:
    data = os.urandom(200_000)
    fd = feed(data)
    try:
        read = hook.read_stdin(fd)
    finally:
        os.close(fd)
    assert (read.data, read.total, read.complete) == (data, len(data), True)
    assert "complete=True" in repr(read)


def test_read_stdin_keeps_at_most_the_limit_but_drains_the_rest() -> None:
    fd = feed(b"a" * 100_000)
    try:
        read = hook.read_stdin(fd, limit=1000)
    finally:
        os.close(fd)
    assert read.data == b"a" * 1000
    assert read.total == 100_000
    assert read.complete


def test_read_stdin_stops_at_the_deadline(stdin_pipe: tuple[int, int]) -> None:
    read_fd, write_fd = stdin_pipe
    os.write(write_fd, b'{"partial":')
    started = time.monotonic()
    read = hook.read_stdin(read_fd, deadline_seconds=0.2)
    assert time.monotonic() - started < 1.5
    assert (read.data, read.complete) == (b'{"partial":', False)


def test_read_stdin_with_no_time_left_reads_nothing(stdin_pipe: tuple[int, int]) -> None:
    read = hook.read_stdin(stdin_pipe[0], deadline_seconds=0)
    assert (read.data, read.total, read.complete) == (b"", 0, False)


def test_read_stdin_treats_a_closed_fd_as_empty_input() -> None:
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    os.close(write_fd)
    read = hook.read_stdin(read_fd)
    assert (read.data, read.total, read.complete) == (b"", 0, True)


def test_read_stdin_treats_a_read_error_as_end_of_input(
    stdin_pipe: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    os.write(stdin_pipe[1], b"x")
    results: list[BaseException] = [BlockingIOError(), OSError(5, "EIO")]

    def failing_read(fd: int, size: int) -> bytes:
        raise results.pop(0)

    with monkeypatch.context() as patch:
        patch.setattr(hook.os, "read", failing_read)
        read = hook.read_stdin(stdin_pipe[0])
    assert (read.data, read.complete) == (b"", True)
    assert results == []


# --- size caps ---------------------------------------------------------------------------------


def test_small_input_is_kept_verbatim() -> None:
    raw = '{"prompt":"blåbær","n":1}\n'.encode()
    assert hook.cap_payload(hook.StdinRead(raw, len(raw), True)) == (raw.decode(), False)


def test_input_of_exactly_the_limit_is_verbatim() -> None:
    raw = b"x" * hook.VERBATIM_LIMIT_BYTES
    text, truncated = hook.cap_payload(hook.StdinRead(raw, len(raw), True))
    assert (len(text), truncated) == (hook.VERBATIM_LIMIT_BYTES, False)


def test_invalid_utf8_is_replaced_not_dropped() -> None:
    raw = b'{"a":"\xff"}'
    text, truncated = hook.cap_payload(hook.StdinRead(raw, len(raw), True))
    assert text == '{"a":"�"}'
    assert truncated is False


def test_input_cut_by_the_deadline_is_marked_truncated() -> None:
    raw = b'{"session_id":'
    assert hook.cap_payload(hook.StdinRead(raw, len(raw), False)) == (raw.decode(), True)


def test_oversize_json_has_long_strings_replaced() -> None:
    big = "y" * 300_000
    wide = "é" * 3000  # 3000 characters, 6000 UTF-8 bytes
    edge = "z" * hook.STRING_LIMIT_BYTES
    payload = {
        "session_id": "abc",
        "tool_input": {"content": big, "list": [wide, "short", 3, None, True]},
        "edge": edge,
    }
    raw = json.dumps(payload).encode()
    assert len(raw) > hook.VERBATIM_LIMIT_BYTES
    text, truncated = hook.cap_payload(hook.StdinRead(raw, len(raw), True))
    assert truncated is True
    assert json.loads(text) == {
        "session_id": "abc",
        "tool_input": {
            "content": {"_truncated": 300_000},
            "list": [{"_truncated": 6000}, "short", 3, None, True],
        },
        "edge": edge,
    }


def test_oversize_string_at_top_level_is_replaced() -> None:
    raw = json.dumps("q" * 300_000).encode()
    text, truncated = hook.cap_payload(hook.StdinRead(raw, len(raw), True))
    assert (json.loads(text), truncated) == ({"_truncated": 300_000}, True)


@pytest.mark.parametrize(
    "raw",
    [
        b"{" + b"x" * 300_000,
        b"\xff" * 300_000,
        b"[" * 300_000,
        b"9" * 300_000,
    ],
    ids=["not-json", "not-utf8", "too-deep", "huge-number"],
)
def test_oversize_unparseable_input_becomes_the_marker(raw: bytes) -> None:
    text, truncated = hook.cap_payload(hook.StdinRead(raw, len(raw), True))
    assert (json.loads(text), truncated) == ({"_malformed_oversize": len(raw)}, True)


def test_input_beyond_the_read_limit_becomes_the_marker() -> None:
    read = hook.StdinRead(b"{}", hook.READ_LIMIT_BYTES + 10, True)
    text, truncated = hook.cap_payload(read)
    assert (json.loads(text), truncated) == (
        {"_malformed_oversize": hook.READ_LIMIT_BYTES + 10},
        True,
    )


# --- the envelope ------------------------------------------------------------------------------


def test_envelope_fields() -> None:
    envelope = hook.build_envelope(
        source="claude",
        event="Stop",
        payload_text='{"a":1}',
        truncated=False,
        environ={**ALLOWED_ENV, "CLAUDE_CODE_MESSAGING_TOKEN": "secret"},
        received_at_ns=NOW_NS,
    )
    assert list(envelope) == [
        "schema",
        "id",
        "received_at_ns",
        "source",
        "event_arg",
        "ppid",
        "env",
        "payload_text",
        "truncated",
        "fylgja_version",
    ]
    assert envelope["schema"] == "fylgja.hook/1"
    assert uuid.UUID(envelope["id"]).version == 4
    assert str(uuid.UUID(envelope["id"])) == envelope["id"]
    assert envelope["received_at_ns"] == NOW_NS
    assert (envelope["source"], envelope["event_arg"]) == ("claude", "Stop")
    assert envelope["ppid"] == os.getppid()
    assert envelope["env"] == ALLOWED_ENV
    assert (envelope["payload_text"], envelope["truncated"]) == ('{"a":1}', False)
    assert envelope["fylgja_version"] == hook.FYLGJA_VERSION


def test_envelope_ids_are_unique_and_overridable() -> None:
    ids = {_envelope()["id"] for _ in range(100)}
    assert len(ids) == 100
    assert _envelope(envelope_id="fixed", ppid=7)["id"] == "fixed"
    assert _envelope(ppid=7)["ppid"] == 7


def test_version_constant_matches_the_installed_distribution() -> None:
    assert version("fylgja") == hook.FYLGJA_VERSION


def test_encoded_envelope_is_one_ascii_line_that_round_trips() -> None:
    envelope = _envelope(payload_text="blåbær \udcff 漢字")
    data = hook.encode_envelope(envelope)
    assert data.endswith(b"\n")
    assert data.count(b"\n") == 1
    data.decode("ascii")
    assert json.loads(data) == envelope


# --- directories and atomic files ---------------------------------------------------------------


def test_private_dir_is_created_with_mode_0700(isolated_env: Path) -> None:
    target = isolated_env / "a" / "b" / "c"
    assert hook.ensure_private_dir(target)
    for path in (isolated_env / "a", isolated_env / "a" / "b", target):
        assert _mode(path) == 0o700
    assert hook.ensure_private_dir(target)


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o500, 0o711])
def test_private_dir_with_the_wrong_mode_is_refused(isolated_env: Path, mode: int) -> None:
    target = isolated_env / "inbox"
    target.mkdir()
    target.chmod(mode)
    try:
        assert not hook.ensure_private_dir(target)
    finally:
        target.chmod(0o700)


def test_private_dir_that_is_a_symlink_is_refused(isolated_env: Path) -> None:
    real = isolated_env / "real"
    real.mkdir(mode=0o700)
    link = isolated_env / "link"
    link.symlink_to(real)
    assert not hook.ensure_private_dir(link)


def test_private_dir_that_is_a_file_is_refused(isolated_env: Path) -> None:
    target = isolated_env / "file"
    target.write_text("x")
    assert not hook.ensure_private_dir(target)
    assert not hook.ensure_private_dir(target / "below")


def test_private_dir_owned_by_another_user_is_refused(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = isolated_env / "inbox"
    target.mkdir(mode=0o700)
    real_uid = os.getuid()
    with monkeypatch.context() as patch:
        patch.setattr(hook.os, "getuid", lambda: real_uid + 1)
        refused = not hook.ensure_private_dir(target)
    assert refused


def test_atomic_write_creates_a_0600_file_and_no_temporary(isolated_env: Path) -> None:
    directory = isolated_env / "inbox"
    directory.mkdir(mode=0o700)
    target = hook.write_atomic(directory, "final.json", b"data\n", tmp_name=".tmp-1")
    assert target == directory / "final.json"
    assert target.read_bytes() == b"data\n"
    assert _mode(target) == 0o600
    assert sorted(p.name for p in directory.iterdir()) == ["final.json"]


def test_atomic_write_replaces_an_existing_file(isolated_env: Path) -> None:
    directory = isolated_env
    (directory / "final.json").write_bytes(b"old")
    hook.write_atomic(directory, "final.json", b"new", tmp_name=".tmp-2")
    assert (directory / "final.json").read_bytes() == b"new"


def test_atomic_write_never_reuses_a_temporary_name(isolated_env: Path) -> None:
    (isolated_env / ".tmp-3").write_bytes(b"someone else's")
    assert hook.write_atomic(isolated_env, "final.json", b"x", tmp_name=".tmp-3") is None
    assert (isolated_env / ".tmp-3").read_bytes() == b"someone else's"
    assert not (isolated_env / "final.json").exists()


def test_atomic_write_never_follows_a_symlinked_temporary(isolated_env: Path) -> None:
    victim = isolated_env / "victim"
    victim.write_bytes(b"keep")
    (isolated_env / ".tmp-4").symlink_to(victim)
    assert hook.write_atomic(isolated_env, "final.json", b"x", tmp_name=".tmp-4") is None
    assert victim.read_bytes() == b"keep"


def test_failed_rename_removes_the_temporary(isolated_env: Path) -> None:
    result = hook.write_atomic(isolated_env, "missing/final.json", b"x", tmp_name=".tmp-5")
    assert result is None
    assert not (isolated_env / ".tmp-5").exists()


def test_failed_fsync_removes_the_temporary(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_fsync(fd: int) -> None:
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as patch:
        patch.setattr(hook.os, "fsync", broken_fsync)
        result = hook.write_atomic(isolated_env, "final.json", b"x", tmp_name=".tmp-6")
    assert result is None
    assert not (isolated_env / ".tmp-6").exists()
    assert not (isolated_env / "final.json").exists()


def test_failed_cleanup_is_ignored(isolated_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: object) -> None:
        raise OSError(1, "Operation not permitted")

    with monkeypatch.context() as patch:
        patch.setattr(hook.os, "rename", broken)
        patch.setattr(hook.os, "unlink", broken)
        result = hook.write_atomic(isolated_env, "final.json", b"x", tmp_name=".tmp-7")
    assert result is None
    assert (isolated_env / ".tmp-7").exists()


def test_partial_writes_are_completed(isolated_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_write = os.write

    def one_byte_at_a_time(fd: int, data: bytes | memoryview) -> int:
        return real_write(fd, bytes(data[:1]))

    with monkeypatch.context() as patch:
        patch.setattr(hook.os, "write", one_byte_at_a_time)
        target = hook.write_atomic(isolated_env, "final.json", b"abcdef", tmp_name=".tmp-8")
    assert target is not None
    assert target.read_bytes() == b"abcdef"


# --- backlog ------------------------------------------------------------------------------------


@pytest.fixture
def inbox() -> Path:
    path = paths.inbox_dir("claude")
    assert hook.ensure_private_dir(path)
    return path


def _stamp(inbox: Path, verdict: bytes, age_ns: int) -> None:
    stamp = inbox / hook.BACKLOG_STAMP
    stamp.write_bytes(verdict)
    mtime = NOW_NS - age_ns
    os.utime(stamp, ns=(mtime, mtime))


def test_scan_counts_only_waiting_envelopes(inbox: Path) -> None:
    (inbox / "1-a.json").write_bytes(b"12345")
    (inbox / "2-b.json").write_bytes(b"123")
    (inbox / ".tmp-x").write_bytes(b"123456789")
    (inbox / hook.BACKLOG_STAMP).write_bytes(b"ok\n")
    (inbox / hook.DROPPED_COUNT).write_bytes(b"4\n")
    (inbox / "quarantine").mkdir()
    (inbox / "dir.json").mkdir()
    (inbox / "link.json").symlink_to(inbox / "1-a.json")
    assert hook.scan_backlog(inbox) == (2, 8)


def test_scan_skips_entries_that_vanish(inbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (inbox / "1-a.json").write_bytes(b"x")

    class Vanishing:
        name = "2-b.json"

        def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
            raise FileNotFoundError

    real_scandir = os.scandir

    class Entries:
        def __enter__(self) -> list[Any]:
            self._inner = real_scandir(inbox)
            return [*self._inner, Vanishing()]

        def __exit__(self, *exc: object) -> None:
            self._inner.close()

    with monkeypatch.context() as patch:
        patch.setattr(hook.os, "scandir", lambda path: Entries())
        counted = hook.scan_backlog(inbox)
    assert counted == (1, 1)


def test_first_check_scans_and_writes_the_stamp(inbox: Path) -> None:
    assert hook.backlog_full(inbox, NOW_NS) is False
    stamp = inbox / hook.BACKLOG_STAMP
    assert stamp.read_bytes() == b"ok\n"
    assert _mode(stamp) == 0o600


def test_backlog_over_the_file_limit_is_full(inbox: Path) -> None:
    for n in range(3):
        (inbox / f"{n}-x.json").write_bytes(b"{}")
    assert hook.backlog_full(inbox, NOW_NS, max_files=2) is True
    assert (inbox / hook.BACKLOG_STAMP).read_bytes() == b"full\n"
    _stamp(inbox, b"full\n", 61 * SECOND_NS)
    assert hook.backlog_full(inbox, NOW_NS, max_files=3) is False


def test_backlog_over_the_byte_limit_is_full(inbox: Path) -> None:
    (inbox / "1-x.json").write_bytes(b"x" * 100)
    assert hook.backlog_full(inbox, NOW_NS, max_bytes=99) is True
    _stamp(inbox, b"full\n", 61 * SECOND_NS)
    assert hook.backlog_full(inbox, NOW_NS, max_bytes=100) is False


@pytest.mark.parametrize(("verdict", "full"), [(b"full\n", True), (b"ok\n", False), (b"", False)])
def test_a_fresh_stamp_is_trusted_without_a_scan(
    inbox: Path, monkeypatch: pytest.MonkeyPatch, verdict: bytes, full: bool
) -> None:
    _stamp(inbox, verdict, 59 * SECOND_NS)

    def no_scan(path: Path) -> tuple[int, int]:
        raise AssertionError("scanned although the stamp is fresh")

    monkeypatch.setattr(hook, "scan_backlog", no_scan)
    assert hook.backlog_full(inbox, NOW_NS) is full


@pytest.mark.parametrize("age_ns", [60 * SECOND_NS, 3600 * SECOND_NS, -5 * SECOND_NS])
def test_a_stale_or_future_stamp_triggers_a_scan(inbox: Path, age_ns: int) -> None:
    _stamp(inbox, b"full\n", age_ns)
    assert hook.backlog_full(inbox, NOW_NS) is False
    assert (inbox / hook.BACKLOG_STAMP).read_bytes() == b"ok\n"


def test_a_stale_stamp_keeps_its_verdict_while_the_scan_runs(
    inbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stamp(inbox, b"full\n", 120 * SECOND_NS)
    seen: list[tuple[bytes, int]] = []

    def scan(path: Path) -> tuple[int, int]:
        stamp = path / hook.BACKLOG_STAMP
        seen.append((stamp.read_bytes(), stamp.stat().st_mtime_ns))
        return 0, 0

    monkeypatch.setattr(hook, "scan_backlog", scan)
    assert hook.backlog_full(inbox, NOW_NS) is False
    [(verdict, mtime_ns)] = seen
    assert verdict == b"full\n"
    assert mtime_ns > NOW_NS - 120 * SECOND_NS


def test_backlog_errors_count_as_not_full(inbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (inbox / hook.BACKLOG_STAMP).mkdir()
    assert hook.backlog_full(inbox, NOW_NS) is False

    def denied(path: object) -> os.stat_result:
        raise PermissionError

    with monkeypatch.context() as patch:
        patch.setattr(hook.os, "lstat", denied)
        full = hook.backlog_full(inbox, NOW_NS)
    assert full is False


def test_unreadable_stamp_counts_as_ok(inbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stamp(inbox, b"full\n", 0)

    def broken_read(fd: int, size: int) -> bytes:
        raise OSError(5, "EIO")

    with monkeypatch.context() as patch:
        patch.setattr(hook.os, "read", broken_read)
        full = hook.backlog_full(inbox, NOW_NS)
    assert full is False


def test_dropped_count_increments(inbox: Path) -> None:
    counter = inbox / hook.DROPPED_COUNT
    hook.increment_dropped(inbox)
    assert counter.read_text() == "1\n"
    assert _mode(counter) == 0o600
    for _ in range(9):
        hook.increment_dropped(inbox)
    assert counter.read_text() == "10\n"


def test_dropped_count_recovers_from_garbage(inbox: Path) -> None:
    (inbox / hook.DROPPED_COUNT).write_text("not a number, and long enough to be cut\n")
    hook.increment_dropped(inbox)
    assert (inbox / hook.DROPPED_COUNT).read_text() == "1\n"


def test_dropped_count_failures_are_silent(inbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (inbox / hook.DROPPED_COUNT).mkdir()
    hook.increment_dropped(inbox)
    (inbox / hook.DROPPED_COUNT).rmdir()

    def broken_flock(fd: int, operation: int) -> None:
        raise OSError(35, "Resource temporarily unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(hook.fcntl, "flock", broken_flock)
        hook.increment_dropped(inbox)
    assert (inbox / hook.DROPPED_COUNT).read_bytes() == b""


# --- delivery ----------------------------------------------------------------------------------


def test_event_is_delivered_under_its_receive_time_and_id(inbox: Path) -> None:
    envelope = _envelope()
    target = hook.deliver_event(inbox, envelope)
    assert target == inbox / f"{NOW_NS}-{envelope['id']}.json"
    assert json.loads(target.read_bytes()) == envelope
    assert _mode(target) == 0o600
    assert _mode(inbox) == 0o700
    assert not [p for p in inbox.iterdir() if p.name.startswith(".tmp-")]


def test_inbox_is_created_on_first_delivery() -> None:
    inbox = paths.inbox_dir("codex")
    assert hook.deliver_event(inbox, _envelope(source="codex")) is not None
    assert _mode(inbox) == 0o700
    assert _mode(paths.hook_inbox_root()) == 0o700


def test_refused_inbox_gets_nothing(inbox: Path) -> None:
    inbox.chmod(0o755)
    try:
        assert hook.deliver_event(inbox, _envelope()) is None
        assert list(inbox.iterdir()) == []
    finally:
        inbox.chmod(0o700)


def test_full_backlog_drops_the_event_and_counts_it(inbox: Path) -> None:
    _stamp(inbox, b"full\n", 0)
    assert hook.deliver_event(inbox, _envelope()) is None
    assert hook.deliver_event(inbox, _envelope()) is None
    assert (inbox / hook.DROPPED_COUNT).read_text() == "2\n"
    assert not [p for p in inbox.iterdir() if p.name.endswith(".json")]


# --- StatusLine (A4) ---------------------------------------------------------------------------


def _statusline(session_id: str = "sess-1", received_at_ns: int = NOW_NS) -> dict[str, Any]:
    payload = json.dumps({"session_id": session_id, "cost": {"total_cost_usd": 1.5}})
    return _envelope(event="StatusLine", payload_text=payload, received_at_ns=received_at_ns)


def test_statusline_writes_the_session_snapshot() -> None:
    envelope = _statusline()
    target = hook.write_statusline(envelope, {})
    assert target == paths.statusline_dir() / "sess-1.json"
    assert json.loads(target.read_bytes()) == envelope
    assert _mode(target) == 0o600
    assert _mode(paths.statusline_dir()) == 0o700
    assert not paths.hook_inbox_root().exists()


def test_statusline_skips_a_snapshot_under_two_seconds_old() -> None:
    first = hook.write_statusline(_statusline(), {})
    assert first is not None
    os.utime(first, ns=(NOW_NS, NOW_NS))
    assert hook.write_statusline(_statusline(received_at_ns=NOW_NS + SECOND_NS), {}) is None
    assert json.loads(first.read_bytes())["received_at_ns"] == NOW_NS


@pytest.mark.parametrize("age_ns", [2 * SECOND_NS, 30 * SECOND_NS, -SECOND_NS])
def test_statusline_replaces_an_older_snapshot(age_ns: int) -> None:
    first = hook.write_statusline(_statusline(), {})
    assert first is not None
    os.utime(first, ns=(NOW_NS, NOW_NS))
    later = _statusline(received_at_ns=NOW_NS + age_ns)
    assert hook.write_statusline(later, {}) == first
    assert json.loads(first.read_bytes())["id"] == later["id"]
    assert [p.name for p in paths.statusline_dir().iterdir()] == ["sess-1.json"]


def test_statusline_falls_back_to_the_session_id_variable() -> None:
    envelope = _envelope(event="StatusLine", payload_text="not json")
    target = hook.write_statusline(envelope, {"CLAUDE_CODE_SESSION_ID": "from-env"})
    assert target == paths.statusline_dir() / "from-env.json"


@pytest.mark.parametrize(
    "payload_text",
    [
        '{"session_id": "../escape"}',
        '{"session_id": ".hidden"}',
        '{"session_id": ""}',
        '{"session_id": 42}',
        "[1, 2]",
        "not json",
        json.dumps({"session_id": "s" * 129}),
    ],
)
def test_statusline_without_a_plain_session_id_writes_nothing(payload_text: str) -> None:
    envelope = _envelope(event="StatusLine", payload_text=payload_text)
    assert hook.write_statusline(envelope, {}) is None
    assert not paths.statusline_dir().exists() or list(paths.statusline_dir().iterdir()) == []


def test_statusline_refuses_a_symlinked_directory(isolated_env: Path) -> None:
    real = isolated_env / "elsewhere"
    real.mkdir(mode=0o700)
    paths.statusline_dir().parent.mkdir(parents=True, exist_ok=True)
    paths.statusline_dir().symlink_to(real)
    assert hook.write_statusline(_statusline(), {}) is None
    assert list(real.iterdir()) == []


def test_statusline_gives_up_when_the_snapshot_cannot_be_inspected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = paths.statusline_dir()
    assert hook.ensure_private_dir(directory)
    real_lstat = os.lstat

    def lstat(path: Any) -> os.stat_result:
        if str(path).endswith("sess-1.json"):
            raise PermissionError
        return real_lstat(path)

    with monkeypatch.context() as patch:
        patch.setattr(hook.os, "lstat", lstat)
        target = hook.write_statusline(_statusline(), {}, directory)
    assert target is None


# --- run() -------------------------------------------------------------------------------------


def test_run_writes_one_envelope_into_the_claude_inbox() -> None:
    fd = feed(b'{"session_id":"abc","hook_event_name":"Stop"}')
    try:
        target = hook.run(["Stop"], stdin_fd=fd, environ=ALLOWED_ENV, received_at_ns=NOW_NS)
    finally:
        os.close(fd)
    assert target is not None
    assert target.parent == paths.inbox_dir("claude")
    assert re.fullmatch(rf"{NOW_NS}-[0-9a-f-]{{36}}\.json", target.name)
    envelope = json.loads(target.read_bytes())
    assert envelope["payload_text"] == '{"session_id":"abc","hook_event_name":"Stop"}'
    assert envelope["event_arg"] == "Stop"
    assert envelope["env"] == ALLOWED_ENV
    assert envelope["truncated"] is False


def test_run_uses_the_source_inbox_and_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TERM_PROGRAM", "test-terminal")
    for name in ("FYLGJA_SUMMARY", "LIDSKJALV_TRACK_SUMMARY"):
        monkeypatch.delenv(name, raising=False)
    fd = feed(b"{}")
    try:
        target = hook.run(["--source", "opencode", "session.idle"], stdin_fd=fd)
    finally:
        os.close(fd)
    assert target is not None
    assert target.parent == paths.inbox_dir("opencode")
    envelope = json.loads(target.read_bytes())
    assert envelope["source"] == "opencode"
    assert envelope["env"]["TERM_PROGRAM"] == "test-terminal"
    assert abs(envelope["received_at_ns"] - time.time_ns()) < 60 * SECOND_NS


def test_run_statusline_writes_the_snapshot_not_an_event() -> None:
    fd = feed(b'{"session_id":"sess-9"}')
    try:
        target = hook.run(["StatusLine"], stdin_fd=fd, environ={}, received_at_ns=NOW_NS)
    finally:
        os.close(fd)
    assert target == paths.statusline_dir() / "sess-9.json"
    assert not paths.hook_inbox_root().exists()


@pytest.mark.parametrize(
    ("argv", "environ"),
    [
        (["Stop"], {"FYLGJA_SUMMARY": "1"}),
        (["Stop"], {"LIDSKJALV_TRACK_SUMMARY": "1"}),
        (["--help"], {}),
        (["--source", "../evil", "Stop"], {}),
        (["--source", "", "Stop"], {}),
        (["--source", "s" * 65, "Stop"], {}),
    ],
    ids=["fylgja-summary", "lidskjalv-summary", "help", "bad-source", "empty-source", "long"],
)
def test_run_writes_nothing_and_leaves_stdin_unread(
    stdin_pipe: tuple[int, int], argv: list[str], environ: dict[str, str]
) -> None:
    read_fd, write_fd = stdin_pipe
    os.write(write_fd, b"{}")
    started = time.monotonic()
    assert hook.run(argv, stdin_fd=read_fd, environ=environ) is None
    assert time.monotonic() - started < 1
    assert os.read(read_fd, 10) == b"{}"
    assert not paths.data_dir().exists() or list(paths.data_dir().iterdir()) == []


# --- main(): the redirect, the alarm and the exit ----------------------------------------------


class _Exit(BaseException):
    def __init__(self, code: int) -> None:
        self.code = code


def _raise_exit(code: int) -> None:
    raise _Exit(code)


def test_main_silences_arms_runs_and_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(hook, "_silence_stdio", lambda: calls.append("silence"))
    monkeypatch.setattr(hook, "_arm_backstop", lambda: calls.append("alarm"))
    monkeypatch.setattr(
        hook, "run", lambda argv, **kwargs: calls.append(("run", list(argv), sorted(kwargs)))
    )
    monkeypatch.setattr(hook.os, "_exit", _raise_exit)
    with pytest.raises(_Exit) as info:
        hook.main(["Stop"])
    assert info.value.code == 0
    assert calls == ["silence", "alarm", ("run", ["Stop"], ["received_at_ns"])]


@pytest.mark.parametrize("error", [RuntimeError("boom"), KeyboardInterrupt(), SystemExit(3)])
def test_main_exits_zero_whatever_run_raises(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    def failing_run(argv: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(hook, "_silence_stdio", lambda: None)
    monkeypatch.setattr(hook, "_arm_backstop", lambda: None)
    monkeypatch.setattr(hook, "run", failing_run)
    monkeypatch.setattr(hook.os, "_exit", _raise_exit)
    with pytest.raises(_Exit) as info:
        hook.main([])
    assert info.value.code == 0


def test_silence_points_stdout_and_stderr_at_dev_null(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[int] = []
    real_open = os.open

    def recording_open(path: str, flags: int, *args: int) -> int:
        fd = real_open(path, flags, *args)
        opened.append(fd)
        return fd

    duplicated: list[tuple[int, int]] = []

    def fake_dup2(fd: int, fd2: int) -> int:
        duplicated.append((fd, fd2))
        if fd2 == 1:
            raise OSError(9, "EBADF")
        return fd2

    with monkeypatch.context() as patch:
        patch.setattr(hook.os, "open", recording_open)
        patch.setattr(hook.os, "dup2", fake_dup2)
        hook._silence_stdio()
    [null] = opened
    assert duplicated == [(null, 1), (null, 2)]
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(null)


def test_silence_keeps_a_null_fd_that_landed_on_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[int] = []
    with monkeypatch.context() as patch:
        patch.setattr(hook.os, "open", lambda *args: 2)
        patch.setattr(hook.os, "dup2", lambda fd, fd2: fd2)
        patch.setattr(hook.os, "close", closed.append)
        hook._silence_stdio()
    assert closed == []


def test_silence_without_dev_null_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_dev_null(*args: object) -> int:
        raise OSError(2, "ENOENT")

    duplicated: list[object] = []
    with monkeypatch.context() as patch:
        patch.setattr(hook.os, "open", no_dev_null)
        patch.setattr(hook.os, "dup2", lambda *args: duplicated.append(args))
        hook._silence_stdio()
    assert duplicated == []


def test_backstop_arms_a_three_second_alarm(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object]] = []
    with monkeypatch.context() as patch:
        patch.setattr(
            hook.signal,
            "signal",
            lambda signum, handler: calls.append(("signal", (signum, handler))),
        )
        patch.setattr(hook.signal, "alarm", lambda seconds: calls.append(("alarm", seconds)))
        hook._arm_backstop()
    assert calls == [("signal", (signal.SIGALRM, hook._on_alarm)), ("alarm", 3)]


def test_alarm_handler_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hook.os, "_exit", _raise_exit)
    with pytest.raises(_Exit) as info:
        hook._on_alarm(signal.SIGALRM, None)
    assert info.value.code == 0
