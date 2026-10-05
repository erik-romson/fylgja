"""Opt-in smoke test: a focus change reaches ``/now`` within 1 s (WP1.6 done condition).

Run by hand, with the daemon running and the AeroSpace collector enabled::

    FYLGJA_RUN_LIVE=1 uv run --frozen pytest -m live tests/live/test_aerospace_live.py

The test **moves focus**: it focuses another existing window and then focuses the original window
again. It reads window ids only (never titles), starts nothing and closes nothing. ``FYLGJA_PORT``
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
from collections.abc import Iterator
from typing import Any

import pytest

pytestmark = pytest.mark.live

DEADLINE_SECONDS = 1.0
POLL_SECONDS = 0.05
AEROSPACE = shutil.which("aerospace") or "/opt/homebrew/bin/aerospace"


def _aerospace(*args: str) -> str:
    result = subprocess.run(
        [AEROSPACE, *args], capture_output=True, text=True, check=True, timeout=10
    )
    return result.stdout


def _window_ids(*args: str) -> list[str]:
    data = json.loads(_aerospace("list-windows", *args, "--json", "--format", "%{window-id}"))
    return [str(item["window-id"]) for item in data]


def _now() -> dict[str, Any]:
    port = os.environ.get("FYLGJA_PORT", "7319")
    request = urllib.request.Request(f"http://127.0.0.1:{port}/now")  # noqa: S310
    with urllib.request.urlopen(request, timeout=2) as response:  # noqa: S310
        body: dict[str, Any] = json.load(response)
    return body


def _focused_window_id() -> str | None:
    focused = _now().get("focused")
    if not isinstance(focused, dict):
        return None
    value = focused.get("window_id")
    return None if value is None else str(value)


@pytest.fixture
def daemon() -> None:
    try:
        _now()
    except (urllib.error.URLError, OSError):
        pytest.skip("the Fylgja daemon is not answering on /now")


@pytest.fixture
def two_windows() -> Iterator[tuple[str, str]]:
    if shutil.which("aerospace") is None and not os.path.exists(AEROSPACE):
        pytest.skip("aerospace is not installed")
    current = _window_ids("--focused")
    if not current:
        pytest.skip("no window is focused")
    others = [w for w in _window_ids("--monitor", "all") if w != current[0]]
    if not others:
        pytest.skip("need a second window to move focus to")
    original = current[0]
    try:
        yield original, others[0]
    finally:
        subprocess.run([AEROSPACE, "focus", "--window-id", original], check=False, timeout=10)


def _wait_for_focus(window_id: str) -> float:
    started = time.monotonic()
    while time.monotonic() - started < DEADLINE_SECONDS:
        if _focused_window_id() == window_id:
            return time.monotonic() - started
        time.sleep(POLL_SECONDS)
    pytest.fail(f"/now did not show window {window_id} as focused within {DEADLINE_SECONDS}s")


@pytest.mark.usefixtures("daemon")
def test_a_focus_change_reaches_now_within_one_second(two_windows: tuple[str, str]) -> None:
    original, other = two_windows
    _aerospace("focus", "--window-id", other)
    _wait_for_focus(other)
    _aerospace("focus", "--window-id", original)
    _wait_for_focus(original)
