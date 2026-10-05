"""The statusline reader (WP2.2, A4, §4.7): mtime triggering, partial or invalid files ignored and
the fields mapped to session and usage records."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from inbox_support import SESSION, make_envelope

from fylgja import hook, paths
from fylgja.collectors.claude.normalize import parse_envelope
from fylgja.collectors.claude.statusline import (
    MAX_AGE,
    StatuslineReader,
    parse_statusline,
)
from fylgja.models import EntityKind, HookEvent, UsageSnapshot
from fylgja.testing.fakes import FakeClock, RecordingSink

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
RESET_5H = 1_791_300_000
RESET_7D = 1_791_800_000

STATUSLINE: dict[str, Any] = {
    "session_id": SESSION,
    "cwd": "/work/project",
    "model": {"id": "m", "display_name": "M"},
    "cost": {"total_cost_usd": 1.2345, "total_duration_ms": 5000},
    "context_window": {"used_percentage": 42.5, "context_window_size": 200000},
    "rate_limits": {
        "five_hour": {"used_percentage": 23.5, "resets_at": RESET_5H},
        "seven_day": {"used_percentage": 41.2, "resets_at": RESET_7D},
    },
    "pr": {"number": 17, "url": "https://example.test/pr/17"},
}


class World:
    def __init__(self) -> None:
        self.clock = FakeClock(NOW)
        self.sink = RecordingSink()
        self.directory = paths.statusline_dir()
        self.directory.mkdir(parents=True, mode=0o700)
        self.reader = StatuslineReader(
            self.directory,
            self.sink,
            self.clock,
            logging.LoggerAdapter(logging.getLogger("test.statusline"), {}),
        )

    def write(
        self,
        payload: dict[str, Any] | str = STATUSLINE,
        *,
        name: str = f"{SESSION}.json",
        mtime: datetime = NOW - timedelta(seconds=5),
        environ: dict[str, str] | None = None,
    ) -> Path:
        envelope = make_envelope(
            "StatusLine", payload, environ={"CLAUDE_PID": "4101"} if environ is None else environ
        )
        path = self.directory / name
        path.write_bytes(hook.encode_envelope(envelope))
        self.touch(path, mtime)
        return path

    @staticmethod
    def touch(path: Path, mtime: datetime) -> None:
        stamp = int(mtime.timestamp() * 1_000_000) * 1000
        os.utime(path, ns=(stamp, stamp))

    def events(self) -> list[HookEvent]:
        return [r for r in self.sink.records if isinstance(r, HookEvent)]

    def usage(self) -> list[UsageSnapshot]:
        return [r for r in self.sink.records if isinstance(r, UsageSnapshot)]


@pytest.fixture
def world() -> World:
    return World()


def test_fields_are_mapped_to_a_session_event_and_a_usage_snapshot(world: World) -> None:
    world.write()
    assert world.reader.poll() == 1
    (event,) = world.events()
    assert event.entity_kind is EntityKind.HOOK_EVENT
    assert event.entity_id == f"claude:{SESSION}"
    assert event.observed_at == NOW - timedelta(seconds=5)
    payload = event.payload
    assert payload.event == "StatusLine"
    assert payload.session_id == SESSION
    assert (payload.context_pct, payload.cost_usd) == (42.5, 1.2345)
    assert (payload.pr_number, payload.pr_url) == (17, "https://example.test/pr/17")
    assert payload.cwd == "/work/project"
    assert payload.pid == 4101
    (usage,) = world.usage()
    assert usage.complete is False
    assert usage.source == "claude"
    rows = {item.payload.window: item.payload for item in usage.items}
    assert set(rows) == {"five_hour", "seven_day"}
    five = rows["five_hour"]
    assert (five.provider, five.used_pct, five.usage_source) == ("claude", 23.5, "statusline")
    assert five.resets_at == datetime.fromtimestamp(RESET_5H, UTC)
    assert rows["seven_day"].used_pct == 41.2


def test_a_snapshot_is_read_again_only_when_its_mtime_changes(world: World) -> None:
    path = world.write()
    assert world.reader.poll() == 1
    assert world.reader.poll() == 0
    assert world.reader.poll() == 0
    assert len(world.events()) == 1

    changed = {**STATUSLINE, "cost": {"total_cost_usd": 2.0}}
    world.write(changed, mtime=NOW - timedelta(seconds=1))
    assert world.reader.poll() == 1
    assert [e.payload.cost_usd for e in world.events()] == [1.2345, 2.0]
    assert len({e.dedup_key for e in world.events()}) == 2

    # The same mtime again: not read, even though the content differs.
    path.write_bytes(path.read_bytes().replace(b"2.0", b"3.0"))
    world.touch(path, NOW - timedelta(seconds=1))
    assert world.reader.poll() == 0
    assert world.reader.emitted == 2


def test_a_deleted_snapshot_is_forgotten_and_a_new_one_with_the_same_mtime_is_read(
    world: World,
) -> None:
    path = world.write()
    world.reader.poll()
    path.unlink()
    assert world.reader.poll() == 0
    world.write()
    assert world.reader.poll() == 1


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b'{"schema":"fylgja.hook/1","id":"6f1e2d3c-4b5a-4c6d-8e7f-0a1b2c3d4e5f","rece',
        b"not json at all\n",
        b"[]",
        b'{"schema":"other/1"}',
    ],
)
def test_partial_or_invalid_files_are_ignored_and_tried_again(world: World, content: bytes) -> None:
    path = world.directory / f"{SESSION}.json"
    path.write_bytes(content)
    world.touch(path, NOW - timedelta(seconds=3))
    assert world.reader.poll() == 0
    assert world.sink.records == []
    assert world.reader.ignored == 1
    assert world.reader.failure is None
    # The writer finishes: the same mtime is not "seen", so the whole file is read.
    world.write(mtime=NOW - timedelta(seconds=3))
    assert world.reader.poll() == 1


def test_a_payload_without_any_reading_emits_nothing(world: World) -> None:
    world.write({"session_id": SESSION, "model": {"id": "m"}})
    assert world.reader.poll() == 0
    assert world.sink.records == []
    assert world.reader.poll() == 0
    assert world.reader.ignored == 1  # looked at once, not at every poll


def test_a_payload_without_a_session_id_uses_the_environment_or_is_ignored(
    world: World,
) -> None:
    no_session = {k: v for k, v in STATUSLINE.items() if k != "session_id"}
    world.write(no_session, name="orphan.json", environ={})
    assert world.reader.poll() == 0
    world.write(no_session, name="from-env.json", environ={"CLAUDE_CODE_SESSION_ID": "sess-9"})
    assert world.reader.poll() == 1
    assert world.events()[0].payload.session_id == "sess-9"


def test_only_regular_snapshot_files_with_valid_names_are_read(world: World) -> None:
    world.write(name="not a name.json")
    world.write(name=".tmp-abc.json")
    world.write(name="ok.txt")
    elsewhere = world.write(name="real.json")
    world.reader.poll()
    world.sink.clear()
    link = world.directory / "linked.json"
    link.symlink_to(elsewhere)
    (world.directory / "dir.json").mkdir()
    assert world.reader.poll() == 0
    assert world.sink.records == []


def test_an_oversize_file_and_an_old_snapshot_are_skipped(world: World) -> None:
    big = world.write(name="big.json")
    big.write_bytes(b" " * (9 * 1024 * 1024))
    world.touch(big, NOW - timedelta(seconds=1))
    world.write(name="old.json", mtime=NOW - timedelta(seconds=MAX_AGE + 10))
    assert world.reader.poll() == 0
    assert world.sink.records == []


def test_a_missing_directory_is_normal_and_an_unreadable_one_is_a_failure(
    world: World, tmp_path: Path
) -> None:
    missing = StatuslineReader(
        tmp_path / "nope",
        world.sink,
        world.clock,
        logging.LoggerAdapter(logging.getLogger("test.statusline"), {}),
    )
    assert missing.poll() == 0
    assert missing.failure is None

    blocked = tmp_path / "file"
    blocked.write_text("x")
    broken = StatuslineReader(
        blocked, world.sink, world.clock, logging.LoggerAdapter(logging.getLogger("t"), {})
    )
    assert broken.poll() == 0
    assert broken.failure is not None
    blocked.unlink()
    blocked.mkdir()
    assert broken.poll() == 0
    assert broken.failure is None


def _reading(payload: dict[str, Any]) -> Any:
    return parse_statusline(parse_envelope(json.dumps(make_envelope("StatusLine", payload))))


def test_numbers_and_times_are_parsed_defensively() -> None:
    reading = _reading(
        {
            "session_id": SESSION,
            "cost": {"total_cost_usd": True},
            "context_window": {"used_percentage": "50"},
            "pr": {"number": True, "url": ""},
            "rate_limits": {
                "five_hour": {"used_percentage": 10, "resets_at": "2026-10-05T17:00:00+00:00"},
                "seven_day": {"used_percentage": 20, "resets_at": RESET_7D * 1000},
                "naive": {"used_percentage": 1, "resets_at": "2026-10-05T17:00:00"},
                "bad window name!": {"used_percentage": 5},
                "nothing": {"other": 1},
                "text": "x",
                "nan": {"used_percentage": float("nan")},
            },
        }
    )
    assert reading.context_pct is None
    assert reading.cost_usd is None
    assert (reading.pr_number, reading.pr_url) == (None, None)
    limits = {limit.window: limit for limit in reading.rate_limits}
    assert set(limits) == {"five_hour", "seven_day", "naive"}
    assert limits["five_hour"].resets_at == datetime(2026, 10, 5, 17, tzinfo=UTC)
    assert limits["seven_day"].resets_at == datetime.fromtimestamp(RESET_7D, UTC)
    assert limits["naive"].resets_at is None
    assert limits["naive"].used_pct == 1


def test_a_reading_with_only_a_context_percentage_has_no_usage_snapshot(world: World) -> None:
    world.write({"session_id": SESSION, "context_window": {"used_percentage": 7}})
    assert world.reader.poll() == 1
    assert world.usage() == []
    assert world.events()[0].payload.context_pct == 7


def test_the_pull_request_may_be_named_pull_request(world: World) -> None:
    world.write({"session_id": SESSION, "pull_request": {"number": 3, "url": "https://x.test/3"}})
    world.reader.poll()
    assert (world.events()[0].payload.pr_number, world.events()[0].payload.pr_url) == (
        3,
        "https://x.test/3",
    )


def test_the_snapshot_time_never_lies_in_the_future(world: World) -> None:
    world.write(mtime=NOW + timedelta(hours=1))
    world.reader.poll()
    assert world.events()[0].observed_at == NOW
