"""``gh`` for the project state (WP5.4): remotes, ``gh pr view`` output, ``gh api -i`` responses,
the failure codes (gh not authenticated is ``unavailable`` with an ``error_kind``) and the backoff.
"""

from __future__ import annotations

import json

import pytest
from project_support import (
    REPO,
    activity,
    api_response,
    engine,
    events,
    fixture_text,
    fs_with,
    projects,
    script_git,
    script_pr,
    script_runs,
)

from fylgja.collectors.project import ghparse
from fylgja.testing.fakes import FakeClock, FakeProcessRunner

APP = "/code/app"
LIVE = activity(live=[APP])

# -- remotes ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:example/repo.git", "example/repo"),
        ("https://github.com/example/repo", "example/repo"),
        ("https://github.com/example/repo.git", "example/repo"),
        ("https://github.com/example/repo/", "example/repo"),
        ("ssh://git@github.com/example/repo.git", "example/repo"),
        ("https://user:token@github.com/example/repo.git", "example/repo"),
        ("https://github.com/ex-ample/re.po_1", "ex-ample/re.po_1"),
        ("https://gitlab.com/example/repo.git", None),
        ("git@github.example.com:example/repo.git", None),
        ("https://github.com/example", None),
        ("https://github.com/example/repo/extra", None),
        ("https://github.com/exa mple/repo", None),
        ("/some/local/path", None),
        ("", None),
    ],
)
def test_github_remotes_give_owner_and_repo(url: str, expected: str | None) -> None:
    found = ghparse.github_repo(url)
    assert (None if found is None else found.full) == expected


def test_user_info_is_dropped_from_a_remote_but_the_scp_user_stays() -> None:
    assert (
        ghparse.public_remote_url("https://user:token@github.com/example/repo.git")
        == "https://github.com/example/repo.git"
    )
    assert ghparse.public_remote_url("https://token@github.com/o/r") == "https://github.com/o/r"
    assert ghparse.public_remote_url("git@github.com:o/r.git") == "git@github.com:o/r.git"
    assert ghparse.public_remote_url(" /local/path ") == "/local/path"


def test_the_repository_name_is_the_last_component() -> None:
    assert ghparse.remote_repo_name("git@github.com:example/repo.git") == "repo"
    assert ghparse.remote_repo_name("https://github.com/example/repo/") == "repo"
    assert ghparse.remote_repo_name("/srv/git/thing.git") == "thing"
    assert ghparse.remote_repo_name("") is None


# -- gh pr view -------------------------------------------------------------------------------


def test_the_pr_command_asks_for_the_four_fields_by_branch_and_repository() -> None:
    argv = ghparse.pr_argv("gh", REPO, "feature/x")
    assert argv == (
        "gh",
        "pr",
        "view",
        "feature/x",
        "-R",
        "example/repo",
        "--json",
        "number,state,reviewDecision,statusCheckRollup",
    )


@pytest.mark.parametrize(
    ("name", "number", "state", "decision", "checks"),
    [
        ("pr-open-checks.json", 2542, "OPEN", "", "fail"),
        ("pr-closed-no-checks.json", 1, "CLOSED", "", None),
        ("pr-merged-approved.json", 77, "MERGED", "APPROVED", "pass"),
        ("pr-pending-checks.json", 78, "OPEN", "REVIEW_REQUIRED", "pending"),
        ("pr-failing-checks.json", 79, "OPEN", "CHANGES_REQUESTED", "fail"),
    ],
)
def test_pr_output_fixtures(
    name: str, number: int, state: str, decision: str, checks: str | None
) -> None:
    info = ghparse.parse_pr(fixture_text(name))
    assert (info.number, info.state, info.review_decision, info.checks) == (
        number,
        state,
        decision,
        checks,
    )


def test_a_pr_with_only_skipped_and_neutral_checks_passes() -> None:
    rollup = [
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SKIPPED"},
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "NEUTRAL"},
        {"__typename": "StatusContext", "state": "EXPECTED"},
    ]
    text = json.dumps({"number": 3, "state": "OPEN", "statusCheckRollup": rollup})
    assert ghparse.parse_pr(text).checks == "pending"
    text = json.dumps({"number": 3, "state": "OPEN", "statusCheckRollup": rollup[:2]})
    assert ghparse.parse_pr(text).checks == "pass"
    failing = [{"__typename": "StatusContext", "state": "ERROR"}]
    text = json.dumps({"number": 3, "state": "OPEN", "statusCheckRollup": failing})
    assert ghparse.parse_pr(text).checks == "fail"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        "{}",
        '{"number": 0, "state": "OPEN"}',
        '{"number": true, "state": "OPEN"}',
        '{"number": 1, "state": ""}',
        '{"number": 1, "state": "OPEN", "reviewDecision": 3}',
        '{"number": 1, "state": "OPEN", "statusCheckRollup": "x"}',
        '{"number": 1, "state": "OPEN", "statusCheckRollup": [1]}',
    ],
)
def test_pr_output_that_is_not_a_pr_is_a_parse_error(text: str) -> None:
    with pytest.raises(ghparse.GhParseError) as raised:
        ghparse.parse_pr(text)
    assert raised.value.kind == ghparse.GH_PARSE


