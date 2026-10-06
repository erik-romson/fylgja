"""``GET /todos`` and the todo writes ``POST /todos``, ``PATCH /todos/{id}``, ``DELETE /todos/{id}``
(WP4.1, ``PLAN-final-plan.md`` §4.2).

The read needs no authentication. The three writes are mutating routes, so the mutation guard
(bearer, Origin, JSON, body limit) has passed before any code here runs.

* ``GET /todos?ws=&project=&session=&status=``: at most one scope filter; ``status`` is a comma
  separated list of ``open``, ``next`` and ``done`` (default: every status). Deleted todos are
  never returned. The answer is a list of the ``/now`` ``Todo`` model, oldest first.
* ``POST /todos``: ``{"text": "...", "ws"|"project"|"session": "...", "status"?: "open"|"next",
  "link"?: "..."}``. Without a scope the todo belongs to the focused workspace (**409** when none is
  focused). Answers **201** with the todo.
* ``PATCH /todos/{id}``: ``{"text"?: "...", "status"?: "open"|"next"|"done"}``, at least one.
  Marking a done todo done again changes nothing. Answers **200** with the todo.
* ``DELETE /todos/{id}``: soft delete, **204**. An unknown or deleted id is **404**.

A body that is not an object with exactly these fields is **422**; the answer never repeats the
text.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, ValidationError

from fylgja.api.deps import daemon_of, json_response, not_found
from fylgja.api.now import todo_model
from fylgja.api.now_models import Todo, dump_json
from fylgja.core.todos import (
    NoFocusedWorkspace,
    TodoNotFound,
    TodoService,
    TodoValidationError,
)

__all__ = ["router"]

router = APIRouter()

_STATUS_NAMES: Final = ("open", "next", "done")


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NewTodo(_Body):
    text: str
    ws: str | None = None
    project: str | None = None
    session: str | None = None
    status: Literal["open", "next"] = "open"
    link: str | None = None


class TodoPatch(_Body):
    text: str | None = None
    status: Literal["open", "next", "done"] | None = None


def _service(request: Request) -> TodoService:
    return daemon_of(request).todos


def _scope(ws: str | None, project: str | None, session: str | None) -> tuple[str, str] | None:
    given = [
        (kind, ref)
        for kind, ref in (("workspace", ws), ("project", project), ("session", session))
        if ref is not None
    ]
    if len(given) > 1:
        raise HTTPException(status_code=422, detail="give at most one of ws, project and session")
    return given[0] if given else None


def _parse[B: _Body](model: type[B], raw: bytes) -> B:
    try:
        return model.model_validate_json(raw)
    except ValidationError as error:
        fields = sorted({str(part) for e in error.errors() for part in e["loc"][:1]})
        raise HTTPException(
            status_code=422, detail=f"invalid body (check: {', '.join(fields) or 'json'})"
        ) from None


@router.get("/todos")
async def list_todos(
    request: Request,
    ws: Annotated[str | None, Query(max_length=1024)] = None,
    project: Annotated[str | None, Query(max_length=1024)] = None,
    session: Annotated[str | None, Query(max_length=1024)] = None,
    status: Annotated[str | None, Query(max_length=64)] = None,
) -> Response:
    scope = _scope(ws, project, session)
    statuses = [s for s in (status or "").split(",") if s]
    if any(s not in _STATUS_NAMES for s in statuses):
        raise HTTPException(status_code=422, detail="status must be open, next or done")
    rows = await _service(request).list_todos(
        scope_kind=None if scope is None else scope[0],
        scope_ref=None if scope is None else scope[1],
        statuses=statuses,
    )
    return json_response(dump_json([todo_model(r) for r in rows], kind=Todo))


@router.post("/todos")
async def add_todo(request: Request) -> Response:
    body = _parse(NewTodo, await request.body())
    scope = _scope(body.ws, body.project, body.session)
    try:
        row = await _service(request).add(
            body.text,
            scope_kind=None if scope is None else scope[0],
            scope_ref=None if scope is None else scope[1],
            status=body.status,
            link=body.link,
        )
    except NoFocusedWorkspace as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except TodoValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    return json_response(dump_json(todo_model(row)), status_code=201)


@router.patch("/todos/{todo_id}")
async def patch_todo(request: Request, todo_id: str) -> Response:
    body = _parse(TodoPatch, await request.body())
    if body.text is None and body.status is None:
        raise HTTPException(status_code=422, detail="give text, status or both")
    try:
        row = await _service(request).update(todo_id, text=body.text, status=body.status)
    except TodoNotFound:
        raise not_found("todo") from None
    except TodoValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    return json_response(dump_json(todo_model(row)))


@router.delete("/todos/{todo_id}")
async def delete_todo(request: Request, todo_id: str) -> Response:
    try:
        await _service(request).delete(todo_id)
    except TodoNotFound:
        raise not_found("todo") from None
    return Response(status_code=204, headers={"cache-control": "no-store"})
