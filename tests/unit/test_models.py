from __future__ import annotations

import json
import subprocess
import sys
import typing
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from fylgja import models
from fylgja.models import (
    RECORD_ADAPTER,
    BuildEvent,
    BuildPayload,
    BuildSnapshot,
    BuildWorkflow,
    ContextItemEvent,
    ContextItemPayload,
    ContextItemSnapshot,
    EntityKind,
    Event,
    FocusEvent,
    FocusPayload,
    FocusSnapshot,
    HealthReport,
    HealthState,
    HookEvent,
    HookEventPayload,
    HookEventSnapshot,
    PresenceEvent,
    PresencePayload,
    PresenceSnapshot,
    ProjectEvent,
    ProjectPayload,
    ProjectSnapshot,
    SessionActivity,
    SessionActivityEvent,
    SessionActivitySnapshot,
    SessionEvent,
    SessionPayload,
    SessionSnapshot,
    Snapshot,
    SnapshotItem,
    Stage,
    TodoEvent,
    TodoPayload,
    TodoSnapshot,
    UsageEvent,
    UsagePayload,
    UsageSnapshot,
    WindowEvent,
    WindowPayload,
    WindowSnapshot,
    WorkspaceEvent,
    WorkspacePayload,
    WorkspaceSnapshot,
    compute_dedup_key,
    parse_record,
)

T0 = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)

# (event class, snapshot class, payload) for every kind.
CASES: list[tuple[type[Any], type[Any], BaseModel]] = [
    (
        WindowEvent,
        WindowSnapshot,
        WindowPayload(workspace="3", app_bundle_id="com.example.app", app_pid=42, title="t"),
    ),
    (WorkspaceEvent, WorkspaceSnapshot, WorkspacePayload(name="3", focused=True)),
    (FocusEvent, FocusSnapshot, FocusPayload(window_id="7", workspace="3")),
    (
        SessionEvent,
        SessionSnapshot,
        SessionPayload(kind="claude", native_id="abc", pid=9, started_at=T0),
    ),
    (
        SessionActivityEvent,
        SessionActivitySnapshot,
        SessionActivity(
            state="waiting",
            current_tool="Bash",
            stage=Stage(name="build", index=1, total=3),
        ),
    ),
    (TodoEvent, TodoSnapshot, TodoPayload(text="do it", status="next", created_at=T0)),
    (
        ContextItemEvent,
        ContextItemSnapshot,
        ContextItemPayload(context_kind="tab", data={"n": 1, "xs": [1, "a", None]}),
    ),
    (PresenceEvent, PresenceSnapshot, PresencePayload(state="idle")),
    (ProjectEvent, ProjectSnapshot, ProjectPayload(path="/p", dirty=True, ahead=1)),
    (
        BuildEvent,
        BuildSnapshot,
        BuildPayload(state="pass", workflows=(BuildWorkflow(workflow="ci", run_id=5),)),
    ),
    (UsageEvent, UsageSnapshot, UsagePayload(provider="claude", used_pct=12.5, resets_at=T0)),
    (
        HookEvent,
        HookEventSnapshot,
        HookEventPayload(
            session_id="abc",
            event="PostToolUse",
            tool_name="Edit",
            edited_paths=("/p/a.py",),
            ids={"CMUX_SURFACE_ID": "s1"},
        ),
    ),
]


def _event(cls: type[Any], payload: BaseModel, **kw: Any) -> Any:
    return cls(source="s", entity_id="e1", observed_at=T0, payload=payload, **kw)


def _snapshot(cls: type[Any], payload: BaseModel, **kw: Any) -> Any:
    return cls(
        source="s",
        entity_id="all",
        observed_at=T0,
        items=(SnapshotItem(entity_id="e1", payload=payload),),
        complete=True,
        **kw,
    )


def test_cases_cover_every_kind() -> None:
    kinds = {typing.get_args(c[0].model_fields["entity_kind"].annotation)[0] for c in CASES}
    assert kinds == set(EntityKind)


@pytest.mark.parametrize(("event_cls", "snap_cls", "payload"), CASES)
def test_event_round_trip(event_cls: type[Any], snap_cls: type[Any], payload: BaseModel) -> None:
    record = _event(event_cls, payload)
    text = RECORD_ADAPTER.dump_json(record).decode()
    again = RECORD_ADAPTER.validate_json(text)
    assert type(again) is event_cls
    assert again == record
    assert again.dedup_key == record.dedup_key
    # Direct model round trip as well.
    assert event_cls.model_validate_json(record.model_dump_json()) == record


@pytest.mark.parametrize(("event_cls", "snap_cls", "payload"), CASES)
def test_snapshot_round_trip(event_cls: type[Any], snap_cls: type[Any], payload: BaseModel) -> None:
    record = _snapshot(snap_cls, payload)
    again = parse_record(RECORD_ADAPTER.dump_json(record))
    assert type(again) is snap_cls
    assert again == record
    assert again.complete is True
    assert again.items[0].payload == payload


