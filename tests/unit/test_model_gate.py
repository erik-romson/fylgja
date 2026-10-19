"""The shared model gate (PLAN-final-plan.md §4.4): one cap, the error kinds, the guards."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from summary_support import FakeRunner, wait_until

from fylgja.core.summaries import (
    CLAUDE_ARGS,
    MODEL,
    ClaudeRunner,
    ModelGate,
    ModelOutcome,
    RunOutput,
)
from fylgja.testing.fakes import FakeClock


def gate_in(tmp_path: Path, runner: FakeRunner, **kwargs: object) -> ModelGate:
    return ModelGate(
        kwargs.pop("max_concurrent", 2),  # type: ignore[arg-type]
        clock=kwargs.pop("clock", FakeClock()),  # type: ignore[arg-type]
        runner=runner,
        cwd=lambda: tmp_path / "summarizer",
        environ={"PATH": "/usr/bin", "SECRET_TOKEN": "x"},
        **kwargs,  # type: ignore[arg-type]
    )


async def call(gate: ModelGate, prompt: str = "p") -> ModelOutcome:
    async with gate.slot():
        return await gate.run(prompt)


async def test_three_callers_never_exceed_two_slots(tmp_path: Path) -> None:
    runner = FakeRunner(default="hello")
    gate = gate_in(tmp_path, runner, max_concurrent=2)
    runner.hold()
    tasks = [asyncio.create_task(call(gate, f"p{n}")) for n in range(3)]
    await wait_until(lambda: runner.in_flight == 2)
    await asyncio.sleep(0.05)
    assert (gate.running, runner.in_flight, len(runner.calls)) == (2, 2, 2)
    runner.release()
    outcomes = await asyncio.gather(*tasks)
    assert [o.stdout for o in outcomes] == ["hello"] * 3
    assert (gate.peak_running, runner.peak, gate.running, gate.calls) == (2, 2, 0, 3)


@pytest.mark.parametrize(
    ("script", "kind"),
    [
        (TimeoutError(), "timeout"),
        (FileNotFoundError("claude"), "not_installed"),
        (OSError("boom"), "spawn_failed"),
        (ValueError("odd"), "runner_error"),
        (RunOutput(1, "partial"), "nonzero_exit"),
        (RunOutput(0, "  \n "), "empty_output"),
    ],
)
async def test_failures_map_to_error_kinds(
    tmp_path: Path, script: BaseException | RunOutput, kind: str
) -> None:
    gate = gate_in(tmp_path, FakeRunner([script]))
    assert await call(gate) == ModelOutcome(stdout=None, error_kind=kind)


async def test_a_call_that_outlives_the_timeout_on_the_daemon_clock_times_out(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    runner = FakeRunner()
    runner.hold()
    gate = gate_in(tmp_path, runner, clock=clock, timeout=30.0)
    task = asyncio.create_task(call(gate))
    await wait_until(lambda: runner.in_flight == 1 and clock.pending == 1)
    await clock.advance(30)
    assert await task == ModelOutcome(error_kind="timeout")
    assert (runner.in_flight, gate.running) == (0, 0)


async def test_an_unusable_cwd_is_reported_and_not_counted(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file")
    runner = FakeRunner()
    gate = ModelGate(1, clock=FakeClock(), runner=runner, cwd=lambda: blocker / "inside")
    assert await call(gate) == ModelOutcome(error_kind="cwd_unavailable")
    assert (gate.calls, runner.calls) == (0, [])


async def test_the_call_gets_the_guard_environment_and_the_summarizer_cwd(tmp_path: Path) -> None:
    runner = FakeRunner()
    gate = gate_in(tmp_path, runner)
    await call(gate)
    (seen,) = runner.calls
    assert seen.cwd == tmp_path / "summarizer"
    assert seen.cwd.is_dir()
    assert seen.env["FYLGJA_SUMMARY"] == "1"
    assert seen.env["LIDSKJALV_TRACK_SUMMARY"] == "1"
    assert seen.env["PATH"] == "/usr/bin"
    assert "SECRET_TOKEN" not in seen.env


def test_a_gate_without_a_runner_calls_haiku() -> None:
    gate = ModelGate(1, clock=FakeClock())
    assert isinstance(gate._runner, ClaudeRunner)  # noqa: SLF001
    assert MODEL == "haiku"
    assert CLAUDE_ARGS == ("-p", "--model", "haiku", "--strict-mcp-config")
    assert ClaudeRunner(("claude",)).argv() == [
        "claude",
        "-p",
        "--model",
        "haiku",
        "--strict-mcp-config",
    ]


async def test_an_unexpected_runner_failure_logs_its_type_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    gate = gate_in(tmp_path, FakeRunner([ValueError("prompt text")]))
    assert await call(gate) == ModelOutcome(error_kind="runner_error")
    (record,) = caplog.records
    assert record.getMessage() == "the model runner failed: ValueError"
    assert record.exc_info is None
