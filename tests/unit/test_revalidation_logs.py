"""Enforces the revalidation-log format from PLAN-final-plan.md section 0."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = REPO_ROOT / "docs" / "revalidation"

NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9-]+\.md$")
COLUMNS = ["claim", "command run", "observed", "verdict", "action"]
VERDICTS = {"holds", "changed", "unknown"}
SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")


def _logs() -> list[Path]:
    return sorted(LOG_DIR.glob("*.md")) if LOG_DIR.is_dir() else []


def _split_row(line: str) -> list[str]:
    """Split a Markdown table row into trimmed cells; `\\|` stays inside a cell."""
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    cells = re.split(r"(?<!\\)\|", body)
    return [cell.strip() for cell in cells]


def _is_separator(cells: list[str]) -> bool:
    return all(SEPARATOR_CELL.match(cell) for cell in cells)


def _tables(text: str) -> list[tuple[list[str], list[list[str]]]]:
    """Return (header cells, body rows) for every pipe table in `text`."""
    tables: list[tuple[list[str], list[list[str]]]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("|") and i + 1 < len(lines):
            header = _split_row(lines[i])
            if lines[i + 1].lstrip().startswith("|") and _is_separator(_split_row(lines[i + 1])):
                rows: list[list[str]] = []
                j = i + 2
                while j < len(lines) and lines[j].lstrip().startswith("|"):
                    rows.append(_split_row(lines[j]))
                    j += 1
                tables.append((header, rows))
                i = j
                continue
        i += 1
    return tables


def _log_tables(text: str) -> list[tuple[list[str], list[list[str]]]]:
    return [t for t in _tables(text) if [c.lower() for c in t[0]] == COLUMNS]


def _check_log(name: str, text: str) -> list[str]:
    """Return every format violation found in one log."""
    problems: list[str] = []
    if not NAME_RE.match(name):
        problems.append(f"file name {name!r} does not match YYYY-MM-DD-<topic>.md")
    tables = _log_tables(text)
    if not tables:
        problems.append(
            "no table with the columns Claim | Command run | Observed | Verdict | Action"
        )
    for _, rows in tables:
        for number, cells in enumerate(rows, start=1):
            where = f"row {number}"
            if len(cells) != len(COLUMNS):
                problems.append(f"{where}: {len(cells)} cells, expected {len(COLUMNS)}")
                continue
            claim, command, observed, verdict, action = cells
            for label, value in (
                ("claim", claim),
                ("command run", command),
                ("observed", observed),
                ("action", action),
            ):
                if not value:
                    problems.append(f"{where}: empty {label}")
            if verdict.strip("` ").lower() not in VERDICTS:
                problems.append(f"{where}: verdict {verdict!r} is not holds, changed or unknown")
            if verdict.strip("` ").lower() == "unknown" and not action:
                problems.append(f"{where}: unknown verdict needs an action")
    return problems


def test_log_directory_files_are_markdown_logs() -> None:
    if not LOG_DIR.is_dir():
        pytest.skip("no docs/revalidation directory yet")
    stray = [p.name for p in LOG_DIR.iterdir() if p.is_file() and p.suffix != ".md"]
    assert stray == []


@pytest.mark.parametrize("log", _logs(), ids=lambda p: p.name)
def test_revalidation_log_format(log: Path) -> None:
    problems = _check_log(log.name, log.read_text(encoding="utf-8"))
    assert problems == [], f"{log.name}: " + "; ".join(problems)


def test_baseline_log_covers_r1_to_r12() -> None:
    baseline = LOG_DIR / "2026-10-05-baseline.md"
    if not baseline.exists():
        pytest.skip("baseline log not written yet")
    text = baseline.read_text(encoding="utf-8")
    (_, rows), *_ = _log_tables(text)
    claims = " ".join(row[0] for row in rows)
    for n in range(1, 13):
        assert re.search(rf"\bR{n}\b", claims), f"R{n} missing from the baseline log"


GOOD = (
    "| Claim | Command run | Observed | Verdict | Action |\n"
    "|---|---|---|---|---|\n"
    "| c | `cmd` | seen | holds | none |\n"
)


def test_checker_accepts_a_well_formed_log() -> None:
    assert _check_log("2026-10-05-x.md", GOOD) == []


def test_checker_accepts_case_and_escaped_pipes() -> None:
    text = (
        "| claim | COMMAND RUN | Observed | VERDICT | action |\n"
        "| :--- | --- | --- | --- | --- |\n"
        "| c | `a \\| b` | seen | `changed` | WP1.1 |\n"
    )
    assert _check_log("2026-10-05-x.md", text) == []


@pytest.mark.parametrize(
    ("name", "text", "fragment"),
    [
        ("baseline.md", GOOD, "does not match"),
        ("2026-10-05-x.md", "no table here\n", "no table"),
        (
            "2026-10-05-x.md",
            "| Claim | Command | Observed | Verdict | Action |\n|---|---|---|---|---|\n",
            "no table",
        ),
        ("2026-10-05-x.md", GOOD + "| c | cmd | seen | holds |\n", "4 cells"),
        ("2026-10-05-x.md", GOOD + "|  | cmd | seen | holds | a |\n", "empty claim"),
        ("2026-10-05-x.md", GOOD + "| c |  | seen | holds | a |\n", "empty command run"),
        ("2026-10-05-x.md", GOOD + "| c | cmd |  | holds | a |\n", "empty observed"),
        ("2026-10-05-x.md", GOOD + "| c | cmd | seen | holds |  |\n", "empty action"),
        ("2026-10-05-x.md", GOOD + "| c | cmd | seen | maybe | a |\n", "not holds"),
        ("2026-10-05-x.md", GOOD + "| c | cmd | seen | unknown |  |\n", "unknown verdict"),
    ],
)
def test_checker_rejects_malformed_logs(name: str, text: str, fragment: str) -> None:
    problems = _check_log(name, text)
    assert any(fragment in p for p in problems), problems
