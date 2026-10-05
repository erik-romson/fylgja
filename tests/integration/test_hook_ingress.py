"""``fylgja hook`` end-to-end in real subprocesses (``PLAN.md`` §4.9 hook-ingress list, WP2.1).

Every case runs ``sys.executable -m fylgja._entry hook …`` with no daemon: 200 concurrent
invocations, private permissions, silence even with invalid stdin or an unwritable inbox, the 3 s
bound with a stdin that never closes, oversize input, and exit code 0 everywhere.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from hook_support import HOOK_COMMAND, clean_env, envelope_files, load, run_hook

from fylgja import paths

STOP_PAYLOAD = {
    "session_id": "00000000-0000-4000-8000-000000004101",
    "transcript_path": "/Users/user/.claude/projects/x/00000000-0000-4000-8000-000000004101.jsonl",
    "cwd": "/Users/user/projects/example-1",
    "hook_event_name": "Stop",
}


def _silent_success(result: subprocess.CompletedProcess[bytes]) -> None:
    assert result.returncode == 0
    assert result.stdout == b""
    assert result.stderr == b""


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_an_event_becomes_one_envelope_file() -> None:
    raw = json.dumps(STOP_PAYLOAD).encode()
    env = clean_env(
        CLAUDE_CODE_SESSION_ID=STOP_PAYLOAD["session_id"],
        CLAUDE_PID="4101",
        CMUX_SURFACE_ID="surface-1",
        HOME_SECRET_LIKE="not copied",
    )
    _silent_success(run_hook("Stop", stdin=raw, env=env))
    [path] = envelope_files()
    envelope = load(path)
    assert envelope["schema"] == "fylgja.hook/1"
    assert envelope["event_arg"] == "Stop"
    assert envelope["source"] == "claude"
    assert envelope["payload_text"] == raw.decode()
    assert envelope["truncated"] is False
    assert envelope["env"] == {
        "CLAUDE_CODE_SESSION_ID": STOP_PAYLOAD["session_id"],
        "CLAUDE_PID": "4101",
        "CMUX_SURFACE_ID": "surface-1",
    }
    assert envelope["ppid"] == os.getpid()
    assert path.name == f"{envelope['received_at_ns']}-{envelope['id']}.json"
    assert abs(envelope["received_at_ns"] - time.time_ns()) < 60 * 10**9


def test_files_are_0600_and_the_inbox_is_0700() -> None:
    _silent_success(run_hook("PreToolUse", stdin=b"{}"))
    _silent_success(run_hook("StatusLine", stdin=b'{"session_id":"s-1"}'))
    inbox = paths.inbox_dir("claude")
    [path] = envelope_files()
    snapshot = paths.statusline_dir() / "s-1.json"
    for directory in (paths.hook_inbox_root(), inbox, paths.statusline_dir()):
        assert stat.S_ISDIR(os.lstat(directory).st_mode)
        assert _mode(directory) == 0o700
        assert os.lstat(directory).st_uid == os.getuid()
    for file in (path, snapshot, inbox / ".backlog-stamp"):
        assert stat.S_ISREG(os.lstat(file).st_mode)
        assert _mode(file) == 0o600
        assert os.lstat(file).st_uid == os.getuid()
    assert not [p for p in inbox.iterdir() if p.name.startswith(".tmp-")]


def test_source_flag_selects_the_inbox() -> None:
    _silent_success(run_hook("--source", "codex", "agent-turn-complete", stdin=b"{}"))
    assert envelope_files("claude") == []
    [path] = envelope_files("codex")
    assert load(path)["source"] == "codex"


@pytest.mark.parametrize(
    "stdin",
    [b"", b"not json at all", b"\xff\xfe\x00\x01garbage", b"{" * 5000, b'{"half": '],
    ids=["empty", "text", "binary", "unbalanced", "torn"],
)
def test_invalid_stdin_is_spooled_silently(stdin: bytes) -> None:
    _silent_success(run_hook("Notification", stdin=stdin))
    [path] = envelope_files()
    envelope = load(path)
    assert envelope["payload_text"] == stdin.decode("utf-8", errors="replace")
    assert envelope["truncated"] is False


def _inbox_is_0500(inbox: Path) -> None:
    inbox.mkdir(parents=True, mode=0o700)
    inbox.chmod(0o500)


def _inbox_is_0755(inbox: Path) -> None:
    inbox.mkdir(parents=True, mode=0o700)
    inbox.chmod(0o755)


def _inbox_is_a_symlink(inbox: Path) -> None:
    target = inbox.parent / "elsewhere"
    target.mkdir(parents=True, mode=0o700)
    inbox.symlink_to(target)


def _inbox_is_a_file(inbox: Path) -> None:
    inbox.parent.mkdir(parents=True, mode=0o700)
    inbox.write_bytes(b"")


def _data_dir_is_a_file(inbox: Path) -> None:
    data = paths.data_dir()
    data.rmdir()
    data.write_bytes(b"")


def _root_not_writable(inbox: Path) -> None:
    inbox.parent.mkdir(parents=True, mode=0o700)
    inbox.parent.chmod(0o500)


@pytest.mark.parametrize(
    "break_inbox",
    [
        _inbox_is_0500,
        _inbox_is_0755,
        _inbox_is_a_symlink,
        _inbox_is_a_file,
        _data_dir_is_a_file,
        _root_not_writable,
    ],
    ids=lambda f: f.__name__.lstrip("_"),
)
def test_unwritable_inbox_is_silent_and_writes_nothing(break_inbox: object) -> None:
    inbox = paths.inbox_dir("claude")
    assert callable(break_inbox)
    break_inbox(inbox)
    try:
        _silent_success(run_hook("Stop", stdin=b"{}"))
        if inbox.is_dir() and not inbox.is_symlink():
            inbox.chmod(0o700)
            assert envelope_files() == []
        elsewhere = inbox.parent / "elsewhere"
        if elsewhere.is_dir():
            assert list(elsewhere.iterdir()) == []
    finally:
        for directory in (inbox, inbox.parent):
            if directory.is_dir() and not directory.is_symlink():
                directory.chmod(0o700)


def test_unwritable_statusline_directory_is_silent() -> None:
    directory = paths.statusline_dir()
    directory.mkdir(parents=True, mode=0o700)
    directory.chmod(0o500)
    try:
        _silent_success(run_hook("StatusLine", stdin=b'{"session_id":"s-1"}'))
    finally:
        directory.chmod(0o700)
    assert list(directory.iterdir()) == []


# The timing tests measure the hook's own runtime, from when it starts to when the parent sees it
# exit, so that a slow interpreter start-up on a busy machine cannot make them flaky. The real
# entry point reports its start as the envelope's ``received_at_ns``; the probe below writes a
# stamp just before it imports the hook.
_TIMED_PROBE = """
import os
import sys
import time

