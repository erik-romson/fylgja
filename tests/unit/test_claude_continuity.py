"""Session continuity (A11): ``process_key`` is stable and ``/clear`` links the predecessor."""

from __future__ import annotations

from pathlib import Path

import pytest
from claude_support import T0, World, row_data, write_row

from fylgja.collectors.claude.registry import Continuity
from fylgja.models import SessionPayload, SessionSnapshot


def _last(world: World) -> dict[str, SessionPayload]:
    snapshots = [r for r in world.sink.records if isinstance(r, SessionSnapshot)]
    return {item.entity_id: item.payload for item in snapshots[-1].items}


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path / "sessions")


# --------------------------------------------------------------------------------------------
# The tracker
# --------------------------------------------------------------------------------------------


def test_a_first_session_has_no_predecessor() -> None:
    assert Continuity().observe("p-1", "s-1") is None


def test_a_rotation_names_the_old_session_as_predecessor() -> None:
    continuity = Continuity()
    continuity.observe("p-1", "s-1")
    assert continuity.observe("p-1", "s-2") == "claude:s-1"
    assert continuity.observe("p-1", "s-2") == "claude:s-1", "the link is kept while it lives"
    assert continuity.observe("p-1", "s-3") == "claude:s-2"


def test_other_processes_do_not_link() -> None:
    continuity = Continuity()
    continuity.observe("p-1", "s-1")
    assert continuity.observe("p-2", "s-2") is None


def test_going_back_to_an_earlier_id_adds_no_loop() -> None:
    continuity = Continuity()
    continuity.observe("p-1", "s-1")
    assert continuity.observe("p-1", "s-2") == "claude:s-1"
    assert continuity.observe("p-1", "s-1") is None, "s-1 already was in this process"
    assert continuity.observe("p-1", "s-2") == "claude:s-1"


def test_retain_forgets_processes_and_sessions_that_are_gone() -> None:
    continuity = Continuity()
    continuity.observe("p-1", "s-1")
    continuity.observe("p-1", "s-2")
    continuity.observe("p-2", "s-9")
    continuity.retain({"p-1"}, {"s-2"})
    assert continuity.observe("p-1", "s-2") == "claude:s-1"
    assert continuity.observe("p-2", "s-10") is None, "p-2 was forgotten, so s-10 is a first"
    continuity.retain(set(), set())
    assert continuity.observe("p-1", "s-2") is None


# --------------------------------------------------------------------------------------------
# Through the collector
# --------------------------------------------------------------------------------------------


async def test_clear_rotation_sets_predecessor_and_keeps_process_key(world: World) -> None:
    world.add(100, "s-before", status="busy")
    await world.collector.poll()
    before = _last(world)["claude:s-before"]
    assert before.predecessor_key is None
    assert before.process_key == f"100-{int(T0)}"

    # `/clear`: the same process (pid and start time), a new session id, written in place.
    write_row(world.registry, row_data(100, "s-after", status="idle", start=T0))
    await world.clock.advance(2)
    assert await world.collector.poll()
    live = _last(world)
    assert set(live) == {"claude:s-after"}, "the old session is closed by the complete snapshot"
    after = live["claude:s-after"]
    assert after.predecessor_key == "claude:s-before"
    assert after.process_key == before.process_key


async def test_the_predecessor_survives_later_polls_and_a_second_rotation(world: World) -> None:
    world.add(100, "s-1")
    await world.collector.poll()
    write_row(world.registry, row_data(100, "s-2"))
    await world.clock.advance(2)
    await world.collector.poll()
    write_row(world.registry, row_data(100, "s-2", status="busy"))
    await world.clock.advance(2)
    await world.collector.poll()
    assert _last(world)["claude:s-2"].predecessor_key == "claude:s-1"

    write_row(world.registry, row_data(100, "s-3"))
    await world.clock.advance(2)
    await world.collector.poll()
    third = _last(world)["claude:s-3"]
    assert third.predecessor_key == "claude:s-2"
    assert third.process_key == f"100-{int(T0)}"


async def test_a_reused_pid_is_a_new_process_without_a_predecessor(world: World) -> None:
    world.add(100, "s-old")
    await world.collector.poll()
    old_key = _last(world)["claude:s-old"].process_key

    # The old process exited; the pid went to a new process that writes a new row.
    world.add(100, "s-new", start=T0 + 7200)
    await world.clock.advance(2)
    await world.collector.poll()
    new = _last(world)["claude:s-new"]
    assert new.process_key == f"100-{int(T0 + 7200)}"
    assert new.process_key != old_key
    assert new.predecessor_key is None


async def test_two_processes_keep_separate_chains(world: World) -> None:
    world.add(100, "a-1")
    world.add(101, "b-1", start=T0 + 5)
    await world.collector.poll()
    write_row(world.registry, row_data(100, "a-2"))
    await world.clock.advance(2)
    await world.collector.poll()
    live = _last(world)
    assert live["claude:a-2"].predecessor_key == "claude:a-1"
    assert live["claude:b-1"].predecessor_key is None
    assert live["claude:a-2"].process_key != live["claude:b-1"].process_key


async def test_a_row_without_proc_start_has_the_same_process_key(world: World) -> None:
    world.add(100, "s-1", start=None)
    world.add(101, "s-2", start=T0)
    await world.collector.poll()
    live = _last(world)
    assert live["claude:s-1"].process_key == f"100-{int(T0)}"
    assert live["claude:s-2"].process_key == f"101-{int(T0)}"
