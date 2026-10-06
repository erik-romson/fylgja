"""Opt-in smoke tests: Codex sessions in ``/now`` (WP3.1 done condition).

Run by hand, with the daemon running, the Codex collector enabled and at least one Codex CLI
session open::

    FYLGJA_RUN_LIVE=1 uv run --frozen pytest -m live tests/live/test_codex_live.py

The tests **start nothing and write nothing**. They list the running ``codex`` processes with
``pgrep`` and ``lsof`` (the rollout file each has open), read the end of that rollout to learn the
true busy or idle state, and compare it with ``/now``:

* ``test_every_running_codex_process_appears_in_now``: each process with an open rollout shows up as
  ``codex:<thread id>`` with the correct state, a project (``project_path`` or ``cwd``) and a
  non-empty one-liner;
* ``test_a_state_change_shows_within_one_poll_interval``: watches the sessions for up to
  ``FYLGJA_LIVE_CODEX_WATCH_SECONDS`` (default 120) and fails when ``/now`` lags the rollout by
  more than one poll interval (2 s, plus 1 s of slack). **A human drives it:** start a turn in a
  Codex session, or interrupt one, while the test watches. It skips when nothing changed.

Only thread ids, states and counts are printed: no prompt text, no titles. ``FYLGJA_PORT``
overrides the daemon port (default 7319).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from fylgja.collectors.codex import parse

pytestmark = pytest.mark.live

POLL_INTERVAL_SECONDS = 2.0
SLACK_SECONDS = 1.0
LAG_LIMIT_SECONDS = POLL_INTERVAL_SECONDS + SLACK_SECONDS
WATCH_SECONDS = float(os.environ.get("FYLGJA_LIVE_CODEX_WATCH_SECONDS", "120"))
TAIL_BYTES = 1 << 20
PGREP = shutil.which("pgrep") or "/usr/bin/pgrep"
LSOF = shutil.which("lsof") or "/usr/sbin/lsof"
EMPTY_ONE_LINER = "—"


@dataclass(frozen=True)
class Running:
    pid: int
    thread_id: str
    rollout: Path

    @property
    def key(self) -> str:
        return parse.session_key(self.thread_id)


def _run(argv: list[str]) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=15)
    return result.stdout


def _running() -> list[Running]:
    """The running Codex processes that have a rollout open, one per thread."""
    pids = parse.parse_pgrep(_run([PGREP, "-x", "-u", str(os.geteuid()), "codex"]))
    if not pids:
        return []
    listing = _run([LSOF, "-nP", "-a", "-p", ",".join(map(str, pids)), "-d", "^txt,^mem", "-Fpfn"])
    found: list[Running] = []
    seen: set[str] = set()
    for pid, info in sorted(parse.parse_lsof_fields(listing).items()):
        for path in sorted(info.rollouts, key=lambda p: Path(p).name):
            thread_id = parse.rollout_thread_id(path)
            if thread_id is not None and thread_id not in seen:
                seen.add(thread_id)
                found.append(Running(pid, thread_id, Path(path)))
                break
    return found


def _rollout_state(path: Path) -> str | None:
    """``busy``, ``idle`` or ``None`` from the end of the rollout, widened until a turn shows."""
    size = path.stat().st_size
    with path.open("rb") as handle:
        limit = min(size, TAIL_BYTES)
        while True:
            handle.seek(size - limit)
            data = handle.read(limit)
            lines = data.split(b"\n")[1:] if limit < size else data.split(b"\n")
            state = parse.analyze_rollout(lines).state
            if state is not None or limit >= size or limit >= 8 * TAIL_BYTES:
                return state
            limit = min(size, limit * 4)


def _now() -> dict[str, Any]:
    port = os.environ.get("FYLGJA_PORT", "7319")
    request = urllib.request.Request(f"http://127.0.0.1:{port}/now")  # noqa: S310
    with urllib.request.urlopen(request, timeout=2) as response:  # noqa: S310
        body: dict[str, Any] = json.load(response)
    return body


def _codex_sessions() -> dict[str, dict[str, Any]]:
    sessions = _now().get("sessions") or []
    return {s["key"]: s for s in sessions if isinstance(s, dict) and s.get("kind") == "codex"}


@pytest.fixture
def daemon() -> None:
    try:
        _now()
    except (urllib.error.URLError, OSError):
        pytest.skip("the Fylgja daemon is not answering on /now")


@pytest.fixture
def running() -> list[Running]:
    found = _running()
    if not found:
        pytest.skip("no Codex CLI process with an open rollout is running")
    return found


@pytest.mark.usefixtures("daemon")
def test_every_running_codex_process_appears_in_now(running: list[Running]) -> None:
    deadline = time.monotonic() + LAG_LIMIT_SECONDS
    problems: list[str] = []
    while True:
        problems = []
        listed = _codex_sessions()
        for process in running:
            session = listed.get(process.key)
            if session is None:
                problems.append(f"{process.key}: not in /now")
                continue
            expected = _rollout_state(process.rollout)
            if expected is not None and session.get("state") != expected:
                problems.append(f"{process.key}: state {session.get('state')!r}, want {expected!r}")
            if not (session.get("project_path") or session.get("cwd")):
                problems.append(f"{process.key}: no project")
            one_liner = session.get("one_liner")
            if not one_liner or one_liner == EMPTY_ONE_LINER:
                problems.append(f"{process.key}: empty one-liner")
        if not problems or time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    assert problems == [], f"{len(running)} running; " + "; ".join(problems)


@pytest.mark.usefixtures("daemon")
def test_a_state_change_shows_within_one_poll_interval(running: list[Running]) -> None:
    truth: dict[str, str | None] = {p.key: _rollout_state(p.rollout) for p in running}
    changed_at: dict[str, float] = {}
    worst = 0.0
    changes = 0
    deadline = time.monotonic() + WATCH_SECONDS
    while time.monotonic() < deadline:
        listed = _codex_sessions()
        for process in running:
            state = _rollout_state(process.rollout)
            if state is not None and state != truth[process.key]:
                truth[process.key] = state
                changed_at[process.key] = time.monotonic()
                changes += 1
            started = changed_at.get(process.key)
            if started is None:
                continue
            if listed.get(process.key, {}).get("state") == truth[process.key]:
                worst = max(worst, time.monotonic() - started)
                del changed_at[process.key]
            elif time.monotonic() - started > LAG_LIMIT_SECONDS:
                pytest.fail(
                    f"{process.key}: /now still shows {listed.get(process.key, {}).get('state')!r}"
                    f" {LAG_LIMIT_SECONDS}s after the rollout turned {truth[process.key]!r}"
                )
        time.sleep(0.1)
    if not changes:
        pytest.skip("no state change was seen: start or interrupt a Codex turn while this runs")
    assert worst <= LAG_LIMIT_SECONDS
