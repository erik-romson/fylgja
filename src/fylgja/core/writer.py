"""The single SQLite writer (``PLAN-final-plan.md`` §4.1, §3.3, A20; ``PLAN.md`` §5).

Everything the daemon stores goes through one :class:`Writer`. Collectors never reach it
directly: they get an ingest-gate sink (:mod:`fylgja.core.ingest`), which validates and redacts
records before they are queued here.

**Batching.** One asyncio task drains a FIFO queue. A transaction takes what is queued, at most
``max_batch`` (500) items, and stops collecting after ``max_delay`` (200 ms, measured on the
injected clock's monotonic time). It never sits waiting for more items, so a lone record commits
at once while a burst shares transactions. The
transaction runs in a worker thread through ``asyncio.to_thread``; one transaction runs at a time.
Each item runs under its own savepoint, so a failing item is rolled back and reported without
losing the rest of its batch.

**Futures.** :meth:`Writer.submit_committed` (behind ``emit_committed``) and
:meth:`Writer.execute` return only after the ``COMMIT`` of the transaction holding their item has
returned. A failed item raises its error to the caller; a stopped writer raises
:class:`WriterClosed`.

**What is stored.**

* Every :class:`~fylgja.models.Event` is appended to ``events`` with
  ``ON CONFLICT (dedup_key) DO NOTHING``. A duplicate (for example a replayed hook envelope) is
  reported as :attr:`Outcome.DUPLICATE` and changes nothing else.
* Records are then projected by entity kind (and what a complete snapshot does):

  - window: a ``windows_snapshot`` change interval (closes missing windows);
  - workspace: the ``workspaces`` row, present, monitor and focus (marks missing ones gone);
  - session: the ``sessions`` row, merged (ends missing sessions);
  - session_activity: a ``session_snapshots`` change interval, and the state into ``sessions``
    (closes missing intervals);
  - project: the ``projects`` row, merged;
  - build: ``projects.build*`` (§4.7);
  - usage: a ``usage_snapshots`` change interval (§4.7, closes missing ones);
  - focus, events only: the live view's focused window.

  Snapshots of the kinds without a projection (todo, context item, presence, focus) are appended
  to ``events`` like events, so a later projection can replay them.

* **Change intervals (A20)** follow :mod:`fylgja.store.queries`: a record carries the full current
  state of an interval entity, an unchanged state writes nothing, a changed one closes the open
  interval and opens a new one, and an observation older than the stored state is stale. Entity
  rows (sessions, projects, workspaces) *merge*: a ``None`` field keeps the stored value, because
  other parts of the core also fill those rows.
* A ``Snapshot`` with ``complete=True`` closes the entities of that kind *and that collector* that
  it does not list, as of its ``observed_at``.

**Live view.** After each commit the writer hands the transaction's changes to ``on_commit`` (the
live view) and only then resolves the futures, so an awaited write is already visible there.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, cast

from pydantic import BaseModel

from fylgja import paths
from fylgja.collectors.base import Clock
from fylgja.collectors.context import SystemClock
from fylgja.core.live_view import FOCUS_KEY, Change, Collection
from fylgja.core.one_liner import session_one_liner
from fylgja.models import (
    BuildPayload,
    EntityKind,
    ProjectPayload,
    Record,
    SessionActivity,
    SessionPayload,
    Snapshot,
    UsagePayload,
    WindowPayload,
    WorkspacePayload,
)
from fylgja.store import db, queries
from fylgja.store.queries import PutStatus
from fylgja.store.rows import HealthRow, SessionRow

__all__ = [
    "MAX_BATCH",
    "MAX_DELAY",
    "Outcome",
    "Tx",
    "Writer",
    "WriterClosed",
    "WriterStats",
    "apply_record",
    "session_key",
    "split_session_key",
]

logger = logging.getLogger("fylgja.writer")

MAX_BATCH: Final = 500
MAX_DELAY: Final = 0.2


class WriterClosed(RuntimeError):
    """The writer is stopped and accepts nothing more."""


class Outcome(StrEnum):
    STORED = "stored"
    DUPLICATE = "duplicate"
    """The dedup key was already stored; nothing was written."""


class Tx:
    """What a write job sees: the connection inside the open transaction (unless it asked to run
    outside one) and the live-view changes it reports."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.changes: list[Change] = []

    def changed(self, change: Change) -> None:
        self.changes.append(change)


