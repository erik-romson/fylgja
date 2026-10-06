"""Opt-in smoke test: every agent session in ``/now`` has a state and one sentence (WP3.6).

Run by hand, with the daemon running and the agent collectors enabled::

    FYLGJA_RUN_LIVE=1 uv run --frozen pytest -m live tests/live/test_phase3_agents_live.py

The same assertion as ``tests/integration/test_phase3_agents.py``, against the running daemon:
each item of ``sessions[]`` (and each nested child) has a non-empty ``state`` and ``one_liner``
(``PLAN-final-plan.md`` §4.6). The test starts nothing and writes nothing, and it prints only
counts and keys by kind: no titles, no prompts. ``FYLGJA_PORT`` overrides the port (7319).
Kinds without a running session are not an error; the test skips when there is no session at all.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections import Counter
from typing import Any

import pytest

pytestmark = pytest.mark.live

EMPTY_ONE_LINER = "—"


def _now() -> dict[str, Any]:
    port = os.environ.get("FYLGJA_PORT", "7319")
    request = urllib.request.Request(f"http://127.0.0.1:{port}/now")  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            body: dict[str, Any] = json.load(response)
    except (urllib.error.URLError, OSError):
        pytest.skip(f"no Fylgja daemon answers on 127.0.0.1:{port}")
    return body


def _walk(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for item in sessions:
        found.append(item)
        found.extend(_walk(item.get("children") or []))
    return found


def test_every_session_has_a_state_and_a_one_liner() -> None:
    now = _now()
    assert now["schema"] == "fylgja.now/1"
    sessions = _walk(now.get("sessions") or [])
    if not sessions:
        pytest.skip("no agent session is running")
    print("sessions by kind:", dict(Counter(str(s.get("kind")) for s in sessions)))

    missing_state = [s["key"] for s in sessions if not str(s.get("state") or "").strip()]
    missing_line = [
        s["key"]
        for s in sessions
        if not str(s.get("one_liner") or "").strip() or s.get("one_liner") == EMPTY_ONE_LINER
    ]
    assert not missing_state, f"sessions without a state: {missing_state}"
    assert not missing_line, f"sessions without a one-liner: {missing_line}"
