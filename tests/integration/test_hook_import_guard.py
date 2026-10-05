"""The hook path imports nothing outside the stdlib (WP2.1, ``PLAN.md`` §4.3).

Two independent checks:

- a probe runs ``fylgja._entry.main()`` with ``hook`` arguments in a subprocess and, from inside
  the hook's final ``os._exit``, dumps every module loaded since the probe started to a file.
  This also catches imports made after the hook silenced stdout and stderr;
- ``python -X importtime -m fylgja._entry hook Stop`` lists the imports on stderr, which the hook
  redirects only after its own module has been imported. The interpreter's start-up imports
  (``site`` and any ``.pth`` files of the environment) are measured separately and subtracted.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from hook_support import clean_env, envelope_files

from fylgja import paths

ALLOWED_FYLGJA = {"fylgja", "fylgja._entry", "fylgja.hook", "fylgja.paths"}
HEAVY = ("typer", "click", "fastapi", "starlette", "pydantic", "uvicorn", "httpx", "anyio")

_PROBE = """
import json
import os
import sys

before = set(sys.modules)
dump_path = sys.argv[1]
real_exit = os._exit


def dumping_exit(code):
    with open(dump_path, "w", encoding="utf-8") as handle:
        json.dump({"new": sorted(set(sys.modules) - before), "code": code}, handle)
    real_exit(code)


os._exit = dumping_exit
sys.argv = ["fylgja", "hook", *sys.argv[2:]]
import fylgja._entry

fylgja._entry.main()
"""


def _third_party(modules: list[str]) -> list[str]:
    stdlib = sys.stdlib_module_names
    return sorted(m for m in modules if m.split(".")[0] not in {*stdlib, "fylgja"})


def _probe(dump: Path, args: list[str], stdin: bytes) -> list[str]:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, str(dump), *args],
        input=stdin,
        capture_output=True,
        env=clean_env(),
        timeout=20,
        check=False,
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")
    data = json.loads(dump.read_text(encoding="utf-8"))
    assert data["code"] == 0
    new: list[str] = data["new"]
    return new


@pytest.mark.parametrize(
    ("args", "stdin"),
    [
        (["Stop"], b'{"session_id":"abc","hook_event_name":"Stop"}'),
        (["StatusLine"], b'{"session_id":"abc"}'),
        (["--source", "codex", "PostToolUse"], json.dumps({"x": "y" * 300_000}).encode()),
        (["Stop"], b"x" * 300_000),
    ],
    ids=["event", "statusline", "oversize-json", "oversize-garbage"],
)
def test_the_hook_path_loads_only_stdlib_and_its_own_modules(
    isolated_env: Path, args: list[str], stdin: bytes
) -> None:
    new = _probe(isolated_env / "modules.json", args, stdin)
    assert _third_party(new) == []
    assert {m for m in new if m.split(".")[0] == "fylgja"} <= ALLOWED_FYLGJA
    assert not [m for m in new if m.split(".")[0] in HEAVY]
    assert "fylgja.hook" in new
    written = envelope_files(args[1] if args[0] == "--source" else "claude")
    written += sorted(paths.statusline_dir().glob("*.json"))
    assert len(written) == 1


def test_dropping_an_event_loads_nothing_more(isolated_env: Path) -> None:
    inbox = paths.inbox_dir("claude")
    inbox.mkdir(parents=True, mode=0o700)
    (inbox / ".backlog-stamp").write_bytes(b"full\n")
    new = _probe(isolated_env / "modules.json", ["Stop"], b"{}")
    assert _third_party(new) == []
    assert (inbox / "dropped.count").read_text() == "1\n"


_IMPORT_LINE = re.compile(r"^import time:\s+\d+\s+\|\s+\d+\s+\|(\s*)(\S+)\s*$")


def _imported(stderr: bytes) -> set[str]:
    names = set()
    for line in stderr.decode("utf-8", errors="replace").splitlines():
        match = _IMPORT_LINE.match(line)
        if match:
            names.add(match.group(2))
    return names


def test_importtime_shows_no_third_party_import() -> None:
    baseline = subprocess.run(
        [sys.executable, "-X", "importtime", "-c", "pass"],
        capture_output=True,
        env=clean_env(),
        timeout=20,
        check=True,
    )
    hooked = subprocess.run(
        [sys.executable, "-X", "importtime", "-m", "fylgja._entry", "hook", "Stop"],
        input=b'{"session_id":"abc"}',
        capture_output=True,
        env=clean_env(),
        timeout=20,
        check=False,
    )
    assert hooked.returncode == 0
    assert hooked.stdout == b""
    start_up = _imported(baseline.stderr)
    by_hook = _imported(hooked.stderr) - start_up
    assert "fylgja.hook" in by_hook
    assert "fylgja.paths" in by_hook
    assert _third_party(sorted(by_hook)) == []
    assert not {m for m in by_hook if m.split(".")[0] in HEAVY}
    assert len(envelope_files()) == 1
