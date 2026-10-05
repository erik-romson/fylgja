"""Registry behaviour with synthetic entry points (``PLAN.md`` §4.9 core registry tests)."""

from __future__ import annotations

import importlib
import sys
import textwrap
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from fylgja import paths
from fylgja.collectors.base import (
    COLLECTOR_API_MAJOR,
    Capability,
    Collector,
    CollectorContext,
    CollectorPlugin,
)
from fylgja.collectors.registry import (
    EntryPointLike,
    Origin,
    discover,
    list_collectors,
    load_registry,
)
from fylgja.config import Config, load_config
from fylgja.models import HealthReport, HealthState


@dataclass(frozen=True)
class FakeDist:
    name: str
    version: str


@dataclass
class FakeEntryPoint:
    name: str
    value: str
    dist: FakeDist | None
    loader: Callable[[], object]
    loads: int = field(default=0)

    def load(self) -> object:
        self.loads += 1
        return self.loader()


class _Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interval: int = 5


def _factory(settings: BaseModel, context: CollectorContext) -> Collector:
    raise NotImplementedError


def _plugin(name: str, api_major: int = COLLECTOR_API_MAJOR) -> CollectorPlugin:
    return CollectorPlugin(
        name=name,
        api_major=api_major,
        capabilities=frozenset({Capability.CONTEXT, Capability.TODOS}),
        config_model=_Settings,
        factory=_factory,
    )


def _ep(
    name: str,
    dist: str | None = "fylgja-vendor",
    version: str = "1.2.3",
    obj: object | None = None,
    loader: Callable[[], object] | None = None,
) -> FakeEntryPoint:
    chosen = loader if loader is not None else (lambda: obj if obj is not None else _plugin(name))
    return FakeEntryPoint(
        name=name,
        value=f"{name}_mod:plugin",
        dist=None if dist is None else FakeDist(dist, version),
        loader=chosen,
    )


def _source(*eps: FakeEntryPoint) -> Callable[[], list[EntryPointLike]]:
    return lambda: list(eps)


def _config(toml_text: str = "") -> Config:
    if toml_text:
        target = paths.config_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(toml_text, encoding="utf-8")
    return load_config()


# --- discovery -----------------------------------------------------------------------------


def test_discovery_reads_metadata_without_loading() -> None:
    ep = _ep("vendor_thing")
    result = discover(_source(ep))
    assert [(e.name, e.distribution, e.version, e.origin) for e in result.entries] == [
        ("vendor_thing", "fylgja-vendor", "1.2.3", Origin.EXTERNAL)
    ]
    assert result.entries[0].value == "vendor_thing_mod:plugin"
    assert ep.loads == 0


@pytest.mark.parametrize("dist_name", ["fylgja", "Fylgja"])
def test_origin_is_built_in_for_the_fylgja_distribution(dist_name: str) -> None:
    assert discover(_source(_ep("x", dist=dist_name))).entries[0].origin is Origin.BUILT_IN


def test_entry_point_without_distribution_is_external() -> None:
    entry = discover(_source(_ep("x", dist=None))).entries[0]
    assert entry.origin is Origin.EXTERNAL
    assert (entry.distribution, entry.version) == (None, None)


def test_default_source_reads_installed_metadata() -> None:
    result = discover()
    assert all(e.entry_point.name == e.name for e in result.entries)


def test_exact_repeat_of_an_entry_point_is_not_a_duplicate() -> None:
    result = discover(_source(_ep("x"), _ep("x")))
    assert len(result.entries) == 1
    assert result.duplicates == ()


# --- duplicates ----------------------------------------------------------------------------


def test_duplicate_external_names_are_all_rejected_before_any_import() -> None:
    first, second = _ep("clash", dist="vendor-a"), _ep("clash", dist="vendor-b")
    other = _ep("fine", dist="vendor-c")
    config = _config("[collectors.clash]\nenabled = true\n[collectors.fine]\nenabled = true\n")
    rows = {
        (r.name, r.distribution): r
        for r in list_collectors(config, source=_source(first, second, other))
    }
    assert first.loads == second.loads == 0
    for key in (("clash", "vendor-a"), ("clash", "vendor-b")):
        row = rows[key]
        assert row.state == "rejected"
        assert not row.enabled
        assert isinstance(row.health, HealthReport)
        assert row.health.state is HealthState.FAILED
        assert row.health.error_kind == "duplicate"
        assert row.api_version == "not_loaded"
    assert rows[("fine", "vendor-c")].state == "enabled"


