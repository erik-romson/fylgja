"""Keeps docs/decisions/ in step with the amendment table in PLAN-final-plan.md section 2."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLAN = REPO_ROOT / "PLAN-final-plan.md"
DECISIONS = REPO_ROOT / "docs" / "decisions"

HEADER_FIELDS = ("Status", "Amendment", "Amends", "Answers", "Relates to", "Date")
SECTIONS = ("Context", "Decision", "Consequences")
ANSWER_REQUIRED = ("A1", "A10", "A21", "A22", "A23", "A24")


def _plan_amendment_ids() -> list[str]:
    ids: list[str] = []
    in_section = False
    for line in PLAN.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            if in_section:
                break
            in_section = line.startswith("## 2.")
            continue
        if in_section:
            match = re.match(r"\| (A\d+) \|", line)
            if match:
                ids.append(match.group(1))
    return ids


def _field(text: str, name: str) -> str | None:
    match = re.search(rf"^- {re.escape(name)}:[ \t]*(.*)$", text, flags=re.MULTILINE)
    return match.group(1).strip() if match else None


def _records() -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(DECISIONS.glob("[0-9][0-9][0-9][0-9]-*.md"))
    }


def _amendment_of(text: str) -> str:
    value = _field(text, "Amendment")
    assert value is not None
    return value


def test_plan_lists_a1_to_a24_contiguously() -> None:
    ids = _plan_amendment_ids()
    assert ids == [f"A{n}" for n in range(1, 25)]


def test_every_amendment_has_exactly_one_record() -> None:
    named = [_amendment_of(text) for text in _records().values()]
    for amendment in _plan_amendment_ids():
        assert named.count(amendment) == 1, amendment


def test_no_record_names_an_unknown_amendment() -> None:
    known = set(_plan_amendment_ids()) | {"none"}
    for name, text in _records().items():
        assert _amendment_of(text) in known, name


def test_record_numbers_are_unique() -> None:
    numbers = [name[:4] for name in _records()]
    assert numbers
    assert len(numbers) == len(set(numbers))


def test_seeded_record_number_matches_amendment() -> None:
    for name, text in _records().items():
        amendment = _amendment_of(text)
        if amendment != "none" and int(name[:4]) <= 24:
            assert amendment == f"A{int(name[:4])}", name


@pytest.mark.parametrize("name", sorted(_records()))
def test_record_has_header_fields_and_sections(name: str) -> None:
    text = _records()[name]
    assert re.match(rf"# {name[:4]}: \S", text), "title line must be '# NNNN: <title>'"
    for field in HEADER_FIELDS:
        value = _field(text, field)
        assert value, f"{name}: missing or empty {field}"
    assert _field(text, "Status") == "accepted"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", _field(text, "Date") or "")
    for section in SECTIONS:
        match = re.search(rf"^## {section}\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
        assert match, f"{name}: missing section {section}"
        assert match.group(1).strip(), f"{name}: empty section {section}"


def test_product_decisions_cite_their_questions() -> None:
    by_amendment = {_amendment_of(text): text for text in _records().values()}
    for amendment in ANSWER_REQUIRED:
        answers = _field(by_amendment[amendment], "Answers") or ""
        assert re.findall(r"Q\d+", answers), f"{amendment}: Answers must hold Q<n> IDs"
