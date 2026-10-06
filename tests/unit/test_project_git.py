"""The git state of a project (WP5.4): parsing ``git status --porcelain=v2``, the command lines, and
the engine against throwaway repositories created by ``git`` subprocesses under ``tmp_path``."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from project_support import (
    Repo,
    activity,
    clone,
    engine,
    events,
    fs_with,
    git_fixture,
    init_repo,
    projects,
    script_git,
)

from fylgja.collectors.project import adapter, gitstate
from fylgja.collectors.project.state import ProjectStateEngine, StateSettings
from fylgja.models import ProjectPayload
from fylgja.testing.fakes import FakeClock, FakeFS, FakeProcessRunner

# -- parsing ----------------------------------------------------------------------------------


def test_a_clean_branch_in_sync_with_its_upstream() -> None:
    status = gitstate.parse_status(git_fixture("status-clean.txt"))
    assert status == gitstate.GitStatus(
        oid="1" * 40, branch="main", upstream="origin/main", ahead=0, behind=0, dirty_count=0
    )
    assert not status.dirty


def test_changed_staged_renamed_conflicted_and_untracked_paths_all_count() -> None:
    status = gitstate.parse_status(git_fixture("status-dirty-ahead-behind.txt"))
    assert (status.branch, status.upstream) == ("feature/x", "origin/feature/x")
    assert (status.ahead, status.behind) == (2, 1)
    assert status.dirty_count == 6  # two changes, a rename, a conflict and two untracked
    assert status.dirty


def test_a_branch_without_an_upstream_has_no_ahead_or_behind() -> None:
    status = gitstate.parse_status(git_fixture("status-no-upstream.txt"))
    assert (status.upstream, status.ahead, status.behind) == (None, None, None)
    assert status.dirty_count == 1


def test_a_detached_head_has_no_branch_and_a_new_repository_has_no_commit() -> None:
    detached = gitstate.parse_status(git_fixture("status-detached.txt"))
    assert (detached.branch, detached.oid) == (None, "4" * 40)
    initial = gitstate.parse_status(git_fixture("status-initial.txt"))
    assert (initial.branch, initial.oid, initial.dirty_count) == ("main", None, 1)


def test_ignored_files_are_not_dirt() -> None:
    assert gitstate.parse_status(git_fixture("status-ignored.txt")).dirty_count == 0


@pytest.mark.parametrize(
    "text",
    [
        "",
        "# branch.oid abc\n",
        "garbage\n",
        "# branch.head main\n# branch.ab +1\n",
        "# branch.head main\n# branch.ab 1 2\n",
        "# branch.head main\n# branch.ab +x -1\n",
        "# branch.head main\n# branch.ab +1 -x\n",
        "# branch.head main\n# branch.ab +1 --1\n",
    ],
)
def test_output_that_is_not_porcelain_v2_is_a_parse_error(text: str) -> None:
    with pytest.raises(gitstate.GitParseError) as raised:
        gitstate.parse_status(text)
    assert raised.value.kind == gitstate.GIT_PARSE


def test_the_last_commit_time_is_epoch_seconds_in_utc() -> None:
    assert gitstate.parse_commit_time("1790000000\n") == datetime(
        2026, 9, 21, 14, 13, 20, tzinfo=UTC
    )
    assert gitstate.parse_commit_time("") is None
    assert gitstate.parse_commit_time(" \n") is None
    with pytest.raises(gitstate.GitParseError):
        gitstate.parse_commit_time("yesterday")
    with pytest.raises(gitstate.GitParseError):
        gitstate.parse_commit_time("99999999999999999999")


def test_every_command_is_git_with_no_optional_locks_in_the_project_root() -> None:
    for argv in (
        gitstate.status_argv("/code/app"),
        gitstate.commit_time_argv("/code/app"),
        gitstate.remote_argv("/code/app"),
    ):
        assert argv[:4] == ("git", "--no-optional-locks", "-C", "/code/app")
    assert gitstate.status_argv("/r", "/opt/bin/git")[0] == "/opt/bin/git"
    assert "--porcelain=v2" in gitstate.status_argv("/r")


def test_failures_are_short_codes() -> None:
    assert gitstate.classify_failure(git_fixture("not-a-repository.stderr.txt")) == (
        gitstate.GIT_NOT_A_REPOSITORY
    )
    assert gitstate.classify_failure("fatal: something else") == gitstate.GIT_FAILED


# -- the engine with scripted git ------------------------------------------------------------


def _scripted(roots: list[str]) -> tuple[FakeClock, FakeProcessRunner, FakeFS]:
    return FakeClock(), FakeProcessRunner(), fs_with(*roots)


def _payload(records: list, index: int = 0) -> ProjectPayload:  # type: ignore[type-arg]
    return events(records)[0][index].payload


async def test_the_state_of_a_project_becomes_a_project_event() -> None:
    clock, runner, fs = _scripted(["/code/app"])
    script_git(runner, "/code/app", status="status-dirty-ahead-behind.txt")
    state = engine(clock, runner, fs)
    records = await state.poll(projects("app"), activity())
    payload = _payload(records)
    assert (payload.path, payload.repo_root) == ("/code/app", "/code/app")
    assert (payload.branch, payload.dirty, payload.ahead, payload.behind) == (
        "feature/x",
        True,
        2,
        1,
    )
    assert payload.last_commit_at == datetime.fromtimestamp(1790000000, tz=UTC)
    assert payload.remote_url == "https://github.com/example/repo.git"
    assert payload.repo_name == "repo"
    assert events(records)[0][0].source == "project"
    assert events(records)[0][0].entity_id == "/code/app"
    assert ("git", "--no-optional-locks", "-C", "/code/app", "status") == runner.calls[0][:5]


async def test_a_credential_in_the_remote_never_reaches_the_record() -> None:
    clock, runner, fs = _scripted(["/code/app"])
    script_git(runner, "/code/app", remote="https://user:s3cret@github.com/example/repo.git")
    records = await engine(clock, runner, fs).poll(projects("app"), activity())
    assert _payload(records).remote_url == "https://github.com/example/repo.git"
    assert "s3cret" not in repr(records)


async def test_no_remote_means_no_remote_url_and_the_directory_names_the_repository() -> None:
    clock, runner, fs = _scripted(["/code/app"])
    script_git(runner, "/code/app", remote=None)
    payload = _payload(await engine(clock, runner, fs).poll(projects("app"), activity()))
    assert payload.remote_url is None
    assert payload.repo_name == "app"


async def test_the_draupnir_group_comes_from_the_parent_directory() -> None:
    clock, runner, fs = _scripted(["/code/shop/main"])
    fs.add_dir("/code/shop/main/.git")
    fs.add_file("/code/shop/draupnir.toml", "")
    script_git(runner, "/code/shop/main")
    known = [adapter.KnownProject(path="/code/shop/main", root="/code/shop/main")]
    payload = _payload(await engine(clock, runner, fs).poll(known, activity()))
    assert payload.group_name == "shop"


async def test_git_runs_on_the_slow_timer_and_an_unchanged_state_is_not_emitted_again() -> None:
    clock, runner, fs = _scripted(["/code/app"])
    script_git(runner, "/code/app")
    state = engine(clock, runner, fs)
    assert len(await state.poll(projects("app"), activity())) == 1
    assert len(runner.calls) == 3
    await clock.advance(299)
    assert await state.poll(projects("app"), activity()) == []
    assert len(runner.calls) == 3  # not due yet
    await clock.advance(2)
    assert await state.poll(projects("app"), activity()) == []  # looked again, nothing new
    assert len(runner.calls) == 6


async def test_a_change_is_emitted_when_it_is_seen() -> None:
    clock, runner, fs = _scripted(["/code/app"])
    script_git(runner, "/code/app", status="status-clean.txt", remote=None)
    state = engine(clock, runner, fs)
    await state.poll(projects("app"), activity())
    script_git(runner, "/code/app", status="status-dirty-ahead-behind.txt", remote=None)
    runner._scripts.clear()  # the longest prefix wins; start the second script over
    script_git(runner, "/code/app", status="status-dirty-ahead-behind.txt", remote=None)
    await clock.advance(301)
    records = await state.poll(projects("app"), activity())
    assert _payload(records).dirty is True


async def test_focus_moving_into_a_project_looks_again_after_the_refresh_gap() -> None:
    clock, runner, fs = _scripted(["/code/app", "/code/other"])
    script_git(runner, "/code/app", remote=None)  # no GitHub remote: no gh calls to count
    script_git(runner, "/code/other", remote=None)
    state = engine(clock, runner, fs, focus_refresh_seconds=30)
    both = projects("app", "other")
    await state.poll(both, activity())
    assert len(runner.calls) == 6
    await clock.advance(10)
    await state.poll(both, activity(focused="/code/app"))
    assert len(runner.calls) == 6  # within the gap: not yet
    await clock.advance(25)
    await state.poll(both, activity(focused="/code/app"))
    assert len(runner.calls) == 9  # only the focused project, 35 s after the first look
    await clock.advance(5)
    await state.poll(both, activity(focused="/code/app"))
    assert len(runner.calls) == 9  # focus staying put is not a change


@pytest.mark.parametrize(
    ("script", "kind"),
    [
        ({"error": FileNotFoundError("git")}, "git_missing"),
        ({"timeout": True}, "git_timeout"),
        ({"error": PermissionError("denied")}, "git_failed"),
        ({"returncode": 128, "stderr": "fatal: not a git repository"}, "git_not_a_repository"),
        ({"returncode": 1, "stderr": "boom"}, "git_failed"),
        ({"stdout": "garbage"}, "git_parse"),
    ],
)
async def test_a_failing_git_is_recorded_as_a_short_code_and_emits_nothing(
    script: dict[str, object], kind: str
) -> None:
    clock, runner, fs = _scripted(["/code/app"])
    runner.script("git", **script)  # type: ignore[arg-type]
    state = engine(clock, runner, fs)
    assert await state.poll(projects("app"), activity()) == []
    assert state.git_failures == {"/code/app": kind}
    # it recovers on the next look
    runner._scripts.clear()
    script_git(runner, "/code/app")
    await clock.advance(301)
    assert len(await state.poll(projects("app"), activity())) == 1
    assert state.git_failures == {}


async def test_a_bad_commit_time_is_a_parse_failure_and_a_new_repository_has_none() -> None:
    clock, runner, fs = _scripted(["/code/app"])
    script_git(runner, "/code/app", commit_time="soon\n")
    state = engine(clock, runner, fs)
    assert await state.poll(projects("app"), activity()) == []
    assert state.git_failures == {"/code/app": "git_parse"}
    runner._scripts.clear()
    runner.script(*gitstate.status_argv("/code/app"), stdout=git_fixture("status-initial.txt"))
    runner.script(*gitstate.commit_time_argv("/code/app"), returncode=128, stderr="no commits")
    runner.script(*gitstate.remote_argv("/code/app"), returncode=1)
    await clock.advance(301)
    payload = _payload(await state.poll(projects("app"), activity()))
    assert (payload.last_commit_at, payload.branch) == (None, "main")


async def test_a_directory_that_is_gone_is_skipped_and_forgotten_projects_leave_no_memory() -> None:
    clock, runner, fs = _scripted([])
    state = engine(clock, runner, fs)
    assert await state.poll(projects("gone"), activity()) == []
    assert runner.calls == []
    assert state.git_failures == {}


# -- the engine against throwaway repositories ----------------------------------------------


def _state(tmp_path: Path) -> tuple[ProjectStateEngine, FakeClock]:
    clock = FakeClock()
    state = ProjectStateEngine(
        StateSettings(),
        clock=clock,
        runner=adapter.default_runner(),
        fs=adapter.default_fs(),
    )
    return state, clock


async def _payload_of(state: ProjectStateEngine, repo: Repo) -> ProjectPayload:
    known = [adapter.KnownProject(path=str(repo.path), root=str(repo.path))]
    records = await state.poll(known, activity())
    return _payload(records)


async def test_a_clean_repository_with_an_upstream(tmp_path: Path) -> None:
    origin = init_repo(tmp_path, "origin")
    origin.commit("a.txt", when="2026-03-01T10:00:00+0000")
    work = clone(tmp_path, origin, "work")
    state, _ = _state(tmp_path)
    payload = await _payload_of(state, work)
    assert (payload.branch, payload.dirty, payload.ahead, payload.behind) == ("main", False, 0, 0)
    assert payload.last_commit_at == datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
    assert payload.remote_url == str(origin.path)
    assert payload.repo_name == "origin"


async def test_a_dirty_repository_counts_changes_and_untracked_files(tmp_path: Path) -> None:
    origin = init_repo(tmp_path, "origin")
    origin.commit("a.txt")
    work = clone(tmp_path, origin, "work")
    (work.path / "a.txt").write_text("changed\n", encoding="utf-8")
    (work.path / "new.txt").write_text("new\n", encoding="utf-8")
    state, _ = _state(tmp_path)
    payload = await _payload_of(state, work)
    assert payload.dirty is True


async def test_a_repository_ahead_of_its_upstream(tmp_path: Path) -> None:
    origin = init_repo(tmp_path, "origin")
    origin.commit("a.txt")
    work = clone(tmp_path, origin, "work")
    work.commit("b.txt")
    work.commit("c.txt")
    state, _ = _state(tmp_path)
    payload = await _payload_of(state, work)
    assert (payload.ahead, payload.behind, payload.dirty) == (2, 0, False)


async def test_a_repository_behind_its_upstream(tmp_path: Path) -> None:
    origin = init_repo(tmp_path, "origin")
    origin.commit("a.txt")
    work = clone(tmp_path, origin, "work")
    origin.commit("b.txt")
    work.git("fetch", "-q")
    state, _ = _state(tmp_path)
    payload = await _payload_of(state, work)
    assert (payload.ahead, payload.behind) == (0, 1)


async def test_a_repository_without_an_upstream_or_a_remote(tmp_path: Path) -> None:
    solo = init_repo(tmp_path, "solo", branch="topic")
    solo.commit("a.txt")
    state, _ = _state(tmp_path)
    payload = await _payload_of(state, solo)
    assert (payload.branch, payload.ahead, payload.behind) == ("topic", None, None)
    assert payload.remote_url is None
    assert payload.repo_name == "solo"


async def test_looking_at_a_repository_takes_no_lock_and_changes_nothing(tmp_path: Path) -> None:
    solo = init_repo(tmp_path, "solo")
    solo.commit("a.txt")
    (solo.path / "a.txt").write_text("edited\n", encoding="utf-8")
    index = solo.path / ".git" / "index"
    before = index.stat().st_mtime_ns
    state, _ = _state(tmp_path)
    await _payload_of(state, solo)
    assert index.stat().st_mtime_ns == before
    assert not (solo.path / ".git" / "index.lock").exists()


async def test_a_directory_that_is_not_a_repository_is_a_failure(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    state, _ = _state(tmp_path)
    known = [adapter.KnownProject(path=str(plain), root=str(plain))]
    assert await state.poll(known, activity()) == []
    assert state.git_failures == {str(plain): "git_not_a_repository"}