@dataclass
class WriterStats:
    transactions: int = 0
    items: int = 0
    duplicates: int = 0
    failed_items: int = 0
    failed_transactions: int = 0
    batch_sizes: deque[int] = field(default_factory=lambda: deque(maxlen=1024))
    """Item count of each recent transaction, oldest first."""


# --------------------------------------------------------------------------------------------
# Session keys
# --------------------------------------------------------------------------------------------


def session_key(kind: str, native_id: str) -> str:
    """``kind:native_id`` (§4.1)."""
    return f"{kind}:{native_id}"


def split_session_key(key: str, *, default_kind: str) -> tuple[str, str]:
    """``(kind, native_id)`` of a session key; a key without a kind gets ``default_kind``."""
    kind, sep, native_id = key.partition(":")
    if sep and kind and native_id:
        return kind, native_id
    return default_kind, key


def _activity_key(entity_id: str, source: str) -> str:
    """The session key a ``session_activity`` record is about (``kind:native_id``)."""
    return session_key(*split_session_key(entity_id, default_kind=source))


# --------------------------------------------------------------------------------------------
# Projection (runs in the writer thread, inside the transaction)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Obs:
    source: str
    entity_id: str
    at: str
    listed: bool
    """The observation is an item of a snapshot (rather than an event)."""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _columns(payload: BaseModel) -> dict[str, Any]:
    """Payload fields as column values: times as stored text, nested values as JSON text."""
    plain = payload.model_dump()
    as_json = payload.model_dump(mode="json")
    columns: dict[str, Any] = {}
    for name, value in plain.items():
        if isinstance(value, datetime):
            columns[name] = db.to_db_time(value)
        elif isinstance(value, dict | list | tuple):
            columns[name] = _json(as_json[name])
        else:
            columns[name] = value
    return columns


def _as[P: BaseModel](payload: BaseModel, kind: type[P]) -> P:
    if not isinstance(payload, kind):
        raise TypeError(f"expected {kind.__name__}, got {type(payload).__name__}")
    return payload


def _window(tx: Tx, obs: _Obs, payload: BaseModel) -> None:
    window = _as(payload, WindowPayload)
    put = queries.put_window(
        tx.connection,
        source=obs.source,
        window_id=obs.entity_id,
        values=_columns(window),
        at=obs.at,
    )
    if put.status is PutStatus.OPENED and put.row is not None:
        tx.changed(Change(Collection.WINDOWS, obs.entity_id, put.row))


def _close_windows(tx: Tx, record: Snapshot[Any], at: str) -> None:
    present = {item.entity_id for item in record.items}
    for window_id in queries.close_missing_windows(
        tx.connection, source=record.source, present=present, at=at
    ):
        tx.changed(Change(Collection.WINDOWS, window_id, None))


def _workspace(tx: Tx, obs: _Obs, payload: BaseModel) -> None:
    workspace = _as(payload, WorkspacePayload)
    row = queries.upsert_workspace(
        tx.connection,
        name=workspace.name,
        source=obs.source,
        monitor=workspace.monitor,
        focused=workspace.focused,
        at=obs.at,
    )
    if row.closed_at is None:
        tx.changed(Change(Collection.WORKSPACES, row.name, row))
    if workspace.focused and row.focused:
        for other in queries.unfocus_other_workspaces(
            tx.connection, source=obs.source, focused=workspace.name, at=obs.at
        ):
            tx.changed(Change(Collection.WORKSPACES, other.name, other))


def _close_workspaces(tx: Tx, record: Snapshot[Any], at: str) -> None:
    present = {item.payload.name for item in record.items}
    for name in queries.close_workspaces_missing(
        tx.connection, source=record.source, present=present, at=at
    ):
        tx.changed(Change(Collection.WORKSPACES, name, None))


def _focus(tx: Tx, obs: _Obs, payload: BaseModel) -> None:
    row = queries.latest_focus(tx.connection)
    if row is not None:
        tx.changed(Change(Collection.FOCUS, FOCUS_KEY, row))


