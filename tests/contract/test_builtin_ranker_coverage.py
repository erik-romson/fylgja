"""Every built-in ranker has its own contract run (``docs/decisions/0052``).

Built-ins are the ``fylgja.rankers`` entry points of the ``fylgja`` distribution, read from
metadata. Each one needs ``tests/contract/test_<name>_ranker_contract.py``.
"""

from __future__ import annotations

from pathlib import Path

from fylgja.rankers.registry import Origin, discover_rankers

CONTRACT_DIR = Path(__file__).resolve().parent


def _builtin_names() -> list[str]:
    return sorted(e.name for e in discover_rankers().entries if e.origin is Origin.BUILT_IN)


def test_there_are_builtin_rankers() -> None:
    assert {"workspace", "recent"} <= set(_builtin_names())


def test_every_builtin_ranker_has_a_contract_run() -> None:
    missing = [
        name
        for name in _builtin_names()
        if not (CONTRACT_DIR / f"test_{name}_ranker_contract.py").is_file()
    ]
    assert missing == [], (
        f"built-in rankers without tests/contract/test_<name>_ranker_contract.py: {missing}"
    )


def test_the_fake_rankers_are_not_builtins() -> None:
    assert "fake_ranker" not in _builtin_names()
    assert "fake_ranker_bomb" not in _builtin_names()
