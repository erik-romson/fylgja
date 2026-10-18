"""Shared by the journal core tests and ``scripts/gen_journal_goldens.py``: builds
:class:`~fylgja.core.journal.JournalInputs` from the synthetic JSON fixtures in
``tests/fixtures/journal/``.

A fixture holds the keyword arguments of :meth:`JournalInputs.create`. Times are ISO strings;
``trigger_source`` is a source object; ``sessions``, ``run_sections``, ``commits``, ``todos`` and
``open_items`` are lists of objects with the fields of their part types.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from fylgja.core.journal import (
    CommitPart,
    JournalInputs,
    OpenItem,
    RunSection,
    SessionPart,
    Source,
    TodoPart,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "journal"
GOLDENS = ("turn", "run", "focus")
"""Each name has ``inputs-<name>.json`` and its golden ``prompt-<name>.txt``."""


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _session(raw: dict[str, Any]) -> SessionPart:
    found = dict(raw)
    found["last_at"] = _time(found["last_at"])
    for name in ("asked", "stages", "files", "open_tasks", "done_tasks"):
        if name in found:
            found[name] = tuple(found[name])
    return SessionPart(**found)


def inputs_from(raw: dict[str, Any]) -> JournalInputs:
    """The inputs a fixture describes."""
    found = dict(raw)
    for name in ("start", "as_of"):
        found[name] = _time(found[name])
    found["trigger_source"] = Source(**found["trigger_source"])
    found["sessions"] = [_session(item) for item in found.get("sessions", [])]
    found["run_sections"] = [RunSection(**item) for item in found.get("run_sections", [])]
    found["commits"] = [
        CommitPart(**{**item, "committed_at": _time(item["committed_at"])})
        for item in found.get("commits", [])
    ]
    found["todos"] = [
        TodoPart(**{**item, "at": _time(item["at"])}) for item in found.get("todos", [])
    ]
    found["open_items"] = [
        OpenItem(**{**item, "created_at": _time(item["created_at"])})
        for item in found.get("open_items", [])
    ]
    return JournalInputs.create(**found)


def fixture_inputs(name: str) -> JournalInputs:
    """The inputs of ``tests/fixtures/journal/inputs-<name>.json``."""
    path = FIXTURES / f"inputs-{name}.json"
    return inputs_from(json.loads(path.read_text(encoding="utf-8")))


def golden_path(name: str) -> Path:
    return FIXTURES / f"prompt-{name}.txt"
