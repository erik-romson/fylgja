"""The shared collector contract run against the real ``fylgja-fake`` distribution."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from fylgja.testing.contract import CollectorContract, ContractEnv, ContractSource


def _write_source(env: ContractEnv, name: str, text: str) -> Path:
    path = env.tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _missing(env: ContractEnv) -> Mapping[str, Any]:
    return {"source_file": str(env.tmp_path / "does-not-exist.json")}


def _malformed(env: ContractEnv) -> Mapping[str, Any]:
    return {"source_file": str(_write_source(env, "bad.json", '{"windows": [{"id": '))}


def _wrong_shape(env: ContractEnv) -> Mapping[str, Any]:
    return {"source_file": str(_write_source(env, "shape.json", '{"windows": "nope"}'))}


def _unreadable(env: ContractEnv) -> Mapping[str, Any]:
    path = _write_source(env, "secret.json", "{}")
    path.chmod(0)
    return {"source_file": str(path)}


def _working(env: ContractEnv) -> Mapping[str, Any]:
    text = (
        '{"windows": [{"id": "w1", "title": "One", "workspace": "a"},'
        ' {"id": "w2", "title": "Two", "workspace": "b"}],'
        ' "todos": [{"id": "t1", "text": "Do it"}]}'
    )
    return {"source_file": str(_write_source(env, "ok.json", text))}


class TestFakePluginContract(CollectorContract):
    @pytest.fixture
    def plugin_name(self) -> str:
        return "fake"

    @pytest.fixture
    def working_source(self) -> ContractSource:
        return ContractSource(label="file", arrange=_working)

    @pytest.fixture
    def config_overrides(self) -> Mapping[str, Any]:
        return {"interval": 2.0}

    @pytest.fixture
    def broken_source(self) -> list[ContractSource]:
        sources = [
            ContractSource(label="missing file", arrange=_missing),
            ContractSource(label="malformed json", arrange=_malformed),
            ContractSource(label="wrong shape", arrange=_wrong_shape),
        ]
        if os.geteuid() != 0:
            sources.append(ContractSource(label="permission denied", arrange=_unreadable))
        return sources


class TestFakePluginDefaultSource(CollectorContract):
    """The built-in tick source needs no file at all."""

    @pytest.fixture
    def plugin_name(self) -> str:
        return "fake"

    @pytest.fixture
    def working_source(self) -> ContractSource:
        return ContractSource(label="built-in")

    @pytest.fixture
    def broken_source(self) -> ContractSource:
        return ContractSource(label="missing file", config={"source_file": "/nonexistent/x.json"})
