"""The §4.6 one-liner: golden cases per rule, the Claude rules over transcript fixtures, a
property test and redaction before storage."""

from __future__ import annotations

import asyncio
import random
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from transcript_support import CLAUDE_TRANSCRIPTS, FIXTURES

from fylgja import paths
from fylgja.collectors.base import Capability, CollectorPlugin
from fylgja.collectors.claude import facts, parse
from fylgja.core import one_liner
from fylgja.core.ingest import IngestGate
from fylgja.core.one_liner import EMPTY, MAX_CHARS, RULES, session_one_liner
from fylgja.core.sessions import (
    HookSignal,
    RegistrySignal,
    SessionState,
    SessionStateEngine,
    activity_of,
    ns_of,
    reduce,
)
from fylgja.core.writer import Writer
from fylgja.models import (
    HookEvent,
    HookEventPayload,
    SessionActivity,
    SessionActivityEvent,
    SessionEvent,
    SessionPayload,
    Stage,
)
from fylgja.privacy import REDACTED, PrivacyFilter
from fylgja.store import db

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def make(**fields: Any) -> SessionActivity:
    return SessionActivity(**fields)


# --------------------------------------------------------------------------------------------
# Golden cases, one or more per rule
# --------------------------------------------------------------------------------------------

GOLDEN: list[tuple[str, SessionActivity, str, str]] = [
    # rule 1: waiting
    (
        "permission",
        make(state="waiting", current_tool="Bash", current_target="rm", title="T"),
        "Waiting for permission: Bash rm",
        "waiting",
    ),
    (
        "permission without a target",
        make(state="waiting", current_tool="Edit"),
        "Waiting for permission: Edit",
        "waiting",
    ),
    (
        "input with a reason",
        make(state="waiting", waiting_reason="input needed", current_tool=None),
        "Waiting for input: input needed",
        "waiting",
    ),
    ("input without a reason", make(state="waiting"), "Waiting for input", "waiting"),
    (
        "waiting beats everything below it",
        make(
            state="waiting",
            waiting_reason="a question",
            stage=Stage(name="build", index=1, total=2),
            open_task="t",
            title="T",
            last_prompt="p",
        ),
        "Waiting for input: a question",
        "waiting",
    ),
    (
        "hook permission with a target",
        make(state="waiting_permission", current_tool="Write", current_target="notes.txt"),
        "Waiting for permission: Write notes.txt",
        "waiting",
    ),
    (
        "hook permission without a tool",
        make(state="waiting_permission", waiting_reason="approve"),
        "Waiting for permission",
        "waiting",
    ),
    (
        "hook input names the reason, never a tool",
        make(state="waiting_input", waiting_reason="question dialog", current_tool="Bash"),
        "Waiting for input: question dialog",
        "waiting",
    ),
    ("hook input without a reason", make(state="waiting_input"), "Waiting for input", "waiting"),
    # rule 2: a norn stage
    (
        "stage with a position",
        make(state="running", title="deploy", stage=Stage(name="build", index=2, total=5)),
        "deploy · build (2/5)",
        "stage",
    ),
    (
        "stage without a pipeline",
        make(state="running", stage=Stage(name="build", index=2, total=5)),
        "build (2/5)",
        "stage",
    ),
    (
        "stage without a total",
        make(state="running", title="deploy", stage=Stage(name="build", index=2)),
        "deploy · build",
        "stage",
    ),
    (
        "a finished run",
        make(state="failed", title="deploy", stage=Stage(name="build", index=5, total=5)),
        "deploy failed",
        "stage",
    ),
    (
        "a finished run without a pipeline",
        make(state="done", stage=Stage(name="x")),
        "Run done",
        "stage",
    ),
    (
        "a stage beats the tool and the task",
        make(
            state="working",
            title="deploy",
            stage=Stage(name="test", index=3, total=4),
            current_tool="Bash",
            open_task="t",
        ),
        "deploy · test (3/4)",
        "stage",
    ),
    # rule 3: a tool
    (
        "tool and target",
        make(state="working", current_tool="Edit", current_target="/a/b.py", open_task="t"),
        "Edit /a/b.py",
        "tool",
    ),
    ("tool alone", make(state="working", current_tool="Grep"), "Grep", "tool"),
    (
        "a hook's running tool",
        make(state="running_tool", current_tool="Bash", current_target="ls"),
        "Bash ls",
        "tool",
    ),
    # rule 4: an open task
    (
        "open task",
        make(state="working", open_task="  write   the\nschema ", title="T", last_prompt="p"),
        "write the schema",
        "task",
    ),
    (
        "open task while a tool runs without a name",
        make(state="running_tool", open_task="write the schema"),
        "write the schema",
        "task",
    ),
    # rule 5: title and prompt
    (
        "title and prompt",
        make(state="idle", title="Fix the build", last_prompt="please fix it"),
        "Fix the build: please fix it",
        "title_prompt",
    ),
    ("prompt without a title", make(last_prompt="just this"), "just this", "title_prompt"),
    (
        "an idle session with a tool and a task still shows its title",
        make(state="idle", current_tool="Edit", open_task="t", title="T", last_prompt="p"),
        "T: p",
        "title_prompt",
    ),
    # rule 6: title and reply
    (
        "title and the first sentence of the reply",
        make(title="Fix the build", last_reply="Done. All tests pass now."),
        "Fix the build: Done.",
        "title_reply",
    ),
    (
        "a reply without sentence punctuation",
        make(title="T", last_reply="no punctuation here"),
        "T: no punctuation here",
        "title_reply",
    ),
    ("title alone", make(title="Just a title"), "Just a title", "title_reply"),
    (
        "reply alone",
        make(last_reply="What now? Anything."),
        "What now?",
        "title_reply",
    ),
    # rule 7: nothing
    ("nothing", make(), EMPTY, "none"),
    (
        "blank fields",
        make(title="  ", last_prompt="\n", last_reply="\t", open_task=" ", state="working"),
        EMPTY,
        "none",
    ),
]


