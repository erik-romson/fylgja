"""Opt-in live test of local Claude messaging against **disposable** sessions (WP3.6).

A human runs it, and never against a working session. Start three throwaway sessions in
temporary directories, one per inbound policy, exactly as spike 0004 §4 describes (only
``crossSessionInbound`` is set):

.. code-block:: sh

   W=$(mktemp -d /tmp/fylgja-live.XXXXXX)
   for p in accept hold refuse; do
     mkdir "$W/$p"; printf '{"crossSessionInbound":"%s"}\\n' "$p" > "$W/$p.json"
   done
   # in three terminals of your own:
   cd "$W/accept" && claude --settings "$W/accept.json"
   cd "$W/hold"   && claude --settings "$W/hold.json"
   cd "$W/refuse" && claude --settings "$W/refuse.json"

Then, from the Fylgja checkout:

.. code-block:: sh

   FYLGJA_RUN_LIVE=1 FYLGJA_MESSAGE_LIVE_DIR="$W" \\
     uv run --frozen pytest -m live tests/live/test_message_live.py

Each test finds its session in the live registry by its ``cwd`` (``$W/<policy>``), sends one
harmless nonce through the real resolver, encoder and transport (one attempt), and checks the
receipt. While the shipped encoder supports no frame (spike 0004 ``no-go``) the receipt must be
``unsupported`` and nothing is written to any socket. Once a frame ships, ``submitted`` is expected
and the target's transcript is checked for the nonce: delivered under ``accept``, not delivered
under ``hold`` or ``refuse``. The test changes no file and grants no permission; quit the three
sessions and remove ``$W`` afterwards.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

from fylgja.collectors.claude import parse
from fylgja.collectors.context import SystemClock
from fylgja.core.system import LocalFileSystem, SubprocessRunner
from fylgja.messaging.claude.encoders import default_encoder
from fylgja.messaging.claude.encoders.v1 import SUPPORTED
from fylgja.messaging.claude.messenger import ClaudeMessenger
from fylgja.messaging.claude.receipts import MemoryReceipts, ReceiptState
from fylgja.messaging.claude.resolve import Resolver, ps_start_time, read_rows
from fylgja.messaging.service import MessageService

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(sys.platform != "darwin", reason="Claude's socket directory is macOS's"),
]

REAL_HOME = Path.home()
"""Taken at import, before the autouse fixture points ``HOME`` at a per-test directory."""
REGISTRY = REAL_HOME / ".claude" / "sessions"
OBSERVE_SECONDS = 20.0


def _disposable_dir(policy: str) -> Path:
    base = os.environ.get("FYLGJA_MESSAGE_LIVE_DIR")
    if not base:
        pytest.skip("set FYLGJA_MESSAGE_LIVE_DIR to the directory of the disposable sessions")
    directory = Path(base) / policy
    if not str(directory).startswith(("/tmp/", "/private/tmp/", "/var/folders/")):  # noqa: S108
        pytest.skip("the disposable sessions must live in a temporary directory")
    return directory


def _row_for(directory: Path) -> parse.RegistryRow:
    rows = [row for row in read_rows(LocalFileSystem(), REGISTRY) if row.cwd == str(directory)]
    if len(rows) != 1:
        pytest.skip(f"expected one disposable session in {directory}, found {len(rows)}")
    return rows[0]


def _transcript_has(row: parse.RegistryRow, nonce: str) -> bool:
    projects = REAL_HOME / ".claude" / "projects"
    for path in projects.glob(f"*/{row.session_id}.jsonl"):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if nonce in json.dumps(record):
                return True
    return False


@pytest.mark.parametrize("policy", ["accept", "hold", "refuse"])
def test_a_nonce_to_a_disposable_session(policy: str) -> None:
    directory = _disposable_dir(policy)
    row = _row_for(directory)
    resolver = Resolver(
        REGISTRY, fs=LocalFileSystem(), start_time=ps_start_time(SubprocessRunner())
    )
    receipts = MemoryReceipts()
    service = MessageService(
        enabled=True,
        max_chars=8000,
        messengers={"claude": ClaudeMessenger(resolver, default_encoder())},
        receipts=receipts,
        clock=SystemClock(),
    )
    nonce = f"FYLGJA-LIVE-NONCE-{uuid.uuid4().hex[:12]}"
    receipt = asyncio.run(service.send(f"claude:{row.session_id}", f"Ignore this test: {nonce}"))
    assert receipts.receipts == [receipt]
    if not SUPPORTED:
        assert (receipt.state, receipt.error_kind) == (
            ReceiptState.UNSUPPORTED,
            "no_supported_frame",
        ), "spike 0004 is no-go: nothing may be written to the socket"
        return
    assert receipt.state is ReceiptState.SUBMITTED, receipt
    deadline = time.monotonic() + OBSERVE_SECONDS
    seen = False
    while time.monotonic() < deadline and not seen:
        seen = _transcript_has(row, nonce)
        time.sleep(1)
    assert seen is (policy == "accept")
