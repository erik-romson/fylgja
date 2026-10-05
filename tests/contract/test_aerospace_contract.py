"""The shared collector contract run against the built-in ``aerospace`` collector.

The sources are fakes: a :class:`FakeProcessRunner` answers the list commands and a
:class:`FakeStreamOpener` hands out :class:`FakeLineStream` objects for ``subscribe``. No AeroSpace
is needed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from fylgja.collectors.aerospace import adapter
from fylgja.config import load_defaults
from fylgja.testing.contract import CollectorContract, ContractEnv, ContractSource
from fylgja.testing.fakes import FakeLineStream, FakeStreamOpener

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "aerospace"
BIN = str(load_defaults()["paths"]["aerospace"])


def _install(env: ContractEnv, opener: FakeStreamOpener) -> None:
    seams = adapter.Seams(env.process_runner, opener)
    env.monkeypatch.setattr(adapter, "default_seams", lambda: seams)


def _working(env: ContractEnv) -> Mapping[str, Any]:
    runner = env.process_runner
    runner.script(BIN, "list-windows", stdout=(FIXTURES / "windows.json").read_bytes())
    runner.script(
        BIN,
        "list-workspaces",
        "--all",
        "--json",
        stdout=(FIXTURES / "workspaces.json").read_bytes(),
    )
    runner.script(BIN, "--version", stdout="aerospace CLI client version: 0.21.3-Beta abc\n")
    runner.script(BIN, "list-workspaces", "--all", "--count", stdout="20\n")
    events = FakeLineStream((FIXTURES / "events.ndjson").read_bytes())
    _install(env, FakeStreamOpener(events))
    return {}


def _missing_binary(env: ContractEnv) -> Mapping[str, Any]:
    _install(env, FakeStreamOpener(error=FileNotFoundError("aerospace")))
    return {}


def _server_down(env: ContractEnv) -> Mapping[str, Any]:
    env.process_runner.script(BIN, returncode=1, stderr="Can't connect to AeroSpace server")
    _install(env, FakeStreamOpener(FakeLineStream(eof=True)))
    return {}


def _garbage_output(env: ContractEnv) -> Mapping[str, Any]:
    env.process_runner.script(
        BIN, "--version", stdout="aerospace CLI client version: 0.21.3-Beta\n"
    )
    env.process_runner.script(BIN, "list-workspaces", returncode=0, stdout=json.dumps({"x": 1}))
    env.process_runner.script(BIN, "list-windows", stdout="not json")
    env.process_runner.script(BIN, "list-workspaces", "--all", "--count", returncode=2)
    _install(env, FakeStreamOpener(FakeLineStream(b"garbage\n", b'{"_event":"focus-chan')))
    return {}


class TestAerospaceContract(CollectorContract):
    @pytest.fixture
    def plugin_name(self) -> str:
        return "aerospace"

    @pytest.fixture
    def working_source(self) -> ContractSource:
        return ContractSource(label="fake aerospace", arrange=_working)

    @pytest.fixture
    def broken_source(self) -> list[ContractSource]:
        return [
            ContractSource(label="binary missing", arrange=_missing_binary),
            ContractSource(label="server down", arrange=_server_down),
            ContractSource(label="garbage output", arrange=_garbage_output),
        ]
