"""The statusline reader (``PLAN-final-plan.md`` WP2.2, A4, §4.7).

``fylgja hook StatusLine`` atomically replaces ``statusline/<session_id>.json`` with a full
``fylgja.hook/1`` envelope whose payload is Claude's statusline input. The reader looks at the
directory every poll and reads **only the files whose mtime changed**. A file that is torn, is not
an envelope or holds none of the readings is ignored (and tried again at its next change); nothing
is ever written, deleted or quarantined here.

Each reading becomes:

* one :class:`~fylgja.models.HookEvent` named ``StatusLine`` with the session's context percentage,
  cost and pull request. The collector lays the newest context percentage and cost of each live
  session onto its activity (:meth:`StatuslineReader.readings`), which fills ``/now``
  ``sessions[].context_pct`` and ``cost_usd`` (§4.7);
* one incomplete :class:`~fylgja.models.UsageSnapshot` with a row per rate-limit window
  (``five_hour``, ``seven_day``, ``spend_limit``): ``usage.providers[]`` of §4.7. Rate limits
  belong to the account, so several sessions report the same windows and the newest reading wins in
  the store.

Both are stamped with the file's mtime, so a snapshot that is old is not mistaken for a new one. A
snapshot older than :data:`MAX_AGE` is skipped: its session is long gone.

The parser, :func:`parse_statusline`, is pure. Nothing here logs content.
"""

from __future__ import annotations

import logging
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from fylgja.collectors.base import Clock, RecordSink
from fylgja.collectors.claude.normalize import (
    Envelope,
    EnvelopeError,
    parse_envelope,
    payload_object,
    session_id_ok,
)
from fylgja.models import (
    HookEvent,
    HookEventPayload,
    SnapshotItem,
    UsagePayload,
    UsageSnapshot,
    compute_dedup_key,
)

__all__ = [
    "MAX_AGE",
    "MAX_FILE_BYTES",
    "PROVIDER",
    "RateLimit",
    "StatuslineReader",
    "StatuslineReading",
    "parse_statusline",
]

PROVIDER: Final = "claude"
USAGE_SOURCE: Final = "statusline"
RATE_LIMIT_INTERVAL: Final = 150.0
"""The rhythm of the rate limits: Claude refreshes the statusline while a session works, so a
reading is ``fresh`` for twice this long (§4.7) and ``stale`` once every session has been quiet
for longer."""
MAX_AGE: Final = 7 * 86400.0
"""Seconds after which a snapshot is not read (its session has ended)."""
MAX_FILE_BYTES: Final = 8 * 1024 * 1024
_NAME: Final = re.compile(r"([A-Za-z0-9][A-Za-z0-9_-]{0,127})\.json")
_WINDOW: Final = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}")
_MAX_WINDOWS: Final = 8


@dataclass(frozen=True)
class RateLimit:
    window: str
    used_pct: float | None
    resets_at: datetime | None


@dataclass(frozen=True)
class StatuslineReading:
    """What one statusline snapshot says. Every part is optional."""

    session_id: str
    context_pct: float | None = None
    cost_usd: float | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    cwd: str | None = None
    rate_limits: tuple[RateLimit, ...] = ()

    @property
    def empty(self) -> bool:
        return (
            self.context_pct is None
            and self.cost_usd is None
            and self.pr_number is None
            and self.pr_url is None
            and not self.rate_limits
        )


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _text(value: object, limit: int = 1024) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _time(value: object) -> datetime | None:
    """Epoch seconds (or milliseconds), or an ISO-8601 text with an offset."""
    number = _number(value)
    try:
        if number is not None:
            seconds = number / 1000 if number > 1e11 else number
            return datetime.fromtimestamp(seconds, UTC) if seconds >= 0 else None
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo is not None else None
    except (OverflowError, OSError, ValueError):
        return None
    return None


def _section(data: Mapping[str, Any], *names: str) -> Mapping[str, Any]:
    for name in names:
        value = data.get(name)
        if isinstance(value, dict):
            return value
    return {}


def _rate_limits(data: Mapping[str, Any]) -> tuple[RateLimit, ...]:
    limits: list[RateLimit] = []
    for window, entry in _section(data, "rate_limits").items():
        if len(limits) >= _MAX_WINDOWS:
            break
        if not isinstance(entry, dict) or _WINDOW.fullmatch(window) is None:
            continue
        used = _number(entry.get("used_percentage"))
        resets = _time(entry.get("resets_at"))
        if used is not None or resets is not None:
            limits.append(RateLimit(window, used, resets))
    return tuple(limits)


def parse_statusline(envelope: Envelope) -> StatuslineReading | None:
    """The reading of a statusline envelope; ``None`` when it names no session."""
    data = payload_object(envelope)
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        session_id = envelope.env.get("CLAUDE_CODE_SESSION_ID")
    if not session_id or not session_id_ok(session_id):
        return None
    pull_request = _section(data, "pr", "pull_request")
    pr_number = pull_request.get("number")
    return StatuslineReading(
        session_id=session_id,
        context_pct=_number(_section(data, "context_window").get("used_percentage")),
        cost_usd=_number(_section(data, "cost").get("total_cost_usd")),
        pr_number=pr_number
        if isinstance(pr_number, int) and not isinstance(pr_number, bool)
        else None,
        pr_url=_text(pull_request.get("url")),
        cwd=_text(data.get("cwd")),
        rate_limits=_rate_limits(data),
    )


