"""Opt-in smoke test of the focus actions on the real machine (WP3.5). A human runs it.

``FYLGJA_RUN_LIVE=1 uv run --frozen pytest -m live tests/live/test_focus_live.py`` with a daemon
running on 127.0.0.1:7319, the API secret installed (``fylgja install secret``) and at least one
cmux-hosted and one JetBrains-hosted session. It **moves focus**: the windows of those sessions come
to the front. iTerm2 is not required. The cmux case is the by-hand confirmation of decision 0025's
AppleScript route: it expects tab level and tells you if the daemon fell back to window level.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

import pytest

from fylgja import paths

pytestmark = pytest.mark.live

BASE = "http://127.0.0.1:7319"


def _get(path: str) -> Any:
    with urllib.request.urlopen(BASE + path, timeout=10) as response:  # noqa: S310
        return json.load(response)


def _post(path: str) -> dict[str, str]:
    secret = paths.api_secret_file().read_text().strip()
    request = urllib.request.Request(  # noqa: S310
        BASE + path,
        data=b"{}",
        method="POST",
        headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
        body: dict[str, str] = json.load(response)
        return body


def _session_of(kind: str) -> str:
    for session in _get("/sessions"):
        host = session.get("host") or {}
        if host.get("kind") == kind and host.get("window_id"):
            return str(session["key"])
    pytest.skip(f"no live session hosted by {kind}")


def test_cmux_session_is_focused_at_tab_level() -> None:
    result = _post(f"/focus/session/{_session_of('cmux')}")
    assert result["focused"] == "tab", f"fell back to window level: {result['via']}"


def test_jetbrains_session_is_focused_at_window_level() -> None:
    result = _post(f"/focus/session/{_session_of('jetbrains')}")
    assert result["focused"] == "window"
