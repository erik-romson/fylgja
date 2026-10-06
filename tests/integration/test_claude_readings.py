"""The Claude collector's readings of a live session (WP5.4, §4.7): the token sums of its
transcript and the cost and context percentage of its newest statusline snapshot, laid onto the
activity it emits. Real files in a temporary directory, a fake ``ps`` and a fake clock."""

from __future__ import annotations

import json
import os
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from claude_support import World
from inbox_support import make_envelope

from fylgja import hook, paths
from fylgja.collectors.claude.transcript import project_slug
from fylgja.models import SessionActivity, SessionActivitySnapshot

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
SESSION = "00000000-0000-4000-8000-000000000001"
CWD = "/work/project"


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path / "sessions")


def _activity(world: World, key: str) -> SessionActivity:
    snapshots = [r for r in world.sink.records if isinstance(r, SessionActivitySnapshot)]
    return {item.entity_id: item.payload for item in snapshots[-1].items}[key]


def _transcript(world: World, name: str, session: str = SESSION) -> Path:
    directory = world.registry.parent / "projects" / project_slug(CWD)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{session}.jsonl"
    shutil.copy(FIXTURES / "usage" / "transcripts" / name, target)
    return target


def _statusline(world: World, payload: dict[str, Any], session: str = SESSION) -> None:
    directory = paths.statusline_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{session}.json"
    path.write_bytes(hook.encode_envelope(make_envelope("StatusLine", payload)))
    stamp = int((world.clock.now() - timedelta(seconds=1)).timestamp() * 1_000_000_000)
    os.utime(path, ns=(stamp, stamp))


async def test_the_activity_carries_the_token_sums_of_the_transcript(world: World) -> None:
    world.add(100, SESSION, cwd=CWD)
    _transcript(world, "usage-basic.jsonl")
    assert await world.collector.poll()
    golden = json.loads((FIXTURES / "lidskjalv" / "costs" / "usage-basic.json").read_text())
    tokens = _activity(world, f"claude:{SESSION}").tokens
    assert tokens is not None
    assert tokens.model_dump() == golden


async def test_the_activity_carries_the_cost_and_context_of_the_statusline(world: World) -> None:
    world.add(100, SESSION, cwd=CWD)
    _statusline(
        world,
        {
            "session_id": SESSION,
            "cost": {"total_cost_usd": 0.5},
            "context_window": {"used_percentage": 33.0},
        },
    )
    world.collector.statusline.poll()
    assert await world.collector.poll()
    activity = _activity(world, f"claude:{SESSION}")
    assert (activity.cost_usd, activity.context_pct) == (0.5, 33.0)
    assert activity.tokens is None  # no transcript yet: no data, not zero


async def test_a_new_reading_is_emitted_at_once_and_a_session_without_any_has_none(
    world: World,
) -> None:
    world.add(100, SESSION, cwd=CWD)
    world.add(101, "00000000-0000-4000-8000-000000000002", cwd=CWD)
    transcript = _transcript(world, "usage-many-turns.jsonl")
    assert await world.collector.poll()
    first = _activity(world, f"claude:{SESSION}").tokens
    assert first is not None
    assert first.input == 820
    other = _activity(world, "claude:00000000-0000-4000-8000-000000000002")
    assert (other.tokens, other.cost_usd, other.context_pct) == (None, None, None)

    line = {"type": "assistant", "message": {"usage": {"input_tokens": 5, "output_tokens": 1}}}
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")
    await world.clock.advance(2)
    assert await world.collector.poll()  # the activity changed, so it is emitted again
    grown = _activity(world, f"claude:{SESSION}").tokens
    assert grown is not None
    assert (grown.input, grown.output) == (825, 1641)

    await world.clock.advance(2)
    assert not await world.collector.poll()  # nothing new: nothing emitted
