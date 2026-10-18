"""The pure helpers that cut a finished run's reports into sections (docs/decisions/0057, J21)."""

from __future__ import annotations

import json
import posixpath
from pathlib import Path

from fylgja.collectors.norn import parse
from fylgja.models import ReportSection

REPORTS = Path(__file__).resolve().parent.parent / "fixtures" / "norn" / "reports"
PLAN = "/w/repo/demo-plan"


def fixture(name: str) -> str:
    return (REPORTS / name).read_text(encoding="utf-8")


def header(name: str) -> parse.RunHeader:
    header = parse.parse_run_json(fixture(name))
    assert header is not None
    return header


def indexed(*paths: str):  # type: ignore[no-untyped-def]
    return lambda path: path in paths


def headings(sections: list[ReportSection]) -> list[str]:
    return [section.heading for section in sections]


# --- the plan directory -----------------------------------------------------------------------


def test_the_plan_directory_comes_from_params_args_of_a_norn_ui_run() -> None:
    found = parse.plan_dir(header("run-ui.json"), indexed(PLAN))
    assert found == PLAN


def test_the_plan_directory_comes_from_argv_of_a_norn_run_resolved_against_cwd() -> None:
    assert parse.plan_dir(header("run-cli.json"), indexed(PLAN)) == PLAN


def test_a_token_that_is_already_absolute_is_taken_as_it_is() -> None:
    raw = json.loads(fixture("run-cli.json"))
    raw["argv"][3] = "/elsewhere/plan"
    parsed = parse.parse_run_json(json.dumps(raw))
    assert parsed is not None
    assert parse.plan_dir(parsed, indexed("/elsewhere/plan")) == "/elsewhere/plan"


def test_the_params_args_of_a_header_are_kept_and_capped() -> None:
    assert header("run-ui.json").params_args == "--resume /w/repo/demo-plan"
    big = json.dumps({"params": {"args": "x" * 9000}})
    parsed = parse.parse_run_json(big)
    assert parsed is not None
    assert parsed.params_args is not None
    assert len(parsed.params_args) == parse.MAX_PARAMS_ARGS
    assert parse.parse_run_json("{}").params_args is None  # type: ignore[union-attr]


def test_a_plan_with_review_header_has_no_plan_directory() -> None:
    raw = json.loads(fixture("run-ui.json"))
    raw["pipeline"] = "plan_with_review"
    parsed = parse.parse_run_json(json.dumps(raw))
    assert parsed is not None
    assert parse.plan_dir(parsed, lambda path: True) is None


def test_a_token_naming_a_directory_without_an_index_gives_none() -> None:
    assert parse.plan_dir(header("run-ui.json"), lambda path: False) is None


def test_unbalanced_quotes_in_params_args_give_none() -> None:
    parsed = parse.parse_run_json(
        json.dumps({"pipeline": "implement_features_v3", "params": {"args": "'unclosed"}})
    )
    assert parsed is not None
    assert parse.plan_dir(parsed, lambda path: True) is None


# --- followups.md -----------------------------------------------------------------------------


def test_followups_gives_a_section_per_open_line_and_never_a_done_one() -> None:
    sections = parse.cut_report(f"{PLAN}/followups.md", fixture("demo-plan/followups.md"))
    assert [section.text for section in sections] == [
        "low Rename the helper (demo.py:12)",
        "human Try the demo on a second machine",
    ]
    assert headings(sections) == ["Unapplied review findings"] * 2
    assert {section.path for section in sections} == {f"{PLAN}/followups.md"}


def test_a_followups_file_with_none_gives_no_lines() -> None:
    text = "# Follow-ups — demo\n\n## Unapplied review findings\n\n- none\n"
    assert parse.cut_report("followups.md", text) == []
    assert parse.cut_report("followups.md", "- [ ] none\n") == []


# --- handoff.md -------------------------------------------------------------------------------


def test_handoff_gives_three_sections_and_not_the_changes_summary() -> None:
    sections = parse.cut_report("handoff.md", fixture("demo-plan/handoff.md"))
    assert headings(sections) == ["For the human", "Known limitations", "Overview"]
    assert sections[0].text == "Check the demo screen once by hand."
    assert "Retries are not capped yet." in sections[1].text


def test_an_h3_does_not_end_a_handoff_section() -> None:
    sections = parse.cut_report("handoff.md", fixture("demo-plan/handoff.md"))
    assert "An H3 inside a section does not end it." in sections[1].text
    assert "The review passed." not in sections[1].text