@pytest.mark.parametrize(
    ("activity", "text", "rule"),
    [case[1:] for case in GOLDEN],
    ids=[case[0] for case in GOLDEN],
)
def test_golden_cases(activity: SessionActivity, text: str, rule: str) -> None:
    assert session_one_liner(activity) == (text, rule)


def test_every_rule_has_a_golden_case() -> None:
    assert {case[3] for case in GOLDEN} == set(RULES)


def test_the_rules_run_in_the_order_of_the_plan() -> None:
    assert RULES == ("waiting", "stage", "tool", "task", "title_prompt", "title_reply", "none")


def test_a_cap_cuts_at_160_with_an_ellipsis() -> None:
    text, rule = session_one_liner(make(title="T", last_prompt="word " * 100))
    assert rule == "title_prompt"
    assert len(text) == MAX_CHARS
    assert text.endswith("…")
    exactly, _ = session_one_liner(make(last_prompt="x" * MAX_CHARS))
    assert exactly == "x" * MAX_CHARS
    over, _ = session_one_liner(make(last_prompt="x" * (MAX_CHARS + 1)))
    assert over == "x" * (MAX_CHARS - 1) + "…"


def test_control_characters_and_newlines_never_reach_the_line() -> None:
    text, _ = session_one_liner(make(last_prompt="a\x00b\x1b[31m\r\nc\x7f"))
    assert text == "a b [31m c"


def test_first_sentence() -> None:
    assert one_liner.first_sentence("One. Two.") == "One."
    assert one_liner.first_sentence("v1.2 is out") == "v1.2 is out"
    assert one_liner.first_sentence(None) == ""
    assert one_liner.first_sentence("  spaced \n out!  Next") == "spaced out!"


def test_finalize_is_the_shared_cap_for_the_workspace_line() -> None:
    assert one_liner.WORKSPACE_SEPARATOR == " — "
    assert one_liner.finalize("") == EMPTY
    assert one_liner.finalize("a" + one_liner.WORKSPACE_SEPARATOR + "b") == "a — b"


# --------------------------------------------------------------------------------------------
# The workspace one-liner (§4.6)
# --------------------------------------------------------------------------------------------

_T = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def _ws(
    key: str,
    line: str | None,
    state: str | None = "idle",
    *,
    since: int | None = None,
    active: int | None = None,
) -> one_liner.WorkspaceSession:
    def at(minutes: int | None) -> datetime | None:
        return None if minutes is None else _T.replace(minute=minutes)

    return one_liner.WorkspaceSession(key, line, state, since=at(since), active_at=at(active))


