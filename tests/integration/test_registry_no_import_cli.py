"""The no-import rule through the CLI and ``serve`` (WP1.2, WP1.10).

``fylgja-fake`` is installed in the dev environment; its ``fake_import_bomb`` entry point writes a
sentinel and raises when imported. Listing collectors through a served daemon, and a full start-up
and shutdown, must leave the sentinel absent. A positive control proves the sentinel would show.
"""

from __future__ import annotations

import json
import signal
from pathlib import Path
from typing import Any

import pytest
from serve_support import free_port, fylgja, get, serving, write_config

from fylgja import paths

pytestmark = pytest.mark.slow

LOADED_ROW_KEYS = {
    "name",
    "origin",
    "distribution",
    "distribution_version",
    "state",
    "enabled",
    "api_version",
    "capabilities",
    "health",
}


def _sentinel() -> Path:
    return paths.state_dir() / "fake_import_bomb.sentinel"


def _rows(output: str) -> dict[str, dict[str, Any]]:
    return {row["name"]: row for row in json.loads(output)}


def test_collectors_list_and_a_full_serve_never_import_a_disabled_external(
    isolated_env: Path,
) -> None:
    port = free_port()
    write_config(port)
    with serving(port, isolated_env / "serve.out") as served:
        listing = fylgja("collectors", "list", "--json")
        assert listing.returncode == 0, listing.stderr
        rows = _rows(listing.stdout)
        assert get(port, "/collectors") == json.loads(listing.stdout)
        table = fylgja("collectors", "list")
        assert table.returncode == 0
        assert "fake_import_bomb" in table.stdout
        assert "not_loaded" in table.stdout
        code, _ = served.stop(signal.SIGTERM)
    assert code == 0
    assert not _sentinel().exists()
    for name in ("fake", "fake_import_bomb"):
        row = rows[name]
        assert set(row) == LOADED_ROW_KEYS
        assert row["origin"] == "external"
        assert row["distribution"] == "fylgja-fake"
        assert row["distribution_version"] == "0.1.0"
        assert row["state"] == "disabled"
        assert row["enabled"] is False
        assert row["api_version"] == "not_loaded"
        assert row["capabilities"] == "not_loaded"
        assert row["health"] == "not_loaded"
    for name in ("aerospace", "claude"):
        assert rows[name]["origin"] == "built-in"
        assert rows[name]["state"] == "disabled"
        assert rows[name]["api_version"] == 1


def test_doctor_never_imports_a_disabled_external(isolated_env: Path) -> None:
    """``fylgja doctor`` (a local command, no daemon) leaves the bomb's sentinel absent."""
    port = free_port()
    write_config(port)
    for args in (("doctor", "--json"), ("doctor",)):
        result = fylgja(*args)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not _sentinel().exists()
    checks = {c["name"]: c for c in json.loads(fylgja("doctor", "--json").stdout)["checks"]}
    for name in ("fake", "fake_import_bomb"):
        assert checks[f"plugin:{name}"]["status"] == "ok"
        assert "not imported" in checks[f"plugin:{name}"]["detail"]
        assert f"source:{name}" not in checks  # a disabled external's doctor() is never asked
    assert checks["daemon"]["status"] == "warn"
    assert not _sentinel().exists()


def test_doctor_alongside_a_running_daemon_still_leaves_no_sentinel(isolated_env: Path) -> None:
    port = free_port()
    write_config(port)
    with serving(port, isolated_env / "serve.out") as served:
        result = fylgja("doctor", "--json")
        served.stop(signal.SIGTERM)
    assert result.returncode == 0, result.stdout + result.stderr
    checks = {c["name"]: c for c in json.loads(result.stdout)["checks"]}
    assert checks["daemon"]["status"] == "ok"
    assert checks["daemon-collectors"]["status"] == "ok"
    assert checks["port"]["detail"].endswith("held by the Fylgja daemon")
    assert not _sentinel().exists()


def test_the_bomb_does_go_off_when_it_is_enabled(isolated_env: Path) -> None:
    """Positive control: without it the absence above could hold for the wrong reason.

    ``doctor`` imports it too once it is enabled, and reports the failure.
    """
    port = free_port()
    write_config(port, "[collectors.fake_import_bomb]\nenabled = true\n")
    result = fylgja("doctor", "--json")
    assert result.returncode == 1  # the failed import is a failing check
    assert _sentinel().read_text(encoding="utf-8") == "imported\n"
    _sentinel().unlink()
    with serving(port, isolated_env / "serve.out") as served:
        row = _rows(fylgja("collectors", "list", "--json").stdout)["fake_import_bomb"]
        assert row["state"] == "enabled"
        assert row["health"]["state"] == "failed"
        assert row["health"]["error_kind"] == "import"
        served.stop(signal.SIGTERM)
    assert _sentinel().read_text(encoding="utf-8") == "imported\n"
