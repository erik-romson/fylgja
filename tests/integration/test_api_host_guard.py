"""The A6 Host guard and the absence of CORS (WP1.9).

A DNS-rebinding page reaches the loopback port with its own name in ``Host``; the guard must turn
it away before any route runs. Only ``127.0.0.1:<port>`` and ``localhost:<port>`` pass, and no
response ever carries an ``Access-Control-*`` header.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from api_support import BASE_URL, PORT, T0, Win, make_daemon, window_snapshot, windows_collector
from fastapi.responses import Response

from fylgja.api.app import create_app, server_config
from fylgja.api.security import HostGuard, allowed_hosts
from fylgja.config import ConfigError, parse_config
from fylgja.core.daemon import Daemon

READ_ROUTES = (
    "/now",
    "/workspaces",
    "/sessions",
    "/projects",
    "/collectors",
    "/health",
    "/view/overview",
)


@pytest.fixture
async def daemon() -> AsyncIterator[Daemon]:
    instance = make_daemon(windows_collector())
    await instance.start()
    instance.sink("wm").emit(window_snapshot("wm", [Win("1", "1", "App", 10, "Secret title")], T0))
    await instance.settle()
    try:
        yield instance
    finally:
        await instance.stop()


def _client(app: Any, base_url: str = BASE_URL) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url)


@pytest.mark.parametrize(
    "host",
    [
        f"evil.example:{PORT}",  # the DNS-rebinding case: the attacker's name, our port
        "evil.example",
        f"127.0.0.1.evil.example:{PORT}",
        "127.0.0.1:8080",
        "127.0.0.1",
        f"localhost.:{PORT}",
        f"0.0.0.0:{PORT}",
        f"[::1]:{PORT}",
        "",
    ],
)
async def test_a_foreign_host_is_rejected_before_any_route(daemon: Daemon, host: str) -> None:
    async with _client(create_app(daemon)) as http:
        for path in READ_ROUTES:
            response = await http.get(path, headers={"Host": host})
            assert response.status_code == 403, (host, path)
            assert response.json() == {"detail": "Host not allowed"}
            assert "Secret title" not in response.text


@pytest.mark.parametrize("host", [f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"LocalHost:{PORT}"])
async def test_both_allowed_hosts_are_accepted(daemon: Daemon, host: str) -> None:
    async with _client(create_app(daemon)) as http:
        for path in READ_ROUTES:
            response = await http.get(path, headers={"Host": host})
            assert response.status_code == 200, (host, path)


async def test_no_response_carries_cors_headers(daemon: Daemon) -> None:
    origin = {"Origin": "http://evil.example"}
    preflight = {
        **origin,
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization",
    }
    async with _client(create_app(daemon)) as http:
        responses = [await http.get(path, headers=origin) for path in READ_ROUTES]
        responses += [await http.options(path, headers=preflight) for path in READ_ROUTES]
        responses.append(await http.get("/now", headers={**origin, "Host": "evil.example"}))
        responses.append(await http.get("/nowhere", headers=origin))
    shown = len(READ_ROUTES)
    assert [r.status_code for r in responses[:shown]] == [200] * shown
    for response in responses:
        cors = [name for name in response.headers if name.lower().startswith("access-control-")]
        assert cors == [], response.request.url


async def test_a_cors_header_added_by_a_route_is_dropped(daemon: Daemon) -> None:
    app = create_app(daemon)

    async def leaky() -> Response:
        return Response(
            "{}",
            media_type="application/json",
            headers={"Access-Control-Allow-Origin": "*", "X-Kept": "yes"},
        )

    app.add_api_route("/leaky", leaky)
    async with _client(app) as http:
        response = await http.get("/leaky")
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
    assert response.headers["x-kept"] == "yes"


async def test_port_zero_checks_against_the_bound_port() -> None:
    daemon = make_daemon(windows_collector(), user_config={"api": {"port": 0}})
    app = create_app(daemon)
    async with _client(app, "http://127.0.0.1:5555") as http:
        assert (await http.get("/health")).status_code == 200
        assert (await http.get("/health", headers={"Host": f"127.0.0.1:{PORT}"})).status_code == 403


# --- the guard on raw ASGI scopes ----------------------------------------------------------


async def _call(guard: HostGuard, scope: dict[str, Any]) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await guard(scope, receive, send)
    return sent


async def _inner(scope: Any, receive: Any, send: Any) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _scope(headers: list[tuple[bytes, bytes]], kind: str = "http") -> dict[str, Any]:
    return {"type": kind, "path": "/now", "headers": headers, "server": ("127.0.0.1", PORT)}


async def test_a_missing_or_repeated_host_header_is_a_bad_request() -> None:
    guard = HostGuard(_inner, port=PORT)
    missing = await _call(guard, _scope([]))
    assert missing[0]["status"] == 400
    host = (b"host", f"127.0.0.1:{PORT}".encode())
    repeated = await _call(guard, _scope([host, host]))
    assert repeated[0]["status"] == 400
    assert (await _call(guard, _scope([host])))[0]["status"] == 200


async def test_a_foreign_websocket_is_closed_before_it_is_accepted() -> None:
    guard = HostGuard(_inner, port=PORT)
    sent = await _call(guard, _scope([(b"host", b"evil.example")], kind="websocket"))
    assert sent == [{"type": "websocket.close", "code": 1008}]


async def test_port_zero_without_a_server_address_is_refused() -> None:
    guard = HostGuard(_inner, port=0)
    scope = _scope([(b"host", f"127.0.0.1:{PORT}".encode())])
    scope["server"] = None
    assert (await _call(guard, scope))[0]["status"] == 403


async def test_lifespan_and_other_scopes_pass_through() -> None:
    seen: list[str] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    await HostGuard(inner, port=PORT)({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    assert seen == ["lifespan"]


def test_allowed_hosts_are_exactly_the_two_loopback_names() -> None:
    assert allowed_hosts(PORT) == {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}


# --- binding -------------------------------------------------------------------------------


def test_uvicorn_binds_to_loopback_only() -> None:
    config = parse_config({"api": {"port": 7400}}, source="test")
    server = server_config(create_app(make_daemon()), config)
    assert (server.host, server.port) == ("127.0.0.1", 7400)
    assert server.proxy_headers is False
    assert server.server_header is False


@pytest.mark.parametrize("host", ["0.0.0.0", "localhost", "::1", "192.168.1.10", ""])
def test_configuration_refuses_any_other_host(host: str) -> None:
    with pytest.raises(ConfigError, match=r"api\.host"):
        parse_config({"api": {"host": host}}, source="test")
