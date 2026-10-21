"""``GET /items`` and ``GET /rankers``: the ranked navigation items and the rankers
(``docs/api/items.md``, ``docs/decisions/0051``, ``docs/decisions/0052``).

Both are reads: no authentication, Host-checked like every route.

* ``GET /items?rank=&days=&q=&open=&limit=`` answers ``fylgja.items/1``
  (:class:`~fylgja.api.items_models.Items`): the items of ``/now`` and the span window, ranked by
  ``rank`` (default: the default ranker in effect), searched by ``q``, kept to those with an open
  place by ``open`` and cut to ``limit`` after ranking. A ``rank`` that names neither a loaded
  ranker nor the ``natural`` floor is **422** and the detail lists the loaded rankers; so are
  ``days`` outside 1–14, ``limit`` outside 1–500, a ``q`` over 200 characters and an ``open``
  that is not a boolean. While summaries are enabled an item carries its cached folder summary
  as ``story`` (``docs/decisions/0053``), judged against the inputs ``POST
  /summaries/folder/{id}`` would take (the items of the default ``days``); nothing is generated.
* ``GET /rankers?rank=`` answers ``fylgja.rankers/1`` (:class:`~fylgja.api.items_models.Rankers`):
  every discovered ranker, the loaded ones as ``choices``, and for a client's stored ``rank`` the
  ``selected`` choice and whether the stored one is stale. A ``rank`` that names no choice is the
  input being checked, not an error; only one over 200 characters is **422**.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from fylgja.api.deps import current_items, current_now, daemon_of, json_response, ranked_or_422
from fylgja.api.item_inputs import (
    DEFAULT_DAYS,
    MAX_DAYS,
    MAX_LIMIT,
    MAX_QUERY,
    build_items_answer,
    build_rankers_answer,
)
from fylgja.api.items_models import ItemJournal, ItemStory
from fylgja.api.journal_answers import build_item_journal
from fylgja.api.now_models import dump_json
from fylgja.api.summary_inputs import item_stories
from fylgja.core.daemon import Daemon
from fylgja.core.items import Item
from fylgja.core.journal import thread_id

__all__ = ["router"]

router = APIRouter()


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def _journals(daemon: Daemon, items: Sequence[Item]) -> dict[str, ItemJournal]:
    """The ``journal`` field of the folder items that are checkouts with a thread row, from the
    journal service's cache (D6); nothing while the journal is off. No I/O."""
    service = daemon.journal
    found: dict[str, ItemJournal] = {}
    if service is None:
        return found
    for item in items:
        key = service.thread_for_path(item.path) if item.kind == "folder" and item.path else None
        if key is None:
            continue
        brief = service.brief(thread_id(key))
        if brief is not None:
            found[item.id] = build_item_journal(
                brief.row.thread_id, brief.row, brief.open_items, service.recap(key)
            )
    return found


@router.get("/items")
async def get_items(
    request: Request,
    rank: str | None = None,
    days: int = DEFAULT_DAYS,
    q: str | None = None,
    only_open: Annotated[bool, Query(alias="open")] = False,
    limit: int | None = None,
) -> Response:
    if not 1 <= days <= MAX_DAYS:
        raise _unprocessable(f"days must be between 1 and {MAX_DAYS}")
    if limit is not None and not 1 <= limit <= MAX_LIMIT:
        raise _unprocessable(f"limit must be between 1 and {MAX_LIMIT}")
    if q is not None and len(q) > MAX_QUERY:
        raise _unprocessable(f"q may be at most {MAX_QUERY} characters")
    daemon = daemon_of(request)
    now = current_now(request)
    name = rank or daemon.effective_default_ranker
    result = ranked_or_422(request, now, name, days=days)
    service = daemon.summaries
    stories: dict[str, ItemStory] = {}
    if service.enabled:  # the summary route's items: those of the default days
        pool = result.items if days == DEFAULT_DAYS else current_items(request)
        stories = item_stories(service, pool, now)
    answer = build_items_answer(
        result,
        now,
        query=q,
        only_open=only_open,
        limit=limit,
        summaries_enabled=service.enabled,
        stories=stories,
        journals=_journals(daemon, result.items),
    )
    return json_response(dump_json(answer))


@router.get("/rankers")
async def get_rankers(request: Request, rank: str | None = None) -> Response:
    if rank is not None and len(rank) > MAX_QUERY:
        raise _unprocessable(f"rank may be at most {MAX_QUERY} characters")
    daemon = daemon_of(request)
    answer = build_rankers_answer(
        daemon.rankers,
        default=daemon.effective_default_ranker,
        configured_default=daemon.config.items.default_ranker,
        rank=rank or None,
    )
    return json_response(dump_json(answer))