@pytest.mark.parametrize(("event_cls", "snap_cls", "payload"), CASES)
def test_union_dispatch_on_entity_kind(
    event_cls: type[Any], snap_cls: type[Any], payload: BaseModel
) -> None:
    event = json.loads(_event(event_cls, payload).model_dump_json())
    snap = json.loads(_snapshot(snap_cls, payload).model_dump_json())
    assert type(parse_record(event)) is event_cls
    assert type(parse_record(snap)) is snap_cls
    assert event["entity_kind"] == snap["entity_kind"]


def test_unknown_entity_kind_is_rejected() -> None:
    record = json.loads(_event(WindowEvent, WindowPayload()).model_dump_json())
    record["entity_kind"] = "teapot"
    with pytest.raises(ValidationError):
        parse_record(record)
    record["entity_kind"] = "window"
    record["record_type"] = "mystery"
    with pytest.raises(ValidationError):
        parse_record(record)


def test_payload_must_match_kind() -> None:
    record = json.loads(_event(WindowEvent, WindowPayload()).model_dump_json())
    record["entity_kind"] = "todo"
    with pytest.raises(ValidationError):
        parse_record(record)


def test_unknown_payload_field_is_rejected() -> None:
    record = json.loads(_event(WindowEvent, WindowPayload()).model_dump_json())
    record["payload"]["surprise"] = 1
    with pytest.raises(ValidationError):
        parse_record(record)


def test_every_kind_has_event_and_snapshot_in_unions() -> None:
    def members(alias: Any) -> list[Any]:
        return list(typing.get_args(typing.get_args(alias)[0]))

    for alias in (models.EventRecord, models.SnapshotRecord):
        kinds = {
            typing.get_args(m.model_fields["entity_kind"].annotation)[0] for m in members(alias)
        }
        assert kinds == set(EntityKind)


@pytest.mark.parametrize("cls", [WindowEvent])
def test_naive_datetime_is_rejected(cls: type[Any]) -> None:
    with pytest.raises(ValidationError):
        cls(
            source="s",
            entity_id="e",
            observed_at=datetime(2026, 10, 5, 12, 0, 0),  # noqa: DTZ001
            payload=WindowPayload(),
        )
    naive_json = json.loads(_event(WindowEvent, WindowPayload()).model_dump_json())
    naive_json["observed_at"] = "2026-10-05T12:00:00"
    with pytest.raises(ValidationError):
        parse_record(naive_json)


def test_naive_datetime_rejected_in_snapshot_payload_and_health() -> None:
    with pytest.raises(ValidationError):
        SessionPayload(kind="claude", native_id="a", started_at=datetime(2026, 1, 1))  # noqa: DTZ001
    with pytest.raises(ValidationError):
        HealthReport(state=HealthState.OK, checked_at=datetime(2026, 1, 1))  # noqa: DTZ001
    with pytest.raises(ValueError, match="timezone-aware"):
        compute_dedup_key("s", "window", "e", datetime(2026, 1, 1), {})  # noqa: DTZ001


def test_records_are_frozen() -> None:
    record = _event(WindowEvent, WindowPayload())
    with pytest.raises(ValidationError):
        record.source = "other"
    with pytest.raises(ValidationError):
        record.payload.title = "x"


def test_empty_identifiers_are_rejected() -> None:
    with pytest.raises(ValidationError):
        WindowEvent(source="", entity_id="e", observed_at=T0, payload=WindowPayload())
    with pytest.raises(ValidationError):
        WindowEvent(source="s", entity_id="", observed_at=T0, payload=WindowPayload())


def test_dedup_key_is_computed_and_supplied_key_wins() -> None:
    record = _event(WindowEvent, WindowPayload(title="a"))
    assert len(record.dedup_key) == 64
    assert "dedup_key" in record.model_fields_set
    supplied = _event(WindowEvent, WindowPayload(title="a"), dedup_key="envelope-1")
    assert supplied.dedup_key == "envelope-1"
    assert parse_record(supplied.model_dump_json()).dedup_key == "envelope-1"


def test_dedup_key_depends_on_every_input() -> None:
    base = _event(WindowEvent, WindowPayload(title="a"))
    same = _event(WindowEvent, WindowPayload(title="a"))
    assert base.dedup_key == same.dedup_key
    variants = [
        WindowEvent(source="t", entity_id="e1", observed_at=T0, payload=WindowPayload(title="a")),
        WindowEvent(source="s", entity_id="e2", observed_at=T0, payload=WindowPayload(title="a")),
        WindowEvent(
            source="s",
            entity_id="e1",
            observed_at=T0 + timedelta(seconds=1),
            payload=WindowPayload(title="a"),
        ),
        WindowEvent(source="s", entity_id="e1", observed_at=T0, payload=WindowPayload(title="b")),
        FocusEvent(source="s", entity_id="e1", observed_at=T0, payload=FocusPayload()),
    ]
    keys = {v.dedup_key for v in variants} | {base.dedup_key}
    assert len(keys) == len(variants) + 1


