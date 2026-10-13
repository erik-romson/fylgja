"""Hand-built inputs for the frecency tests: items, spans and a ``RankInput``."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta

from fylgja.core.activity import build_activity
from fylgja.core.items import Item, Place, item_id
from fylgja.rankers.base import Ranked, RankInput
from fylgja.rankers.frecency import FrecencySettings, rank
from fylgja.store.rows import SpanRow

DAY = datetime(2026, 10, 12, 0, 0, tzinfo=UTC)
IDLE = timedelta(minutes=5)
BREAK = timedelta(minutes=30)
ALPHA = "folder:/work/alpha/main"
BETA = "folder:/work/beta/main"
GAMMA = "folder:/work/gamma/main"
DELTA = "folder:/work/delta/main"
APP = "app:com.example.app"


def at(hours: float, minutes: float = 0.0, seconds: float = 0.0) -> datetime:
    """A moment on the fixed day: ``at(9, 30)`` is 09:30:00."""
    return DAY + timedelta(hours=hours, minutes=minutes, seconds=seconds)


def span(key: str, start: datetime, end: datetime | None) -> SpanRow:
    folder = key.startswith("folder:")
    return SpanRow(
        window_id=None,
        workspace=None,
        app_bundle_id=None if folder else key.removeprefix("app:"),
        title=None,
        project_path=key.removeprefix("folder:") if folder else None,
        session_key=None,
        started_at=start,
        ended_at=end,
        end_reason=None if end is None else "focus_change",
    )


def item(
    key: str,
    *,
    state: Iterable[str] = (),
    last_focus_at: datetime | None = None,
    window: str | None = None,
    open: bool = True,
) -> Item:
    folder = key.startswith("folder:")
    places = (
        ()
        if window is None
        else (
            Place(
                kind="window",
                ref=window,
                role="app",
                label="App",
                title=None,
                workspace="1",
                open=open,
                waiting=False,
                last_focus_at=last_focus_at,
            ),
        )
    )
    return Item(
        id=item_id(key),
        key=key,
        kind="folder" if folder else "app",
        name=key,
        path=key.removeprefix("folder:") if folder else None,
        group=None,
        tickets=(),
        places=places,
        state=frozenset(state),
        last_focus_at=last_focus_at,
        seconds_today=0.0,
        summary="",
        go=None,
        go_by_role={},
    )


def world(
    now: datetime, items: Sequence[Item], spans: Sequence[SpanRow], *, suspended: bool = False
) -> RankInput:
    activity = build_activity(
        spans, now=now, suspended=suspended, idle_threshold=IDLE, session_break=BREAK
    )
    return RankInput(now=now, items=tuple(items), activity=activity, workspaces=("1",))


def ranked(data: RankInput, **options: float) -> list[Ranked]:
    return rank(data, FrecencySettings(**options))


def parts(row: Ranked) -> dict[str, float]:
    return {part.name: part.value for part in row.parts}


def by_key(rows: Sequence[Ranked]) -> dict[str, Ranked]:
    return {row.key: row for row in rows}
