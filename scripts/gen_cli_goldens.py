"""Regenerate the golden renderings in ``tests/fixtures/cli/`` from the fixture payloads.

Run by hand after a deliberate change of ``fylgja.cli.render``; the tests only compare:

    uv run --frozen python scripts/gen_cli_goldens.py
"""

from __future__ import annotations

import json
import os
import time
from datetime import date
from pathlib import Path

from fylgja.cli import render

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "cli"
WIDTHS = (100, 60)
JOURNAL_TODAY = date(2026, 10, 18)
"""The "today" the journal golden is rendered for, in UTC (the test sets the same zone)."""


def _load(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def main() -> None:
    os.environ["TZ"] = "UTC"
    time.tzset()
    now = _load("now.json")
    collectors = _load("collectors.json")
    assert isinstance(now, dict)
    assert isinstance(collectors, list)
    for width in WIDTHS:
        outputs = {
            f"now-{width}.txt": render.render_now(now, width),
            f"workspaces-{width}.txt": render.render_workspaces(now["workspaces"], width),
            f"workspace-2-{width}.txt": render.render_workspace(
                now["workspaces"][1], now["sessions"], width
            ),
            f"sessions-{width}.txt": render.render_sessions(now["sessions"], width),
            f"collectors-{width}.txt": render.render_collectors(collectors, width),
        }
        for name, text in outputs.items():
            (FIXTURES / name).write_text(text + "\n", encoding="utf-8")
    journal = _load("journal.json")
    assert isinstance(journal, dict)
    text = render.render_journal(journal, 100, JOURNAL_TODAY)
    (FIXTURES / "journal-100.txt").write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
