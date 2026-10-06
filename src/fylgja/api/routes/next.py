"""``GET /next`` and ``POST /next/feedback``: "what next" and its rating (WP6.2,
``PLAN-final-plan.md`` §4.2).

* ``GET /next`` (a read, no authentication): ``{"list_id": "...", "items": [...]}``. The items are
  the ``/now`` ``next[]`` items, best first, each with its ``suggestion_id``, ``kind``,
  ``target_ref``, ``score``, ``reason`` and ``rank`` (1 is the top). The list is the one ``/now``
  shows, and every list that is generated is in ``suggestions_log`` with its reasons. The list is
  empty (and not logged) when nothing waits for the person.
* ``POST /next/feedback`` (a mutating route: bearer, Origin, JSON, body limit): ``{"list_id":
  "...", "rating": "good"|"bad", "suggestion_id"?: "..."}``. The rating is recorded on the list's
  top item, or on ``suggestion_id`` when it is given, with the time (``feedback_at``). A second
  rating replaces the first. Answers **200** with ``{list_id, suggestion_id, rank, rating,
  feedback_at}``; **404** for a ``list_id`` that was never logged (or a ``suggestion_id`` that is
  not an item of that list); **422** for a body that is not exactly these fields.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, ValidationError

from fylgja.api.deps import current_now, daemon_of, json_response, not_found
from fylgja.api.now import next_items
from fylgja.api.now_models import NextItem, NowModel, dump_json
from fylgja.core.next_lists import SuggestionNotFound

__all__ = ["NextList", "Rating", "router"]

router = APIRouter()


class NextList(NowModel):
    """The answer of ``GET /next``."""

    list_id: str
    items: list[NextItem]


class Rating(NowModel):
    """The answer of ``POST /next/feedback``."""

    list_id: str
    suggestion_id: str
    rank: int
    rating: Literal["good", "bad"]
    feedback_at: datetime


class FeedbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    list_id: str
    rating: Literal["good", "bad"]
    suggestion_id: str | None = None


@router.get("/next")
async def get_next(request: Request) -> Response:
    now = current_now(request)
    if now.next is not None and now.next_list_id is not None:
        answer = NextList(list_id=now.next_list_id, items=now.next)
    else:  # no section is shown: an empty list, which is not logged
        empty = daemon_of(request).next_lists.publish([], now.generated_at)
        answer = NextList(list_id=empty.list_id, items=next_items(empty))
    return json_response(dump_json(answer))


@router.post("/next/feedback")
async def post_feedback(request: Request) -> Response:
    try:
        body = FeedbackBody.model_validate_json(await request.body())
    except ValidationError as error:
        fields = sorted({str(part) for e in error.errors() for part in e["loc"][:1]})
        raise HTTPException(
            status_code=422, detail=f"invalid body (check: {', '.join(fields) or 'json'})"
        ) from None
    try:
        rated = await daemon_of(request).next_lists.rate(
            body.list_id, body.rating, body.suggestion_id
        )
    except SuggestionNotFound:
        raise not_found("list" if body.suggestion_id is None else "suggestion") from None
    return json_response(
        dump_json(
            Rating(
                list_id=rated.list_id,
                suggestion_id=rated.suggestion_id,
                rank=rated.rank,
                rating=rated.rating,
                feedback_at=rated.feedback_at,
            )
        )
    )
