"""``fylgja serve`` as a real process: start-up, loopback only, and a graceful stop (WP1.10)."""

from __future__ import annotations

import json
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from serve_support import (
    free_port,
    fylgja,
    get,
    serving,
    write_config,
)

from fylgja import paths

pytestmark = pytest.mark.slow


def _listeners(process: subprocess.Popen[bytes]) -> list[str]:
    lsof = shutil.which("lsof")
    if lsof is None:
        pytest.skip("lsof is not installed")
    result = subprocess.run(
        [lsof, "-nP", "-a", "-p", str(process.pid), "-iTCP", "-sTCP:LISTEN", "-Fn"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    return sorted({line[1:] for line in result.stdout.splitlines() if line.startswith("n")})


def test_health_then_sigterm_exits_cleanly_within_5_seconds(isolated_env: Path) -> None:
    port = free_port()
    write_config(port)
    with serving(port, isolated_env / "serve.out") as served:
        health = get(port, "/health")
        assert health["status"] == "ok"
        assert health["now_schema"] == "fylgja.now/1"
        code, seconds = served.stop(signal.SIGTERM)
    assert code == 0
    assert seconds < 5.0
    log = paths.log_file().read_text(encoding="utf-8")
    assert "daemon started" in log
    assert "daemon stopped" in log
    assert log.rstrip().endswith("fylgja.serve: stopped")


def test_sigint_stops_it_the_same_way(isolated_env: Path) -> None:
    port = free_port()
    write_config(port)
    with serving(port, isolated_env / "serve.out") as served:
        code, seconds = served.stop(signal.SIGINT)
    assert code == 0
    assert seconds < 5.0


def test_the_socket_is_bound_to_loopback_only(isolated_env: Path) -> None:
    port = free_port()
    write_config(port)
    with serving(port, isolated_env / "serve.out") as served:
        assert _listeners(served.process) == [f"127.0.0.1:{port}"]
        served.stop(signal.SIGTERM)


def test_the_read_commands_talk_to_the_served_daemon(isolated_env: Path) -> None:
    port = free_port()
    write_config(port)
    with serving(port, isolated_env / "serve.out") as served:
        for command in (["now"], ["ws"], ["sessions"], ["collectors", "list"]):
            result = fylgja(*command)
            assert result.returncode == 0, result.stderr
        listing = fylgja("collectors", "list", "--json")
        assert {row["name"] for row in json.loads(listing.stdout)} >= {
            "aerospace",
            "claude",
        }
        served.stop(signal.SIGTERM)
    down = fylgja("now")
    assert down.returncode == 3
    assert "daemon not running" in down.stderr


def test_serving_creates_the_database_and_logs_without_the_home_directory(
    isolated_env: Path,
) -> None:
    port = free_port()
    write_config(port)
    with serving(port, isolated_env / "serve.out") as served:
        assert paths.database_file().is_file()
        served.stop(signal.SIGTERM)
    assert not any((isolated_env / "home").iterdir())


def test_a_port_that_is_taken_fails_fast(isolated_env: Path) -> None:
    port = free_port()
    write_config(port)
    with serving(port, isolated_env / "first.out") as first:
        second = subprocess.run(
            [sys.executable, "-m", "fylgja._entry", "serve"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert second.returncode != 0
        assert get(port, "/health")["status"] == "ok"  # the first one is unharmed
        first.stop(signal.SIGTERM)


def test_an_invalid_config_exits_2_and_names_the_file(isolated_env: Path) -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text("[api]\nhost = '0.0.0.0'\n", encoding="utf-8")
    result = fylgja("serve")
    assert result.returncode == 2
    assert "config.toml" in result.stderr
