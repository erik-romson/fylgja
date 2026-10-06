"""Run ``fylgja serve`` in a subprocess for the lifecycle tests (the ``tests/`` tree has no
packages). The environment's ``FYLGJA_*`` directories and ``HOME`` come from the autouse fixture;
the config sets a free loopback port and disables the built-in collectors, so nothing here reads
AeroSpace, the Claude registry or anything else of the machine."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fylgja import paths

READY_TIMEOUT = 20.0


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def write_config(port: int, extra: str = "") -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(
        f"[api]\nport = {port}\n"
        "[collectors.aerospace]\nenabled = false\n"
        "[collectors.claude]\nenabled = false\n"
        "[collectors.codex]\nenabled = false\n"
        "[collectors.opencode]\nenabled = false\n"
        "[collectors.norn]\nenabled = false\n"
        "[collectors.project]\nenabled = false\n"
        "[collectors.presence]\nenabled = false\n" + extra,
        encoding="utf-8",
    )


def fylgja(*args: str, timeout: float = 25.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "fylgja._entry", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def get(port: int, path: str) -> Any:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as response:
        return json.loads(response.read())


@dataclass
class Served:
    process: subprocess.Popen[bytes]
    port: int
    output: Path

    def stop(self, signum: int, timeout: float = 5.0) -> tuple[int, float]:
        """Send ``signum``; returns the exit code and the seconds the exit took."""
        started = time.monotonic()
        self.process.send_signal(signum)
        code = self.process.wait(timeout)
        return code, time.monotonic() - started


def wait_ready(process: subprocess.Popen[bytes], port: int) -> None:
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"serve exited early with {process.returncode}")
        try:
            if get(port, "/health").get("status") == "ok":
                return
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.1)
    raise AssertionError("serve did not answer /health in time")


@contextmanager
def serving(port: int, output: Path) -> Iterator[Served]:
    with output.open("wb") as sink:
        process = subprocess.Popen(
            [sys.executable, "-m", "fylgja._entry", "serve"], stdout=sink, stderr=sink
        )
        served = Served(process, port, output)
        try:
            wait_ready(process, port)
            yield served
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(10)