def test_workspace_line_prefers_a_waiting_session() -> None:
    sessions = [
        _ws("a", "Edit x.py", "working", active=50),
        _ws("b", "Waiting for input: confirm", "waiting_input", since=10, active=10),
    ]
    assert (
        one_liner.workspace_one_liner("fylgja", sessions) == "fylgja — Waiting for input: confirm"
    )


def test_workspace_line_takes_the_session_waiting_longest() -> None:
    sessions = [
        _ws("a", "Waiting for permission: Bash", "waiting", since=30),
        _ws("b", "Waiting for input", "waiting_permission", since=5),
        _ws("c", "Waiting for input: no time", "waiting_input"),
    ]
    assert one_liner.workspace_one_liner("ws", sessions) == "ws — Waiting for input"


def test_workspace_line_else_takes_the_most_recently_active_session() -> None:
    sessions = [
        _ws("a", "Old work", active=1),
        _ws("b", "Newest work", active=40),
        _ws("c", "Undated work"),
    ]
    assert one_liner.workspace_one_liner("ws", sessions) == "ws — Newest work"
    undated = [_ws("b", "Second"), _ws("a", "First")]
    assert one_liner.workspace_one_liner("ws", undated) == "ws — First"


def test_workspace_line_skips_sessions_that_say_nothing() -> None:
    sessions = [_ws("a", EMPTY, "waiting", since=1), _ws("b", None), _ws("c", "Real", active=2)]
    assert one_liner.workspace_one_liner("ws", sessions) == "ws — Real"


def test_workspace_line_falls_back_to_title_context_then_the_label() -> None:
    assert one_liner.workspace_one_liner("ws", [], title_context="notes.md") == "ws — notes.md"
    assert one_liner.workspace_one_liner("ws", []) == "ws"
    assert one_liner.workspace_one_liner(EMPTY, [_ws("a", "Alone", active=1)]) == "Alone"
    assert one_liner.workspace_one_liner(None, []) == EMPTY
    assert one_liner.workspace_one_liner(EMPTY, []) == EMPTY


def test_workspace_line_is_capped_and_redacted() -> None:
    long_line = one_liner.workspace_one_liner("label", [_ws("a", "x" * 400, active=1)])
    assert len(long_line) == MAX_CHARS
    assert long_line.endswith("…")
    redacted = one_liner.workspace_one_liner(
        "label", [_ws("a", "secret", active=1)], redact=lambda text: text.replace("secret", "***")
    )
    assert redacted == "label — ***"


def test_waiting_states_cover_registry_and_hooks() -> None:
    assert {"waiting", "waiting_permission", "waiting_input"} == one_liner.WAITING_STATES
    assert _ws("a", "x", "waiting_input").waiting
    assert not _ws("a", "x", "working").waiting


# --------------------------------------------------------------------------------------------
# The Claude rules over the transcript fixtures
# --------------------------------------------------------------------------------------------


def _row(status: str, *, name: str | None = None, waiting_for: str | None = None) -> Any:
    return parse.RegistryRow(
        pid=4300,
        session_id="s",
        cwd="/work",
        status=status,
        name=name,
        waiting_for=waiting_for,
    )


def _facts_of(path: Path) -> tuple[dict[str, Any], facts.PendingTool | None]:
    accumulator = facts.FactsAccumulator()
    accumulator.feed_all(
        e
        for e in (facts.decode_line(x) for x in path.read_text(encoding="utf-8").split("\n"))
        if e is not None
    )
    return accumulator.facts(), accumulator.pending_tool()


def _claude(path: Path, status: str, **row: Any) -> tuple[str, str]:
    result, pending = _facts_of(path)
    return session_one_liner(parse.activity_of(_row(status, **row), result, pending))


LIDSKJALV = FIXTURES / "lidskjalv" / "transcripts" / "test-1.jsonl"
RATATOSK = FIXTURES / "ratatosk"


def test_claude_idle_session_shows_title_and_last_prompt() -> None:
    assert _claude(LIDSKJALV, "idle") == (
        "Exporter wiring and tests: add a test for the exporter",
        "title_prompt",
    )


