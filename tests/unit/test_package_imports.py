from __future__ import annotations

import subprocess
import sys

_PROBE = """
import sys

before = set(sys.modules)
import fylgja

new = set(sys.modules) - before
stdlib = set(sys.stdlib_module_names)
foreign = sorted(
    name for name in new
    if name.split(".")[0] not in stdlib and name.split(".")[0] != "fylgja"
)
print("\\n".join(foreign))
"""


def test_import_fylgja() -> None:
    import fylgja

    assert fylgja.__name__ == "fylgja"


def test_import_fylgja_testing() -> None:
    import fylgja.testing

    assert fylgja.testing.__doc__


def test_importing_fylgja_pulls_in_no_third_party_module() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, check=True, timeout=20
    )
    assert result.stdout.strip() == ""
