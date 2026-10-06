"""Generate ``tests/fixtures/envelope/opencode-session-status.json`` with ``bun`` (WP3.2).

Dev-only; the tests only read the golden. Run it by hand after a deliberate change to
``src/fylgja/collectors/opencode/fylgja.ts``:

    uv run --frozen python scripts/gen_opencode_envelope.py

The script loads the plugin in ``bun``, sends it one ``session.status`` event with placeholder
values and a throwaway data directory under ``target/``, reads the envelope the plugin wrote and
replaces the volatile parts (id, nanosecond stamp, parent pid, terminal ids) with placeholders. The
result is pretty-printed like ``fylgja.hook-1.json``; the real file is one compact ASCII line.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "src" / "fylgja" / "collectors" / "opencode" / "fylgja.ts"
OUT = ROOT / "tests" / "fixtures" / "envelope" / "opencode-session-status.json"
WORK = ROOT / "target" / "gen-opencode-envelope"

SESSION_ID = "ses_00000000000000000000000001"
DIRECTORY = "/work/example/api-service"
DRIVER = """
const { FylgjaPlugin } = await import(process.argv[1])
const hooks = await FylgjaPlugin({ directory: process.argv[2] })
await hooks.event({
  event: {
    type: "session.status",
    properties: { sessionID: process.argv[3], status: { type: "busy" } },
  },
})
"""

PLACEHOLDERS: dict[str, Any] = {
    "id": "7a2b9c1d-3e4f-4a5b-8c6d-0e1f2a3b4c5d",
    "received_at_ns": 1791231759255399000,
    "ppid": 4201,
}
PLACEHOLDER_ENV = {
    "CMUX_WORKSPACE_ID": "00000000-0000-4000-8000-0000000000a1",
    "CMUX_SURFACE_ID": "00000000-0000-4000-8000-0000000000a2",
    "CMUX_TAB_ID": "00000000-0000-4000-8000-0000000000a3",
    "CMUX_PANEL_ID": "00000000-0000-4000-8000-0000000000a4",
    "TERM_PROGRAM": "ghostty",
}


TERMINAL_VARIABLES = (
    "CMUX_WORKSPACE_ID",
    "CMUX_SURFACE_ID",
    "CMUX_TAB_ID",
    "CMUX_PANEL_ID",
    "ITERM_SESSION_ID",
    "TERM_SESSION_ID",
    "TERM_PROGRAM",
)


def main() -> int:
    bun = shutil.which("bun")
    if bun is None:
        print("bun is not on PATH; write the golden by hand to match fylgja.ts", file=sys.stderr)
        return 1
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    # Terminal identifiers of the developer's own shell must not leak into the golden.
    inherited = {k: v for k, v in os.environ.items() if k not in TERMINAL_VARIABLES}
    env = {**inherited, "FYLGJA_DATA_DIR": str(WORK / "data"), **PLACEHOLDER_ENV}
    subprocess.run(
        [bun, "-e", DRIVER, str(PLUGIN), DIRECTORY, SESSION_ID],
        check=True,
        env=env,
        cwd=WORK,
        timeout=60,
    )
    files = sorted((WORK / "data" / "hook-inbox" / "opencode").glob("*.json"))
    if len(files) != 1:
        print(f"expected one envelope, found {len(files)}", file=sys.stderr)
        return 1
    envelope = json.loads(files[0].read_text(encoding="utf-8"))
    envelope.update(PLACEHOLDERS)
    OUT.write_text(json.dumps(envelope, indent=2) + "\n", encoding="utf-8")
    shutil.rmtree(WORK, ignore_errors=True)
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
