#!/usr/bin/env python3
"""Sanitize staged captures into committed fixtures (``PLAN-final-plan.md`` §0, WP1.5).

Dev-only; it is not packaged. It copies ``target/fixture-staging/<src>/...`` to
``tests/fixtures/<src>/...`` and replaces:

- home paths: ``/Users/<name>`` becomes ``/Users/user`` (and the ``-Users-<name>-`` form Claude uses
  in project directory names becomes ``-Users-user-``);
- email addresses: ``user@example.com``;
- the email domains in use (``git config user.email`` plus the built-in list): ``example.com``;
- names in the local deny-list ``tests/fixture-denylist.local.txt`` (one per line, ``#`` comments):
  ``customer-N``, where N is the entry's position in the file;
- token-shaped values: ``<redacted-token>``, and ``CLAUDE_CODE_MESSAGING_TOKEN`` or
  ``CMUX_SOCKET_CAPABILITY`` with a value: ``<redacted-secret-env>``.

It drops ``*.key`` files and keeps the directory structure and file modes. An empty staged
directory becomes a directory with a ``.gitkeep``. Run it by hand; tests never run it:

    uv run --frozen python scripts/sanitize_fixture.py ratatosk lidskjalv/transcripts ...
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STAGING = REPO_ROOT / "target" / "fixture-staging"
FIXTURES = REPO_ROOT / "tests" / "fixtures"
DENYLIST = REPO_ROOT / "tests" / "fixture-denylist.local.txt"

# Email domains in use. ``git config user.email`` adds its own domain at run time.
KNOWN_DOMAINS = ("wilhelmsen.com", "zenior.no")

SKIP_NAMES = {".DS_Store"}

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_HOME = re.compile(r"/Users/(?!Shared(?![A-Za-z0-9._-]))[A-Za-z0-9._-]+")
_SECRET_ENV = re.compile(
    r"(?:CLAUDE_CODE_MESSAGING_TOKEN|CMUX_SOCKET_CAPABILITY)[\"']?\s*[:=]\s*[\"']?[^\s\"'&;,}<]+"
)
_TOKENS = (
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"Bearer [A-Za-z0-9._~+/=-]{16,}"),
)


def git_email_domain() -> str | None:
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
    _, _, domain = result.stdout.strip().partition("@")
    return domain.lower() or None


def read_denylist(path: Path) -> list[str]:
    if not path.is_file():
        return []
    names: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        name = line.strip()
        if name and not name.startswith("#"):
            names.append(name)
    return names


class Sanitizer:
    def __init__(self, domains: Iterable[str], deny: Sequence[str], home_name: str) -> None:
        unique = sorted({d.lower() for d in domains if d}, key=len, reverse=True)
        self._domains = [
            re.compile(rf"(?:[A-Za-z0-9-]+\.)*{re.escape(d)}", re.IGNORECASE) for d in unique
        ]
        self._deny = [
            (re.compile(re.escape(name), re.IGNORECASE), f"customer-{index}")
            for index, name in sorted(enumerate(deny, start=1), key=lambda p: -len(p[1]))
        ]
        self._home_slug = re.compile(rf"-Users-{re.escape(home_name)}-") if home_name else None

    def text(self, value: str) -> str:
        value = _SECRET_ENV.sub("<redacted-secret-env>", value)
        for token in _TOKENS:
            value = token.sub("<redacted-token>", value)
        value = _EMAIL.sub("user@example.com", value)
        for domain in self._domains:
            value = domain.sub("example.com", value)
        value = _HOME.sub("/Users/user", value)
        if self._home_slug is not None:
            value = self._home_slug.sub("-Users-user-", value)
        for pattern, replacement in self._deny:
            value = pattern.sub(replacement, value)
        return value


def sanitize_tree(source: Path, target: Path, sanitizer: Sanitizer) -> tuple[int, int]:
    """Copy ``source`` to ``target``. Returns ``(files written, files dropped)``."""
    written = dropped = 0
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        destination = target / relative
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            if not any(path.iterdir()):
                (destination / ".gitkeep").touch()
            continue
        if path.suffix == ".key" or path.name in SKIP_NAMES:
            dropped += 1
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = path.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            destination.write_bytes(data)
        else:
            destination.write_text(sanitizer.text(text), encoding="utf-8")
        shutil.copymode(path, destination)
        written += 1
    return written, dropped


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("sources", nargs="+", help="paths below the staging directory")
    parser.add_argument("--staging", type=Path, default=STAGING)
    parser.add_argument("--out", type=Path, default=FIXTURES)
    parser.add_argument("--denylist", type=Path, default=DENYLIST)
    parser.add_argument("--home-name", default=Path.home().name)
    args = parser.parse_args(argv)

    domains = [*KNOWN_DOMAINS]
    from_git = git_email_domain()
    if from_git:
        domains.append(from_git)
    sanitizer = Sanitizer(domains, read_denylist(args.denylist), args.home_name)

    status = 0
    for name in args.sources:
        source = args.staging / name
        if not source.is_dir():
            print(f"missing in staging: {source}", file=sys.stderr)
            status = 1
            continue
        written, dropped = sanitize_tree(source, args.out / name, sanitizer)
        print(f"{name}: {written} files written, {dropped} dropped")
    return status


if __name__ == "__main__":
    sys.exit(main())
