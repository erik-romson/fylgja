"""Ancestor chains of Claude sessions (WP1.8): the pure parsers and the collector's use of them.

The collector asks ``ps -A -o pid=,ppid=,comm=`` once in a poll that has a live session without a
chain, attaches each chain to the session record and keeps it until the process is gone.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from claude_support import T0, World

from fylgja.collectors.claude import adapter, parse
from fylgja.collectors.seams import ProcessResult
from fylgja.models import ProcessRef, SessionSnapshot

# --- the pure parsers ----------------------------------------------------------------------------

TABLE = """\
    1     0 /sbin/launchd
  200     1 /Applications/Some App.app/Contents/MacOS/Some App
  300   200 -zsh
  400   300 claude
  500     1 loop-a
  501   502 loop-b
  502   501 loop-c
garbage line
"""


def test_parse_ps_table_reads_pid_ppid_and_a_comm_with_spaces() -> None:
    table = parse.parse_ps_table(TABLE)
    assert table[200] == (1, "/Applications/Some App.app/Contents/MacOS/Some App")
    assert table[400] == (300, "claude")
    assert 0 not in table
    assert "garbage" not in str(table)


def test_the_chain_runs_from_the_pid_up_to_but_not_including_launchd() -> None:
    chain = parse.ancestor_chain(parse.parse_ps_table(TABLE), 400)
    assert [ref.pid for ref in chain] == [400, 300, 200]
    assert chain[0] == ProcessRef(pid=400, ppid=300, comm="claude")


def test_a_pid_the_table_does_not_list_has_no_chain() -> None:
    assert parse.ancestor_chain(parse.parse_ps_table(TABLE), 999) == ()


def test_a_loop_in_the_table_ends_the_chain() -> None:
    chain = parse.ancestor_chain(parse.parse_ps_table(TABLE), 501)
    assert [ref.pid for ref in chain] == [501, 502]


def test_a_chain_is_cut_at_the_maximum_length() -> None:
    table = {pid: (pid + 1, f"p{pid}") for pid in range(2, 2 + parse.MAX_CHAIN * 2)}
    assert len(parse.ancestor_chain(table, 2)) == parse.MAX_CHAIN


def test_session_payload_carries_the_chain_and_an_empty_chain_is_none() -> None:
    row = parse.RegistryRow(pid=1, session_id="s", cwd="/w")
    chain = (ProcessRef(pid=1, ppid=0, comm="x"),)
    with_chain = parse.session_payload(row, process_key=None, predecessor_key=None, ancestry=chain)
    assert with_chain.ancestry == chain
    without = parse.session_payload(row, process_key=None, predecessor_key=None, ancestry=())
    assert without.ancestry is None


# --- the adapter ----------------------------------------------------------------------------------


async def test_ps_process_table_runs_one_command_and_parses_it() -> None:
    class Runner:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        async def run(
            self,
            argv: object,
            *,
            timeout: float | None = None,  # noqa: ASYNC109 - mirrors the protocol
            stdin: bytes | None = None,
        ) -> ProcessResult:
            self.calls.append(tuple(argv))  # type: ignore[call-overload]
            return ProcessResult(tuple(argv), 0, TABLE.encode())  # type: ignore[call-overload]

    runner = Runner()
    table = await adapter.ps_process_table(runner, "/bin/ps", timeout=5)
    assert runner.calls == [("/bin/ps", "-A", "-o", "pid=,ppid=,comm=")]
    assert table[400] == (300, "claude")


@pytest.mark.parametrize(
    ("kind", "error"),
    [("ps_missing", FileNotFoundError()), ("ps_timeout", TimeoutError())],
)
async def test_ps_process_table_failures_are_classified(kind: str, error: Exception) -> None:
    class Failing:
        async def run(
            self,
            argv: object,
            *,
            timeout: float | None = None,  # noqa: ASYNC109 - mirrors the protocol
            stdin: bytes | None = None,
        ) -> ProcessResult:
            raise error

    with pytest.raises(adapter.PsFailure) as caught:
        await adapter.ps_process_table(Failing(), "/bin/ps", timeout=5)
    assert caught.value.kind == kind


# --- the collector --------------------------------------------------------------------------------


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path / "sessions")


def _sessions(world: World) -> dict[str, ProcessRef | None]:
    snapshots = [r for r in world.sink.records if isinstance(r, SessionSnapshot)]
    return {
        item.entity_id: (item.payload.ancestry[0] if item.payload.ancestry else None)
        for item in snapshots[-1].items
    }


async def test_every_live_session_carries_its_chain_from_one_ps_call(world: World) -> None:
    world.add(100, "s-100")
    world.add(101, "s-101")
    world.ps.table = {100: (50, "claude"), 101: (50, "claude"), 50: (10, "zsh"), 10: (1, "cmux")}
    await world.collector.poll()
    snapshot = next(r for r in world.sink.records if isinstance(r, SessionSnapshot))
    chains = {item.entity_id: item.payload.ancestry for item in snapshot.items}
    assert [ref.comm for ref in chains["claude:s-100"] or ()] == ["claude", "zsh", "cmux"]
    assert [ref.pid for ref in chains["claude:s-101"] or ()] == [101, 50, 10]
    assert world.ps.table_calls == [("/bin/ps", "-A", "-o", "pid=,ppid=,comm=")]


async def test_chains_are_kept_and_ps_is_not_asked_again_for_known_processes(world: World) -> None:
    world.add(100, "s-100")
    await world.collector.poll()
    await world.clock.advance(2)
    await world.collector.poll()
    await world.clock.advance(40)  # a heartbeat re-emits the snapshot
    assert await world.collector.poll()
    assert len(world.ps.table_calls) == 1
    assert _sessions(world)["claude:s-100"] is not None


async def test_a_new_session_triggers_one_more_call(world: World) -> None:
    world.add(100, "s-100")
    await world.collector.poll()
    world.add(101, "s-101")
    await world.clock.advance(2)
    await world.collector.poll()
    assert len(world.ps.table_calls) == 2
    assert all(ref is not None for ref in _sessions(world).values())


async def test_a_reused_pid_gets_a_new_chain(world: World) -> None:
    world.add(100, "s-100")
    world.ps.table = {100: (50, "claude"), 50: (1, "zsh")}
    await world.collector.poll()
    world.ps.starts[100] = T0 + 500  # the pid now belongs to another process
    world.add(100, "s-new", start=T0 + 500)
    world.ps.table = {100: (60, "claude"), 60: (1, "bash")}
    await world.clock.advance(2)
    await world.collector.poll()
    assert len(world.ps.table_calls) == 2
    snapshot = [r for r in world.sink.records if isinstance(r, SessionSnapshot)][-1]
    assert snapshot.items[0].payload.ancestry == (
        ProcessRef(pid=100, ppid=60, comm="claude"),
        ProcessRef(pid=60, ppid=1, comm="bash"),
    )


async def test_a_pid_missing_from_the_table_is_retried_at_the_next_poll(world: World) -> None:
    world.add(100, "s-100")
    world.ps.table = {}
    await world.collector.poll()
    assert _sessions(world)["claude:s-100"] is None
    world.ps.table = {100: (1, "claude")}
    await world.clock.advance(2)
    await world.collector.poll()
    assert _sessions(world)["claude:s-100"] is not None
    assert len(world.ps.table_calls) == 2


async def test_a_failing_process_table_costs_only_the_chains(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    world.add(100, "s-100")
    world.ps.table_error = FileNotFoundError()
    with caplog.at_level(logging.INFO):
        assert await world.collector.poll()
        await world.clock.advance(2)
        await world.collector.poll()
        assert _sessions(world) == {"claude:s-100": None}, "the session is still listed"
        health = await world.collector.doctor()
        assert health.state.value == "ok", "a missing chain is not a degraded collector"
        failures = [r for r in caplog.records if "process table failed" in r.getMessage()]
        assert len(failures) == 1, "the failure is logged once, not every poll"
        world.ps.table_error = None
        await world.clock.advance(2)
        await world.collector.poll()
    assert _sessions(world)["claude:s-100"] is not None
    assert any("process table recovered" in r.getMessage() for r in caplog.records)
