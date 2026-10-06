"""The shared collector contract run against the built-in ``codex`` collector.

The Codex directory is a real temporary one: a state database built from the sanitized DDL and rows,
and the committed rollouts. ``pgrep``, ``lsof`` and ``ps`` answer from the committed process
fixtures through a :class:`FakeProcessRunner`. No Codex is needed.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from codex_support import LSOF, PGREP, CodexHome, make_home, script_processes

from fylgja.collectors.codex import adapter
from fylgja.testing.contract import CollectorContract, ContractEnv, ContractSource


def _install(env: ContractEnv) -> None:
    seams = adapter.Seams(env.process_runner, adapter.LocalFileSystem())
    env.monkeypatch.setattr(adapter, "default_seams", lambda: seams)


def _home(env: ContractEnv, **options: Any) -> CodexHome:
    """The Codex directory of this test; a second run of the same test reuses it."""
    existing = env.tmp_path / "codex-home"
    if existing.exists():
        return CodexHome(existing, existing / "sessions" / "2026" / "10" / "06")
    return make_home(env.tmp_path, **options)


def _working(env: ContractEnv) -> Mapping[str, Any]:
    codex = _home(env)
    script_processes(env.process_runner, codex)
    _install(env)
    return {"codex_home": str(codex.home)}


def _pgrep_missing(env: ContractEnv) -> Mapping[str, Any]:
    codex = _home(env)
    _install(env)  # no script for pgrep: the command is missing
    return {"codex_home": str(codex.home)}


def _lsof_missing(env: ContractEnv) -> Mapping[str, Any]:
    codex = _home(env)
    env.process_runner.script(PGREP, stdout="41001\n")
    _install(env)
    return {"codex_home": str(codex.home)}


def _lsof_failing(env: ContractEnv) -> Mapping[str, Any]:
    codex = _home(env)
    env.process_runner.script(PGREP, stdout="41001\n")
    env.process_runner.script(LSOF, returncode=2, stderr="lsof: bad option")
    _install(env)
    return {"codex_home": str(codex.home)}


def _unknown_state_version(env: ContractEnv) -> Mapping[str, Any]:
    codex = _home(env, db=False)
    (codex.home / "state_9.sqlite").write_bytes(b"a layout nobody revalidated")
    script_processes(env.process_runner, codex)
    _install(env)
    return {"codex_home": str(codex.home)}


def _corrupt_database(env: ContractEnv) -> Mapping[str, Any]:
    codex = _home(env, db=False)
    (codex.home / "state_5.sqlite").write_bytes(b"corrupt" * 100)
    script_processes(env.process_runner, codex)
    _install(env)
    return {"codex_home": str(codex.home)}


def _missing_directory(env: ContractEnv) -> Mapping[str, Any]:
    env.process_runner.script(PGREP, returncode=1)
    _install(env)
    return {"codex_home": str(env.tmp_path / "does-not-exist")}


class TestCodexContract(CollectorContract):
    @pytest.fixture
    def plugin_name(self) -> str:
        return "codex"

    @pytest.fixture
    def working_source(self) -> ContractSource:
        return ContractSource(label="fixture codex directory", arrange=_working)

    @pytest.fixture
    def broken_source(self) -> list[ContractSource]:
        return [
            ContractSource(label="pgrep missing", arrange=_pgrep_missing),
            ContractSource(label="lsof missing", arrange=_lsof_missing),
            ContractSource(label="lsof failing", arrange=_lsof_failing),
            ContractSource(label="unknown state_N", arrange=_unknown_state_version),
            ContractSource(label="corrupt database", arrange=_corrupt_database),
            ContractSource(label="codex directory missing", arrange=_missing_directory),
        ]


def test_the_contract_directory_helper_is_idempotent(tmp_path: Path) -> None:
    env = ContractEnv(tmp_path, pytest.MonkeyPatch())
    first = _home(env)
    again = _home(env)
    assert first.home == again.home