def statusline_records(
    reading: StatuslineReading,
    envelope: Envelope,
    *,
    observed_at: datetime,
    source: str,
    kind: str,
) -> list[HookEvent | UsageSnapshot]:
    """The records one reading yields, all stamped ``observed_at``."""
    entity_id = f"{kind}:{reading.session_id}"
    pid = envelope.env.get("CLAUDE_PID", "")
    event = HookEvent(
        source=source,
        entity_id=entity_id,
        observed_at=observed_at,
        payload=HookEventPayload(
            session_id=reading.session_id,
            event="StatusLine",
            cwd=reading.cwd,
            pid=int(pid) if pid.isascii() and pid.isdigit() and len(pid) <= 9 else None,
            envelope_id=envelope.id,
            context_pct=reading.context_pct,
            cost_usd=reading.cost_usd,
            pr_number=reading.pr_number,
            pr_url=reading.pr_url,
        ),
        dedup_key=compute_dedup_key(
            source,
            "hook_event",
            entity_id,
            observed_at,
            {"envelope": envelope.id, "statusline": True},
        ),
    )
    records: list[HookEvent | UsageSnapshot] = [event]
    if reading.rate_limits:
        records.append(
            UsageSnapshot(
                source=source,
                entity_id=PROVIDER,
                observed_at=observed_at,
                complete=False,
                items=tuple(
                    SnapshotItem(
                        entity_id=f"{PROVIDER}:{limit.window}",
                        payload=UsagePayload(
                            provider=PROVIDER,
                            window=limit.window,
                            used_pct=limit.used_pct,
                            resets_at=limit.resets_at,
                            usage_source=USAGE_SOURCE,
                            poll_interval_s=RATE_LIMIT_INTERVAL,
                        ),
                    )
                    for limit in reading.rate_limits
                ),
            )
        )
    return records


class StatuslineReader:
    """Polls ``statusline/`` and emits a reading for each snapshot whose mtime changed."""

    def __init__(
        self,
        directory: Path,
        sink: RecordSink,
        clock: Clock,
        logger: logging.LoggerAdapter[logging.Logger],
        *,
        source: str = "claude",
        kind: str = "claude",
        max_age: float = MAX_AGE,
    ) -> None:
        self._directory = directory
        self._sink = sink
        self._clock = clock
        self._logger = logger
        self._source = source
        self._kind = kind
        self._max_age = max_age
        self._seen: dict[str, int] = {}
        self._latest: dict[str, tuple[float | None, float | None]] = {}
        self.emitted = 0
        """Readings emitted so far."""
        self.ignored = 0
        """Files skipped as unreadable, partial or empty at their latest look."""
        self._failure: str | None = None

    def readings(self, session_id: str) -> tuple[float | None, float | None]:
        """``(context_pct, cost_usd)`` of the newest snapshot of a session that held them (§4.7).

        Each part is the newest value seen, so a snapshot without a cost keeps the earlier one.
        ``(None, None)`` when the session has no snapshot.
        """
        return self._latest.get(session_id, (None, None))

    def _remember(self, reading: StatuslineReading) -> None:
        context_pct, cost_usd = self._latest.get(reading.session_id, (None, None))
        self._latest[reading.session_id] = (
            reading.context_pct if reading.context_pct is not None else context_pct,
            reading.cost_usd if reading.cost_usd is not None else cost_usd,
        )

    @property
    def failure(self) -> str | None:
        """Why the directory cannot be read, or ``None``. A missing directory is not a failure."""
        return self._failure

    def poll(self) -> int:
        """Read what changed. Returns the number of readings emitted."""
        try:
            names = sorted(os.listdir(self._directory))
        except FileNotFoundError:
            self._seen.clear()
            self._latest.clear()
            self._set_failure(None)
            return 0
        except OSError as exc:
            self._set_failure(f"cannot list the statusline directory ({type(exc).__name__})")
            return 0
        self._set_failure(None)
        present = {name for name in names if _NAME.fullmatch(name) is not None}
        for gone in set(self._seen) - present:
            del self._seen[gone]
            self._latest.pop(gone.removesuffix(".json"), None)
        count = 0
        now = self._clock.now().timestamp()
        for name in sorted(present):
            if self._read(name, now):
                count += 1
        return count

    def _read(self, name: str, now: float) -> bool:
        path = self._directory / name
        try:
            info = os.lstat(path)
        except OSError:
            return False
        if self._seen.get(name) == info.st_mtime_ns:
            return False
        if not _is_regular(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            self._seen[name] = info.st_mtime_ns
            return False
        if now - info.st_mtime > self._max_age:
            self._seen[name] = info.st_mtime_ns
            return False
        try:
            envelope = parse_envelope(_read_regular(path))
            reading = parse_statusline(envelope)
        except (OSError, EnvelopeError):
            self.ignored += 1
            return False  # partial or invalid: look again at the next change
        if reading is None or reading.empty:
            self.ignored += 1
            self._seen[name] = info.st_mtime_ns
            return False
        self._seen[name] = info.st_mtime_ns
        observed_at = min(datetime.fromtimestamp(info.st_mtime, UTC), self._clock.now())
        try:
            records = statusline_records(
                reading, envelope, observed_at=observed_at, source=self._source, kind=self._kind
            )
        except ValueError:
            self.ignored += 1
            return False
        self._remember(reading)
        for record in records:
            self._sink.emit(record)
        self.emitted += 1
        return True

    def _set_failure(self, failure: str | None) -> None:
        if failure is not None and self._failure is None:
            self._logger.warning("claude statusline: %s", failure)
        elif failure is None and self._failure is not None:
            self._logger.info("claude statusline recovered")
        self._failure = failure


def _is_regular(mode: int) -> bool:
    return (mode & 0o170000) == 0o100000


def _read_regular(path: Path) -> bytes:
    """The file's bytes; a symbolic link is refused."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read(MAX_FILE_BYTES + 1)
    finally:
        os.close(descriptor)
