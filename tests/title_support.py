"""Helpers shared by the title-parser tests: fixture loading and comparison."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fylgja.collectors.appcontext.titles import TitleContext

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "appcontext"
TITLES_DIR = FIXTURE_ROOT / "titles"
UNREAD_DIR = FIXTURE_ROOT / "unread"


def load_items(directory: Path) -> list[tuple[str, dict[str, Any]]]:
    """Every ``(file stem, item)`` pair of the ``*.json`` files in ``directory``."""
    items: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(directory.glob("*.json")):
        for item in json.loads(path.read_text(encoding="utf-8")):
            items.append((path.stem, item))
    return items


def as_expected(context: TitleContext | None) -> dict[str, Any] | None:
    """A context in the shape the fixtures use for ``expected``."""
    if context is None:
        return None
    return {
        "kind": context.kind,
        "fields": dict(context.fields),
        "unread": context.unread,
        "confidence": context.confidence,
    }


def item_id(pair: tuple[str, dict[str, Any]]) -> str:
    stem, item = pair
    return f"{stem}:{item['title'][:40]}"
