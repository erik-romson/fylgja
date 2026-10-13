"""The shared ranker contract run against the example external ranker of ``fylgja-fake``.

``fake_ranker`` is an external: the contract enables it with ``[rankers.fake_ranker] enabled =
true`` and passes its one setting, which proves the whole outside path (discovery, opt-in,
settings and the contract) without a change to Fylgja.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from fylgja.testing.rankers import RankerContract


class TestFakeRankerContract(RankerContract):
    @pytest.fixture
    def ranker_name(self) -> str:
        return "fake_ranker"

    @pytest.fixture
    def ranker_settings(self) -> Mapping[str, Any]:
        return {"place_weight": 2.0}


class TestFakeRankerDefaultSettings(RankerContract):
    """The defaults alone are enough."""

    @pytest.fixture
    def ranker_name(self) -> str:
        return "fake_ranker"
