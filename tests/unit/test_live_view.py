"""The live view: version increments, complete snapshots closing missing entities, change
notification and rebuilding from the store (§3.1)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from fylgja import paths
from fylgja.core.live_view import (
    FOCUS_KEY,
    Change,
    Collection,
    LiveUpdate,
    LiveView,
    load_changes,
)
from fylgja.core.writer import Writer
from fylgja.models import (
    SessionPayload,
    SessionSnapshot,
    SnapshotItem,
    WindowPayload,
    WindowSnapshot,
)
from fylgja.store import db
from fylgja.store.rows import FocusRow, HealthRow, WindowRow

pytestmark = pytest.mark.timeout(20)

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


class Clock:
    def now(self) -> datetime:
        return T0

    def monotonic(self) -> float:
        return 0.0

    async def sleep(self, seconds: float) -> None:  # pragma: no cover - unused
        raise AssertionError


def window_row(wid: str, title: str = "t") -> WindowRow:
    return WindowRow(
        window_id=wid,
        source="aero",
        workspace="1",
        app_bundle_id=None,
        app_name="App",
        app_pid=1,
        title=title,
        valid_from=T0,
    )


def windows(seconds: float, *ids: str, complete: bool = True) -> WindowSnapshot:
    return WindowSnapshot(
        source="aero",
        entity_id="all",
        observed_at=T0 + timedelta(seconds=seconds),
        complete=complete,
        items=tuple(
            SnapshotItem(entity_id=wid, payload=WindowPayload(workspace="1", title=f"t {wid}"))
            for wid in ids
        ),
    )


@pytest.fixture
async def stack() -> AsyncIterator[tuple[Writer, LiveView]]:
    view = LiveView()
    writer = Writer(paths.database_file(), clock=Clock(), on_commit=view.apply)
    await writer.start()
    try:
        yield writer, view
    finally:
        await writer.stop()


# --------------------------------------------------------------------------------------------
# Versions
# --------------------------------------------------------------------------------------------


def test_each_effective_apply_increments_the_version_by_one() -> None:
    view = LiveView()
    assert view.version == 0
    update = view.apply([Change(Collection.WINDOWS, "w1", window_row("w1"))])
    assert update is not None
    assert update.version == view.version == 1
    second = view.apply(
        [
            Change(Collection.WINDOWS, "w2", window_row("w2")),
            Change(Collection.WINDOWS, "w3", window_row("w3")),
        ]
    )
    assert second is not None
    assert second.version == 2  # one increment per apply, not per change
    assert second.collections == frozenset({Collection.WINDOWS})


def test_an_apply_that_changes_nothing_keeps_the_version() -> None:
    view = LiveView()
    view.apply([Change(Collection.WINDOWS, "w1", window_row("w1"))])
    assert view.apply([Change(Collection.WINDOWS, "w1", window_row("w1"))]) is None
    assert view.apply([Change(Collection.WINDOWS, "missing", None)]) is None
    assert view.apply([]) is None
    assert view.version == 1


def test_fields_outside_equality_refresh_silently() -> None:
    view = LiveView()
    health = HealthRow("aero", "ok", T0, T0, 0, None)
    view.apply([Change(Collection.COLLECTORS, "aero", health)])
    later = T0 + timedelta(seconds=30)
    focus = FocusRow("aero", "w1", "1", None, None, T0)
    view.apply([Change(Collection.FOCUS, FOCUS_KEY, focus)])
    version = view.version
    assert view.apply([Change(Collection.FOCUS, FOCUS_KEY, replace(focus))]) is None
    moved = view.apply([Change(Collection.FOCUS, FOCUS_KEY, replace(focus, observed_at=later))])
    assert moved is not None  # observed_at is part of a focus row's identity
    assert view.version == version + 1
    assert view.focused is not None
    assert view.focused.observed_at == later
    assert view.collectors["aero"] == health


async def test_last_seen_moves_without_a_new_version(stack: tuple[Writer, LiveView]) -> None:
    writer, view = stack

    def listing(seconds: float) -> SessionSnapshot:
        return SessionSnapshot(
            source="claude",
            entity_id="registry",
            observed_at=T0 + timedelta(seconds=seconds),
            complete=True,
            items=(
                SnapshotItem(
                    entity_id="claude:a", payload=SessionPayload(kind="claude", native_id="a")
                ),
            ),
        )

    await writer.submit_committed(listing(0))
    version = view.version
    await writer.submit_committed(listing(2))
    assert view.version == version
    assert view.sessions["claude:a"].last_seen == T0 + timedelta(seconds=2)


def test_a_change_must_carry_its_collection_row_type() -> None:
    with pytest.raises(TypeError, match="WindowRow"):
        Change(Collection.WINDOWS, "x", HealthRow("aero", "ok", T0, None, 0, None))
    Change(Collection.WINDOWS, "x", None)  # a removal carries no row


# --------------------------------------------------------------------------------------------
# Complete snapshots close missing entities
# --------------------------------------------------------------------------------------------


async def test_a_complete_snapshot_closes_missing_entities(
    stack: tuple[Writer, LiveView],
) -> None:
    writer, view = stack
    await writer.submit_committed(windows(0, "w1", "w2", "w3"))
    assert set(view.windows) == {"w1", "w2", "w3"}
    opened = view.version
    await writer.submit_committed(windows(30, "w1", "w3"))
    assert set(view.windows) == {"w1", "w3"}
    assert view.version == opened + 1
    reader = db.connect_reader(paths.database_file())
    try:
        closed = reader.execute(
            "SELECT valid_to FROM windows_snapshot WHERE window_id = 'w2'"
        ).fetchone()[0]
    finally:
        reader.close()
    assert closed == db.to_db_time(T0 + timedelta(seconds=30))


async def test_an_incomplete_snapshot_closes_nothing(stack: tuple[Writer, LiveView]) -> None:
    writer, view = stack
    await writer.submit_committed(windows(0, "w1", "w2"))
    version = view.version
    await writer.submit_committed(windows(30, "w1", complete=False))
    assert set(view.windows) == {"w1", "w2"}
    assert view.version == version  # w1 unchanged, nothing closed


async def test_an_empty_complete_snapshot_closes_everything(
    stack: tuple[Writer, LiveView],
) -> None:
    writer, view = stack
    await writer.submit_committed(windows(0, "w1", "w2"))
    await writer.submit_committed(windows(30))
    assert dict(view.windows) == {}


# --------------------------------------------------------------------------------------------
# Change notification
# --------------------------------------------------------------------------------------------


async def test_subscribers_are_notified_of_committed_changes(
    stack: tuple[Writer, LiveView],
) -> None:
    writer, view = stack
    updates: list[LiveUpdate] = []
    unsubscribe = view.subscribe(updates.append)
    await writer.submit_committed(windows(0, "w1", "w2"))
    await writer.submit_committed(windows(30, "w1"))
    assert [u.version for u in updates] == [1, 2]
    assert {(c.collection, c.key) for c in updates[0].changes} == {
        (Collection.WINDOWS, "w1"),
        (Collection.WINDOWS, "w2"),
    }
    assert [(c.key, c.row) for c in updates[1].changes] == [("w2", None)]
    await writer.submit_committed(windows(60, "w1"))  # nothing changed: no notification
    assert len(updates) == 2
    unsubscribe()
    unsubscribe()  # idempotent
    await writer.submit_committed(windows(90))
    assert len(updates) == 2


def test_a_failing_subscriber_does_not_stop_the_others() -> None:
    view = LiveView()
    seen: list[int] = []

    def broken(update: LiveUpdate) -> None:
        raise RuntimeError("subscriber bug")

    view.subscribe(broken)
    view.subscribe(lambda update: seen.append(update.version))
    view.apply([Change(Collection.WINDOWS, "w1", window_row("w1"))])
    assert seen == [1]


# --------------------------------------------------------------------------------------------
# Rebuild and snapshots
# --------------------------------------------------------------------------------------------


async def test_rebuild_from_the_store(stack: tuple[Writer, LiveView]) -> None:
    writer, view = stack
    await writer.submit_committed(windows(0, "w1", "w2"))
    restarted = LiveView()
    restarted.apply([Change(Collection.WINDOWS, "stale", window_row("stale"))])
    reader = db.connect_reader(paths.database_file())
    try:
        update = restarted.rebuild(reader)
        assert restarted.rebuild(reader) is None  # already in step
        assert len(load_changes(reader)) == 2
    finally:
        reader.close()
    assert update is not None
    assert ("stale", None) in [(c.key, c.row) for c in update.changes]
    assert dict(restarted.windows) == dict(view.windows)
    assert restarted.focused is None


def test_snapshot_is_an_immutable_copy() -> None:
    view = LiveView()
    view.apply([Change(Collection.WINDOWS, "w1", window_row("w1"))])
    snap = view.snapshot()
    view.apply([Change(Collection.WINDOWS, "w2", window_row("w2"))])
    assert set(snap.windows) == {"w1"}
    assert snap.version == 1
    with pytest.raises(TypeError):
        snap.windows["x"] = window_row("x")  # type: ignore[index]
    with pytest.raises(TypeError):
        view.windows["x"] = window_row("x")  # type: ignore[index]
    assert dict(view.table(Collection.WINDOWS)) == dict(view.windows)
    assert dict(view.workspaces) == dict(view.projects) == dict(view.usage) == {}
