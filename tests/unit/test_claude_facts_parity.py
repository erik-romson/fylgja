"""Parity of the Python facts block with Lidskjalv's ``_SUMMARY_JQ`` (WP1.7, A21).

``tests/fixtures/lidskjalv/facts/<fixture>.json`` is the recorded output of the jq program,
written once by ``spikes/gen_lidskjalv_facts_goldens.py``. The tests never run or need Lidskjalv
or ``jq``.

**Deliberate difference: ``tasks``.** The jq program reports ``todos`` (the last ``TodoWrite``)
only. The port follows the task tools too (R7: ``~/.claude/tasks`` is gone), so it is compared with
``todos`` only for transcripts that use ``TodoWrite`` alone, and the task tools have dedicated
fixtures in ``test_claude_transcript.py``. ``session_id`` is the caller's, not a transcript fact.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fylgja.collectors.claude import facts

ROOT = Path(__file__).resolve().parent.parent / "fixtures"
GOLDENS = ROOT / "lidskjalv" / "facts"

SAME_FIELDS = (
    "title",
    "prompt",
    "said",
    "mode",
    "branch",
    "version",
    "model",
    "turns",
    "started",
    "last_at",
    "prompts",
    "tools",
    "files",
)


def _fixtures() -> list[tuple[str, Path]]:
    found = [(p.stem, p) for p in sorted((ROOT / "lidskjalv" / "transcripts").glob("*.jsonl"))]
    found += [
        (f"ratatosk-{p.parent.name}-{p.stem}", p)
        for p in sorted((ROOT / "ratatosk").glob("*/*.jsonl"))
    ]
    return found


def _entries(path: Path) -> list[dict[str, Any]]:
    """What ``jq -R`` plus ``fromjson?`` sees: every line that is a JSON object."""
    decoded = (facts.decode_line(line) for line in path.read_text(encoding="utf-8").split("\n"))
    return [entry for entry in decoded if entry is not None]


def test_there_is_a_golden_for_every_fixture_and_no_other() -> None:
    names = {name for name, _ in _fixtures()}
    assert names, "no transcript fixture found"
    assert {p.stem for p in GOLDENS.glob("*.json")} == names


@pytest.mark.parametrize(("name", "path"), _fixtures(), ids=[n for n, _ in _fixtures()])
def test_facts_equal_the_golden(name: str, path: Path) -> None:
    golden = json.loads((GOLDENS / f"{name}.json").read_text(encoding="utf-8"))
    ported = facts.facts_of(_entries(path))
    for field in SAME_FIELDS:
        assert ported[field] == golden[field], field
    assert set(ported) == {*SAME_FIELDS, "tasks"}
    assert golden["session_id"] == name.replace("ratatosk-", "rt-")


@pytest.mark.parametrize(("name", "path"), _fixtures(), ids=[n for n, _ in _fixtures()])
def test_tasks_equal_the_goldens_todos_when_only_todowrite_is_used(name: str, path: Path) -> None:
    golden = json.loads((GOLDENS / f"{name}.json").read_text(encoding="utf-8"))
    text = path.read_text(encoding="utf-8")
    assert '"name":"TaskCreate"' not in text
    assert '"name":"TaskUpdate"' not in text
    assert facts.facts_of(_entries(path))["tasks"] == golden["todos"]


def test_the_fixtures_exercise_the_interesting_parts() -> None:
    goldens = [json.loads(p.read_text(encoding="utf-8")) for p in GOLDENS.glob("*.json")]
    assert any(g["todos"] for g in goldens)
    assert any(g["tools"] for g in goldens)
    assert any(g["files"] for g in goldens)
    assert any(g["prompts"] for g in goldens)
    assert any(g["said"] == "" for g in goldens)


def test_incremental_folding_equals_the_one_shot_facts() -> None:
    for _, path in _fixtures():
        accumulator = facts.FactsAccumulator()
        for entry in _entries(path):
            accumulator.feed(entry)
        assert accumulator.facts() == facts.facts_of(_entries(path))
