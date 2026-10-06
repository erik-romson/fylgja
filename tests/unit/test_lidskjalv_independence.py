"""Static guard: nothing in Fylgja's package or tests depends on a Lidskjalv installation (A21).

``src/fylgja/`` and ``tests/`` (without ``tests/live/``, which may take real paths from the
environment, and ``tests/fixtures/``, which holds committed data) must not

* mention ``compare_lidskjalv`` (the dual-run script is a dev tool; nothing imports it);
* read ``~/.cache/lidskjalv``;
* name the Lidskjalv checkout;
* invoke ``lidskjalv.sh``.

The dynamic counterpart is ``scripts/independence_check.sh``. This file is not scanned itself: its
patterns and its negative examples are the very strings it forbids.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SELF = Path(__file__).resolve()
SCANNED_ROOTS = ("src/fylgja", "tests")
EXCLUDED_PARTS = (("tests", "live"), ("tests", "fixtures"))

# rule -> (regex over the source text, why)
TEXT_RULES: dict[str, tuple[re.Pattern[str], str]] = {
    "dual-run-script": (
        re.compile(r"compare_lidskjalv"),
        "scripts/compare_lidskjalv.py is a dev tool; nothing imports or runs it",
    ),
    "cache-read": (
        re.compile(r"""\.cache["']?\s*[/,]\s*["']?lidskjalv"""),
        "Lidskjalv's cache is Lidskjalv's: Fylgja never reads it",
    ),
    "checkout-path": (
        re.compile(r"""erik-romson["']?\s*[/,]\s*["']?lidskjalv"""),
        "no code or test needs the Lidskjalv checkout",
    ),
}

# Python calls that start a process (the last name of the called function).
LAUNCHERS = frozenset(
    {
        "run",
        "Popen",
        "call",
        "check_call",
        "check_output",
        "system",
        "popen",
        "create_subprocess_exec",
        "create_subprocess_shell",
        "execv",
        "execve",
        "execl",
        "execlp",
        "execvp",
        "spawnv",
        "spawnl",
    }
)
SCRIPT = "lidskjalv.sh"
# A reference to a Lidskjalv-named file, such as ``lidskjalv-mapping.md``.
FILE_REFERENCE = re.compile(r"lidskjalv-[\w.-]+\.\w+")


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    rule: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}"


# (path, rule) -> why this exact reference is fine. Every entry must still be needed.
ALLOWED: dict[tuple[str, str], str] = {
    # The doctor's informational "lidskjalv-hooks" check recognizes Lidskjalv's hook entries by the
    # script name in their command. It reads settings.json only, never the script, and it cannot
    # change health (A21).
    ("src/fylgja/cli/doctor.py", "script-name-in-package"): "informational doctor check",
    # The mapping test reads Fylgja's own document docs/api/lidskjalv-mapping.md (the field mapping
    # from Lidskjalv's read sites to /now); the document is named after Lidskjalv, not read from it.
    ("tests/unit/test_lidskjalv_mapping.py", "lidskjalv-file-reference"): "doc reference",
}


