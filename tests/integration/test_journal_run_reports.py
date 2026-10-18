"""A finished run's report sections feed the journal entry (J8 section 2, J10, J11, J21, F4).

The run is a synthetic session in ``/w/repo`` (so ``flow`` is test data; the journal reads only
states). Its sections arrive in ``SessionActivity.report``, as the norn collector emits them.
"""

from __future__ import annotations

import re
from typing import Any

from api_support import session, session_snapshot
from summary_support import Call
from test_journal_daemon import (
    MAIN_ID,
    REPO,
    World,
    reply,
    stored,
    world,
)

from fylgja.core.journal import RUN_SENTENCE
from fylgja.models import ReportSection, SessionActivity, SessionActivityEvent

RUN = "flow:r1"
PLAN = f"{REPO}/demo-plan"
SECTIONS = [
    ReportSection(
        path=f"{PLAN}/followups.md",
        heading="Unapplied review findings",
        text="low Rename the helper",
    ),
    ReportSection(
        path=f"{PLAN}/handoff.md", heading="Known limitations", text="Retries are not capped."
    ),
    ReportSection(
        path=f"{PLAN}/handoff.md", heading="For the human", text="Check the demo screen."
    ),
]


def adds(count: int, kind: str = "next") -> list[dict[str, str]]:
    return [{"kind": kind, "text": f"Do the {kind} thing {n}"} for n in range(count)]


async def start_run(w: World) -> None:
    agents = w.daemon.sink("agents")
    agents.emit(
        session_snapshot(
            "agents",
            [
                session("claude", "a", pid=501, cwd=REPO, host_pid=500),
                session("claude", "b", pid=601, cwd="/w/other", host_pid=600),
                session("flow", "r1", cwd=REPO),
            ],
            w.clock.now(),
        )
    )
    await w.quiet()
    await finish(w, "running", None)
    await w.until(30)


async def finish(w: World, state: str, report: list[ReportSection] | None) -> None:
    w.daemon.sink("agents").emit(
        SessionActivityEvent(
            source="agents",
            entity_id=RUN,
            observed_at=w.clock.now(),
            payload=SessionActivity(state=state, title="implement_features_v3", report=report),
        )
    )
    await w.quiet()


def cite_sections(w: World, **body: Any) -> None:
    """Answer with adds that cite the report sections by the ``S`` ids the prompt gave them."""

    async def answer(call: Call) -> None:
        ids = re.findall(r'^S(\d+) report section "([^"]+)"', call.prompt, re.MULTILINE)
        adds_ = [
            {"kind": "next", "text": f"Do {heading.lower()} {n}", "source": f"S{number}"}
            for n, (number, heading) in enumerate(ids)
        ]
        w.runner.default = reply(sources=["S1"], add=adds_, **body)

    w.runner.on_call = answer


async def test_a_finished_run_with_reports_gives_one_run_finished_entry() -> None:
    async with world() as w:
        cite_sections(w)
        await start_run(w)
        await finish(w, "passed", SECTIONS)
        found = stored(w.daemon)
        assert [entry.trigger for entry in found.entries] == ["run_finished"]
        assert len(w.runner.calls) == 1


async def test_the_prompt_holds_the_run_variant_sentence_and_the_sections() -> None:
    async with world() as w:
        cite_sections(w)
        await start_run(w)
        await finish(w, "passed", SECTIONS)
        prompt = w.runner.calls[0].prompt
        assert RUN_SENTENCE in prompt
        assert "The run finished: passed." in prompt
        assert 'report section "Unapplied review findings" of followups.md:' in prompt
        assert "    low Rename the helper" in prompt
        assert 'report section "For the human" of handoff.md:' in prompt
        assert "    Check the demo screen." in prompt


async def test_five_next_adds_are_accepted_and_cite_file_sources() -> None:
    async with world() as w:
        reports = [*SECTIONS, SECTIONS[0], SECTIONS[1]]
        cite_sections(w)
        await start_run(w)
        await finish(w, "passed", reports)
        found = stored(w.daemon)
        assert found.thread is not None
        assert found.thread.error_kind is None
        assert len(found.items) == 5
        assert {item.kind for item in found.items} == {"next"}
        for item in found.items:
            assert item.source["kind"] == "file"
            assert item.source["path"].startswith(PLAN)
            assert item.source["section"] in {s.heading for s in SECTIONS}
        assert {item.source["path"] for item in found.items} == {
            f"{PLAN}/followups.md",
            f"{PLAN}/handoff.md",
        }
        assert found.entries[0].sources
        assert any(source["kind"] == "file" for source in found.entries[0].sources)
        assert found.thread.thread_id == MAIN_ID


async def test_a_run_without_reports_has_the_normal_caps() -> None:
    async with world() as w:
        w.runner.default = reply(add=adds(4))
        await start_run(w)
        await finish(w, "passed", None)
        prompt = w.runner.calls[0].prompt
        assert RUN_SENTENCE not in prompt
        assert "report section" not in prompt
        found = stored(w.daemon)
        assert found.thread is not None
        assert found.thread.error_kind == "bad_output"
        assert found.entries == []
        assert found.items == []


async def test_a_run_without_reports_accepts_three_next_adds() -> None:
    async with world() as w:
        w.runner.default = reply(add=adds(3))
        await start_run(w)
        await finish(w, "passed", None)
        found = stored(w.daemon)
        assert [entry.trigger for entry in found.entries] == ["run_finished"]
        assert len(found.items) == 3


async def test_the_sections_reach_the_daemons_session_memory() -> None:
    async with world() as w:
        cite_sections(w)
        await start_run(w)
        await finish(w, "passed", SECTIONS)
        extras = w.daemon.state().sessions[RUN]
        assert extras.report == tuple(SECTIONS)
