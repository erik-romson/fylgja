"""Filesystem locations (``PLAN-final-plan.md`` A16).

Stdlib only: the hook path imports this module, so it must stay cheap. Nothing is created on
import or by any function here; callers create directories when they write.

``FYLGJA_CONFIG_DIR``, ``FYLGJA_DATA_DIR`` and ``FYLGJA_STATE_DIR`` replace the directories that
default to ``~/.config/fylgja``, ``~/.local/share/fylgja`` and ``~/.local/state/fylgja``. The
variables are read on every call, so tests can change them freely.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

__all__ = [
    "CONFIG_DIR_ENV",
    "DATA_DIR_ENV",
    "STATE_DIR_ENV",
    "api_secret_file",
    "backups_dir",
    "config_dir",
    "config_file",
    "data_dir",
    "database_file",
    "hook_inbox_root",
    "inbox_dir",
    "log_file",
    "quarantine_dir",
    "state_dir",
    "statusline_dir",
    "summarizer_dir",
]

CONFIG_DIR_ENV = "FYLGJA_CONFIG_DIR"
DATA_DIR_ENV = "FYLGJA_DATA_DIR"
STATE_DIR_ENV = "FYLGJA_STATE_DIR"

_SOURCE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def _resolve(variable: str, default: tuple[str, ...]) -> Path:
    override = os.environ.get(variable)
    if override:
        return Path(override).expanduser()
    return Path.home().joinpath(*default)


def config_dir() -> Path:
    """Directory holding ``config.toml`` and the API secret."""
    return _resolve(CONFIG_DIR_ENV, (".config", "fylgja"))


def data_dir() -> Path:
    """Directory holding the database, the hook inbox and the statusline snapshots."""
    return _resolve(DATA_DIR_ENV, (".local", "share", "fylgja"))


def state_dir() -> Path:
    """Directory holding the log, the summarizer cwd and the installer backups."""
    return _resolve(STATE_DIR_ENV, (".local", "state", "fylgja"))


def config_file() -> Path:
    return config_dir() / "config.toml"


def api_secret_file() -> Path:
    """The API secret; the owner creates it with mode 0600."""
    return config_dir() / "api-secret"


def database_file() -> Path:
    return data_dir() / "fylgja.db"


def hook_inbox_root() -> Path:
    return data_dir() / "hook-inbox"


def inbox_dir(source: str) -> Path:
    """The hook inbox of one source, ``hook-inbox/<source>/``."""
    if _SOURCE_PATTERN.fullmatch(source) is None:
        raise ValueError(f"invalid inbox source name {source!r}")
    return hook_inbox_root() / source


def quarantine_dir(source: str) -> Path:
    """Where unreadable inbox files of ``source`` are moved."""
    return inbox_dir(source) / "quarantine"


def statusline_dir() -> Path:
    return data_dir() / "statusline"


def log_file() -> Path:
    return state_dir() / "fylgja.log"


def summarizer_dir() -> Path:
    """Working directory of summary model calls (on the default ``ignore_cwd`` list)."""
    return state_dir() / "summarizer"


def backups_dir() -> Path:
    return state_dir() / "backups"
