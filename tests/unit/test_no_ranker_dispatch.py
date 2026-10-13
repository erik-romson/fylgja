"""No built-in ranker name is a dispatch key in the modules that rank (``docs/decisions/0052``).

Rankers are plugins: adding, removing or disabling one, a built-in included, needs no change to
Fylgja's code. The daemon knows only ``[items] default_ranker`` from the configuration and the
reserved ``natural`` floor, so the modules that load, run and serve rankers never compare a value
with a built-in ranker's name, never match on one and never key a mapping (or a subscript) by one.
A name in a docstring, a comment, a message or a call argument is fine.

This reuses the AST walk of ``test_no_collector_dispatch.py``. It checks an explicit list of modules
rather than all of ``api/``: ``"workspace"`` is also a summary and todo scope there
(``api/summary_inputs.py``, ``api/routes/summaries.py``, ``api/routes/todos.py``, ``api/sse.py``),
compared and used as a key for that meaning. Instead, every ``api/`` module that imports
:mod:`fylgja.rankers` must be in the list, so a new module that ranks is covered.

The built-in names are read from ``[project.entry-points."fylgja.rankers"]``.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

from test_no_collector_dispatch import dispatch_uses

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "fylgja"
CHECKED = (
    "core/daemon.py",
    "rankers/base.py",
    "rankers/registry.py",
    "api/deps.py",
    "api/item_inputs.py",
    "api/items_models.py",
    "api/now.py",
    "api/routes/items.py",
    "api/routes/now.py",
    "api/routes/views.py",
)
RANKERS_PACKAGE = "fylgja.rankers"


def builtin_rankers() -> frozenset[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return frozenset(data["project"]["entry-points"]["fylgja.rankers"])


def imports_rankers(source: str) -> bool:
    """``source`` imports :mod:`fylgja.rankers` or a module in it."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            names = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        else:
            continue
        if any(name == RANKERS_PACKAGE or name.startswith(RANKERS_PACKAGE + ".") for name in names):
            return True
    return False


def test_the_checked_modules_exist() -> None:
    for entry in CHECKED:
        assert (SOURCE / entry).is_file(), entry


def test_the_built_in_names_are_read_from_the_entry_points() -> None:
    assert {"workspace", "recent", "frecency"} <= builtin_rankers()


def test_no_ranking_module_uses_a_built_in_ranker_name_as_a_dispatch_key() -> None:
    names = builtin_rankers()
    offences = [
        f"src/fylgja/{entry}:{line}: the built-in ranker name {name!r} is {how} a value; the "
        "daemon names no ranker (docs/decisions/0052)"
        for entry in CHECKED
        for line, name, how in dispatch_uses((SOURCE / entry).read_text(encoding="utf-8"), names)
    ]
    assert not offences, "ranker name used as a dispatch key:\n" + "\n".join(offences)


def test_every_api_module_that_imports_the_rankers_is_checked() -> None:
    importing = {
        path.relative_to(SOURCE).as_posix()
        for path in sorted((SOURCE / "api").rglob("*.py"))
        if imports_rankers(path.read_text(encoding="utf-8"))
    }
    assert importing, "no api module imports fylgja.rankers: the check is looking elsewhere"
    assert importing <= set(CHECKED), sorted(importing - set(CHECKED))


def test_the_import_check_sees_every_form() -> None:
    assert imports_rankers("from fylgja.rankers.base import natural_key\n")
    assert imports_rankers("from fylgja.rankers import registry\n")
    assert imports_rankers("import fylgja.rankers.registry\n")
    assert imports_rankers("def f():\n    from fylgja.rankers.base import Ranked\n")
    assert not imports_rankers("from fylgja.rankers_extra import x\nimport fylgja.core\n")


def test_the_check_finds_each_kind_of_dispatch() -> None:
    names = frozenset({"recent", "frecency"})
    cases = {
        "if name == 'recent':\n    pass\n": "compared with",
        "x = name in ('a', 'frecency')\n": "compared with",
        "x = {'recent': 1}\n": "a mapping key",
        "x = table['frecency']\n": "a subscript",
        "match name:\n    case 'recent':\n        pass\n": "a match pattern",
        "match name:\n    case 'a' | 'frecency':\n        pass\n": "a match pattern",
    }
    for source, how in cases.items():
        uses = dispatch_uses(source, names)
        assert [u[2] for u in uses] == [how], source


def test_the_check_ignores_documentation_messages_and_arguments() -> None:
    source = (
        '"""The frecency ranker."""\n'
        "def f(name):\n"
        "    '''recent is a plugin'''\n"
        "    log('frecency failed')\n"
        "    todos_of(rows, 'recent', name)\n"
        "    PART = 'frecency'\n"
        "    return name == configured or {'k': 'recent'}\n"
    )
    assert dispatch_uses(source, frozenset({"recent", "frecency"})) == []
