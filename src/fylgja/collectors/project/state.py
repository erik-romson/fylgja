"""The project state engine: git, pull request and build per known project (WP5.4, §4.7).

The engine is driven by the collector: each tick it gets the known projects and which of them
are active, runs what is due through the process runner and returns the records to emit. It
keeps its memory per project path, owns the call budgets and never touches the store.

**What runs when** (``poll``):

* **git** (``git --no-optional-locks``: status, last commit, remote) for *every* known project
  on the slow timer (``state_interval_seconds``, 5 min), and sooner (after at least
  ``focus_refresh_seconds``) when focus moves into the project. A project directory that is
  gone is skipped.
* **gh** only for an **active** project (a live session, or focus within ``active_window_seconds``,
  an hour) whose ``origin`` is on github.com and whose HEAD is on a branch:
  * ``gh pr view <branch> -R o/r --json number,state,reviewDecision,statusCheckRollup``, on the
    same timer, with a calls-per-hour budget of its own (``gh_pr_calls_per_hour``) and an
    exponential backoff after a failure;
  * ``gh api -i repos/o/r/actions/runs?branch=…&per_page=30`` with ``If-None-Match`` (a 304
    reuses the last fold), on the same timer and sooner when the branch tip moved or focus
    arrived, and under the calls-per-hour budget ``gh_calls_per_hour`` (default 30). **Every**
    call counts against it, a 304 too: the budget is a ceiling on how often the process is
    started, and it is a rolling window, so no sixty minutes ever hold more calls than the limit.
    A call that the budget refuses is not made and the section simply ages (``stale``).
* **gh is blocked** for a while when it is missing, not authenticated or rate-limited (the
  response says fewer than 100 calls remain): no call is made, and each active project's build
  section is emitted as ``unavailable`` with the reason as ``error_kind``.

Failures are short codes only. Nothing about a path, a branch, a remote or a response is logged.
"""

from __future__ import annotations

import posixpath
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from fylgja.collectors.base import Clock
from fylgja.collectors.project import builds, ghparse, gitstate
from fylgja.collectors.project.adapter import KnownProject, ProjectActivity
from fylgja.collectors.seams import FileSystem, ProcessResult, ProcessRunner
from fylgja.core.project_roots import ProjectLocator
from fylgja.models import BuildEvent, ProjectEvent, ProjectPayload, Record

__all__ = [
    "CallBudget",
    "ProjectStateEngine",
    "StateSettings",
]

SOURCE: Final = "project"

BUDGET_WINDOW_SECONDS: Final = 3600.0
FAILURE_BACKOFF_SECONDS: Final = 300.0
MAX_BACKOFF_SECONDS: Final = 3600.0
AUTH_BLOCK_SECONDS: Final = 900.0
RATE_BLOCK_SECONDS: Final = 3600.0
LOW_RATE_REMAINING: Final = 100
NO_PR_STATE: Final = "none"


class StateSettings(BaseModel):
    """The project-state part of ``[collectors.project]``."""

    model_config = ConfigDict(extra="forbid")

    state_interval_seconds: float = Field(default=300.0, gt=0)
    """The slow timer: git, PR and build of a project are looked at this often."""
    focus_refresh_seconds: float = Field(default=30.0, ge=0)
    """The shortest gap between two looks at a project that focus moved into."""
    active_window_seconds: float = Field(default=3600.0, gt=0)
    """A project is active for this long after a live session or focus."""
    gh_calls_per_hour: int = Field(default=30, ge=0)
    """The budget of the actions-runs call, in any sixty minutes."""
    gh_pr_calls_per_hour: int = Field(default=120, ge=0)
    """The budget of ``gh pr view``, in any sixty minutes."""
    command_timeout_seconds: float = Field(default=20.0, gt=0)
    git_command: str = "git"
    gh_command: str = "gh"


class CallBudget:
    """At most ``limit`` calls in any ``window`` seconds (rolling, on the monotonic clock)."""

    def __init__(self, limit: int, window: float = BUDGET_WINDOW_SECONDS) -> None:
        self._limit = limit
        self._window = window
        self._calls: deque[float] = deque()

    def _expire(self, now: float) -> None:
        while self._calls and now - self._calls[0] >= self._window:
            self._calls.popleft()

    def used(self, now: float) -> int:
        self._expire(now)
        return len(self._calls)

    def try_acquire(self, now: float) -> bool:
        """Count one call and return ``True``, or return ``False`` when the budget is spent."""
        self._expire(now)
        if len(self._calls) >= self._limit:
            return False
        self._calls.append(now)
        return True


