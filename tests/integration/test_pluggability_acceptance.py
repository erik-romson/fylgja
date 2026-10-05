"""The pluggability acceptance test (``PLAN.md`` §4.9, WP1.5).

A synthetic collector package is installed, enabled through configuration, observed through
``/now``, stopped and tested without a change to Fylgja core code. ``/now`` arrives with step 14;
until then this test xfails, and strict mode makes a forgotten marker fail the suite.
"""

from __future__ import annotations

import pytest

from fylgja import paths


@pytest.mark.xfail(strict=True, reason="needs /now from step 14")
async def test_installed_collector_is_enabled_observed_via_now_and_stopped() -> None:
    # Imported here so that collecting this module works before the API exists.
    from fastapi.testclient import TestClient
    from fylgja.api.app import create_app

    config = paths.config_file()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("[collectors.fake]\nenabled = true\ninterval = 1.0\n", encoding="utf-8")

    app = create_app()
    with TestClient(app) as client:
        secret = paths.api_secret_file().read_text(encoding="utf-8").strip()
        headers = {"Authorization": f"Bearer {secret}"}
        collectors = client.get("/collectors", headers=headers).json()
        assert any(c["name"] == "fake" and c["state"] == "enabled" for c in collectors)

        now = client.get("/now", headers=headers).json()
        assert now["schema"] == "fylgja.now/1"
        assert any(w.get("title") == "Fake window" for w in now["windows"])

    # Leaving the ``with`` block ran the shutdown; the fake collector's sentinel-free stop is
    # covered by the contract suite.
