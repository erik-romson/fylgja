"""Shared pytest configuration: marker gating and per-test environment isolation.

Convention: the ``tests/`` tree has no ``__init__.py`` files, so test module basenames must be
unique across ``unit/``, ``contract/`` and ``integration/``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from fylgja.cli.install import Layout

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_ROOT = REPO_ROOT / "target" / "tests"

# Typer forces a colour terminal when one of these is set (GitHub Actions sets the first), which
# splits option names such as ``--wait`` with escape codes. It reads them once, on import.
for _name in ("GITHUB_ACTIONS", "FORCE_COLOR", "PY_COLORS"):
    os.environ.pop(_name, None)

_GATES = {
    "slow": ("FYLGJA_RUN_SLOW", "slow test: set FYLGJA_RUN_SLOW=1 to run"),
    "live": ("FYLGJA_RUN_LIVE", "live test: set FYLGJA_RUN_LIVE=1 to run"),
}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for marker, (variable, reason) in _GATES.items():
        if os.environ.get(variable) == "1":
            continue
        skip = pytest.mark.skip(reason=reason)
        for item in items:
            if marker in item.keywords:
                item.add_marker(skip)


def _real_uv_cache_dir() -> str | None:
    configured = os.environ.get("UV_CACHE_DIR")
    if configured:
        return configured
    try:
        result = subprocess.run(
            ["uv", "cache", "dir"], capture_output=True, text=True, check=True, timeout=20
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


@pytest.fixture(scope="session")
def uv_cache_dir() -> str | None:
    """The real uv cache directory, resolved once, before ``HOME`` is moved."""
    return _real_uv_cache_dir()


@pytest.fixture(autouse=True)
def isolated_env(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    uv_cache_dir: str | None,
) -> Iterator[Path]:
    """Point HOME and the FYLGJA_*_DIR variables at a per-test directory under target/tests/."""
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.nodeid)[-80:]
    root = TEST_ROOT / f"{name}-{os.getpid()}"
    shutil.rmtree(root, ignore_errors=True)
    home = root / "home"
    for sub in ("home", "config", "data", "state"):
        (root / sub).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FYLGJA_CONFIG_DIR", str(root / "config"))
    monkeypatch.setenv("FYLGJA_DATA_DIR", str(root / "data"))
    monkeypatch.setenv("FYLGJA_STATE_DIR", str(root / "state"))
    if uv_cache_dir:
        monkeypatch.setenv("UV_CACHE_DIR", uv_cache_dir)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def fake_home(isolated_env: Path, monkeypatch: pytest.MonkeyPatch) -> Layout:
    """A fake ``$HOME`` with symlinks into a copy of ``tests/fixtures/dotfiles/``.

    ``FYLGJA_CONFIG_DIR`` points at ``<home>/.config/fylgja`` so that the config file link lands
    where dotfiles expects it, and ``FYLGJA_STATE_DIR`` at ``<home>/.local/state/fylgja``.
    """
    from dotfiles_support import build_home

    home = isolated_env / "home"
    monkeypatch.setenv("FYLGJA_CONFIG_DIR", str(home / ".config" / "fylgja"))
    monkeypatch.setenv("FYLGJA_STATE_DIR", str(home / ".local" / "state" / "fylgja"))
    return build_home(home)