def test_claude_working_session_shows_the_task_in_progress() -> None:
    assert _claude(LIDSKJALV, "busy") == ("add a test", "task")


def test_claude_waiting_session_without_a_pending_tool_waits_for_input() -> None:
    assert _claude(LIDSKJALV, "waiting", waiting_for="input needed") == (
        "Waiting for input: input needed",
        "waiting",
    )


def test_claude_working_session_shows_the_running_tool() -> None:
    path = CLAUDE_TRANSCRIPTS / "running-tool-synthetic.jsonl"
    assert _claude(path, "busy") == ("Bash pytest", "tool")
    assert _claude(path, "waiting") == ("Waiting for permission: Bash pytest", "waiting")
    # An idle session is not running anything, whatever the transcript's last line says.
    assert _claude(path, "idle", name="Fix the build")[1] == "title_prompt"


def test_claude_session_with_task_tools_shows_its_open_task() -> None:
    path = CLAUDE_TRANSCRIPTS / "task-tools-synthetic.jsonl"
    assert _claude(path, "busy") == ("write the schema", "task")


@pytest.mark.parametrize(
    ("transcript", "status", "name", "expected"),
    [
        (
            "clear-model-session/transcript-after-clear.jsonl",
            "idle",
            None,
            (EMPTY, "none"),
        ),
        (
            "clear-model-session/transcript-after-clear.jsonl",
            "idle",
            "kept name",
            ("kept name", "title_reply"),
        ),
        (
            "clear-model-session/transcript-before-clear.jsonl",
            "idle",
            None,
            ("Say: one", "title_prompt"),
        ),
        (
            "interrupt-session/transcript.jsonl",
            "idle",
            None,
            (
                "Write essay on Ratatoskr Norse mythology: [Request interrupted by user]",
                "title_prompt",
            ),
        ),
        (
            "stopfailure-session/transcript.jsonl",
            "idle",
            None,
            ("Say hello.", "title_prompt"),
        ),
    ],
)
def test_claude_rules_over_the_ratatosk_transcripts(
    transcript: str, status: str, name: str | None, expected: tuple[str, str]
) -> None:
    assert _claude(RATATOSK / transcript, status, name=name) == expected


def test_claude_probe_session_one_liner_is_capped() -> None:
    text, rule = _claude(RATATOSK / "probe-session" / "transcript.jsonl", "idle")
    assert rule == "title_prompt"
    assert text.startswith("Respond with pong: Create a new file named probe.txt")
    assert len(text) <= MAX_CHARS


# --------------------------------------------------------------------------------------------
# The Codex rules over the sanitized state rows and rollouts (WP3.1)
# --------------------------------------------------------------------------------------------


def _codex(number: int, rollout: str | None) -> tuple[str, str]:
    from codex_support import ROLLOUT_FIXTURES, rows_fixture, thread_id

    from fylgja.collectors.codex import parse as codex_parse

    row = next(r for r in rows_fixture()["threads"] if r["id"] == thread_id(number))
    facts = None
    if rollout is not None:
        lines = (ROLLOUT_FIXTURES / rollout).read_bytes().split(b"\n")
        facts = codex_parse.analyze_rollout(lines)
    return session_one_liner(codex_parse.activity_of(codex_parse.thread_row(row), facts))


@pytest.mark.parametrize(
    ("number", "rollout", "expected"),
    [
        # Idle, and the generated title is just the start of the prompt: the prompt alone.
        (1, "idle.jsonl", ("Add pagination to the orders endpoint", "title_prompt")),
        # Busy with a function call that has no output yet: rule 3, the current call.
        (2, "busy-tool.jsonl", ("exec_command pytest tests/test_login.py -x --lf", "tool")),
        # A pending apply_patch names the file it touches.
        (3, "busy-patch.jsonl", ("apply_patch src/helpers.py", "tool")),
        # An aborted turn is idle: no current call, the last prompt shows.
        (4, "aborted.jsonl", ("Summarize the changelog", "title_prompt")),
        # No turn yet: only the title.
        (6, "fresh.jsonl", ("Sub-agent thread", "title_reply")),
        # No rollout readable: the title still says what the thread is.
        (2, None, ("Login test failure", "title_reply")),
    ],
)
def test_codex_one_liner_goldens(
    number: int, rollout: str | None, expected: tuple[str, str]
) -> None:
    assert _codex(number, rollout) == expected