class _CommandFailed(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True)
class _Kinds:
    """The failure codes of one command line tool."""

    missing: str
    timeout: str
    failed: str


GIT_KINDS: Final = _Kinds(gitstate.GIT_MISSING, gitstate.GIT_TIMEOUT, gitstate.GIT_FAILED)
GH_KINDS: Final = _Kinds(ghparse.GH_MISSING, ghparse.GH_TIMEOUT, ghparse.GH_FAILED)


class _NoPullRequest:
    """The branch was looked up and has no pull request."""


NO_PR: Final = _NoPullRequest()


@dataclass
class _Memory:
    """What the engine knows about one project path. Times are on the monotonic clock."""

    # git
    git_next: float = 0.0
    git_checked: float | None = None
    git_failure: str | None = None
    status: gitstate.GitStatus | None = None
    remote_url: str | None = None
    repo: ghparse.GitHubRepo | None = None
    repo_name: str | None = None
    commit_time: datetime | None = None
    group: str | None = None
    emitted: ProjectPayload | None = None
    # pull request
    pr: ghparse.PullRequestInfo | _NoPullRequest | None = None
    pr_next: float = 0.0
    pr_checked: float | None = None
    pr_failures: int = 0
    # build
    build_key: tuple[str, str] | None = None
    etag: str | None = None
    fold: builds.BuildFold | None = None
    build_next: float = 0.0
    build_checked: float | None = None
    build_failures: int = 0


def _backoff(failures: int) -> float:
    return min(FAILURE_BACKOFF_SECONDS * (1 << (failures - 1)), MAX_BACKOFF_SECONDS)


