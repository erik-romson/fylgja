"""Pure parsers of the AeroSpace collector: one fixture per event type, snapshots, torn lines."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fylgja.collectors.aerospace import parse
from fylgja.models import EntityKind, parse_record

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "aerospace"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def _lines(name: str) -> list[bytes]:
    return (FIXTURES / name).read_bytes().splitlines(keepends=True)


def _event(line: bytes | str) -> parse.AeroEvent:
    parsed = parse.parse_event_line(line)
    assert parsed is not None, "blank line"
    assert not isinstance(parsed, parse.MalformedLine), parsed
    return parsed


# -- events: one fixture per type ------------------------------------------------------------


def test_the_captured_stream_has_the_four_connect_events() -> None:
    events = [_event(line) for line in _lines("events.ndjson")]
    assert [type(e) for e in events] == [
        parse.FocusChanged,
        parse.ModeChanged,
        parse.FocusedMonitorChanged,
        parse.FocusedWorkspaceChanged,
    ]


def test_focus_changed() -> None:
    event = _event(_lines("events.ndjson")[0])
    assert event == parse.FocusChanged(window_id="1898", workspace="2")


def test_focused_workspace_changed() -> None:
    event = _event(_lines("events.ndjson")[3])
    assert event == parse.FocusedWorkspaceChanged(workspace="2", previous="2")


def test_focused_monitor_changed() -> None:
    event = _event(_lines("events.ndjson")[2])
    assert event == parse.FocusedMonitorChanged(monitor_id="1", workspace="2")


def test_mode_changed() -> None:
    assert _event(_lines("events.ndjson")[1]) == parse.ModeChanged(mode="main")


def test_window_detected_and_binding_triggered_are_tolerated() -> None:
    detected, binding, future = (_event(line) for line in _lines("events-synthetic.ndjson"))
    assert detected == parse.WindowDetected(window_id="4242", workspace="3")
    assert binding == parse.BindingTriggered(mode="main")
    assert "binding" not in repr(binding), "the keystroke is never kept"
    assert future == parse.UnknownEvent(name="some-future-event")


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (b'{"_event":"focus-changed","windowId":7,"workspace":"w"}\n', True),
        (b'{"_event":"focused-workspace-changed","workspace":"w"}\n', True),
        (b'{"_event":"focused-monitor-changed","monitorId":1,"workspace":"w"}\n', True),
        (b'{"_event":"window-detected","windowId":7}\n', True),
        (b'{"_event":"mode-changed","mode":"service"}\n', False),
        (b'{"_event":"binding-triggered","mode":"main"}\n', False),
        (b'{"_event":"brand-new"}\n', False),
    ],
)
def test_which_events_trigger_a_snapshot(line: bytes, expected: bool) -> None:
    assert parse.triggers_snapshot(_event(line)) is expected


def test_event_fields_of_the_wrong_type_become_none() -> None:
    event = _event('{"_event":"focus-changed","windowId":true,"workspace":["x"]}\n')
    assert event == parse.FocusChanged(window_id=None, workspace=None)
    mode = _event('{"_event":"mode-changed","mode":5}\n')
    assert mode == parse.ModeChanged(mode=None)


def test_unknown_fields_on_a_known_event_are_ignored() -> None:
    event = _event('{"_event":"focus-changed","windowId":1,"workspace":"a","extra":{"x":1}}\n')
    assert event == parse.FocusChanged(window_id="1", workspace="a")


# -- torn and malformed lines -----------------------------------------------------------------


def test_a_torn_last_line_is_malformed_and_counted_by_reason() -> None:
    torn = b'{"_event":"focus-changed","windowId":18'
    assert parse.parse_event_line(torn) == parse.MalformedLine("torn")


def test_a_complete_line_that_is_not_json_is_invalid_json() -> None:
    assert parse.parse_event_line(b"not json at all\n") == parse.MalformedLine("invalid_json")


def test_an_unterminated_but_complete_line_still_parses() -> None:
    assert _event(b'{"_event":"mode-changed","mode":"main"}') == parse.ModeChanged("main")


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        (b"\xff\xfe\n", "bad_utf8"),
        (b"[1,2]\n", "not_object"),
        (b'"text"\n', "not_object"),
        (b'{"windowId":1}\n', "no_event"),
        (b'{"_event":""}\n', "no_event"),
        (b'{"_event":3}\n', "no_event"),
    ],
)
def test_other_malformed_lines(line: bytes, reason: str) -> None:
    assert parse.parse_event_line(line) == parse.MalformedLine(reason)


@pytest.mark.parametrize("blank", [b"", b"\n", b"  \r\n", ""])
def test_blank_lines_are_ignored(blank: bytes | str) -> None:
    assert parse.parse_event_line(blank) is None


def test_str_lines_parse_like_bytes() -> None:
    assert _event('{"_event":"mode-changed","mode":"main"}\n') == parse.ModeChanged("main")


# -- window snapshot ----------------------------------------------------------------------------


def test_window_snapshot_fixture() -> None:
    parsed = parse.parse_windows((FIXTURES / "windows.json").read_bytes())
    raw = json.loads((FIXTURES / "windows.json").read_text())
    assert parsed.skipped == 0
    assert len(parsed.windows) == len(raw) == 31
    ids = [int(w.window_id) for w in parsed.windows]
    assert ids == sorted(ids), "sorted by window id for a stable snapshot"
    first = next(w for w in parsed.windows if w.window_id == "109")
    assert first.app_bundle_id == "com.eltima.cmd1.pro.mas"
    assert first.app_name == "Commander One PRO"
    assert first.app_pid == 14082
    assert first.workspace == "1"
    assert first.title


def test_unknown_fields_and_missing_optional_fields_in_windows() -> None:
    data = json.dumps(
        [
            {"window-id": 5, "brand-new-key": [1], "workspace": "9"},
            {"window-id": "6", "window-title": None, "app-pid": "nope"},
        ]
    )
    parsed = parse.parse_windows(data)
    assert [w.window_id for w in parsed.windows] == ["5", "6"]
    assert parsed.windows[0] == parse.WindowInfo("5", "9", None, None, None, None)
    assert parsed.windows[1].app_pid is None
    assert parsed.windows[1].title is None


def test_the_no_bundle_id_placeholder_is_read_as_no_bundle_id() -> None:
    data = json.dumps(
        [
            {"window-id": 1, "app-bundle-id": parse.NO_BUNDLE_ID, "app-name": "Bundleless"},
            {"window-id": 2, "app-bundle-id": "", "app-name": "Empty"},
        ]
    )
    windows = parse.parse_windows(data).windows
    assert [(w.app_bundle_id, w.app_name) for w in windows] == [
        (None, "Bundleless"),
        (None, "Empty"),
    ]


def test_unusable_window_entries_are_skipped_and_counted() -> None:
    data = json.dumps([{"window-id": 1}, {"window-id": 1}, {"title": "x"}, 5, {"window-id": True}])
    parsed = parse.parse_windows(data)
    assert [w.window_id for w in parsed.windows] == ["1"]
    assert parsed.skipped == 4


def test_an_empty_window_list_is_valid() -> None:
    parsed = parse.parse_windows(b"[]")
    assert parsed.windows == ()
    assert parsed.skipped == 0


@pytest.mark.parametrize("bad", [b"", b"[{", b'{"window-id": 1}', b"null", "Error: no server"])
def test_a_snapshot_that_is_not_an_array_raises(bad: bytes | str) -> None:
    with pytest.raises(parse.SnapshotParseError):
        parse.parse_windows(bad)
    with pytest.raises(parse.SnapshotParseError):
        parse.parse_workspaces(bad)


# -- workspace snapshot -------------------------------------------------------------------------


def test_workspace_snapshot_fixture_lists_every_workspace() -> None:
    parsed = parse.parse_workspaces((FIXTURES / "workspaces.json").read_bytes())
    assert parsed.skipped == 0
    names = [w.name for w in parsed.workspaces]
    assert len(names) == 20
    assert len(set(names)) == 20
    assert all(w.monitor == "1" for w in parsed.workspaces)
    window_workspaces = {
        w.workspace for w in parse.parse_windows((FIXTURES / "windows.json").read_bytes()).windows
    }
    empty = [n for n in names if n not in window_workspaces]
    assert empty, "the fixture keeps persistent workspaces that hold no window"
    assert sum(w.focused for w in parsed.workspaces) <= 1


def test_workspace_entries_are_validated() -> None:
    data = json.dumps(
        [
            {"workspace": "a", "workspace-is-focused": True, "monitor-id": 2, "other": 1},
            {"workspace": 7},
            {"workspace": "a"},
            {"monitor-id": 1},
        ]
    )
    parsed = parse.parse_workspaces(data)
    assert parsed.workspaces == (
        parse.WorkspaceInfo("a", True, "2"),
        parse.WorkspaceInfo("7", False, None),
    )
    assert parsed.skipped == 2


# -- version ---------------------------------------------------------------------------------


def test_parse_version() -> None:
    text = (
        "aerospace CLI client version: 0.21.3-Beta d56e1637\n"
        "AeroSpace.app server version: 0.21.3-Beta d56e1637\n"
    )
    assert parse.parse_version(text) == "0.21.3-Beta"
    assert parse.parse_version(text.encode()) == "0.21.3-Beta"
    assert parse.parse_version(b"\xff nothing") is None


# -- records ---------------------------------------------------------------------------------


def test_window_snapshot_record_is_complete_and_schema_valid() -> None:
    windows = parse.parse_windows((FIXTURES / "windows.json").read_bytes()).windows
    record = parse.window_snapshot(windows, NOW)
    assert record.complete is True
    assert record.entity_kind is EntityKind.WINDOW
    assert (record.source, record.entity_id) == ("aerospace", "all")
    assert len(record.items) == 31
    assert record.items[0].entity_id == windows[0].window_id
    assert parse_record(record.model_dump(mode="json")) == record
    assert parse.window_snapshot(windows, NOW).dedup_key == record.dedup_key


def test_an_empty_window_snapshot_is_still_a_complete_record() -> None:
    record = parse.window_snapshot([], NOW)
    assert record.complete is True
    assert record.items == ()


def test_workspace_snapshot_record_is_complete_and_schema_valid() -> None:
    workspaces = parse.parse_workspaces((FIXTURES / "workspaces.json").read_bytes()).workspaces
    record = parse.workspace_snapshot(workspaces, NOW)
    assert record.complete is True
    assert record.entity_kind is EntityKind.WORKSPACE
    assert [i.entity_id for i in record.items] == [w.name for w in workspaces]
    assert parse_record(record.model_dump(mode="json")) == record


def test_focus_event_record() -> None:
    record = parse.focus_event(
        NOW, window_id="9", workspace="2", app_bundle_id="com.x", app_name="X", title="T"
    )
    assert record.entity_kind is EntityKind.FOCUS
    assert record.entity_id == parse.FOCUS_ENTITY_ID
    assert record.payload.window_id == "9"
    assert record.payload.workspace == "2"
    assert record.payload.app_name == "X"
    assert parse_record(record.model_dump(mode="json")) == record
