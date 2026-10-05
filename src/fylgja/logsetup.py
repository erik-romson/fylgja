"""Logging (``PLAN-final-plan.md`` §4.5).

Stdlib ``logging`` with a :class:`~logging.handlers.RotatingFileHandler` (5 files of 5 MB) at
:func:`fylgja.paths.log_file`, and structured context through a :class:`~logging.LoggerAdapter`
(``collector=``, ``session=``).

What is never logged:

* **prompt text, tokens and environment dumps.** Records that carry such a field (``prompt``,
  ``token``, ``env`` ...) as structured context lose it, and every message is scrubbed of the
  patterns that look like a secret (and of any literal secret registered with
  :func:`register_secret`) before it is written;
* **titles at INFO level and above.** A ``title`` field is kept for DEBUG records only.

The scrubbing is a safety net. Code that logs does not pass prompt text, titles or tokens in the
first place.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from collections.abc import Iterable, MutableMapping, Sequence
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Final

from fylgja import paths

__all__ = [
    "CONTEXT_FIELDS",
    "LOG_BACKUP_COUNT",
    "LOG_MAX_BYTES",
    "ContextAdapter",
    "PrivacyLogFilter",
    "get_logger",
    "register_secret",
    "setup_logging",
    "teardown_logging",
]

LOG_MAX_BYTES: Final = 5 * 1024 * 1024
LOG_BACKUP_COUNT: Final = 5
"""Rotated files kept next to the live one."""

CONTEXT_FIELDS: Final = ("collector", "session")
"""Structured context shown after the message as ``key=value``."""

NEVER_LOGGED: Final = frozenset({"prompt", "prompts", "token", "secret", "env", "environ"})
"""Context fields dropped from every record."""
TITLE_FIELDS: Final = frozenset({"title", "titles"})
"""Context fields kept for DEBUG records only."""

MASK: Final = "[redacted]"

_BASELINE_PATTERNS: Final = (
    r"sk-[A-Za-z0-9_-]{16,}",
    r"gh[pousr]_[A-Za-z0-9]{20,}",
    r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{16,}",
    r"(?:CLAUDE_CODE_MESSAGING_TOKEN|CMUX_SOCKET_CAPABILITY)\s*[:=]\s*\S+",
)
_HANDLER_MARK: Final = "_fylgja_handler"
_FORMAT: Final = "%(asctime)s %(levelname)s %(name)s: %(message)s"

_lock = threading.Lock()
_secrets: set[str] = set()
_previous_level: int | None = None


def register_secret(value: str) -> None:
    """Mask every occurrence of ``value`` in log messages from now on (an API secret, a token)."""
    if value:
        with _lock:
            _secrets.add(value)


class PrivacyLogFilter(logging.Filter):
    """Drops what must not be logged and masks secrets in the message."""

    def __init__(self, patterns: Iterable[str] = ()) -> None:
        super().__init__()
        self._patterns = [re.compile(p) for p in (*_BASELINE_PATTERNS, *patterns)]

    def scrub(self, text: str) -> str:
        with _lock:
            literals = sorted(_secrets, key=len, reverse=True)
        for literal in literals:
            text = text.replace(literal, MASK)
        for pattern in self._patterns:
            text = pattern.sub(MASK, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        for name in NEVER_LOGGED:
            record.__dict__.pop(name, None)
        if record.levelno > logging.DEBUG:
            for name in TITLE_FIELDS:
                record.__dict__.pop(name, None)
        record.msg = self.scrub(record.getMessage())
        record.args = None
        for name in CONTEXT_FIELDS:
            value = record.__dict__.get(name)
            if isinstance(value, str):
                record.__dict__[name] = self.scrub(value)
        return True


class _ContextFormatter(logging.Formatter):
    """The standard line plus ``collector=`` and ``session=`` when the record has them."""

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        context = [
            f"{name}={str(record.__dict__[name]).replace(chr(10), ' ')}"
            for name in CONTEXT_FIELDS
            if record.__dict__.get(name) is not None
        ]
        return f"{line} [{' '.join(context)}]" if context else line


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """A rotating log file readable by its owner only."""

    def _open(self) -> Any:
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, 0o600)
        except OSError:  # pragma: no cover - the file was just opened
            pass
        return stream


class ContextAdapter(logging.LoggerAdapter[logging.Logger]):
    """Adds the adapter's context (``collector=``, ``session=``) to every record."""

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        extra = dict(self.extra or {})
        extra.update(kwargs.get("extra") or {})
        kwargs["extra"] = extra
        return msg, kwargs


def get_logger(
    name: str, *, collector: str | None = None, session: str | None = None
) -> ContextAdapter:
    """A logger of ``name`` whose records carry ``collector=`` and ``session=`` when given."""
    context = {
        key: value
        for key, value in (("collector", collector), ("session", session))
        if value is not None
    }
    return ContextAdapter(logging.getLogger(name), context)


def setup_logging(
    *,
    level: int | str = logging.INFO,
    path: Path | None = None,
    redact_patterns: Sequence[str] = (),
) -> logging.Handler:
    """Send the root logger's records to the rotating log file (default ``paths.log_file()``).

    Calling it again replaces the handler it installed before.
    """
    global _previous_level
    target = paths.log_file() if path is None else path
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = _PrivateRotatingFileHandler(
        target, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
    )
    handler.setFormatter(_ContextFormatter(_FORMAT))
    handler.addFilter(PrivacyLogFilter(redact_patterns))
    setattr(handler, _HANDLER_MARK, True)
    root = logging.getLogger()
    _remove_handlers(root)
    root.addHandler(handler)
    if _previous_level is None:
        _previous_level = root.level
    root.setLevel(level)
    return handler


def _remove_handlers(root: logging.Logger) -> None:
    for existing in list(root.handlers):
        if getattr(existing, _HANDLER_MARK, False):
            root.removeHandler(existing)
            existing.close()


def teardown_logging() -> None:
    """Remove the handler :func:`setup_logging` installed and restore the root level."""
    global _previous_level
    root = logging.getLogger()
    _remove_handlers(root)
    if _previous_level is not None:
        root.setLevel(_previous_level)
        _previous_level = None
    with _lock:
        _secrets.clear()
