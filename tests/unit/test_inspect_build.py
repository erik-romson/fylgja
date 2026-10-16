"""The builders of ``GET /inspect/collectors`` (``docs/api/inspect.md``, ``docs/decisions/0048``).

* :func:`redact_settings`: a secret-named key, a ``[privacy] redact_patterns`` match under a neutral
  key (the ``sk-`` canary), the home prefix, a nested list and table, untouched numbers and
  booleans, and an unset value left out;
* :func:`build_collectors` over hand-built registry items and states: a runnable built-in with its
  per-kind counts, a disabled external that was never loaded, a rejected duplicate, a collector in
  backoff, the ``[collectors.<name>]`` tables with nothing installed, and the AppleScript switches;
* the serialization rule: no ``null`` in the JSON.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from api_support import HOME, Dist, EntryPoint, config, nulls
from pydantic import BaseModel

from fylgja.api.inspect import (
    SECRET_KEY,
    build_collectors,
    build_explain,
    build_pipeline,
    redact_settings,
)
from fylgja.api.inspect_models import (
    INSPECT_COLLECTORS_SCHEMA,
    INSPECT_PIPELINE_SCHEMA,
    InspectCollectors,
)
from fylgja.api.now_models import LabelSource, NextItem, Now, Waiting, Workspace, dump_json
from fylgja.collectors.appcontext.switches import SWITCHES, SwitchRecord
from fylgja.collectors.base import COLLECTOR_API_MAJOR, Capability, CollectorPlugin
from fylgja.collectors.registry import DiscoveredEntry, RegisteredCollector
from fylgja.collectors.supervisor import CollectorHealth
from fylgja.config import Config
from fylgja.core.daemon import CollectorState
from fylgja.core.ingest import PluginStats, Rejection
from fylgja.core.labels import InferredLabel
from fylgja.core.live_view import Collection
from fylgja.core.notify import NotifyStats
from fylgja.core.ranking import SCORES
from fylgja.core.resolver import Placement, ProjectPlacement, Resolution
from fylgja.models import EntityKind, HealthReport, HealthState
from fylgja.privacy import REDACTED, PrivacyFilter

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
CANARY = "sk-" + "Q7" * 10
"""Matches the default ``[privacy] redact_patterns`` entry ``sk-[A-Za-z0-9_-]{16,}``."""


def _privacy() -> PrivacyFilter:
    return PrivacyFilter.from_config(config().privacy)


# --------------------------------------------------------------------------------------------
# redact_settings
# --------------------------------------------------------------------------------------------


class Part(BaseModel):
    cookie: str = "c-value-1"
    label: str = "plain"


class Secrets(BaseModel):
    api_token: str = "t-value-1"
    password: str = "p-value-1"
    keys: list[str] = ["k-value-1"]
    note: str = f"uses {CANARY} here"
    root: Path = Path(HOME) / "projects"
    home: str = HOME
    sibling: str = f"{HOME}s/x"
    roots: list[str] = [f"{HOME}/alpha", "/work/beta/main"]
    groups: dict[str, str] = {f"{HOME}/alpha": "alpha/main"}
    parts: list[Part] = [Part()]
    max_depth: int = 3
    history: bool = False
    ratio: float = 0.5
    authority: str = "kept"
    unset: str | None = None
    unset_token: str | None = None


def test_settings_are_redacted_by_key_name_privacy_patterns_and_home() -> None:
    shown = redact_settings(Secrets(), privacy=_privacy(), home=HOME)
    assert shown == {
        "api_token": REDACTED,
        "password": REDACTED,
        "keys": REDACTED,
        "note": f"uses {REDACTED} here",
        "root": "~/projects",
        "home": "~",
        "sibling": f"{HOME}s/x",
        "roots": ["~/alpha", "/work/beta/main"],
        "groups": {"~/alpha": "alpha/main"},
        "parts": [{"cookie": REDACTED, "label": "plain"}],
        "max_depth": 3,
        "history": False,
        "ratio": 0.5,
        "authority": "kept",
    }
    text = json.dumps(shown)
    for secret in (CANARY, "t-value-1", "p-value-1", "k-value-1", "c-value-1"):
        assert secret not in text


def test_a_home_with_a_trailing_slash_and_an_empty_home() -> None:
    class Paths(BaseModel):
        root: str = f"{HOME}/projects"

    assert redact_settings(Paths(), privacy=_privacy(), home=f"{HOME}/") == {"root": "~/projects"}
    assert redact_settings(Paths(), privacy=_privacy(), home="") == {"root": f"{HOME}/projects"}


@pytest.mark.parametrize(
    ("key", "secret"),
    [
        ("secret", True),
        ("client_secret", True),
        ("api_token", True),
        ("TOKENS", True),
        ("password", True),
        ("passwd", True),
        ("key", True),
        ("private_key_file", True),
        ("auth", True),
        ("auth_header", True),
        ("credentials", True),
        ("session_cookies", True),
        ("keyboard", False),
        ("hotkey", False),
        ("authority", False),
        ("tokenizer", False),
        ("roots", False),
    ],
)
def test_the_secret_key_rule(key: str, secret: bool) -> None:
    assert (SECRET_KEY.search(key) is not None) is secret


# --------------------------------------------------------------------------------------------
# build_collectors
# --------------------------------------------------------------------------------------------


class WmSettings(BaseModel):
    interval_seconds: int = 30
    auth_token: str = "a-value-1"
    socket: str = f"{HOME}/.wm/socket"


class Empty(BaseModel):
    pass


def _plugin(name: str, *caps: Capability, settings: type[BaseModel] = Empty) -> CollectorPlugin:
    return CollectorPlugin(
        name=name,
        api_major=COLLECTOR_API_MAJOR,
        capabilities=frozenset(caps),
        config_model=settings,
        factory=lambda settings, context: None,  # type: ignore[arg-type,return-value]
    )


def _entry(name: str, *, builtin: bool = True) -> DiscoveredEntry:
    dist = Dist("fylgja" if builtin else "fylgja-vendor", "2.1.0")
    target = EntryPoint(name, f"pkg.{name}:plugin", dist, None)
    return DiscoveredEntry(name, target.value, dist.name, dist.version, target)


def _state(item: RegisteredCollector, health: CollectorHealth) -> CollectorState:
    plugin = item.plugin
    return CollectorState(
        name=item.name,
        origin=item.entry.origin,
        enabled=item.enabled,
        capabilities=frozenset() if plugin is None else plugin.capabilities,
        health=health,
    )


def _wm_stats() -> PluginStats:
    stats = PluginStats(accepted=3, dropped=1, lost=0)
    stats.rejected[Rejection.SOURCE] += 2
    stats.kinds = Counter({(EntityKind.WINDOW, "snapshot"): 2, (EntityKind.FOCUS, "event"): 1})
    stats.last_at = {
        (EntityKind.WINDOW, "snapshot"): T0 + timedelta(seconds=20),
        (EntityKind.FOCUS, "event"): T0 + timedelta(seconds=5),
    }
    return stats


def _fixture() -> tuple[list[RegisteredCollector], list[CollectorState], dict[str, PluginStats]]:
    wm = RegisteredCollector(
        _entry("wm"),
        enabled=True,
        plugin=_plugin("wm", Capability.WINDOWS, settings=WmSettings),
        settings=WmSettings(),
    )
    vendor = RegisteredCollector(_entry("vendor", builtin=False), enabled=False)
    dup = RegisteredCollector(
        _entry("dup"), enabled=True, plugin=_plugin("dup", Capability.CONTEXT), settings=Empty()
    )
    rival = RegisteredCollector(
        _entry("dup", builtin=False),
        enabled=False,
        health=HealthReport(
            state=HealthState.FAILED, error_kind="duplicate", detail="claimed twice", checked_at=T0
        ),
        rejected=True,
    )
    flaky = RegisteredCollector(
        _entry("flaky"),
        enabled=True,
        plugin=_plugin("flaky", Capability.SESSIONS),
        settings=Empty(),
    )
    registry = [dup, rival, flaky, vendor, wm]
    states = [
        _state(dup, CollectorHealth("dup", HealthState.OK, since=T0, last_ok_at=T0)),
        _state(
            rival,
            CollectorHealth(
                "dup",
                HealthState.FAILED,
                since=T0,
                last_error_kind="duplicate",
                detail="claimed twice",
            ),
        ),
        _state(
            flaky,
            CollectorHealth(
                "flaky",
                HealthState.FAILED,
                since=T0,
                restarts=2,
                last_error_kind="crash",
                detail=f"{HOME}/logs/flaky.log: {CANARY}",
                retry_in=4.5,
            ),
        ),
        _state(vendor, CollectorHealth("vendor", HealthState.DISABLED, since=T0)),
        _state(wm, CollectorHealth("wm", HealthState.OK, since=T0, last_ok_at=T0)),
    ]
    stats = {"dup": PluginStats(accepted=1), "flaky": PluginStats(), "wm": _wm_stats()}
    return registry, states, stats


def _config() -> Config:
    return config(
        {
            "actions": {"applescript": {"chrome": True, "iterm2": False}},
            "collectors": {"wm": {"interval_seconds": 30}, "gone": {"enabled": True}},
        }
    )


def _build(
    switch_records: dict[str, SwitchRecord] | None = None,
    *,
    started_at: datetime | None = T0,
) -> InspectCollectors:
    registry, states, stats = _fixture()
    return build_collectors(
        registry,
        states,
        stats,
        _config(),
        privacy=_privacy(),
        home=HOME,
        switch_records={} if switch_records is None else switch_records,
        notify=NotifyStats(),
        generated_at=T0 + timedelta(minutes=1),
        started_at=started_at,
    )


def _json(body: InspectCollectors) -> dict[str, object]:
    data = json.loads(dump_json(body))
    assert isinstance(data, dict)
    return data


def test_the_answer_lists_every_registry_item_in_order() -> None:
    body = _build()
    assert body.schema_ == INSPECT_COLLECTORS_SCHEMA
    assert body.generated_at == T0 + timedelta(minutes=1)
    assert body.started_at == T0
    assert [(c.name, c.state) for c in body.collectors] == [
        ("dup", "enabled"),
        ("dup", "rejected"),
        ("flaky", "enabled"),
        ("vendor", "disabled"),
        ("wm", "enabled"),
    ]


def test_a_running_collector_shows_its_settings_and_counts_per_kind() -> None:
    wm = _build().collectors[-1]
    assert (wm.origin, wm.distribution, wm.distribution_version) == ("built-in", "fylgja", "2.1.0")
    assert (wm.enabled, wm.loaded, wm.api_version) == (True, True, COLLECTOR_API_MAJOR)
    assert wm.capabilities == ["windows"]
    assert wm.entity_kinds == ["focus", "window", "workspace"]
    assert wm.settings == {
        "interval_seconds": 30,
        "auth_token": REDACTED,
        "socket": "~/.wm/socket",
    }
    assert wm.health.state is HealthState.OK
    assert wm.health.last_ok_at == T0
    records = wm.records
    assert records is not None
    assert (records.accepted, records.dropped, records.lost) == (3, 1, 0)
    assert records.rejected == {"schema": 0, "source": 2, "entity_kind": 0, "timestamp": 0}
    assert [(k.entity_kind, k.record_type, k.accepted, k.last_at) for k in records.kinds] == [
        ("focus", "event", 1, T0 + timedelta(seconds=5)),
        ("window", "snapshot", 2, T0 + timedelta(seconds=20)),
    ]
    assert records.last_at == T0 + timedelta(seconds=20)


def test_a_disabled_external_was_never_loaded_and_shows_metadata_only() -> None:
    vendor = _build().collectors[3]
    assert (vendor.origin, vendor.distribution) == ("external", "fylgja-vendor")
    assert (vendor.state, vendor.enabled, vendor.loaded) == ("disabled", False, False)
    assert vendor.health.state is HealthState.DISABLED
    shown = _json(_build())["collectors"]
    assert isinstance(shown, list)
    assert set(shown[3]) == {
        "name",
        "origin",
        "distribution",
        "distribution_version",
        "state",
        "enabled",
        "loaded",
        "health",
    }


def test_a_rejected_duplicate_has_no_counts_of_its_own() -> None:
    kept, rival = _build().collectors[:2]
    assert kept.records is not None
    assert kept.records.accepted == 1
    assert (rival.state, rival.origin, rival.loaded) == ("rejected", "external", False)
    assert rival.records is None
    assert rival.settings is None
    assert rival.health.state is HealthState.FAILED
    assert rival.health.last_error_kind == "duplicate"


def test_a_collector_in_backoff_shows_its_retry_and_a_redacted_detail() -> None:
    flaky = _build().collectors[2]
    assert flaky.health.restarts == 2
    assert flaky.health.retry_in_seconds == 4.5
    assert flaky.health.detail == f"~/logs/flaky.log: {REDACTED}"
    assert flaky.settings == {}
    assert flaky.records is not None
    assert flaky.records.kinds == []
    assert flaky.records.last_at is None


def test_tables_without_an_installed_collector_are_listed() -> None:
    body = _build()
    assert "gone" in body.not_installed
    assert {"wm", "dup", "flaky", "vendor"}.isdisjoint(body.not_installed)
    assert body.not_installed == sorted(body.not_installed)
    assert body.not_installed == sorted(
        set(_config().collectors) - {"wm", "dup", "flaky", "vendor"}
    )


def test_the_switches_show_configuration_and_doctor_s_record() -> None:
    unchecked = _build()
    assert [(s.name, s.configured, s.doctor_ok) for s in unchecked.applescript] == [
        (SWITCHES[0], True, None),
        (SWITCHES[1], False, None),
    ]
    record = SwitchRecord(ok=False, at=T0, reason=f"timeout {CANARY}")
    checked = _build({SWITCHES[0]: record, "other": SwitchRecord(ok=True, at=T0)})
    first = checked.applescript[0]
    assert (first.doctor_ok, first.doctor_at) == (False, T0)
    assert first.doctor_reason == f"timeout {REDACTED}"
    assert [s.name for s in checked.applescript] == list(SWITCHES)


def test_the_json_has_no_null_and_no_secret() -> None:
    data = _json(_build(started_at=None))
    assert list(nulls(data)) == []
    assert "started_at" not in data
    assert data["schema"] == INSPECT_COLLECTORS_SCHEMA
    text = json.dumps(data)
    for secret in (CANARY, "a-value-1", HOME):
        assert secret not in text


def test_states_must_match_the_registry() -> None:
    registry, states, stats = _fixture()
    with pytest.raises(ValueError, match="one collector state"):
        build_collectors(
            registry,
            states[:-1],
            stats,
            _config(),
            privacy=_privacy(),
            home=HOME,
            switch_records={},
            notify=NotifyStats(),
            generated_at=T0,
            started_at=None,
        )


# --------------------------------------------------------------------------------------------
# build_pipeline
# --------------------------------------------------------------------------------------------


class _Stats:
    def __init__(self, sizes: list[int]) -> None:
        self.transactions = len(sizes)
        self.items = sum(sizes)
        self.duplicates = 1
        self.failed_items = 2
        self.failed_transactions = 3
        self.batch_sizes = sizes


class _Writer:
    def __init__(self, sizes: list[int]) -> None:
        self.running = True
        self.pending = 4
        self.stats = _Stats(sizes)


class _View:
    version = 17

    def table(self, collection: Collection) -> dict[str, object]:
        return {"a": 1, "b": 2} if collection is Collection.WINDOWS else {}


class _Summaries:
    enabled = False
    running = 1
    calls = 5

    def counts(self) -> tuple[int, int]:
        return 6, 2


_PRIVACY_CONFIG = config({"privacy": {"redact_patterns": ["a", "b"], "ignore_bundle_ids": ["x.y"]}})


def _pipeline(sizes: list[int]) -> dict[str, object]:
    stats = _wm_stats()
    other = PluginStats(accepted=1, dropped=2, lost=3)
    other.rejected[Rejection.SOURCE] += 1
    other.rejected[Rejection.SCHEMA] += 4
    other.kinds = Counter({(EntityKind.SESSION, "snapshot"): 1})
    body = build_pipeline(
        {"wm": stats, "agents": other, "idle": PluginStats()},
        _Writer(sizes),  # type: ignore[arg-type]
        _View(),  # type: ignore[arg-type]
        _Summaries(),  # type: ignore[arg-type]
        _PRIVACY_CONFIG,
        notify=NotifyStats(),
        sse_clients=2,
        sse_max_clients=16,
        lifecycle=["start:writer", "start:gate"],
        generated_at=T0 + timedelta(minutes=1),
        started_at=T0,
    )
    data = json.loads(dump_json(body))
    assert isinstance(data, dict)
    return data


def test_the_pipeline_sums_the_gate_and_sorts_the_flows() -> None:
    data = _pipeline([2, 4])
    assert data["schema"] == INSPECT_PIPELINE_SCHEMA
    assert data["lifecycle"] == ["start:writer", "start:gate"]
    assert data["collectors_running"] == 2
    assert data["gate"] == {
        "accepted": 4,
        "dropped": 3,
        "lost": 3,
        "rejected": {"schema": 4, "source": 3, "entity_kind": 0, "timestamp": 0},
    }
    flows = data["flows"]
    assert isinstance(flows, list)
    assert [(f["collector"], f["entity_kind"], f["record_type"], f["accepted"]) for f in flows] == [
        ("agents", "session", "snapshot", 1),
        ("wm", "focus", "event", 1),
        ("wm", "window", "snapshot", 2),
    ]
    assert "last_at" not in flows[0]
    assert "last_at" in flows[1]


def test_the_pipeline_reports_writer_view_events_privacy_and_summaries() -> None:
    data = _pipeline([2, 4])
    assert data["writer"] == {
        "running": True,
        "pending": 4,
        "transactions": 2,
        "items": 6,
        "duplicates": 1,
        "failed_items": 2,
        "failed_transactions": 3,
        "batch_max": 4,
        "batch_mean": 3.0,
    }
    live_view = data["live_view"]
    assert isinstance(live_view, dict)
    assert live_view["version"] == 17
    assert live_view["rows"]["windows"] == 2  # type: ignore[index]
    assert live_view["rows"]["todos"] == 0  # type: ignore[index]
    assert data["events"] == {"clients": 2, "max_clients": 16}
    privacy = _PRIVACY_CONFIG.privacy
    assert data["privacy"] == {
        "redact_patterns": len(privacy.redact_patterns),
        "ignore_bundle_ids": len(privacy.ignore_bundle_ids),
        "ignore_title_patterns": len(privacy.ignore_title_patterns),
    }
    summaries = data["summaries"]
    assert isinstance(summaries, dict)
    assert (summaries["enabled"], summaries["running"], summaries["calls"]) == (False, 1, 5)
    assert (summaries["cached"], summaries["jobs"]) == (6, 2)
    assert data["messaging_enabled"] is False
    assert "started_at" in data
    assert "generated_at" in data


def test_an_empty_batch_window_is_zero_and_privacy_values_never_appear() -> None:
    data = _pipeline([])
    writer = data["writer"]
    assert isinstance(writer, dict)
    assert (writer["batch_max"], writer["batch_mean"]) == (0, 0.0)
    assert list(nulls(data)) == []
    privacy = data["privacy"]
    assert isinstance(privacy, dict)
    assert all(isinstance(value, int) for value in privacy.values())


# --------------------------------------------------------------------------------------------
# build_explain
# --------------------------------------------------------------------------------------------


def _workspace(name: str, label: str, source: LabelSource, inferred: str | None) -> Workspace:
    return Workspace(
        name=name,
        label=label,
        label_source=source,
        inferred_label=inferred,
        one_liner=label,
        windows=[],
        projects=[],
        sessions=[],
    )


def _placement(key: str, *, note: str = "", workspace: str | None = "1") -> Placement:
    return Placement(
        session_key=key,
        project_path="/work/alpha/main",
        project_via="git-root",
        project_confidence="high",
        group_name=None,
        host_kind=None,
        host_ref=None,
        window_id="10",
        workspace=workspace,
        via="ancestry",
        confidence="high",
        note=note,
    )


def _project(path: str, name: str) -> ProjectPlacement:
    return ProjectPlacement(
        path=path,
        name=name,
        repo_root=path,
        group_name=None,
        sessions=1,
        windows=1,
        workspace="1",
        via="sessions",
        confidence="high",
    )


def _picture() -> Now:
    return Now(
        generated_at=T0,
        workspaces=[
            _workspace("10", "Docs", LabelSource.INFERRED, "Safari docs"),
            _workspace("2", "My label", LabelSource.MANUAL, "alpha"),
            _workspace("1", "alpha", LabelSource.INFERRED, "alpha"),
        ],
        waiting=[Waiting(kind="input", target="claude:k", reason="asks")],
        next=[
            NextItem(
                suggestion_id="nl-1.1",
                kind="input",
                target_ref="claude:k",
                score=100.0,
                reason="asks",
                rank=1,
            )
        ],
        collectors=[],
        degraded=[],
    )


def _resolution() -> Resolution:
    return Resolution(
        sessions={
            "claude:b": _placement("claude:b", note="two windows"),
            "claude:a": _placement("claude:a"),
        },
        projects={
            "/work/beta/main": _project("/work/beta/main", "beta"),
            "/work/alpha/main": _project("/work/alpha/main", "alpha"),
        },
        labels={
            "1": InferredLabel("alpha", "project"),
            "10": InferredLabel("Safari docs", "app-title"),
            "2": InferredLabel("alpha", "project"),
        },
    )


def test_explain_gives_the_rule_source_and_natural_order_of_each_label() -> None:
    body = build_explain(_picture(), _resolution(), generated_at=T0)
    assert body.resolved is True
    assert [(w.workspace, w.label_source, w.rule) for w in body.labels] == [
        ("1", LabelSource.INFERRED, "project"),
        ("2", LabelSource.MANUAL, "project"),
        ("10", LabelSource.INFERRED, "app-title"),
    ]
    assert body.labels[1].label == "My label"
    assert body.labels[1].inferred_label == "alpha"


def test_explain_sorts_placements_and_leaves_an_empty_note_out() -> None:
    body = build_explain(_picture(), _resolution(), generated_at=T0)
    assert [p.path for p in body.projects] == ["/work/alpha/main", "/work/beta/main"]
    assert [s.session_key for s in body.sessions] == ["claude:a", "claude:b"]
    data = json.loads(dump_json(body))
    assert "note" not in data["sessions"][0]
    assert data["sessions"][1]["note"] == "two windows"
    assert list(_nulls(data)) == []


def test_explain_before_the_first_resolution_has_no_rule_and_no_placements() -> None:
    body = build_explain(_picture(), None, generated_at=T0)
    assert body.resolved is False
    assert [w.rule for w in body.labels] == [None, None, None]
    assert (body.projects, body.sessions) == ([], [])
    assert "rule" not in json.loads(dump_json(body))["labels"][0]


def test_explain_carries_the_ranking_rules_and_the_next_and_waiting_lists() -> None:
    body = build_explain(_picture(), None, generated_at=T0)
    assert body.schema_ == "fylgja.inspect.explain/1"
    assert body.generated_at == T0
    assert body.ranking[0].kind == "permission"
    assert body.ranking[0].score == 100.0
    assert [r.kind for r in body.ranking] == list(SCORES)
    assert len(body.ranking) == 8
    assert body.idle_window_seconds == 3600.0
    assert body.max_items == 10
    assert [i.suggestion_id for i in body.next] == ["nl-1.1"]
    assert [w.target for w in body.waiting] == ["claude:k"]


def test_explain_of_a_bare_picture_has_empty_lists() -> None:
    bare = Now(generated_at=T0, collectors=[], degraded=[])
    body = build_explain(bare, None, generated_at=T0)
    assert (body.labels, body.next, body.waiting) == ([], [], [])


def _nulls(value: object, path: str = "$") -> list[str]:
    if value is None:
        return [path]
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found += _nulls(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += _nulls(item, f"{path}[{index}]")
    return found
