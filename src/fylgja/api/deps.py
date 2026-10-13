"""What the routes share: the daemon of the app, the JSON response of the serialization rule, and
the ranked navigation items (``docs/api/items.md``)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Final

from fastapi import HTTPException, Request
from fastapi.responses import Response

from fylgja.api.item_inputs import DEFAULT_DAYS, build_items
from fylgja.api.now import build_now
from fylgja.api.now_models import Now
from fylgja.core.daemon import Daemon, ItemBuilder, RankResult
from fylgja.core.items import Item
from fylgja.rankers.base import FLOOR_NAME

__all__ = [
    "JSON",
    "current_items",
    "current_now",
    "daemon_of",
    "item_builder",
    "json_response",
    "not_found",
    "ranked_or_422",
    "unknown_ranker",
]

JSON: Final = "application/json"
_NO_STORE: Final = {"cache-control": "no-store"}


def daemon_of(request: Request) -> Daemon:
    daemon = request.app.state.daemon
    if not isinstance(daemon, Daemon):  # pragma: no cover - create_app always sets it
        raise RuntimeError("the app has no daemon")
    return daemon


def current_now(request: Request) -> Now:
    """``/now`` as of this moment (built on the event loop thread)."""
    return build_now(daemon_of(request).state())


def json_response(body: bytes, status_code: int = 200) -> Response:
    """Live data: JSON, never cached."""
    return Response(content=body, status_code=status_code, media_type=JSON, headers=_NO_STORE)


def not_found(what: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"{what} not found")


# -- the navigation items --------------------------------------------------------------------


def item_builder(daemon: Daemon, picture: Callable[[], Now], *, days: int) -> ItemBuilder:
    """What :meth:`Daemon.ranked` calls on a memo miss: the items of ``picture()`` as of the
    floored time it is given, with the window folders and the span window of ``daemon``."""

    def build(moment: datetime) -> tuple[Sequence[Item], Sequence[str]]:
        now = picture()
        workspaces = now.workspaces or ()
        folders = daemon.window_folders.snapshot(
            window.id for workspace in workspaces for window in workspace.windows
        )
        items = build_items(
            now.model_copy(update={"generated_at": moment}),
            folders,
            daemon.span_window.view(moment),
            daemon.config.items,
            days=days,
        )
        return items, [workspace.name for workspace in workspaces]

    return build


def unknown_ranker(daemon: Daemon) -> HTTPException:
    """The 422 of a ``rank`` that names no loaded ranker; the detail lists the loaded ones."""
    loaded = ", ".join(daemon.loaded_rankers) or "none"
    return HTTPException(
        status_code=422,
        detail=f"rank must name a loaded ranker ({loaded}) or the {FLOOR_NAME} floor",
    )


def ranked_or_422(request: Request, now: Now, name: str, *, days: int = DEFAULT_DAYS) -> RankResult:
    """The items of ``now`` ranked by ``name`` (memoized by the daemon); **422** when ``name`` is
    neither a loaded ranker nor the ``natural`` floor. Shared by every route that ranks."""
    daemon = daemon_of(request)
    if not daemon.can_rank(name):
        raise unknown_ranker(daemon)
    return daemon.ranked(name, item_builder(daemon, lambda: now, days=days), days=days)


def current_items(request: Request, *, days: int = DEFAULT_DAYS) -> tuple[Item, ...]:
    """The navigation items of this moment, sorted by key (the ranker input; memoized). ``/now``
    is built only when the memo has no answer for the current key."""
    daemon = daemon_of(request)
    data = daemon.rank_input(
        item_builder(daemon, lambda: current_now(request), days=days), days=days
    )
    return data.items