def test_dedup_key_is_timezone_offset_independent() -> None:
    plus2 = timezone(timedelta(hours=2))
    a = WindowEvent(source="s", entity_id="e", observed_at=T0, payload=WindowPayload())
    b = WindowEvent(
        source="s", entity_id="e", observed_at=T0.astimezone(plus2), payload=WindowPayload()
    )
    assert a.dedup_key == b.dedup_key


def test_snapshot_dedup_key_covers_items_and_complete() -> None:
    payload = WindowPayload(title="a")
    full = _snapshot(WindowSnapshot, payload)
    partial = WindowSnapshot(
        source="s",
        entity_id="all",
        observed_at=T0,
        items=full.items,
        complete=False,
    )
    empty = WindowSnapshot(source="s", entity_id="all", observed_at=T0, complete=True)
    assert len({full.dedup_key, partial.dedup_key, empty.dedup_key}) == 3
    assert full.dedup_key == _snapshot(WindowSnapshot, payload).dedup_key


_KEY_PROBE = """
import json
from datetime import UTC, datetime
from fylgja.models import (
    SessionActivity, SessionActivityEvent, Stage, WindowEvent, WindowPayload,
    WindowSnapshot, SnapshotItem,
)

t = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)
a = WindowEvent(
    source="s", entity_id="e1", observed_at=t, payload=WindowPayload(title="caf\\u00e9")
)
b = SessionActivityEvent(
    source="s", entity_id="k", observed_at=t,
    payload=SessionActivity(state="working", stage=Stage(name="x", index=1, total=2)),
)
c = WindowSnapshot(
    source="s", entity_id="all", observed_at=t, complete=True,
    items=(SnapshotItem(entity_id="e1", payload=WindowPayload(app_pid=3)),),
)
print(json.dumps([a.dedup_key, b.dedup_key, c.dedup_key]))
"""


def _keys_in_subprocess(env_extra: dict[str, str]) -> list[str]:
    import os

    env = {**os.environ, **env_extra}
    out = subprocess.run(
        [sys.executable, "-c", _KEY_PROBE],
        capture_output=True,
        text=True,
        check=True,
        timeout=25,
        env=env,
    )
    keys: list[str] = json.loads(out.stdout)
    return keys


def test_dedup_key_is_stable_across_processes() -> None:
    first = _keys_in_subprocess({"PYTHONHASHSEED": "1"})
    second = _keys_in_subprocess({"PYTHONHASHSEED": "12345"})
    assert first == second
    assert len(set(first)) == 3

    a = WindowEvent(source="s", entity_id="e1", observed_at=T0, payload=WindowPayload(title="café"))
    assert a.dedup_key == first[0]


def test_session_activity_fields_all_optional() -> None:
    empty = SessionActivity()
    assert empty.model_dump() == {
        "state": None,
        "waiting_reason": None,
        "current_tool": None,
        "current_target": None,
        "open_task": None,
        "stage": None,
        "last_prompt": None,
        "last_reply": None,
        "title": None,
        "branch": None,
        "facts": None,
        "tokens": None,
        "context_pct": None,
        "cost_usd": None,
        "report": None,
        "recap": None,
    }
    assert set(Stage.model_fields) == {"name", "index", "total"}
    full = SessionActivity(
        state="waiting",
        waiting_reason="permission",
        current_tool="Edit",
        current_target="/a/b.py",
        open_task="t",
        stage=Stage(name="s", index=2, total=5),
        last_prompt="p",
        last_reply="r",
        title="ti",
        branch="main",
        facts={"turns": 2, "prompts": ["p"], "tasks": [{"status": "pending", "content": "c"}]},
    )
    assert SessionActivity.model_validate_json(full.model_dump_json()) == full


def test_health_report() -> None:
    report = HealthReport(
        state=HealthState.DEGRADED, detail="slow", error_kind="timeout", checked_at=T0
    )
    assert HealthReport.model_validate_json(report.model_dump_json()) == report
    assert {s.value for s in HealthState} == {"starting", "ok", "degraded", "failed", "disabled"}
    minimal = HealthReport(state=HealthState.OK, checked_at=T0)
    assert minimal.detail is None
    assert minimal.error_kind is None
    with pytest.raises(ValidationError):
        HealthReport(state="exploded", checked_at=T0)  # type: ignore[arg-type]


def test_event_and_snapshot_are_exported_generics() -> None:
    assert issubclass(WindowEvent, Event)
    assert issubclass(WindowSnapshot, Snapshot)
