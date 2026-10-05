"""Pure parsing of Claude registry rows, liveness, filters and record builders (WP1.7)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from claude_support import FIXTURE_ROWS, T0, proc_start_text, ps_text, row_data

from fylgja.collectors.claude import parse
from fylgja.models import EntityKind


def _fixture(name: str) -> parse.RegistryRow:
    pid = int(name.removesuffix(".json"))
    row = parse.parse_row((FIXTURE_ROWS / name).read_bytes(), file_pid=pid)
    assert isinstance(row, parse.RegistryRow), row
    return row


def _parse(data: object, pid: int = 10) -> parse.RegistryRow | parse.RowRejected:
    return parse.parse_row(json.dumps(data).encode(), file_pid=pid)


def _row(**fields: object) -> parse.RegistryRow:
    row = parse.parse_row(json.dumps(row_data(10, "s-1", **fields)).encode(), file_pid=10)  # type: ignore[arg-type]
    assert isinstance(row, parse.RegistryRow)
    return row


# --------------------------------------------------------------------------------------------
# File names
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "pid"),
    [("14936.json", 14936), ("1.json", 1), ("007.json", 7)],
)
def test_registry_names_that_match_the_glob(name: str, pid: int) -> None:
    assert parse.registry_pid(name) == pid


@pytest.mark.parametrize(
    "name",
    [
        "14936.abcdef0123456789.key",
        "14936.key",
        "14936.json.bak",
        "14936.json\n",
        "x14936.json",
        ".14936.tmp",
        "json",
        ".json",
        "abc.json",
        "1 2.json",
        "",
    ],
)
def test_names_outside_the_glob_are_not_rows(name: str) -> None:
    assert parse.registry_pid(name) is None


# --------------------------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------------------------


def test_the_idle_row_of_version_2_1_284() -> None:
    row = _fixture("4101.json")
    assert (row.pid, row.version, row.status, row.entrypoint) == (4101, "2.1.284", "idle", "cli")
    assert row.session_id == "00000000-0000-4000-8000-000000004101"
    assert row.peer_protocol == 1
    assert row.kind == "interactive"
    assert row.bridge_session_id is None
    assert row.proc_start == datetime(2026, 10, 4, 18, 31, 51)  # noqa: DTZ001 - naive UTC text
    assert row.started_at_ms == 1791138746439
    assert row.messaging_socket_path == "/tmp/cc-socks/4101.sock"


def test_a_waiting_row_carries_its_reason() -> None:
    row = _fixture("4102.json")
    assert (row.status, row.waiting_for, row.version) == ("waiting", "input needed", "2.1.289")
    assert parse.map_state(row.status) == "waiting"
    assert parse.activity_of(row).waiting_reason == "input needed"


def test_an_sdk_row_without_proc_start_is_tagged_sdk_py() -> None:
    row = _fixture("4103.json")
    assert row.entrypoint == "sdk-py"
    assert row.proc_start is None
    assert row.status == "busy"
    assert parse.session_payload(row, process_key=None, predecessor_key=None).entrypoint == "sdk-py"


def test_a_bridge_session_id_becomes_a_remote_control_link() -> None:
    row = _fixture("4104.json")
    assert row.bridge_session_id == "session_SYNTHETIC0000000000"
    payload = parse.session_payload(row, process_key="4104-1", predecessor_key=None)
    assert payload.remote_url == "https://claude.ai/code/session_SYNTHETIC0000000000"


def test_a_missing_bridge_session_id_gives_no_link() -> None:
    row = _fixture("4105.json")
    assert row.bridge_session_id is None
    assert parse.session_payload(row, process_key=None, predecessor_key=None).remote_url is None


@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURE_ROWS.glob("*.json")))
def test_every_fixture_row_parses_and_builds_a_session(name: str) -> None:
    row = _fixture(name)
    payload = parse.session_payload(row, process_key="k", predecessor_key="claude:old")
    assert payload.kind == "claude"
    assert payload.native_id == row.session_id
    assert payload.pid == row.pid
    assert payload.version == row.version
    assert payload.peer_protocol == 1
    assert payload.predecessor_key == "claude:old"
    assert payload.started_at is not None


def test_unknown_fields_are_tolerated() -> None:
    data = row_data(10, "s-1", futureField={"a": [1, 2]}, another="x")
    row = _parse(data)
    assert isinstance(row, parse.RegistryRow)
    assert row.session_id == "s-1"


def test_optional_fields_of_the_wrong_type_are_dropped() -> None:
    row = _parse(
        row_data(
            10,
            "s-1",
            version=7,
            entrypoint=["cli"],
            name=None,
            peerProtocol="1",
            startedAt=True,
            status=3,
            procStart="not a date",
            bridgeSessionId=5,
        )
    )
    assert isinstance(row, parse.RegistryRow)
    assert row.version is None
    assert row.entrypoint is None
    assert row.name is None
    assert row.peer_protocol is None
    assert row.started_at_ms is None
    assert row.status is None
    assert row.proc_start is None
    assert row.bridge_session_id is None


def test_a_float_start_is_accepted_as_milliseconds() -> None:
    row = _parse(row_data(10, "s-1", startedAt=1791138746439.0))
    assert isinstance(row, parse.RegistryRow)
    assert row.started_at_ms == 1791138746439


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"", "not_json"),
        (b'{"pid": 10', "not_json"),
        (b"\xff\xfe", "not_json"),
        (b"[1, 2]", "not_an_object"),
        (b'"text"', "not_an_object"),
        (b"null", "not_an_object"),
        (b"x" * ((1 << 20) + 1), "too_large"),
        (b"[" * 100000, "not_json"),
    ],
)
def test_bad_bytes_are_rejected_with_a_reason(data: bytes, reason: str) -> None:
    result = parse.parse_row(data, file_pid=10)
    assert result == parse.RowRejected(reason)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"pid": "10"}, "bad_pid"),
        ({"pid": True}, "bad_pid"),
        ({"pid": None}, "bad_pid"),
        ({"pid": 11}, "pid_mismatch"),
        ({"sessionId": ""}, "bad_session_id"),
        ({"sessionId": 5}, "bad_session_id"),
        ({"cwd": ""}, "bad_cwd"),
        ({"cwd": None}, "bad_cwd"),
    ],
)
def test_a_row_must_have_pid_session_and_cwd(change: dict[str, object], reason: str) -> None:
    assert _parse({**row_data(10, "s-1"), **change}) == parse.RowRejected(reason)


def test_missing_required_keys_are_rejected() -> None:
    for key, reason in (("pid", "bad_pid"), ("sessionId", "bad_session_id"), ("cwd", "bad_cwd")):
        data = row_data(10, "s-1")
        del data[key]
        assert _parse(data) == parse.RowRejected(reason)


# --------------------------------------------------------------------------------------------
# Times, ps output and liveness
# --------------------------------------------------------------------------------------------


def test_proc_start_and_ps_text_parse_with_padded_days() -> None:
    assert parse.parse_proc_start("Mon Oct  5 11:38:48 2026") == datetime(2026, 10, 5, 11, 38, 48)  # noqa: DTZ001
    assert parse.parse_proc_start("Mon Oct 15 11:38:48 2026  ") == datetime(
        2026, 10, 15, 11, 38, 48
    )  # noqa: DTZ001
    assert parse.parse_proc_start("") is None
    assert parse.parse_proc_start("yesterday") is None


def test_ps_output_is_parsed_per_pid_and_garbage_is_skipped() -> None:
    text = (
        "  101 Mon Oct  5 11:38:48 2026\n"
        "garbage line\n"
        "x12 Mon Oct  5 11:38:48 2026\n"
        "  102 not a date\n"
        "\n"
        "20345 Sun Oct  4 18:31:51 2026   \n"
    )
    assert parse.parse_ps_starts(text) == {
        101: datetime(2026, 10, 5, 11, 38, 48),  # noqa: DTZ001
        20345: datetime(2026, 10, 4, 18, 31, 51),  # noqa: DTZ001
    }
    assert parse.parse_ps_starts("") == {}


def test_proc_start_is_utc_and_ps_text_is_local_for_the_same_instant() -> None:
    row = _row(start=T0)
    assert row.proc_start is not None
    assert row.proc_start.replace(tzinfo=UTC).timestamp() == T0
    assert parse.parse_proc_start(ps_text(T0)) is not None
    assert proc_start_text(T0).endswith("2026")


def test_a_row_is_live_when_the_process_start_equals_proc_start() -> None:
    assert parse.check_liveness(_row(start=T0), T0) is parse.Liveness.LIVE
    assert parse.check_liveness(_row(start=T0), T0 + 1) is parse.Liveness.LIVE
    assert parse.check_liveness(_row(start=T0), T0 - 1) is parse.Liveness.LIVE


def test_a_row_whose_pid_was_reused_is_not_live() -> None:
    assert parse.check_liveness(_row(start=T0), T0 + 2) is parse.Liveness.REUSED
    assert parse.check_liveness(_row(start=T0), T0 + 86400) is parse.Liveness.REUSED


def test_a_row_without_a_process_is_dead() -> None:
    assert parse.check_liveness(_row(start=T0), None) is parse.Liveness.DEAD


def test_without_proc_start_a_later_process_means_a_reused_pid() -> None:
    row = _row(start=None)  # startedAt is T0 + 20
    assert parse.check_liveness(row, T0) is parse.Liveness.LIVE
    assert parse.check_liveness(row, T0 + 20 + 4) is parse.Liveness.LIVE
    assert parse.check_liveness(row, T0 + 20 + 6) is parse.Liveness.REUSED


def test_without_proc_start_or_started_at_a_process_is_live() -> None:
    row = parse.RegistryRow(pid=1, session_id="s", cwd="/w")
    assert parse.check_liveness(row, T0) is parse.Liveness.LIVE


def test_process_and_session_keys() -> None:
    assert parse.process_key(42, 1790000000.9) == "42-1790000000"
    assert parse.session_key("abc") == "claude:abc"


# --------------------------------------------------------------------------------------------
# ignore_cwd
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cwd", "patterns", "expected"),
    [
        ("/Users/u/Library/CodexBar/ClaudeProbe", ["CodexBar/ClaudeProbe"], True),
        ("/Users/u/Library/CodexBar/ClaudeProbe/sub", ["CodexBar/ClaudeProbe"], True),
        ("/CodexBar/ClaudeProbe", ["CodexBar/ClaudeProbe"], True),
        ("/Users/u/CodexBar", ["CodexBar/ClaudeProbe"], False),
        ("/Users/u/MyCodexBar/ClaudeProbe", ["CodexBar/ClaudeProbe"], False),
        ("/work/project", ["CodexBar/ClaudeProbe"], False),
        ("/state/summarizer", ["/state/summarizer"], True),
        ("/state/summarizer/deep/er", ["/state/summarizer"], True),
        ("/state/summarizer-other", ["/state/summarizer"], False),
        ("/other/state/summarizer", ["/state/summarizer"], False),
        ("/work/project", [], False),
        ("/work/project", [""], False),
        ("/work/project", ["/"], True),
        ("/a/b", ["x", "/a/b"], True),
    ],
)
def test_cwd_ignore_patterns(cwd: str, patterns: list[str], expected: bool) -> None:
    assert parse.cwd_ignored(cwd, patterns) is expected


# --------------------------------------------------------------------------------------------
# State, links and records
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "state"),
    [
        ("idle", "idle"),
        ("busy", "working"),
        ("waiting", "waiting"),
        ("sleeping", None),
        (None, None),
    ],
)
def test_state_comes_from_the_registry_status(status: str | None, state: str | None) -> None:
    assert parse.map_state(status) == state


@pytest.mark.parametrize("bridge", [None, "", "has space", "a/b", "x" * 201, "../up", "a?b"])
def test_unusable_bridge_ids_give_no_link(bridge: str | None) -> None:
    assert parse.remote_url(bridge) is None


def test_a_reason_is_kept_only_while_waiting() -> None:
    assert parse.activity_of(_row(status="busy", waitingFor="x")).waiting_reason is None
    assert parse.activity_of(_row(status="waiting", waitingFor="x")).waiting_reason == "x"
    assert parse.activity_of(_row(status="waiting")).waiting_reason is None


def test_snapshots_are_complete_and_scoped() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    row = _row()
    payload = parse.session_payload(row, process_key="10-1", predecessor_key=None)
    sessions = parse.session_snapshot([(parse.session_key("s-1"), payload)], now)
    activity = parse.activity_snapshot([(parse.session_key("s-1"), parse.activity_of(row))], now)
    for snapshot, kind in ((sessions, EntityKind.SESSION), (activity, EntityKind.SESSION_ACTIVITY)):
        assert snapshot.source == "claude"
        assert snapshot.entity_kind is kind
        assert snapshot.complete is True
        assert snapshot.entity_id == "all"
        assert [item.entity_id for item in snapshot.items] == ["claude:s-1"]
    assert parse.session_snapshot([], now).items == ()
