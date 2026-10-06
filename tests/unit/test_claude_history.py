"""Claude's ``history.jsonl`` as a "worked on" signal (WP5.2): parsing tolerant of unknown keys,
incremental reads, coalescing and what never leaves the reader (the prompt text)."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from claude_support import World

from fylgja.collectors.claude.collector import ClaudeSettings
from fylgja.collectors.claude.history import (
    HistoryEntry,
    HistoryReader,
    parse_entry,
    worked_on_event,
)
from fylgja.models import ContextItemEvent
from fylgja.testing.fakes import FakeClock, FakeFS, RecordingSink

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
PATH = Path("/home/u/.claude/history.jsonl")
SECRET_PROMPT = "please deploy with token-canary-4711"


def ms(minutes: float) -> int:
    return int((NOW + timedelta(minutes=minutes)).timestamp() * 1000)


def line(project: str | None, minutes: float, **extra: Any) -> str:
    entry: dict[str, Any] = {"display": SECRET_PROMPT, "pastedContents": {}, **extra}
    if project is not None:
        entry["project"] = project
    entry["timestamp"] = ms(minutes)
    return json.dumps(entry) + "\n"


class Rig:
    def __init__(self, **options: Any) -> None:
        self.fs = FakeFS()
        self.sink = RecordingSink()
        self.clock = FakeClock(NOW)
        self.reader = HistoryReader(
            PATH,
            self.fs,
            self.sink,
            self.clock,
            logging.LoggerAdapter(logging.getLogger("test.history"), {}),
            **options,
        )

    def write(self, text: str) -> None:
        old = self.fs.read_bytes(PATH) if self.fs.exists(PATH) else b""
        self.fs.add_file(PATH, old + text.encode())

    def markers(self) -> list[tuple[str, datetime]]:
        found = []
        for record in self.sink.records:
            assert isinstance(record, ContextItemEvent)
            found.append((str(record.payload.data["project"]), record.observed_at))
        return found


# --- parse_entry -------------------------------------------------------------------------------


def test_an_entry_is_a_project_and_a_time_whatever_else_it_holds() -> None:
    entry = parse_entry(
        {
            "display": SECRET_PROMPT,
            "pastedContents": {"1": {"content": "x"}},
            "timestamp": 1_761_302_885_343,
            "project": "/Users/u/code/app/",
            "sessionId": "abc",
            "someKeyFromTheFuture": [1, 2],
        }
    )
    assert entry == HistoryEntry(
        "/Users/u/code/app", datetime.fromtimestamp(1_761_302_885.343, UTC)
    )


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        (1_761_302_885_343, datetime(2025, 10, 24, 10, 48, 5, 343000, tzinfo=UTC)),
        (1_761_302_885, datetime(2025, 10, 24, 10, 48, 5, tzinfo=UTC)),
        (1_761_302_885.5, datetime(2025, 10, 24, 10, 48, 5, 500000, tzinfo=UTC)),
        ("2025-10-24T10:48:05Z", datetime(2025, 10, 24, 10, 48, 5, tzinfo=UTC)),
        ("2025-10-24T12:48:05+02:00", datetime(2025, 10, 24, 10, 48, 5, tzinfo=UTC)),
    ],
)
def test_the_time_is_epoch_seconds_or_milliseconds_or_iso(
    timestamp: object, expected: datetime
) -> None:
    entry = parse_entry({"project": "/p", "timestamp": timestamp})
    assert entry is not None
    assert entry.at == expected


@pytest.mark.parametrize(
    "entry",
    [
        {},
        {"project": "/p"},
        {"timestamp": 1_761_302_885_343},
        {"project": "relative/dir", "timestamp": 1_761_302_885_343},
        {"project": "", "timestamp": 1_761_302_885_343},
        {"project": 5, "timestamp": 1_761_302_885_343},
        {"project": "/p\x00q", "timestamp": 1_761_302_885_343},
        {"project": "/" + "a" * 2000, "timestamp": 1_761_302_885_343},
        {"project": "/p", "timestamp": True},
        {"project": "/p", "timestamp": -5},
        {"project": "/p", "timestamp": float("inf")},
        {"project": "/p", "timestamp": 10**30},
        {"project": "/p", "timestamp": "yesterday"},
        {"project": "/p", "timestamp": "2025-10-24T10:48:05"},  # no offset: unknown zone
        {"project": "/p", "timestamp": None},
    ],
)
def test_an_entry_without_a_usable_project_and_time_is_skipped(entry: dict[str, Any]) -> None:
    assert parse_entry(entry) is None


def test_the_root_directory_is_a_project_of_its_own() -> None:
    entry = parse_entry({"project": "/", "timestamp": 1_761_302_885_343})
    assert entry is not None
    assert entry.project == "/"


def test_a_marker_is_a_context_item_with_the_project_and_nothing_else() -> None:
    event = worked_on_event(HistoryEntry("/code/app", NOW))
    assert event.source == "claude"
    assert (event.payload.context_kind, event.payload.data) == (
        "worked_on",
        {"project": "/code/app"},
    )
    assert event.observed_at == NOW
    assert SECRET_PROMPT not in event.model_dump_json()


# --- the reader --------------------------------------------------------------------------------


def test_the_first_poll_reads_the_file_and_marks_recent_prompts_only() -> None:
    rig = Rig(backfill_seconds=3600, coalesce_seconds=0)
    rig.write(line("/code/old", -120) + line("/code/a", -30) + line("/code/b", -1))
    assert rig.reader.poll() == 2
    assert [p for p, _ in rig.markers()] == ["/code/a", "/code/b"]
    assert rig.markers()[0][1] == NOW - timedelta(minutes=30)  # the prompt's own time
    assert rig.reader.prompts == 3
    assert rig.reader.emitted == 2


def test_later_polls_read_only_what_was_appended() -> None:
    rig = Rig(coalesce_seconds=0)
    rig.write(line("/code/a", -10))
    assert rig.reader.poll() == 1
    assert rig.reader.poll() == 0  # nothing new
    rig.write(line("/code/b", -5))
    assert rig.reader.poll() == 1
    assert [p for p, _ in rig.markers()] == ["/code/a", "/code/b"]
    assert rig.reader.prompts == 2


def test_a_torn_last_line_is_completed_by_the_next_poll() -> None:
    rig = Rig(coalesce_seconds=0)
    whole = line("/code/a", -10)
    rig.write(whole[:30])
    assert rig.reader.poll() == 0
    rig.write(whole[30:])
    assert rig.reader.poll() == 1


def test_a_project_is_marked_once_per_coalescing_window() -> None:
    rig = Rig(coalesce_seconds=60)
    rig.write(
        line("/code/a", -10)
        + line("/code/a", -9.5)  # 30 s later: the same marker
        + line("/code/b", -9.5)  # another project: its own
        + line("/code/a", -8)  # a minute and a half on: a new marker
    )
    assert rig.reader.poll() == 3
    assert [p for p, _ in rig.markers()] == ["/code/a", "/code/b", "/code/a"]
    assert rig.reader.prompts == 4


def test_ignored_directories_and_junk_lines_leave_no_marker() -> None:
    rig = Rig(ignore_cwd=("CodexBar/ClaudeProbe", "/state/summarizer"), coalesce_seconds=0)
    rig.write(
        line("/x/CodexBar/ClaudeProbe", -5)
        + line("/state/summarizer/sub", -5)
        + "not json\n"
        + "[1, 2]\n"
        + "\n"
        + line(None, -5)
        + line("/code/a", -5)
    )
    assert rig.reader.poll() == 1
    assert [p for p, _ in rig.markers()] == ["/code/a"]
    assert rig.reader.skipped == 1  # the entry with no project; the others were not entries


def test_a_prompt_from_the_future_is_stamped_no_later_than_now() -> None:
    rig = Rig(coalesce_seconds=0)
    rig.write(line("/code/a", 3))
    assert rig.reader.poll() == 1
    assert rig.markers() == [("/code/a", NOW)]


def test_the_prompt_text_never_reaches_a_record_or_a_log(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    rig = Rig(coalesce_seconds=0)
    rig.write(line("/code/a", -5))
    rig.reader.poll()
    dumps = " ".join(record.model_dump_json() for record in rig.sink.records)
    assert SECRET_PROMPT not in dumps
    assert SECRET_PROMPT not in caplog.text
    assert "worked-on" in caplog.text  # the poll said how many, and nothing else


def test_a_missing_file_is_not_an_error_and_a_file_that_appears_is_read() -> None:
    rig = Rig()
    assert rig.reader.poll() == 0
    rig.write(line("/code/a", -5))
    assert rig.reader.poll() == 1


def test_a_replaced_file_is_read_again_and_a_marker_is_not_lost_to_old_coalescing() -> None:
    rig = Rig(coalesce_seconds=3600)
    rig.write(line("/code/a", -50) + line("/code/b", -40))
    assert rig.reader.poll() == 2
    rig.fs.replace_file(PATH, line("/code/a", -50).encode())  # rotated: shorter, new inode
    assert rig.reader.poll() == 1  # read from the start; the same marker (its dedup key matches)
    assert [p for p, _ in rig.markers()][-1] == "/code/a"
    assert rig.markers()[-1] == rig.markers()[0]


def test_a_large_backlog_is_read_over_several_reads_of_one_poll() -> None:
    rig = Rig(coalesce_seconds=0, max_read=400)
    rig.write("".join(line(f"/code/p{i}", -20 + i * 0.1) for i in range(30)))
    assert rig.reader.poll() == 30


# --- inside the collector ----------------------------------------------------------------------


async def test_the_collector_reads_the_history_next_to_the_registry(tmp_path: Path) -> None:
    world = World(tmp_path / "claude" / "sessions")
    prompt = world.clock.now() - timedelta(minutes=5)
    (tmp_path / "claude" / "history.jsonl").write_text(
        json.dumps(
            {"display": SECRET_PROMPT, "project": "/code/a", "timestamp": prompt.timestamp() * 1000}
        )
        + "\n",
        encoding="utf-8",
    )
    task = asyncio.create_task(world.collector.run())
    try:
        for _ in range(3):
            await world.clock.advance(5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    marks = [r for r in world.sink.records if isinstance(r, ContextItemEvent)]
    assert [(m.payload.context_kind, m.payload.data, m.observed_at) for m in marks] == [
        ("worked_on", {"project": "/code/a"}, prompt)
    ]


async def test_the_collector_reads_no_history_when_switched_off(tmp_path: Path) -> None:
    world = World(tmp_path / "claude" / "sessions", settings=ClaudeSettings(history_enabled=False))
    (tmp_path / "claude" / "history.jsonl").write_text(line("/code/a", -5), encoding="utf-8")
    task = asyncio.create_task(world.collector.run())
    try:
        await world.clock.advance(5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not [r for r in world.sink.records if isinstance(r, ContextItemEvent)]


def test_the_history_settings_are_part_of_the_plugin_configuration() -> None:
    settings = ClaudeSettings(
        history_file=Path("/elsewhere/history.jsonl"),
        history_enabled=False,
        history_backfill_hours=2,
        history_coalesce_seconds=10,
    )
    assert settings.history_file == Path("/elsewhere/history.jsonl")
    assert ClaudeSettings().history_enabled is True
    with pytest.raises(ValueError, match="history_poll_seconds"):
        ClaudeSettings(history_poll_seconds=0)
