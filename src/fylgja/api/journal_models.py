"""The ``fylgja.journal/1`` contract: the models behind the journal routes (``docs/api/journal.md``,
``docs/decisions/0057``).

The serialization rule of ``fylgja.now/1`` holds here too: every model is a
:class:`~fylgja.api.now_models.NowModel`, dumped with :func:`~fylgja.api.now_models.dump_json`, so a
field without a value is absent and ``null`` never appears. Nothing here is reachable from
:class:`~fylgja.api.now_models.Now`, so ``/now`` is unchanged.

This module imports only the standard library, Pydantic and :mod:`fylgja.api.now_models`.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import AwareDatetime, Field

from fylgja.api.now_models import NowModel

__all__ = [
    "JOURNAL_SCHEMA",
    "DoneDay",
    "EntrySource",
    "JournalEntry",
    "JournalHow",
    "JournalItem",
    "JournalRefresh",
    "JournalSource",
    "JournalThread",
    "JournalThreads",
    "OpenItems",
    "ThreadBrief",
    "Where",
]

JOURNAL_SCHEMA: Final = "fylgja.journal/1"
"""The version string the journal answers carry in ``schema``."""


class JournalSource(NowModel):
    """What an item or an entry is based on."""

    kind: Literal["session", "run", "commit", "todo", "file", "thread", "person"]
    ref: str
    """The session key, the full commit hash, the todo id, the thread id or the file path."""
    label: str
    """A short redacted text: a title, a subject or a todo text."""
    path: str | None = None
    """Set for a ``file`` source."""
    section: str | None = None
    """Set for a ``file`` source: the heading the text came from."""


class EntrySource(JournalSource):
    """A source of an entry, with the ``S`` id the entry cited it by."""

    id: str


class JournalItem(NowModel):
    """One item of a thread's ledger."""

    id: str
    kind: Literal["next", "remember", "blocked"]
    text: str
    status: Literal["open", "done", "dropped"]
    pinned: bool
    shown: bool
    """Among the first three open items of its kind."""
    source: JournalSource
    created_at: AwareDatetime
    closed_at: AwareDatetime | None = None
    closed_by: Literal["model", "user", "fylgja"] | None = None
    todo_id: str | None = None
    """The Fylgja todo made from the item."""
    has_how: bool
    """A "How do I do this?" answer is stored."""


class Where(NowModel):
    """ "Where you are" of a thread."""

    text: str | None = None
    generated_at: AwareDatetime | None = None
    state: Literal["pending", "failed", "fresh", "missing"]
    """``pending``: an attempt is running. ``failed``: the last attempt failed (earlier text is
    kept). ``fresh``: there is text. ``missing``: otherwise."""


class ThreadBrief(NowModel):
    """A thread in the list: only the shown items of each kind."""

    id: str
    name: str
    folder: str
    branch: str
    last_at: AwareDatetime | None = None
    where: Where | None = None
    next: list[JournalItem]
    remember: list[JournalItem]
    blocked: list[JournalItem]
    open: int
    """How many items are open, shown or not."""
    error_kind: str | None = None


class JournalThreads(NowModel):
    """The threads with an entry lately (``GET /journal/threads``)."""

    schema_: Literal["fylgja.journal/1"] = Field(default=JOURNAL_SCHEMA, alias="schema")
    """The contract version."""
    enabled: bool
    threads: list[ThreadBrief]
    """Newest entry first."""


class OpenItems(NowModel):
    """Every open item of a thread, in order, by kind."""

    next: list[JournalItem]
    remember: list[JournalItem]
    blocked: list[JournalItem]


class DoneDay(NowModel):
    """One local day of "Done lately"."""

    day: str
    """``YYYY-MM-DD``, local time."""
    relative: Literal["today", "yesterday"] | None = None
    headlines: list[str]
    """Up to three entry headlines of the day, newest first."""
    items: list[JournalItem]
    """The items closed as done that day, newest first."""


class JournalEntry(NowModel):
    """One entry of a thread."""

    trigger: str
    started_at: AwareDatetime
    ended_at: AwareDatetime
    headline: str
    did: list[str]
    sources: list[EntrySource]


class JournalThread(NowModel):
    """One thread's journal (``GET /journal/threads/{id}``, ``/journal/focused``,
    ``/journal/checkout``)."""

    schema_: Literal["fylgja.journal/1"] = Field(default=JOURNAL_SCHEMA, alias="schema")
    """The contract version."""
    enabled: bool
    id: str
    name: str
    folder: str
    branch: str
    repo_root: str
    last_at: AwareDatetime | None = None
    where: Where
    open: OpenItems
    done_recent: list[DoneDay]
    entries: list[JournalEntry]
    """Newest first."""
    error_kind: str | None = None
    error_at: AwareDatetime | None = None


class JournalHow(NowModel):
    """The "How do I do this?" checklist of an item."""

    item: str
    state: Literal["fresh", "stale", "pending", "failed", "missing"]
    steps: list[str] | None = None
    generated_at: AwareDatetime | None = None
    error_kind: str | None = None


class JournalRefresh(NowModel):
    """The answer of ``POST /journal/threads/{id}/refresh``."""

    thread: str
    state: Literal["pending", "nothing_new"]
    """``pending``: an attempt runs (started now or joined). ``nothing_new``: the window holds
    nothing, so no call was made."""
