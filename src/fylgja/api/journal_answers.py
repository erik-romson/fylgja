"""The journal answers, built from store rows and the service's cache (J16, J17;
``docs/api/journal.md``).

Pure: no FastAPI, no I/O and no clock (``now`` and ``tz`` are passed in). The order of the open
items, ``shown``, the day groups and ``where.state`` are computed here, so a client only renders.
A value that is absent is left out, never ``null``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, tzinfo
from typing import Any, Final

from fylgja.api.items_models import ItemJournal, ItemJournalNext
from fylgja.api.journal_models import (
    DoneDay,
    EntrySource,
    JournalEntry,
    JournalHow,
    JournalItem,
    JournalSource,
    JournalThread,
    JournalThreads,
    OpenItems,
    Recap,
    ThreadBrief,
    Where,
)
from fylgja.core.items import folder_name
from fylgja.core.journal import SHOWN_PER_KIND, done_by_day, mark_shown, order_open
from fylgja.core.journal_service import HowAnswer
from fylgja.store.rows import JournalEntryRow, JournalItemRow, JournalThreadRow, RecapRow

__all__ = [
    "build_item_journal",
    "build_thread",
    "build_threads",
    "how_model",
    "item_model",
    "open_items",
    "where_of",
]

_KINDS: Final = ("next", "remember", "blocked")


def where_of(row: JournalThreadRow | None, *, pending: bool) -> Where:
    """``pending`` while an attempt runs, ``failed`` after a failure (any earlier text is kept),
    ``fresh`` with text, else ``missing`` (J16)."""
    text = None if row is None else row.where_text
    at = None if row is None or text is None else row.where_at
    if pending:
        state = "pending"
    elif row is not None and row.error_kind is not None:
        state = "failed"
    elif text is not None:
        state = "fresh"
    else:
        state = "missing"
    return Where(text=text, generated_at=at, state=state)  # type: ignore[arg-type]


def _source(found: Mapping[str, Any]) -> JournalSource:
    return JournalSource(
        kind=found["kind"],
        ref=found["ref"],
        label=found["label"],
        path=found.get("path"),
        section=found.get("section"),
    )


def item_model(row: JournalItemRow, *, shown: bool) -> JournalItem:
    return JournalItem(
        id=row.id,
        kind=row.kind,  # type: ignore[arg-type]
        text=row.text,
        status=row.status,  # type: ignore[arg-type]
        pinned=row.pinned,
        shown=shown,
        source=_source(row.source),
        created_at=row.created_at,
        closed_at=row.closed_at,
        closed_by=row.closed_by,  # type: ignore[arg-type]
        todo_id=row.todo_id,
        has_how=row.how_text is not None,
    )


def open_items(items: Iterable[JournalItemRow]) -> OpenItems:
    """Every open item by kind, in J16 order, the first three of a kind ``shown``."""
    found: dict[str, list[JournalItem]] = {kind: [] for kind in _KINDS}
    for row, shown in mark_shown(item for item in items if item.status == "open"):
        found[row.kind].append(item_model(row, shown=shown))
    return OpenItems(next=found["next"], remember=found["remember"], blocked=found["blocked"])


def _folder(root: str) -> str:
    return folder_name(root)


def _brief(
    tid: str, row: JournalThreadRow, items: Sequence[JournalItemRow], *, pending: bool
) -> ThreadBrief:
    opened = open_items(items)
    return ThreadBrief(
        id=tid,
        name=row.name,
        folder=_folder(row.repo_root),
        branch=row.branch,
        last_at=row.last_at,
        where=where_of(row, pending=pending),
        next=[item for item in opened.next if item.shown],
        remember=[item for item in opened.remember if item.shown],
        blocked=[item for item in opened.blocked if item.shown],
        open=len(opened.next) + len(opened.remember) + len(opened.blocked),
        error_kind=row.error_kind,
    )


def build_threads(
    *, enabled: bool, threads: Iterable[tuple[JournalThreadRow, Sequence[JournalItemRow], bool]]
) -> JournalThreads:
    """``GET /journal/threads``: each thread as ``(row, its open items, pending)``, in the order
    given (newest entry first). While the journal is off: no threads."""
    if not enabled:
        return JournalThreads(enabled=False, threads=[])
    return JournalThreads(
        enabled=True,
        threads=[
            _brief(row.thread_id, row, items, pending=pending) for row, items, pending in threads
        ],
    )


def _entry(row: JournalEntryRow) -> JournalEntry:
    return JournalEntry(
        trigger=row.trigger,
        started_at=row.started_at,
        ended_at=row.ended_at,
        headline=row.headline,
        did=[str(line) for line in row.did],
        sources=[
            EntrySource(id=str(found["id"]), **_source(found).model_dump(exclude_none=True))
            for found in row.sources
        ],
    )


def build_thread(
    *,
    tid: str,
    name: str,
    branch: str,
    repo_root: str,
    row: JournalThreadRow | None,
    items: Sequence[JournalItemRow],
    pending: bool,
    entries: Sequence[JournalEntryRow],
    recent: Sequence[JournalEntryRow],
    done: Sequence[JournalItemRow],
    now: datetime,
    tz: tzinfo | None,
    recap: RecapRow | None = None,
) -> JournalThread:
    """One thread's journal. ``row`` is ``None`` for a thread that has no row yet: the empty
    journal, with ``where.state`` ``missing`` (or ``pending``). ``entries`` are the newest ones
    to show; ``recent`` and ``done`` feed "Done lately". ``recap`` is the checkout's newest."""
    days = [
        DoneDay(
            day=day.day.isoformat(),
            relative=day.relative,  # type: ignore[arg-type]
            headlines=list(day.headlines),
            items=[item_model(item, shown=False) for item in day.items],
        )
        for day in done_by_day(recent, done, now, tz)
    ]
    return JournalThread(
        enabled=True,
        id=tid,
        name=name if row is None else row.name,
        folder=_folder(repo_root),
        branch=branch,
        repo_root=repo_root,
        last_at=None if row is None else row.last_at,
        where=where_of(row, pending=pending),
        recap=None
        if recap is None
        else Recap(text=recap.text, at=recap.at, session=recap.session_key),
        open=open_items(items),
        done_recent=days,
        entries=[_entry(entry) for entry in entries],
        error_kind=None if row is None else row.error_kind,
        error_at=None if row is None else row.error_at,
    )


def build_item_journal(
    tid: str, row: JournalThreadRow, items: Sequence[JournalItemRow]
) -> ItemJournal:
    """The ``journal`` field of a folder item (D6): the thread's "where you are" and its shown
    ``next`` items, with ``next_more`` for the open ones that are not shown."""
    nexts = [item for item in order_open(items) if item.status == "open" and item.kind == "next"]
    shown = nexts[:SHOWN_PER_KIND]
    more = len(nexts) - len(shown)
    return ItemJournal(
        thread=tid,
        where=row.where_text,
        next=[ItemJournalNext(id=item.id, text=item.text) for item in shown],
        next_more=more or None,
    )


def how_model(answer: HowAnswer) -> JournalHow:
    """The "How do I do this?" answer of an item (J17, J20)."""
    return JournalHow(
        item=answer.item,
        state=answer.state,  # type: ignore[arg-type]
        steps=None if answer.steps is None else list(answer.steps),
        generated_at=answer.generated_at,
        error_kind=answer.error_kind,
    )