def test_handoff_headings_match_without_regard_to_case() -> None:
    text = (
        "## FOR THE HUMAN\n\nLook.\n\n## known Limitations\n\nNone yet.\n\n## overview\n\nShort.\n"
    )
    sections = parse.cut_report("handoff.md", text)
    assert headings(sections) == ["FOR THE HUMAN", "known Limitations", "overview"]


def test_an_empty_section_is_left_out_and_a_long_one_is_cut() -> None:
    text = "## Overview\n\n## For the human\n\n" + "é" * 3000 + "\n"
    sections = parse.cut_report("handoff.md", text)
    assert headings(sections) == ["For the human"]
    assert len(sections[0].text.encode("utf-8")) <= parse.MAX_SECTION_BYTES


# --- review.md --------------------------------------------------------------------------------


def test_review_gives_the_verdict_and_the_blocking_findings() -> None:
    sections = parse.cut_report("review.md", fixture("demo-plan/review.md"))
    assert headings(sections) == ["Verdict", "Blocking findings"]
    assert sections[0].text == "VERDICT: PASS"


def test_for_the_human_of_review_is_taken_only_without_followups() -> None:
    with_followups = parse.cut_report("review.md", fixture("demo-plan/review.md"))
    assert "For the human" not in headings(with_followups)
    without = parse.cut_report("review.md", fixture("demo-plan/review.md"), has_followups=False)
    assert headings(without) == ["Verdict", "Blocking findings", "For the human"]


def test_a_review_without_a_verdict_line_gives_only_its_sections() -> None:
    sections = parse.cut_report("review.md", "Some words\n\n## Blocking findings\n\nOne.\n")
    assert headings(sections) == ["Blocking findings"]


def test_an_unknown_file_gives_nothing() -> None:
    assert parse.cut_report("learning.md", "## Overview\n\nx\n") == []


# --- the run report ---------------------------------------------------------------------------


def test_the_run_report_gives_the_deviation_lines_and_the_stop_heading() -> None:
    sections = parse.cut_report("tmp/demo-run/report.md", fixture("tmp/demo-run/report.md"))
    assert headings(sections) == ["Run report"]
    lines = sections[0].text.split("\n")
    assert "The retry loop uses a fixed delay, a judgement call." in lines
    assert "The test double replaces the clock, a deviation from the plan." in lines
    assert "## STOP in step-02-demo" in lines
    assert "No deviations." not in lines


def test_a_run_report_gives_at_most_twenty_lines() -> None:
    text = "".join(f"deviation {number}\n" for number in range(50))
    sections = parse.cut_report("report.md", text)
    assert len(sections[0].text.split("\n")) == parse.MAX_REPORT_LINES
    assert parse.cut_report("report.md", "- None, no deviation at all.\nNo deviation.\n") == []


def test_the_run_report_path_comes_from_the_readme_first_then_the_index() -> None:
    readme = fixture("demo-plan/README.md")
    index = fixture("demo-plan/index.md")
    assert parse.report_path(readme, index) == "tmp/demo-run/report.md"
    assert parse.report_path(None, index) == "tmp/demo-run/report.md"
    assert parse.report_path("see tmp/a-run/report.md", "tmp/b-run/report.md") == (
        "tmp/a-run/report.md"
    )
    assert parse.report_path("nothing here", None) is None
    assert parse.report_path("tmp/../x-run/report.md", None) is None


# --- the size limit ---------------------------------------------------------------------------


def test_thirty_sections_of_four_kilobytes_stop_below_the_digest_limit() -> None:
    sections = [
        ReportSection(path="p", heading=f"h{number}", text="x" * parse.MAX_SECTION_BYTES)
        for number in range(30)
    ]
    kept = parse.assemble(sections)
    size = sum(len(s.heading.encode()) + len(s.text.encode()) for s in kept)
    assert 0 < len(kept) < 30
    assert size <= parse.REPORT_DIGEST_MAX_BYTES == 24576
    assert kept == sections[: len(kept)]  # in order, from the front


def test_the_digest_limit_is_the_journals() -> None:
    from fylgja.core.journal import RUN_DIGEST_MAX_BYTES

    assert parse.REPORT_DIGEST_MAX_BYTES == RUN_DIGEST_MAX_BYTES


def test_a_whole_demo_plan_fits_and_keeps_the_order_of_the_table() -> None:
    plan = REPORTS / "demo-plan"
    sections: list[ReportSection] = []
    for name in ("followups.md", "handoff.md", "review.md"):
        sections += parse.cut_report(posixpath.join(PLAN, name), (plan / name).read_text())
    assert headings(parse.assemble(sections)) == [
        "Unapplied review findings",
        "Unapplied review findings",
        "For the human",
        "Known limitations",
        "Overview",
        "Verdict",
        "Blocking findings",
    ]
