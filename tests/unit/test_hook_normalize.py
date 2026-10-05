"""The hook normalizer (WP2.2, A5): every event type, bodies dropped, the 300-character prompt cap,
the allowlisted terminal ids and the ways an envelope can be unusable."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from inbox_support import SESSION, TERMINAL_ENV, make_envelope

from fylgja.collectors.claude.normalize import (
    EnvelopeError,
    datetime_of_ns,
    normalize,
    parse_envelope,
)
from fylgja.models import HookEvent, parse_record
from fylgja.testing.fixtures import fixtures_dir

BODY_CANARY = "body-canary-91c7e3"
RATATOSK = fixtures_dir() / "ratatosk"


def _event(envelope: dict[str, Any]) -> HookEvent:
    return normalize(parse_envelope(json.dumps(envelope)))


def _payload_for(event: str, keys: list[str]) -> dict[str, Any]:
    """A payload with a value for each captured key; every value is recognizable."""
    values: dict[str, Any] = {
        "session_id": SESSION,
        "hook_event_name": event,
        "cwd": "/work/project",
        "permission_mode": "default",
        "transcript_path": f"/x/{BODY_CANARY}.jsonl",
        "prompt": "do the thing",
        "tool_name": "Bash",
        "tool_input": {"command": f"echo {BODY_CANARY}"},
        "tool_response": {"stdout": BODY_CANARY},
        "last_assistant_message": BODY_CANARY,
        "source": "startup",
        "reason": "other",
        "notification_type": "idle_prompt",
        "message": BODY_CANARY,
        "model": "m",
    }
    return {key: values.get(key, BODY_CANARY) for key in keys}


def _captures() -> list[tuple[str, str, dict[str, Any]]]:
    """``(scenario, event, payload)`` for each distinct event shape of the ratatosk captures."""
    cases: list[tuple[str, str, dict[str, Any]]] = []
    for scenario in sorted(p for p in RATATOSK.iterdir() if p.is_dir()):
        seen: set[tuple[str, tuple[str, ...]]] = set()
        for line in (scenario / "hook-events.ndjson").read_text().splitlines():
            record = json.loads(line)
            full = record.get("full")
            if isinstance(full, dict):  # a captured payload, kept whole by the capture
                cases.append(
                    (scenario.name, record["event"], {"hook_event_name": record["event"], **full})
                )
                continue
            keys = record.get("keys")
            if not isinstance(keys, list):
                continue
            shape = (record["event"], tuple(keys))
            if shape in seen:
                continue
            seen.add(shape)
            cases.append((scenario.name, record["event"], _payload_for(record["event"], keys)))
    return cases


CAPTURES = _captures()


def test_the_captures_cover_every_event_type_of_the_hooks_docs_we_rely_on() -> None:
    events = {event for _, event, _ in CAPTURES}
    assert {
        "SessionStart",
        "SessionEnd",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "Stop",
        "StopFailure",
        "Notification",
        "PermissionRequest",
    } <= events


@pytest.mark.parametrize(
    ("scenario", "event", "payload"),
    CAPTURES,
    ids=[f"{s}:{e}:{i}" for i, (s, e, _) in enumerate(CAPTURES)],
)
def test_every_captured_event_normalizes_to_the_allowlist_and_drops_bodies(
    scenario: str, event: str, payload: dict[str, Any]
) -> None:
    result = _event(make_envelope(event, payload))
    assert result.payload.event == event
    session = payload["session_id"]
    assert result.payload.session_id == session
    assert result.entity_id == f"claude:{session}"
    dumped = result.model_dump_json()
    assert BODY_CANARY not in dumped
    allowed = {
        "session_id",
        "hook_event_name",
        "cwd",
        "permission_mode",
        "tool_name",
        "prompt",
        "notification_type",
        "reason",
    }
    for key, value in payload.items():
        if key not in allowed and isinstance(value, str) and len(value) > 8:
            assert value not in dumped, key
    assert set(result.payload.model_dump(exclude_none=True)) <= {
        "session_id",
        "event",
        "tool_name",
        "notification_type",
        "reason",
        "cwd",
        "permission_mode",
        "edited_paths",
        "prompt",
        "pid",
        "ids",
        "envelope_id",
    }
    assert parse_record(result.model_dump(mode="json")) == result


def test_each_event_keeps_its_own_fields() -> None:
    def make(event: str, **payload: Any) -> HookEvent:
        return _event(
            make_envelope(
                event, {"session_id": SESSION, "hook_event_name": event, "cwd": "/w", **payload}
            )
        )

    start = make("SessionStart", source="startup", model="m")
    assert (start.payload.event, start.payload.cwd) == ("SessionStart", "/w")
    end = make("SessionEnd", reason="clear")
    assert end.payload.reason == "clear"
    tool = make("PreToolUse", tool_name="Bash", tool_input={"command": BODY_CANARY})
    assert tool.payload.tool_name == "Bash"
    notification = make("Notification", notification_type="permission_prompt", message="m")
    assert notification.payload.notification_type == "permission_prompt"
    permission = make("PermissionRequest", tool_name="Write", permission_mode="plan")
    assert (permission.payload.tool_name, permission.payload.permission_mode) == ("Write", "plan")
    failure = make("StopFailure")
    assert failure.payload.event == "StopFailure"


@pytest.mark.parametrize(
    ("tool", "tool_input", "expected"),
    [
        ("Edit", {"file_path": "/w/a.py", "old_string": BODY_CANARY}, ("/w/a.py",)),
        ("Write", {"file_path": "/w/b.py", "content": BODY_CANARY}, ("/w/b.py",)),
        ("MultiEdit", {"file_path": "/w/c.py", "edits": [BODY_CANARY]}, ("/w/c.py",)),
        (
            "NotebookEdit",
            {"notebook_path": "/w/n.ipynb", "new_source": BODY_CANARY},
            ("/w/n.ipynb",),
        ),
        ("Bash", {"file_path": "/w/not-an-edit.py", "command": BODY_CANARY}, None),
        ("Read", {"file_path": "/w/read.py"}, None),
    ],
)
def test_edited_paths_come_from_editing_tools_only(
    tool: str, tool_input: dict[str, Any], expected: tuple[str, ...] | None
) -> None:
    payload = {
        "session_id": SESSION,
        "hook_event_name": "PostToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
        "tool_response": {"output": BODY_CANARY},
    }
    result = _event(make_envelope("PostToolUse", payload))
    assert result.payload.edited_paths == expected
    assert BODY_CANARY not in result.model_dump_json()


def test_the_prompt_is_cut_to_300_characters() -> None:
    long = "p" * 301 + "tail"
    payload = {"session_id": SESSION, "hook_event_name": "UserPromptSubmit", "prompt": long}
    result = _event(make_envelope("UserPromptSubmit", payload))
    assert result.payload.prompt == "p" * 300
    exact = _event(make_envelope("UserPromptSubmit", {**payload, "prompt": "q" * 300}))
    assert exact.payload.prompt == "q" * 300
    short = _event(make_envelope("UserPromptSubmit", {**payload, "prompt": "hi"}))
    assert short.payload.prompt == "hi"


def test_only_the_allowlisted_terminal_ids_and_the_pid_are_kept() -> None:
    environ = {
        **TERMINAL_ENV,
        "CMUX_TAB_ID": "tab-1",
        "CMUX_PANEL_ID": "panel-1",
        "ITERM_SESSION_ID": "w0t0p0:iterm-1",
        "TERM_SESSION_ID": "term-1",
        "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/cc-socks/4101.sock",
        "CLAUDE_CODE_BRIDGE_SESSION_ID": "session_bridge-1",
    }
    result = _event(make_envelope("Stop", environ=environ))
    assert result.payload.pid == 4101
    assert result.payload.ids == {
        "CMUX_WORKSPACE_ID": "ws-1",
        "CMUX_SURFACE_ID": "surface-1",
        "CMUX_TAB_ID": "tab-1",
        "CMUX_PANEL_ID": "panel-1",
        "ITERM_SESSION_ID": "w0t0p0:iterm-1",
        "TERM_SESSION_ID": "term-1",
        "TERM_PROGRAM": "ghostty",
    }
    dumped = result.model_dump_json()
    assert "cc-socks" not in dumped
    assert "bridge" not in dumped


def test_no_ids_and_no_pid_when_the_environment_has_none() -> None:
    envelope = make_envelope(
        "Stop", environ={"CLAUDE_CODE_SESSION_ID": SESSION, "CLAUDE_PID": "not-a-pid"}
    )
    result = _event(envelope)
    assert result.payload.ids is None
    assert result.payload.pid is None


def test_the_event_falls_back_to_the_command_line_and_the_session_to_the_environment() -> None:
    marker = json.dumps({"_malformed_oversize": 300_000})
    result = _event(make_envelope("PostToolUse", marker, truncated=True))
    assert result.payload.event == "PostToolUse"
    assert result.payload.session_id == SESSION
    garbage = _event(make_envelope("Stop", "{not json"))
    assert (garbage.payload.event, garbage.payload.session_id) == ("Stop", SESSION)
    truncated_strings = json.dumps(
        {
            "session_id": SESSION,
            "hook_event_name": "UserPromptSubmit",
            "prompt": {"_truncated": 9000},
            "cwd": {"_truncated": 9000},
        }
    )
    big = _event(make_envelope("UserPromptSubmit", truncated_strings, truncated=True))
    assert big.payload.prompt is None
    assert big.payload.cwd is None


def test_the_payload_event_name_wins_over_the_command_line() -> None:
    payload = {"session_id": SESSION, "hook_event_name": "Stop"}
    assert _event(make_envelope("SomethingElse", payload)).payload.event == "Stop"


def test_the_timestamp_and_dedup_key_come_from_the_envelope() -> None:
    envelope_id = str(uuid.uuid4())
    ns = 1_791_231_759_255_399_000
    first = _event(make_envelope("Stop", received_at_ns=ns, envelope_id=envelope_id))
    again = _event(make_envelope("Stop", received_at_ns=ns, envelope_id=envelope_id))
    other = _event(make_envelope("Stop", received_at_ns=ns, envelope_id=str(uuid.uuid4())))
    assert first.observed_at == datetime_of_ns(ns)
    assert first.observed_at.tzinfo is UTC
    assert first.dedup_key == again.dedup_key
    assert first.dedup_key != other.dedup_key
    assert first.payload.envelope_id == envelope_id
    assert first.source == "claude"


def test_datetime_of_ns_keeps_microseconds() -> None:
    assert datetime_of_ns(1_000_000_000_123_456_789) == datetime(
        2001, 9, 9, 1, 46, 40, 123456, tzinfo=UTC
    )
    with pytest.raises(EnvelopeError):
        datetime_of_ns(10**30)


def _good() -> dict[str, Any]:
    return make_envelope("Stop")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.update(schema="fylgja.hook/2"),
        lambda e: e.pop("schema"),
        lambda e: e.update(id="not an id!"),
        lambda e: e.update(id=7),
        lambda e: e.update(received_at_ns=-1),
        lambda e: e.update(received_at_ns=True),
        lambda e: e.update(received_at_ns="1"),
        lambda e: e.update(source="bad source"),
        lambda e: e.update(event_arg=5),
        lambda e: e.update(ppid="1"),
        lambda e: e.update(env=["x"]),
        lambda e: e.update(env={"A": 1}),
        lambda e: e.update(payload_text=None),
        lambda e: e.update(truncated="no"),
    ],
)
def test_a_malformed_envelope_is_refused(mutate: Any) -> None:
    envelope = _good()
    mutate(envelope)
    with pytest.raises(EnvelopeError):
        parse_envelope(json.dumps(envelope))


@pytest.mark.parametrize("data", [b"", b"{", b"[]", b'"x"', b"null", b"\xff\xfe", b"[" * 100_000])
def test_garbage_is_not_an_envelope(data: bytes) -> None:
    with pytest.raises(EnvelopeError):
        parse_envelope(data)


def _refused(envelope: dict[str, Any], match: str) -> None:
    with pytest.raises(EnvelopeError, match=match):
        normalize(parse_envelope(json.dumps(envelope)))


def test_unattributable_envelopes_are_refused() -> None:
    _refused(make_envelope("Stop", "{}", environ={}), "session")
    bad_session = {"session_id": "../etc", "hook_event_name": "Stop"}
    _refused(make_envelope("Stop", bad_session, environ={}), "session")
    _refused(make_envelope(None, {"session_id": SESSION}), "event")
    odd_event = {"session_id": SESSION, "hook_event_name": "bad name!"}
    _refused(make_envelope("Stop", odd_event), "event")


def test_an_envelope_for_another_inbox_is_refused() -> None:
    other = parse_envelope(json.dumps(make_envelope("Stop", source="codex")))
    with pytest.raises(EnvelopeError, match="source"):
        normalize(other)
    assert normalize(other, source="codex").source == "codex"


def test_error_messages_never_quote_content() -> None:
    secret = "message-secret-77"
    for text in (secret, json.dumps({"schema": secret})):
        with pytest.raises(EnvelopeError) as caught:
            parse_envelope(text)
        assert secret not in str(caught.value)
