"""The shared collector contract run against the built-in ``appcontext`` collector (WP5.3).

The working source is a window source that follows the fake clock: a JetBrains window, a cmux
window (its session store is in the fake filesystem) and a Chrome window, with focus moving between
them every few seconds and titles that change, so tier 1 and tier 2 both emit. The broken sources
each fail one way: the store is unreadable, an AppleScript call times out (the switch is on and
Chrome is focused), and a second-tier input file is garbled.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

import pytest
from appcontext_support import (
    CHROME,
    CMUX,
    CMUX_STORE,
    HOME,
    OBSIDIAN,
    OBSIDIAN_JSON,
    PYCHARM,
    T0,
    FakeHistory,
    FakeSource,
    cmux_store,
    window,
)

from fylgja.collectors.appcontext import adapter, switches
from fylgja.collectors.appcontext.adapter import AppContextInputs, AppleScriptSwitches
from fylgja.testing.contract import CollectorContract, ContractEnv, ContractSource


class _FollowingSource(FakeSource):
    """Windows and focus that change with the fake clock."""

    def __init__(self, env: ContractEnv) -> None:
        super().__init__()
        self._env = env

    def read(self) -> adapter.WindowSnapshot:
        seconds = int(self._env.clock.monotonic())
        self.windows = [
            window("1", PYCHARM, f"proj [~/proj] – file{seconds // 10}.py"),
            window("2", CMUX, "alpha"),
            window("3", CHROME, "Page - Google Chrome - Person 1"),
        ]
        self.focus_window = ("1", "2", "3")[(seconds // 5) % 3]
        self.focus_at = T0 + timedelta(seconds=seconds - seconds % 5)
        return super().read()


def _install(env: ContractEnv, inputs: AppContextInputs) -> None:
    env.monkeypatch.setattr(adapter, "default_inputs", lambda database: inputs)


def _inputs(
    env: ContractEnv, source: adapter.WindowSource, *, chrome: bool = False
) -> AppContextInputs:
    return AppContextInputs(
        source=source,
        runner=env.process_runner,
        fs=env.fs,
        home=HOME,
        history=FakeHistory(),
        switches=AppleScriptSwitches(chrome=chrome),
    )


def _working(env: ContractEnv) -> Mapping[str, Any]:
    env.fs.add_file(CMUX_STORE, cmux_store({"ws": ("surface", "/work/alpha", 1.0)}))
    env.process_runner.script(switches.OSASCRIPT, stdout=b"https://example.org/a\n")
    _install(env, _inputs(env, _FollowingSource(env), chrome=True))
    return {}


def _store_unreadable(env: ContractEnv) -> Mapping[str, Any]:
    broken = env.tmp_path / "broken" / "fylgja.db"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(b"this is not a database" * 64)
    _install(env, _inputs(env, adapter.StoreWindowSource(broken)))
    return {"database": str(broken)}


def _applescript_times_out(env: ContractEnv) -> Mapping[str, Any]:
    env.process_runner.script(switches.OSASCRIPT, timeout=True)
    source = FakeSource()
    source.windows = [window("3", CHROME, "Page - Google Chrome")]
    source.focus_on("3")
    _install(env, _inputs(env, source, chrome=True))
    return {}


def _input_file_garbled(env: ContractEnv) -> Mapping[str, Any]:
    env.fs.add_file(OBSIDIAN_JSON, "{ this is not json")
    source = FakeSource()
    source.windows = [window("5", OBSIDIAN, "Daily - Notes - Obsidian v1.13.7")]
    source.focus_on("5")
    _install(env, _inputs(env, source))
    return {}


class TestAppcontextContract(CollectorContract):
    @pytest.fixture
    def plugin_name(self) -> str:
        return "appcontext"

    @pytest.fixture
    def working_source(self) -> ContractSource:
        return ContractSource(label="windows that follow the clock", arrange=_working)

    @pytest.fixture
    def broken_source(self) -> list[ContractSource]:
        return [
            ContractSource(label="store unreadable", arrange=_store_unreadable),
            ContractSource(label="AppleScript times out", arrange=_applescript_times_out),
            ContractSource(label="garbled input file", arrange=_input_file_garbled),
        ]
