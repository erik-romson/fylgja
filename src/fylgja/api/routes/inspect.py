"""``GET /inspect/*``: how collection works right now, read from the daemon's memory
(``PLAN-final-plan.md`` §4.2, ``docs/api/inspect.md``, ``docs/decisions/0048``).

Reads: no authentication, Host-checked like every route. Each answer has its own schema string,
``fylgja.inspect.<name>/1``.

* ``GET /inspect/collectors`` (``fylgja.inspect.collectors/1``): per collector its state, its
  redacted settings, what the ingest gate accepted per record kind and when, the
  ``[collectors.<name>]`` tables with no collector installed, and the AppleScript switches with
  doctor's recorded check. The switches are reported, never run.
* ``GET /inspect/pipeline`` (``fylgja.inspect.pipeline/1``): the ingest gate's totals, the
  collector × kind flow matrix, the writer, the live view, the ``/events`` clients, the summary
  service, the ``[privacy]`` lists as counts only and the daemon lifecycle.
* ``GET /inspect/explain`` (``fylgja.inspect.explain/1``): how each workspace label was chosen,
  where the resolver placed each project and session, and the Next-strip scores and lists.

No route runs ``fylgja doctor`` or ``osascript``, or reads SQLite. The one file read is doctor's
small switch state file (at most :data:`~fylgja.collectors.appcontext.switches.MAX_STATE_BYTES`),
off the event loop.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import Response

from fylgja.api import sse
from fylgja.api.deps import current_now, daemon_of, json_response
from fylgja.api.inspect import build_collectors, build_explain, build_pipeline
from fylgja.api.now_models import dump_json
from fylgja.collectors.appcontext import switches

__all__ = ["router"]

router = APIRouter(prefix="/inspect")


def _switch_records() -> dict[str, switches.SwitchRecord]:
    """Doctor's recorded AppleScript checks; empty when the file is missing or unreadable."""
    try:
        with open(switches.state_file(), "rb") as handle:
            data = handle.read(switches.MAX_STATE_BYTES)
    except OSError:
        return {}
    return switches.parse_records(data.decode("utf-8", errors="replace"))


@router.get("/collectors")
async def inspect_collectors(request: Request) -> Response:
    daemon = daemon_of(request)
    records = await asyncio.to_thread(_switch_records)
    body = build_collectors(
        daemon.registry,
        daemon.collector_states(),
        daemon.gate.all_stats(),
        daemon.config,
        privacy=daemon.privacy,
        home=daemon.home,
        switch_records=records,
        generated_at=daemon.clock.now(),
        started_at=daemon.started_at,
    )
    return json_response(dump_json(body))


@router.get("/pipeline")
async def inspect_pipeline(request: Request) -> Response:
    daemon = daemon_of(request)
    body = build_pipeline(
        daemon.gate.all_stats(),
        daemon.writer,
        daemon.view,
        daemon.summaries,
        daemon.config,
        sse_clients=sse.hub_of(request).clients,
        sse_max_clients=sse.MAX_CLIENTS,
        lifecycle=daemon.lifecycle,
        generated_at=daemon.clock.now(),
        started_at=daemon.started_at,
    )
    return json_response(dump_json(body))


@router.get("/explain")
async def inspect_explain(request: Request) -> Response:
    daemon = daemon_of(request)
    body = build_explain(current_now(request), daemon.resolution, generated_at=daemon.clock.now())
    return json_response(dump_json(body))
