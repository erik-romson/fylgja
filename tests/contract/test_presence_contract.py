"""The shared collector contract run against the built-in ``presence`` collector (WP5.1).

The working source is a :class:`FakeSignals` whose readings follow the fake clock: the screen is
locked from 5 s to 15 s, the microphone runs from 20 s to 30 s and the keyboard is untouched from
10 s to 40 s (an idle threshold of 20 s, so idle runs from 30 s to 45 s). The broken sources use the
real :class:`SystemSignals` over fake frameworks: every framework missing, a platform other than
macOS, no ``ioreg`` and a store that cannot be read.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from presence_support import FakeFrameworks, FakeSignals, store_with_focus

from fylgja.collectors.presence import adapter
from fylgja.testing.contract import CollectorContract, ContractEnv, ContractSource

IOREG_ARGV = (adapter.IOREG, "-r", "-c", "IOHIDSystem", "-d", "1")
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "presence"


def _between(env: ContractEnv, start: float, end: float) -> Callable[[], bool]:
    return lambda: start <= env.clock.monotonic() < end


def _install(env: ContractEnv, signals: adapter.PresenceSignals) -> None:
    env.monkeypatch.setattr(adapter, "default_signals", lambda database: signals)


def _working(env: ContractEnv) -> Mapping[str, Any]:
    signals = FakeSignals()
    signals.locked = _between(env, 5, 15)
    signals.microphone = _between(env, 20, 30)
    signals.idle = lambda: max(0.0, env.clock.monotonic() - 10) if _between(env, 10, 40)() else 0.0
    _install(env, signals)
    return {"idle_threshold_seconds": 20}


def _system(
    env: ContractEnv, frameworks: FakeFrameworks, database: Path, *, platform: str = "darwin"
) -> Mapping[str, Any]:
    system = adapter.SystemSignals(
        env.process_runner, database, loader=frameworks, platform=platform
    )
    _install(env, system)
    return {"database": str(database)}


def _focused_store(env: ContractEnv, name: str) -> Path:
    return store_with_focus(env.tmp_path / name / "fylgja.db", window_id="42")


def _frameworks_missing(env: ContractEnv) -> Mapping[str, Any]:
    frameworks = FakeFrameworks()
    frameworks.missing = {
        adapter.COREAUDIO,
        adapter.COREMEDIAIO,
        adapter.COREGRAPHICS,
        adapter.COREFOUNDATION,
    }
    env.process_runner.script(*IOREG_ARGV, stdout=(FIXTURES / "ioreg-active.txt").read_bytes())
    return _system(env, frameworks, _focused_store(env, "missing"))


def _not_macos(env: ContractEnv) -> Mapping[str, Any]:
    return _system(env, FakeFrameworks(), _focused_store(env, "linux"), platform="linux")


def _no_ioreg(env: ContractEnv) -> Mapping[str, Any]:
    # Nothing is scripted for ioreg, so the fake runner raises FileNotFoundError.
    return _system(env, FakeFrameworks(), _focused_store(env, "no-ioreg"))


def _store_unreadable(env: ContractEnv) -> Mapping[str, Any]:
    broken = env.tmp_path / "broken" / "fylgja.db"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(b"this is not a database" * 64)
    env.process_runner.script(*IOREG_ARGV, stdout=(FIXTURES / "ioreg-active.txt").read_bytes())
    return _system(env, FakeFrameworks(), broken)


def _one_signal_fails(env: ContractEnv) -> Mapping[str, Any]:
    frameworks = FakeFrameworks()
    frameworks.raising = {"CMIOObjectGetPropertyDataSize": OSError("boom")}
    env.process_runner.script(*IOREG_ARGV, stdout=(FIXTURES / "ioreg-active.txt").read_bytes())
    return _system(env, frameworks, _focused_store(env, "one"))


class TestPresenceContract(CollectorContract):
    @pytest.fixture
    def plugin_name(self) -> str:
        return "presence"

    @pytest.fixture
    def working_source(self) -> ContractSource:
        return ContractSource(label="signals that follow the clock", arrange=_working)

    @pytest.fixture
    def broken_source(self) -> list[ContractSource]:
        return [
            ContractSource(label="every framework missing", arrange=_frameworks_missing),
            ContractSource(label="not macOS", arrange=_not_macos),
            ContractSource(label="ioreg missing", arrange=_no_ioreg),
            ContractSource(label="store unreadable", arrange=_store_unreadable),
            ContractSource(label="one ctypes call raises", arrange=_one_signal_fails),
        ]
