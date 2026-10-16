"""Keeps docs/api/README.md in step with the routes the API registers."""

from __future__ import annotations

import re
from pathlib import Path

from api_support import make_daemon

from fylgja.api.app import create_app
from fylgja.api.security import http_routes

REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX = REPO_ROOT / "docs" / "api" / "README.md"

_DOC_ROUTE = re.compile(r"`(GET|POST|PUT|PATCH|DELETE) (/[^`\s?]*)")
_PATH_PARAM = re.compile(r"\{[^}]*\}")


def _normal(method: str, path: str) -> tuple[str, str]:
    """``(method, path)`` with every path parameter as ``{}``: the index may name it ``{id}``."""
    return method, _PATH_PARAM.sub("{}", path)


def _app_routes() -> set[tuple[str, str]]:
    return {_normal(m, p) for m, p in http_routes(create_app(make_daemon(), secret="x"))}


def _indexed_routes() -> set[tuple[str, str]]:
    return {_normal(m, p) for m, p in _DOC_ROUTE.findall(INDEX.read_text(encoding="utf-8"))}


def test_every_route_is_in_the_api_index() -> None:
    missing = sorted(_app_routes() - _indexed_routes())
    assert not missing, f"add these routes to docs/api/README.md: {missing}"


def test_the_api_index_names_no_route_that_is_gone() -> None:
    stale = sorted(_indexed_routes() - _app_routes())
    assert not stale, f"docs/api/README.md names routes the API does not have: {stale}"