def test_codex_busy_without_a_current_call_shows_title_and_prompt() -> None:
    activity = SessionActivity(state="busy", title="Login test failure", last_prompt="Fix it")
    assert session_one_liner(activity) == ("Login test failure: Fix it", "title_prompt")


def test_codex_busy_is_a_working_state_and_idle_hides_the_call() -> None:
    assert "busy" in one_liner.WORKING_STATES
    busy = SessionActivity(state="busy", current_tool="exec_command", current_target="ls")
    assert session_one_liner(busy) == ("exec_command ls", "tool")
    idle = busy.model_copy(update={"state": "idle"})
    assert session_one_liner(idle) == (EMPTY, "none")


def test_every_fixture_session_gets_a_non_empty_line_in_every_state() -> None:
    paths_ = [LIDSKJALV, *sorted(RATATOSK.glob("*/*.jsonl")), *sorted(CLAUDE_TRANSCRIPTS.glob("*"))]
    for path in paths_:
        for status in ("idle", "busy", "waiting"):
            text, rule = _claude(path, status, name="n")
            assert text
            assert rule in RULES


# --------------------------------------------------------------------------------------------
# The waiting rules driven from real state transitions (WP2.3)
# --------------------------------------------------------------------------------------------


def _signal(event: str, n: int, **fields: Any) -> HookSignal:
    moment = T0.replace(second=n)
    return HookSignal(event=event, received_at_ns=ns_of(moment), observed_at=moment, **fields)


def _line(state: SessionState | None, base: SessionActivity | None = None) -> tuple[str, str]:
    assert state is not None
    return session_one_liner(activity_of(state, base))


TRANSITIONS: list[tuple[str, list[HookSignal], tuple[str, str]]] = [
    ("a prompt", [_signal("UserPromptSubmit", 1)], ("Fix: add a test", "title_prompt")),
    (
        "a tool starts",
        [_signal("UserPromptSubmit", 1), _signal("PreToolUse", 2, tool_name="Bash")],
        ("Bash", "tool"),
    ),
    (
        "a file tool starts",
        [_signal("PreToolUse", 1, tool_name="Edit", target="module.py")],
        ("Edit module.py", "tool"),
    ),
    (
        "the tool asks for permission",
        [
            _signal("PreToolUse", 1, tool_name="Edit", target="module.py"),
            _signal("PermissionRequest", 2, tool_name="Edit", target="module.py"),
        ],
        ("Waiting for permission: Edit module.py", "waiting"),
    ),
    (
        "the notification follows the request",
        [
            _signal("PreToolUse", 1, tool_name="Edit", target="module.py"),
            _signal("PermissionRequest", 2, tool_name="Edit", target="module.py"),
            _signal("Notification", 3, notification_type="permission_prompt"),
        ],
        ("Waiting for permission: Edit module.py", "waiting"),
    ),
    (
        "a notification alone",
        [_signal("Notification", 1, notification_type="permission_prompt")],
        ("Waiting for permission", "waiting"),
    ),
    (
        "the user grants it",
        [
            _signal("PermissionRequest", 1, tool_name="Bash"),
            _signal("PostToolUse", 2, tool_name="Bash"),
        ],
        ("Fix: add a test", "title_prompt"),
    ),
    (
        "the agent needs input",
        [_signal("Notification", 1, notification_type="agent_needs_input")],
        ("Waiting for input: agent needs input", "waiting"),
    ),
    (
        "an elicitation dialog",
        [
            _signal("PreToolUse", 1, tool_name="Bash"),
            _signal("Notification", 2, notification_type="elicitation_dialog"),
        ],
        ("Waiting for input: question dialog", "waiting"),
    ),
    (
        "the turn is done",
        [_signal("UserPromptSubmit", 1), _signal("Stop", 2)],
        ("Fix: add a test", "title_prompt"),
    ),
    (
        "the person answers",
        [
            _signal("Notification", 1, notification_type="agent_needs_input"),
            _signal("UserPromptSubmit", 2),
        ],
        ("Fix: add a test", "title_prompt"),
    ),
]