def _launcher_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _strings(node: ast.AST) -> list[str]:
    return [
        n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def scan_source(rel: str, text: str) -> list[Violation]:
    """Every violation in one file, ``rel`` being its repository-relative POSIX path."""
    found: list[Violation] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for rule, (pattern, _why) in TEXT_RULES.items():
            if pattern.search(line):
                found.append(Violation(rel, number, rule))
    if not rel.endswith(".py"):
        return found
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return found
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _launcher_name(node) in LAUNCHERS:
            arguments = [*node.args, *(k.value for k in node.keywords)]
            if any(SCRIPT in s for argument in arguments for s in _strings(argument)):
                found.append(Violation(rel, node.lineno, "invocation"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if rel.startswith("src/fylgja/") and SCRIPT in node.value:
                found.append(Violation(rel, node.lineno, "script-name-in-package"))
            if FILE_REFERENCE.search(node.value):
                found.append(Violation(rel, node.lineno, "lidskjalv-file-reference"))
    return found


def _files() -> list[Path]:
    paths: list[Path] = []
    for root in SCANNED_ROOTS:
        for path in sorted((REPO / root).rglob("*")):
            relative = path.relative_to(REPO).parts
            if not path.is_file() or "__pycache__" in relative or path.resolve() == SELF:
                continue
            if any(relative[: len(part)] == part for part in EXCLUDED_PARTS):
                continue
            if path.suffix in {".py", ".sh", ".toml", ".md", ".json", ".sql"}:
                paths.append(path)
    return paths


def _violations() -> list[Violation]:
    found: list[Violation] = []
    for path in _files():
        text = path.read_text(encoding="utf-8", errors="replace")
        found.extend(scan_source(path.relative_to(REPO).as_posix(), text))
    return found


def test_the_scan_covers_the_package_and_the_tests() -> None:
    names = {p.relative_to(REPO).as_posix() for p in _files()}
    assert "src/fylgja/cli/doctor.py" in names
    assert "tests/unit/test_lidskjalv_mapping.py" in names
    assert not any(n.startswith(("tests/live/", "tests/fixtures/")) for n in names)
    assert SELF.relative_to(REPO).as_posix() not in names


def test_nothing_depends_on_a_lidskjalv_installation() -> None:
    unexpected = [v for v in _violations() if (v.path, v.rule) not in ALLOWED]
    assert not unexpected, "Lidskjalv dependency (A21):\n" + "\n".join(map(str, unexpected))


def test_every_allow_list_entry_is_still_needed() -> None:
    used = {(v.path, v.rule) for v in _violations()}
    stale = sorted(set(ALLOWED) - used)
    assert not stale, f"remove these allow-list entries: {stale}"


def test_the_allow_list_is_exactly_the_doctor_check_and_the_mapping_doc() -> None:
    assert sorted(ALLOWED) == [
        ("src/fylgja/cli/doctor.py", "script-name-in-package"),
        ("tests/unit/test_lidskjalv_mapping.py", "lidskjalv-file-reference"),
    ]


# -- the scanner itself ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("snippet", "rule"),
    [
        ("import compare_lidskjalv\n", "dual-run-script"),
        ("from scripts import compare_lidskjalv as c\n", "dual-run-script"),
        ("p = Path.home() / '.cache' / 'lidskjalv'\n", "cache-read"),
        ("p = '~/.cache/lidskjalv/cc_sessions'\n", "cache-read"),
        ("p = Path.home() / 'projects' / 'erik-romson' / 'lidskjalv' / 'main'\n", "checkout-path"),
        ("p = '~/projects/erik-romson/lidskjalv/main/bin'\n", "checkout-path"),
        ("subprocess.run(['sh', '~/.claude/lidskjalv.sh', 'show'])\n", "invocation"),
        ("os.system('~/.claude/lidskjalv.sh panel --json')\n", "invocation"),
        ("await asyncio.create_subprocess_exec(base / 'lidskjalv.sh')\n", "invocation"),
        ("subprocess.run(cmd, input='x', args=[f'{h}/lidskjalv.sh'])\n", "invocation"),
    ],
)
def test_the_scanner_flags_each_forbidden_pattern(snippet: str, rule: str) -> None:
    assert rule in {v.rule for v in scan_source("tests/unit/test_x.py", snippet)}


@pytest.mark.parametrize(
    "snippet",
    [
        "(home / '.claude' / 'lidskjalv.sh').unlink()\n",
        "assert fylgja_hook_event('~/.claude/lidskjalv.sh Stop') is None\n",
        "subprocess.run(['git', 'status'])\n",
        "x = 'LIDSKJALV_TRACK_SUMMARY'\n",
        "fixtures.load_json('lidskjalv/builds/runs-empty.json')\n",
    ],
)
def test_the_scanner_leaves_data_and_unrelated_calls_alone(snippet: str) -> None:
    assert scan_source("tests/unit/test_x.py", snippet) == []


def test_the_script_name_is_only_for_the_doctor_in_the_package() -> None:
    snippet = "NAME = 'lidskjalv.sh'\n"
    assert [v.rule for v in scan_source("src/fylgja/core/x.py", snippet)] == [
        "script-name-in-package"
    ]
    assert scan_source("tests/unit/test_x.py", snippet) == []
