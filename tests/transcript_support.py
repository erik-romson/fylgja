"""Helpers for the Claude transcript tests: build and append transcript lines."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CLAUDE_TRANSCRIPTS = FIXTURES / "claude" / "transcripts"


def line(**fields: Any) -> str:
    return json.dumps(fields, separators=(",", ":")) + "\n"


def user(text: str, ts: str = "2026-10-05T10:00:00.000Z", **extra: Any) -> str:
    return line(type="user", timestamp=ts, message={"role": "user", "content": text}, **extra)


def assistant(
    *blocks: Mapping[str, Any], ts: str = "2026-10-05T10:00:05.000Z", **extra: Any
) -> str:
    message = {"role": "assistant", "model": "claude-opus-4-8", "content": list(blocks)}
    return line(type="assistant", timestamp=ts, message=message, **extra)


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def tool_use(call_id: str, name: str, **tool_input: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": call_id, "name": name, "input": tool_input}


def tool_result(call_id: str) -> str:
    block = {"type": "tool_result", "tool_use_id": call_id, "content": "ok"}
    return line(type="user", message={"role": "user", "content": [block]})


def append(path: Path, data: str | bytes) -> None:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    with path.open("ab") as handle:
        handle.write(raw)
