"""The work journal's read routes (``docs/api/journal.md``, ``docs/decisions/0057``).

All four are reads: no authentication, Host-checked like every route. They answer
``fylgja.journal/1`` (:mod:`fylgja.api.journal_models`) from the journal service's cache and, for
one thread's entries and its "Done lately", from the store.

* ``GET /journal/threads?days=`` lists the threads with an entry in the last ``days`` (default 14,
  1–90): only each thread's shown items, with the count of open ones. While the journal is off it
  answers 200 with ``enabled: false`` and no threads. ``days`` out of range is **422**.
* ``GET /journal/threads/{id}?entries=`` is one thread's journal: ``entries`` (default 20, 1–100)
  newest entries. A thread that is the current one of a known checkout but has no row yet answers
  the empty journal. A malformed or unknown id is **404**.
* ``GET /journal/focused`` is the journal of the focused window's checkout; ``GET
  /journal/checkout?path=`` of the checkout that holds the absolute ``path`` (a relative one is
  **422**). **404** when nothing is focused or the checkout has no thread (not a git checkout, a
  detached HEAD, no branch seen yet).

The person's actions are mutating routes, behind the mutation guard (bearer, Origin, JSON, body
limit):

* ``PATCH /journal/items/{id}`` with ``{status?: open|done|dropped, pinned?: bool, text?: str}``, at
  least one; an explicit ``null`` or an empty or overlong ``text`` is **422**. Answers **200** with
  the item; a change that changes nothing writes nothing and publishes nothing.
* ``POST /journal/threads/{id}/items`` with ``{kind, text}`` adds an open item: **201**. The thread
  must have a row, else **404**.
* ``POST /journal/items/{id}/todo`` makes (or returns) the Fylgja todo of the item: **201** when it
  was made, **200** when the item already had one.
* ``POST /journal/threads/{id}/refresh`` starts a manual attempt (**202** ``pending``), joins the
  one that runs (**202**) or finds nothing new (**200** ``nothing_new``).

* ``POST /journal/items/{id}/how`` answers the item's "How do I do this?" checklist: **200**
  ``fresh`` with its steps when the stored ones match, otherwise **202** ``pending`` (a job runs,
  started now or joined). ``GET /journal/items/{id}/how`` reads its state and never starts a job.
  Both answer ``JournalHow``; the finished job publishes ``journal.changed``.

Every journal route but the list answers **409** while ``[journal] enabled`` is false.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import PurePosixPath
from typing import Final, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, ValidationError

from fylgja.api.deps import daemon_of, json_response, not_found
from fylgja.api.journal_answers import build_thread, build_threads, how_model, item_model
from fylgja.api.journal_models import JournalRefresh
from fylgja.api.now import todo_model
from fylgja.api.now_models import dump_json
from fylgja.core.daemon import Daemon
from fylgja.core.journal_service import (
    InvalidItemText,
    ItemNotFound,
    ItemResult,
    JournalService,
    ThreadIdentity,
    ThreadNotFound,
    ThreadRead,
)
from fylgja.core.project_roots import MAX_DEPTH

__all__ = ["router"]

router = APIRouter()

DEFAULT_DAYS: Final = 14
MAX_DAYS: Final = 90
DEFAULT_ENTRIES: Final = 20
MAX_ENTRIES: Final = 100
_ID: Final = re.compile(r"^[0-9a-f]{16}$")
_EMPTY: Final = ThreadRead((), (), ())


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def journal_of(daemon: Daemon) -> JournalService:
    """The journal service; **409** while the journal is off."""
    service = daemon.journal
    if service is None:
        raise HTTPException(status_code=409, detail="the journal is off")
    return service


def no_journal() -> HTTPException:
    return HTTPException(status_code=404, detail="no journal for this checkout")


async def _thread(
    daemon: Daemon, service: JournalService, found: ThreadIdentity, entries: int
) -> Response:
    brief = service.brief(found.id)
    read = _EMPTY if brief is None else await service.read_thread(found.id, entries)
    answer = build_thread(
        tid=found.id,
        name=found.name,
        branch=found.key.branch,
        repo_root=found.key.repo_root,
        row=None if brief is None else brief.row,
        items=() if brief is None else brief.open_items,
        pending=service.pending(found.id),
        entries=read.entries,
        recent=read.recent,
        done=read.done,
        now=daemon.clock.now(),
        tz=None,
        recap=service.recap(found.key),
    )
    return json_response(dump_json(answer))


@router.get("/journal/threads")
async def get_threads(request: Request, days: int = DEFAULT_DAYS) -> Response:
    if not 1 <= days <= MAX_DAYS:
        raise _unprocessable(f"days must be between 1 and {MAX_DAYS}")
    daemon = daemon_of(request)
    service = daemon.journal
    if service is None:
        return json_response(dump_json(build_threads(enabled=False, threads=())))
    found = service.recent(daemon.clock.now() - timedelta(days=days))
    answer = build_threads(
        enabled=True, threads=[(brief.row, brief.open_items, brief.pending) for brief in found]
    )
    return json_response(dump_json(answer))


@router.get("/journal/threads/{thread}")
async def get_thread(request: Request, thread: str, entries: int = DEFAULT_ENTRIES) -> Response:
    daemon = daemon_of(request)
    service = journal_of(daemon)
    if not 1 <= entries <= MAX_ENTRIES:
        raise _unprocessable(f"entries must be between 1 and {MAX_ENTRIES}")
    found = service.identity(thread) if _ID.fullmatch(thread) else None
    if found is None:
        raise not_found("journal thread")
    return await _thread(daemon, service, found, entries)


@router.get("/journal/focused")
async def get_focused(request: Request) -> Response:
    daemon = daemon_of(request)
    service = journal_of(daemon)
    focused = daemon.view.focused
    if focused is None or focused.window_id is None:
        raise HTTPException(status_code=404, detail="nothing is focused")
    path = daemon.focused_project()
    found = None if path is None else service.identity_of_path(path)
    if found is None:
        raise no_journal()
    return await _thread(daemon, service, found, DEFAULT_ENTRIES)


def _known_checkout(daemon: Daemon, path: str) -> str | None:
    """The nearest checkout the daemon tracks that holds ``path``. The path is matched against
    the known checkouts, never looked up on disk, so any page that can send a GET cannot make the
    daemon probe the filesystem or learn which of its folders exist."""
    projects = daemon.view.projects
    start = PurePosixPath(path)
    for directory in (start, *start.parents)[:MAX_DEPTH]:
        if str(directory) in projects:
            return str(directory)
    return None


@router.get("/journal/checkout")
async def get_checkout(request: Request, path: str) -> Response:
    daemon = daemon_of(request)
    service = journal_of(daemon)
    if not path.startswith("/"):
        raise _unprocessable("path must be absolute")
    root = _known_checkout(daemon, path)
    found = None if root is None else service.identity_of_path(root)
    if found is None:
        raise no_journal()
    return await _thread(daemon, service, found, DEFAULT_ENTRIES)


# -- the person's actions (J18, J19) ----------------------------------------------------------


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ItemPatch(_Body):
    """The fields are judged by ``model_fields_set``; none accepts ``null``."""

    status: Literal["open", "done", "dropped"] = "open"
    pinned: bool = False
    text: str = ""


class NewItem(_Body):
    kind: Literal["next", "remember", "blocked"]
    text: str


def _parse[B: _Body](model: type[B], raw: bytes) -> B:
    try:
        return model.model_validate_json(raw)
    except ValidationError as error:
        fields = sorted({str(part) for e in error.errors() for part in e["loc"][:1]})
        raise _unprocessable(f"invalid body (check: {', '.join(fields) or 'json'})") from None


_BAD_TEXT: Final = "the text must be 1 to 200 characters"


def _item_response(result: ItemResult, status_code: int = 200) -> Response:
    return json_response(
        dump_json(item_model(result.item, shown=result.shown)), status_code=status_code
    )


@router.patch("/journal/items/{item}")
async def patch_item(request: Request, item: str) -> Response:
    service = journal_of(daemon_of(request))
    body = _parse(ItemPatch, await request.body())
    given = body.model_fields_set
    if not given:
        raise _unprocessable("give status, pinned, text or several")
    try:
        result = await service.patch_item(
            item,
            status=body.status if "status" in given else None,
            pinned=body.pinned if "pinned" in given else None,
            text=body.text if "text" in given else None,
        )
    except ItemNotFound:
        raise not_found("journal item") from None
    except InvalidItemText:
        raise _unprocessable(_BAD_TEXT) from None
    return _item_response(result)


@router.post("/journal/threads/{thread}/items")
async def add_item(request: Request, thread: str) -> Response:
    service = journal_of(daemon_of(request))
    body = _parse(NewItem, await request.body())
    try:
        result = await service.add_item(thread, body.kind, body.text)
    except ThreadNotFound:
        raise not_found("journal thread") from None
    except InvalidItemText:
        raise _unprocessable(_BAD_TEXT) from None
    return _item_response(result, 201)


@router.post("/journal/items/{item}/todo")
async def make_todo(request: Request, item: str) -> Response:
    service = journal_of(daemon_of(request))
    try:
        todo, made = await service.make_todo(item)
    except ItemNotFound:
        raise not_found("journal item") from None
    return json_response(dump_json(todo_model(todo)), status_code=201 if made else 200)


@router.post("/journal/threads/{thread}/refresh")
async def refresh_thread(request: Request, thread: str) -> Response:
    service = journal_of(daemon_of(request))
    try:
        if not _ID.fullmatch(thread):
            raise ThreadNotFound(thread)
        state = await service.refresh(thread)
    except ThreadNotFound:
        raise not_found("journal thread") from None
    answer = JournalRefresh(thread=thread, state=state)  # type: ignore[arg-type]
    return json_response(dump_json(answer), status_code=200 if state == "nothing_new" else 202)


@router.post("/journal/items/{item}/how")
async def request_how(request: Request, item: str) -> Response:
    service = journal_of(daemon_of(request))
    try:
        answer = await service.request_how(item)
    except ItemNotFound:
        raise not_found("journal item") from None
    return json_response(
        dump_json(how_model(answer)), status_code=200 if answer.state == "fresh" else 202
    )


@router.get("/journal/items/{item}/how")
async def get_how(request: Request, item: str) -> Response:
    service = journal_of(daemon_of(request))
    try:
        answer = await service.how(item)
    except ItemNotFound:
        raise not_found("journal item") from None
    return json_response(dump_json(how_model(answer)))