def test_a_pr_without_a_rollup_or_decision_parses() -> None:
    info = ghparse.parse_pr('{"number": 4, "state": "OPEN", "reviewDecision": null}')
    assert (info.review_decision, info.checks) == ("", None)


# -- gh api -i --------------------------------------------------------------------------------


def test_the_runs_call_encodes_the_branch_and_sends_the_etag() -> None:
    argv = ghparse.actions_argv("gh", REPO, "feature/x y")
    assert argv == (
        "gh",
        "api",
        "-i",
        "repos/example/repo/actions/runs?branch=feature%2Fx%20y&per_page=30",
    )
    with_tag = ghparse.actions_argv("gh", REPO, "main", 'W/"abc"')
    assert with_tag[-2:] == ("-H", 'If-None-Match: W/"abc"')
    assert ghparse.actions_argv("gh", REPO, "main", "")[-1].startswith("repos/")


def test_a_200_response_has_status_etag_remaining_and_body() -> None:
    response = ghparse.parse_api_response(api_response("api-200.txt").encode())
    assert response.status == 200
    assert response.etag == 'W/"0123456789abcdef0123456789abcdef"'
    assert response.rate_remaining == 4823
    assert json.loads(response.body)["workflow_runs"][0]["id"] == 100


def test_crlf_headers_are_read_like_lf_ones() -> None:
    text = api_response("api-200.txt").replace("\n", "\r\n")
    response = ghparse.parse_api_response(text.encode())
    assert (response.status, response.rate_remaining) == (200, 4823)
    assert json.loads(response.body)["total_count"] == 1


def test_a_304_has_a_status_an_etag_and_no_body() -> None:
    response = ghparse.parse_api_response(api_response("api-304.txt").encode())
    assert (response.status, response.body) == (304, b"")
    assert response.etag == 'W/"0123456789abcdef0123456789abcdef"'
    assert response.rate_remaining == 4800


@pytest.mark.parametrize("raw", [b"", b"gh: something went wrong\n", b"{}"])
def test_output_without_a_status_line_has_no_status(raw: bytes) -> None:
    assert ghparse.parse_api_response(raw).status is None


def test_headers_without_a_colon_and_a_non_numeric_remaining_are_ignored() -> None:
    raw = b"HTTP/1.1 200 OK\nbroken\nX-RateLimit-Remaining: lots\nETag:\n\n{}"
    response = ghparse.parse_api_response(raw)
    assert (response.status, response.etag, response.rate_remaining) == (200, None, None)


@pytest.mark.parametrize(
    ("fixture", "kind"),
    [
        ("api-401.txt", "gh_not_authenticated"),
        ("api-403-ratelimit.txt", "gh_rate_limited"),
        ("api-404.txt", "gh_not_found"),
    ],
)
def test_an_http_error_is_a_short_code(fixture: str, kind: str) -> None:
    response = ghparse.parse_api_response(api_response(fixture).encode())
    assert ghparse.classify_api_failure(response, "") == kind


@pytest.mark.parametrize(
    ("status", "body", "kind"),
    [
        (403, b'{"message": "Resource not accessible"}', "gh_forbidden"),
        (429, b"", "gh_rate_limited"),
        (500, b"", "gh_server_error"),
        (503, b"", "gh_server_error"),
        (418, b"", "gh_failed"),
    ],
)
def test_other_http_statuses(status: int, body: bytes, kind: str) -> None:
    response = ghparse.ApiResponse(status=status, etag=None, rate_remaining=None, body=body)
    assert ghparse.classify_api_failure(response, "") == kind


