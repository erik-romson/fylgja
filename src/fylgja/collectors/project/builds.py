"""The build fold: GitHub Actions runs to ``projects[].build`` (``PLAN-final-plan.md`` §4.7, A21).

A pure Python port of Lidskjalv's ``_BUILDS_FOLD_JQ``. It reads the body of
``gh api repos/{owner}/{repo}/actions/runs?branch=…&per_page=30`` (newest run first) and keeps:

* the runs of the **newest head sha** (the first run's), nothing older;
* of those, one run per workflow: a **re-run** collapses to the run with the highest
  ``run_number`` (the later one when two have the same number);
* no ``skipped`` or ``neutral`` run;
* per kept run its ``state`` (``running`` unless completed; then ``pass`` for ``success``, ``fail``
  for ``failure``, ``timed_out`` and ``startup_failure``, ``cancelled``, and ``none`` for every
  other conclusion), its duration when completed, and the **typical duration** of its workflow:
  the median over the whole page of the durations of that workflow's successful runs (an even
  count gives the mean of the two middle values, rounded half away from zero, as jq's ``round``);
* the aggregate: the worst of ``fail`` > ``running`` > ``cancelled`` > ``pass`` > ``none``, and
  the url of the only workflow, or the commit's checks page when there are several.

A page with no run is ``none`` with no sha; a newest sha whose runs are all dropped is ``none``
with that sha. Anything the program would have failed on (a body that is not JSON, a kept run
without a start time, a time that is not ``YYYY-MM-DDTHH:MM:SSZ``) raises :class:`BuildFoldError`.
The parity test compares this port with goldens that ``spikes/gen_lidskjalv_build_goldens.py``
produced once from the jq program.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

from fylgja.models import BuildPayload, BuildWorkflow

__all__ = [
    "BUILD_PARSE",
    "BuildFold",
    "BuildFoldError",
    "BuildState",
    "fold_runs",
    "fold_runs_body",
]

BUILD_PARSE: Final = "build_parse"

type BuildState = Literal["pass", "fail", "running", "cancelled", "none"]

_RANK: Final[Mapping[str, int]] = {"fail": 4, "running": 3, "cancelled": 2, "pass": 1, "none": 0}
_BY_RANK: Final[Mapping[int, BuildState]] = {
    4: "fail",
    3: "running",
    2: "cancelled",
    1: "pass",
    0: "none",
}
_FAILED: Final = frozenset({"failure", "timed_out", "startup_failure"})
_DROPPED: Final = frozenset({"skipped", "neutral"})


class BuildFoldError(ValueError):
    """The runs response is not something the fold can read."""

    kind = BUILD_PARSE


@dataclass(frozen=True)
class BuildFold:
    """The folded runs of one branch."""

    state: BuildState
    head_sha: str | None
    url: str | None
    workflows: tuple[BuildWorkflow, ...]

    def to_payload(
        self,
        project_path: str,
        *,
        error_kind: str | None = None,
        poll_interval_s: float | None = None,
    ) -> BuildPayload:
        return BuildPayload(
            state=self.state,
            project_path=project_path,
            head_sha=self.head_sha,
            url=self.url,
            workflows=self.workflows,
            error_kind=error_kind,
            poll_interval_s=poll_interval_s,
        )


NONE_FOLD: Final = BuildFold(state="none", head_sha=None, url=None, workflows=())


def _epoch(value: object) -> int:
    """``fromdateiso8601``: only ``YYYY-MM-DDTHH:MM:SSZ``."""
    if not isinstance(value, str):
        raise BuildFoldError("timestamp")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise BuildFoldError("timestamp") from exc
    return int(parsed.timestamp())


def _round_half_away(value: float) -> int:
    return math.floor(value + 0.5) if value >= 0 else -math.floor(-value + 0.5)


def _median(values: Sequence[int]) -> int | None:
    ordered = sorted(values)
    count = len(ordered)
    if count == 0:
        return None
    if count % 2 == 1:
        return ordered[count // 2]
    return _round_half_away((ordered[count // 2 - 1] + ordered[count // 2]) / 2)


def _order(value: object) -> tuple[int, float]:
    """jq's order for the numbers a run carries: ``null`` sorts before every number."""
    if value is None:
        return (0, 0.0)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise BuildFoldError("sort_key")
    return (1, float(value))


