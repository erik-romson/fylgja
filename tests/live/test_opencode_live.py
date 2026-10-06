"""Opt-in smoke tests: OpenCode sessions in ``/now`` (WP3.2 done condition).

Run by hand, with the daemon running, the OpenCode collector enabled and at least one OpenCode TUI
open::

    FYLGJA_RUN_LIVE=1 uv run --frozen pytest -m live tests/live/test_opencode_live.py

The tests **start nothing and write nothing**. They list the running ``opencode`` processes with
``pgrep`` and ``lsof`` (the working directory of each), read ``opencode.db`` read-only to learn the
true busy or idle state of the newest session of that directory, and compare it with ``/now``:

* ``test_every_running_opencode_session_appears_in_now``: each process with a session shows up as
  ``opencode:<session id>`` with the correct state, a project (``project_path`` or ``cwd``) and a
  non-empty one-liner;
* ``test_a_state_change_shows_within_the_lag_limit``: watches the sessions for up to
  ``FYLGJA_LIVE_OPENCODE_WATCH_SECONDS`` (default 120) and fails when ``/now`` lags the database by
  more than the limit. **A human drives it:** start a turn in an OpenCode session, or let one
  finish, while the test watches. It skips when nothing changed.

The limit is 2 s with the optional TypeScript plugin (``~/.config/opencode/plugins/fylgja.ts``
exists: the envelope wakes the poll) and one poll interval (2 s) without it, each with 0.5 s of
slack for the HTTP call and the database read.

Only session ids, states and counts are printed: no titles, no message text. ``FYLGJA_PORT``
overrides the daemon port (default 7319), ``XDG_DATA_HOME`` the database location.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from fylgja.collectors.codex import parse as process_parse
from fylgja.collectors.opencode import adapter, parse

pytestmark = pytest.mark.live

POLL_INTERVAL_SECONDS = 2.0
SLACK_SECONDS = 0.5
PLUGIN_FILE = Path.home() / ".config" / "opencode" / "plugins" / "fylgja.ts"
WATCH_SECONDS = float(os.environ.get("FYLGJA_LIVE_OPENCODE_WATCH_SECONDS", "120"))
STALE_SECONDS = 1800.0
PGREP = shutil.which("pgrep") or "/usr/bin/pgrep"
LSOF = shutil.which("lsof") or "/usr/sbin/lsof"
EMPTY_ONE_LINER = "—"


def lag_limit() -> float:
    """2 s with the plugin installed, one poll interval without it; each plus the slack."""
    return POLL_INTERVAL_SECONDS + SLACK_SECONDS


@dataclass(frozen=True)
class Running:
    pid: int
    cwd: str
    session_id: str

    @property
    def key(self) -> str:
        return parse.session_key(self.session_id)


def _run(argv: list[str]) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=15)
    return result.stdout


def _database() -> sqlite3.Connection:
    uri = adapter.default_db_path().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=1.0)
    connection.execute("PRAGMA query_only = ON")
    return connection


def _state(connection: sqlite3.Connection, session_id: str) -> str:
    """``busy`` or ``idle`` from the newest assistant message, by the collector's rule."""
    row = connection.execute(
        "SELECT json_extract(data, '$.time.completed') IS NULL,"
        " json_extract(data, '$.error') IS NOT NULL, time_updated"
        " FROM message WHERE session_id = ? AND json_extract(data, '$.role') = 'assistant'"
        " ORDER BY time_created DESC, id DESC LIMIT 1",
        [session_id],
    ).fetchone()
    if row is None or not row[0] or row[1]:
        return parse.IDLE
    session = connection.execute(
        "SELECT time_updated FROM session WHERE id = ?", [session_id]
    ).fetchone()
    alive = max(int(row[2]), int(session[0]))
    return parse.BUSY if time.time() * 1000 - alive <= STALE_SECONDS * 1000 else parse.IDLE


