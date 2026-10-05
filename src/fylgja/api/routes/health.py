"""``GET /health``: is the daemon up (``fylgja serve`` and doctor wait on it)."""

from __future__ import annotations

from functools import cache
from importlib.metadata import PackageNotFoundError, version
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import AwareDatetime

from fylgja.api.deps import daemon_of, json_response
from fylgja.api.now_models import NOW_SCHEMA, NowModel, dump_json

__all__ = ["HealthStatus", "router"]

router = APIRouter()


class HealthStatus(NowModel):
    status: Literal["ok", "starting", "stopping"]
    """``ok`` while the daemon runs."""
    version: str
    """The installed Fylgja version."""
    now_schema: str
    """The ``/now`` contract version."""
    started_at: AwareDatetime | None = None
    live_version: int
    """The live view's change counter."""
    degraded: list[str]


@cache
def _version() -> str:
    try:
        return version("fylgja")
    except PackageNotFoundError:  # pragma: no cover - the package is always installed
        return "unknown"


@router.get("/health")
async def get_health(request: Request) -> Response:
    daemon = daemon_of(request)
    if daemon.running:
        status: Literal["ok", "starting", "stopping"] = "ok"
    elif daemon.started:
        status = "stopping"
    else:
        status = "starting"
    degraded = sorted({c.name for c in daemon.collector_states() if c.degraded})
    body = HealthStatus(
        status=status,
        version=_version(),
        now_schema=NOW_SCHEMA,
        started_at=daemon.started_at,
        live_version=daemon.view.version,
        degraded=degraded,
    )
    return json_response(dump_json(body))
