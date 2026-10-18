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

Every journal route but the list answers **409** while ``[journal] enabled`` is false.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from fylgja.api.deps import daemon_of, json_response, not_found
from fylgja.api.journal_answers import build_thread, build_threads
from fylgja.api.now_models import dump_json
from fylgja.core.daemon import Daemon
from fylgja.core.journal_service import JournalService, ThreadIdentity, ThreadRead

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


@router.get("/journal/checkout")
async def get_checkout(request: Request, path: str) -> Response:
    daemon = daemon_of(request)
    service = journal_of(daemon)
    if not path.startswith("/"):
        raise _unprocessable("path must be absolute")
    root = daemon.locator.locate(path).git_root
    found = None if root is None else service.identity_of_path(root)
    if found is None:
        raise no_journal()
    return await _thread(daemon, service, found, DEFAULT_ENTRIES)
