"""Helpers for the project-state tests: a store, scripted ``git`` and ``gh``, throwaway repos.

The fakes come from :mod:`fylgja.testing.fakes`; nothing here needs ``gh``, a network or a daemon.
The real ``git`` is used only where a test builds a throwaway repository under its own directory
(``Repo``), always in a subprocess with an explicit ``cwd`` and an isolated environment.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

from fylgja.collectors.base import CollectorContext
from fylgja.collectors.context import build_context
from fylgja.collectors.project import ghparse, gitstate
from fylgja.collectors.project.adapter import KnownProject, ProjectActivity
from fylgja.collectors.project.state import ProjectStateEngine, StateSettings
from fylgja.collectors.seams import ProcessResult
from fylgja.models import BuildEvent, ProjectEvent, Record
from fylgja.store import db, queries
from fylgja.testing.fakes import FakeClock, FakeFS, FakeProcessRunner, RecordingSink

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "project"
GH = FIXTURES / "gh"
GIT = FIXTURES / "git"
REPO = ghparse.GitHubRepo("example", "repo")
REMOTE = "https://github.com/example/repo.git"


def fixture_text(name: str) -> str:
    return (GH / name).read_text(encoding="utf-8")


def git_fixture(name: str) -> str:
    return (GIT / name).read_text(encoding="utf-8")


def store_with(path: Path, projects: Sequence[tuple[str, str]], at: str) -> Path:
    """A store whose ``projects`` lists ``(project path, repo root)`` pairs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = db.open_store(path)
    try:
        connection.execute("BEGIN")
        for project, root in projects:
            queries.upsert_project(connection, path=project, values={"repo_root": root}, at=at)
        connection.execute("COMMIT")
    finally:
        connection.close()
    return path


def script_git(
    runner: FakeProcessRunner,
    root: str,
    *,
    status: str = "status-clean.txt",
    commit_time: str = "1790000000\n",
    remote: str | None = REMOTE,
) -> None:
    """Script the three git commands the engine runs for ``root``."""
    runner.script(*gitstate.status_argv(root), stdout=git_fixture(status))
    runner.script(*gitstate.commit_time_argv(root), stdout=commit_time)
    if remote is None:
        runner.script(*gitstate.remote_argv(root), returncode=1)
    else:
        runner.script(*gitstate.remote_argv(root), stdout=remote + "\n")


def script_pr(
    runner: FakeProcessRunner, branch: str = "main", *, name: str = "pr-open-checks.json"
) -> None:
    runner.script(*ghparse.pr_argv("gh", REPO, branch), stdout=fixture_text(name))


def script_runs(
    runner: FakeProcessRunner, branch: str = "main", *, etag: str | None = None, **kwargs: object
) -> None:
    runner.script(*ghparse.actions_argv("gh", REPO, branch, etag), **kwargs)  # type: ignore[arg-type]


def api_response(name: str = "api-200.txt") -> str:
    return fixture_text(name)


def activity(
    *,
    live: Sequence[str] = (),
    focused: str | None = None,
    clock: FakeClock | None = None,
    recent: Sequence[str] = (),
) -> ProjectActivity:
    now = clock.now() if clock is not None else datetime(2026, 1, 1).astimezone()
    last = {path: now - timedelta(minutes=5) for path in recent}
    if focused is not None:
        last[focused] = now
    return ProjectActivity(live=frozenset(live), last_focus=last, focused=focused)


def engine(
    clock: FakeClock,
    runner: FakeProcessRunner,
    fs: FakeFS,
    **settings: object,
) -> ProjectStateEngine:
    return ProjectStateEngine(
        StateSettings.model_validate(settings), clock=clock, runner=runner, fs=fs
    )


def projects(*names: str, base: str = "/code") -> list[KnownProject]:
    return [KnownProject(path=f"{base}/{n}", root=f"{base}/{n}") for n in names]


def fs_with(*roots: str) -> FakeFS:
    fs = FakeFS()
    for root in roots:
        fs.add_dir(root)
    return fs


def events(records: Sequence[Record]) -> tuple[list[ProjectEvent], list[BuildEvent]]:
    return (
        [r for r in records if isinstance(r, ProjectEvent)],
        [r for r in records if isinstance(r, BuildEvent)],
    )


def context(clock: FakeClock, sink: RecordingSink | None = None) -> CollectorContext:
    return build_context(
        "project",
        RecordingSink() if sink is None else sink,
        clock=clock,
        logger=logging.getLogger("test.project"),
    )


