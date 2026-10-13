"""The shared ranker contract run against the built-in ``frecency`` ranker."""

from __future__ import annotations

import pytest

from fylgja.testing.rankers import RankerContract


class TestFrecencyRankerContract(RankerContract):
    @pytest.fixture
    def ranker_name(self) -> str:
        return "frecency"
