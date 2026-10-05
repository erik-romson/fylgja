"""``fylgja._entry``: cheap to import, and the console script hands over to Typer."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from fylgja import _entry

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_PROBE = """
import sys
before = set(sys.modules)
import fylgja._entry
stdlib = sys.stdlib_module_names
new = sorted(
    m for m in set(sys.modules) - before
    if m.split(".")[0] not in stdlib and m.split(".")[0] != "fylgja"
)
print("\\n".join(new))
print("fylgja.cli.app" in sys.modules)
"""


def test_the_entry_module_imports_no_third_party_module() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, check=True, timeout=25
    )
    assert result.stdout.split() == ["False"]


def _fake_typer_app(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    fake = ModuleType("fylgja.cli.app")
    fake.app = lambda **kwargs: calls.append(kwargs)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fylgja.cli.app", fake)
    return calls


@pytest.mark.parametrize(
    "argv", [["fylgja", "now"], ["fylgja"], ["fylgja", "--help"], ["fylgja", "now", "hook"]]
)
def test_main_hands_over_to_the_typer_app(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    calls = _fake_typer_app(monkeypatch)
    monkeypatch.setattr(sys, "argv", argv)
    _entry.main()
    assert calls == [{"prog_name": "fylgja"}]


class _HookExit(BaseException):
    """Stands in for ``os._exit``: the real hook never returns."""


@pytest.mark.parametrize(
    ("argv", "hook_argv"),
    [
        (["fylgja", "hook", "Stop"], ["Stop"]),
        (["fylgja", "hook"], []),
        (["fylgja", "hook", "--source", "codex", "Stop"], ["--source", "codex", "Stop"]),
    ],
)
def test_hook_is_dispatched_before_typer(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], hook_argv: list[str]
) -> None:
    from fylgja import hook

    typer_calls = _fake_typer_app(monkeypatch)
    seen: list[list[str]] = []

    def fake_hook_main(args: list[str]) -> None:
        seen.append(list(args))
        raise _HookExit

    monkeypatch.setattr(hook, "main", fake_hook_main)
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(_HookExit):
        _entry.main()
    assert seen == [hook_argv]
    assert typer_calls == []


_HOOK_PROBE = """
import sys
import fylgja._entry
sys.argv = ["fylgja", "hook", "--help"]
fylgja._entry.main()
"""


def test_hook_dispatch_never_returns_and_exits_zero_silently() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _HOOK_PROBE + "print('not reached')"],
        capture_output=True,
        timeout=25,
        check=False,
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")


def test_the_console_script_points_at_main() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["scripts"] == {"fylgja": "fylgja._entry:main"}


def test_python_dash_m_runs_the_real_app() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "fylgja._entry", "--help"],
        capture_output=True,
        text=True,
        check=True,
        timeout=25,
    )
    for command in ("serve", "now", "ws", "sessions", "collectors"):
        assert command in result.stdout


def test_help_imports_neither_the_daemon_nor_the_api() -> None:
    probe = (
        "import sys\n"
        "from fylgja.cli.app import app\n"
        "from typer.testing import CliRunner\n"
        "CliRunner().invoke(app, ['--help'])\n"
        "bad = [m for m in ('fastapi', 'uvicorn', 'fylgja.core.daemon', 'fylgja.api.app') "
        "if m in sys.modules]\n"
        "print(bad)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, timeout=25
    )
    assert result.stdout.strip() == "[]"
