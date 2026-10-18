"""Regenerate the journal prompt goldens in ``tests/fixtures/journal/`` from the input fixtures.

Dev-only; the tests only compare. Run it by hand after a deliberate change of the journal prompt
(``fylgja.core.journal.journal_prompt``), and read the golden diff before keeping it:

    uv run --frozen python scripts/gen_journal_goldens.py

It reads ``inputs-<name>.json`` (synthetic text only) for each of ``turn``, ``run`` and ``focus``
and writes ``prompt-<name>.txt`` next to it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def _support() -> Any:
    """``tests/journal_support.py``: the fixture loader the tests use."""
    sys.path.insert(0, str(ROOT / "tests"))
    import journal_support

    return journal_support


def main() -> None:
    from fylgja.core.journal import journal_prompt

    support = _support()
    for name in support.GOLDENS:
        prompt = journal_prompt(support.fixture_inputs(name))
        support.golden_path(name).write_text(prompt + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
