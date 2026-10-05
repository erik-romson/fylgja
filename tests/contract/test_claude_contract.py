"""The shared collector contract run against the built-in ``claude`` collector (registry half).

The registry is a real temporary directory of committed fixture rows; ``ps`` is a
:class:`FakeProcessRunner` that lists each row's process. No Claude Code is needed.

The working source also holds one hook envelope in the inbox and one statusline snapshot (WP2.2),
so the records of the inbox drain and the statusline reader pass the same checks: schema-valid,
stable identity and dedup keys, clean cancellation. The broken sources include an inbox that is not
a directory.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest
from claude_support import FIXTURE_ROWS, ps_text
from inbox_support import SESSION, make_envelope, write_envelope

from fylgja import hook, paths
from fylgja.collectors.claude import adapter, parse
from fylgja.testing.contract import CollectorContract, ContractEnv, ContractSource
from fylgja.testing.fakes import EPOCH

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


def _spool_hook_files() -> None:
    """One hook envelope and one statusline snapshot, stamped just before the fake clock's start."""
    before = int(EPOCH.timestamp()) - 10
    write_envelope(
        paths.inbox_dir("claude"),
        make_envelope(
            "Stop",
            received_at_ns=before * 1_000_000_000,
            envelope_id="6f1e2d3c-4b5a-4c6d-8e7f-0a1b2c3d4e5f",
        ),
    )
    snapshot = make_envelope(
        "StatusLine",
        {
            "session_id": SESSION,
            "cost": {"total_cost_usd": 1.5},
            "context_window": {"used_percentage": 12},
            "rate_limits": {"five_hour": {"used_percentage": 20, "resets_at": before + 3600}},
        },
        received_at_ns=before * 1_000_000_000,
        envelope_id="0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
    )
    directory = paths.statusline_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{SESSION}.json"
    target.write_bytes(hook.encode_envelope(snapshot))
    os.utime(target, (before + 5, before + 5))


def _working(env: ContractEnv) -> Mapping[str, Any]:
    registry = _registry(env)
    _script_ps(env, _copy_rows(registry))
    _install(env)
    _spool_hook_files()
    return {"registry_dir": str(registry)}


def _inbox_not_a_directory(env: ContractEnv) -> Mapping[str, Any]:
    registry = _registry(env)
    _script_ps(env, _copy_rows(registry))
    _install(env)
    inbox = paths.inbox_dir("claude")
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text("not a directory", encoding="utf-8")
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
            ContractSource(label="inbox is not a directory", arrange=_inbox_not_a_directory),
        ]
