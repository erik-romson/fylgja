"""The ``fylgja.now/1`` guard: the schema does not change unless its version does (§4.2).

``docs/api/now.schema.json`` is generated from the models by ``scripts/gen_now_schema.py`` and
committed. This test generates the schema in memory and compares; it never writes the file.

* The models and the committed file differ while the version string is the same: the contract
  changed without a version bump. Fail.
* They differ and the version was bumped: the committed file is stale. Fail with "regenerate".

It also pins the serialization rule the schema describes: an optional field is absent, never
``null``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from fylgja.api.now_models import (
    NOW_SCHEMA,
    CollectorInfo,
    Facts,
    Now,
    OneLinerRule,
    Session,
    SessionState,
    Workspace,
    dump_json,
    now_json_schema,
)
from fylgja.core.one_liner import RULES
from fylgja.models import HealthState

REPO = Path(__file__).resolve().parents[2]
COMMITTED = REPO / "docs" / "api" / "now.schema.json"
SCRIPT = REPO / "scripts" / "gen_now_schema.py"
T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def version_of(schema: dict[str, Any]) -> str:
    """The contract version a schema document describes (``properties.schema.const``)."""
    return str(schema["properties"]["schema"]["const"])


def guard(generated: dict[str, Any], committed: dict[str, Any]) -> str | None:
    """``None`` when the committed schema is current, else why the guard fails."""
    if generated == committed:
        return None
    if version_of(generated) == version_of(committed):
        return (
            f"the {version_of(generated)} contract changed without a version bump: bump "
            "NOW_SCHEMA in fylgja/api/now_models.py, record a decision and regenerate "
            "docs/api/now.schema.json with scripts/gen_now_schema.py"
        )
    return (
        f"the version moved from {version_of(committed)} to {version_of(generated)}: regenerate "
        "docs/api/now.schema.json with scripts/gen_now_schema.py"
    )


def committed() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(COMMITTED.read_text(encoding="utf-8"))
    return data


# --- the guard -----------------------------------------------------------------------------


def test_schema_unchanged_unless_version_bumped() -> None:
    generated = now_json_schema()
    if generated != committed():
        assert version_of(generated) != version_of(committed()), guard(generated, committed())


def test_committed_schema_is_current() -> None:
    assert guard(now_json_schema(), committed()) is None


def test_the_guard_fails_a_change_without_a_bump_and_asks_to_regenerate_after_one() -> None:
    base = now_json_schema()
    changed = json.loads(json.dumps(base))
    changed["$defs"]["Session"]["properties"]["extra"] = {"type": "string"}
    message = guard(changed, base)
    assert message is not None
    assert "without a version bump" in message
    bumped = json.loads(json.dumps(changed))
    bumped["properties"]["schema"]["const"] = "fylgja.now/2"
    message = guard(bumped, base)
    assert message is not None
    assert "regenerate" in message
    assert guard(base, json.loads(json.dumps(base))) is None


def test_generating_the_schema_twice_gives_the_same_document() -> None:
    assert now_json_schema() == now_json_schema()


def test_the_script_check_mode_agrees_and_writes_nothing() -> None:
    before = COMMITTED.stat().st_mtime_ns
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert COMMITTED.stat().st_mtime_ns == before


def test_the_script_writes_the_committed_text(isolated_env: Path) -> None:
    target = isolated_env / "now.schema.json"
    subprocess.run(
        [sys.executable, str(SCRIPT), "--output", str(target)],
        capture_output=True,
        timeout=60,
        check=True,
    )
    assert target.read_text(encoding="utf-8") == COMMITTED.read_text(encoding="utf-8")
    stale = subprocess.run(
        [sys.executable, str(SCRIPT), "--check", "--output", str(isolated_env / "missing.json")],
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert stale.returncode == 1


# --- what the schema says ------------------------------------------------------------------


def test_the_version_string_is_one_value_everywhere() -> None:
    schema = now_json_schema()
    assert NOW_SCHEMA == "fylgja.now/1"
    assert version_of(schema) == NOW_SCHEMA
    assert schema["$id"] == NOW_SCHEMA
    assert Now(generated_at=T0, collectors=[], degraded=[]).schema_ == NOW_SCHEMA


def test_required_fields_are_exactly_those_always_written() -> None:
    schema = now_json_schema()
    assert schema["required"] == ["schema", "generated_at", "collectors", "degraded"]
    defs = schema["$defs"]
    assert defs["Session"]["required"] == ["key", "kind", "native_id"]
    assert defs["Workspace"]["required"] == [
        "name",
        "label",
        "label_source",
        "one_liner",
        "focused",
        "windows",
        "projects",
        "sessions",
    ]
    assert "required" not in defs["Facts"]


def _types(node: Any) -> list[Any]:
    found: list[Any] = []
    if isinstance(node, dict):
        if "type" in node:
            found.append(node["type"])
        for value in node.values():
            found.extend(_types(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_types(value))
    return found


def test_the_schema_never_allows_null() -> None:
    schema = now_json_schema()
    assert "null" not in json.dumps(_types(schema))
    assert '"default": null' not in json.dumps(schema)


def test_every_later_section_is_in_the_contract() -> None:
    defs = now_json_schema()["$defs"]
    top = now_json_schema()["properties"]
    assert {"usage", "waiting", "next", "next_list_id", "presence", "focused"} <= set(top)
    assert {"context"} <= set(defs["Focused"]["properties"])
    assert {"context"} <= set(defs["Window"]["properties"])
    assert {"todos", "story", "one_liner", "label", "label_source", "inferred_label"} <= set(
        defs["Workspace"]["properties"]
    )
    session_fields = {
        "tokens",
        "cost_usd",
        "context_pct",
        "pipeline",
        "stage",
        "todos",
        "story",
        "children",
        "facts",
        "lane_key",
        "one_liner",
        "one_liner_from",
        "host",
    }
    assert session_fields <= set(defs["Session"]["properties"])
    assert {"pr", "build", "dirty", "ahead", "behind", "last_commit_at"} <= set(
        defs["Project"]["properties"]
    )
    for section in ("Build", "Tokens", "Usage"):
        assert {"source", "observed_at", "freshness", "error_kind"} <= set(
            defs[section]["properties"]
        )
    assert set(defs["Usage"]["properties"]) >= {"providers", "cost"}
    assert set(defs["UsageProvider"]["properties"]) >= {
        "provider",
        "window",
        "used_pct",
        "resets_at",
    }
    assert set(defs["Waiting"]["properties"]) >= {"kind", "target", "reason", "since"}
    assert set(defs["NextItem"]["properties"]) >= {
        "suggestion_id",
        "kind",
        "target_ref",
        "score",
        "reason",
    }
    assert set(defs["Host"]["properties"]) >= {
        "kind",
        "ref",
        "window_id",
        "workspace",
        "cmux_workspace",
        "cmux_surface",
        "via",
        "confidence",
    }


def test_the_vocabularies() -> None:
    assert {state.value for state in SessionState} >= {
        "idle",
        "busy",
        "waiting",
        "working",
        "running_tool",
        "waiting_permission",
        "waiting_input",
        "turn_done",
        "seen",
        "error",
        "ended",
        "running",
        "stale",
        "passed",
        "failed",
        "cancelled",
        "paused",
    }
    assert tuple(rule.value for rule in OneLinerRule) == RULES
    defs = now_json_schema()["$defs"]
    assert set(defs["Freshness"]["enum"]) == {"fresh", "stale", "unavailable"}
    assert set(defs["LabelSource"]["enum"]) == {"manual", "inferred", "none"}


# --- the serialization rule ----------------------------------------------------------------


def test_dumping_drops_absent_values_and_keeps_falsy_ones() -> None:
    now = Now(
        generated_at=T0,
        workspaces=[
            Workspace(
                name="1",
                label="—",
                label_source="none",
                one_liner="—",
                windows=[],
                projects=[],
                sessions=[],
            )
        ],
        sessions=[Session(key="claude:a", kind="claude", native_id="a", facts=Facts(turns=0))],
        collectors=[CollectorInfo(name="wm", origin="built-in", state=HealthState.OK)],
        degraded=[],
    )
    data = json.loads(dump_json(now))
    assert data == {
        "schema": NOW_SCHEMA,
        "generated_at": "2026-10-05T12:00:00Z",
        "workspaces": [
            {
                "name": "1",
                "label": "—",
                "label_source": "none",
                "one_liner": "—",
                "focused": False,
                "windows": [],
                "projects": [],
                "sessions": [],
            }
        ],
        "sessions": [
            {"key": "claude:a", "kind": "claude", "native_id": "a", "facts": {"turns": 0}}
        ],
        "collectors": [{"name": "wm", "origin": "built-in", "state": "ok", "restarts": 0}],
        "degraded": [],
    }
    assert Now.model_validate(data) == now


def test_dumping_lists_including_empty_ones() -> None:
    assert dump_json([], kind=Session) == b"[]"
    item = Session(key="k:1", kind="k", native_id="1")
    assert json.loads(dump_json([item])) == [{"key": "k:1", "kind": "k", "native_id": "1"}]
    assert json.loads(dump_json([item], kind=Session)) == [
        {"key": "k:1", "kind": "k", "native_id": "1"}
    ]


def test_models_are_closed() -> None:
    with pytest.raises(ValueError, match="extra"):
        Session.model_validate({"key": "k:1", "kind": "k", "native_id": "1", "surprise": 1})
