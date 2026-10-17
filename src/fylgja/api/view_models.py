"""The ``fylgja.view.overview/1`` contract: the models behind ``GET /view/overview``
(``docs/api/views.md``, ``docs/decisions/0047``).

A view model is page-shaped: it carries what one tab of the navigator page shows, already capped,
ordered, grouped and with every click target decided, so the page only renders it. ``/now``
(``fylgja.now/1``) stays the data contract; a view can change without touching it.

The serialization rule of ``fylgja.now/1`` holds here too: every model is a
:class:`~fylgja.api.now_models.NowModel`, dumped with :func:`~fylgja.api.now_models.dump_json`, so a
field without a value is absent and ``null`` never appears.

This module imports only the standard library, Pydantic and :mod:`fylgja.api.now_models`.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import AwareDatetime, Field

from fylgja.api.now_models import NextItem, NowModel

__all__ = [
    "ATTENTION_SCHEMA",
    "OVERVIEW_SCHEMA",
    "Attention",
    "Card",
    "CardContext",
    "CardProject",
    "CardSession",
    "CardTodo",
    "FocusTarget",
    "NextEntry",
    "NextList",
    "Overview",
    "TodoGroup",
    "TodoRow",
]

OVERVIEW_SCHEMA: Final = "fylgja.view.overview/1"
"""The version string ``/view/overview`` carries in ``schema``."""

ATTENTION_SCHEMA: Final = "fylgja.view.attention/1"
"""The version string ``/view/attention`` carries in ``schema``."""


class NextList(NowModel):
    """The answer of ``GET /next``, which the Overview's Next strip shows too."""

    list_id: str
    items: list[NextItem]


class FocusTarget(NowModel):
    """What a click focuses: a session (``POST /focus/session/{ref}``) or a window
    (``POST /focus/window/{ref}``)."""

    kind: Literal["session", "window"]
    ref: str
    """The session key or the AeroSpace window id."""


class NextEntry(NowModel):
    """One item of the Next strip, best first."""

    rank: int
    """1 is the top item."""
    kind: str
    """The ranking rule that produced it (``docs/api/next.md``)."""
    reason: str
    action: str
    """What the person is to do, in words."""
    title: str | None = None
    """What it is about: the session's title, the project's name, the window's app or the todo."""
    folder: str | None = None
    """The session's working folder or the project's path; a long one as ``…`` and its end."""
    since: AwareDatetime | None = None
    """When it began waiting (a todo: when it was created)."""
    go: FocusTarget | None = None
    """What a click focuses; absent when the item has nothing to focus."""


class CardProject(NowModel):
    """A project of the sessions placed in the workspace."""

    name: str
    branch: str | None = None
    dirty: bool = False
    pr_number: int | None = None
    pr_checks: str | None = None
    """The aggregate state of the PR's checks."""
    build_state: str | None = None
    """``pass``, ``fail``, ``running``, ``cancelled`` or ``none``."""
    build_failed: bool = False


class CardSession(NowModel):
    """A session placed in the workspace."""

    key: str
    state: str
    """The session state, ``unknown`` when the source gave none."""
    waiting: bool
    """The session waits for the person (permission, input or a finished turn)."""
    text: str
    """Its one-liner, else its title, name or kind."""
    child: bool = False
    """Driven by the run listed above it on the card (a norn run and its agent sessions)."""
    go: FocusTarget


class CardContext(NowModel):
    """What is open in one of the workspace's windows."""

    text: str
    """The item's title, else its kind."""
    unread: bool = False


class CardTodo(NowModel):
    """An open or next todo of the workspace."""

    text: str
    next: bool = False


class Card(NowModel):
    """One workspace with something in it: a window, a session or a todo."""

    name: str
    label: str | None = None
    focused: bool = False
    windows: int
    """How many windows the workspace has."""
    one_liner: str | None = None
    go: FocusTarget | None = None
    """The focused window of the workspace, else its first; absent without a window."""
    projects: list[CardProject]
    sessions: list[CardSession]
    """Waiting sessions first, otherwise in ``/now`` order; at most ``MAX_SESSIONS``."""
    sessions_more: int = 0
    """How many sessions the cap left out."""
    context: list[CardContext]
    """The context items of every window, in window order; at most ``MAX_CONTEXT``."""
    context_more: int = 0
    todos: list[CardTodo]
    """``next`` todos first, then oldest first; at most ``MAX_TODOS``."""
    todos_more: int = 0


class TodoRow(NowModel):
    """One open or next todo of the todo list."""

    id: str
    text: str
    next: bool = False
    source: str | None = None
    """``fylgja`` for a native todo, else the import source (``<collector>-<list kind>``)."""
    can_finish: bool
    """A native todo is finished here; an imported one only where it came from."""


class TodoGroup(NowModel):
    """A heading of the todo list and the todos under it.

    ``focused`` holds the focused workspace's todos; ``all`` is the heading over every other todo,
    which follow in one ``scope`` group per workspace, project or session, in first-seen order.
    """

    title: str
    kind: Literal["focused", "all", "scope"]
    empty: str | None = None
    """What the page shows when the group has nothing under it."""
    todos: list[TodoRow]


class Overview(NowModel):
    """The Overview tab in one call (``GET /view/overview``)."""

    schema_: Literal["fylgja.view.overview/1"] = Field(default=OVERVIEW_SCHEMA, alias="schema")
    """The contract version."""
    generated_at: AwareDatetime
    focused_workspace: str | None = None
    next: list[NextEntry]
    """What ``GET /next`` answers, with a click target per item."""
    next_list_id: str | None = None
    """The ``list_id`` of ``GET /next`` (for ``POST /next/feedback``)."""
    cards: list[Card]
    todo_groups: list[TodoGroup]


class Attention(NowModel):
    """The tab title and icon while an agent waits (``GET /view/attention``)."""

    schema_: Literal["fylgja.view.attention/1"] = Field(default=ATTENTION_SCHEMA, alias="schema")
    """The contract version."""
    generated_at: AwareDatetime
    waiting: int
    """How many ``/now`` ``waiting[]`` entries are a ``permission`` or ``input`` wait."""
    title: str
    """``(N) Fylgja`` while ``waiting`` is above 0, else ``Fylgja``."""
    icon: str
    """``/favicon-attention.png`` while ``waiting`` is above 0, else ``/favicon.png``."""
