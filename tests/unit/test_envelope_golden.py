"""The ``fylgja.hook/1`` envelope matches its golden and its contract page.

The golden ``tests/fixtures/envelope/fylgja.hook-1.json`` is what external writers (the norn and
OpenCode plugins) test against, and ``docs/api/inbox-envelope.md`` documents it field by field.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest

from fylgja import hook

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN = REPO_ROOT / "tests" / "fixtures" / "envelope" / "fylgja.hook-1.json"
CONTRACT = REPO_ROOT / "docs" / "api" / "inbox-envelope.md"


def _golden() -> dict[str, Any]:
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _built(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "source": "claude",
        "event": "Stop",
        "payload_text": '{"session_id":"abc","hook_event_name":"Stop"}',
        "truncated": False,
        "environ": {"CLAUDE_CODE_SESSION_ID": "abc", "TERM_PROGRAM": "ghostty"},
        "received_at_ns": 1_791_231_759_255_399_000,
    }
    values.update(overrides)
    return hook.build_envelope(**values)


def _section(text: str, heading: str) -> str:
    pattern = rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)"
    match = re.search(pattern, text, re.MULTILINE | re.DOTALL)
    assert match, f"no section {heading!r} in {CONTRACT.name}"
    return match.group(1)


def test_built_envelope_has_the_golden_keys_in_order() -> None:
    assert list(_built()) == list(_golden())


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"truncated": True, "payload_text": '{"_malformed_oversize":300000}'},
        {"source": "norn", "event": "run.started", "environ": {}},
    ],
    ids=["plain", "oversize", "external-source"],
)
def test_built_envelope_has_the_golden_types(overrides: dict[str, Any]) -> None:
    golden = _golden()
    built = _built(**overrides)
    for key, value in golden.items():
        assert type(built[key]) is type(value), key
    assert all(isinstance(v, str) for v in built["env"].values())


def test_event_arg_may_be_null() -> None:
    assert _built(event=None)["event_arg"] is None


def test_golden_values_follow_the_contract() -> None:
    golden = _golden()
    assert golden["schema"] == hook.SCHEMA == "fylgja.hook/1"
    assert uuid.UUID(golden["id"]).version == 4
    assert str(uuid.UUID(golden["id"])) == golden["id"]
    assert golden["received_at_ns"] > 10**18
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", golden["source"])
    assert set(golden["env"]) <= set(hook.ENV_ALLOWLIST)
    assert all(len(v) <= hook.ENV_VALUE_MAX_CHARS for v in golden["env"].values())
    payload = json.loads(golden["payload_text"])
    assert payload["hook_event_name"] == golden["event_arg"]
    assert golden["truncated"] is False


def test_golden_round_trips_through_the_encoder() -> None:
    golden = _golden()
    assert json.loads(hook.encode_envelope(golden)) == golden


def test_contract_field_table_lists_exactly_the_golden_keys() -> None:
    section = _section(CONTRACT.read_text(encoding="utf-8"), "Fields")
    fields = re.findall(r"^\| `([a-z_]+)` \|", section, re.MULTILINE)
    assert fields == list(_golden())


def test_contract_allowlist_matches_the_hook() -> None:
    section = _section(CONTRACT.read_text(encoding="utf-8"), "Environment allowlist (A3)")
    names = re.findall(r"^- `([A-Z_]+)`", section, re.MULTILINE)
    assert names == list(hook.ENV_ALLOWLIST)
    assert "CLAUDE_CODE_MESSAGING_TOKEN" not in names
    assert "CMUX_SOCKET_CAPABILITY" not in names


def test_contract_states_the_limits_the_hook_uses() -> None:
    text = CONTRACT.read_text(encoding="utf-8")
    assert hook.VERBATIM_LIMIT_BYTES == 256 * 1024
    assert hook.STRING_LIMIT_BYTES == 4 * 1024
    assert hook.READ_LIMIT_BYTES == 4 * 1024 * 1024
    for fragment in ("256 KiB", "4 KiB", "4 MiB", "100 000 files", "512 MiB", "60 s", "2 s"):
        assert fragment in text, fragment
    assert hook.BACKLOG_MAX_FILES == 100_000
    assert hook.BACKLOG_MAX_BYTES == 512 * 1024 * 1024
    assert hook.BACKLOG_CHECK_INTERVAL_NS == 60 * 10**9
    assert hook.STATUSLINE_MIN_INTERVAL_NS == 2 * 10**9
    assert f"`{hook.BACKLOG_STAMP}`" in text
    assert f"`{hook.DROPPED_COUNT}`" in text
    assert f'{{"{hook.TRUNCATED_KEY}": <bytes>}}' in text
    assert f'{{"{hook.OVERSIZE_KEY}": <bytes>}}' in text
