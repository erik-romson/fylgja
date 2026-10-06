"""Times as the command line takes them (``fylgja history --since``): stdlib only.

``today`` and ``yesterday`` are local midnights, ``90m``, ``2h``, ``3d`` and ``1w`` count back from
now, and anything else is an ISO 8601 date or date and time (local time when it carries no offset).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Final

__all__ = ["parse_when"]

_RELATIVE: Final = re.compile(r"(\d{1,6})\s*([smhdw])")
_UNITS: Final = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def _midnight(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def parse_when(text: str, now: datetime | None = None) -> datetime:
    """``text`` as an aware UTC datetime. Raises :class:`ValueError` when it is none of the forms.

    ``now`` is for tests; it must be aware.
    """
    current = (datetime.now(UTC) if now is None else now).astimezone()
    word = text.strip().lower()
    if word == "today":
        return _midnight(current).astimezone(UTC)
    if word == "yesterday":
        return _midnight(current - timedelta(days=1)).astimezone(UTC)
    relative = _RELATIVE.fullmatch(word)
    if relative is not None:
        seconds = int(relative.group(1)) * _UNITS[relative.group(2)]
        return (current - timedelta(seconds=seconds)).astimezone(UTC)
    try:
        parsed = datetime.fromisoformat(text.strip())
    except ValueError:
        raise ValueError(
            f"cannot read {text!r}: use today, yesterday, 2h, 3d or an ISO 8601 date or time"
        ) from None
    return parsed.astimezone(UTC)  # a naive time is local time
