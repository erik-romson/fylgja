"""Privacy (§4.4, A5): redaction before the row is written, the ignore lists, the caps and the
A5 hook-payload allowlist."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from fylgja import paths
from fylgja.collectors.base import Capability, CollectorPlugin
from fylgja.config import load_config
from fylgja.core.ingest import IngestGate
from fylgja.core.live_view import LiveView
from fylgja.core.writer import Writer
from fylgja.models import (
    ContextItemEvent,
    ContextItemPayload,
    FocusEvent,
    FocusPayload,
    PresenceEvent,
    PresencePayload,
    SessionActivity,
    SessionActivityEvent,
    SessionPayload,
    SessionSnapshot,
    SnapshotItem,
    TodoEvent,
    TodoPayload,
    WindowEvent,
    WindowPayload,
    WindowSnapshot,
)
from fylgja.privacy import (
    HOOK_FIELDS,
    PROMPT_MAX_CHARS,
    REDACTED,
    REPLY_MAX_CHARS,
    PrivacyFilter,
    minimize_hook_payload,
    truncate,
)
from fylgja.store import db

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
SECRET = "sk-CANARYabcdefghijklmnop0123"


class Clock:
    def now(self) -> datetime:
        return T0

    def monotonic(self) -> float:
        return 0.0

    async def sleep(self, seconds: float) -> None:  # pragma: no cover - unused
        raise AssertionError


class EmptyConfig(BaseModel):
    pass


def make_filter() -> PrivacyFilter:
    return PrivacyFilter(
        redact_patterns=[r"sk-[A-Za-z0-9_-]{16,}", r"(?i)project\s+falcon"],
        ignore_bundle_ids=["com.example.vault"],
        ignore_title_patterns=[r"(?i)\bincognito\b"],
    )


def window(title: str | None = None, bundle: str | None = None, wid: str = "w1") -> WindowEvent:
    return WindowEvent(
        source="aero",
        entity_id=wid,
        observed_at=T0,
        payload=WindowPayload(title=title, app_bundle_id=bundle),
    )


# --------------------------------------------------------------------------------------------
# Redaction is applied before the row is written
# --------------------------------------------------------------------------------------------


@pytest.fixture
async def stack() -> AsyncIterator[tuple[IngestGate, Writer, LiveView]]:
    view = LiveView()
    writer = Writer(paths.database_file(), clock=Clock(), on_commit=view.apply)
    plugins = [
        CollectorPlugin(
            name=name,
            api_major=1,
            capabilities=frozenset(caps),
            config_model=EmptyConfig,
            factory=lambda settings, context: None,  # type: ignore[arg-type,return-value]
        )
        for name, caps in (
            ("aero", {Capability.WINDOWS}),
            ("claude", {Capability.SESSIONS}),
        )
    ]
    gate = IngestGate(writer, plugins, privacy=make_filter(), clock=Clock())
    await writer.start()
    try:
        yield gate, writer, view
    finally:
        await writer.stop()


def database_bytes() -> bytes:
    base = paths.database_file()
    parts = [base, Path(f"{base}-wal")]
    return b"".join(part.read_bytes() for part in parts if part.exists())


async def test_the_stored_row_never_contains_the_raw_value(
    stack: tuple[IngestGate, Writer, LiveView],
) -> None:
    gate, writer, view = stack
    published: list[Any] = []
    view.subscribe(published.append)
    gate.emit("aero", window(title=f"deploy {SECRET} now"))
    gate.emit(
        "claude",
        SessionActivityEvent(
            source="claude",
            entity_id="claude:s1",
            observed_at=T0,
            payload=SessionActivity(
                title="Project Falcon review",
                last_prompt=f"please use {SECRET}",
                last_reply=f"I used {SECRET}",
            ),
        ),
    )
    await gate.sink("claude").emit_committed(
        SessionSnapshot(
            source="claude",
            entity_id="registry",
            observed_at=T0,
            items=(
                SnapshotItem(
                    entity_id="claude:s2",
                    payload=SessionPayload(kind="claude", native_id="s2", name=f"name {SECRET}"),
                ),
            ),
        )
    )
    await writer.flush()

    raw = database_bytes()
    assert raw  # the database and its WAL were written
    assert SECRET.encode() not in raw
    assert b"Falcon" not in raw
    reader = db.connect_reader(paths.database_file())
    try:
        title = reader.execute("SELECT title FROM windows_snapshot").fetchone()[0]
        prompt = reader.execute("SELECT last_prompt FROM session_snapshots").fetchone()[0]
        payloads = [r[0] for r in reader.execute("SELECT payload FROM events")]
    finally:
        reader.close()
    assert title == f"deploy {REDACTED} now"
    assert prompt == f"please use {REDACTED}"
    assert all(SECRET not in payload for payload in payloads)
    # nothing unredacted was published to live-view subscribers (SSE) either
    assert SECRET not in repr(published)
    assert view.sessions["claude:s1"].title == f"{REDACTED} review"
    assert view.sessions["claude:s2"].name == f"name {REDACTED}"


async def test_ignored_windows_never_reach_sqlite(
    stack: tuple[IngestGate, Writer, LiveView],
) -> None:
    gate, writer, view = stack
    gate.emit("aero", window(title="vault entry", bundle="com.example.vault", wid="w1"))
    gate.emit("aero", window(title="Incognito - Browser", wid="w2"))
    gate.emit(
        "aero",
        WindowSnapshot(
            source="aero",
            entity_id="all",
            observed_at=T0,
            complete=True,
            items=(
                SnapshotItem(entity_id="w3", payload=WindowPayload(title="editor")),
                SnapshotItem(
                    entity_id="w4", payload=WindowPayload(app_bundle_id="com.example.vault")
                ),
            ),
        ),
    )
    await writer.flush()
    assert set(view.windows) == {"w3"}
    assert b"vault entry" not in database_bytes()
    assert gate.stats("aero").dropped == 2


# --------------------------------------------------------------------------------------------
# The filter itself
# --------------------------------------------------------------------------------------------


def test_titles_and_text_are_redacted() -> None:
    privacy = make_filter()
    focus = FocusEvent(
        source="aero", entity_id="w", observed_at=T0, payload=FocusPayload(title=f"x {SECRET}")
    )
    filtered = privacy.filter(focus)
    assert isinstance(filtered, FocusEvent)
    assert filtered.payload.title == f"x {REDACTED}"
    assert filtered.dedup_key == focus.dedup_key  # identity is kept
    todo = TodoEvent(
        source="todo_md",
        entity_id="1",
        observed_at=T0,
        payload=TodoPayload(text=f"rotate {SECRET}"),
    )
    filtered_todo = privacy.filter(todo)
    assert isinstance(filtered_todo, TodoEvent)
    assert filtered_todo.payload.text == f"rotate {REDACTED}"


def test_unchanged_records_are_returned_as_is() -> None:
    privacy = make_filter()
    clean = window(title="plain")
    assert privacy.filter(clean) is clean
    presence = PresenceEvent(
        source="presence", entity_id="me", observed_at=T0, payload=PresencePayload(state="idle")
    )
    assert privacy.filter(presence) is presence
    snapshot = WindowSnapshot(
        source="aero",
        entity_id="all",
        observed_at=T0,
        items=(SnapshotItem(entity_id="w1", payload=WindowPayload(title="plain")),),
    )
    assert privacy.filter(snapshot) is snapshot


def test_prompt_and_reply_are_capped_after_redaction() -> None:
    privacy = make_filter()
    prompt = "a" * 290 + " " + SECRET + " " + "b" * 400
    record = SessionActivityEvent(
        source="claude",
        entity_id="claude:s",
        observed_at=T0,
        payload=SessionActivity(last_prompt=prompt, last_reply="r" * 2000, open_task=SECRET),
    )
    filtered = privacy.filter(record)
    assert isinstance(filtered, SessionActivityEvent)
    activity = filtered.payload
    assert activity.last_prompt is not None
    assert len(activity.last_prompt) == PROMPT_MAX_CHARS
    assert SECRET[:8] not in activity.last_prompt  # redacted before the cut, not cut mid-secret
    assert activity.last_prompt.endswith(" [redacted")  # 291 + 9 characters
    assert activity.last_reply == "r" * REPLY_MAX_CHARS
    assert activity.open_task == REDACTED


def test_context_item_data_is_redacted_recursively() -> None:
    privacy = make_filter()
    record = ContextItemEvent(
        source="appcontext",
        entity_id="tab",
        observed_at=T0,
        payload=ContextItemPayload(
            context_kind="tab",
            title="docs",
            data={"url": f"https://x/?k={SECRET}", "list": [SECRET, 3], "n": None},
        ),
    )
    filtered = privacy.filter(record)
    assert isinstance(filtered, ContextItemEvent)
    assert filtered.payload.data == {
        "url": f"https://x/?k={REDACTED}",
        "list": [REDACTED, 3],
        "n": None,
    }


def test_ignore_lists() -> None:
    privacy = make_filter()
    assert privacy.filter(window(bundle="com.example.vault")) is None
    assert privacy.filter(window(title="Private - INCOGNITO window")) is None
    assert privacy.filter(window(title="incognitoish")) is not None  # word boundary
    focus = FocusEvent(
        source="aero",
        entity_id="w",
        observed_at=T0,
        payload=FocusPayload(app_bundle_id="com.example.vault"),
    )
    assert privacy.filter(focus) is None
    assert privacy.ignores_bundle(None) is False
    assert privacy.ignores_title(None) is False


def test_filter_refuses_non_records() -> None:
    with pytest.raises(TypeError, match="not a record"):
        make_filter().filter(WindowPayload())  # type: ignore[arg-type]


def test_from_config_uses_the_privacy_section() -> None:
    privacy = PrivacyFilter.from_config(load_config().privacy)
    assert privacy.redact("key ghp_" + "a" * 30) == f"key {REDACTED}"
    assert privacy.ignores_bundle("com.1password.1password")
    assert privacy.ignores_title("New Incognito Tab")


def test_truncate() -> None:
    assert truncate("abc", 5) == "abc"
    assert truncate("abcdef", 3) == "abc"


# --------------------------------------------------------------------------------------------
# A5: the hook payload minimizer
# --------------------------------------------------------------------------------------------

RAW_POST_TOOL_USE: dict[str, Any] = {
    "session_id": "abc-123",
    "transcript_path": "/Users/user/.claude/projects/x/abc-123.jsonl",
    "cwd": "/Users/user/projects/demo",
    "permission_mode": "bypassPermissions",
    "hook_event_name": "PostToolUse",
    "tool_name": "Edit",
    "tool_input": {
        "file_path": "/Users/user/projects/demo/app.py",
        "old_string": "BODY-OLD",
        "new_string": "BODY-NEW",
    },
    "tool_response": {"filePath": "/Users/user/projects/demo/app.py", "patch": "BODY-PATCH"},
    "tool_use_id": "toolu_1",
}


def test_minimizer_keeps_only_the_allowlist() -> None:
    minimized = minimize_hook_payload(RAW_POST_TOOL_USE)
    assert set(minimized) <= set(HOOK_FIELDS)
    assert minimized == {
        "session_id": "abc-123",
        "event": "PostToolUse",
        "tool_name": "Edit",
        "cwd": "/Users/user/projects/demo",
        "permission_mode": "bypassPermissions",
        "edited_paths": ["/Users/user/projects/demo/app.py"],
    }
    assert "BODY" not in repr(minimized)


def test_minimizer_prompt_notification_and_reason() -> None:
    privacy = make_filter()
    prompt = minimize_hook_payload(
        {"hook_event_name": "UserPromptSubmit", "prompt": f"{SECRET} " + "x" * 500},
        redact=privacy.redact,
    )
    assert prompt["event"] == "UserPromptSubmit"
    assert len(prompt["prompt"]) == PROMPT_MAX_CHARS
    assert prompt["prompt"].startswith(REDACTED)
    assert privacy.minimize_hook_payload({"prompt": SECRET})["prompt"] == REDACTED
    notification = minimize_hook_payload(
        {
            "hook_event_name": "Notification",
            "notification_type": "permission_prompt",
            "message": "Claude needs your permission to use Bash",
        }
    )
    assert notification == {"event": "Notification", "notification_type": "permission_prompt"}
    end = minimize_hook_payload({"event": "SessionEnd", "reason": "clear", "extra": {"a": 1}})
    assert end == {"event": "SessionEnd", "reason": "clear"}


def test_minimizer_drops_non_strings_and_non_edit_paths() -> None:
    minimized = minimize_hook_payload(
        {
            "session_id": 42,
            "cwd": "",
            "tool_name": "Read",
            "tool_input": {"file_path": "/etc/hosts"},
            "prompt": None,
        }
    )
    assert minimized == {"tool_name": "Read"}
    notebook = minimize_hook_payload(
        {"tool_name": "NotebookEdit", "tool_input": {"notebook_path": "/n.ipynb", "file_path": 3}}
    )
    assert notebook["edited_paths"] == ["/n.ipynb"]
    assert "edited_paths" not in minimize_hook_payload({"tool_name": "Write", "tool_input": "x"})
    long_cwd = minimize_hook_payload({"cwd": "/" + "d" * 5000})
    assert len(long_cwd["cwd"]) == 1024
