"""Opt-in smoke test of the presence collector under launchd (WP5.1). A human runs it.

Setup: the daemon runs under launchd (``fylgja install launchd --apply``) with the presence
collector enabled, and ``fylgja doctor`` shows ``source:presence`` as ``ok``. For the idle test,
lower the threshold for the run in ``~/.config/fylgja/config.toml``
(``[collectors.presence] idle_threshold_seconds = 30``), restart the daemon, and pass the same
number in ``FYLGJA_PRESENCE_IDLE_THRESHOLD`` (default 300, which means a five-minute wait). A
window must be focused while the idle test runs: the idle poll only runs while a focus span is
open.

Run it with ``-s`` so the prompts show::

    FYLGJA_RUN_LIVE=1 uv run --frozen pytest -m live -s tests/live/test_presence_live.py

Each test asks you to do one thing, then reads the presence events of the daemon's store, read-only
(``mode=ro``), that arrived after the prompt:

* lock: lock the screen, wait a few seconds, unlock it;
* idle: touch nothing for the threshold plus about a minute, then touch the mouse;
* call: start a call or a recording (FaceTime, Voice Memos, a meeting) and stop it again.

The requirement is that each event appears within 15 s of its transition. The transition time is the
event's own ``observed_at`` (idle events are stamped with the moment the threshold was crossed and
the moment of the first input), so the test checks ``received_at - observed_at <= 15 s``: the
collector's detection lag. The test starts, locks and records nothing itself and writes no file.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest

from fylgja import paths

pytestmark = pytest.mark.live

LABEL = "io.github.erik-romson.fylgja"
LAG_SECONDS = 15.0
POLL_SECONDS = 0.5
ACTION_SECONDS = 120.0
IDLE_THRESHOLD = float(os.environ.get("FYLGJA_PRESENCE_IDLE_THRESHOLD", "300"))

EVENTS_SQL = """
SELECT id, entity_kind, entity_id, observed_at, received_at,
       json_extract(payload, '$.state') AS state,
       json_extract(payload, '$.data.event') AS call_event
FROM events
WHERE source = 'presence' AND record_type = 'event' AND id > ?
ORDER BY id
"""


def _connect() -> sqlite3.Connection:
    database = paths.database_file()
    if not database.exists():
        pytest.skip("the Fylgja database does not exist: is the daemon running?")
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _last_id(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
    return int(row[0])


def _name(row: sqlite3.Row) -> str:
    if row["entity_id"] == "call":
        return str(row["call_event"])
    edge = "start" if row["state"] in {"idle", "locked"} else "end"
    return f"{row['entity_id']}.{edge}"


def _lag(row: sqlite3.Row) -> float:
    observed = datetime.fromisoformat(row["observed_at"])
    received = datetime.fromisoformat(row["received_at"])
    return (received - observed).total_seconds()


def _wait_for(
    connection: sqlite3.Connection, since: int, wanted: list[str], timeout: float
) -> dict[str, sqlite3.Row]:
    """The first row of each wanted event after ``since``, in order, or a failure."""
    found: dict[str, sqlite3.Row] = {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for row in connection.execute(EVENTS_SQL, (since,)).fetchall():
            name = _name(row)
            if name in wanted and name not in found:
                found[name] = row
        if all(name in found for name in wanted):
            return found
        time.sleep(POLL_SECONDS)
    pytest.fail(f"no {[n for n in wanted if n not in found]} within {timeout:.0f} s of the prompt")


def _check_lags(found: dict[str, sqlite3.Row]) -> None:
    for name, row in found.items():
        assert -1.0 <= _lag(row) <= LAG_SECONDS, f"{name} was detected {_lag(row):.1f} s late"


@pytest.fixture(scope="module")
def under_launchd() -> None:
    result = subprocess.run(  # noqa: S603
        ["/bin/launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0 or "state = running" not in result.stdout:
        pytest.skip(f"{LABEL} is not running under launchd (fylgja install launchd --apply)")


@pytest.fixture
def db(under_launchd: None) -> Iterator[sqlite3.Connection]:
    connection = _connect()
    try:
        yield connection
    finally:
        connection.close()


def _prompt(capsys: pytest.CaptureFixture[str], text: str) -> None:
    with capsys.disabled():
        print(f"\n>>> {text}", flush=True)
        input(">>> press Enter, then do it: ")


def test_a_screen_lock_and_unlock_appear_within_15_seconds(
    db: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    since = _last_id(db)
    _prompt(capsys, "lock the screen, wait about 5 s, unlock it (you have 2 minutes)")
    found = _wait_for(db, since, ["lock.start", "lock.end"], ACTION_SECONDS)
    _check_lags(found)
    assert found["lock.start"]["id"] < found["lock.end"]["id"]


def test_an_idle_period_appears_within_15_seconds(
    db: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    since = _last_id(db)
    _prompt(
        capsys,
        f"focus a window, touch nothing for {IDLE_THRESHOLD + 60:.0f} s, then move the mouse "
        f"(the test waits {IDLE_THRESHOLD + 180:.0f} s)",
    )
    found = _wait_for(db, since, ["idle.start", "idle.end"], IDLE_THRESHOLD + 180)
    _check_lags(found)
    started = datetime.fromisoformat(found["idle.start"]["observed_at"])
    ended = datetime.fromisoformat(found["idle.end"]["observed_at"])
    assert (ended - started).total_seconds() >= 0, "idle ended before it started"


def test_a_call_start_and_end_appear_within_15_seconds(
    db: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    since = _last_id(db)
    _prompt(
        capsys,
        "start a call or a recording (microphone or camera), keep it for about 10 s, stop it "
        "(you have 2 minutes)",
    )
    found = _wait_for(db, since, ["call.start", "call.end"], ACTION_SECONDS)
    _check_lags(found)
    data: Any = json.loads(
        db.execute(
            "SELECT payload FROM events WHERE id = ?", (found["call.start"]["id"],)
        ).fetchone()["payload"]
    )
    assert data["data"]["microphone"] or data["data"]["camera"]
