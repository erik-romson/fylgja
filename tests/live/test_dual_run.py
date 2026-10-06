"""Opt-in live tests for the Phase 2 dual run (WP2.7): never run by CI or by the pipeline.

Run them by hand, one at a time::

    FYLGJA_RUN_LIVE=1 uv run --frozen pytest -m live tests/live/test_dual_run.py -k does_not_wait
    FYLGJA_RUN_LIVE=1 uv run --frozen pytest -m live tests/live/test_dual_run.py -k downtime

Preconditions (both tests):

* the daemon is installed under launchd (``fylgja install launchd --apply``), label
  ``io.github.erik-romson.fylgja``, and answers on ``127.0.0.1:$FYLGJA_PORT`` (default 7319);
* ``~/.local/bin/fylgja`` exists (the executable the installed hooks call);
* Claude Code hooks are installed (``fylgja install hooks --apply``), for the downtime test;
* **a human is present.** The downtime test stops the daemon for ``FYLGJA_LIVE_DOWNTIME_SECONDS``
  (default 600) and needs a Claude Code session to be working in that time, started by hand. The
  latency test makes two small paid ``claude -p`` calls per run.

"Claude does not wait for Fylgja" runs a disposable ``claude -p`` in a temporary directory with a
``--settings`` file that registers a Fylgja hook, wrapped in a script that sleeps 5 s before it
calls ``fylgja hook``. The wrapper points ``FYLGJA_DATA_DIR`` at the temporary directory, so the
envelopes never reach the real inbox. The wall time of the run with the slow hook must equal the
wall time of a run without it within ± 10 % (median of ``FYLGJA_LIVE_RUNS`` interleaved pairs,
default 3). ``fylgja hook`` has no delay knob: the delay lives only in the wrapper.

"Downtime replay" stops the daemon, waits while a session writes envelopes into the hook inbox,
restarts the daemon and checks that every envelope that was in the inbox before the restart is
ingested exactly once: the inbox files are counted before the restart, and every envelope id must
then have exactly one row in ``events`` (read-only). ``KeepAlive`` would restart a killed daemon
after the 10 s throttle, so the test sends ``launchctl kill TERM`` and then boots the job out;
the ``finally`` block bootstraps it again from ``~/Library/LaunchAgents``.

The tests read ids, counts and times only: no prompt text and no tokens are printed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = [pytest.mark.live, pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")]

LABEL = "io.github.erik-romson.fylgja"
HOOK_DELAY_SECONDS = 5
LATENCY_TOLERANCE = 0.10
DOWNTIME_SECONDS = int(os.environ.get("FYLGJA_LIVE_DOWNTIME_SECONDS", "600"))
LATENCY_RUNS = int(os.environ.get("FYLGJA_LIVE_RUNS", "3"))
PROMPT = "Reply with the single word: ok"

# The autouse isolation fixture moves HOME and FYLGJA_*_DIR per test. These tests work on the
# real installation, so they take the real locations from the environment as it was at import.
REAL_HOME = Path(os.path.expanduser("~"))
REAL_DATA_DIR = (
    Path(os.environ["FYLGJA_DATA_DIR"]).expanduser()
    if os.environ.get("FYLGJA_DATA_DIR")
    else REAL_HOME / ".local" / "share" / "fylgja"
)
FYLGJA_BIN = REAL_HOME / ".local" / "bin" / "fylgja"
PLIST = REAL_HOME / "Library" / "LaunchAgents" / f"{LABEL}.plist"
ENVELOPE_NAME = re.compile(r"[0-9]+-([0-9a-f-]{36})\.json")


def _service_target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *args], capture_output=True, text=True, check=False, timeout=30
    )


def _daemon_answers() -> bool:
    port = os.environ.get("FYLGJA_PORT", "7319")
    request = urllib.request.Request(f"http://127.0.0.1:{port}/now")  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=2) as response:  # noqa: S310
            return bool(response.status == 200)
    except (urllib.error.URLError, OSError):
        return False


def _wait_until(predicate: object, seconds: float, step: float = 0.5) -> bool:
    assert callable(predicate)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return bool(predicate())


# --------------------------------------------------------------------------------------------
# 1. Claude does not wait for Fylgja
# --------------------------------------------------------------------------------------------


def _write_hook_settings(directory: Path, *, slow_hook: bool) -> tuple[Path, Path]:
    """Write the ``--settings`` file; return it and the marker the wrapper touches when it runs."""
    marker = directory / "hook-started"
    wrapper = directory / "slow-fylgja-hook.sh"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'touch "{marker}"\n'
        f"sleep {HOOK_DELAY_SECONDS}\n"
        f'FYLGJA_DATA_DIR="{directory / "data"}" exec "{FYLGJA_BIN}" hook "$1"\n',
        encoding="utf-8",
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    hooks: dict[str, list[dict[str, object]]] = {}
    if slow_hook:
        for event in ("UserPromptSubmit", "Stop"):
            hooks[event] = [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": f"{wrapper} {event}", "async": True}],
                }
            ]
    settings = directory / ("settings-hook.json" if slow_hook else "settings-plain.json")
    settings.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    return settings, marker


def _timed_claude(workdir: Path, settings: Path) -> float:
    """Wall time of one disposable ``claude -p`` turn, in seconds."""
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE",)}
    env["HOME"] = str(REAL_HOME)
    started = time.monotonic()
    result = subprocess.run(
        [
            "claude",
            "-p",
            PROMPT,
            "--settings",
            str(settings),
            "--no-session-persistence",
            "--output-format",
            "json",
        ],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
        stdin=subprocess.DEVNULL,
    )
    elapsed = time.monotonic() - started
    assert result.returncode == 0, f"claude -p exited {result.returncode}"
    return elapsed


@pytest.mark.timeout(1800)
def test_claude_does_not_wait_for_fylgja(tmp_path: Path) -> None:
    if shutil.which("claude") is None:
        pytest.skip("claude is not on PATH")
    if not FYLGJA_BIN.is_file() or not os.access(FYLGJA_BIN, os.X_OK):
        pytest.skip(f"{FYLGJA_BIN} is not an executable")
    workdir = tmp_path / "work"
    workdir.mkdir()
    plain, _ = _write_hook_settings(tmp_path, slow_hook=False)
    hooked, marker = _write_hook_settings(tmp_path, slow_hook=True)

    plain_times: list[float] = []
    hooked_times: list[float] = []
    for _ in range(LATENCY_RUNS):
        plain_times.append(_timed_claude(workdir, plain))
        marker.unlink(missing_ok=True)
        hooked_times.append(_timed_claude(workdir, hooked))
        assert marker.exists(), "the slow hook never started, so the run proves nothing"

    baseline = statistics.median(plain_times)
    with_hook = statistics.median(hooked_times)
    ratio = with_hook / baseline
    print(f"claude -p latency: without hook {baseline:.2f}s, with 5s hook {with_hook:.2f}s")
    assert abs(ratio - 1.0) <= LATENCY_TOLERANCE, (
        f"a {HOOK_DELAY_SECONDS}s hook changed the turn latency by {100 * (ratio - 1):+.1f} % "
        f"(plain {plain_times}, hooked {hooked_times})"
    )
    assert with_hook - baseline < HOOK_DELAY_SECONDS, "the run waited for the hook's sleep"


# --------------------------------------------------------------------------------------------
# 2. Downtime replay
# --------------------------------------------------------------------------------------------


def _inbox_ids(inbox: Path) -> dict[str, Path]:
    ids: dict[str, Path] = {}
    if inbox.is_dir():
        for path in sorted(inbox.iterdir()):
            match = ENVELOPE_NAME.fullmatch(path.name)
            if match:
                ids[match[1]] = path
    return ids


def _event_rows_by_envelope(database: Path, envelope_ids: list[str]) -> dict[str, int]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=10)
    try:
        counts = {}
        for envelope_id in envelope_ids:
            row = connection.execute(
                "SELECT count(*) FROM events WHERE json_extract(payload, '$.envelope_id') = ?",
                (envelope_id,),
            ).fetchone()
            counts[envelope_id] = int(row[0])
        return counts
    finally:
        connection.close()


@pytest.fixture
def stopped_daemon() -> Iterator[None]:
    """Stop the daemon for the test; always bring it back."""
    if not _daemon_answers():
        pytest.skip("the Fylgja daemon is not answering on /now")
    if _launchctl("print", _service_target()).returncode != 0:
        pytest.skip(f"{LABEL} is not loaded under launchd")
    if not PLIST.is_file():
        pytest.skip(f"{PLIST} is missing, so the job could not be restarted")
    try:
        yield
    finally:
        if not _daemon_answers():
            _launchctl("bootstrap", f"gui/{os.getuid()}", str(PLIST))
            _launchctl("kickstart", _service_target())


@pytest.mark.usefixtures("stopped_daemon")
@pytest.mark.timeout(DOWNTIME_SECONDS + 900)
def test_downtime_replay_ingests_every_envelope_exactly_once() -> None:
    inbox = REAL_DATA_DIR / "hook-inbox" / "claude"
    database = REAL_DATA_DIR / "fylgja.db"
    assert database.is_file(), f"no database at {database}"

    # Stop the daemon. TERM first, as specified; KeepAlive would restart it after the 10 s
    # throttle, so the job is then booted out for the length of the downtime.
    _launchctl("kill", "TERM", _service_target())
    _launchctl("bootout", _service_target())
    assert _wait_until(lambda: not _daemon_answers(), 30), "the daemon kept answering after TERM"

    print(
        f"\nDaemon stopped. Work in a Claude Code session for {DOWNTIME_SECONDS}s "
        "(the test restarts the daemon afterwards)."
    )
    time.sleep(DOWNTIME_SECONDS)

    envelopes = _inbox_ids(inbox)
    assert envelopes, "no envelope arrived during the downtime: was a session working?"
    waiting = sorted(envelopes)
    print(f"{len(waiting)} envelope files in the inbox before the restart")

    boot = _launchctl("bootstrap", f"gui/{os.getuid()}", str(PLIST))
    assert boot.returncode == 0, f"launchctl bootstrap exited {boot.returncode}"
    assert _wait_until(_daemon_answers, 90), "the daemon did not come back"
    assert _wait_until(
        lambda: not any(path.exists() for path in envelopes.values()), 180, step=1.0
    ), "the drain left envelope files behind"

    counts = _event_rows_by_envelope(database, waiting)
    missing = [i for i, n in counts.items() if n == 0]
    duplicated = [i for i, n in counts.items() if n > 1]
    assert not missing, f"{len(missing)} envelopes were never ingested: {missing[:5]}"
    assert not duplicated, f"{len(duplicated)} envelopes were ingested twice: {duplicated[:5]}"
    assert sum(counts.values()) == len(waiting)
