"""Every built-in collector has its own contract run (``PLAN.md`` §4.9).

Built-ins are the ``fylgja.collectors`` entry points of the ``fylgja`` distribution, read from
metadata. Each one needs ``tests/contract/test_<name>_contract.py``.
"""

from __future__ import annotations

from pathlib import Path

from fylgja.collectors.registry import Origin, discover

CONTRACT_DIR = Path(__file__).resolve().parent


def _builtin_names() -> list[str]:
    return sorted(e.name for e in discover().entries if e.origin is Origin.BUILT_IN)


def test_every_builtin_collector_has_a_contract_run() -> None:
    missing = [
        name
        for name in _builtin_names()
        if not (CONTRACT_DIR / f"test_{name}_contract.py").is_file()
    ]
    assert missing == [], (
        f"built-in collectors without tests/contract/test_<name>_contract.py: {missing}"
    )


def test_the_fake_plugin_is_not_a_builtin() -> None:
    assert "fake" not in _builtin_names()
    assert "fake_import_bomb" not in _builtin_names()