def test_external_cannot_shadow_a_built_in() -> None:
    builtin = _ep("windows", dist="fylgja")
    thief = _ep("windows", dist="vendor-a")
    config = _config("[collectors.windows]\nenabled = true\n")
    registry = load_registry(config, _source(builtin, thief))
    assert thief.loads == 0
    by_dist = {c.entry.distribution: c for c in registry}
    assert by_dist["fylgja"].runnable
    assert by_dist["vendor-a"].rejected
    assert not by_dist["vendor-a"].runnable


# --- loading -------------------------------------------------------------------------------


def test_enabled_external_loads_and_lists_full_row() -> None:
    config = _config("[collectors.vendor_thing]\nenabled = true\ninterval = 9\n")
    ep = _ep("vendor_thing")
    registry = load_registry(config, _source(ep))
    assert ep.loads == 1
    (item,) = registry
    assert item.runnable
    assert isinstance(item.settings, _Settings)
    assert item.settings.interval == 9
    (row,) = list_collectors(config, registry=registry)
    assert row.model_dump(mode="json", exclude={"health": {"checked_at"}}) == {
        "name": "vendor_thing",
        "origin": "external",
        "distribution": "fylgja-vendor",
        "distribution_version": "1.2.3",
        "state": "enabled",
        "enabled": True,
        "api_version": COLLECTOR_API_MAJOR,
        "capabilities": ["context", "todos"],
        "health": {"state": "starting", "detail": "not yet started", "error_kind": None},
    }


def test_live_health_overrides_the_starting_default() -> None:
    config = _config("[collectors.vendor_thing]\nenabled = true\n")
    live = HealthReport(state=HealthState.OK, checked_at=_now())
    (row,) = list_collectors(
        config, source=_source(_ep("vendor_thing")), health={"vendor_thing": live}
    )
    assert row.health == live


def _now() -> Any:
    from datetime import UTC, datetime

    return datetime.now(UTC)


def test_disabled_external_is_not_loaded_and_shows_metadata_only() -> None:
    ep = _ep("vendor_thing")
    (row,) = list_collectors(_config(), source=_source(ep))
    assert ep.loads == 0
    assert row.model_dump(mode="json") == {
        "name": "vendor_thing",
        "origin": "external",
        "distribution": "fylgja-vendor",
        "distribution_version": "1.2.3",
        "state": "disabled",
        "enabled": False,
        "api_version": "not_loaded",
        "capabilities": "not_loaded",
        "health": "not_loaded",
    }


def test_explicit_false_keeps_an_external_disabled() -> None:
    ep = _ep("vendor_thing")
    list_collectors(_config("[collectors.vendor_thing]\nenabled = false\n"), source=_source(ep))
    assert ep.loads == 0


def test_enable_and_disable_toggle_loading() -> None:
    ep = _ep("vendor_thing")
    (off,) = load_registry(_config(), _source(ep))
    assert not off.enabled
    assert not off.runnable
    assert ep.loads == 0
    (on,) = load_registry(_config("[collectors.vendor_thing]\nenabled = true\n"), _source(ep))
    assert on.enabled
    assert on.runnable
    assert ep.loads == 1
    (off_again,) = load_registry(
        _config("[collectors.vendor_thing]\nenabled = false\n"), _source(ep)
    )
    assert not off_again.enabled
    assert ep.loads == 1


def test_built_ins_load_by_default_and_can_be_disabled() -> None:
    ep = _ep("windows", dist="fylgja")
    (default,) = load_registry(_config(), _source(ep))
    assert default.enabled
    assert default.runnable
    assert ep.loads == 1
    config = _config("[collectors.windows]\nenabled = false\n")
    (off,) = load_registry(config, _source(ep))
    assert not off.enabled
    assert not off.runnable
    assert off.loaded
    (row,) = list_collectors(config, registry=(off,))
    assert row.state == "disabled"
    assert isinstance(row.health, HealthReport)
    assert row.health.state is HealthState.DISABLED
    assert row.capabilities == ["context", "todos"]


