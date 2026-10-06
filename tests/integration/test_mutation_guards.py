"""The mutation security layer (WP3.4, A6).

Every request with a mutating method needs a bearer secret, an allowed ``Origin`` (or none, from the
CLI), ``Content-Type: application/json`` and a body inside the size limit. Two groups of tests:

* **route enumeration**: every mutating route registered on the real app is parametrized and must
  show every rejection and accept a valid CLI request. Today the real app has no mutating route,
  so the parametrization is empty; later steps fill it without touching this file;
* **guard mechanics**: a test-only app with the same middleware and dummy mutating routes proves
  each rejection now.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from api_support import BASE_URL, PORT, make_daemon, windows_collector
from fastapi import FastAPI, Request

from fylgja.api.app import create_app
from fylgja.api.security import (
    BODY_LIMIT,
    MESSAGE_BODY_LIMIT,
    HostGuard,
    MutationGuard,
    body_limit,
    load_secret,
    mutating_routes,
)
from fylgja.core.daemon import Daemon

SECRET = "k7Qz-test-secret-9fA2xLmN0pRsTuVwXyZ_abcdefghij"
ORIGINS = [f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"]
JSON = "application/json"
GUARD_STATUSES = {401, 403, 413, 415}
MESSAGE_PATH = "/sessions/claude:abc/message"


def _valid(**extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {SECRET}", "Content-Type": JSON, **extra}


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)


def _filled(path: str) -> str:
    return re.sub(r"\{[^}]*\}", "x", path)


def _assert_quiet(response: httpx.Response) -> None:
    """A rejection says what was missing and never repeats what was sent."""
    assert SECRET not in response.text
    assert not any(name.lower().startswith("access-control-") for name in response.headers)
    assert set(response.json()) == {"detail"}


async def assert_guarded(http: httpx.AsyncClient, method: str, path: str) -> None:
    """The five rejections and the one acceptance, for one mutating route."""

    async def send(headers: dict[str, str], content: Any = b"{}") -> httpx.Response:
        return await http.request(method, path, headers=headers, content=content)

    # a missing bearer, a wrong bearer, another scheme
    no_bearer = await send({"Content-Type": JSON})
    assert no_bearer.status_code == 401
    assert no_bearer.headers["www-authenticate"] == "Bearer"
    _assert_quiet(no_bearer)
    for authorization in (f"Bearer {SECRET[:-1]}x", "Bearer ", f"Basic {SECRET}", SECRET):
        wrong = await send({"Authorization": authorization, "Content-Type": JSON})
        assert wrong.status_code == 401, authorization
        _assert_quiet(wrong)
    # a foreign Origin, with an otherwise valid request
    for origin in ("http://evil.example", "null", f"http://127.0.0.1:{PORT + 1}"):
        foreign = await send(_valid(Origin=origin))
        assert foreign.status_code == 403, origin
        assert origin not in foreign.text
        _assert_quiet(foreign)
    # form CSRF and a missing content type
    for content_type in ("text/plain", "application/x-www-form-urlencoded", "application/jsonx"):
        form = await send(_valid() | {"Content-Type": content_type})
        assert form.status_code == 415, content_type
        _assert_quiet(form)
    headers = _valid()
    del headers["Content-Type"]
    assert (await send(headers, content=None)).status_code == 415
    # an oversize body: declared, and streamed without a Content-Length
    limit = body_limit(_filled(path))

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(limit // 1024 + 2):
            yield b" " * 1024

    declared = await send(_valid(), content=b" " * (limit + 1))
    assert declared.status_code == 413
    _assert_quiet(declared)
    streamed = await send(_valid(), content=chunks())
    assert streamed.status_code == 413
    # a valid CLI request: bearer, JSON, no Origin
    accepted = await send(_valid())
    assert accepted.status_code not in GUARD_STATUSES, accepted.status_code


# --- route enumeration -----------------------------------------------------------------------

ROUTES = mutating_routes(create_app(make_daemon(windows_collector()), secret=SECRET))


@pytest.fixture
async def daemon() -> AsyncIterator[Daemon]:
    instance = make_daemon(windows_collector())
    await instance.start()
    try:
        yield instance
    finally:
        await instance.stop()


@pytest.mark.parametrize(("method", "path"), ROUTES, ids=[f"{m} {p}" for m, p in ROUTES])
async def test_every_mutating_route_is_guarded(daemon: Daemon, method: str, path: str) -> None:
    async with _client(create_app(daemon, secret=SECRET)) as http:
        await assert_guarded(http, method, _filled(path))


async def test_a_route_added_to_the_real_app_later_is_guarded(daemon: Daemon) -> None:
    app = create_app(daemon, secret=SECRET)

    @app.post("/later")
    async def later() -> dict[str, bool]:
        return {"ok": True}

    assert ("POST", "/later") in mutating_routes(app)
    async with _client(app) as http:
        await assert_guarded(http, "POST", "/later")


async def test_the_guard_keys_on_the_method_not_on_the_route(daemon: Daemon) -> None:
    async with _client(create_app(daemon, secret=SECRET)) as http:
        # POST to a read route and to nothing at all: refused before routing, not a 405 or 404
        assert (await http.post("/now", content=b"{}")).status_code == 401
        assert (await http.request("DELETE", "/nowhere", content=b"{}")).status_code == 401
        assert (await http.post("/now", headers=_valid(), content=b"{}")).status_code == 405
        # the read routes need nothing
        for path in ("/now", "/workspaces", "/sessions", "/projects", "/collectors", "/health"):
            assert (await http.get(path)).status_code == 200, path


async def test_the_real_app_without_a_secret_refuses_every_mutation(daemon: Daemon) -> None:
    app = create_app(daemon)  # no api-secret file in the per-test config directory
    assert load_secret() is None

    @app.post("/later")
    async def later() -> dict[str, bool]:
        return {"ok": True}

    async with _client(app) as http:
        for authorization in ("Bearer ", "Bearer anything", "Bearer None"):
            response = await http.post(
                "/later", headers={"Authorization": authorization, "Content-Type": JSON}
            )
            assert response.status_code == 401


async def test_the_real_app_reads_the_secret_file_at_start(
    daemon: Daemon, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fylgja import paths

    paths.api_secret_file().parent.mkdir(parents=True, exist_ok=True)
    paths.api_secret_file().write_text(SECRET + "\n", encoding="utf-8")
    assert load_secret() == SECRET
    app = create_app(daemon)

    @app.post("/later")
    async def later() -> dict[str, bool]:
        return {"ok": True}

    async with _client(app) as http:
        assert (await http.post("/later", headers=_valid(), content=b"{}")).status_code == 200
    paths.api_secret_file().write_text("\n", encoding="utf-8")
    assert load_secret() is None


# --- guard mechanics on a test-only app ------------------------------------------------------


class Probe:
    def __init__(self) -> None:
        self.calls = 0
        self.bodies: list[bytes] = []


def build_app(
    probe: Probe, *, secret: str | None = SECRET, origins: list[str] | None = None
) -> FastAPI:
    """The dummy app: the production middleware order over dummy mutating routes."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    async def handler(request: Request) -> dict[str, Any]:
        probe.calls += 1
        probe.bodies.append(await request.body())
        return {"ok": True}

    for method in ("POST", "PUT", "PATCH", "DELETE"):
        app.add_api_route("/dummy", handler, methods=[method])
    app.add_api_route("/sessions/{key}/message", handler, methods=["POST"])
    app.add_api_route("/read", handler, methods=["GET"])
    app.add_middleware(
        MutationGuard, secret=secret, allowed_origins=ORIGINS if origins is None else origins
    )
    app.add_middleware(HostGuard, port=PORT)
    return app


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_the_dummy_routes_are_enumerated_and_guarded(method: str) -> None:
    probe = Probe()
    app = build_app(probe)
    assert (method, "/dummy") in mutating_routes(app)
    async with _client(app) as http:
        await assert_guarded(http, method, "/dummy")
    assert probe.calls == 1  # only the valid request reached the route


