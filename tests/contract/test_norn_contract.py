"""The shared collector contract run against the built-in ``norn`` collector.

The working source is a directory with the sanitized run files of a crashed run (no plugin) plus
the subscriber's envelopes in the hook inbox. ``ps`` answers from the fake runner and pid liveness
is a seam; no norn and no process is needed.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from norn_support import INBOX_NOW, copy_case, deliver, index_of, ps_table

from fylgja import paths
from fylgja.collectors.norn import adapter
from fylgja.testing.contract import CollectorContract, ContractEnv, ContractSource


def _install(env: ContractEnv, *, with_ps: bool = True) -> None:
    seams = adapter.Seams(env.process_runner, adapter.LocalFileSystem(), pid_alive=lambda pid: True)
    env.monkeypatch.setattr(adapter, "default_seams", lambda: seams)
    # The fixtures are relative to one instant; the contract's fake clock starts elsewhere.
    env.clock = type(env.clock)(INBOX_NOW)
    if with_ps:
        env.process_runner.script("/bin/ps", stdout=ps_table((1, "/sbin/launchd")))
        env.process_runner.script("/usr/sbin/lsof", stdout="")


def _root(env: ContractEnv) -> Path:
    root = env.tmp_path / "projects"
    if not root.exists():
        root.mkdir()
        copy_case(root, "crashed", at=INBOX_NOW)
    return root


def _working(env: ContractEnv) -> Mapping[str, Any]:
    _install(env)
    root = _root(env)
    inbox = paths.inbox_dir("norn")
    shutil.rmtree(inbox, ignore_errors=True)
    deliver(inbox, "lifecycle", stop=index_of("lifecycle", "Heartbeat") + 1)
    return {"roots": [str(root)], "inbox_watch": "poll"}


def _roots_missing(env: ContractEnv) -> Mapping[str, Any]:
    _install(env)
    return {"roots": [str(env.tmp_path / "missing")], "inbox_watch": "poll"}


def _ps_missing(env: ContractEnv) -> Mapping[str, Any]:
    _install(env, with_ps=False)  # an unfinished run in the files needs the process table
    return {"roots": [str(_root(env))], "inbox_watch": "poll"}


def _ps_failing(env: ContractEnv) -> Mapping[str, Any]:
    _install(env, with_ps=False)
    env.process_runner.script("/bin/ps", returncode=3, stderr="ps: broken")
    return {"roots": [str(_root(env))], "inbox_watch": "poll"}


def _inbox_not_a_directory(env: ContractEnv) -> Mapping[str, Any]:
    _install(env)
    inbox = paths.inbox_dir("norn")
    shutil.rmtree(inbox, ignore_errors=True)
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text("not a directory", encoding="utf-8")
    return {"roots": [str(_root(env))], "inbox_watch": "poll"}


class TestNornContract(CollectorContract):
    @pytest.fixture
    def plugin_name(self) -> str:
        return "norn"

    @pytest.fixture
    def working_source(self) -> ContractSource:
        return ContractSource(label="fixture run files and inbox", arrange=_working)

    @pytest.fixture
    def broken_source(self) -> list[ContractSource]:
        return [
            ContractSource(label="no scan root exists", arrange=_roots_missing),
            ContractSource(label="ps missing", arrange=_ps_missing),
            ContractSource(label="ps failing", arrange=_ps_failing),
            ContractSource(label="inbox is not a directory", arrange=_inbox_not_a_directory),
        ]
