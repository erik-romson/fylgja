"""Helpers for the hook-ingress tests: a clean environment and the ``fylgja hook`` subprocess.

The tests may themselves run inside a Claude Code session, whose environment carries a real
session id, socket path and messaging token. :func:`clean_env` strips every such variable, so a
test sees only the values it sets.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from fylgja import paths

HOOK_COMMAND = [sys.executable, "-m", "fylgja._entry", "hook"]

ENVELOPE_NAME = re.compile(r"^[0-9]+-[0-9a-f-]{36}\.json$")

_STRIPPED_PREFIXES = ("CLAUDE", "CMUX_", "COVERAGE_")
_STRIPPED_NAMES = frozenset(
    {
        "ITERM_SESSION_ID",
        "TERM_SESSION_ID",
        "TERM_PROGRAM",
        "FYLGJA_SUMMARY",
        "LIDSKJALV_TRACK_SUMMARY",
    }
)


def clean_env(**extra: str) -> dict[str, str]:
    """``os.environ`` (with the test's FYLGJA_* dirs) minus agent variables, plus ``extra``."""
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in _STRIPPED_NAMES and not name.startswith(_STRIPPED_PREFIXES)
    }
    env.update(extra)
    return env


def run_hook(
    *args: str,
    stdin: bytes = b"",
    env: dict[str, str] | None = None,
    timeout: float = 20,
) -> subprocess.CompletedProcess[bytes]:
    """Run ``python -m fylgja._entry hook <args>`` to completion, capturing stdout and stderr."""
    return subprocess.run(
        [*HOOK_COMMAND, *args],
        input=stdin,
        capture_output=True,
        env=clean_env() if env is None else env,
        timeout=timeout,
        check=False,
    )


def envelope_files(source: str = "claude") -> list[Path]:
    """The envelope files in ``hook-inbox/<source>/``, in name order."""
    inbox = paths.inbox_dir(source)
    if not inbox.is_dir():
        return []
    return sorted(p for p in inbox.iterdir() if ENVELOPE_NAME.match(p.name))


def load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="ascii"))
    assert isinstance(data, dict)
    return data


def all_bytes_under(*roots: Path) -> list[tuple[Path, bytes]]:
    """Every regular file below ``roots`` with its content."""
    found: list[tuple[Path, bytes]] = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                found.append((path, path.read_bytes()))
    return found


def feed(data: bytes) -> int:
    """A read fd that yields ``data`` and then end-of-file; a thread writes, so any size works."""
    read_fd, write_fd = os.pipe()

    def write() -> None:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(write_fd, view) :]
        finally:
            os.close(write_fd)

    threading.Thread(target=write, daemon=True).start()
    return read_fd
