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