async def test_the_message_route_allows_64_kib_and_other_routes_16_kib() -> None:
    probe = Probe()
    async with _client(build_app(probe)) as http:
        await assert_guarded(http, "POST", MESSAGE_PATH)
        big = b" " * (BODY_LIMIT + 1)
        assert (await http.post("/dummy", headers=_valid(), content=big)).status_code == 413
        assert (await http.post(MESSAGE_PATH, headers=_valid(), content=big)).status_code == 200
        top = b" " * MESSAGE_BODY_LIMIT
        assert (await http.post(MESSAGE_PATH, headers=_valid(), content=top)).status_code == 200
        over = b" " * (MESSAGE_BODY_LIMIT + 1)
        assert (await http.post(MESSAGE_PATH, headers=_valid(), content=over)).status_code == 413
        exact = b" " * BODY_LIMIT
        assert (await http.post("/dummy", headers=_valid(), content=exact)).status_code == 200


def test_the_body_limits() -> None:
    assert (BODY_LIMIT, MESSAGE_BODY_LIMIT) == (16 * 1024, 64 * 1024)
    assert body_limit("/sessions/claude:1/message") == MESSAGE_BODY_LIMIT
    assert body_limit("/sessions/claude:1/message/") == MESSAGE_BODY_LIMIT
    for path in ("/sessions/claude:1", "/sessions/a/b/message", "/todos", "/message"):
        assert body_limit(path) == BODY_LIMIT


