"""Commit subjects for the journal window (J8 section 3, D3): the argv, the parser, the window
filter and ``read_commits`` with every failure mapped to no commits."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

import pytest

from fylgja.core.journal import (
    COMMIT_HORIZON,
    in_window,
    log_argv,
    parse_log,
    show_argv,
)
from fylgja.core.journal_service import GIT_TIMEOUT, read_commits
from fylgja.testing.fakes import FakeProcessRunner

SINCE = datetime(2025, 10, 17, 16, 0, tzinfo=UTC)
EPOCH = 1760716800
AS_OF = SINCE + timedelta(hours=2)
GIT = "/usr/bin/git"
ROOT = "/work/repo"
H1 = "a" * 40
H2 = "b" * 40
H3 = "c" * 40


def redact(text: str) -> str:
    return re.sub(r"ACME\w*", "[redacted]", text)


def line(commit_hash: str, at: int, subject: str) -> str:
    return f"{commit_hash}\x00{at}\x00{subject}"


def script(runner: FakeProcessRunner, *lines: str, returncode: int = 0) -> None:
    runner.script(GIT, stdout="\n".join(lines) + "\n", returncode=returncode)


def test_the_argv() -> None:
    argv = log_argv(GIT, ROOT, "feature/x", SINCE)
    assert argv == [
        GIT,
        "--no-optional-locks",
        "-C",
        ROOT,
        "log",
        "--no-merges",
        "--format=%H%x00%ct%x00%s",
        f"--since=@{EPOCH}",
        "-n",
        "50",
        "refs/heads/feature/x",
        "--",
    ]
    assert SINCE.timestamp() == EPOCH


def test_the_show_argv() -> None:
    assert show_argv(GIT, ROOT, H1) == [
        GIT,
        "--no-optional-locks",
        "-C",
        ROOT,
        "show",
        "--stat",
        "--format=%s%n%n%b",
        H1,
        "--",
    ]


def test_two_lines_and_a_malformed_one() -> None:
    out = "\n".join(
        [
            line(H1, EPOCH + 5, "first"),
            "garbage without separators",
            line(H2, EPOCH + 9, "second"),
            "",
        ]
    )
    commits = parse_log(out, redact)
    assert [(c.hash, c.subject) for c in commits] == [(H1, "first"), (H2, "second")]
    assert commits[0].committed_at == datetime.fromtimestamp(EPOCH + 5, UTC)


@pytest.mark.parametrize(
    "bad",
    [
        f"{H1}\x00{EPOCH}",  # too few parts
        f"{H1}\x00{EPOCH}\x00a\x00b",  # too many parts
        f"short\x00{EPOCH}\x00subject",  # not a 40-hex hash
        f"{'G' * 40}\x00{EPOCH}\x00subject",
        f"{H1}\x00soon\x00subject",  # not an integer time
    ],
)
def test_malformed_lines_are_skipped(bad: str) -> None:
    assert parse_log(bad, redact) == []


def test_a_vertical_tab_in_a_subject_is_kept_whole() -> None:
    commits = parse_log(line(H1, EPOCH, "one\x0btwo") + "\n" + line(H2, EPOCH, "next"), redact)
    assert len(commits) == 2
    assert commits[0].subject == "one two"  # whitespace collapsed, not split


def test_a_subject_is_redacted() -> None:
    (commit,) = parse_log(line(H1, EPOCH, "fix ACMEcorp import"), redact)
    assert commit.subject == "fix [redacted] import"


def test_a_long_subject_is_cut() -> None:
    (commit,) = parse_log(line(H1, EPOCH, "x" * 300), redact)
    assert commit.subject == "x" * 100


async def test_a_repo_with_no_commits_gives_none(caplog: pytest.LogCaptureFixture) -> None:
    runner = FakeProcessRunner()
    runner.script(GIT, returncode=128, stderr="fatal: bad revision")
    with caplog.at_level(logging.DEBUG, logger="fylgja.journal"):
        assert await read_commits(runner, GIT, ROOT, "main", SINCE, AS_OF, redact) == []
    assert ROOT not in caplog.text
    assert "main" not in caplog.text


@pytest.mark.parametrize(
    "error", [TimeoutError("slow"), FileNotFoundError("no git"), PermissionError("denied")]
)
async def test_failures_give_none(error: BaseException) -> None:
    runner = FakeProcessRunner()
    runner.script(GIT, error=error)
    assert await read_commits(runner, GIT, ROOT, "main", SINCE, AS_OF, redact) == []


async def test_a_missing_git_gives_none() -> None:
    assert await read_commits(FakeProcessRunner(), GIT, ROOT, "main", SINCE, AS_OF, redact) == []


async def test_the_call_has_a_timeout() -> None:
    runner = FakeProcessRunner()
    script(runner)
    await read_commits(runner, GIT, ROOT, "main", SINCE, AS_OF, redact)
    assert runner.timeouts == [GIT_TIMEOUT] == [5.0]


async def test_since_is_clamped_to_the_horizon() -> None:
    runner = FakeProcessRunner()
    script(runner)
    old = AS_OF - timedelta(days=30)
    await read_commits(runner, GIT, ROOT, "main", old, AS_OF, redact)
    expected = int((AS_OF - COMMIT_HORIZON).timestamp())
    assert f"--since=@{expected}" in runner.calls[0]
    assert COMMIT_HORIZON == timedelta(days=7)


async def test_a_commit_at_since_is_left_out_and_one_at_as_of_is_kept() -> None:
    runner = FakeProcessRunner()
    script(
        runner,
        line(H1, EPOCH, "at since"),  # git's --since is inclusive
        line(H2, int(AS_OF.timestamp()), "at as_of"),
        line(H3, EPOCH + 1, "just after"),
    )
    commits = await read_commits(runner, GIT, ROOT, "main", SINCE, AS_OF, redact)
    assert [c.subject for c in commits] == ["at as_of", "just after"]


async def test_a_fractional_since_floors_in_the_argv_and_filters_in_code() -> None:
    runner = FakeProcessRunner()
    script(runner, line(H1, EPOCH, "same second"), line(H2, EPOCH + 1, "next second"))
    since = SINCE + timedelta(seconds=0.7)
    commits = await read_commits(runner, GIT, ROOT, "main", since, AS_OF, redact)
    assert f"--since=@{EPOCH}" in runner.calls[0]
    assert [c.subject for c in commits] == ["next second"]


async def test_a_future_commit_appears_in_exactly_one_window() -> None:
    future = int(AS_OF.timestamp()) + 60
    runner = FakeProcessRunner()
    script(runner, line(H1, future, "from the future"))
    assert await read_commits(runner, GIT, ROOT, "main", SINCE, AS_OF, redact) == []
    later = AS_OF + timedelta(hours=1)
    commits = await read_commits(runner, GIT, ROOT, "main", AS_OF, later, redact)
    assert [c.subject for c in commits] == ["from the future"]


def test_in_window_is_exclusive_below_and_inclusive_above() -> None:
    commits = parse_log(
        "\n".join([line(H1, EPOCH, "a"), line(H2, EPOCH + 1, "b"), line(H3, EPOCH + 7201, "c")]),
        redact,
    )
    kept = in_window(commits, SINCE, AS_OF)
    assert [c.subject for c in kept] == ["b"]
