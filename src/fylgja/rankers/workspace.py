"""The built-in ``workspace`` ranker: workspace number order, as today's cards.

It is the :func:`~fylgja.rankers.base.natural_order` floor offered as a selectable ranker: items by
the natural order of their newest open place's workspace, then by key, with the part
``workspace <n>``. Its workspace order is the natural order. It has no settings.
"""

from __future__ import annotations

from pydantic import BaseModel

from fylgja.rankers.base import (
    Ranked,
    RankerPlugin,
    RankInput,
    natural_order,
    natural_workspace_order,
)

__all__ = ["plugin", "rank"]


def rank(data: RankInput, settings: BaseModel) -> list[Ranked]:
    """The floor ordering (see the module docstring)."""
    return natural_order(data)


plugin = RankerPlugin(
    name="workspace",
    description="Workspace number order, as today's workspace cards",
    rank=rank,
    order_workspaces=natural_workspace_order,
)