@pytest.mark.parametrize(
    ("stderr", "kind"),
    [
        (fixture_text("auth-missing.stderr.txt"), "gh_not_authenticated"),
        ("gh: not logged in to any hosts", "gh_not_authenticated"),
        ("gh: HTTP 401: Bad credentials", "gh_not_authenticated"),
        ("gh: API rate limit exceeded", "gh_rate_limited"),
        ("gh: HTTP 429", "gh_rate_limited"),
        (fixture_text("offline.stderr.txt"), "gh_offline"),
        ("dial tcp: lookup api.github.com: no such host", "gh_offline"),
        ("gh: HTTP 404: Not Found", "gh_not_found"),
        ("Could not resolve to a Repository", "gh_not_found"),
        ("gh: HTTP 403", "gh_forbidden"),
        ("something odd", "gh_failed"),
    ],
)
def test_gh_stderr_is_a_short_code(stderr: str, kind: str) -> None:
    assert ghparse.classify_stderr(stderr) == kind
    no_status = ghparse.ApiResponse(status=None, etag=None, rate_remaining=None, body=b"")
    assert ghparse.classify_api_failure(no_status, stderr) == kind


def test_a_branch_without_a_pr_is_recognised_and_is_not_a_failure() -> None:
    assert ghparse.is_no_pull_request(fixture_text("pr-none.stderr.txt"))
    assert not ghparse.is_no_pull_request("gh: HTTP 404")


# -- the engine: pull requests ----------------------------------------------------------------


def _world(**settings: object) -> tuple[FakeClock, FakeProcessRunner, object]:
    clock, runner = FakeClock(), FakeProcessRunner()
    script_git(runner, APP)
    return clock, runner, engine(clock, runner, fs_with(APP), **settings)


def _pr_calls(runner: FakeProcessRunner) -> list[tuple[str, ...]]:
    return [call for call in runner.calls if call[1:3] == ("pr", "view")]


def _api_calls(runner: FakeProcessRunner) -> list[tuple[str, ...]]:
    return [call for call in runner.calls if call[1:2] == ("api",)]


async def test_an_active_project_gets_its_pr_and_a_pull_request_free_branch_is_marked() -> None:
    clock, runner, state = _world()
    script_pr(runner, "main", name="pr-merged-approved.json")
    script_runs(runner, stdout=api_response("api-200.txt"))
    records = await state.poll(projects("app"), LIVE)  # type: ignore[attr-defined]
    project_events, build_events = events(records)
    # the git record first, then the same project again with its PR
    assert [e.payload.pr_number for e in project_events] == [None, 77]
    last = project_events[-1].payload
    assert (last.pr_state, last.review_decision) == ("MERGED", "APPROVED")
    assert len(build_events) == 1
    assert len(_pr_calls(runner)) == 1


async def test_a_branch_with_no_pr_stores_the_none_marker() -> None:
    clock, runner, state = _world()
    runner.script(
        *ghparse.pr_argv("gh", REPO, "main"),
        returncode=1,
        stderr='no pull requests found for branch "main"',
    )
    script_runs(runner, stdout=api_response("api-200.txt"))
    records = await state.poll(projects("app"), LIVE)  # type: ignore[attr-defined]
    last = events(records)[0][-1].payload
    assert (last.pr_number, last.pr_state, last.review_decision) == (0, "none", "")


async def test_an_inactive_project_makes_no_gh_call() -> None:
    clock, runner, state = _world()
    records = await state.poll(projects("app"), activity())  # type: ignore[attr-defined]
    assert len(events(records)[0]) == 1  # git state only
    assert _pr_calls(runner) == []
    assert _api_calls(runner) == []


async def test_a_project_without_a_github_remote_or_on_a_detached_head_makes_no_gh_call() -> None:
    clock, runner = FakeClock(), FakeProcessRunner()
    script_git(runner, APP, remote="https://gitlab.com/example/repo.git")
    state = engine(clock, runner, fs_with(APP))
    await state.poll(projects("app"), LIVE)
    assert _pr_calls(runner) == []
    assert _api_calls(runner) == []
    clock, runner = FakeClock(), FakeProcessRunner()
    script_git(runner, APP, status="status-detached.txt")
    state = engine(clock, runner, fs_with(APP))
    await state.poll(projects("app"), LIVE)
    assert _pr_calls(runner) == []
    assert _api_calls(runner) == []


