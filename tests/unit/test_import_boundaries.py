"""Import-boundary tests: heavy front-end frameworks never load with the core modules.

Later steps add more modules to ``MODULES``.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

FORBIDDEN = ("fastapi", "starlette", "typer", "uvicorn", "click")

MODULES = [
    "fylgja.models",
    "fylgja.collectors.base",
    "fylgja.config",
    "fylgja.collectors.registry",
    "fylgja.paths",
    "fylgja.collectors.context",
    "fylgja.collectors.supervisor",
    "fylgja.privacy",
    "fylgja.store.db",
    "fylgja.store.queries",
    "fylgja.store.rows",
    "fylgja.core.live_view",
    "fylgja.core.writer",
    "fylgja.core.ingest",
    "fylgja.core.retention",
    "fylgja.core.one_liner",
    "fylgja.core.hosts",
    "fylgja.core.labels",
    "fylgja.core.project_roots",
    "fylgja.core.resolver",
    "fylgja.core.sessions",
    "fylgja.collectors.aerospace",
    "fylgja.collectors.claude",
    "fylgja.collectors.claude.inbox",
    "fylgja.collectors.claude.normalize",
    "fylgja.collectors.claude.statusline",
    "fylgja.core.system",
    "fylgja.core.daemon",
    "fylgja.api",
    "fylgja.api.now_models",
    "fylgja.api.now",
    "fylgja.logsetup",
    "fylgja.cli.client",
    "fylgja.cli.render",
    "fylgja.cli.install",
    "fylgja.cli.doctor",
    "fylgja.hook",
]

_PROBE = """
import importlib
import sys

for name in {modules!r}:
    importlib.import_module(name)

forbidden = {forbidden!r}
loaded = sorted(m for m in sys.modules if m.split(".")[0] in forbidden)
print("\\n".join(loaded))
"""


def _loaded_forbidden(modules: list[str]) -> str:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE.format(modules=modules, forbidden=FORBIDDEN)],
        capture_output=True,
        text=True,
        check=True,
        timeout=25,
    )
    return result.stdout.strip()


def test_core_modules_together_load_no_framework() -> None:
    assert _loaded_forbidden(MODULES) == ""


@pytest.mark.parametrize("module", MODULES)
def test_module_alone_loads_no_framework(module: str) -> None:
    assert _loaded_forbidden([module]) == ""


_STDLIB_PROBE = """
import sys

before = set(sys.modules)
import fylgja.paths

stdlib = sys.stdlib_module_names
new = sorted(
    m for m in set(sys.modules) - before
    if m.split(".")[0] not in stdlib and m.split(".")[0] != "fylgja"
)
print("\\n".join(new))
"""


def test_paths_loads_no_third_party_module() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _STDLIB_PROBE],
        capture_output=True,
        text=True,
        check=True,
        timeout=25,
    )
    assert result.stdout.strip() == ""


def test_paths_does_not_pull_in_the_rest_of_fylgja() -> None:
    probe = (
        "import sys, fylgja.paths\n"
        "print(sorted(m for m in sys.modules if m.startswith('fylgja')))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, timeout=25
    )
    assert result.stdout.strip() == "['fylgja', 'fylgja.paths']"


def test_hook_loads_no_third_party_module_and_only_paths() -> None:
    probe = (
        "import sys\n"
        "before = set(sys.modules)\n"
        "import fylgja.hook\n"
        "new = set(sys.modules) - before\n"
        "stdlib = sys.stdlib_module_names\n"
        "print(sorted(m for m in new if m.split('.')[0] not in stdlib and m != 'fylgja'"
        " and not m.startswith('fylgja.')))\n"
        "print(sorted(m for m in sys.modules if m.startswith('fylgja')))\n"
        "print('typing' in new)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, timeout=25
    )
    assert result.stdout.splitlines() == [
        "[]",
        "['fylgja', 'fylgja.hook', 'fylgja.paths']",
        "False",
    ]