# -- throwaway repositories -----------------------------------------------------------------


def _env(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_TERMINAL_PROMPT": "0",
    }


class Repo:
    """A throwaway repository under ``base``, driven through ``git`` subprocesses."""

    def __init__(self, path: Path, home: Path) -> None:
        self.path = path
        self._home = home

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.path,
            env=_env(self._home),
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        return result.stdout

    def commit(self, name: str, text: str = "x\n", *, when: str | None = None) -> None:
        (self.path / name).write_text(text, encoding="utf-8")
        self.git("add", name)
        if when is None:
            self.git("commit", "-m", f"add {name}")
        else:
            env = {**_env(self._home), "GIT_COMMITTER_DATE": when, "GIT_AUTHOR_DATE": when}
            subprocess.run(
                ["git", "commit", "-m", f"add {name}"],
                cwd=self.path,
                env=env,
                capture_output=True,
                check=True,
                timeout=60,
            )


def init_repo(base: Path, name: str, *, branch: str = "main") -> Repo:
    path = base / name
    path.mkdir(parents=True)
    repo = Repo(path, base / "home")
    (base / "home").mkdir(exist_ok=True)
    repo.git("init", "-q", "-b", branch)
    return repo


def clone(base: Path, origin: Repo, name: str) -> Repo:
    path = base / name
    result = subprocess.run(
        ["git", "clone", "-q", str(origin.path), str(path)],
        cwd=base,
        env=_env(base / "home"),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert result.returncode == 0
    return Repo(path, base / "home")


def store_with_activity(
    path: Path,
    now: datetime,
    *,
    projects: Sequence[str],
    live: Sequence[str] = (),
    spans: Sequence[tuple[str, float, float | None]] = (),
) -> Path:
    """A store with known projects, live sessions and focus spans.

    A span is ``(project, started seconds ago, ended seconds ago or None for an open span)``.
    """
    store_with(path, [(p, p) for p in projects], db.to_db_time(now))
    connection = db.open_store(path)
    try:
        connection.execute("BEGIN")
        for index, project in enumerate(live):
            key = f"claude:s{index}"
            queries.upsert_session(
                connection,
                session_key=key,
                kind="claude",
                native_id=f"s{index}",
                source="claude",
                values={},
                first_seen=db.to_db_time(now),
                at=db.to_db_time(now),
                listed=True,
            )
            queries.set_session_placement(
                connection,
                session_key=key,
                project_path=project,
                host_kind=None,
                host_ref=None,
                window_id=None,
                workspace=None,
            )
        for index, (project, started, ended) in enumerate(spans):
            begin = db.to_db_time(now - timedelta(seconds=started))
            queries.insert_open_span(
                connection,
                window_id=f"w{index}",
                workspace=None,
                app_bundle_id=None,
                title=None,
                project_path=project,
                session_key=None,
                started_at=begin,
            )
            if ended is not None:
                queries.close_open_span(
                    connection,
                    window_id=f"w{index}",
                    started_at=begin,
                    ended_at=db.to_db_time(now - timedelta(seconds=ended)),
                    end_reason="focus",
                    project_path=None,
                    session_key=None,
                )
        connection.execute("COMMIT")
    finally:
        connection.close()
    return path


class TimedRunner(FakeProcessRunner):
    """A fake runner that notes the monotonic time of every call."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__()
        self._clock = clock
        self.times: list[tuple[float, tuple[str, ...]]] = []

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - mirrors subprocess timeouts
        stdin: bytes | None = None,
    ) -> ProcessResult:
        self.times.append((self._clock.monotonic(), tuple(argv)))
        return await super().run(argv, timeout=timeout, stdin=stdin)

    def calls_of(self, *prefix: str) -> list[float]:
        """Times of the ``gh`` calls whose argv, after ``gh``, starts with ``prefix``."""
        return [
            t for t, argv in self.times if argv[0] == "gh" and argv[1 : 1 + len(prefix)] == prefix
        ]

    def git_calls(self, subcommand: str) -> list[float]:
        """Times of the ``git`` calls of one subcommand (``status``, ``log``, ``config``)."""
        return [t for t, argv in self.times if argv[0] == "git" and argv[4] == subcommand]


def step(clock: FakeClock, seconds: float) -> None:
    """Move a clock that nothing sleeps on forward without ``advance``'s event-loop settling.

    The engine is driven by explicit ``poll`` calls, so a simulated day of ticks need not yield to
    the event loop a hundred times per tick.
    """
    clock._mono += seconds