with open(os.environ["FYLGJA_TEST_STAMP"], "w", encoding="ascii") as handle:
    handle.write(str(time.time_ns()))
import fylgja.hook as hook

if sys.argv[1] == "--stuck":
    hook.run = lambda *args, **kwargs: time.sleep(60)
sys.argv = ["fylgja", "hook", *sys.argv[2:]]
import fylgja._entry

fylgja._entry.main()
"""


def _run_open_stdin(
    command: list[str], env: dict[str, str], stdin: bytes = b""
) -> tuple[int, bytes, bytes, int]:
    """Run ``command`` with a stdin that stays open; ``(code, stdout, stderr, exit_time_ns)``."""
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert process.stdin is not None
    try:
        if stdin:
            process.stdin.write(stdin)
            process.stdin.flush()
        returncode = process.wait(timeout=15)
        exited_ns = time.time_ns()
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
        process.stdin.close()
    return returncode, stdout, stderr, exited_ns


def _timed_probe(tmp: Path, mode: str, *args: str, **env: str) -> tuple[int, bytes, bytes, float]:
    """Run the probe; ``(code, stdout, stderr, seconds from the stamp to the exit)``."""
    stamp = tmp / "started_ns"
    code, out, err, exited_ns = _run_open_stdin(
        [sys.executable, "-c", _TIMED_PROBE, mode, *args],
        clean_env(FYLGJA_TEST_STAMP=str(stamp), **env),
    )
    return code, out, err, (exited_ns - int(stamp.read_text(encoding="ascii"))) / 1e9


@pytest.mark.slow
def test_stdin_that_never_closes_still_ends_within_3_s() -> None:
    code, stdout, stderr, exited_ns = _run_open_stdin(
        [*HOOK_COMMAND, "Stop"], clean_env(), stdin=b'{"session_id":"abc",'
    )
    assert (code, stdout, stderr) == (0, b"", b"")
    [path] = envelope_files()
    envelope = load(path)
    assert envelope["payload_text"] == '{"session_id":"abc",'
    assert envelope["truncated"] is True
    runtime = (exited_ns - envelope["received_at_ns"]) / 1e9
    # The 2 s stdin deadline ends it, well before the 3 s alarm.
    assert 1.9 <= runtime < 3.0


@pytest.mark.slow
def test_the_alarm_backstop_ends_a_stuck_hook_with_exit_0(isolated_env: Path) -> None:
    code, stdout, stderr, runtime = _timed_probe(isolated_env, "--stuck", "Stop")
    assert (code, stdout, stderr) == (0, b"", b"")
    assert 2.9 <= runtime < 3.5


@pytest.mark.parametrize("guard", ["FYLGJA_SUMMARY", "LIDSKJALV_TRACK_SUMMARY"])
def test_summary_sessions_exit_at_once_without_reading_stdin(
    isolated_env: Path, guard: str
) -> None:
    code, stdout, stderr, runtime = _timed_probe(isolated_env, "--real", "Stop", **{guard: "1"})
    assert (code, stdout, stderr) == (0, b"", b"")
    # Reading the open stdin would have taken the full 2 s deadline.
    assert runtime < 1.0
    code, stdout, stderr, _ = _run_open_stdin([*HOOK_COMMAND, "Stop"], clean_env(**{guard: "1"}))
    assert (code, stdout, stderr) == (0, b"", b"")
    assert not paths.hook_inbox_root().exists()
    assert not paths.statusline_dir().exists()


def test_oversize_json_keeps_its_structure_with_long_strings_cut() -> None:
    payload = {**STOP_PAYLOAD, "hook_event_name": "PostToolUse", "tool_response": "r" * 400_000}
    _silent_success(run_hook("PostToolUse", stdin=json.dumps(payload).encode()))
    [path] = envelope_files()
    envelope = load(path)
    assert envelope["truncated"] is True
    assert json.loads(envelope["payload_text"]) == {
        **STOP_PAYLOAD,
        "hook_event_name": "PostToolUse",
        "tool_response": {"_truncated": 400_000},
    }
    assert path.stat().st_size < 4096


def test_oversize_garbage_becomes_the_marker() -> None:
    raw = b"x" * 300_000
    _silent_success(run_hook("PostToolUse", stdin=raw))
    [path] = envelope_files()
    envelope = load(path)
    assert envelope["truncated"] is True
    assert json.loads(envelope["payload_text"]) == {"_malformed_oversize": 300_000}


def test_input_beyond_the_read_limit_is_drained_and_marked() -> None:
    raw = json.dumps({"blob": "b" * (5 * 1024 * 1024)}).encode()
    _silent_success(run_hook("PostToolUse", stdin=raw))
    [path] = envelope_files()
    envelope = load(path)
    assert json.loads(envelope["payload_text"]) == {"_malformed_oversize": len(raw)}


@pytest.mark.parametrize(
    "args",
    [(), ("--help",), ("-h",), ("--source", "../escape", "Stop"), ("--source",), ("Stop", "x")],
    ids=["no-event", "help", "short-help", "bad-source", "source-without-value", "extra-arg"],
)
def test_exit_code_is_0_for_any_arguments(args: tuple[str, ...]) -> None:
    _silent_success(run_hook(*args, stdin=b"{}"))
    escaped = paths.data_dir() / "escape"
    assert not escaped.exists()


def test_statusline_snapshot_is_replaced_not_appended() -> None:
    _silent_success(run_hook("StatusLine", stdin=b'{"session_id":"s-1","n":1}'))
    snapshot = paths.statusline_dir() / "s-1.json"
    first = load(snapshot)
    _silent_success(run_hook("StatusLine", stdin=b'{"session_id":"s-1","n":2}'))
    assert load(snapshot) == first, "a second render inside 2 s must be skipped"
    old = time.time_ns() - 5 * 10**9
    os.utime(snapshot, ns=(old, old))
    _silent_success(run_hook("StatusLine", stdin=b'{"session_id":"s-1","n":3}'))
    third = load(snapshot)
    assert json.loads(third["payload_text"]) == {"session_id": "s-1", "n": 3}
    assert third["event_arg"] == "StatusLine"
    assert [p.name for p in paths.statusline_dir().iterdir()] == ["s-1.json"]
    assert envelope_files() == []


def test_full_backlog_drops_and_counts() -> None:
    inbox = paths.inbox_dir("claude")
    inbox.mkdir(parents=True, mode=0o700)
    (inbox / ".backlog-stamp").write_bytes(b"full\n")
    for _ in range(3):
        _silent_success(run_hook("Stop", stdin=b"{}"))
    assert envelope_files() == []
    assert (inbox / "dropped.count").read_text() == "3\n"


def test_the_installed_console_script_dispatches_the_hook() -> None:
    script = Path(sys.executable).with_name("fylgja")
    if not script.exists():
        pytest.skip("no fylgja console script beside the interpreter")
    result = subprocess.run(
        [str(script), "hook", "SessionStart"],
        input=b'{"source":"startup"}',
        capture_output=True,
        env=clean_env(),
        timeout=20,
        check=False,
    )
    _silent_success(result)
    [path] = envelope_files()
    assert load(path)["event_arg"] == "SessionStart"


@pytest.mark.slow
@pytest.mark.timeout(180)
def test_200_concurrent_invocations_yield_200_distinct_complete_files(
    isolated_env: Path,
) -> None:
    count = 200
    payload_dir = isolated_env / "payloads"
    payload_dir.mkdir()
    output = isolated_env / "output.log"
    env = clean_env(CLAUDE_CODE_SESSION_ID="concurrent")
    processes: list[subprocess.Popen[bytes]] = []
    with output.open("ab") as sink:
        for n in range(count):
            body = {**STOP_PAYLOAD, "n": n, "padding": "p" * (n * 37 % 2000)}
            payload = payload_dir / f"{n}.json"
            payload.write_text(json.dumps(body), encoding="utf-8")
            with payload.open("rb") as stdin:
                processes.append(
                    subprocess.Popen(
                        [*HOOK_COMMAND, "Stop"],
                        stdin=stdin,
                        stdout=sink,
                        stderr=sink,
                        env=env,
                    )
                )
        codes = [process.wait(timeout=150) for process in processes]
    assert codes == [0] * count
    assert output.read_bytes() == b""
    files = envelope_files()
    assert len(files) == count
    envelopes = [load(path) for path in files]
    assert len({e["id"] for e in envelopes}) == count
    seen = sorted(json.loads(e["payload_text"])["n"] for e in envelopes)
    assert seen == list(range(count))
    for path, envelope in zip(files, envelopes, strict=True):
        assert path.name == f"{envelope['received_at_ns']}-{envelope['id']}.json"
        assert _mode(path) == 0o600
        body = json.loads(envelope["payload_text"])
        assert (payload_dir / f"{body['n']}.json").read_text(encoding="utf-8") == envelope[
            "payload_text"
        ]
    inbox = paths.inbox_dir("claude")
    assert not [p for p in inbox.iterdir() if p.name.startswith(".tmp-")]
    assert not (inbox / "dropped.count").exists()
