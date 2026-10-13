"""The no-import rule against a real installed distribution (``PLAN.md`` §4.1, WP1.2/WP1.5).

``fylgja-fake`` is installed in the dev environment. Its ``fake_import_bomb`` entry point points
at a module that writes a sentinel and raises when imported, so any import shows up here.
"""

from __future__ import annotations

import os
import subprocess
import sys
from importlib.metadata import distribution
from pathlib import Path

import pytest

from fylgja import paths
from fylgja.collectors.registry import (
    NOT_LOADED,
    Origin,
    discover,
    list_collectors,
    load_registry,
)
from fylgja.config import Config, load_config
from fylgja.rankers.registry import discover_rankers, load_rankers

BOMB_MODULE = "fylgja_fake.bomb"


def _sentinel() -> Path:
    return paths.state_dir() / "fake_import_bomb.sentinel"


def _config(toml_text: str = "") -> Config:
    if toml_text:
        target = paths.config_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(toml_text, encoding="utf-8")
    return load_config()


def _assert_bomb_untouched() -> None:
    assert not _sentinel().exists()
    assert BOMB_MODULE not in sys.modules


def test_the_fake_distribution_is_really_installed() -> None:
    dist = distribution("fylgja-fake")
    names = {ep.name for ep in dist.entry_points if ep.group == "fylgja.collectors"}
    assert names == {"fake", "fake_import_bomb"}
    assert not any(r.startswith("fylgja") for r in (dist.requires or []))


def test_discovery_reads_metadata_and_never_imports_the_bomb() -> None:
    result = discover()
    by_name = {e.name: e for e in result.entries}
    assert {"fake", "fake_import_bomb"} <= set(by_name)
    for name in ("fake", "fake_import_bomb"):
        assert by_name[name].distribution == "fylgja-fake"
        assert by_name[name].origin is Origin.EXTERNAL
    assert by_name["fake_import_bomb"].value == "fylgja_fake.bomb:plugin"
    _assert_bomb_untouched()


def test_list_collectors_shows_fake_as_a_disabled_external_not_loaded() -> None:
    rows = {r.name: r for r in list_collectors(_config())}
    for name in ("fake", "fake_import_bomb"):
        row = rows[name]
        assert row.origin is Origin.EXTERNAL
        assert row.state == "disabled"
        assert not row.enabled
        assert row.api_version == NOT_LOADED
        assert row.capabilities == NOT_LOADED
        assert row.health == NOT_LOADED
        assert row.distribution == "fylgja-fake"
        assert row.distribution_version == "0.1.0"
    _assert_bomb_untouched()


def test_enabling_fake_loads_only_fake() -> None:
    config = _config("[collectors.fake]\nenabled = true\n")
    rows = {r.name: r for r in list_collectors(config)}
    assert rows["fake"].state == "enabled"
    assert rows["fake"].api_version == 1
    assert rows["fake"].capabilities == ["todos", "windows"]
    assert rows["fake_import_bomb"].api_version == NOT_LOADED
    registry = {c.name: c for c in load_registry(config)}
    assert registry["fake"].runnable
    assert not registry["fake_import_bomb"].loaded
    _assert_bomb_untouched()


def test_a_fresh_interpreter_discovers_and_lists_without_the_bomb(isolated_env: Path) -> None:
    sentinel = isolated_env / "bomb.sentinel"
    probe = (
        "import sys\n"
        "from fylgja.collectors.registry import discover, list_collectors\n"
        "from fylgja.config import load_config\n"
        "discover(); list_collectors(load_config())\n"
        "print('fylgja_fake.bomb' in sys.modules)\n"
    )
    env = {**os.environ, "FYLGJA_FAKE_SENTINEL": str(sentinel)}
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        timeout=25,
        env=env,
    )
    assert result.stdout.strip() == "False"
    assert not sentinel.exists()


