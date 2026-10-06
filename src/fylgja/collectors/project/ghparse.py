"""Pure parsers for ``gh`` output and GitHub remotes (``PLAN-final-plan.md`` WP5.4, §4.7).

No I/O and no clock: every function reads text or bytes a command already printed.

* **Remotes.** :func:`github_repo` reads ``owner/repo`` from the three forms Lidskjalv accepts
  (``git@github.com:o/r.git``, ``https://github.com/o/r[.git]``, ``ssh://git@github.com/o/r.git``).
  :func:`public_remote_url` drops any user info (``https://token@…``), so a credential in a
  remote never reaches the store.
* **Pull requests.** ``gh pr view <branch> -R o/r --json
  number,state,reviewDecision,statusCheckRollup`` prints one object; :func:`parse_pr` keeps the
  number, the state, the review decision (``""`` when there is none) and an aggregate of the
  checks.
* **API responses.** ``gh api -i`` prints the status line, the headers, a blank line and the body.
  :func:`parse_api_response` keeps the status, the ``ETag``, ``X-RateLimit-Remaining`` and the
  body. gh exits non-zero for a 304, so the output is read whatever the exit status.
* **Failures** are short codes: ``gh_missing``, ``gh_timeout``, ``gh_not_authenticated``,
  ``gh_rate_limited``, ``gh_offline``, ``gh_not_found``, ``gh_forbidden``, ``gh_server_error``,
  ``gh_failed`` and ``gh_parse``. A message, a path or a token is never part of one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Final, Literal
from urllib.parse import quote

__all__ = [
    "GH_FAILED",
    "GH_FORBIDDEN",
    "GH_MISSING",
    "GH_NOT_AUTHENTICATED",
    "GH_NOT_FOUND",
    "GH_OFFLINE",
    "GH_PARSE",
    "GH_RATE_LIMITED",
    "GH_SERVER_ERROR",
    "GH_TIMEOUT",
    "ApiResponse",
    "GhParseError",
    "GitHubRepo",
    "PullRequestInfo",
    "actions_argv",
    "classify_api_failure",
    "classify_stderr",
    "github_repo",
    "is_no_pull_request",
    "parse_api_response",
    "parse_pr",
    "pr_argv",
    "public_remote_url",
    "remote_repo_name",
]

GH_MISSING: Final = "gh_missing"
GH_TIMEOUT: Final = "gh_timeout"
GH_NOT_AUTHENTICATED: Final = "gh_not_authenticated"
GH_RATE_LIMITED: Final = "gh_rate_limited"
GH_OFFLINE: Final = "gh_offline"
GH_NOT_FOUND: Final = "gh_not_found"
GH_FORBIDDEN: Final = "gh_forbidden"
GH_SERVER_ERROR: Final = "gh_server_error"
GH_FAILED: Final = "gh_failed"
GH_PARSE: Final = "gh_parse"

PR_FIELDS: Final = "number,state,reviewDecision,statusCheckRollup"
PER_PAGE: Final = 30

_USERINFO: Final = re.compile(r"^(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<user>[^/@]*@)", re.IGNORECASE)
_GITHUB_PREFIXES: Final = ("git@github.com:", "https://github.com/", "ssh://github.com/")
_REPO_PART: Final = re.compile(r"[A-Za-z0-9._-]+")


class GhParseError(ValueError):
    """The output is not what ``gh`` prints."""

    kind = GH_PARSE


@dataclass(frozen=True)
class GitHubRepo:
    owner: str
    name: str

    @property
    def full(self) -> str:
        return f"{self.owner}/{self.name}"


def _strip_userinfo(url: str) -> str:
    return _USERINFO.sub(lambda m: m.group("scheme"), url.strip())


def public_remote_url(url: str) -> str:
    """The remote without credentials: ``https://user:token@host/…`` loses ``user:token@``.

    The ``git@host:path`` form keeps its fixed ``git@`` (a user name, not a secret).
    """
    return _strip_userinfo(url)


def github_repo(url: str) -> GitHubRepo | None:
    """``owner/repo`` of a github.com remote, ``None`` for any other remote."""
    text = _strip_userinfo(url)  # also turns ssh://git@github.com/ into ssh://github.com/
    path: str | None = None
    for prefix in _GITHUB_PREFIXES:
        if text.startswith(prefix):
            path = text[len(prefix) :]
            break
    if path is None:
        return None
    path = path.removesuffix("/").removesuffix(".git")
    parts = path.split("/")
    if len(parts) != 2 or not all(_REPO_PART.fullmatch(part) for part in parts):
        return None
    return GitHubRepo(parts[0], parts[1])


def remote_repo_name(url: str) -> str | None:
    """The last path component of a remote, without ``.git``; ``None`` when there is none."""
    text = _strip_userinfo(url).rstrip("/").removesuffix(".git")
    name = re.split(r"[/:]", text)[-1] if text else ""
    return name or None


# -- pull requests --------------------------------------------------------------------------


@dataclass(frozen=True)
class PullRequestInfo:
    number: int
    state: str
    """``OPEN``, ``CLOSED`` or ``MERGED``."""
    review_decision: str
    """``APPROVED``, ``CHANGES_REQUESTED``, ``REVIEW_REQUIRED`` or ``""`` (none)."""
    checks: Literal["pass", "fail", "pending"] | None
    """The aggregate of the status checks; ``None`` when the PR has none."""


def pr_argv(gh: str, repo: GitHubRepo, branch: str) -> tuple[str, ...]:
    return (gh, "pr", "view", branch, "-R", repo.full, "--json", PR_FIELDS)


def _check_state(item: dict[str, Any]) -> Literal["pass", "fail", "pending"]:
    if item.get("__typename") == "StatusContext":
        state = str(item.get("state", "")).upper()
        if state == "SUCCESS":
            return "pass"
        return "pending" if state in {"PENDING", "EXPECTED", ""} else "fail"
    if str(item.get("status", "")).upper() != "COMPLETED":
        return "pending"
    if str(item.get("conclusion", "")).upper() in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
        return "pass"
    return "fail"


def _aggregate(rollup: object) -> Literal["pass", "fail", "pending"] | None:
    if rollup is None:
        return None
    if not isinstance(rollup, list) or not all(isinstance(item, dict) for item in rollup):
        raise GhParseError("statusCheckRollup")
    states = {_check_state(item) for item in rollup}
    for worst in ("fail", "pending", "pass"):
        if worst in states:
            return worst
    return None


def parse_pr(text: str | bytes) -> PullRequestInfo:
    """Parse the ``--json`` output of ``gh pr view``. Raises :class:`GhParseError`."""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise GhParseError("json") from exc
    if not isinstance(data, dict):
        raise GhParseError("shape")
    number, state, decision = data.get("number"), data.get("state"), data.get("reviewDecision")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise GhParseError("number")
    if not isinstance(state, str) or not state:
        raise GhParseError("state")
    if decision is not None and not isinstance(decision, str):
        raise GhParseError("reviewDecision")
    return PullRequestInfo(
        number=number,
        state=state,
        review_decision=decision or "",
        checks=_aggregate(data.get("statusCheckRollup")),
    )


def is_no_pull_request(stderr: str) -> bool:
    """``gh pr view`` for a branch without a PR: not a failure."""
    return "no pull requests found" in stderr.lower()


# -- the actions-runs call ------------------------------------------------------------------


def actions_argv(
    gh: str, repo: GitHubRepo, branch: str, etag: str | None = None
) -> tuple[str, ...]:
    """``gh api -i repos/o/r/actions/runs?branch=…&per_page=30`` (with ``If-None-Match``)."""
    endpoint = f"repos/{repo.full}/actions/runs?branch={quote(branch, safe='')}&per_page={PER_PAGE}"
    argv = (gh, "api", "-i", endpoint)
    return argv if not etag else (*argv, "-H", f"If-None-Match: {etag}")


@dataclass(frozen=True)
class ApiResponse:
    """What ``gh api -i`` printed."""

    status: int | None
    """The HTTP status; ``None`` when the output has no status line."""
    etag: str | None
    rate_remaining: int | None
    body: bytes


_STATUS_LINE: Final = re.compile(rb"HTTP/\S+\s+(\d{3})")


def parse_api_response(stdout: bytes) -> ApiResponse:
    """Split ``gh api -i`` output into status, headers of interest and body (CRLF or LF)."""
    head, separator, body = stdout.partition(b"\r\n\r\n")
    if not separator:
        head, separator, body = stdout.partition(b"\n\n")
    lines = head.replace(b"\r\n", b"\n").split(b"\n")
    match = _STATUS_LINE.match(lines[0]) if lines else None
    if match is None:
        return ApiResponse(status=None, etag=None, rate_remaining=None, body=b"")
    etag: str | None = None
    remaining: int | None = None
    for line in lines[1:]:
        name, colon, value = line.partition(b":")
        if not colon:
            continue
        key = name.strip().lower()
        text = value.strip().decode("utf-8", errors="replace")
        if key == b"etag":
            etag = text or None
        elif key == b"x-ratelimit-remaining" and text.isdigit():
            remaining = int(text)
    return ApiResponse(status=int(match.group(1)), etag=etag, rate_remaining=remaining, body=body)


# -- failures -------------------------------------------------------------------------------


def classify_stderr(stderr: str) -> str:
    """The short code of a ``gh`` command that failed without an HTTP answer."""
    lowered = stderr.lower()
    if (
        "gh auth login" in lowered
        or "not logged in" in lowered
        or "authentication" in lowered
        or "bad credentials" in lowered
        or "http 401" in lowered
    ):
        return GH_NOT_AUTHENTICATED
    if "rate limit" in lowered or "http 429" in lowered:
        return GH_RATE_LIMITED
    if (
        "error connecting" in lowered
        or "could not resolve host" in lowered
        or "no such host" in lowered
        or "i/o timeout" in lowered
    ):
        return GH_OFFLINE
    if "http 404" in lowered or "could not resolve to a" in lowered:
        return GH_NOT_FOUND
    if "http 403" in lowered:
        return GH_FORBIDDEN
    return GH_FAILED


def classify_api_failure(response: ApiResponse, stderr: str) -> str:
    """The short code of an API answer that is neither 200 nor 304."""
    status = response.status
    if status is None:
        return classify_stderr(stderr)
    if status == 401:
        return GH_NOT_AUTHENTICATED
    if status == 429:
        return GH_RATE_LIMITED
    if status == 403:
        limited = response.rate_remaining == 0 or b"rate limit" in response.body.lower()
        return GH_RATE_LIMITED if limited else GH_FORBIDDEN
    if status == 404:
        return GH_NOT_FOUND
    if status >= 500:
        return GH_SERVER_ERROR
    return GH_FAILED
