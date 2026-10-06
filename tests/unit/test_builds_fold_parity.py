"""The build fold is a port of Lidskjalv's ``_BUILDS_FOLD_JQ`` (``PLAN-final-plan.md`` §4.7, A21).

For every sanitized runs response in ``tests/fixtures/project/gh/actions-*.json`` the Python fold
must equal the golden that ``spikes/gen_lidskjalv_build_goldens.py`` produced once from the jq
program (``tests/fixtures/lidskjalv/builds/``). The tests never run or need Lidskjalv or jq.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fylgja.collectors.project import builds

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNS = REPO_ROOT / "tests" / "fixtures" / "project" / "gh"
GOLDENS = REPO_ROOT / "tests" / "fixtures" / "lidskjalv" / "builds"
FIXTURES = sorted(RUNS.glob("actions-*.json"))
OWNER_REPO = "example/repo"  # what the generator passes to the program


def _golden_form(fold: builds.BuildFold) -> dict[str, Any]:
    """The fold in the jq program's own names: epoch seconds and ``run_duration``."""
    return {
        "state": fold.state,
        "head_sha": fold.head_sha,
        "url": fold.url,
        "workflows": [
            {
                "workflow": w.workflow,
                "state": w.state,
                "status": w.status,
                "conclusion": w.conclusion,
                "run_id": w.run_id,
                "url": w.url,
                "run_started_at": int(w.run_started_at.timestamp()) if w.run_started_at else None,
                "run_duration": w.run_duration_s,
                "typical_duration": w.typical_duration_s,
            }
            for w in fold.workflows
        ],
    }


def _fixture_ids() -> list[str]:
    return [path.stem for path in FIXTURES]


@pytest.mark.parametrize("fixture", FIXTURES, ids=_fixture_ids())
def test_the_python_fold_equals_the_jq_golden(fixture: Path) -> None:
    golden = json.loads((GOLDENS / fixture.name).read_text(encoding="utf-8"))
    fold = builds.fold_runs_body(fixture.read_bytes(), OWNER_REPO)
    assert _golden_form(fold) == golden


def test_every_fixture_has_a_golden_and_the_cases_the_plan_names_are_there() -> None:
    names = {path.stem for path in FIXTURES}
    assert {path.stem for path in GOLDENS.glob("actions-*.json")} == names
    # a rerun, a skipped run, an in-progress run and a cancelled run (WP5.4)
    assert {"actions-rerun", "actions-skipped", "actions-running", "actions-cancelled"} <= names


def test_the_goldens_cover_every_aggregate_state() -> None:
    states = {
        json.loads(path.read_text(encoding="utf-8"))["state"]
        for path in GOLDENS.glob("actions-*.json")
    }
    assert states == {"pass", "fail", "running", "cancelled", "none"}


def test_a_rerun_collapses_to_the_highest_run_number_and_dropped_runs_vanish() -> None:
    rerun = builds.fold_runs_body((RUNS / "actions-rerun.json").read_bytes(), OWNER_REPO)
    assert [(w.run_id, w.state) for w in rerun.workflows] == [(402, "pass")]
    skipped = builds.fold_runs_body((RUNS / "actions-skipped.json").read_bytes(), OWNER_REPO)
    assert [w.workflow for w in skipped.workflows] == ["ci.yml"]
    mixed = builds.fold_runs_body((RUNS / "actions-mixed-sha.json").read_bytes(), OWNER_REPO)
    assert mixed.head_sha == "fff666"
    assert [w.run_id for w in mixed.workflows] == [601]
    # the typical duration is over the whole page, and only successful runs count: the failed run
    # of the older sha does not
    assert mixed.workflows[0].typical_duration_s == 300


def test_the_aggregate_url_is_the_runs_for_one_workflow_and_the_checks_page_for_several() -> None:
    one = builds.fold_runs_body((RUNS / "actions-pass.json").read_bytes(), OWNER_REPO)
    assert one.url == one.workflows[0].url
    several = builds.fold_runs_body((RUNS / "actions-fail-multi.json").read_bytes(), OWNER_REPO)
    assert several.url == "https://github.com/example/repo/commit/bbb222/checks"
    assert several.state == "fail"


def test_a_page_without_runs_is_none_and_so_is_a_page_whose_runs_are_all_dropped() -> None:
    empty = builds.fold_runs_body((RUNS / "actions-empty.json").read_bytes(), OWNER_REPO)
    assert empty == builds.NONE_FOLD
    only_skipped = {
        "workflow_runs": [
            {
                "id": 1,
                "head_sha": "abc",
                "workflow_id": 1,
                "run_number": 1,
                "status": "completed",
                "conclusion": "skipped",
            }
        ]
    }
    fold = builds.fold_runs(only_skipped, OWNER_REPO)
    assert (fold.state, fold.head_sha, fold.url, fold.workflows) == ("none", "abc", None, ())
    assert builds.fold_runs({}, OWNER_REPO) == builds.NONE_FOLD
    assert builds.fold_runs({"workflow_runs": None}, OWNER_REPO) == builds.NONE_FOLD