def _end_session(tx: Tx, key: str, at: str) -> None:
    queries.close_open_activity(tx.connection, session_key=key, at=at)
    tx.changed(Change(Collection.SESSIONS, key, None))
    tx.changed(Change(Collection.ACTIVITY, key, None))


def _publish_session(tx: Tx, row: SessionRow) -> None:
    if row.ended_at is None:
        tx.changed(Change(Collection.SESSIONS, row.session_key, row))
    else:
        _end_session(tx, row.session_key, db.to_db_time(row.ended_at))


def _session(tx: Tx, obs: _Obs, payload: BaseModel) -> None:
    session = _as(payload, SessionPayload)
    key = session_key(session.kind, session.native_id)
    values = _columns(session)
    started = values.get("started_at")
    first_seen = obs.at if started is None else min(started, obs.at)
    row = queries.upsert_session(
        tx.connection,
        session_key=key,
        kind=session.kind,
        native_id=session.native_id,
        source=obs.source,
        values=values,
        first_seen=first_seen,
        at=obs.at,
        listed=obs.listed,
    )
    _publish_session(tx, row)


def _end_sessions(tx: Tx, record: Snapshot[Any], at: str) -> None:
    present = {session_key(item.payload.kind, item.payload.native_id) for item in record.items}
    for key in queries.end_missing_sessions(
        tx.connection, source=record.source, present=present, at=at
    ):
        _end_session(tx, key, at)


def _activity(tx: Tx, obs: _Obs, payload: BaseModel) -> None:
    activity = _as(payload, SessionActivity)
    kind, native_id = split_session_key(obs.entity_id, default_kind=obs.source)
    key = session_key(kind, native_id)
    connection = tx.connection
    queries.ensure_session(
        connection, session_key=key, kind=kind, native_id=native_id, source=obs.source, at=obs.at
    )
    before = queries.get_session(connection, key)
    if before is None or before.ended_at is not None:
        return  # activity of an ended session is not recorded
    columns = _columns(activity)
    # The title may have arrived with an earlier record; the one-liner uses the session's latest.
    known = activity if activity.title else activity.model_copy(update={"title": before.title})
    one_liner, _ = session_one_liner(known)
    values = {
        "state": activity.state,
        "waiting_reason": activity.waiting_reason,
        "last_tool": activity.current_tool,
        "current_target": activity.current_target,
        "open_task": activity.open_task,
        "stage": columns["stage"],
        "last_prompt": activity.last_prompt,
        "last_reply": activity.last_reply,
        "one_liner": one_liner,
    }
    put = queries.put_activity(
        connection, session_key=key, source=obs.source, values=values, at=obs.at
    )
    if put.status is PutStatus.STALE:
        return
    queries.update_session_from_activity(
        connection,
        session_key=key,
        state=activity.state,
        waiting_reason=activity.waiting_reason,
        title=activity.title,
        branch=activity.branch,
        at=obs.at,
    )
    after = queries.get_session(connection, key)
    if after is not None:
        tx.changed(Change(Collection.SESSIONS, key, after))
    if put.status is PutStatus.OPENED and put.row is not None:
        tx.changed(Change(Collection.ACTIVITY, key, put.row))


def _close_activity(tx: Tx, record: Snapshot[Any], at: str) -> None:
    present = {_activity_key(item.entity_id, record.source) for item in record.items}
    for key in queries.close_missing_activity(
        tx.connection, source=record.source, present=present, at=at
    ):
        tx.changed(Change(Collection.ACTIVITY, key, None))


def _project(tx: Tx, obs: _Obs, payload: BaseModel) -> None:
    project = _as(payload, ProjectPayload)
    row = queries.upsert_project(
        tx.connection, path=project.path, values=_columns(project), at=obs.at
    )
    tx.changed(Change(Collection.PROJECTS, row.path, row))


def _build(tx: Tx, obs: _Obs, payload: BaseModel) -> None:
    build = _as(payload, BuildPayload)
    path = build.project_path or obs.entity_id
    fold = build.model_dump(mode="json", exclude_none=True, exclude={"project_path", "error_kind"})
    row = queries.upsert_build(
        tx.connection,
        path=path,
        build_state=build.state,
        build_json=_json(fold),
        error_kind=build.error_kind,
        at=obs.at,
    )
    tx.changed(Change(Collection.PROJECTS, row.path, row))


