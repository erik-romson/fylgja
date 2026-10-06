"""Manual labels (WP4.2): a manual label beats the inferred one, inference keeps changing
underneath it without affecting what is shown, and clearing restores the inferred label."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from api_support import workspace_snapshot

from fylgja.core.labels import (
    EMPTY_LABEL,
    LabelService,
    effective_label,
)
from fylgja.core.live_view import Change, Collection, LiveView
from fylgja.core.writer import Tx, Writer
from fylgja.store import db, queries
from fylgja.testing.fakes import FakeClock

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


@dataclass
class Rig:
    service: LabelService
    writer: Writer
    view: LiveView
    clock: FakeClock

    async def infer(self, name: str, label: str | None) -> None:
        """What the resolver does: store the inferred label and publish the row."""
        stamp = db.to_db_time(self.clock.now())

        def job(tx: Tx) -> None:
            row = queries.set_inferred_label(tx.connection, name=name, label=label, at=stamp)
            if row is not None:
                tx.changed(Change(Collection.WORKSPACES, name, row))

        await self.writer.execute(job)

    def shown(self, name: str) -> tuple[str, str]:
        """``(label, label_source)`` as ``/now`` derives them from the live view."""
        row = self.view.workspaces[name]
        return effective_label(row.manual_label, row.inferred_label)


@pytest.fixture
async def rig(isolated_env: Path) -> AsyncIterator[Rig]:
    clock = FakeClock(T0)
    view = LiveView()
    writer = Writer(isolated_env / "labels.db", clock=clock, on_commit=view.apply)
    await writer.start()
    await writer.submit_committed(workspace_snapshot("wm", ["1", "2"], T0))
    try:
        yield Rig(LabelService(writer, clock=clock), writer, view, clock)
    finally:
        await writer.stop()


# --- effective_label ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("manual", "inferred", "expected"),
    [
        ("mine", "fylgja", ("mine", "manual")),
        ("mine", None, ("mine", "manual")),
        ("mine", EMPTY_LABEL, ("mine", "manual")),
        (None, "fylgja", ("fylgja", "inferred")),
        ("", "fylgja", ("fylgja", "inferred")),
        ("   ", "fylgja", ("fylgja", "inferred")),
        (None, None, (EMPTY_LABEL, "none")),
        (None, EMPTY_LABEL, (EMPTY_LABEL, "none")),
        (None, "  ", (EMPTY_LABEL, "none")),
    ],
)
def test_the_effective_label_and_its_source(
    manual: str | None, inferred: str | None, expected: tuple[str, str]
) -> None:
    assert effective_label(manual, inferred) == expected


# --- precedence over a live writer ----------------------------------------------------------


async def test_a_manual_label_beats_the_inferred_one(rig: Rig) -> None:
    await rig.infer("1", "fylgja")
    assert rig.shown("1") == ("fylgja", "inferred")
    row = await rig.service.set_manual("1", "billing work")
    assert (row.manual_label, row.manual_label_at) == ("billing work", T0)
    assert rig.shown("1") == ("billing work", "manual")
    assert rig.view.workspaces["1"].manual_label == "billing work"


async def test_inference_changes_underneath_without_affecting_the_effective_label(
    rig: Rig,
) -> None:
    await rig.infer("1", "fylgja")
    await rig.service.set_manual("1", "billing work")
    for inferred in ("norn", "Safari docs", None, "fylgja"):
        await rig.infer("1", inferred)
        assert rig.shown("1") == ("billing work", "manual")
        assert rig.view.workspaces["1"].inferred_label == inferred  # kept up to date underneath


async def test_clearing_restores_the_latest_inferred_label(rig: Rig) -> None:
    await rig.infer("1", "fylgja")
    await rig.service.set_manual("1", "billing work")
    await rig.infer("1", "norn")  # changed while the manual label was set
    row = await rig.service.clear_manual("1")
    assert (row.manual_label, row.manual_label_at) == (None, None)
    assert rig.shown("1") == ("norn", "inferred")


async def test_clearing_with_nothing_inferred_shows_the_dash(rig: Rig) -> None:
    await rig.service.set_manual("2", "scratch")
    await rig.service.clear_manual("2")
    assert rig.shown("2") == (EMPTY_LABEL, "none")


async def test_a_new_manual_label_replaces_the_old_one_and_moves_its_time(rig: Rig) -> None:
    await rig.service.set_manual("1", "first")
    await rig.clock.advance(60)
    row = await rig.service.set_manual("1", "second")
    assert row.manual_label == "second"
    assert row.manual_label_at is not None
    assert row.manual_label_at > T0


async def test_the_same_label_again_keeps_its_time_and_publishes_nothing(rig: Rig) -> None:
    first = await rig.service.set_manual("1", "same")
    version = rig.view.version
    await rig.clock.advance(60)
    again = await rig.service.set_manual("1", "same")
    assert again.manual_label_at == first.manual_label_at
    assert rig.view.version == version


async def test_clearing_twice_is_fine(rig: Rig) -> None:
    await rig.service.set_manual("1", "x")
    await rig.service.clear_manual("1")
    version = rig.view.version
    again = await rig.service.clear_manual("1")
    assert again.manual_label is None
    assert rig.view.version == version


async def test_a_workspace_that_re_appears_keeps_its_manual_label(rig: Rig) -> None:
    await rig.service.set_manual("1", "keep me")
    await rig.writer.submit_committed(workspace_snapshot("wm", ["1", "2"], T0))
    assert rig.shown("1") == ("keep me", "manual")
