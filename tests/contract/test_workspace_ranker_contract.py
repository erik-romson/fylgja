"""The shared ranker contract run against the built-in ``workspace`` ranker."""

from __future__ import annotations

import pytest

from fylgja.testing.rankers import RankerContract


class TestWorkspaceRankerContract(RankerContract):
    @pytest.fixture
    def ranker_name(self) -> str:
        return "workspace"
