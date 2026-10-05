"""The shared collector contract run against the built-in ``claude`` collector (registry half).

The registry is a real temporary directory of committed fixture rows; ``ps`` is a
:class:`FakeProcessRunner` that lists each row's process. No Claude Code is needed.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest
from claude_support import FIXTURE_ROWS, ps_text

from fylgja.collectors.claude import adapter, parse
from fylgja.testing.contract import CollectorContract, ContractEnv, ContractSource

PS = "/bin/ps"


def _install(env: ContractEnv) -> None:
    seams = adapter.Seams(env.process_runner, adapter.LocalFileSystem())
    env.monkeypatch.setattr(adapter, "default_seams", lambda: seams)


def _registry(env: ContractEnv) -> Path:
    registry = env.tmp_path / "registry"
    registry.mkdir(parents=True, exist_ok=True)
    return registry


def _copy_rows(registry: Path) -> list[parse.RegistryRow]:
    rows: list[parse.RegistryRow] = []
    for source in sorted(FIXTURE_ROWS.glob("*.json")):
        shutil.copy(source, registry / source.name)
        row = parse.parse_row(source.read_bytes(), file_pid=int(source.stem))
        assert isinstance(row, parse.RegistryRow)
        rows.append(row)
    return rows


def _script_ps(env: ContractEnv, rows: list[parse.RegistryRow]) -> None:
    lines = []
    for row in rows:
        started = (
            row.proc_start.replace(tzinfo=UTC).timestamp()
            if row.proc_start is not None
            else (row.started_at_ms or 0) / 1000 - 5
        )
        lines.append(f"{row.pid:>5} {ps_text(started)}")
    env.process_runner.script(PS, stdout="\n".join(lines) + "\n")


def _working(env: ContractEnv) -> Mapping[str, Any]:
    registry = _registry(env)
    _script_ps(env, _copy_rows(registry))
    _install(env)
    return {"registry_dir": str(registry)}


def _missing_registry(env: ContractEnv) -> Mapping[str, Any]:
    _install(env)
    return {"registry_dir": str(env.tmp_path / "does-not-exist")}


def _ps_missing(env: ContractEnv) -> Mapping[str, Any]:
    registry = _registry(env)
    _copy_rows(registry)  # no script for ps: the command is missing
    _install(env)
    return {"registry_dir": str(registry)}


def _ps_failing(env: ContractEnv) -> Mapping[str, Any]:
    registry = _registry(env)
    _copy_rows(registry)
    env.process_runner.script(PS, returncode=2, stderr="ps: illegal option")
    _install(env)
    return {"registry_dir": str(registry)}


def _garbage_rows(env: ContractEnv) -> Mapping[str, Any]:
    registry = _registry(env)
    (registry / "4101.json").write_text("not json", encoding="utf-8")
    (registry / "4102.json").write_text('{"pid": 1}', encoding="utf-8")
    _install(env)
    return {"registry_dir": str(registry)}


class TestClaudeContract(CollectorContract):
    @pytest.fixture
    def plugin_name(self) -> str:
        return "claude"

    @pytest.fixture
    def working_source(self) -> ContractSource:
        return ContractSource(label="fixture registry", arrange=_working)

    @pytest.fixture
    def broken_source(self) -> list[ContractSource]:
        return [
            ContractSource(label="registry missing", arrange=_missing_registry),
            ContractSource(label="ps missing", arrange=_ps_missing),
            ContractSource(label="ps failing", arrange=_ps_failing),
            ContractSource(label="garbage rows", arrange=_garbage_rows),
        ]