def test_the_median_is_rounded_half_away_from_zero_like_jq() -> None:
    assert builds._median([]) is None
    assert builds._median([5]) == 5
    assert builds._median([1, 2]) == 2  # 1.5 rounds up
    assert builds._median([10, 1, 7, 2]) == 5  # (2 + 7) / 2 = 4.5 -> 5
    assert builds._median([-3, -2]) == -3  # -2.5 rounds away from zero
    assert builds._median([3, 1, 2]) == 2


@pytest.mark.parametrize(
    ("status", "conclusion", "state"),
    [
        ("queued", None, "running"),
        ("in_progress", None, "running"),
        ("completed", "success", "pass"),
        ("completed", "failure", "fail"),
        ("completed", "timed_out", "fail"),
        ("completed", "startup_failure", "fail"),
        ("completed", "cancelled", "cancelled"),
        ("completed", "action_required", "none"),
        ("completed", None, "none"),
    ],
)
def test_the_state_of_one_run(status: str, conclusion: str | None, state: str) -> None:
    run = {
        "id": 1,
        "head_sha": "abc",
        "workflow_id": 1,
        "run_number": 1,
        "name": "CI",
        "status": status,
        "conclusion": conclusion,
        "run_started_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:01:00Z",
    }
    fold = builds.fold_runs({"workflow_runs": [run]}, OWNER_REPO)
    assert fold.state == state
    assert fold.workflows[0].workflow == "CI"  # no path: the name


def test_the_worst_state_wins() -> None:
    def run(wid: int, status: str, conclusion: str | None) -> dict[str, Any]:
        return {
            "id": wid,
            "head_sha": "abc",
            "workflow_id": wid,
            "run_number": 1,
            "name": f"w{wid}",
            "status": status,
            "conclusion": conclusion,
            "run_started_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:01:00Z",
        }

    ok, running, cancelled = (
        run(1, "completed", "success"),
        run(2, "in_progress", None),
        run(3, "completed", "cancelled"),
    )
    failed = run(4, "completed", "failure")
    assert builds.fold_runs({"workflow_runs": [ok, cancelled]}, OWNER_REPO).state == "cancelled"
    assert (
        builds.fold_runs({"workflow_runs": [ok, cancelled, running]}, OWNER_REPO).state == "running"
    )
    assert builds.fold_runs({"workflow_runs": [ok, running, failed]}, OWNER_REPO).state == "fail"


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"workflow_runs": "x"}',
        b'{"workflow_runs": [1]}',
        b'{"workflow_runs": [{"head_sha": 5}]}',
        b'{"workflow_runs": [{"head_sha": "a", "workflow_id": "x"}]}',
        b'{"workflow_runs": [{"head_sha": "a", "workflow_id": 1, "run_number": true}]}',
        # a kept run without a usable start time is what the jq program fails on too
        b'{"workflow_runs": [{"head_sha": "a", "workflow_id": 1, "name": "n",'
        b' "status": "completed", "conclusion": "success", "updated_at": "2026-01-01T00:00:00Z"}]}',
        b'{"workflow_runs": [{"head_sha": "a", "workflow_id": 1, "name": "n",'
        b' "status": "queued", "run_started_at": "2026-01-01 00:00:00",'
        b' "updated_at": "2026-01-01T00:00:00Z"}]}',
        b'{"workflow_runs": [{"head_sha": "a", "workflow_id": 1, "name": 7,'
        b' "status": "queued", "run_started_at": "2026-01-01T00:00:00Z"}]}',
        b'{"workflow_runs": [{"head_sha": "a", "workflow_id": 1, "path": 7, "name": "n",'
        b' "status": "queued", "run_started_at": "2026-01-01T00:00:00Z"}]}',
        b'{"workflow_runs": [{"head_sha": "a", "workflow_id": 1, "name": "n", "id": "x",'
        b' "status": "queued", "run_started_at": "2026-01-01T00:00:00Z"}]}',
        b'{"workflow_runs": [{"head_sha": "a", "workflow_id": 1, "name": "n", "html_url": 3,'
        b' "status": "queued", "run_started_at": "2026-01-01T00:00:00Z"}]}',
    ],
)
def test_a_response_the_program_could_not_fold_is_an_error(body: bytes) -> None:
    with pytest.raises(builds.BuildFoldError) as raised:
        builds.fold_runs_body(body, OWNER_REPO)
    assert raised.value.kind == builds.BUILD_PARSE


def test_the_fold_becomes_a_build_payload() -> None:
    fold = builds.fold_runs_body((RUNS / "actions-running.json").read_bytes(), OWNER_REPO)
    payload = fold.to_payload("/code/app", error_kind="gh_timeout", poll_interval_s=300)
    assert payload.state == "running"
    assert payload.project_path == "/code/app"
    assert payload.error_kind == "gh_timeout"
    assert payload.poll_interval_s == 300
    assert payload.workflows[0].typical_duration_s == 480
    assert payload.workflows[0].run_duration_s is None
