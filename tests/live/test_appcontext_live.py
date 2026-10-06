"""Opt-in smoke test of application context (WP5.3). A human runs it.

Setup: the daemon runs under launchd (``fylgja install launchd --apply``) with the ``appcontext``
collector enabled, cmux is running, and ``fylgja doctor`` shows ``source:appcontext`` as ``ok``.

Run it with ``-s`` so the prompt shows::

    FYLGJA_RUN_LIVE=1 uv run --frozen pytest -m live -s tests/live/test_appcontext_live.py

The test asks you to focus a cmux window (click it, or ``aerospace focus`` from your own shell),
then reads ``/now`` of the running daemon (a ``GET``, nothing else). The requirement is that
``focused.context[]`` holds the cmux workspace and tab item (kind ``cmux_tab``, with the workspace
and surface ids and the working directory) within 1 s of the focus: the item's ``observed_at`` minus
``focused.since``. The test focuses nothing, runs no ``osascript`` and writes no file.

By hand, once, if the answer is ``unknown`` in the revalidation log: switch the focus between two
cmux workspaces and check that ``confidence`` says what you see (``medium`` when the title names the
workspace, ``low`` for a guess).
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import pytest

from fylgja.cli import client

pytestmark = pytest.mark.live

CMUX_BUNDLE = "com.cmuxterm.app"
WITHIN_SECONDS = 1.0
POLL_SECONDS = 0.1
ACTION_SECONDS = 60.0


def _now() -> dict[str, Any]:
    try:
        body: dict[str, Any] = client.get_json("/now")
    except client.DaemonDown:
        pytest.skip("the Fylgja daemon is not running")
    return body


def _since(focused: dict[str, Any]) -> datetime | None:
    stamp = focused.get("since")
    return datetime.fromisoformat(stamp) if isinstance(stamp, str) else None


def test_the_cmux_workspace_and_tab_context_follows_the_focus() -> None:
    names = {c["name"] for c in _now().get("collectors", [])}
    if "appcontext" not in names:
        pytest.skip("the appcontext collector is not registered")
    started = _since(_now().get("focused") or {})
    print("\nFocus a cmux window now (you have 60 s)...")  # noqa: T201
    deadline = time.monotonic() + ACTION_SECONDS
    while time.monotonic() < deadline:
        focused = _now().get("focused") or {}
        since = _since(focused)
        if focused.get("bundle_id") == CMUX_BUNDLE and since is not None and since != started:
            items = [c for c in focused.get("context", []) if c.get("kind") == "cmux_tab"]
            if items:
                item = items[0]
                observed = datetime.fromisoformat(item["observed_at"])
                lag = (observed - since).total_seconds()
                assert lag <= WITHIN_SECONDS, f"the cmux context came {lag:.2f} s after the focus"
                assert item["data"]["cmux_workspace"]
                assert item["data"]["cmux_surface"]
                assert item["confidence"] in {"low", "medium", "high"}
                return
        time.sleep(POLL_SECONDS)
    pytest.fail("no cmux workspace and tab context in /now within 60 s of focusing a cmux window")
