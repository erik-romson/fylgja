"""An example external ranker: the items with the most places first (``docs/rankers.md``).

It is off until ``[rankers.fake_ranker] enabled = true``. ``place_weight`` scales the score.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from fylgja.rankers.base import Part, Ranked, RankerPlugin, RankInput

__all__ = ["FakeRankerSettings", "plugin", "rank"]


class FakeRankerSettings(BaseModel):
    """``[rankers.fake_ranker]`` minus ``enabled``. Every field has a default."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    place_weight: float = Field(default=1.0, gt=0, le=100)
    """Points per place."""


def rank(data: RankInput, settings: FakeRankerSettings) -> list[Ranked]:
    """Most places first, then by key: a total order, whatever the input order."""
    scored = []
    for item in data.items:
        value = len(item.places) * settings.place_weight
        scored.append(Ranked(item.key, value, (Part("places", value),)))
    return sorted(scored, key=lambda r: (-r.score, r.key))


plugin = RankerPlugin(
    name="fake_ranker",
    description="Most places first",
    rank=rank,
    config_model=FakeRankerSettings,
)
