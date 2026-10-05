"""Helpers for the inbox, normalizer and statusline tests: envelopes built the way the hook builds
them, and spool files written without the fsync the real writer pays for."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fylgja import hook

SESSION = "00000000-0000-4000-8000-000000004101"
TERMINAL_ENV = {
    "CLAUDE_CODE_SESSION_ID": SESSION,
    "CLAUDE_PID": "4101",
    "CMUX_WORKSPACE_ID": "ws-1",
    "CMUX_SURFACE_ID": "surface-1",
    "TERM_PROGRAM": "ghostty",
}


def make_envelope(
    event: str | None = "Stop",
    payload: Mapping[str, Any] | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    received_at_ns: int | None = None,
    envelope_id: str | None = None,
    source: str = "claude",
    truncated: bool = False,
) -> dict[str, Any]:
    """An envelope as ``fylgja hook`` builds it. ``payload`` may be a dict or raw text."""
    if payload is None:
        payload = {"session_id": SESSION, "hook_event_name": event}
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return hook.build_envelope(
        source=source,
        event=event,
        payload_text=text,
        truncated=truncated,
        environ=TERMINAL_ENV if environ is None else environ,
        received_at_ns=time.time_ns() if received_at_ns is None else received_at_ns,
        ppid=4100,
        envelope_id=envelope_id or str(uuid.uuid4()),
    )


def spool_name(envelope: Mapping[str, Any]) -> str:
    return f"{envelope['received_at_ns']}-{envelope['id']}.json"


def write_envelope(directory: Path, envelope: Mapping[str, Any]) -> Path:
    """Write ``envelope`` as a spool file (mode 0600, no fsync)."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / spool_name(envelope)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, hook.encode_envelope(envelope))
    finally:
        os.close(descriptor)
    return path