@pytest.mark.parametrize(
    ("events", "expected"),
    [case[1:] for case in TRANSITIONS],
    ids=[case[0] for case in TRANSITIONS],
)
def test_the_waiting_rules_fire_from_real_state_transitions(
    events: list[HookSignal], expected: tuple[str, str]
) -> None:
    base = SessionActivity(title="Fix", last_prompt="add a test")
    state: SessionState | None = None
    for event in events:
        state = reduce(state, event)
    assert _line(state, base) == expected


def test_a_hook_state_replaces_the_registry_waiting_line() -> None:
    registry = SessionActivity(
        state="waiting",
        current_tool="Bash",
        current_target="rm -rf build",
        waiting_reason="approve Bash",
        title="Fix",
    )
    assert session_one_liner(registry) == ("Waiting for permission: Bash rm -rf build", "waiting")
    granted = reduce(
        reduce(None, _signal("PermissionRequest", 1, tool_name="Bash")),
        _signal("PostToolUse", 2, tool_name="Bash"),
    )
    assert _line(granted, registry) == ("Fix", "title_reply")  # no longer waiting
    asked = reduce(None, _signal("PermissionRequest", 1, tool_name="Bash"))
    assert _line(asked, registry) == ("Waiting for permission: Bash rm -rf build", "waiting")


def test_the_registry_is_the_fallback_before_the_first_hook() -> None:
    state = reduce(None, RegistrySignal("waiting", T0, waiting_reason="input needed"))
    assert _line(state, SessionActivity(state="waiting", waiting_reason="input needed")) == (
        "Waiting for input: input needed",
        "waiting",
    )


def test_the_engine_hands_the_writer_activities_whose_line_is_the_waiting_rule() -> None:
    emitted: list[Any] = []
    engine = SessionStateEngine(emitted.append, lambda key: None)

    def record(event: str, n: int, **fields: Any) -> HookEvent:
        return HookEvent(
            source="claude",
            entity_id="claude:s",
            observed_at=T0.replace(second=n),
            payload=HookEventPayload(session_id="s", event=event, **fields),
        )

    engine.apply(record("PreToolUse", 1, tool_name="Write", edited_paths=("/w/notes.txt",)))
    engine.apply(record("PermissionRequest", 2, tool_name="Write", edited_paths=("/w/notes.txt",)))
    engine.apply(record("Notification", 3, notification_type="elicitation_dialog"))
    lines = [
        session_one_liner(item.payload)
        for item in emitted
        if isinstance(item, SessionActivityEvent)
    ]
    assert lines == [
        ("Write notes.txt", "tool"),
        ("Waiting for permission: Write notes.txt", "waiting"),
        ("Waiting for input: question dialog", "waiting"),
    ]


# --------------------------------------------------------------------------------------------
# Property: any activity gives a non-empty line of at most 160 characters
# --------------------------------------------------------------------------------------------

_ALPHABET = "abc XYZ.!?\n\t\x00\x1b—é日本😀[]():/\\'\"0123456789"
_STATES = [
    None,
    "idle",
    "working",
    "waiting",
    "running",
    "failed",
    "done",
    "cancelled",
    "x",
    "running_tool",
    "waiting_permission",
    "waiting_input",
    "turn_done",
    "seen",
    "error",
    "ended",
]


def _random_text(rng: random.Random) -> str | None:
    kind = rng.random()
    if kind < 0.25:
        return None
    if kind < 0.35:
        return ""
    length = rng.choice([1, 3, 20, 159, 160, 161, 400, 2000])
    return "".join(rng.choice(_ALPHABET) for _ in range(length))


def _random_activity(rng: random.Random) -> SessionActivity:
    stage = None
    if rng.random() < 0.4:
        stage = Stage(
            name=_random_text(rng),
            index=rng.choice([None, 0, 3, 10**9]),
            total=rng.choice([None, 0, 7, 10**9]),
        )
    return SessionActivity(
        state=rng.choice(_STATES),
        waiting_reason=_random_text(rng),
        current_tool=_random_text(rng),
        current_target=_random_text(rng),
        open_task=_random_text(rng),
        stage=stage,
        last_prompt=_random_text(rng),
        last_reply=_random_text(rng),
        title=_random_text(rng),
        branch=_random_text(rng),
    )


