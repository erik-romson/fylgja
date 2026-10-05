"""Committed fixtures are sanitized (``PLAN-final-plan.md`` §0 "Fixture hygiene", WP1.5).

The scan covers ``tests/fixtures/`` and fails on:

- the developer's home path (``/Users/erik``, also in Claude's ``-Users-erik-`` project slugs);
- the email domains in use (listed below, plus the one in ``git config user.email``);
- any email address that is not ``@example.com``;
- customer names in the gitignored local deny-list ``tests/fixture-denylist.local.txt``, read
  only when that file exists;
- ``*.key`` files (session socket keys);
- ``CLAUDE_CODE_MESSAGING_TOKEN`` or ``CMUX_SOCKET_CAPABILITY`` followed by a value, and other
  token-shaped strings.

``scripts/sanitize_fixture.py`` produces fixtures that pass; the tests never write them.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"
DENYLIST = REPO_ROOT / "tests" / "fixture-denylist.local.txt"

# Email domains seen in git config and in the staged captures.
EMAIL_DOMAINS = ("wilhelmsen.com", "zenior.no")
ALLOWED_EMAIL_DOMAIN = "example.com"

HOME_PATTERNS = ("/Users/erik", "-Users-erik-")
SECRET_ENV = re.compile(
    r"(?:CLAUDE_CODE_MESSAGING_TOKEN|CMUX_SOCKET_CAPABILITY)[\"']?\s*[:=]\s*[\"']?[^\s\"'<]"
)
TOKEN_SHAPES = {
    "openai/anthropic style key": re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    "github token": re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"),
    "slack token": re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    "aws key id": re.compile(r"AKIA[0-9A-Z]{16}"),
    "jwt": re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "bearer token": re.compile(r"Bearer [A-Za-z0-9._~+/=-]{16,}"),
}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")


def _fixture_files() -> list[Path]:
    return sorted(p for p in FIXTURES.rglob("*") if p.is_file() or p.is_symlink())


def _text(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace")


def _git_email_domain() -> str | None:
    try:
        result = subprocess.run(
            ["git", "config", "user.email"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            cwd=REPO_ROOT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip().partition("@")[2].lower() or None


def _denylist() -> list[str]:
    if not DENYLIST.is_file():
        return []
    lines = (line.strip() for line in DENYLIST.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def test_fixtures_directory_is_populated() -> None:
    names = {_rel(p) for p in _fixture_files()}
    assert "tests/fixtures/ratatosk/probe-session/hook-events.ndjson" in names
    assert "tests/fixtures/lidskjalv/builds/runs-pass.json" in names
    assert "tests/fixtures/lidskjalv/transcripts/test-1.jsonl" in names


def test_no_key_files() -> None:
    assert [_rel(p) for p in FIXTURES.rglob("*.key")] == []


def test_no_sqlite_databases() -> None:
    bad = [_rel(p) for p in _fixture_files() if p.suffix in {".db", ".sqlite", ".sqlite3"}]
    assert bad == []


def test_no_home_path_of_the_developer() -> None:
    hits = [
        f"{_rel(p)}: {pattern}"
        for p in _fixture_files()
        for pattern in HOME_PATTERNS
        if pattern in _text(p)
    ]
    assert hits == []


def test_no_email_domain_in_use() -> None:
    domains = {*EMAIL_DOMAINS}
    from_git = _git_email_domain()
    if from_git:
        domains.add(from_git)
    hits = [
        f"{_rel(p)}: {domain}"
        for p in _fixture_files()
        for domain in sorted(domains)
        if domain in _text(p).lower()
    ]
    assert hits == []


def test_every_email_address_is_the_placeholder() -> None:
    hits = [
        f"{_rel(p)}: @{match.group(1)}"
        for p in _fixture_files()
        for match in EMAIL.finditer(_text(p))
        if match.group(1).lower() != ALLOWED_EMAIL_DOMAIN
    ]
    assert hits == []


def test_no_customer_name_from_the_local_denylist() -> None:
    names = _denylist()
    if not names:
        pytest.skip("no local deny-list (tests/fixture-denylist.local.txt)")
    hits = [
        f"{_rel(p)}: deny-list entry {index}"
        for p in _fixture_files()
        for index, name in enumerate(names, start=1)
        if name.lower() in _text(p).lower()
    ]
    assert hits == []


def test_no_messaging_token_or_capability_value() -> None:
    hits = [_rel(p) for p in _fixture_files() if SECRET_ENV.search(_text(p))]
    assert hits == []


def test_no_token_shaped_string() -> None:
    hits = [
        f"{_rel(p)}: {label}"
        for p in _fixture_files()
        for label, pattern in TOKEN_SHAPES.items()
        if pattern.search(_text(p))
    ]
    assert hits == []


# --- the scanners themselves --------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "CLAUDE_CODE_MESSAGING_TOKEN=abc123",
        'export CMUX_SOCKET_CAPABILITY="abc123"',
        '{"CLAUDE_CODE_MESSAGING_TOKEN": "abc123"}',
        "CMUX_SOCKET_CAPABILITY: abc123",
    ],
)
def test_secret_env_pattern_matches_a_value(text: str) -> None:
    assert SECRET_ENV.search(text)


@pytest.mark.parametrize(
    "text",
    ["CLAUDE_CODE_MESSAGING_TOKEN", "<redacted-secret-env>", "CMUX_SOCKET_CAPABILITY=<redacted>"],
)
def test_secret_env_pattern_ignores_names_without_a_value(text: str) -> None:
    assert not SECRET_ENV.search(text)
