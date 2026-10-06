"""Phase 3 acceptance: every agent has a state and one sentence (WP3.6, ``PLAN.md`` §12).

``tests/phase3_support.py`` runs the real Claude, Codex, OpenCode and norn collectors over their
own sanitized fixtures and reads ``GET /now`` from a daemon fed with what they emitted. The script
``scripts/record_now_fixtures.py`` builds the same payload and writes
``tests/fixtures/now/phase3-agents.json``; this test builds it again, compares, and checks the
Phase 3 rule: each ``sessions[]`` item has a non-empty ``state`` and ``one_liner`` (§4.6), all four
kinds are present, and the ``sdk-py`` session a norn run drives sits under that run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from phase3_support import FIXTURE, KINDS, RUN_SESSION, build_now

from fylgja.api.now_models import Now

EMPTY_ONE_LINER = "—"


@pytest.fixture
async def now() -> dict[str, Any]:
    return await build_now()


def test_the_fixture_is_committed_and_valid() -> None:
    recorded = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert recorded["schema"] == "fylgja.now/1"
    Now.model_validate(recorded)


async def test_the_payload_equals_the_committed_fixture(now: dict[str, Any]) -> None:
    recorded = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert now == recorded, "rerun: uv run --frozen python scripts/record_now_fixtures.py"


async def test_the_payload_is_the_same_in_another_directory(tmp_path: Path) -> None:
    assert await build_now(tmp_path) == await build_now(tmp_path / "again")


def _walk(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for item in sessions:
        found.append(item)
        found.extend(_walk(item.get("children") or []))
    return found


async def test_every_session_has_a_state_and_a_one_liner(now: dict[str, Any]) -> None:
    sessions = now["sessions"]
    assert sessions
    for item in _walk(sessions):
        key = item["key"]
        state, one_liner = item.get("state"), item.get("one_liner")
        assert isinstance(state, str), key
        assert state.strip(), key
        assert isinstance(one_liner, str), key
        assert one_liner.strip(), key
        assert one_liner != EMPTY_ONE_LINER, key
        assert item.get("one_liner_from"), key


async def test_all_four_kinds_are_present(now: dict[str, Any]) -> None:
    kinds = {item["kind"] for item in now["sessions"]}
    assert kinds == set(KINDS)
    assert "sessions" not in now["degraded"]
    assert now["degraded"] == []


async def test_the_states_the_collectors_report(now: dict[str, Any]) -> None:
    by_key = {item["key"]: item for item in now["sessions"]}
    by_kind: dict[str, set[str]] = {}
    for item in by_key.values():
        by_kind.setdefault(item["kind"], set()).add(item["state"])
    assert by_kind["claude"] == {"working", "waiting", "idle"}
    assert by_kind["codex"] == {"idle", "busy"}
    assert by_kind["opencode"] == {"idle", "busy"}
    assert by_kind["norn"] == {"waiting_input"}


async def test_the_norn_run_asks_for_input_and_carries_its_sdk_session(
    now: dict[str, Any],
) -> None:
    runs = [item for item in now["sessions"] if item["kind"] == "norn"]
    assert len(runs) == 1
    run = runs[0]
    assert run["state"] == "waiting_input"
    assert run["one_liner"].startswith("Waiting for input")
    assert [child["key"] for child in run["children"]] == [f"claude:{RUN_SESSION}"]
    child = run["children"][0]
    assert child["parent_key"] == run["key"]
    assert child["kind"] == "claude"
    assert child["state"] == "working"
    # the nested session is the run's, not an independent top-level session
    assert f"claude:{RUN_SESSION}" not in {item["key"] for item in now["sessions"]}