@pytest.mark.parametrize("seed", range(40))
def test_any_activity_yields_a_short_non_empty_line(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(100):
        activity = _random_activity(rng)
        text, rule = session_one_liner(activity)
        assert text
        assert 1 <= len(text) <= MAX_CHARS
        assert rule in RULES
        assert "\n" not in text
        assert "\x00" not in text
        assert session_one_liner(activity) == (text, rule)  # deterministic


def test_the_property_covers_every_rule() -> None:
    rng = random.Random(7)
    seen = {session_one_liner(_random_activity(rng))[1] for _ in range(4000)}
    assert seen == set(RULES)


# --------------------------------------------------------------------------------------------
# Redaction comes before storage
# --------------------------------------------------------------------------------------------

SECRET = "sk-canary-0123456789abcdefghij"
PATTERN = r"sk-[A-Za-z0-9-]{10,}"


def test_the_line_is_redacted_when_a_redactor_is_given() -> None:
    privacy = PrivacyFilter(redact_patterns=[PATTERN])
    activity = make(state="working", current_tool="Bash", current_target=SECRET)
    text, rule = session_one_liner(activity, redact=privacy.redact)
    assert (text, rule) == (f"Bash {REDACTED}", "tool")
    # A secret that is only formed by the composition is caught too.
    split = make(title="sk-canary", last_prompt="0123456789abcdef")
    assert session_one_liner(split, redact=lambda t: t.replace(": ", "-"))[0] == (
        "sk-canary-0123456789abcdef"
    )
    assert SECRET not in session_one_liner(make(last_prompt=SECRET), redact=privacy.redact)[0]


class _Empty(BaseModel):
    pass


class _Clock:
    def now(self) -> datetime:
        return T0

    def monotonic(self) -> float:
        return 0.0

    async def sleep(self, seconds: float) -> None:
        await asyncio.Event().wait()


def _rows(path: Path, sql: str) -> list[sqlite3.Row]:
    connection = db.connect_reader(path)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


async def test_the_stored_one_liner_is_redacted_capped_and_follows_the_title() -> None:
    plugin = CollectorPlugin(
        name="claude",
        api_major=1,
        capabilities=frozenset({Capability.SESSIONS}),
        config_model=_Empty,
        factory=lambda settings, context: None,
    )
    writer = Writer(paths.database_file(), clock=_Clock())
    await writer.start()
    try:
        gate = IngestGate(
            writer, [plugin], clock=_Clock(), privacy=PrivacyFilter(redact_patterns=[PATTERN])
        )
        sink = gate.sink("claude")
        session = SessionEvent(
            source="claude",
            entity_id="claude:s1",
            observed_at=T0,
            payload=SessionPayload(kind="claude", native_id="s1"),
        )
        sink.emit(session)

        def activity(seconds: int, **fields: Any) -> SessionActivityEvent:
            return SessionActivityEvent(
                source="claude",
                entity_id="claude:s1",
                observed_at=T0.replace(second=seconds),
                payload=SessionActivity(**fields),
            )

        sink.emit(activity(1, state="idle", title="Fix", last_prompt=f"use {SECRET} " + "z" * 400))
        sink.emit(activity(2, state="working", current_tool="Bash", current_target=SECRET))
        sink.emit(activity(3, state="idle", last_prompt="x"))  # no title: the session's is kept
        sink.emit(activity(4, state="idle", title="Renamed", last_prompt="x"))
        await writer.flush()
        history = _rows(
            paths.database_file(),
            "SELECT one_liner, last_prompt FROM session_snapshots ORDER BY id",
        )
    finally:
        await writer.stop()
    lines = [row["one_liner"] for row in history]
    assert lines[0].startswith(f"Fix: use {REDACTED} zzz")
    assert len(lines[0]) <= MAX_CHARS
    assert lines[1] == f"Bash {REDACTED}"
    assert lines[2] == "Fix: x"
    assert lines[3] == "Renamed: x"  # only the title changed, yet a new interval was opened
    assert all(SECRET not in str(value) for row in history for value in tuple(row))