async def test_gh_not_authenticated_makes_the_build_unavailable_with_an_error_kind() -> None:
    clock, runner, state = _world()
    runner.script("gh", returncode=4, stderr=fixture_text("auth-missing.stderr.txt"))
    records = await state.poll(projects("app"), LIVE)  # type: ignore[attr-defined]
    _, build_events = events(records)
    assert [(b.payload.state, b.payload.error_kind) for b in build_events] == [
        ("none", "gh_not_authenticated")
    ]
    assert state.gh_issue == "gh_not_authenticated"  # type: ignore[attr-defined]
    # blocked: no more calls for a while, and the build stays unavailable
    before = len(runner.calls)
    await clock.advance(301)
    records = await state.poll(projects("app"), LIVE)  # type: ignore[attr-defined]
    assert len(runner.calls) == before + 3  # git again, no gh
    assert events(records)[1][0].payload.error_kind == "gh_not_authenticated"
    await clock.advance(1000)  # the block is over: gh is tried again
    runner._scripts.clear()
    script_git(runner, APP)
    script_pr(runner)
    script_runs(runner, stdout=api_response("api-200.txt"))
    records = await state.poll(projects("app"), LIVE)  # type: ignore[attr-defined]
    assert events(records)[1][-1].payload.error_kind is None
    assert state.gh_issue is None  # type: ignore[attr-defined]


async def test_gh_missing_blocks_gh_and_says_so() -> None:
    clock, runner, state = _world()
    records = await state.poll(projects("app"), LIVE)  # type: ignore[attr-defined]
    assert events(records)[1][0].payload.error_kind == "gh_missing"
    assert state.gh_issue == "gh_missing"  # type: ignore[attr-defined]


async def test_a_failing_pr_call_backs_off_exponentially_and_keeps_the_last_pr() -> None:
    clock, runner, state = _world()
    script_pr(runner)
    script_runs(runner, stdout=api_response("api-200.txt"))
    await state.poll(projects("app"), LIVE)  # type: ignore[attr-defined]
    runner._scripts = [s for s in runner._scripts if s.prefix[:2] != ("gh", "pr")]
    runner.script(*ghparse.pr_argv("gh", REPO, "main"), returncode=1, stderr="boom")
    calls: list[float] = []
    for _ in range(40):
        await clock.advance(60)
        before = len(_pr_calls(runner))
        records = await state.poll(projects("app"), LIVE)  # type: ignore[attr-defined]
        if len(_pr_calls(runner)) > before:
            calls.append(clock.monotonic())
        assert not any(  # the stored PR is neither cleared nor replaced by a failure
            e.payload.pr_number == 0 for e in events(records)[0]
        )
    gaps = [b - a for a, b in zip(calls, calls[1:], strict=False)]
    assert gaps[:3] == [300.0, 600.0, 1200.0]  # 300 s, doubled after each failure


async def test_pr_failures_are_short_codes() -> None:
    for script, state_kind in (
        ({"timeout": True}, "gh_timeout"),
        ({"returncode": 0, "stdout": "garbage"}, "gh_parse"),
        ({"returncode": 1, "stderr": "weird"}, "gh_failed"),
    ):
        clock, runner, state = _world()
        runner.script("gh", "pr", **script)  # type: ignore[arg-type]
        script_runs(runner, stdout=api_response("api-200.txt"))
        records = await state.poll(projects("app"), LIVE)  # type: ignore[attr-defined]
        assert [e.payload.pr_number for e in events(records)[0]] == [None], state_kind
    # an OS-level failure to start gh
    clock, runner, state = _world()
    runner.script("gh", "pr", error=PermissionError("denied"))
    script_runs(runner, stdout=api_response("api-200.txt"))
    assert [e.payload.pr_number for e in events(await state.poll(projects("app"), LIVE))[0]] == [  # type: ignore[attr-defined]
        None
    ]


async def test_a_branch_change_clears_the_old_branchs_pr_until_the_new_one_is_known() -> None:
    clock, runner, state = _world()
    script_pr(runner, "main", name="pr-merged-approved.json")
    script_runs(runner, stdout=api_response("api-200.txt"))
    await state.poll(projects("app"), LIVE)  # type: ignore[attr-defined]
    runner._scripts.clear()
    script_git(runner, APP, status="status-no-upstream.txt")  # now on branch "topic"
    runner.script("gh", returncode=1, stderr='no pull requests found for branch "topic"')
    await clock.advance(301)
    records = await state.poll(projects("app"), LIVE)  # type: ignore[attr-defined]
    payloads = [e.payload for e in events(records)[0]]
    assert payloads[-1].branch == "topic"
    assert (payloads[-1].pr_number, payloads[-1].pr_state) == (0, "none")
    assert ("gh", "pr", "view", "topic") == _pr_calls(runner)[-1][:4]
