"""The norn collector's pure parsers: envelopes, ``.history`` lines, ``run.json`` and the tail of
``events.jsonl``, over the committed fixtures. Unknown keys are tolerated (R3: files have no
version)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from norn_support import FILES, INBOX, inbox_files

from fylgja.collectors.claude.normalize import Envelope, EnvelopeError, parse_envelope
from fylgja.collectors.norn import parse
from fylgja.models import SessionActivity, SessionPayload

KEY = re.compile(r"norn:plan_with_review@[0-9a-f]{6}:[0-9]+")


def envelope(**payload: Any) -> Envelope:
    document = {
        "schema": "fylgja.hook/1",
        "id": "6f1e2d3c-4b5a-4c6d-8e7f-0a1b2c3d4e5f",
        "received_at_ns": 1791208800000000000,
        "source": "norn",
        "event_arg": None,
        "ppid": 1,
        "env": {},
        "payload_text": json.dumps(payload),
        "truncated": False,
        "fylgja_version": "norn-fylgja-subscriber/1",
    }
    return parse_envelope(json.dumps(document))


IDENTITY = {"state_key": "/w/x/plan", "history_run_id": 3, "pid": 7, "cwd": "/w/x"}


# --- the inbox envelopes --------------------------------------------------------------------


def _events(scenario: str) -> list[parse.NornEvent]:
    return [
        parse.parse_envelope_event(parse_envelope(path.read_bytes()))
        for path in inbox_files(scenario)
    ]


def test_every_inbox_fixture_is_a_valid_envelope_with_the_run_identity() -> None:
    names = {path.parent.name for path in INBOX.glob("*/*.json")}
    assert names == {"lifecycle", "failed", "cancelled"}
    for scenario in names:
        events = _events(scenario)
        assert events, scenario
        assert len({e.key for e in events}) == 1
        assert KEY.fullmatch(events[0].key)
        assert [e.at for e in events] == sorted(e.at for e in events)
        for event in events:
            assert event.pipeline == "plan_with_review"
            assert event.pid == 51234
            assert event.cwd == "/Users/user/projects/example"
            assert event.name in parse.EVENT_NAMES


def test_the_lifecycle_fixture_covers_every_event_of_the_plan() -> None:
    names = [e.name for e in _events("lifecycle")]
    assert {"RunStarted", "StageStarted", "StageFinished", "CallingAgent", "TurnEvent"} <= set(
        names
    )
    assert {"WaitingInput", "RunFinished", "Heartbeat"} <= set(names)
    assert "RunPaused" in {e.name for e in _events("cancelled")}
    assert "RunCancelled" in {e.name for e in _events("cancelled")}
    started = _events("lifecycle")[0]
    assert started.argv == ("/Users/user/projects/norn/norn/__main__.py", "ui")
    assert (started.provider, started.stage_total, started.history_run_id) == ("claude-code", 5, 7)
    waiting = next(e for e in _events("lifecycle") if e.name == "WaitingInput")
    assert (waiting.wait_kind, waiting.stage) == ("agent", "draft plan")
    turn = next(e for e in _events("lifecycle") if e.name == "TurnEvent")
    assert turn.session_id == "00000000-0000-4000-8000-00000000a001"
    finished = next(e for e in _events("failed") if e.name == "StageFinished")
    assert (finished.status, finished.success) == ("failed", False)
    assert finished.status in parse.STAGE_STATUSES


def test_unknown_payload_keys_and_unknown_events_are_tolerated() -> None:
    event = parse.parse_envelope_event(
        envelope(event="BrandNewEvent", future={"a": [1]}, stage_total="soon", **IDENTITY)
    )
    assert event.name == "BrandNewEvent"
    assert event.stage_total is None  # a value of the wrong type is dropped, not an error
    assert event.pid == 7


def test_the_event_name_falls_back_to_the_envelope_argument() -> None:
    document = json.loads(
        json.dumps(
            {
                "schema": "fylgja.hook/1",
                "id": "6f1e2d3c-4b5a-4c6d-8e7f-0a1b2c3d4e5f",
                "received_at_ns": 5,
                "source": "norn",
                "event_arg": "Heartbeat",
                "ppid": 1,
                "env": {},
                "payload_text": json.dumps(IDENTITY),
                "truncated": False,
                "fylgja_version": "",
            }
        )
    )
    event = parse.parse_envelope_event(parse_envelope(json.dumps(document)))
    assert event.name == "Heartbeat"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"source": "claude"}, "source does not match the inbox"),
        ({"payload_text": "[1]"}, "payload is not a JSON object"),
        ({"payload_text": "not json"}, "payload is not a JSON object"),
        ({"payload_text": json.dumps({"event": "x" * 80, **IDENTITY})}, "no usable event name"),
        (
            {"payload_text": json.dumps({**IDENTITY, "history_run_id": None})},
            "no usable event name",
        ),
        ({"payload_text": json.dumps({"event": "RunStarted"})}, "no run id"),
    ],
)
def test_unusable_envelopes_are_rejected_without_quoting_content(
    changes: dict[str, Any], message: str
) -> None:
    base = envelope(event="Heartbeat", **IDENTITY)
    document = {
        "schema": "fylgja.hook/1",
        "id": base.id,
        "received_at_ns": base.received_at_ns,
        "source": "norn",
        "event_arg": None,
        "ppid": 1,
        "env": {},
        "payload_text": base.payload_text,
        "truncated": False,
        "fylgja_version": "",
        **changes,
    }
    with pytest.raises(EnvelopeError, match=message):
        parse.parse_envelope_event(parse_envelope(json.dumps(document)))


def test_values_are_cleaned_and_capped() -> None:
    event = parse.parse_envelope_event(
        envelope(
            event="StageStarted",
            stage="a\nb " + "x" * 400,
            session_id="not a valid id!",
            pid=True,
            argv=["a"] * 100 + [3],
            **{k: v for k, v in IDENTITY.items() if k != "pid"},
        )
    )
    assert event.stage is not None
    assert "\n" not in event.stage
    assert len(event.stage) == parse.MAX_TEXT
    assert event.session_id is None
    assert event.pid is None  # a boolean is not a pid
    assert len(event.argv) == parse.MAX_ARGV


# --- keys -------------------------------------------------------------------------------------


def test_the_state_key_may_be_absolute_or_relative_to_the_working_directory() -> None:
    absolute = parse.run_key("/w/x/plan", None, 3, None)
    relative = parse.run_key("plan", "/w/x", 3, None)
    suffixed = parse.run_key("/w/x/plan.py", None, 3, None)
    assert absolute == relative == suffixed
    assert absolute is not None
    assert re.fullmatch(r"norn:plan@[0-9a-f]{6}:3", absolute)
    assert "/" not in absolute


def test_two_projects_with_the_same_state_key_and_run_id_get_different_keys() -> None:
    assert parse.run_key("/w/a/plan", None, 1, None) != parse.run_key("/w/b/plan", None, 1, None)


def test_a_run_without_history_is_named_by_its_uuid_and_without_either_it_has_no_key() -> None:
    key = parse.run_key("/w/x/plan", None, None, "5F0C2B1E-7d3a-4a52-9b86-0d1f6c1d7e01")
    assert key is not None
    assert key.endswith(":u5f0c2b1e")
    assert parse.run_key("/w/x/plan", None, None, None) is None
    assert parse.run_key("/w/x/plan", None, None, "zz") is None
    assert parse.run_key("/w/x/plan", None, -1, None) is None


def test_state_dir_and_stem_defaults() -> None:
    assert parse.state_dir_and_stem(None, "/w") == ("/w", "run")
    assert parse.state_dir_and_stem(None, None) == ("", "run")
    assert parse.state_dir_and_stem("pipeline.py", "/w/p") == ("/w/p", "pipeline")
    assert parse.state_dir_and_stem("/", None)[1]  # never an empty stem


def test_the_child_key_is_a_claude_session_key() -> None:
    assert parse.claude_session_key("abc") == "claude:abc"


# --- .history -------------------------------------------------------------------------------


def _history(case: str) -> dict[int, parse.HistoryRecord]:
    path = next((FILES / case).glob("*.history"))
    return parse.parse_history_tail(path.read_bytes(), complete=True)


def test_the_last_history_line_of_a_run_wins() -> None:
    lines = (
        (FILES / "finished" / "plan_with_review.history").read_text(encoding="utf-8").splitlines()
    )
    assert len(lines) == 5
    records = _history("finished")
    assert list(records) == [1]
    record = records[1]
    assert record.in_progress is False
    assert record.success is True
    assert len(record.stages) == 21
    assert record.timestamp is not None
    assert json.loads(lines[0])["in_progress"] is True  # the first line says otherwise


def test_history_session_ids_are_distinct_and_in_order() -> None:
    record = _history("finished")[1]
    assert len(record.session_ids) == 3
    assert record.session_ids[-1] == record.session_id or record.session_id in record.session_ids
    assert len(set(record.session_ids)) == 3


def test_the_stage_statuses_in_the_fixtures_are_the_known_ones() -> None:
    seen: set[str] = set()
    for case in ("finished", "failed", "passed", "crashed"):
        for record in _history(case).values():
            seen |= {s.status for s in record.stages if s.status}
    assert seen
    assert seen <= parse.STAGE_STATUSES


def test_a_failed_run_says_which_stage_failed() -> None:
    record = _history("failed")[14]
    assert (record.success, record.in_progress, record.failed_stage) == (
        False,
        False,
        "preflight repository",
    )
    assert any(stage.status == "failed" for stage in record.stages)


def test_a_cut_first_line_and_a_torn_last_line_are_skipped() -> None:
    lines = (FILES / "finished" / "plan_with_review.history").read_bytes().splitlines()
    cut = lines[2][40:] + b"\n" + lines[3] + b"\n" + lines[4][:30]
    assert parse.parse_history_tail(cut, complete=False) == {
        1: parse.parse_history_tail(lines[3], complete=True)[1]
    }
    assert parse.parse_history_tail(cut, complete=True) == {
        1: parse.parse_history_tail(lines[3], complete=True)[1]
    }


def test_history_lines_that_are_not_records_never_stop_the_others() -> None:
    good = json.dumps({"run_id": 5, "in_progress": False, "success": False, "extra": {"x": 1}})
    data = "\n".join(
        [
            "",
            "garbage",
            "[1, 2]",
            json.dumps({"run_id": "five"}),
            json.dumps({"run_id": True}),
            json.dumps({"no": "id"}),
            json.dumps({"run_id": 6, "stage_log": ["x", {"name": 3}, {"name": "ok"}]}),
            good,
        ]
    )
    records = parse.parse_history_tail(data.encode(), complete=True)
    assert sorted(records) == [5, 6]
    assert records[5].success is False
    assert [s.name for s in records[6].stages] == ["ok"]
    assert records[6].session_ids == ()


# --- run.json -------------------------------------------------------------------------------


def test_run_json_of_a_finished_and_of_an_unfinished_run() -> None:
    finished = parse.parse_run_json(
        (FILES / "finished/plan_with_review.runs/1/run.json").read_text()
    )
    assert finished is not None
    assert (finished.pipeline, finished.run_id, finished.history_run_id) == (
        "plan_with_review",
        1,
        1,
    )
    assert finished.success is True
    assert finished.finished_at is not None
    assert finished.argv[-1] == "ui"
    assert finished.cwd == "/Users/user/projects/erik-romson/fylgja/main"
    crashed = parse.parse_run_json(
        (FILES / "crashed/plan_with_review.runs/1/run.json").read_bytes()
    )
    assert crashed is not None
    assert (crashed.finished_at, crashed.success, crashed.failed_stage) == (None, None, None)


def test_run_json_of_an_older_norn_has_fewer_keys() -> None:
    header = parse.parse_run_json(
        json.dumps({"pipeline": "test", "run_id": 4, "state_key": "/w/pipeline.py", "x": 1})
    )
    assert header is not None
    assert (header.history_run_id, header.cwd, header.argv) == (None, None, ())
    assert parse.parse_run_json("not json") is None
    assert parse.parse_run_json("[1]") is None


# --- events.jsonl ---------------------------------------------------------------------------


def _tail(case: str, *, complete: bool = True) -> parse.EventsTail:
    path = next((FILES / case).glob("*.runs/*/events.jsonl"))
    return parse.parse_events_tail(path.read_bytes(), complete=complete)


def test_the_end_of_a_finished_run_says_it_passed_and_lists_its_sessions() -> None:
    tail = _tail("finished")
    assert tail.outcome == "passed"
    assert len(tail.session_ids) == 3
    assert tail.waiting is None


def test_a_run_blocked_in_waiting_input_says_why() -> None:
    tail = _tail("waiting")
    assert tail.waiting == "failure_recovery"
    assert tail.outcome is None


def test_a_run_cut_in_a_stage_names_the_stage_in_flight() -> None:
    tail = _tail("crashed")
    assert tail.stage == "check open questions"
    assert tail.outcome is None
    assert tail.waiting is None


def test_a_failed_run_ends_with_a_failed_finish() -> None:
    assert _tail("failed").outcome == "failed"
    assert _tail("passed").outcome == "passed"


def test_events_tail_reads_cut_files_and_unknown_events() -> None:
    lines = [
        {"type": "RunPaused"},
        {"type": "FutureEvent", "x": 1},
        {"type": "StageStarted", "name": "a"},
        {"type": "WaitingInput", "kind": "budget"},
        {"type": "RunPaused"},
        {"type": "TurnEvent", "event": {"session_id": "s-1"}},
        {"type": "TurnEvent", "event": "text"},
        {"type": "RunCancelled"},
    ]
    data = b"half a line\n" + b"\n".join(json.dumps(x).encode() for x in lines) + b"\n[1]\nnope\n"
    tail = parse.parse_events_tail(data)
    assert tail.outcome == "cancelled"
    assert tail.session_ids == ("s-1",)
    assert (tail.waiting, tail.paused) == (None, False)  # the TurnEvent cleared both
    only = parse.parse_events_tail(b'{"type":"WaitingInput"}\n{"type":"RunPaused"}', complete=True)
    assert (only.waiting, only.paused) == ("agent", True)


# --- records --------------------------------------------------------------------------------


def test_snapshots_are_complete_and_carry_the_norn_source() -> None:
    from datetime import UTC, datetime

    at = datetime(2026, 10, 5, tzinfo=UTC)
    sessions = parse.session_snapshot(
        [("norn:x@a:1", SessionPayload(kind="norn", native_id="x@a:1"))], at
    )
    activity = parse.activity_snapshot([("norn:x@a:1", SessionActivity(state="running"))], at)
    for record in (sessions, activity):
        assert record.complete is True
        assert (record.source, record.entity_id) == ("norn", "all")
        assert [i.entity_id for i in record.items] == ["norn:x@a:1"]


def test_distinct_keeps_order_and_caps() -> None:
    assert parse.distinct(["a", "b"], ["b", "c"]) == ("a", "b", "c")
    assert len(parse.distinct([str(n) for n in range(200)])) == parse.MAX_SESSIONS


def test_events_of_lines_skips_what_is_not_an_object() -> None:
    lines = [b"", b"x", b"[1]", b'{"a": 1}']
    assert list(parse.events_of_lines(lines)) == [{"a": 1}]


def test_fixture_directory_layout(tmp_path: Path) -> None:
    for case in ("finished", "crashed", "waiting", "failed", "passed"):
        assert list((FILES / case).glob("*.history")), case
        assert list((FILES / case).glob("*.checkpoint")), case
