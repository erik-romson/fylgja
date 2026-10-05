"""Canary secrets never reach Fylgja's files (``PLAN-final-plan.md`` §4.5, A3, ``PLAN.md`` §4.3.1).

A canary ``CLAUDE_CODE_MESSAGING_TOKEN`` and ``CMUX_SOCKET_CAPABILITY`` sit in the hook's
environment, next to every allowlisted variable. No file the hook writes may contain them.

Later steps extend this file: the inbox drain (step 19) checks the database, and messaging
(step 31) checks the collector, logs and API responses. Use :data:`CANARIES`,
:func:`canary_env` and :func:`assert_no_canary` for those checks.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from hook_support import all_bytes_under, clean_env, envelope_files, feed, run_hook

from fylgja import hook, paths

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
