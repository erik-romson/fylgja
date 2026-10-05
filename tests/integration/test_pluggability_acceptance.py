"""The pluggability acceptance test (``PLAN.md`` §4.9, WP1.5, WP1.9).

A synthetic collector package, the real ``fylgja-fake`` distribution, is installed, enabled through
configuration, observed through ``/now``, stopped and tested, without a change to Fylgja core code:

* **installed:** it is a dev path dependency with its own ``fylgja.collectors`` entry point;
* **enabled:** only by ``[collectors.fake] enabled = true`` in the user's ``config.toml`` (the
  built-ins are switched off the same way, so the test needs no AeroSpace and no Claude);
* **observed:** the app, built by ``create_app()`` from configuration alone and run through its
  ASGI lifespan as a server runs it, shows the fake window and workspace in ``/now``; read routes
  need no secret;
* **stopped:** the lifespan's shutdown stops the daemon, the collector's task ends and its last
  health (``disabled``, stopped) is in the store;
* **tested:** ``tests/contract/test_fake_plugin_contract.py`` runs the shared contract on it.

No core module names the plugin.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
from api_support import lifespan

from fylgja import paths
from fylgja.api.app import create_app
from fylgja.core.daemon import Daemon
from fylgja.store import db

BASE_URL = "http://127.0.0.1:7319"
SOURCE = Path(__file__).resolve().parents[2] / "src" / "fylgja"


async def _wait_for_fake_window(http: httpx.AsyncClient, deadline: float = 10.0) -> dict[str, Any]:
    limit = time.monotonic() + deadline
    while True:
        response = await http.get("/now")
        assert response.status_code == 200
        now: dict[str, Any] = response.json()
        workspaces = {w["name"]: w for w in now.get("workspaces", [])}
        windows = workspaces.get("fake", {}).get("windows", [])
        if any(w.get("title") == "Fake window" for w in windows):
            return now
        assert time.monotonic() < limit, "the fake window never reached /now"
        await asyncio.sleep(0.05)


async def test_installed_collector_is_enabled_observed_via_now_and_stopped() -> None:
    config = paths.config_file()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "[collectors.fake]\nenabled = true\ninterval = 1.0\n\n"
        "[collectors.aerospace]\nenabled = false\n\n"
        "[collectors.claude]\nenabled = false\n",
        encoding="utf-8",
    )

    app = create_app()
    daemon = app.state.daemon
    assert isinstance(daemon, Daemon)
    transport = httpx.ASGITransport(app=app)
    async with lifespan(app), httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
        collectors = (await http.get("/collectors")).json()  # no Authorization: reads need none
        assert any(c["name"] == "fake" and c["state"] == "enabled" for c in collectors)
        bomb = next(c for c in collectors if c["name"] == "fake_import_bomb")
        assert (bomb["state"], bomb["health"]) == ("disabled", "not_loaded")

        now = await _wait_for_fake_window(http)
        assert now["schema"] == "fylgja.now/1"
        fake = next(c for c in now["collectors"] if c["name"] == "fake")
        assert fake["origin"] == "external"
        assert fake["state"] in ("starting", "ok")
        assert "fake" not in now["degraded"]
        window = next(w for w in now["workspaces"] if w["name"] == "fake")["windows"][0]
        assert window == {
            "id": "fake-window-1",
            "app": "Fake",
            "title": "Fake window",
            "focused": False,
        }

        assert (await http.get("/workspaces/fake")).status_code == 200
        assert (await http.get("/health")).json()["status"] == "ok"

    # The lifespan's shutdown ran: the collector stopped and said so.
    task = daemon.supervisor.task("fake")
    assert task is not None
    assert task.done()
    assert not daemon.running
    connection = db.connect_reader(paths.database_file())
    try:
        row = connection.execute(
            "SELECT state FROM collector_health WHERE name = 'fake'"
        ).fetchone()
    finally:
        connection.close()
    assert row["state"] == "disabled"


async def test_a_daemon_the_caller_started_is_left_to_the_caller() -> None:
    app = create_app()
    daemon = app.state.daemon
    assert isinstance(daemon, Daemon)
    await daemon.start()
    try:
        async with lifespan(app):
            assert daemon.running
        assert daemon.running  # the lifespan did not own it
    finally:
        await daemon.stop()


def test_no_core_module_names_the_plugin() -> None:
    for path in SOURCE.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "fylgja_fake" not in text, path
        assert '"fake"' not in text, path
