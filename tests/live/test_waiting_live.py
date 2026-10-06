"""Opt-in live check of waiting-on-me (WP6.1): the 2 s promises, against the running daemon.

Run by hand, with the daemon running and the agent collectors enabled, and do the thing each test
waits for::

    FYLGJA_RUN_LIVE=1 uv run --frozen pytest -m live tests/live/test_waiting_live.py

* ``test_a_claude_permission_prompt_appears_within_2s``: start a Claude session and let it ask for a
  permission (a tool the session does not allow yet).
* ``test_a_norn_run_waiting_for_input_appears_within_2s``: let a norn run reach a wait for input.
* ``test_a_finished_turn_leaves_within_2s_of_its_window_gaining_focus``: let a Claude turn finish
  in a window you are not looking at, then focus that window.

Each test polls ``/now`` every 0.25 s for ``FYLGJA_LIVE_WAIT_S`` seconds (120 by default) and skips
when what it waits for does not happen. It asserts only on a transition it watched itself: the lag
is the wall-clock time between the event the daemon stamped (``since`` of the entry, ``since`` of
the focus) and the poll that first showed the change. The test starts nothing and writes nothing,
and prints no titles, prompts or reasons: only kinds, keys by kind and seconds. ``FYLGJA_PORT``
overrides the port (7319).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

pytestmark = pytest.mark.live

LIMIT_SECONDS = 2.0
POLL_SECONDS = 0.25


def _wait_seconds() -> float:
    return float(os.environ.get("FYLGJA_LIVE_WAIT_S", "120"))


def _now() -> dict[str, Any]:
    port = os.environ.get("FYLGJA_PORT", "7319")
    request = urllib.request.Request(f"http://127.0.0.1:{port}/now")  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            body: dict[str, Any] = json.load(response)
    except (urllib.error.URLError, OSError):
        pytest.skip(f"no Fylgja daemon answers on 127.0.0.1:{port}")
    return body


def _moment(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _entries(now: dict[str, Any], kind: str, prefix: str = "") -> list[dict[str, Any]]:
    return [
        e
        for e in now.get("waiting") or []
        if e.get("kind") == kind and str(e.get("target", "")).startswith(prefix)
    ]


def _lag(since: object) -> float | None:
    moment = _moment(since)
    return None if moment is None else (datetime.now(UTC) - moment).total_seconds()


def _watch_appear(kind: str, prefix: str, what: str) -> None:
    """Wait for an entry of ``kind`` to appear after a poll that did not show one."""
    deadline = time.monotonic() + _wait_seconds()
    known = {e["target"] for e in _entries(_now(), kind, prefix)}
    while time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        now = _now()
        fresh = [e for e in _entries(now, kind, prefix) if e["target"] not in known]
        if not fresh:
            known &= {e["target"] for e in _entries(now, kind, prefix)}
            continue
        lag = _lag(fresh[0].get("since"))
        print(f"{what}: appeared in waiting[] as {kind}, lag {lag}")
        assert lag is not None, "the entry carries no since"
        assert lag <= LIMIT_SECONDS, f"{what} took {lag:.2f}s to reach waiting[]"
        return
    pytest.skip(f"{what} did not happen within {_wait_seconds():.0f}s")


def test_a_claude_permission_prompt_appears_within_2s() -> None:
    _watch_appear("permission", "claude:", "a Claude permission prompt")


def test_a_norn_run_waiting_for_input_appears_within_2s() -> None:
    _watch_appear("input", "norn:", "a norn run waiting for input")


def _until(poll: Callable[[dict[str, Any]], bool], seconds: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        now = _now()
        if poll(now):
            return now
        time.sleep(POLL_SECONDS)
    return None


def test_a_finished_turn_leaves_within_2s_of_its_window_gaining_focus() -> None:
    waiting = _until(lambda now: bool(_entries(now, "turn_done")), _wait_seconds())
    if waiting is None:
        pytest.skip(f"no finished turn waited within {_wait_seconds():.0f}s")
    targets = {e["target"] for e in _entries(waiting, "turn_done")}
    print(f"finished turns waiting: {len(targets)}")

    gone = _until(
        lambda now: not targets & {e["target"] for e in _entries(now, "turn_done")}, _wait_seconds()
    )
    if gone is None:
        pytest.skip(f"no waiting turn was looked at within {_wait_seconds():.0f}s")
    focused = gone.get("focused") or {}
    lag = _lag(focused.get("since"))
    print(f"the finished turn left waiting[], lag since focus {lag}")
    hosted = [
        s
        for s in gone.get("sessions") or []
        if s.get("key") in targets
        and (s.get("host") or {}).get("window_id") == focused.get("window_id")
    ]
    if not hosted or lag is None:
        pytest.skip("the entry left, but not because its window gained focus")
    assert lag <= LIMIT_SECONDS, f"the turn took {lag:.2f}s to leave waiting[]"