def test_incompatible_api_major_fails_with_api_major_kind() -> None:
    ep = _ep("vendor_thing", obj=_plugin("vendor_thing", api_major=COLLECTOR_API_MAJOR + 1))
    config = _config("[collectors.vendor_thing]\nenabled = true\n")
    (item,) = load_registry(config, _source(ep))
    assert not item.runnable
    assert item.health is not None
    assert item.health.state is HealthState.FAILED
    assert item.health.error_kind == "api_major"
    (row,) = list_collectors(config, registry=(item,))
    assert row.api_version == COLLECTOR_API_MAJOR + 1


def test_import_error_in_enabled_plugin_is_failed_import_health() -> None:
    def boom() -> object:
        raise ImportError("no module named vendor_dep")

    config = _config("[collectors.vendor_thing]\nenabled = true\n")
    (row,) = list_collectors(config, source=_source(_ep("vendor_thing", loader=boom)))
    assert row.state == "enabled"
    assert isinstance(row.health, HealthReport)
    assert row.health.state is HealthState.FAILED
    assert row.health.error_kind == "import"
    assert "ImportError" in (row.health.detail or "")
    assert row.api_version == "not_loaded"
    assert row.capabilities == "not_loaded"


def test_import_error_in_built_in_is_reported_not_raised() -> None:
    def boom() -> object:
        raise RuntimeError("broken")

    (item,) = load_registry(_config(), _source(_ep("windows", dist="fylgja", loader=boom)))
    assert item.health is not None
    assert item.health.error_kind == "import"


def test_non_plugin_object_is_rejected() -> None:
    config = _config("[collectors.vendor_thing]\nenabled = true\n")
    (item,) = load_registry(config, _source(_ep("vendor_thing", obj=object())))
    assert item.health is not None
    assert item.health.error_kind == "entry_point"


def test_plugin_name_must_match_entry_point_name() -> None:
    config = _config("[collectors.vendor_thing]\nenabled = true\n")
    (item,) = load_registry(config, _source(_ep("vendor_thing", obj=_plugin("other"))))
    assert item.health is not None
    assert item.health.error_kind == "name"


def test_invalid_namespace_fails_with_config_health_naming_the_key() -> None:
    config = _config('[collectors.vendor_thing]\nenabled = true\ninterval = "soon"\nextra = 1\n')
    (item,) = load_registry(config, _source(_ep("vendor_thing")))
    assert not item.runnable
    assert item.health is not None
    assert item.health.error_kind == "config"
    detail = item.health.detail or ""
    assert "[collectors.vendor_thing].interval" in detail
    assert "[collectors.vendor_thing].extra" in detail
    assert "soon" not in detail


# --- no import before opt-in ---------------------------------------------------------------


@pytest.fixture
def import_bomb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, str]]:
    sentinel = tmp_path / "bomb.sentinel"
    module = "fylgja_import_bomb_mod"
    (tmp_path / f"{module}.py").write_text(
        textwrap.dedent(
            f"""
            from pathlib import Path

            Path({str(sentinel)!r}).write_text("imported")
            raise RuntimeError("import bomb")
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    yield sentinel, module
    sys.modules.pop(module, None)


def _bomb_entry(module: str) -> FakeEntryPoint:
    def load() -> object:
        return importlib.import_module(module).plugin

    entry = _ep("import_bomb", dist="fylgja-fake", loader=load)
    return FakeEntryPoint(entry.name, f"{module}:plugin", entry.dist, load)


def test_disabled_external_is_never_imported(import_bomb: tuple[Path, str]) -> None:
    sentinel, module = import_bomb
    source = _source(_bomb_entry(module))
    config = _config()
    discover(source)
    rows = list_collectors(config, source=source)
    load_registry(config, source)
    explicit_off = _config("[collectors.import_bomb]\nenabled = false\n")
    list_collectors(explicit_off, source=source)
    assert [r.state for r in rows] == ["disabled"]
    assert rows[0].health == "not_loaded"
    assert not sentinel.exists()
    assert module not in sys.modules


def test_enabling_the_bomb_reports_import_failure_without_raising(
    import_bomb: tuple[Path, str],
) -> None:
    sentinel, module = import_bomb
    config = _config("[collectors.import_bomb]\nenabled = true\n")
    (row,) = list_collectors(config, source=_source(_bomb_entry(module)))
    assert sentinel.exists()
    assert isinstance(row.health, HealthReport)
    assert row.health.error_kind == "import"