async def test_an_oversize_body_never_reaches_the_route() -> None:
    probe = Probe()
    async with _client(build_app(probe)) as http:
        response = await http.post("/dummy", headers=_valid(), content=b" " * (BODY_LIMIT + 1))
    assert response.status_code == 413
    assert probe.calls == 0


@pytest.mark.parametrize("origin", ORIGINS + [ORIGINS[0].upper(), ORIGINS[1] + "/"])
async def test_an_allowed_origin_with_the_bearer_is_accepted(origin: str) -> None:
    probe = Probe()
    async with _client(build_app(probe)) as http:
        response = await http.post("/dummy", headers=_valid(Origin=origin), content=b"{}")
    assert response.status_code == 200
    assert probe.bodies == [b"{}"]


async def test_the_origin_alone_is_not_enough() -> None:
    probe = Probe()
    async with _client(build_app(probe)) as http:
        response = await http.post(
            "/dummy", headers={"Origin": ORIGINS[0], "Content-Type": JSON}, content=b"{}"
        )
    assert response.status_code == 401
    assert probe.calls == 0


async def test_a_repeated_origin_is_refused() -> None:
    probe = Probe()
    headers = [
        ("Authorization", f"Bearer {SECRET}"),
        ("Content-Type", JSON),
        ("Origin", ORIGINS[0]),
        ("Origin", ORIGINS[0]),
    ]
    async with _client(build_app(probe)) as http:
        response = await http.post("/dummy", headers=headers, content=b"{}")
    assert response.status_code == 403


async def test_a_repeated_authorization_or_content_type_is_refused() -> None:
    probe = Probe()
    async with _client(build_app(probe)) as http:
        two_bearers = [("Authorization", f"Bearer {SECRET}")] * 2 + [("Content-Type", JSON)]
        assert (await http.post("/dummy", headers=two_bearers, content=b"{}")).status_code == 401
        two_types = [("Authorization", f"Bearer {SECRET}")] + [("Content-Type", JSON)] * 2
        assert (await http.post("/dummy", headers=two_types, content=b"{}")).status_code == 415
    assert probe.calls == 0


async def test_charset_and_case_do_not_break_the_json_content_type() -> None:
    probe = Probe()
    async with _client(build_app(probe)) as http:
        for content_type in ("application/json; charset=utf-8", "Application/JSON"):
            headers = _valid() | {"Content-Type": content_type}
            assert (await http.post("/dummy", headers=headers, content=b"{}")).status_code == 200
        scheme = _valid() | {"Authorization": f"bearer {SECRET}"}
        assert (await http.post("/dummy", headers=scheme, content=b"{}")).status_code == 200


async def test_the_checks_run_in_the_documented_order() -> None:
    probe = Probe()
    bad = {"Origin": "http://evil.example", "Content-Type": "text/plain"}
    async with _client(build_app(probe)) as http:
        # bearer first: an attacker without it learns nothing about the other rules
        assert (await http.post("/dummy", headers=bad, content=b"x")).status_code == 401
        with_bearer = bad | {"Authorization": f"Bearer {SECRET}"}
        assert (await http.post("/dummy", headers=with_bearer, content=b"x")).status_code == 403
        right_origin = with_bearer | {"Origin": ORIGINS[0]}
        assert (await http.post("/dummy", headers=right_origin, content=b"x")).status_code == 415
    assert probe.calls == 0


async def test_without_a_secret_every_mutation_is_refused() -> None:
    probe = Probe()
    async with _client(build_app(probe, secret=None)) as http:
        for authorization in ("Bearer ", "Bearer None", "Bearer anything", "Bearer"):
            headers = {"Authorization": authorization, "Content-Type": JSON}
            assert (await http.post("/dummy", headers=headers, content=b"{}")).status_code == 401
    async with _client(build_app(probe, secret="")) as http:
        assert (await http.post("/dummy", headers=_valid(), content=b"{}")).status_code == 401
    assert probe.calls == 0


async def test_an_empty_allowed_origins_list_refuses_every_origin() -> None:
    probe = Probe()
    async with _client(build_app(probe, origins=[])) as http:
        origin = _valid(Origin=ORIGINS[0])
        assert (await http.post("/dummy", headers=origin, content=b"{}")).status_code == 403
        assert (await http.post("/dummy", headers=_valid(), content=b"{}")).status_code == 200


async def test_reads_need_no_bearer() -> None:
    probe = Probe()
    async with _client(build_app(probe)) as http:
        for method in ("GET", "HEAD"):
            response = await http.request(method, "/read")
            assert response.status_code in (200, 405)
        assert (await http.get("/read")).status_code == 200