class ProjectStateEngine:
    def __init__(
        self,
        settings: StateSettings,
        *,
        clock: Clock,
        runner: ProcessRunner,
        fs: FileSystem,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._runner = runner
        self._fs = fs
        self._locator = ProjectLocator(fs)
        self._memory: dict[str, _Memory] = {}
        self._focused: str | None = None
        self._runs_budget = CallBudget(settings.gh_calls_per_hour)
        self._pr_budget = CallBudget(settings.gh_pr_calls_per_hour)
        self._blocked_until = 0.0
        self._block_kind: str | None = None

    # -- what the collector reports ---------------------------------------------------------

    @property
    def git_failures(self) -> dict[str, str]:
        """The failure code of every project whose last git look failed, by project path."""
        return {p: m.git_failure for p, m in self._memory.items() if m.git_failure}

    @property
    def gh_issue(self) -> str | None:
        """Why gh is blocked right now (a failure code), else ``None``."""
        if self._block_kind is not None and self._clock.monotonic() < self._blocked_until:
            return self._block_kind
        return None

    # -- one tick ---------------------------------------------------------------------------

    async def poll(
        self, projects: Sequence[KnownProject], activity: ProjectActivity
    ) -> list[Record]:
        """Run what is due and return the records to emit, most active projects first."""
        records: list[Record] = []
        now = self._clock.now()
        mono = self._clock.monotonic()
        known = {project.path for project in projects}
        for path in [p for p in self._memory if p not in known]:
            del self._memory[path]  # a project the store forgot is forgotten here too
        focused = activity.focused
        if focused is not None and focused != self._focused and focused in known:
            self._expedite(self._memory.setdefault(focused, _Memory()), mono)
        self._focused = focused

        def order(project: KnownProject) -> tuple[int, float, str]:
            seen = activity.recency(project.path)
            rank = 0 if project.path == focused else 1 if project.path in activity.live else 2
            return (rank, 0.0 if seen is None else -seen.timestamp(), project.path)

        for project in sorted(projects, key=order):
            memory = self._memory.setdefault(project.path, _Memory())
            if not self._fs.exists(project.root):
                memory.git_failure = None  # a directory that is gone is not a failure
                continue
            if mono >= memory.git_next:
                records.extend(await self._git(project, memory, now, mono))
            status = memory.status
            if memory.repo is None or status is None or status.branch is None:
                continue
            if not activity.active(project.path):
                continue
            records.extend(await self._pr(project, memory, status.branch, now, mono))
            records.extend(await self._build(project, memory, status.branch, now, mono))
        return records

    def _sooner(self, due: float, checked: float | None, mono: float) -> float:
        """``due`` brought forward to ``focus_refresh_seconds`` after the last look."""
        if checked is None:
            return due
        return min(due, max(mono, checked + self._settings.focus_refresh_seconds))

    def _expedite(self, memory: _Memory, mono: float) -> None:
        """Focus moved into the project: look sooner, but not within ``focus_refresh_seconds``
        of the last look, and never ahead of a failure backoff."""
        memory.git_next = self._sooner(memory.git_next, memory.git_checked, mono)
        if memory.pr_failures == 0:
            memory.pr_next = self._sooner(memory.pr_next, memory.pr_checked, mono)
        if memory.build_failures == 0:
            memory.build_next = self._sooner(memory.build_next, memory.build_checked, mono)

    async def _exec(self, argv: tuple[str, ...], kinds: _Kinds) -> ProcessResult:
        try:
            return await self._runner.run(argv, timeout=self._settings.command_timeout_seconds)
        except FileNotFoundError as exc:
            raise _CommandFailed(kinds.missing) from exc
        except TimeoutError as exc:
            raise _CommandFailed(kinds.timeout) from exc
        except OSError as exc:
            raise _CommandFailed(kinds.failed) from exc

    def _block(self, kind: str, mono: float) -> None:
        """gh is not going to work for a while: stop calling it."""
        if kind in {ghparse.GH_NOT_AUTHENTICATED, ghparse.GH_MISSING}:
            seconds = AUTH_BLOCK_SECONDS
        elif kind == ghparse.GH_RATE_LIMITED:
            seconds = RATE_BLOCK_SECONDS
        else:
            return
        self._blocked_until = max(self._blocked_until, mono + seconds)
        self._block_kind = kind

    # -- git --------------------------------------------------------------------------------

    async def _git(
        self, project: KnownProject, memory: _Memory, now: datetime, mono: float
    ) -> list[Record]:
        memory.git_checked = mono
        memory.git_next = mono + self._settings.state_interval_seconds
        git, root = self._settings.git_command, project.root
        try:
            status = await self._git_status(git, root)
            commit_time = await self._git_commit_time(git, root)
            remote = await self._git_remote(git, root)
        except _CommandFailed as failure:
            memory.git_failure = failure.kind
            return []
        memory.git_failure = None
        previous = memory.status
        memory.status = status
        memory.commit_time = commit_time
        if previous is not None:
            if previous.branch != status.branch:
                memory.pr = NO_PR  # clears the old branch's PR until the new one is looked up
                memory.pr_next = 0.0
                memory.pr_failures = 0
            if previous.oid != status.oid or previous.branch != status.branch:
                # The branch tip moved (a commit, a pull, a checkout): its build is news.
                if memory.build_failures == 0:
                    memory.build_next = self._sooner(memory.build_next, memory.build_checked, mono)
        memory.remote_url = None if remote is None else ghparse.public_remote_url(remote)
        memory.repo = None if remote is None else ghparse.github_repo(remote)
        name = None if remote is None else ghparse.remote_repo_name(remote)
        memory.repo_name = name or posixpath.basename(root.rstrip("/")) or None
        self._locator.forget()
        memory.group = self._locator.locate(root).group
        return self._project_records(project, memory, now)

    async def _git_status(self, git: str, root: str) -> gitstate.GitStatus:
        result = await self._exec(gitstate.status_argv(root, git), GIT_KINDS)
        if result.returncode != 0:
            raise _CommandFailed(gitstate.classify_failure(_text(result.stderr)))
        try:
            return gitstate.parse_status(_text(result.stdout))
        except gitstate.GitParseError as exc:
            raise _CommandFailed(gitstate.GIT_PARSE) from exc

    async def _git_commit_time(self, git: str, root: str) -> datetime | None:
        result = await self._exec(gitstate.commit_time_argv(root, git), GIT_KINDS)
        if result.returncode != 0:  # no commit yet
            return None
        try:
            return gitstate.parse_commit_time(_text(result.stdout))
        except gitstate.GitParseError as exc:
            raise _CommandFailed(gitstate.GIT_PARSE) from exc

    async def _git_remote(self, git: str, root: str) -> str | None:
        result = await self._exec(gitstate.remote_argv(root, git), GIT_KINDS)
        text = _text(result.stdout).strip()
        return text if result.returncode == 0 and text else None

    def _project_records(
        self, project: KnownProject, memory: _Memory, now: datetime
    ) -> list[Record]:
        status = memory.status
        if status is None:  # pragma: no cover - callers set the status first
            return []
        number: int | None = None
        state: str | None = None
        decision: str | None = None
        if isinstance(memory.pr, _NoPullRequest):
            number, state, decision = 0, NO_PR_STATE, ""
        elif memory.pr is not None:
            number, state, decision = (
                memory.pr.number,
                memory.pr.state,
                memory.pr.review_decision,
            )
        payload = ProjectPayload(
            path=project.path,
            repo_root=project.root,
            remote_url=memory.remote_url,
            repo_name=memory.repo_name,
            group_name=memory.group,
            branch=status.branch,
            dirty=status.dirty,
            ahead=status.ahead,
            behind=status.behind,
            last_commit_at=memory.commit_time,
            pr_number=number,
            pr_state=state,
            review_decision=decision,
        )
        if payload == memory.emitted:
            return []
        memory.emitted = payload
        return [
            ProjectEvent(source=SOURCE, entity_id=project.path, observed_at=now, payload=payload)
        ]

    # -- pull request ----------------------------------------------------------------------

    async def _pr(
        self, project: KnownProject, memory: _Memory, branch: str, now: datetime, mono: float
    ) -> list[Record]:
        repo = memory.repo
        if repo is None or mono < memory.pr_next or mono < self._blocked_until:
            return []
        if not self._pr_budget.try_acquire(mono):
            return []
        memory.pr_checked = mono
        try:
            result = await self._exec(
                ghparse.pr_argv(self._settings.gh_command, repo, branch), GH_KINDS
            )
        except _CommandFailed as failure:
            return self._pr_failed(memory, failure.kind, mono)
        if result.returncode == 0:
            try:
                memory.pr = ghparse.parse_pr(result.stdout)
            except ghparse.GhParseError:
                return self._pr_failed(memory, ghparse.GH_PARSE, mono)
        else:
            stderr = _text(result.stderr)
            if not ghparse.is_no_pull_request(stderr):
                return self._pr_failed(memory, ghparse.classify_stderr(stderr), mono)
            memory.pr = NO_PR
        memory.pr_failures = 0
        memory.pr_next = mono + self._settings.state_interval_seconds
        return self._project_records(project, memory, now)

    def _pr_failed(self, memory: _Memory, kind: str, mono: float) -> list[Record]:
        """The PR keeps what it was (the store keeps the last values); retry after a backoff."""
        memory.pr_failures += 1
        memory.pr_next = mono + _backoff(memory.pr_failures)
        self._block(kind, mono)
        return []

    # -- build ------------------------------------------------------------------------------

    async def _build(
        self, project: KnownProject, memory: _Memory, branch: str, now: datetime, mono: float
    ) -> list[Record]:
        repo = memory.repo
        if repo is None:  # pragma: no cover - callers check
            return []
        key = (repo.full, branch)
        if memory.build_key != key:  # another repository or branch: nothing carries over
            memory.build_key = key
            memory.etag = None
            memory.fold = None
            memory.build_next = 0.0
            memory.build_failures = 0
        if mono < memory.build_next:
            return []
        interval = self._settings.state_interval_seconds
        if mono < self._blocked_until:
            memory.build_next = mono + interval
            return [self._build_record(project, memory, now, self._block_kind)]
        if not self._runs_budget.try_acquire(mono):
            return []  # not an attempt: the section ages, and the next tick asks again
        memory.build_checked = mono
        etag = memory.etag if memory.fold is not None else None
        try:
            result = await self._exec(
                ghparse.actions_argv(self._settings.gh_command, repo, branch, etag), GH_KINDS
            )
        except _CommandFailed as failure:
            return self._build_failed(project, memory, failure.kind, now, mono)
        response = ghparse.parse_api_response(result.stdout)
        if response.status == 200:
            try:
                memory.fold = builds.fold_runs_body(response.body, repo.full)
            except builds.BuildFoldError as exc:
                return self._build_failed(project, memory, exc.kind, now, mono)
            memory.etag = response.etag
        elif not (response.status == 304 and memory.fold is not None):
            kind = ghparse.classify_api_failure(response, _text(result.stderr))
            return self._build_failed(project, memory, kind, now, mono)
        memory.build_failures = 0
        memory.build_next = mono + interval
        if response.rate_remaining is not None and response.rate_remaining < LOW_RATE_REMAINING:
            self._block(ghparse.GH_RATE_LIMITED, mono)
        return [self._build_record(project, memory, now, None)]

    def _build_failed(
        self, project: KnownProject, memory: _Memory, kind: str, now: datetime, mono: float
    ) -> list[Record]:
        """The last fold stays, flagged with the failure; retry after a backoff."""
        memory.build_failures += 1
        memory.build_next = mono + _backoff(memory.build_failures)
        self._block(kind, mono)
        return [self._build_record(project, memory, now, kind)]

    def _build_record(
        self, project: KnownProject, memory: _Memory, now: datetime, error_kind: str | None
    ) -> Record:
        fold = memory.fold or builds.NONE_FOLD
        return BuildEvent(
            source=SOURCE,
            entity_id=project.path,
            observed_at=now,
            payload=fold.to_payload(
                project.path,
                error_kind=error_kind,
                poll_interval_s=self._settings.state_interval_seconds,
            ),
        )


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")
