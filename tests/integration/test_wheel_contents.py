from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.slow


@pytest.mark.timeout(600)
@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")
def test_wheel_ships_py_typed_and_testing_package(isolated_env: Path) -> None:
    out_dir = isolated_env / "wheel"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out_dir)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )
    wheels = list(out_dir.glob("fylgja-*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())
    assert "fylgja/py.typed" in names
    assert "fylgja/testing/__init__.py" in names
    assert "fylgja/collectors/opencode/fylgja.ts" in names
