"""The shared collector contract run against the built-in ``opencode`` collector.

``opencode.db`` is a real temporary database built from the sanitized DDL and rows. ``pgrep``,
``lsof`` and ``ps`` answer from the committed process fixtures through a
:class:`FakeProcessRunner`. No OpenCode is needed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

import pytest
from opencode_support import FIXTURES, LSOF, PGREP, build_db, now, script_processes

from fylgja.collectors.opencode import adapter
from fylgja.testing.contract import CollectorContract, ContractEnv, ContractSource


def _install(env: ContractEnv) -> None:
    seams = adapter.Seams(env.process_runner, adapter.LocalFileSystem())
    env.monkeypatch.setattr(adapter, "default_seams", lambda: seams)
    # The fixture rows are relative to one instant; the contract's fake clock starts elsewhere.
    env.clock = type(env.clock)(now())


def _database(env: ContractEnv, name: str = "opencode.db") -> str:
    path = env.tmp_path / name
    if not path.exists():
        build_db(path)
    return str(path)


def _working(env: ContractEnv) -> Mapping[str, Any]:
    script_processes(env.process_runner)
    _install(env)
    return {"db_path": _database(env), "inbox_watch": "poll"}


def _db_missing(env: ContractEnv) -> Mapping[str, Any]:
    script_processes(env.process_runner)
    _install(env)
    return {"db_path": str(env.tmp_path / "missing" / "opencode.db"), "inbox_watch": "poll"}


def _db_corrupt(env: ContractEnv) -> Mapping[str, Any]:
    script_processes(env.process_runner)
    _install(env)
    path = env.tmp_path / "corrupt.db"
    path.write_bytes(b"corrupt" * 100)
    return {"db_path": str(path), "inbox_watch": "poll"}


def _schema_changed(env: ContractEnv) -> Mapping[str, Any]:
    script_processes(env.process_runner)
    _install(env)
    ddl = (FIXTURES / "opencode.schema.sql").read_text(encoding="utf-8")
    path = env.tmp_path / "changed.db"
    if not path.exists():
        connection = sqlite3.connect(path)
        connection.executescript(ddl.replace("`time_archived` integer,", ""))
        connection.close()
    return {"db_path": str(path), "inbox_watch": "poll"}


def _pgrep_missing(env: ContractEnv) -> Mapping[str, Any]:
    _install(env)  # no script for pgrep: the command is missing
    return {"db_path": _database(env), "inbox_watch": "poll"}


def _lsof_failing(env: ContractEnv) -> Mapping[str, Any]:
    env.process_runner.script(PGREP, stdout="51001\n")
    env.process_runner.script(LSOF, returncode=2, stderr="lsof: bad option")
    _install(env)
    return {"db_path": _database(env), "inbox_watch": "poll"}


class TestOpenCodeContract(CollectorContract):
    @pytest.fixture
    def plugin_name(self) -> str:
        return "opencode"

    @pytest.fixture
    def working_source(self) -> ContractSource:
        return ContractSource(label="fixture opencode database", arrange=_working)

    @pytest.fixture
    def broken_source(self) -> list[ContractSource]:
        return [
            ContractSource(label="database missing", arrange=_db_missing),
            ContractSource(label="corrupt database", arrange=_db_corrupt),
            ContractSource(label="changed schema", arrange=_schema_changed),
            ContractSource(label="pgrep missing", arrange=_pgrep_missing),
            ContractSource(label="lsof failing", arrange=_lsof_failing),
        ]