def _run_state(run: Mapping[str, Any]) -> BuildState:
    if run.get("status") != "completed":
        return "running"
    conclusion = run.get("conclusion")
    if conclusion == "success":
        return "pass"
    if conclusion in _FAILED:
        return "fail"
    if conclusion == "cancelled":
        return "cancelled"
    return "none"


def _duration(run: Mapping[str, Any]) -> int:
    return _epoch(run.get("updated_at")) - _epoch(run.get("run_started_at"))


def _workflow_name(run: Mapping[str, Any]) -> str:
    path = run.get("path")
    if path:
        if not isinstance(path, str):
            raise BuildFoldError("path")
        return path.split("/")[-1]
    name = run.get("name")
    if not isinstance(name, str):
        raise BuildFoldError("name")
    return name


def _str_or_none(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise BuildFoldError("field")


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise BuildFoldError("field")
    return value


def _workflow(run: Mapping[str, Any], page: Sequence[Mapping[str, Any]]) -> BuildWorkflow:
    wid = run.get("workflow_id")
    typical = _median(
        [
            _duration(other)
            for other in page
            if other.get("workflow_id") == wid and _is_success(other)
        ]
    )
    completed = run.get("status") == "completed"
    started = _epoch(run.get("run_started_at"))
    return BuildWorkflow(
        workflow=_workflow_name(run),
        state=_run_state(run),
        status=_str_or_none(run.get("status")),
        conclusion=_str_or_none(run.get("conclusion")),
        run_id=_int_or_none(run.get("id")),
        url=_str_or_none(run.get("html_url")),
        run_started_at=datetime.fromtimestamp(started, tz=UTC),
        run_duration_s=_duration(run) if completed else None,
        typical_duration_s=typical,
    )


def _is_success(run: Mapping[str, Any]) -> bool:
    return run.get("conclusion") == "success"


def fold_runs(body: object, owner_repo: str) -> BuildFold:
    """Fold a decoded runs response. ``owner_repo`` builds the checks-page url."""
    if not isinstance(body, Mapping):
        raise BuildFoldError("shape")
    runs = body.get("workflow_runs")
    if runs is None:
        runs = []
    if not isinstance(runs, list) or not all(isinstance(run, Mapping) for run in runs):
        raise BuildFoldError("shape")
    page: list[Mapping[str, Any]] = list(runs)
    if not page:
        return NONE_FOLD
    newest = page[0].get("head_sha")
    newest_sha = _str_or_none(newest)
    # Runs on the newest sha, one per workflow (the highest run_number), in workflow_id order.
    by_workflow: dict[tuple[int, float], list[Mapping[str, Any]]] = {}
    for run in page:
        if run.get("head_sha") == newest:
            by_workflow.setdefault(_order(run.get("workflow_id")), []).append(run)
    collapsed = [
        sorted(by_workflow[key], key=lambda run: _order(run.get("run_number")))[-1]
        for key in sorted(by_workflow)
    ]
    kept = [run for run in collapsed if run.get("conclusion") not in _DROPPED]
    if not kept:
        return BuildFold(state="none", head_sha=newest_sha, url=None, workflows=())
    workflows = tuple(_workflow(run, page) for run in kept)
    state = _BY_RANK[max(_RANK[w.state or "none"] for w in workflows)]
    url = (
        workflows[0].url
        if len(workflows) == 1
        else f"https://github.com/{owner_repo}/commit/{newest_sha}/checks"
    )
    return BuildFold(state=state, head_sha=newest_sha, url=url, workflows=workflows)


def fold_runs_body(body: bytes | str, owner_repo: str) -> BuildFold:
    """Fold the raw response body. Raises :class:`BuildFoldError` for text that is not JSON."""
    try:
        decoded = json.loads(body)
    except (ValueError, RecursionError) as exc:
        raise BuildFoldError("json") from exc
    return fold_runs(decoded, owner_repo)
