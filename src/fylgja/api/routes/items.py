"""``GET /items`` and ``GET /rankers``: the ranked navigation items and the rankers
(``docs/api/items.md``, ``docs/decisions/0051``, ``docs/decisions/0052``).

Both are reads: no authentication, Host-checked like every route.

* ``GET /items?rank=&days=&q=&limit=`` answers ``fylgja.items/1``
  (:class:`~fylgja.api.items_models.Items`): the items of ``/now`` and the span window, ranked by
  ``rank`` (default: the default ranker in effect), searched by ``q`` and cut to ``limit`` after
  ranking. A ``rank`` that names neither a loaded ranker nor the ``natural`` floor is **422** and
  the detail lists the loaded rankers; so are ``days`` outside 1–14, ``limit`` outside 1–500 and a
  ``q`` over 200 characters.
* ``GET /rankers?rank=`` answers ``fylgja.rankers/1`` (:class:`~fylgja.api.items_models.Rankers`):
  every discovered ranker, the loaded ones as ``choices``, and for a client's stored ``rank`` the
  ``selected`` choice and whether the stored one is stale. A ``rank`` that names no choice is the
  input being checked, not an error; only one over 200 characters is **422**.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from fylgja.api.deps import current_now, daemon_of, json_response, ranked_or_422
from fylgja.api.item_inputs import (
    DEFAULT_DAYS,
    MAX_DAYS,
    MAX_LIMIT,
    MAX_QUERY,
    build_items_answer,
    build_rankers_answer,
)
from fylgja.api.now_models import dump_json

__all__ = ["router"]

router = APIRouter()


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


@router.get("/items")
async def get_items(
    request: Request,
    rank: str | None = None,
    days: int = DEFAULT_DAYS,
    q: str | None = None,
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
    answer = build_items_answer(
        result, now, query=q, limit=limit, summaries_enabled=daemon.summaries.enabled
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