def _running() -> list[Running]:
    """The running OpenCode processes with a session row; two in one directory share the newest."""
    pids = [
        int(line)
        for line in _run([PGREP, "-x", "-u", str(os.geteuid()), "opencode"]).split()
        if line.isdigit()
    ]
    if not pids:
        return []
    listing = _run([LSOF, "-nP", "-a", "-p", ",".join(map(str, pids)), "-d", "cwd", "-Fpfn"])
    cwds = {pid: info.cwd for pid, info in process_parse.parse_lsof_fields(listing).items()}
    found: list[Running] = []
    with _database() as connection:
        for pid, cwd in sorted(cwds.items()):
            if cwd is None:
                continue
            rows = connection.execute(
                "SELECT id FROM session WHERE parent_id IS NULL AND time_archived IS NULL"
                " AND directory = ? ORDER BY time_updated DESC, id",
                [parse.norm_dir(cwd)],
            ).fetchall()
            taken = {item.session_id for item in found if item.cwd == cwd}
            session_id = next((str(r[0]) for r in rows if str(r[0]) not in taken), None)
            if session_id is not None:
                found.append(Running(pid, cwd, session_id))
    return found


def _now() -> dict[str, Any]:
    port = os.environ.get("FYLGJA_PORT", "7319")
    request = urllib.request.Request(f"http://127.0.0.1:{port}/now")  # noqa: S310
    with urllib.request.urlopen(request, timeout=2) as response:  # noqa: S310
        body: dict[str, Any] = json.load(response)
    return body


def _opencode_sessions() -> dict[str, dict[str, Any]]:
    sessions = _now().get("sessions") or []
    return {s["key"]: s for s in sessions if isinstance(s, dict) and s.get("kind") == "opencode"}


@pytest.fixture
def daemon() -> None:
    try:
        _now()
    except (urllib.error.URLError, OSError):
        pytest.skip("the Fylgja daemon is not answering on /now")


@pytest.fixture
def running() -> list[Running]:
    if not adapter.default_db_path().is_file():
        pytest.skip("there is no opencode.db")
    found = _running()
    if not found:
        pytest.skip("no OpenCode process with a session is running")
    return found


@pytest.mark.usefixtures("daemon")
def test_every_running_opencode_session_appears_in_now(running: list[Running]) -> None:
    deadline = time.monotonic() + lag_limit()
    problems: list[str] = []
    while True:
        problems = []
        listed = _opencode_sessions()
        with _database() as connection:
            for process in running:
                session = listed.get(process.key)
                if session is None:
                    problems.append(f"{process.key}: not in /now")
                    continue
                expected = _state(connection, process.session_id)
                if session.get("state") != expected:
                    problems.append(
                        f"{process.key}: state {session.get('state')!r}, want {expected!r}"
                    )
                if not (session.get("project_path") or session.get("cwd")):
                    problems.append(f"{process.key}: no project")
                one_liner = session.get("one_liner")
                if not one_liner or one_liner == EMPTY_ONE_LINER:
                    problems.append(f"{process.key}: empty one-liner")
        if not problems or time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    plugin = "with" if PLUGIN_FILE.is_file() else "without"
    assert problems == [], f"{len(running)} running ({plugin} the plugin); " + "; ".join(problems)


@pytest.mark.usefixtures("daemon")
def test_a_state_change_shows_within_the_lag_limit(running: list[Running]) -> None:
    limit = lag_limit()
    with _database() as connection:
        truth = {p.key: _state(connection, p.session_id) for p in running}
    changed_at: dict[str, float] = {}
    worst = 0.0
    changes = 0
    deadline = time.monotonic() + WATCH_SECONDS
    while time.monotonic() < deadline:
        listed = _opencode_sessions()
        with _database() as connection:
            for process in running:
                state = _state(connection, process.session_id)
                if state != truth[process.key]:
                    truth[process.key] = state
                    changed_at[process.key] = time.monotonic()
                    changes += 1
        for process in running:
            started = changed_at.get(process.key)
            if started is None:
                continue
            if listed.get(process.key, {}).get("state") == truth[process.key]:
                worst = max(worst, time.monotonic() - started)
                del changed_at[process.key]
            elif time.monotonic() - started > limit:
                pytest.fail(
                    f"{process.key}: /now still shows {listed.get(process.key, {}).get('state')!r}"
                    f" {limit}s after the database turned {truth[process.key]!r}"
                )
        time.sleep(0.1)
    if not changes:
        pytest.skip("no state change was seen: start or finish an OpenCode turn while this runs")
    assert worst <= limit
