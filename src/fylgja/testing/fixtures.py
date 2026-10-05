"""Load sanitized fixtures from ``tests/fixtures/`` by name (``PLAN.md`` §4.9).

A fixture name is a relative POSIX path below the fixtures directory, for example
``"ratatosk/probe-session/hook-events.ndjson"``. The directory is ``$FYLGJA_FIXTURES_DIR`` when
that is set, else the nearest ``tests/fixtures`` above the current working directory. Fixtures
are immutable: this module never writes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
from typing import Any, Final

__all__ = [
    "FIXTURES_ENV",
    "FixtureNotFound",
    "fixture_path",
    "fixtures_dir",
    "list_fixtures",
    "load_bytes",
    "load_json",
    "load_ndjson",
    "load_text",
]

FIXTURES_ENV: Final = "FYLGJA_FIXTURES_DIR"


class FixtureNotFound(FileNotFoundError):
    """The named fixture, or the fixtures directory, does not exist."""


def fixtures_dir() -> Path:
    """The fixtures directory."""
    configured = os.environ.get(FIXTURES_ENV)
    if configured:
        return Path(configured)
    start = Path.cwd().resolve()
    for parent in (start, *start.parents):
        candidate = parent / "tests" / "fixtures"
        if candidate.is_dir():
            return candidate
    raise FixtureNotFound(f"no tests/fixtures directory above {start}; set {FIXTURES_ENV}")


def fixture_path(name: str) -> Path:
    """The path of fixture ``name``. Absolute names and ``..`` are rejected."""
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError(f"invalid fixture name {name!r}")
    path = fixtures_dir().joinpath(*relative.parts)
    if not path.exists():
        raise FixtureNotFound(f"no fixture named {name!r}")
    return path


def load_bytes(name: str) -> bytes:
    return fixture_path(name).read_bytes()


def load_text(name: str) -> str:
    return fixture_path(name).read_text(encoding="utf-8")


def load_json(name: str) -> Any:
    return json.loads(load_text(name))


def load_ndjson(name: str) -> list[Any]:
    """Decoded lines of an NDJSON fixture. Blank lines are skipped."""
    return [json.loads(line) for line in load_text(name).splitlines() if line.strip()]


def list_fixtures(prefix: str = "") -> list[str]:
    """Names of the fixture files below ``prefix``, sorted."""
    root = fixtures_dir()
    base = root if not prefix else fixture_path(prefix)
    if base.is_file():
        return [base.relative_to(root).as_posix()]
    return sorted(p.relative_to(root).as_posix() for p in base.rglob("*") if p.is_file())
