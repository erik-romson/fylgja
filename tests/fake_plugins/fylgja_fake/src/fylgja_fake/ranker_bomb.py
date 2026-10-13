"""A ranker module that must never be imported.

Importing it writes a sentinel file and then raises. The ranker registry tests install this
distribution for real and assert that discovery and loading leave the sentinel absent, which proves
that a ranker that is not enabled is never imported (``docs/decisions/0052``).
"""

from __future__ import annotations

import os
from pathlib import Path


def _sentinel() -> Path:
    explicit = os.environ.get("FYLGJA_FAKE_RANKER_SENTINEL")
    if explicit:
        return Path(explicit)
    state = os.environ.get("FYLGJA_STATE_DIR")
    base = Path(state) if state else Path.home() / ".local" / "state" / "fylgja"
    return base / "fake_ranker_bomb.sentinel"


_path = _sentinel()
_path.parent.mkdir(parents=True, exist_ok=True)
_path.write_text("imported\n", encoding="utf-8")

raise RuntimeError("fylgja_fake.ranker_bomb was imported, but it is never enabled")
