"""The sources of ``usage`` (WP5.4, §4.7): Claude's statusline ``rate_limits``, the CodexBar CLI's
usage and cost output, which providers each covers, a missing CodexBar, and change-interval
storage.

CodexBar's output is read from sanitized fixtures under ``tests/fixtures/usage/codexbar``
(``docs/revalidation/2026-10-06-usage.md``); nothing here runs CodexBar or needs a network.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from inbox_support import make_envelope

from fylgja import hook, paths
from fylgja.collectors.claude.normalize import parse_envelope
from fylgja.collectors.claude.statusline import (
    RATE_LIMIT_INTERVAL,
    StatuslineReader,
    parse_statusline,
    statusline_records,
)
from fylgja.collectors.project import usage
from fylgja.collectors.project.usage import (
    Failure,
    ProviderCost,
    ProviderUsage,
    UsageEngine,
    UsageParseError,
    UsageSettings,
    Window,
    cost_argv,
    parse_cost,
    parse_usage,
    usage_argv,
    window_name,
)
from fylgja.config import ConfigError
from fylgja.core.live_view import LiveView
from fylgja.core.writer import Writer
from fylgja.models import UsagePayload, UsageSnapshot
from fylgja.store import db
from fylgja.testing.fakes import FakeClock, FakeProcessRunner

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "usage"
CODEXBAR = FIXTURES / "codexbar"
STATUSLINE = FIXTURES / "statusline"
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
COMMAND = "/bin/codexbar"


def fixture(name: str) -> bytes:
    return (CODEXBAR / name).read_bytes()


def statusline_payload(name: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((STATUSLINE / name).read_text(encoding="utf-8"))
    return payload


# -- Claude: the statusline's rate_limits ------------------------------------------------------


def _reading(name: str) -> Any:
    payload = statusline_payload(name)
    envelope = parse_envelope(hook.encode_envelope(make_envelope("StatusLine", payload)))
    reading = parse_statusline(envelope)
    assert reading is not None
    return reading, envelope


def _usage_snapshot(name: str) -> UsageSnapshot:
    reading, envelope = _reading(name)
    records = statusline_records(reading, envelope, observed_at=NOW, source="claude", kind="claude")
    snapshots = [r for r in records if isinstance(r, UsageSnapshot)]
    assert len(snapshots) == 1
    return snapshots[0]


def test_statusline_rate_limits_map_to_provider_windows() -> None:
    snapshot = _usage_snapshot("payload-pro.json")
    assert snapshot.complete is False  # rate limits are the account's: other sessions report them
    rows = {
        item.payload.window: item.payload
        for item in snapshot.items
        if isinstance(item.payload, UsagePayload)
    }
    assert set(rows) == {"five_hour", "seven_day"}
    five = rows["five_hour"]
    assert (five.provider, five.used_pct, five.usage_source) == ("claude", 23.5, "statusline")
    assert five.resets_at == datetime.fromtimestamp(1791300000, UTC)
    assert five.poll_interval_s == RATE_LIMIT_INTERVAL
    assert rows["seven_day"].used_pct == 41.2
    assert rows["seven_day"].resets_at == datetime.fromtimestamp(1791800000, UTC)


def test_the_spend_limit_window_is_a_window_too() -> None:
    snapshot = _usage_snapshot("payload-spend-limit.json")
    assert [(i.payload.window, i.payload.used_pct) for i in snapshot.items] == [
        ("spend_limit", 62.8)
    ]


def test_an_api_key_session_has_no_rate_limits_and_so_no_usage_snapshot() -> None:
    reading, envelope = _reading("payload-api-key.json")
    records = statusline_records(reading, envelope, observed_at=NOW, source="claude", kind="claude")
    assert not [r for r in records if isinstance(r, UsageSnapshot)]
    assert (reading.context_pct, reading.cost_usd) == (12.5, 0.5)


def test_the_reader_remembers_the_newest_cost_and_context_of_each_session() -> None:
    clock = FakeClock(NOW)
    directory = paths.statusline_dir()
    directory.mkdir(parents=True, mode=0o700)
    reader = StatuslineReader(
        directory,
        _Sink(),
        clock,
        logging.LoggerAdapter(logging.getLogger("test.usage"), {}),
    )
    session = "00000000-0000-4000-8000-000000000001"
    assert reader.readings(session) == (None, None)

    def write(payload: dict[str, Any], seconds: int) -> None:
        path = directory / f"{session}.json"
        path.write_bytes(hook.encode_envelope(make_envelope("StatusLine", payload)))
        stamp = int((NOW - timedelta(seconds=seconds)).timestamp() * 1_000_000_000)
        os.utime(path, ns=(stamp, stamp))

    write(statusline_payload("payload-pro.json"), 20)
    reader.poll()
    assert reader.readings(session) == (8.0, 0.01234)
    # a later snapshot that has a cost but no context percentage keeps the earlier percentage
    later = {"session_id": session, "cost": {"total_cost_usd": 0.5}}
    write(later, 10)
    reader.poll()
    assert reader.readings(session) == (8.0, 0.5)
    (directory / f"{session}.json").unlink()
    reader.poll()
    assert reader.readings(session) == (None, None)  # the snapshot is gone, and so is its reading


class _Sink:
    def emit(self, record: Any) -> None:
        return None


# -- CodexBar: usage ----------------------------------------------------------------------------


def test_the_codex_windows_are_named_by_their_length() -> None:
    parsed = parse_usage(fixture("usage-codex.json"), "codex")
    assert isinstance(parsed, ProviderUsage)
    assert parsed.windows == (
        Window("five_hour", 0, datetime(2026, 10, 6, 16, 28, 31, tzinfo=UTC)),
        Window("seven_day", 3, datetime(2026, 10, 12, 11, 37, 8, tzinfo=UTC)),
    )


def test_a_third_window_and_unknown_lengths_are_named_by_minutes() -> None:
    parsed = parse_usage(fixture("usage-codex-three-windows.json"), "codex")
    assert isinstance(parsed, ProviderUsage)
    assert [(w.name, w.used_pct) for w in parsed.windows] == [
        ("five_hour", 41.5),
        ("seven_day", 12),
        ("43200m", 7),
    ]


def test_odd_windows_keep_what_is_usable() -> None:
    parsed = parse_usage(fixture("usage-codex-odd-windows.json"), "codex")
    assert isinstance(parsed, ProviderUsage)
    # no length: the slot name; a fractional length: the slot name; a window with neither a
    # number nor a time is dropped; an unparsable reset time is no time
    assert [(w.name, w.used_pct, w.resets_at is None) for w in parsed.windows] == [
        ("primary", 10, False),
        ("secondary", 20, True),
    ]


def test_two_windows_of_one_length_keep_their_slot_names() -> None:
    parsed = parse_usage(fixture("usage-codex-same-length.json"), "codex")
    assert isinstance(parsed, ProviderUsage)
    assert [w.name for w in parsed.windows] == ["primary", "secondary"]
    assert parsed.windows[1].resets_at == datetime(2026, 10, 6, 17, 28, 31, tzinfo=UTC)


@pytest.mark.parametrize(
    ("minutes", "slot", "expected"),
    [
        (300, "primary", "five_hour"),
        (300.0, "primary", "five_hour"),
        (10080, "secondary", "seven_day"),
        (60, "tertiary", "60m"),
        (None, "primary", "primary"),
        (0, "primary", "primary"),
        (-1, "secondary", "secondary"),
        (True, "secondary", "secondary"),
        ("300", "secondary", "secondary"),
        (float("nan"), "primary", "primary"),
    ],
)
def test_window_names(minutes: object, slot: str, expected: str) -> None:
    assert window_name(slot, minutes) == expected


@pytest.mark.parametrize(
    ("name", "provider", "kind"),
    [
        ("usage-copilot-no-strategy.json", "copilot", "codexbar_no_strategy"),
        ("usage-claude-no-credentials.json", "claude", "codexbar_not_authenticated"),
        ("usage-unsupported.json", "copilot", "codexbar_unsupported"),
        ("usage-timeout-error.json", "codex", "codexbar_timeout"),
        ("usage-other-error.json", "codex", "codexbar_provider_error"),
        ("usage-error-without-message.json", "codex", "codexbar_provider_error"),
        ("usage-codex-no-windows.json", "codex", "codexbar_no_data"),
        ("usage-other-provider.json", "codex", "codexbar_no_data"),
        ("usage-empty-array.json", "codex", "codexbar_no_data"),
    ],
)
def test_a_provider_that_cannot_answer_is_a_failure_with_a_short_code(
    name: str, provider: str, kind: str
) -> None:
    assert parse_usage(fixture(name), provider) == Failure(provider, kind)


def test_a_single_object_is_read_like_a_one_element_array() -> None:
    assert isinstance(parse_usage(fixture("usage-object.json"), "codex"), ProviderUsage)


@pytest.mark.parametrize("name", ["usage-garbled.txt", "usage-scalar.json"])
def test_output_that_is_not_json_objects_is_a_parse_error(name: str) -> None:
    with pytest.raises(UsageParseError) as caught:
        parse_usage(fixture(name), "codex")
    assert caught.value.kind == "codexbar_parse"
    with pytest.raises(UsageParseError):
        parse_cost(fixture(name), "codex")


def test_no_error_text_survives_parsing() -> None:
    for name in ("usage-copilot-no-strategy.json", "usage-claude-no-credentials.json"):
        parsed = parse_usage(fixture(name), json.loads(fixture(name))[0]["provider"])
        assert isinstance(parsed, Failure)
        assert " " not in parsed.kind
        assert "credentials" not in repr(parsed)


# -- CodexBar: cost -----------------------------------------------------------------------------


def test_the_cost_is_the_last_thirty_days() -> None:
    codex = parse_cost(fixture("cost-codex.json"), "codex")
    claude = parse_cost(fixture("cost-claude.json"), "claude")
    assert isinstance(codex, ProviderCost)
    assert isinstance(claude, ProviderCost)
    assert codex.usd == pytest.approx(24.276115)
    assert claude.usd == pytest.approx(3039.778643)
    assert (codex.period, claude.period) == ("30d", "30d")


def test_one_output_covers_several_providers_and_each_is_picked_by_name() -> None:
    combined = fixture("cost-all.json")
    assert isinstance(parse_cost(combined, "codex"), ProviderCost)
    assert isinstance(parse_cost(combined, "claude"), ProviderCost)
    assert parse_cost(combined, "cursor") == Failure("cursor", "codexbar_not_authenticated")
    assert parse_cost(combined, "gemini") == Failure("gemini", "codexbar_no_data")


@pytest.mark.parametrize("name", ["cost-no-total.json", "cost-negative.json"])
def test_a_cost_without_a_usable_total_is_no_data(name: str) -> None:
    assert parse_cost(fixture(name), "codex") == Failure("codex", "codexbar_no_data")


# -- which providers each source covers ---------------------------------------------------------


def test_the_default_providers_split_between_the_statusline_and_codexbar() -> None:
    settings = UsageSettings()
    # Claude's quota comes from the statusline, so CodexBar is not asked for it ...
    assert "claude" not in settings.usage_providers
    assert settings.usage_providers == ["codex", "copilot"]
    # ... but its spend comes from CodexBar's local scan, which needs no login
    assert settings.cost_providers == ["codex", "claude"]


# -- the engine ---------------------------------------------------------------------------------


def _runner() -> FakeProcessRunner:
    runner = FakeProcessRunner()
    runner.script(COMMAND, "usage", "--provider", "codex", stdout=fixture("usage-codex.json"))
    runner.script(
        COMMAND,
        "usage",
        "--provider",
        "copilot",
        stdout=fixture("usage-copilot-no-strategy.json"),
        returncode=1,
    )
    runner.script(COMMAND, "cost", "--provider", "codex", stdout=fixture("cost-codex.json"))
    runner.script(COMMAND, "cost", "--provider", "claude", stdout=fixture("cost-claude.json"))
    return runner


def _engine(
    runner: FakeProcessRunner, clock: FakeClock | None = None, **settings: Any
) -> tuple[UsageEngine, FakeClock]:
    clock = FakeClock(NOW) if clock is None else clock
    return UsageEngine(
        UsageSettings(codexbar_command=COMMAND, **settings), clock=clock, runner=runner
    ), clock


def _items(snapshot: UsageSnapshot) -> dict[str, UsagePayload]:
    return {item.entity_id: item.payload for item in snapshot.items}


def test_the_argv_asks_for_one_provider_as_json() -> None:
    assert usage_argv("cb", "codex") == (
        "cb",
        "usage",
        "--provider",
        "codex",
        "--json",
        "--no-color",
    )
    assert cost_argv("cb", "claude") == (
        "cb",
        "cost",
        "--provider",
        "claude",
        "--json",
        "--no-color",
    )


async def test_the_first_poll_emits_one_complete_snapshot_of_everything() -> None:
    runner = _runner()
    engine, _ = _engine(runner)
    records = await engine.poll()
    assert len(records) == 1
    snapshot = records[0]
    assert isinstance(snapshot, UsageSnapshot)
    assert snapshot.complete is True  # what is missing from it is closed
    assert (snapshot.source, snapshot.observed_at) == ("project", NOW)
    items = _items(snapshot)
    assert set(items) == {
        "codex:five_hour",
        "codex:seven_day",
        "copilot:usage",
        "codex:cost",
        "claude:cost",
    }
    five = items["codex:five_hour"]
    assert (five.provider, five.window, five.used_pct, five.usage_source) == (
        "codex",
        "five_hour",
        0,
        "codexbar",
    )
    assert five.poll_interval_s == 300
    assert items["codex:seven_day"].used_pct == 3
    cost = items["claude:cost"]
    assert (cost.cost_period, cost.window, cost.poll_interval_s) == ("30d", None, 900)
    assert cost.cost_usd == pytest.approx(3039.778643)
    failed = items["copilot:usage"]
    assert (failed.error_kind, failed.used_pct) == ("codexbar_no_strategy", None)
    assert runner.calls == [
        usage_argv(COMMAND, "codex"),
        usage_argv(COMMAND, "copilot"),
        cost_argv(COMMAND, "codex"),
        cost_argv(COMMAND, "claude"),
    ]


async def test_what_is_not_due_is_not_asked_again() -> None:
    runner = _runner()
    engine, clock = _engine(runner)
    await engine.poll()
    calls = len(runner.calls)
    await clock.advance(60)
    assert await engine.poll() == []
    assert len(runner.calls) == calls
    await clock.advance(241)  # the usage timer (300 s) is due, the cost timer (900 s) is not
    records = await engine.poll()
    assert len(runner.calls) == calls + 2
    assert set(_items(records[0])) >= {"codex:cost", "claude:cost", "codex:five_hour"}
    await clock.advance(600)  # now the cost timer is due too
    await engine.poll()
    assert len(runner.calls) == calls + 2 + 2 + 2


async def test_codexbar_missing_is_a_failure_for_every_provider_and_never_raises() -> None:
    engine, _ = _engine(FakeProcessRunner())  # nothing scripted: the command does not exist
    records = await engine.poll()
    snapshot = records[0]
    assert isinstance(snapshot, UsageSnapshot)
    items = _items(snapshot)
    assert {i.error_kind for i in items.values()} == {"codexbar_missing"}
    assert set(items) == {
        "codex:usage",
        "copilot:usage",
        "codex:cost",
        "claude:cost",
    }
    assert all(i.used_pct is None and i.cost_usd is None for i in items.values())
    # a failed cost attempt names its period, which is how /now tells the two kinds apart
    assert items["codex:cost"].cost_period == "30d"
    assert items["codex:usage"].cost_period is None
    assert engine.failures == {
        "usage:codex": "codexbar_missing",
        "usage:copilot": "codexbar_missing",
        "cost:codex": "codexbar_missing",
        "cost:claude": "codexbar_missing",
    }
    assert "CodexBar unavailable (codexbar_missing)" in engine.summary()


@pytest.mark.parametrize(
    ("script", "kind"),
    [
        ({"timeout": True}, "codexbar_timeout"),
        ({"error": PermissionError("no")}, "codexbar_failed"),
        ({"stdout": b"codexbar: panic\n", "returncode": 1}, "codexbar_failed"),
        ({"stdout": b"codexbar: panic\n", "returncode": 0}, "codexbar_parse"),
        ({"stdout": b"", "returncode": 1}, "codexbar_failed"),
    ],
)
async def test_each_way_a_call_can_fail_has_its_own_code(script: dict[str, Any], kind: str) -> None:
    runner = FakeProcessRunner()
    runner.script(COMMAND, **script)
    engine, _ = _engine(runner, usage_providers=["codex"], cost_providers=[])
    await engine.poll()
    assert engine.failures == {"usage:codex": kind}


async def test_a_nonzero_exit_with_valid_json_is_still_read() -> None:
    runner = FakeProcessRunner()
    runner.script(COMMAND, stdout=fixture("cost-all.json"), returncode=1)  # cursor failed
    engine, _ = _engine(runner, usage_providers=[], cost_providers=["codex"])
    records = await engine.poll()
    assert _items(records[0])["codex:cost"].cost_usd == pytest.approx(24.276115)
    assert engine.failures == {}


async def test_a_provider_that_recovers_stops_being_a_failure() -> None:
    runner = FakeProcessRunner()
    runner.script(COMMAND, "usage", stdout=b"[]", times=1)
    runner.script(COMMAND, "usage", stdout=fixture("usage-codex.json"))
    engine, clock = _engine(runner, usage_providers=["codex"], cost_providers=[])
    await engine.poll()
    assert engine.failures == {"usage:codex": "codexbar_no_data"}
    await clock.advance(301)
    await engine.poll()
    assert engine.failures == {}
    assert engine.summary() == "usage: 2 quota windows, 0 cost providers"


async def test_disabled_asks_nothing_and_says_so() -> None:
    for settings in (
        {"usage_enabled": False},
        {"usage_providers": [], "cost_providers": []},
    ):
        runner = _runner()
        engine, _ = _engine(runner, **settings)
        assert engine.enabled is False
        assert await engine.poll() == []
        assert runner.calls == []
        assert engine.summary() == "usage: disabled"


def test_before_the_first_poll_there_is_no_data_and_the_summary_says_why() -> None:
    engine, _ = _engine(_runner())
    assert engine.polled is False
    assert engine.summary() == "usage: no data yet (CodexBar not asked yet)"


def test_provider_names_are_normalised() -> None:
    engine, _ = _engine(
        _runner(), usage_providers=["Codex", " codex ", "", "COPILOT"], cost_providers=["claude"]
    )
    assert engine._usage_providers == ["codex", "copilot"]


async def test_only_the_requested_providers_are_asked() -> None:
    runner = _runner()
    engine, _ = _engine(runner, usage_providers=["codex"], cost_providers=[])
    records = await engine.poll()
    assert runner.calls == [usage_argv(COMMAND, "codex")]
    assert set(_items(records[0])) == {"codex:five_hour", "codex:seven_day"}


def test_the_binary_comes_from_the_paths_table_or_the_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert usage.resolve_binary() == "/opt/homebrew/bin/codexbar"

    def broken() -> Any:
        raise ConfigError("config.toml", "paths", "broken")

    monkeypatch.setattr(usage, "load_config", broken)
    assert usage.resolve_binary() == "/opt/homebrew/bin/codexbar"


# -- change-interval storage (A20) --------------------------------------------------------------


@pytest.fixture
async def stored() -> AsyncIterator[tuple[Writer, LiveView, Path]]:
    view = LiveView()
    writer = Writer(paths.database_file(), clock=FakeClock(NOW), on_commit=view.apply)
    await writer.start()
    try:
        yield writer, view, paths.database_file()
    finally:
        await writer.stop()


def _count(path: Path, where: str = "1") -> int:
    connection = db.connect_reader(path)
    try:
        return int(
            connection.execute(f"SELECT count(*) FROM usage_snapshots WHERE {where}").fetchone()[0]
        )
    finally:
        connection.close()


async def _store(writer: Writer, engine: UsageEngine, clock: FakeClock) -> None:
    for record in await engine.poll():
        writer.submit(record)
    await writer.flush()


async def test_rows_are_stored_as_change_intervals(
    stored: tuple[Writer, LiveView, Path],
) -> None:
    writer, view, path = stored
    runner = _runner()
    engine, clock = _engine(runner)
    await _store(writer, engine, clock)
    assert _count(path) == 4  # two windows and two costs; the failed provider has no row
    assert _count(path, "valid_to IS NULL") == 4
    assert set(view.usage) == {
        "project:codex:five_hour",
        "project:codex:seven_day",
        "project:codex:",
        "project:claude:",
    }
    assert view.usage["project:codex:five_hour"].source == "codexbar"
    assert view.usage["project:claude:"].cost_period == "30d"

    await clock.advance(301)  # the same readings again: nothing is written
    await _store(writer, engine, clock)
    assert _count(path) == 4

    runner2 = FakeProcessRunner()  # the codex five-hour reading moved
    changed = json.loads(fixture("usage-codex.json"))
    changed[0]["usage"]["primary"]["usedPercent"] = 17
    runner2.script(COMMAND, "usage", "--provider", "codex", stdout=json.dumps(changed))
    runner2.script(COMMAND, "usage", "--provider", "copilot", stdout=b"[]")
    runner2.script(COMMAND, "cost", "--provider", "codex", stdout=fixture("cost-codex.json"))
    runner2.script(COMMAND, "cost", "--provider", "claude", stdout=fixture("cost-claude.json"))
    engine._runner = runner2
    await clock.advance(301)
    await _store(writer, engine, clock)
    assert _count(path) == 5  # one more interval: the old one is closed, the new one open
    assert _count(path, "valid_to IS NOT NULL") == 1
    assert view.usage["project:codex:five_hour"].used_pct == 17
    assert view.usage["project:codex:seven_day"].used_pct == 3


async def test_a_failed_attempt_keeps_the_last_rows_open(
    stored: tuple[Writer, LiveView, Path],
) -> None:
    writer, view, path = stored
    engine, clock = _engine(_runner())
    await _store(writer, engine, clock)
    before = _count(path, "valid_to IS NULL")
    engine._runner = FakeProcessRunner()  # CodexBar is gone
    await clock.advance(1000)
    await _store(writer, engine, clock)
    assert _count(path, "valid_to IS NULL") == before  # nothing was closed, nothing was added
    assert _count(path) == before
    assert "project:codex:five_hour" in view.usage


async def test_a_row_a_complete_snapshot_no_longer_lists_is_closed(
    stored: tuple[Writer, LiveView, Path],
) -> None:
    writer, view, path = stored
    engine, clock = _engine(_runner())
    await _store(writer, engine, clock)
    engine2, _ = _engine(_runner(), clock, usage_providers=["codex"], cost_providers=[])
    await clock.advance(10)
    await _store(writer, engine2, clock)
    assert set(view.usage) == {"project:codex:five_hour", "project:codex:seven_day"}
    assert _count(path, "valid_to IS NULL") == 2