@pytest.mark.parametrize("explicit", [True, False])
def test_the_bomb_really_fires_when_enabled_in_a_subprocess(
    isolated_env: Path, explicit: bool
) -> None:
    """Positive control: without it the absence checks above could pass for the wrong reason."""
    sentinel = isolated_env / "bomb.sentinel" if explicit else _sentinel()
    (isolated_env / "config" / "config.toml").write_text(
        "[collectors.fake_import_bomb]\nenabled = true\n", encoding="utf-8"
    )
    probe = (
        "from fylgja.collectors.registry import load_registry\n"
        "from fylgja.config import load_config\n"
        "item = {c.name: c for c in load_registry(load_config())}['fake_import_bomb']\n"
        "print(item.health.state.value, item.health.error_kind)\n"
    )
    env = dict(os.environ)
    env.pop("FYLGJA_FAKE_SENTINEL", None)
    if explicit:
        env["FYLGJA_FAKE_SENTINEL"] = str(sentinel)
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        timeout=25,
        env=env,
    )
    assert result.stdout.strip() == "failed import"
    assert sentinel.read_text(encoding="utf-8") == "imported\n"


# --------------------------------------------------------------------------------------------
# The ranker group of the same distribution (docs/decisions/0052)
# --------------------------------------------------------------------------------------------

RANKER_BOMB_MODULE = "fylgja_fake.ranker_bomb"


def _ranker_sentinel() -> Path:
    return paths.state_dir() / "fake_ranker_bomb.sentinel"


def _assert_ranker_bomb_untouched() -> None:
    assert not _ranker_sentinel().exists()
    assert RANKER_BOMB_MODULE not in sys.modules


def test_the_fake_distribution_also_ships_two_rankers() -> None:
    dist = distribution("fylgja-fake")
    names = {ep.name for ep in dist.entry_points if ep.group == "fylgja.rankers"}
    assert names == {"fake_ranker", "fake_ranker_bomb"}


def test_the_ranker_group_leaves_collector_discovery_alone() -> None:
    collectors = {e.name for e in (*discover().entries, *discover().duplicates)}
    assert not collectors & {"fake_ranker", "fake_ranker_bomb", "workspace", "recent"}
    rankers = {e.name for e in (*discover_rankers().entries, *discover_rankers().duplicates)}
    assert not rankers & {"fake", "fake_import_bomb"}
    _assert_ranker_bomb_untouched()


def test_enabling_fake_ranker_loads_only_it_with_its_settings() -> None:
    config = _config("[rankers.fake_ranker]\nenabled = true\nplace_weight = 3.0\n")
    registry = {r.name: r for r in load_rankers(config)}
    fake = registry["fake_ranker"]
    assert fake.origin is Origin.EXTERNAL
    assert (fake.distribution, fake.version) == ("fylgja-fake", "0.1.0")
    assert fake.loaded
    assert fake.settings is not None
    assert fake.settings.model_dump() == {"place_weight": 3.0}
    assert not registry["fake_ranker_bomb"].loaded
    assert registry["fake_ranker_bomb"].error is None
    _assert_ranker_bomb_untouched()


def test_a_fresh_interpreter_loads_rankers_without_the_ranker_bomb(isolated_env: Path) -> None:
    sentinel = isolated_env / "ranker-bomb.sentinel"
    probe = (
        "import sys\n"
        "from fylgja.rankers.registry import load_rankers\n"
        "from fylgja.config import load_config\n"
        "load_rankers(load_config())\n"
        "print('fylgja_fake.ranker_bomb' in sys.modules)\n"
    )
    env = {**os.environ, "FYLGJA_FAKE_RANKER_SENTINEL": str(sentinel)}
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        timeout=25,
        env=env,
    )
    assert result.stdout.strip() == "False"
    assert not sentinel.exists()


def test_the_ranker_bomb_really_fires_when_enabled_in_a_subprocess(isolated_env: Path) -> None:
    """Positive control: without it the absence checks above could pass for the wrong reason."""
    sentinel = isolated_env / "ranker-bomb.sentinel"
    (isolated_env / "config" / "config.toml").write_text(
        "[rankers.fake_ranker_bomb]\nenabled = true\n", encoding="utf-8"
    )
    probe = (
        "from fylgja.rankers.registry import load_rankers\n"
        "from fylgja.config import load_config\n"
        "item = {r.name: r for r in load_rankers(load_config())}['fake_ranker_bomb']\n"
        "print(item.loaded, item.error.split(':')[0])\n"
    )
    env = {**os.environ, "FYLGJA_FAKE_RANKER_SENTINEL": str(sentinel)}
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        timeout=25,
        env=env,
    )
    assert result.stdout.strip() == "False import"
    assert sentinel.read_text(encoding="utf-8") == "imported\n"