async def test_the_host_check_runs_before_the_mutation_guard() -> None:
    probe = Probe()
    async with _client(build_app(probe)) as http:
        for headers in (_valid(), {}):
            response = await http.post(
                "/dummy", headers=headers | {"Host": f"evil.example:{PORT}"}, content=b"{}"
            )
            assert response.status_code == 403
            assert response.json() == {"detail": "Host not allowed"}
    assert probe.calls == 0


async def test_a_rejected_request_logs_no_secret_and_no_origin(
    caplog: pytest.LogCaptureFixture,
) -> None:
    probe = Probe()
    caplog.set_level(logging.DEBUG)
    async with _client(build_app(probe)) as http:
        await http.post(
            "/dummy",
            headers={"Authorization": f"Bearer {SECRET}x", "Origin": "http://evil.example"},
            content=b"{}",
        )
        await http.post("/dummy", headers=_valid(Origin="http://evil.example"), content=b"{}")
    assert "evil.example" not in caplog.text
    assert SECRET not in caplog.text


# --- the middleware on raw ASGI scopes -------------------------------------------------------


async def _ok(scope: Any, receive: Any, send: Any) -> None:
    while (await receive())["type"] == "http.request":
        break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _scope(headers: list[tuple[bytes, bytes]], method: str = "POST") -> dict[str, Any]:
    return {"type": "http", "method": method, "path": "/dummy", "headers": headers}


def _headers(**extra: bytes) -> list[tuple[bytes, bytes]]:
    base = {b"authorization": f"Bearer {SECRET}".encode(), b"content-type": JSON.encode()}
    return list((base | {k.replace("_", "-").encode(): v for k, v in extra.items()}).items())


async def _run(scope: dict[str, Any], *messages: dict[str, Any]) -> list[dict[str, Any]]:
    guard = MutationGuard(_ok, secret=SECRET, allowed_origins=ORIGINS)
    queue = list(messages)
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await guard(scope, receive, send)
    return sent


async def test_a_garbled_content_length_is_refused_as_too_large() -> None:
    sent = await _run(_scope(_headers(content_length=b"12x")))
    assert sent[0]["status"] == 413
    sent = await _run(
        _scope(_headers(content_length=b"2")), {"type": "http.request", "body": b"{}"}
    )
    assert sent[0]["status"] == 200


async def test_a_client_that_disconnects_mid_body_is_passed_through() -> None:
    seen: list[str] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        seen.append((await receive())["type"])

    guard = MutationGuard(inner, secret=SECRET, allowed_origins=ORIGINS)

    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:  # pragma: no cover - never answered
        raise AssertionError(message)

    await guard(_scope(_headers()), receive, send)
    assert seen == ["http.disconnect"]


async def test_the_body_is_replayed_to_the_route_in_chunks() -> None:
    received: list[dict[str, Any]] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        received.append(await receive())
        received.append(await receive())
        received.append(await receive())

    guard = MutationGuard(inner, secret=SECRET, allowed_origins=ORIGINS)
    queue = [
        {"type": "http.request", "body": b"ab", "more_body": True},
        {"type": "http.request", "body": b"cd", "more_body": False},
    ]

    async def receive() -> dict[str, Any]:
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:  # pragma: no cover
        raise AssertionError(message)

    await guard(_scope(_headers()), receive, send)
    assert [m.get("body") for m in received[:2]] == [b"ab", b"cd"]
    assert received[2]["type"] == "http.disconnect"


async def test_other_scopes_and_read_methods_pass_through_untouched() -> None:
    seen: list[str] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        seen.append(f"{scope['type']}:{scope.get('method', '-')}")

    guard = MutationGuard(inner, secret=SECRET, allowed_origins=ORIGINS)
    for scope in (
        {"type": "lifespan"},
        {"type": "websocket", "headers": []},
        _scope([], method="GET"),
        _scope([], method="OPTIONS"),
        _scope([], method="HEAD"),
    ):
        await guard(scope, None, None)  # type: ignore[arg-type]
    assert seen == ["lifespan:-", "websocket:-", "http:GET", "http:OPTIONS", "http:HEAD"]


def test_mutating_routes_lists_each_method_and_skips_other_routes() -> None:
    app = build_app(Probe())
    found = mutating_routes(app)
    assert sorted(found) == [
        ("DELETE", "/dummy"),
        ("PATCH", "/dummy"),
        ("POST", "/dummy"),
        ("POST", "/sessions/{key}/message"),
        ("PUT", "/dummy"),
    ]
    assert mutating_routes(object()) == []
