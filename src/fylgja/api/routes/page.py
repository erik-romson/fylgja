"""``GET /``: the navigator page (WP4.4, WP7.1, A19; ``PLAN-final-plan.md`` §4.2 "Secret").

One static HTML file with plain JavaScript and no build step: the "next" strip, one card per
workspace, live over ``/events``, with the todo entry of WP4.4. The API secret goes into
``<meta name="fylgja-secret">`` when the page is served, so the page can send the bearer on its
writes. That is safe for the reasons §4.2 gives: the Host check turns away a foreign page (DNS
rebinding) before this route runs, and no response carries a CORS header, so no other origin can
read the page. The page is never cached, cannot be framed, loads nothing from elsewhere and may only
connect back to its own origin. Its only images are the tab icon, ``GET /favicon.png``, and the
same icon with a dot, ``GET /favicon-attention.png``, which the page swaps in while an agent waits.

The secret is read from ``app.state.secret``, which :func:`~fylgja.api.app.create_app` sets. The
answer holds the secret and is therefore never logged.
"""

from __future__ import annotations

from functools import cache
from html import escape
from importlib import resources
from typing import Final

from fastapi import APIRouter, Request
from fastapi.responses import Response

__all__ = ["PLACEHOLDER", "router"]

router = APIRouter()

PLACEHOLDER: Final = "{{fylgja-secret}}"
"""Where the static file takes the secret."""

_HEADERS: Final = {
    "cache-control": "no-store",
    "pragma": "no-cache",
    "content-security-policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    ),
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
}


@cache
def _icon(name: str) -> bytes:
    return resources.files("fylgja").joinpath(f"api/static/{name}").read_bytes()


@cache
def _template() -> str:
    return resources.files("fylgja").joinpath("api/static/index.html").read_text(encoding="utf-8")


def render_page(secret: str | None) -> str:
    """The page with ``secret`` in the meta tag (empty without a secret: writes then fail 401)."""
    return _template().replace(PLACEHOLDER, escape(secret or "", quote=True))


@router.get("/", include_in_schema=False)
async def page(request: Request) -> Response:
    secret = getattr(request.app.state, "secret", None)
    return Response(
        content=render_page(secret if isinstance(secret, str) else None),
        media_type="text/html",
        headers=_HEADERS,
    )


@router.get("/favicon.png", include_in_schema=False)
async def favicon() -> Response:
    return _icon_response("favicon.png")


@router.get("/favicon-attention.png", include_in_schema=False)
async def favicon_attention() -> Response:
    return _icon_response("favicon-attention.png")


def _icon_response(name: str) -> Response:
    return Response(
        content=_icon(name),
        media_type="image/png",
        headers={"cache-control": "max-age=86400", "x-content-type-options": "nosniff"},
    )
