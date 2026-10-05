"""Generate ``tests/fixtures/hooks/*.json``, the session state machine's event sequences (WP2.3).

Dev-only; the tests only read the fixtures. Run it by hand after a deliberate change:

    uv run --frozen python scripts/gen_hook_fixtures.py

Nothing on the machine is read except the sanitized ratatosk summaries in
``tests/fixtures/ratatosk/<scenario>/hook-events.ndjson``. Those summaries hold the event names,
the tool names, the reasons and the payload *keys* of real hook captures, but not the payloads.
This script rebuilds one payload per event (the documented keys, placeholder values) and attaches
the state each event must lead to. Fresh live captures are a by-hand follow-up: capture them with
``fylgja hook`` on a throwaway Claude session, sanitize them with ``scripts/sanitize_fixture.py``
and add a scenario here.

Every file is ``{"scenario", "origin", "pid", "events": [...]}``. An event is ``{"received_at_ns",
"payload", "expect"}``; ``expect`` names what the session of that event looks like afterwards
(``state``, ``tool``, ``target``, ``reason`` and, when relevant, ``predecessor``).

The ``synthesized-*`` files follow the current hooks documentation (R9) for the events and
notification types no capture holds.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
RATATOSK = ROOT / "tests" / "fixtures" / "ratatosk"
OUT = ROOT / "tests" / "fixtures" / "hooks"

BASE_NS = int(datetime(2026, 10, 5, 12, 0, tzinfo=UTC).timestamp()) * 1_000_000_000
STEP_NS = 1_000_000_000
CWD = "/work/hook-probe"
TRANSCRIPT = "/work/.claude/projects/hook-probe/transcript.jsonl"


def expect(
    state: str | None,
    *,
    tool: str | None = None,
    target: str | None = None,
    reason: str | None = None,
    predecessor: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"state": state, "tool": tool, "target": target, "reason": reason}
    if predecessor is not None:
        result["predecessor"] = predecessor
    return result


def payload_of(summary: dict[str, Any], session_id: str) -> dict[str, Any]:
    """One full-looking payload for a ratatosk summary line."""
    full = summary.get("full")
    if isinstance(full, dict):
        payload = dict(full)
        payload["session_id"] = session_id
        payload["transcript_path"] = TRANSCRIPT
        payload["cwd"] = CWD
        return payload
    event = summary["event"]
    payload: dict[str, Any] = {
        "session_id": session_id,
        "transcript_path": TRANSCRIPT,
        "cwd": CWD,
        "hook_event_name": event,
    }
    if "permission_mode" in summary.get("keys", ()) or event not in ("SessionStart", "SessionEnd"):
        payload["permission_mode"] = "default"
    tool = summary.get("tool_name")
    if tool is not None:
        payload["tool_name"] = tool
        payload["tool_input"] = {"file_path": f"{CWD}/notes.txt", "content": "placeholder"}
        if event == "PostToolUse":
            payload["tool_response"] = {"ok": True}
    if event == "UserPromptSubmit":
        payload["prompt"] = "placeholder prompt"
    if event == "Notification":
        payload["notification_type"] = "permission_prompt"
        payload["message"] = "Claude needs your permission"
    if event == "SessionEnd":
        payload["reason"] = summary.get("reason", "other")
    if event == "SessionStart":
        payload["source"] = summary.get("source", "startup")
    return payload


def load(scenario: str) -> list[dict[str, Any]]:
    path = RATATOSK / scenario / "hook-events.ndjson"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def build(
    scenario: str,
    expectations: list[dict[str, Any]],
    *,
    pid: int,
    session_ids: dict[str, str] | None = None,
) -> dict[str, Any]:
    """A sequence from the ratatosk summaries; ``session_ids`` maps captured ids to placeholders."""
    summaries = load(scenario)
    if len(summaries) != len(expectations):
        raise SystemExit(
            f"{scenario}: {len(summaries)} events but {len(expectations)} expectations"
        )
    mapping = session_ids or {}
    current = ""
    events = []
    for index, (summary, wanted) in enumerate(zip(summaries, expectations, strict=True)):
        original = summary.get("session_id") or summary.get("full", {}).get("session_id")
        if original is not None:
            current = mapping.get(original, original)
        if not current:
            raise SystemExit(f"{scenario}: event {index} has no session")
        events.append(
            {
                "received_at_ns": BASE_NS + index * STEP_NS,
                "payload": payload_of(summary, current),
                "expect": wanted,
            }
        )
    return {
        "scenario": scenario,
        "origin": f"tests/fixtures/ratatosk/{scenario}/hook-events.ndjson",
        "pid": pid,
        "events": events,
    }


def synthesized() -> dict[str, Any]:
    """Events and notification types no capture holds, as the hooks documentation shapes them."""
    sid = "00000000-0000-4000-8000-0000000000a1"

    def hook(name: str, **fields: Any) -> dict[str, Any]:
        base = {
            "session_id": sid,
            "transcript_path": TRANSCRIPT,
            "cwd": CWD,
            "permission_mode": "default",
            "hook_event_name": name,
        }
        return {**base, **fields}

    edit = {"file_path": f"{CWD}/src/module.py", "old_string": "a", "new_string": "b"}
    steps: list[tuple[dict[str, Any], dict[str, Any]]] = [
        (hook("SessionStart", source="startup", model="placeholder-model"), expect(None)),
        (hook("UserPromptSubmit", prompt="placeholder prompt"), expect("working")),
        (
            hook("PreToolUse", tool_name="Bash", tool_input={"command": "placeholder"}),
            expect("running_tool", tool="Bash"),
        ),
        (
            hook("PermissionRequest", tool_name="Bash", tool_input={"command": "placeholder"}),
            expect("waiting_permission", tool="Bash", reason="approve Bash"),
        ),
        (
            hook("PermissionDenied", tool_name="Bash", reason="denied by the user"),
            expect("working"),
        ),
        (
            hook("PreToolUse", tool_name="Edit", tool_input=edit),
            expect("running_tool", tool="Edit", target="module.py"),
        ),
        (
            hook("Notification", notification_type="permission_prompt", message="placeholder"),
            expect("waiting_permission", tool="Edit", target="module.py", reason="approve Edit"),
        ),
        (
            hook("PostToolUse", tool_name="Edit", tool_input=edit, tool_response={"ok": True}),
            expect("working"),
        ),
        (
            hook("Notification", notification_type="agent_needs_input", message="placeholder"),
            expect("waiting_input", reason="agent needs input"),
        ),
        (hook("UserPromptSubmit", prompt="placeholder answer"), expect("working")),
        (
            hook("Notification", notification_type="elicitation_dialog", message="placeholder"),
            expect("waiting_input", reason="question dialog"),
        ),
        (
            hook("PostToolUseFailure", tool_name="Bash", error="placeholder"),
            expect("working"),
        ),
        (
            hook("Stop", last_assistant_message="placeholder", stop_hook_active=False),
            expect("turn_done"),
        ),
        (
            hook("Notification", notification_type="idle_prompt", message="placeholder"),
            expect("turn_done"),  # an unseen finished turn is not overwritten by idleness
        ),
        (
            hook("Notification", notification_type="agent_completed", message="placeholder"),
            expect("turn_done"),  # names no state
        ),
        (hook("UserPromptSubmit", prompt="placeholder prompt"), expect("working")),
        (
            hook("Notification", notification_type="idle_prompt", message="placeholder"),
            expect("idle"),
        ),
        (hook("SubagentStop", agent_type="placeholder"), expect("idle")),
        (
            hook("StopFailure", error="rate_limit", last_assistant_message="placeholder"),
            expect("error"),
        ),
        (hook("SessionEnd", reason="other"), expect("ended")),
    ]
    events = [
        {
            "received_at_ns": BASE_NS + index * STEP_NS,
            "payload": payload,
            "expect": wanted,
        }
        for index, (payload, wanted) in enumerate(steps)
    ]
    return {
        "scenario": "synthesized-hooks-docs",
        "origin": "synthesized from the hooks documentation (R9); no capture holds these events",
        "pid": 4290,
        "events": events,
    }


def main() -> None:
    probe_id = "00000000-0000-4000-8000-0000000000b1"
    clear_old = "00000000-0000-4000-8000-0000000000c1"
    clear_new = "00000000-0000-4000-8000-0000000000c2"
    interrupt_id = "00000000-0000-4000-8000-0000000000d1"
    failure_id = "00000000-0000-4000-8000-0000000000e1"
    scenarios = {
        "probe-session": build(
            "probe-session",
            [
                expect(None),
                expect("working"),
                expect("turn_done"),
                expect("working"),
                expect("running_tool", tool="Bash"),
                expect("working"),
                expect("turn_done"),
                expect("working"),
                expect("running_tool", tool="Write", target="notes.txt"),
                expect(
                    "waiting_permission", tool="Write", target="notes.txt", reason="approve Write"
                ),
                expect(
                    "waiting_permission", tool="Write", target="notes.txt", reason="approve Write"
                ),
                expect("working"),
                expect("turn_done"),
                expect("ended"),
            ],
            pid=4201,
            session_ids={"d4560ae0-7cb4-4719-832a-c6f90230334f": probe_id},
        ),
        "interrupt-session": build(
            "interrupt-session",
            [expect(None), expect("working")],
            pid=4202,
            session_ids={"288dfd50-0b9a-4497-831f-b7fa5e349306": interrupt_id},
        ),
        "stopfailure-session": build(
            "stopfailure-session",
            [expect(None), expect("working"), expect("error")],
            pid=4203,
            session_ids={"d1641e5b-8fd3-40f4-8b10-a091848c94c7": failure_id},
        ),
        "clear-model-session": build(
            "clear-model-session",
            [
                expect(None),
                expect("working"),
                expect("turn_done"),
                expect("ended"),
                expect(None, predecessor=f"claude:{clear_old}"),
            ],
            pid=4204,
            session_ids={
                "07c2d066-18d3-4816-a0d3-d3b83bb11d08": clear_old,
                "d09ef654-9c0d-4e51-85c9-65eff5f3cba5": clear_new,
            },
        ),
        "synthesized-hooks-docs": synthesized(),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    for name, document in scenarios.items():
        (OUT / f"{name}.json").write_text(
            json.dumps(document, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
        )
        print(f"wrote tests/fixtures/hooks/{name}.json ({len(document['events'])} events)")


if __name__ == "__main__":
    main()