def _usage(tx: Tx, obs: _Obs, payload: BaseModel) -> None:
    usage = _as(payload, UsagePayload)
    columns = _columns(usage)
    values = {
        "used_pct": usage.used_pct,
        "resets_at": columns["resets_at"],
        "cost_usd": usage.cost_usd,
        "cost_period": usage.cost_period,
        "source": usage.usage_source or obs.source,
    }
    put = queries.put_usage(
        tx.connection,
        collector=obs.source,
        provider=usage.provider,
        window=usage.window or "",
        values=values,
        at=obs.at,
    )
    if put.status is PutStatus.OPENED and put.row is not None:
        tx.changed(Change(Collection.USAGE, put.row.key, put.row))


def _close_usage(tx: Tx, record: Snapshot[Any], at: str) -> None:
    present = {(item.payload.provider, item.payload.window or "") for item in record.items}
    for key in queries.close_missing_usage(
        tx.connection, collector=record.source, present=present, at=at
    ):
        tx.changed(Change(Collection.USAGE, key, None))


ItemProjector = Callable[[Tx, _Obs, BaseModel], None]
Closer = Callable[[Tx, Snapshot[Any], str], None]


@dataclass(frozen=True)
class _Projection:
    item: ItemProjector
    close_missing: Closer | None = None


_PROJECTIONS: Final[Mapping[EntityKind, _Projection]] = {
    EntityKind.WINDOW: _Projection(_window, _close_windows),
    EntityKind.WORKSPACE: _Projection(_workspace, _close_workspaces),
    EntityKind.SESSION: _Projection(_session, _end_sessions),
    EntityKind.SESSION_ACTIVITY: _Projection(_activity, _close_activity),
    EntityKind.PROJECT: _Projection(_project),
    EntityKind.BUILD: _Projection(_build),
    EntityKind.USAGE: _Projection(_usage, _close_usage),
}

_EVENT_ONLY: Final[Mapping[EntityKind, ItemProjector]] = {EntityKind.FOCUS: _focus}


def _store_raw(tx: Tx, record: Record, received_at: str) -> Outcome:
    payload: Any
    if isinstance(record, Snapshot):
        payload = {
            "complete": record.complete,
            "items": [
                {
                    "entity_id": item.entity_id,
                    "payload": item.payload.model_dump(mode="json", exclude_none=True),
                }
                for item in record.items
            ],
        }
    else:
        payload = record.payload.model_dump(mode="json", exclude_none=True)
    inserted = queries.insert_event(
        tx.connection,
        dedup_key=record.dedup_key,
        source=record.source,
        record_type=record.record_type,
        entity_kind=record.entity_kind.value,
        entity_id=record.entity_id,
        observed_at=db.to_db_time(record.observed_at),
        received_at=received_at,
        payload_json=_json(payload),
    )
    return Outcome.STORED if inserted else Outcome.DUPLICATE


def apply_record(tx: Tx, record: Record, received_at: str) -> Outcome:
    """Store one validated record (see the module docstring for what that means per kind)."""
    at = db.to_db_time(record.observed_at)
    projection = _PROJECTIONS.get(record.entity_kind)
    if isinstance(record, Snapshot):
        if projection is None:
            return _store_raw(tx, record, received_at)
        for item in record.items:
            projection.item(tx, _Obs(record.source, item.entity_id, at, True), item.payload)
        if record.complete and projection.close_missing is not None:
            projection.close_missing(tx, record, at)
        return Outcome.STORED
    outcome = _store_raw(tx, record, received_at)
    if outcome is Outcome.DUPLICATE:
        return outcome
    observation = _Obs(record.source, record.entity_id, at, False)
    handler = projection.item if projection is not None else _EVENT_ONLY.get(record.entity_kind)
    if handler is not None:
        handler(tx, observation, record.payload)
    return Outcome.STORED


def _save_health(tx: Tx, row: HealthRow) -> None:
    queries.put_health(
        tx.connection,
        name=row.name,
        state=row.state,
        since=db.to_db_time(row.since),
        last_ok_at=None if row.last_ok_at is None else db.to_db_time(row.last_ok_at),
        restarts=row.restarts,
        last_error_kind=row.last_error_kind,
    )
    tx.changed(Change(Collection.COLLECTORS, row.name, row))


