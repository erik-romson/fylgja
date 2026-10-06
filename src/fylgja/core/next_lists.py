"""Generated "what next" lists: ids, the log and the feedback (WP6.2, A7, A8).

``PLAN-final-plan.md`` §4.1 (``suggestions_log``), §4.2 (``/next``, ``POST /next/feedback``).
:class:`NextLists` gives every list the ranking produces a ``list_id`` and every item a
``suggestion_id``, writes the list to ``suggestions_log`` through the writer, and records feedback
on a logged item. The CLI never touches the store: it posts to the daemon (A7).

* **One list per situation.** A list is generated when the ranking differs from the list before it
  (kind, target, score and reason of every item, in order); an unchanged ranking is the same list
  with the same ids, so a client that polls ``/now`` does not fill the log. Every list a client was
  shown is in the log with its reasons.
* **Ids** are derived from the time of generation and the content of the list, so they are the
  same for the same run of a test on a fake clock: ``list_id`` is ``nl-`` and 16 hex digits,
  ``suggestion_id`` is ``<list_id>.<rank>``.
* **An empty list** has an id but no rows to log, so feedback on it is "unknown".
* **Feedback** goes to the list's top item, or to ``suggestion_id`` when it names an item of that
  list; a second rating replaces the first. The write runs in the writer after the list's own
  insert, so a rating can never overtake the list it rates.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from fylgja.collectors.base import Clock
from fylgja.core.ranking import Candidate
from fylgja.core.writer import Tx, Writer, WriterClosed
from fylgja.store import db, queries
from fylgja.store.rows import SuggestionRow

__all__ = [
    "RATINGS",
    "RatingName",
    "NextLists",
    "Published",
    "PublishedItem",
    "Rated",
    "SuggestionNotFound",
]

logger = logging.getLogger("fylgja.next")

RatingName = Literal["good", "bad"]
RATINGS: Final[tuple[RatingName, ...]] = ("good", "bad")


class SuggestionNotFound(Exception):
    """No logged list has this ``list_id`` (or the list has no such ``suggestion_id``)."""


@dataclass(frozen=True)
class PublishedItem:
    suggestion_id: str
    rank: int
    """1 is the top item."""
    kind: str
    target_ref: str
    score: float
    reason: str


@dataclass(frozen=True)
class Published:
    """One generated list."""

    list_id: str
    generated_at: datetime
    items: tuple[PublishedItem, ...]


@dataclass(frozen=True)
class Rated:
    """What a feedback call recorded."""

    list_id: str
    suggestion_id: str
    rank: int
    rating: RatingName
    feedback_at: datetime


_Signature = tuple[tuple[str, str, float, str], ...]


def _signature(candidates: Sequence[Candidate]) -> _Signature:
    return tuple((c.kind, c.target_ref, c.score, c.reason) for c in candidates)


def _list_id(generated_at: datetime, signature: _Signature) -> str:
    digest = hashlib.sha256(f"{generated_at.isoformat()}\0{signature!r}".encode()).hexdigest()
    return f"nl-{digest[:16]}"


class NextLists:
    """See the module docstring. Call :meth:`publish` on the event loop thread."""

    def __init__(self, writer: Writer, *, clock: Clock) -> None:
        self._writer = writer
        self._clock = clock
        self._last: tuple[_Signature, Published] | None = None

    def publish(self, candidates: Sequence[Candidate], now: datetime | None = None) -> Published:
        """The list for ``candidates``: the one before when nothing changed, else a new, logged
        one."""
        signature = _signature(candidates)
        if self._last is not None and self._last[0] == signature:
            return self._last[1]
        stamp = self._clock.now() if now is None else now
        list_id = _list_id(stamp, signature)
        items = tuple(
            PublishedItem(
                suggestion_id=f"{list_id}.{rank}",
                rank=rank,
                kind=c.kind,
                target_ref=c.target_ref,
                score=c.score,
                reason=c.reason,
            )
            for rank, c in enumerate(candidates, start=1)
        )
        published = Published(list_id, stamp, items)
        if items:
            try:
                self._writer.enqueue(self._log(published), label="suggestions")
            except WriterClosed:
                return published  # nothing is written any more, so the list is not remembered
        self._last = (signature, published)
        return published

    @staticmethod
    def _log(published: Published) -> Callable[[Tx], None]:
        generated = db.to_db_time(published.generated_at)
        rows = [
            {
                "id": item.suggestion_id,
                "list_id": published.list_id,
                "generated_at": generated,
                "rank": item.rank,
                "kind": item.kind,
                "target_ref": item.target_ref,
                "score": item.score,
                "reason": item.reason,
            }
            for item in published.items
        ]

        def job(tx: Tx) -> None:
            queries.insert_suggestions(tx.connection, rows)

        return job

    async def rate(
        self, list_id: str, rating: RatingName, suggestion_id: str | None = None
    ) -> Rated:
        """Record ``rating`` on the top item of ``list_id`` (or on ``suggestion_id``).

        Raises :class:`SuggestionNotFound` for a list that was never logged and for an id that is
        not an item of that list.
        """
        if rating not in RATINGS:
            raise ValueError(f"rating must be one of {', '.join(RATINGS)}")
        stamp = self._clock.now()
        at = db.to_db_time(stamp)

        def job(tx: Tx) -> SuggestionRow:
            logged = queries.load_suggestions(tx.connection, list_id)
            if not logged:
                raise SuggestionNotFound("no such list")
            if suggestion_id is None:
                target = logged[0]
            else:
                found = [row for row in logged if row.id == suggestion_id]
                if not found:
                    raise SuggestionNotFound("no such suggestion in that list")
                target = found[0]
            queries.rate_suggestion(tx.connection, suggestion_id=target.id, rating=rating, at=at)
            return target

        row = await self._writer.execute(job)
        return Rated(list_id, row.id, row.rank, rating, stamp)

    async def logged(self, list_id: str) -> list[SuggestionRow]:
        """The logged items of ``list_id``, top first (reads the store)."""
        return await asyncio.to_thread(self._read, list_id)

    def _read(self, list_id: str) -> list[SuggestionRow]:
        connection = db.connect_reader(self._writer.database)
        try:
            return queries.load_suggestions(connection, list_id)
        finally:
            connection.close()
