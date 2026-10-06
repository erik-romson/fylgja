"""No built-in collector name is a dispatch key in the core (``PLAN.md`` §4.1, WP3.6).

Phase 3 added the Codex, OpenCode and norn collectors as plugins, and the daemon, the supervisor,
the registry, the ingest gate, the writer, the store and the API serve any plugin the same way.
This is the standing check behind "adding the plugins needed no change to daemon dispatch": the
named modules never compare a value with a built-in collector's name, never match on one and never
key a mapping (or a subscript) by one. A name in a docstring, a comment or a message is fine.

The built-in names are read from ``[project.entry-points."fylgja.collectors"]``, so a collector
added later is covered without an edit here.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "fylgja"
CHECKED = (
    "core/daemon.py",
    "collectors/supervisor.py",
    "collectors/registry.py",
    "core/ingest.py",
    "core/writer.py",
    "store",
    "api",
)


def builtin_names() -> frozenset[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return frozenset(data["project"]["entry-points"]["fylgja.collectors"])


def _strings(node: ast.AST | None) -> list[str]:
    """The string constants directly in ``node`` (a literal or a tuple, list or set of them)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return [value for item in node.elts for value in _strings(item)]
    return []


def dispatch_uses(source: str, names: frozenset[str]) -> list[tuple[int, str, str]]:
    """``(line, name, how)`` for each built-in name used as a dispatch key in ``source``."""
    found: list[tuple[int, str, str]] = []

    def note(node: Any, candidates: list[str], how: str) -> None:
        found.extend((node.lineno, text, how) for text in candidates if text in names)

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Compare):
            for operand in (node.left, *node.comparators):
                note(node, _strings(operand), "compared with")
        elif isinstance(node, ast.Dict):
            for key in node.keys:
                note(node, _strings(key), "a mapping key")
        elif isinstance(node, ast.Subscript):
            note(node, _strings(node.slice), "a subscript")
        elif isinstance(node, ast.MatchValue):
            note(node, _strings(node.value), "a match pattern")
        elif isinstance(node, ast.MatchMapping):
            for key in node.keys:
                note(node, _strings(key), "a mapping pattern key")
    return found


def checked_files() -> list[Path]:
    files: list[Path] = []
    for entry in CHECKED:
        path = SOURCE / entry
        files.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
    return files


def test_the_checked_modules_exist() -> None:
    assert len(checked_files()) >= 8
    for entry in CHECKED:
        assert (SOURCE / entry).exists(), entry


def test_the_built_in_names_are_read_from_the_entry_points() -> None:
    assert {"aerospace", "claude", "codex", "opencode", "norn"} <= builtin_names()


def test_no_core_module_uses_a_built_in_collector_name_as_a_dispatch_key() -> None:
    names = builtin_names()
    offences = [
        f"{path.relative_to(ROOT)}:{line}: the built-in collector name {name!r} is {how} "
        "a value; core code must work for any plugin (PLAN.md §4.1)"
        for path in checked_files()
        for line, name, how in dispatch_uses(path.read_text(encoding="utf-8"), names)
    ]
    assert not offences, "collector name used as a dispatch key:\n" + "\n".join(offences)


def test_the_check_finds_each_kind_of_dispatch() -> None:
    names = frozenset({"codex", "norn"})
    cases = {
        "if name == 'codex':\n    pass\n": "compared with",
        "x = name in ('a', 'norn')\n": "compared with",
        "x = {'codex': 1}\n": "a mapping key",
        "x = table['norn']\n": "a subscript",
        "match name:\n    case 'codex':\n        pass\n": "a match pattern",
        "match name:\n    case 'a' | 'norn':\n        pass\n": "a match pattern",
    }
    for source, how in cases.items():
        uses = dispatch_uses(source, names)
        assert [u[2] for u in uses] == [how], source


def test_the_check_ignores_documentation_and_other_strings() -> None:
    source = (
        '"""The codex collector."""\n'
        "def f(name):\n"
        "    '''norn is a plugin'''\n"
        "    log('codex failed')\n"
        "    return name == 'other' or {'k': 'norn'}\n"
    )
    assert dispatch_uses(source, frozenset({"codex", "norn"})) == []