def _noop(tx: Tx) -> None:
    return None


# --------------------------------------------------------------------------------------------
# The writer task
# --------------------------------------------------------------------------------------------

Job = Callable[[Tx], Any]


class _Item:
    __slots__ = ("exclusive", "future", "job", "label")

    def __init__(
        self,
        job: Job,
        future: asyncio.Future[Any] | None,
        *,
        exclusive: bool = False,
        label: str = "job",
    ) -> None:
        self.job = job
        self.future = future
        self.exclusive = exclusive
        self.label = label


@dataclass(frozen=True)
class _Result:
    value: Any = None
    error: Exception | None = None


class Writer:
    """The only SQLite writer. See the module docstring."""

    def __init__(
        self,
        database: Path | None = None,
        *,
        clock: Clock | None = None,
        max_batch: int = MAX_BATCH,
        max_delay: float = MAX_DELAY,
        on_commit: Callable[[Sequence[Change]], object] | None = None,
    ) -> None:
        if max_batch < 1:
            raise ValueError("max_batch must be at least 1")
        if max_delay < 0:
            raise ValueError("max_delay must not be negative")
        self._database = database
        self._clock: Clock = SystemClock() if clock is None else clock
        self._max_batch = max_batch
        self._max_delay = max_delay
        self._on_commit = on_commit
        self._queue: asyncio.Queue[_Item | None] = asyncio.Queue()
        self._carry: _Item | None = None
        self._stop_seen = False
        self._closing = False
        self._connection: sqlite3.Connection | None = None
        self._task: asyncio.Task[None] | None = None
        self._inflight: asyncio.Future[tuple[list[_Result], list[Change]]] | None = None
        self.stats = WriterStats()

    # -- state --------------------------------------------------------------------------------

    @property
    def database(self) -> Path:
        return paths.database_file() if self._database is None else self._database

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def idle(self) -> bool:
        """Nothing is queued and no transaction is running."""
        return self._queue.empty() and self._carry is None and self._inflight is None

    @property
    def pending(self) -> int:
        return self._queue.qsize() + (0 if self._carry is None else 1)

    # -- lifecycle ----------------------------------------------------------------------------

    async def start(self) -> None:
        """Open the database (creating and migrating it) and start the writer task."""
        if self._closing:
            raise WriterClosed("writer is stopped")
        if self._task is not None:
            raise RuntimeError("writer already started")
        self._connection = await asyncio.to_thread(db.open_store, self.database)
        self._task = asyncio.create_task(self._run(), name="fylgja-writer")

    async def stop(self) -> None:
        """Commit everything queued so far, then close. Later submissions raise
        :class:`WriterClosed`."""
        if self._closing:
            return
        self._closing = True
        task = self._task
        if task is not None and not task.done():
            self._queue.put_nowait(None)
            await asyncio.wait([task])
        if self._inflight is not None:
            await asyncio.wait([self._inflight])
            self._inflight = None
        self._fail_pending(WriterClosed("writer stopped"))
        connection, self._connection = self._connection, None
        if connection is not None:
            await asyncio.to_thread(connection.close)

    # -- submitting ---------------------------------------------------------------------------

    def submit(self, record: Record, *, received_at: datetime | None = None) -> None:
        """Queue a validated record without waiting for it."""
        self._put(self._record_item(record, received_at, None))

    async def submit_committed(
        self, record: Record, *, received_at: datetime | None = None
    ) -> Outcome:
        """Queue a validated record and return once it is committed."""
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._put(self._record_item(record, received_at, future))
        return cast(Outcome, await future)

    async def execute[T](self, job: Callable[[Tx], T], *, transaction: bool = True) -> T:
        """Run ``job`` in the writer and return its result once committed.

        With ``transaction=False`` the job runs alone and outside a transaction (needed for
        ``wal_checkpoint``). Changes it reports through :meth:`Tx.changed` reach the live view.
        """
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._put(_Item(job, future, exclusive=not transaction))
        return cast(T, await future)

    def save_health(self, row: HealthRow) -> None:
        """Queue a ``collector_health`` update."""
        self._put(_Item(lambda tx: _save_health(tx, row), None, label="health"))

    async def flush(self) -> None:
        """Return once everything queued before this call is committed."""
        await self.execute(_noop)

    def _record_item(
        self,
        record: Record,
        received_at: datetime | None,
        future: asyncio.Future[Any] | None,
    ) -> _Item:
        stamp = db.to_db_time(self._clock.now() if received_at is None else received_at)

        def job(tx: Tx) -> Outcome:
            return apply_record(tx, record, stamp)

        return _Item(job, future, label=f"{record.record_type}:{record.entity_kind.value}")

    def _put(self, item: _Item) -> None:
        if self._closing:
            raise WriterClosed("writer is stopped")
        self._queue.put_nowait(item)

    # -- the task -----------------------------------------------------------------------------

    async def _run(self) -> None:
        clock = self._clock
        while True:
            if self._carry is not None:
                first, self._carry = self._carry, None
            elif self._stop_seen:
                return
            else:
                taken = await self._queue.get()
                if taken is None:
                    return
                first = taken
            if first.exclusive:
                await self._process([first], exclusive=True)
                continue
            batch = [first]
            started = clock.monotonic()
            while len(batch) < self._max_batch and clock.monotonic() - started < self._max_delay:
                try:
                    following = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0)  # let producers that are ready add to this batch
                    if self._queue.empty():
                        break
                    continue
                if following is None:
                    self._stop_seen = True
                    break
                if following.exclusive:
                    self._carry = following
                    break
                batch.append(following)
            await self._process(batch, exclusive=False)

    async def _process(self, batch: list[_Item], *, exclusive: bool) -> None:
        inflight = asyncio.ensure_future(asyncio.to_thread(self._commit, batch, exclusive))
        self._inflight = inflight
        try:
            results, changes = await asyncio.shield(inflight)
        except asyncio.CancelledError:
            # The thread may still finish the commit; stop() waits for it before closing.
            self._resolve_all(batch, WriterClosed("writer cancelled"))
            raise
        except Exception as exc:
            self._inflight = None
            self.stats.failed_transactions += 1
            logger.error(
                "write transaction of %d item(s) failed: %s", len(batch), type(exc).__name__
            )
            self._resolve_all(batch, exc)
            return
        self._inflight = None
        self.stats.transactions += 1
        self.stats.items += len(batch)
        self.stats.batch_sizes.append(len(batch))
        if changes and self._on_commit is not None:
            try:
                self._on_commit(changes)
            except Exception:
                logger.exception("live view update failed")
        for item, result in zip(batch, results, strict=True):
            if result.error is None:
                if result.value is Outcome.DUPLICATE:
                    self.stats.duplicates += 1
                if item.future is not None and not item.future.done():
                    item.future.set_result(result.value)
                continue
            self.stats.failed_items += 1
            if item.future is not None:
                if not item.future.done():
                    item.future.set_exception(result.error)
            else:
                logger.error("write of %s failed: %s", item.label, type(result.error).__name__)

    def _commit(self, batch: list[_Item], exclusive: bool) -> tuple[list[_Result], list[Change]]:
        connection = self._connection
        if connection is None:
            raise WriterClosed("writer is not open")
        tx = Tx(connection)
        if exclusive:
            try:
                value = batch[0].job(tx)
            except Exception as exc:
                return [_Result(error=exc)], []
            return [_Result(value=value)], tx.changes
        results: list[_Result] = []
        connection.execute("BEGIN IMMEDIATE")
        try:
            for item in batch:
                connection.execute("SAVEPOINT item")
                mark = len(tx.changes)
                try:
                    value = item.job(tx)
                except Exception as exc:
                    connection.execute("ROLLBACK TO SAVEPOINT item")
                    connection.execute("RELEASE SAVEPOINT item")
                    del tx.changes[mark:]
                    results.append(_Result(error=exc))
                else:
                    connection.execute("RELEASE SAVEPOINT item")
                    results.append(_Result(value=value))
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                with contextlib.suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
            raise
        return results, tx.changes

    def _resolve_all(self, batch: list[_Item], error: Exception) -> None:
        for item in batch:
            if item.future is not None and not item.future.done():
                item.future.set_exception(error)

    def _fail_pending(self, error: Exception) -> None:
        pending: list[_Item] = []
        if self._carry is not None:
            pending.append(self._carry)
            self._carry = None
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is not None:
                pending.append(item)
        self._resolve_all(pending, error)
