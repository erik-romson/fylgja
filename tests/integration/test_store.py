"""Store, single writer and ingest gate together (WP1.4; ``PLAN.md`` §4.9).

Migrations from empty and on rerun, concurrent fake collectors under the real supervisor with only
validated records reaching SQLite, ``emit_committed`` futures, batching limits and the per-kind
projections (change intervals, merges, complete snapshots). Every database lives under the
per-test directory that the isolation fixture creates and removes.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from fylgja import paths
from fylgja.collectors.base import Capability, CollectorContext, CollectorPlugin, RecordSink
from fylgja.collectors.registry import DiscoveredEntry, RegisteredCollector
from fylgja.collectors.supervisor import Supervisor
from fylgja.core.ingest import IngestGate, RecordRejected, Rejection
from fylgja.core.live_view import LiveView
from fylgja.core.writer import Outcome, Tx, Writer, WriterClosed
from fylgja.models import (
    BuildPayload,
    BuildSnapshot,
    FocusEvent,
    FocusPayload,
    HealthReport,
    HealthState,
    PresenceEvent,
    PresencePayload,
    ProjectEvent,
    ProjectPayload,
    SessionActivity,
    SessionActivityEvent,
    SessionActivitySnapshot,
    SessionEvent,
    SessionPayload,
    SessionSnapshot,
    SnapshotItem,
    TodoPayload,
    TodoSnapshot,
    UsagePayload,
    UsageSnapshot,
    WindowEvent,
    WindowPayload,
    WindowSnapshot,
    WorkspaceEvent,
    WorkspacePayload,
    WorkspaceSnapshot,
)
from fylgja.privacy import PrivacyFilter
from fylgja.store import db, queries
from fylgja.store.db import Migration, MigrationError

pytestmark = pytest.mark.timeout(20)

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


class FixedClock:
    """Wall time that moves only when told; ``sleep`` blocks until cancelled."""

    def __init__(self, now: datetime = T0) -> None:
        self._now = now
        self._mono = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    async def sleep(self, seconds: float) -> None:
        await asyncio.Event().wait()


class EmptyConfig(BaseModel):
    pass


class _EntryPoint:
    name = "x"
    value = "x:y"
    dist = None

    def load(self) -> object:
        raise AssertionError("never loaded")


def plugin(name: str, *capabilities: Capability, factory: Any = None) -> CollectorPlugin:
    return CollectorPlugin(
        name=name,
        api_major=1,
        capabilities=frozenset(capabilities),
        config_model=EmptyConfig,
        factory=factory or (lambda settings, context: None),
    )


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def window(source: str, wid: str, title: str = "t", seconds: float = 0) -> WindowEvent:
    return WindowEvent(
        source=source,
        entity_id=wid,
        observed_at=at(seconds),
        payload=WindowPayload(workspace="1", app_name="App", title=title),
    )


def windows(source: str, seconds: float, *ids: str, complete: bool = True) -> WindowSnapshot:
    return WindowSnapshot(
        source=source,
        entity_id="all",
        observed_at=at(seconds),
        complete=complete,
        items=tuple(
            SnapshotItem(entity_id=wid, payload=WindowPayload(workspace="1", title=f"title {wid}"))
            for wid in ids
        ),
    )


def rows(path: Path, sql: str, *params: Any) -> list[sqlite3.Row]:
    connection = db.connect_reader(path)
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def count(path: Path, table: str, where: str = "1", *params: Any) -> int:
    result = rows(path, f"SELECT count(*) FROM {table} WHERE {where}", *params)
    return int(result[0][0])


@pytest.fixture
def db_path() -> Path:
    return paths.database_file()


@pytest.fixture
async def view() -> LiveView:
    return LiveView()


@pytest.fixture
async def writer(db_path: Path, view: LiveView) -> AsyncIterator[Writer]:
    instance = Writer(db_path, clock=FixedClock(), on_commit=view.apply)
    await instance.start()
    try:
        yield instance
    finally:
        await instance.stop()


def gate_for(writer: Writer, *plugins: CollectorPlugin, **kwargs: Any) -> IngestGate:
    return IngestGate(writer, plugins, clock=FixedClock(), **kwargs)


# --------------------------------------------------------------------------------------------
# Migrations and connections
# --------------------------------------------------------------------------------------------

EXPECTED_COLUMNS = {
    "events": {
        "id",
        "dedup_key",
        "source",
        "entity_kind",
        "entity_id",
        "observed_at",
        "received_at",
        "payload",
    },
    "windows_snapshot": {
        "window_id",
        "workspace",
        "app_bundle_id",
        "app_name",
        "app_pid",
        "title",
        "valid_from",
        "valid_to",
    },
    "focus_spans": {
        "window_id",
        "workspace",
        "app_bundle_id",
        "title",
        "project_path",
        "session_key",
        "started_at",
        "ended_at",
        "end_reason",
    },
    "sessions": {
        "session_key",
        "kind",
        "native_id",
        "pid",
        "process_key",
        "cwd",
        "project_path",
        "host_kind",
        "host_ref",
        "window_id",
        "workspace",
        "name",
        "title",
        "model",
        "branch",
        "state",
        "state_since",
        "waiting_reason",
        "parent_key",
        "predecessor_key",
        "remote_url",
        "first_seen",
        "last_seen",
        "ended_at",
    },
    "session_snapshots": {
        "session_key",
        "valid_from",
        "valid_to",
        "state",
        "last_tool",
        "last_prompt",
        "last_reply",
        "one_liner",
        "context_pct",
        "cost_usd",
        "tokens_input",
        "tokens_output",
        "tokens_cache_read",
        "tokens_cache_write",
    },
    "projects": {
        "path",
        "repo_root",
        "remote_url",
        "repo_name",
        "group_name",
        "branch",
        "dirty",
        "ahead",
        "behind",
        "pr_number",
        "pr_state",
        "review_decision",
        "build_state",
        "build",
        "build_observed_at",
        "build_error_kind",
        "last_commit_at",
        "updated_at",
    },
    "usage_snapshots": {
        "provider",
        "window",
        "used_pct",
        "resets_at",
        "cost_usd",
        "cost_period",
        "source",
        "valid_from",
        "valid_to",
    },
    "workspaces": {"name", "manual_label", "manual_label_at", "inferred_label", "inferred_at"},
    "todos": {
        "id",
        "scope_kind",
        "scope_ref",
        "text",
        "status",
        "created_at",
        "done_at",
        "source",
        "source_ref",
        "link",
        "updated_at",
        "deleted_at",
        "deleted_reason",
    },
    "suggestions_log": {
        "id",
        "list_id",
        "generated_at",
        "rank",
        "kind",
        "target_ref",
        "score",
        "reason",
        "feedback",
        "feedback_at",
        "acted_at",
    },
    "message_receipts": {
        "message_id",
        "session_key",
        "created_at",
        "state",
        "state_at",
        "bytes",
        "error_kind",
    },
    "summaries": {
        "scope_kind",
        "scope_ref",
        "kind",
        "text",
        "input_digest",
        "model",
        "generated_at",
        "error_kind",
    },
    "collector_health": {"name", "state", "since", "last_ok_at", "restarts", "last_error_kind"},
}


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def test_migration_from_empty_creates_every_table(db_path: Path) -> None:
    assert not db_path.exists()
    connection = db.open_store(db_path)
    try:
        assert db.schema_version(connection) == len(db.load_migrations()) == 2
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert tables == set(EXPECTED_COLUMNS)
        for table, expected in EXPECTED_COLUMNS.items():
            assert expected <= _columns(connection, table), table
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 2000
    finally:
        connection.close()
    assert db_path.is_relative_to(Path.cwd() / "target" / "tests")


def test_unique_constraints(db_path: Path) -> None:
    connection = db.open_store(db_path)
    try:
        insert_event = (
            "INSERT INTO events (dedup_key, source, record_type, entity_kind, entity_id, "
            "observed_at, received_at, payload) VALUES ('k', 's', 'event', 'window', 'e', 't', "
            "'t', '{}')"
        )
        connection.execute(insert_event)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(insert_event)
        todo = (
            "INSERT INTO todos (id, text, created_at, source, source_ref, updated_at) "
            "VALUES (?, 'x', 't', 'todo_md', ?, 't')"
        )
        connection.execute(todo, ("a", "ref"))
        connection.execute(todo, ("b", None))
        connection.execute(todo, ("c", None))  # manual rows have no source_ref
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(todo, ("d", "ref"))
        summary = (
            "INSERT INTO summaries (scope_kind, scope_ref, kind) VALUES ('session', 'claude:x', ?)"
        )
        connection.execute(summary, ("arc",))
        connection.execute(summary, ("now",))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(summary, ("arc",))
    finally:
        connection.close()


def test_migration_rerun_is_idempotent(db_path: Path) -> None:
    connection = db.open_store(db_path)
    try:
        connection.execute(
            "INSERT INTO workspaces (name, manual_label) VALUES ('3', 'kept across reruns')"
        )
        assert db.migrate(connection) == len(db.load_migrations())
        assert db.migrate(connection) == len(db.load_migrations())
    finally:
        connection.close()
    again = db.open_store(db_path)
    try:
        assert db.schema_version(again) == len(db.load_migrations())
        label = again.execute("SELECT manual_label FROM workspaces WHERE name = '3'").fetchone()
        assert label[0] == "kept across reruns"
    finally:
        again.close()


_SUMMARY_ROW = (
    "INSERT INTO summaries (id, scope_kind, scope_ref, kind, text, input_digest, model, "
    "generated_at, error_kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _summaries(connection: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in connection.execute(
            "SELECT id, scope_kind, scope_ref, kind, text, input_digest, model, generated_at, "
            "error_kind FROM summaries ORDER BY id"
        )
    ]


def test_migration_2_keeps_existing_summaries(db_path: Path) -> None:
    """0002 rebuilds ``summaries`` for the ``folder`` scope: every row of a version 1 database
    survives with its id, the constraints still hold and a folder row is accepted."""
    connection = db.connect_writer(db_path)
    try:
        first = db.load_migrations()[:1]
        assert db.migrate(connection, first) == 1
        noon, midnight = "2026-10-05T12:00:00.000000Z", "2026-10-05T00:00:00.000000Z"
        kept = [
            (7, "session", "claude:x", "arc", "an arc", "d" * 64, "haiku", noon, None),
            (8, "session", "claude:x", "now", "a now", "d" * 64, "haiku", noon, None),
            (9, "workspace", "3", "text", None, None, None, None, "timeout"),
            (12, "day", "2026-10-04", "text", "a day", "e" * 64, "haiku", midnight, None),
        ]
        connection.executemany(_SUMMARY_ROW, kept)
        folder = (20, "folder", "0123456789abcdef", "text", "a folder", None, None, None, None)
        with pytest.raises(sqlite3.IntegrityError):  # version 1 knows no folder scope
            connection.execute(_SUMMARY_ROW, folder)

        assert db.migrate(connection) == 2
        assert db.schema_version(connection) == 2
        assert _summaries(connection) == kept
        connection.execute(_SUMMARY_ROW, folder)
        assert _summaries(connection) == [*kept, folder]
        assert _columns(connection, "summaries") == EXPECTED_COLUMNS["summaries"] | {"id"}
        with pytest.raises(sqlite3.IntegrityError):  # UNIQUE (scope_kind, scope_ref, kind)
            connection.execute(_SUMMARY_ROW, (21, *folder[1:]))
        with pytest.raises(sqlite3.IntegrityError):  # the CHECK still rejects other scopes
            connection.execute(_SUMMARY_ROW, (22, "project", "x", "text", *folder[4:]))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(_SUMMARY_ROW, (23, "folder", "x", "story", *folder[4:]))
        names = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
        assert "summaries_new" not in names
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()

    again = db.open_store(db_path)  # a rerun on the migrated file is a no-op
    try:
        assert db.schema_version(again) == 2
        assert [row[0] for row in _summaries(again)] == [7, 8, 9, 12, 20]
        stored = queries.load_summaries(again)
        assert [
            (r.scope_kind, r.scope_ref, r.text) for r in stored if r.scope_kind == "folder"
        ] == [("folder", "0123456789abcdef", "a folder")]
    finally:
        again.close()


def test_failed_migration_leaves_the_previous_version(db_path: Path) -> None:
    connection = db.connect_writer(db_path)
    try:
        good = Migration(1, "one", "CREATE TABLE one (x INTEGER);")
        bad = Migration(2, "two", "CREATE TABLE two (x INTEGER);\nINSERT INTO missing VALUES (1);")
        with pytest.raises(MigrationError, match="0002_two"):
            db.migrate(connection, (good, bad))
        assert db.schema_version(connection) == 1
        assert not connection.in_transaction
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
        assert "one" in tables
        assert "two" not in tables  # rolled back with the failed file
        with pytest.raises(MigrationError, match="mid-statement"):
            db.migrate(connection, (good, Migration(2, "cut", "CREATE TABLE cut (x")))
        with pytest.raises(MigrationError, match="newer"):
            db.migrate(connection, ())
    finally:
        connection.close()


def test_reader_connections_are_read_only(db_path: Path) -> None:
    db.open_store(db_path).close()
    reader = db.connect_reader(db_path)
    try:
        assert reader.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("INSERT INTO workspaces (name) VALUES ('x')")
    finally:
        reader.close()


def test_spans_before_a_moment_come_newest_first_and_complete_a_window_read(
    db_path: Path,
) -> None:
    connection = db.open_store(db_path)
    try:
        for start, end in ((0, 60), (60, 120), (200, 300), (400, None)):
            started = db.to_db_time(at(start))
            queries.insert_open_span(
                connection,
                window_id="w",
                workspace="1",
                app_bundle_id=None,
                title=None,
                project_path=None,
                session_key=None,
                started_at=started,
            )
            if end is not None:
                queries.close_open_span(
                    connection,
                    window_id="w",
                    started_at=started,
                    ended_at=db.to_db_time(at(end)),
                    end_reason="focus",
                    project_path=None,
                    session_key=None,
                )
        before = queries.load_spans_before(connection, before=db.to_db_time(at(120)), limit=10)
        assert [(r.started_at, r.ended_at) for r in before] == [(at(60), at(120)), (at(0), at(60))]
        limited = queries.load_spans_before(connection, before=db.to_db_time(at(400)), limit=2)
        assert [r.started_at for r in limited] == [at(200), at(60)]  # the open span never
        after = queries.load_spans(
            connection, since=db.to_db_time(at(120)), until=db.to_db_time(at(3600))
        )
        assert [r.started_at for r in after] == [at(200), at(400)]
        assert after[-1].ended_at is None
    finally:
        connection.close()


def test_stored_times_sort_as_text() -> None:
    plus2 = datetime(2026, 1, 1, 14, 0, tzinfo=UTC).astimezone(
        datetime.now(UTC).astimezone().tzinfo
    )
    stamps = [at(-1), at(0), at(0.000001), plus2]
    texts = [db.to_db_time(s) for s in stamps]
    assert sorted(texts) == [db.to_db_time(s) for s in sorted(stamps)]
    assert all(db.from_db_time(t) == s for t, s in zip(texts, stamps, strict=True))
    assert db.from_db_time("2026-10-05T12:00:00") == T0
    with pytest.raises(ValueError, match="timezone"):
        db.to_db_time(datetime(2026, 1, 1))


# --------------------------------------------------------------------------------------------
# Concurrent fake collectors: only validated records reach SQLite (PLAN.md §4.9)
# --------------------------------------------------------------------------------------------

VALID_PER_COLLECTOR = 60


def _allowed(name: str, observed_at: datetime) -> object:
    """A record of a kind ``name`` may emit, carrying the INVALID marker."""
    if name == "beta":
        return SessionActivityEvent(
            source=name,
            entity_id="claude:x",
            observed_at=observed_at,
            payload=SessionActivity(title="INVALID"),
        )
    return WindowEvent(
        source=name, entity_id="x", observed_at=observed_at, payload=WindowPayload(title="INVALID")
    )


def _forbidden(name: str) -> object:
    """A record of a kind ``name`` may not emit."""
    if name == "beta":
        return WindowEvent(
            source=name, entity_id="x", observed_at=T0, payload=WindowPayload(title="INVALID")
        )
    return SessionActivityEvent(
        source=name, entity_id="c:x", observed_at=T0, payload=SessionActivity(title="INVALID")
    )


def _invalid_records(name: str) -> list[object]:
    """One or two records per rejection reason, each carrying the INVALID marker."""
    return [
        WindowEvent(
            source="someone-else",
            entity_id="x",
            observed_at=T0,
            payload=WindowPayload(title="INVALID"),
        ),
        _forbidden(name),
        WindowEvent.model_construct(
            source=name,
            entity_id="",
            observed_at=T0,
            payload=WindowPayload(title="INVALID"),
            record_type="event",
        ),
        {"record_type": "event", "entity_kind": "window", "source": name, "title": "INVALID"},
        _allowed(name, T0 - timedelta(days=31)),
        _allowed(name, T0 + timedelta(hours=1)),
    ]


def _valid_record(name: str, index: int) -> object:
    if name == "beta":
        return SessionEvent(
            source=name,
            entity_id=f"claude:s{index}",
            observed_at=at(index),
            payload=SessionPayload(kind="claude", native_id=f"s{index}", name=f"valid {index}"),
        )
    return WindowEvent(
        source=name,
        entity_id=f"{name}-w{index}",
        observed_at=at(index),
        payload=WindowPayload(title=f"valid {index}"),
    )


def _collector(name: str, *caps: Capability, done: asyncio.Event) -> RegisteredCollector:
    class Collector:
        def __init__(self, context: CollectorContext) -> None:
            self._sink: RecordSink = context.sink

        async def run(self) -> None:
            invalid = _invalid_records(name)
            for index in range(VALID_PER_COLLECTOR):
                self._sink.emit(_valid_record(name, index))  # type: ignore[arg-type]
                if index < len(invalid):
                    self._sink.emit(invalid[index])  # type: ignore[arg-type]
                await asyncio.sleep(0)
            done.set()
            await asyncio.Event().wait()

        async def doctor(self) -> HealthReport:
            return HealthReport(state=HealthState.OK, checked_at=T0)

    descriptor = plugin(name, *caps, factory=lambda settings, context: Collector(context))
    entry = DiscoveredEntry(name, "x:y", "fylgja-fake", "0", _EntryPoint())
    return RegisteredCollector(entry, enabled=True, plugin=descriptor, settings=EmptyConfig())


async def test_concurrent_fake_collectors_store_only_validated_records(
    writer: Writer, db_path: Path
) -> None:
    names = {"alpha": (Capability.WINDOWS,), "beta": (Capability.SESSIONS,)}
    names["gamma"] = (Capability.WINDOWS, Capability.PROJECT_STATE)
    done = {name: asyncio.Event() for name in names}
    items = [_collector(name, *caps, done=done[name]) for name, caps in names.items()]
    gate = IngestGate(writer, [item.plugin for item in items if item.plugin], clock=FixedClock())
    supervisor = Supervisor(
        items,
        gate.sink("alpha"),
        clock=FixedClock(),
        sink_factory=gate.sink,
        on_health=gate.observe_health,
    )
    supervisor.start()
    try:
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in done.values())), 10)
    finally:
        await supervisor.shutdown(2.0)
    await writer.flush()

    for name in names:
        stats = gate.stats(name)
        assert stats.accepted == VALID_PER_COLLECTOR
        assert stats.rejected_total == 6
        assert set(stats.rejected) == set(Rejection), name
        assert count(db_path, "events", "source = ?", name) == VALID_PER_COLLECTOR
    assert count(db_path, "events") == 3 * VALID_PER_COLLECTOR
    assert count(db_path, "windows_snapshot") == 2 * VALID_PER_COLLECTOR
    assert count(db_path, "sessions") == VALID_PER_COLLECTOR
    assert count(db_path, "events", "source NOT IN ('alpha', 'beta', 'gamma')") == 0
    for table in ("events", "windows_snapshot", "sessions"):
        dumped = rows(db_path, f"SELECT * FROM {table}")
        assert not any("INVALID" in str(value) for row in dumped for value in tuple(row))
    assert count(db_path, "collector_health") == 3  # supervisor health went through the gate


# --------------------------------------------------------------------------------------------
# emit_committed futures
# --------------------------------------------------------------------------------------------


async def test_emit_committed_resolves_only_after_the_commit(writer: Writer, db_path: Path) -> None:
    gate = gate_for(writer, plugin("alpha", Capability.WINDOWS))
    sink = gate.sink("alpha")
    entered = threading.Event()
    release = threading.Event()

    def blocking_job(tx: Tx) -> str:
        entered.set()
        if not release.wait(10):
            raise TimeoutError("never released")
        return "released"

    job = asyncio.create_task(writer.execute(blocking_job))
    assert await asyncio.to_thread(entered.wait, 10)
    committed = asyncio.create_task(sink.emit_committed(window("alpha", "w1")))
    await asyncio.sleep(0.1)
    assert not committed.done()
    assert count(db_path, "events") == 0  # not visible before the commit
    release.set()
    assert await job == "released"
    await asyncio.wait_for(committed, 5)
    assert count(db_path, "events") == 1  # a separate read-only connection sees it


async def test_emit_committed_reports_rejections_and_privacy_drops(
    writer: Writer, db_path: Path
) -> None:
    privacy = PrivacyFilter(ignore_bundle_ids=["com.example.secret"])
    gate = gate_for(writer, plugin("alpha", Capability.WINDOWS), privacy=privacy)
    sink = gate.sink("alpha")
    with pytest.raises(RecordRejected) as rejected:
        await sink.emit_committed(window("beta", "w1"))
    assert rejected.value.reason is Rejection.SOURCE
    ignored = WindowEvent(
        source="alpha",
        entity_id="w2",
        observed_at=T0,
        payload=WindowPayload(app_bundle_id="com.example.secret", title="hidden"),
    )
    assert await sink.emit_committed(ignored) is None
    assert gate.stats("alpha").dropped == 1
    await writer.flush()
    assert count(db_path, "events") == 0


async def test_duplicates_are_ignored_and_reported(writer: Writer, db_path: Path) -> None:
    record = window("alpha", "w1")
    assert await writer.submit_committed(record) is Outcome.STORED
    assert await writer.submit_committed(record) is Outcome.DUPLICATE
    assert writer.stats.duplicates == 1
    assert count(db_path, "events") == 1
    assert count(db_path, "windows_snapshot") == 1


async def test_a_failing_item_does_not_sink_its_batch(writer: Writer, db_path: Path) -> None:
    def explode(tx: Tx) -> None:
        tx.connection.execute("INSERT INTO workspaces (name) VALUES ('partial')")
        raise ValueError("boom")

    writer.submit(window("alpha", "w1"))
    failing = asyncio.create_task(writer.execute(explode))
    writer.submit(window("alpha", "w2"))
    with pytest.raises(ValueError, match="boom"):
        await failing
    await writer.flush()
    assert count(db_path, "events") == 2
    assert count(db_path, "workspaces") == 0  # the failed item's own writes rolled back
    assert writer.stats.failed_items == 1


async def test_fire_and_forget_failures_are_counted(writer: Writer, db_path: Path) -> None:
    writer.submit(
        SessionActivityEvent(
            source="beta",
            entity_id="claude:s1",
            observed_at=T0,
            payload=SessionActivity(last_prompt="x" * 301),  # violates the 300-char CHECK
        )
    )
    await writer.flush()
    assert writer.stats.failed_items == 1
    assert count(db_path, "events") == 0


async def test_stop_commits_queued_items_then_refuses(db_path: Path) -> None:
    instance = Writer(db_path, clock=FixedClock())
    await instance.start()
    with pytest.raises(RuntimeError, match="already started"):
        await instance.start()
    for index in range(10):
        instance.submit(window("alpha", f"w{index}"))
    await instance.stop()
    await instance.stop()  # idempotent
    assert count(db_path, "events") == 10
    with pytest.raises(WriterClosed):
        instance.submit(window("alpha", "late"))
    with pytest.raises(WriterClosed):
        await instance.start()
    gate = gate_for(instance, plugin("alpha", Capability.WINDOWS))
    gate.emit("alpha", window("alpha", "lost"))
    assert gate.stats("alpha").lost == 1


async def test_queued_before_start_is_committed_after_start(db_path: Path) -> None:
    instance = Writer(db_path, clock=FixedClock())
    instance.submit(window("alpha", "early"))
    assert instance.pending == 1
    assert not instance.running
    await instance.start()
    assert instance.running
    await instance.flush()
    assert instance.idle
    await instance.stop()
    assert count(db_path, "events") == 1
    assert instance.database == db_path


def test_writer_rejects_bad_limits() -> None:
    with pytest.raises(ValueError, match="max_batch"):
        Writer(max_batch=0)
    with pytest.raises(ValueError, match="max_delay"):
        Writer(max_delay=-1)


# --------------------------------------------------------------------------------------------
# Batching limits
# --------------------------------------------------------------------------------------------


async def test_a_transaction_holds_at_most_500_records(writer: Writer, db_path: Path) -> None:
    total = 1234
    for index in range(total):
        writer.submit(window("alpha", f"w{index}"))  # no yield: all queued before the writer runs
    await writer.flush()
    sizes = list(writer.stats.batch_sizes)
    assert max(sizes) == 500
    assert sizes[:2] == [500, 500]
    assert sum(sizes) == total + 1  # plus the flush job
    assert count(db_path, "events") == total


class SteppingClock(FixedClock):
    """Monotonic time advances 10 ms on every reading."""

    def monotonic(self) -> float:
        self._mono += 0.01
        return self._mono


async def test_a_transaction_stops_collecting_after_max_delay(db_path: Path) -> None:
    instance = Writer(db_path, clock=SteppingClock(), max_batch=10**9, max_delay=0.045)
    await instance.start()
    try:
        for index in range(20):
            instance.submit(window("alpha", f"w{index}"))
        await instance.flush()
    finally:
        await instance.stop()
    # Each item costs one 10 ms reading, so a 45 ms window closes after five items.
    assert list(instance.stats.batch_sizes) == [5, 5, 5, 5, 1]
    assert count(db_path, "events") == 20


async def test_a_lone_record_commits_without_waiting_for_the_window(db_path: Path) -> None:
    instance = Writer(db_path, clock=FixedClock(), max_delay=30.0)
    await instance.start()
    try:
        started = asyncio.get_running_loop().time()
        await asyncio.wait_for(instance.submit_committed(window("alpha", "w1")), 5)
        assert asyncio.get_running_loop().time() - started < 5
    finally:
        await instance.stop()


# --------------------------------------------------------------------------------------------
# Projections
# --------------------------------------------------------------------------------------------


async def emit_all(writer: Writer, *records: Any) -> None:
    for record in records:
        writer.submit(record)
    await writer.flush()


async def test_window_change_intervals(writer: Writer, db_path: Path, view: LiveView) -> None:
    await emit_all(writer, windows("aero", 0, "w1", "w2"))
    assert count(db_path, "windows_snapshot") == 2
    await emit_all(writer, windows("aero", 30, "w1", "w2"))  # unchanged: nothing written
    assert count(db_path, "windows_snapshot") == 2
    changed = WindowEvent(
        source="aero",
        entity_id="w1",
        observed_at=at(40),
        payload=WindowPayload(workspace="2", title="title w1"),
    )
    await emit_all(writer, changed)
    w1 = rows(db_path, "SELECT * FROM windows_snapshot WHERE window_id = 'w1' ORDER BY id")
    assert [(r["workspace"], r["valid_from"], r["valid_to"]) for r in w1] == [
        ("1", db.to_db_time(at(0)), db.to_db_time(at(40))),
        ("2", db.to_db_time(at(40)), None),
    ]
    assert view.windows["w1"].workspace == "2"
    stale = WindowEvent(
        source="aero", entity_id="w1", observed_at=at(35), payload=WindowPayload(workspace="9")
    )
    await emit_all(writer, stale)
    assert count(db_path, "windows_snapshot", "window_id = 'w1'") == 2
    await emit_all(writer, windows("aero", 50, "w1"))  # complete: w2 closes
    closed = rows(db_path, "SELECT valid_to FROM windows_snapshot WHERE window_id = 'w2'")
    assert closed[0][0] == db.to_db_time(at(50))
    assert "w2" not in view.windows
    late = WindowEvent(
        source="aero", entity_id="w2", observed_at=at(45), payload=WindowPayload(title="old")
    )
    await emit_all(writer, late)  # older than the close: stale, no overlapping interval
    assert count(db_path, "windows_snapshot", "window_id = 'w2'") == 1
    await emit_all(writer, windows("other", 60, "w9", complete=True))
    assert count(db_path, "windows_snapshot", "valid_to IS NULL AND source = 'aero'") == 1


async def test_workspaces_focus_and_presence(writer: Writer, db_path: Path, view: LiveView) -> None:
    def snapshot(seconds: float, focused: str, *names: str) -> WorkspaceSnapshot:
        return WorkspaceSnapshot(
            source="aero",
            entity_id="all",
            observed_at=at(seconds),
            complete=True,
            items=tuple(
                SnapshotItem(
                    entity_id=name,
                    payload=WorkspacePayload(name=name, focused=name == focused, monitor="main"),
                )
                for name in names
            ),
        )

    await emit_all(writer, snapshot(0, "1", "1", "2", "3"))
    assert set(view.workspaces) == {"1", "2", "3"}
    assert view.workspaces["1"].focused
    await emit_all(writer, snapshot(10, "2", "1", "2"))
    assert set(view.workspaces) == {"1", "2"}
    assert view.workspaces["2"].focused
    assert not view.workspaces["1"].focused
    gone = rows(db_path, "SELECT closed_at, focused FROM workspaces WHERE name = '3'")
    assert tuple(gone[0]) == (db.to_db_time(at(10)), 0)
    await emit_all(writer, snapshot(20, "3", "1", "2", "3"))  # back again
    assert view.workspaces["3"].focused
    assert view.workspaces["3"].first_seen == at(0)


async def test_sessions_keep_process_key_and_predecessor(
    writer: Writer, db_path: Path, view: LiveView
) -> None:
    """A11: the continuity fields are stored, and a later record that omits them keeps them."""

    def event(seconds: float, **fields: Any) -> SessionEvent:
        return SessionEvent(
            source="claude",
            entity_id="claude:b",
            observed_at=at(seconds),
            payload=SessionPayload(kind="claude", native_id="b", pid=100, **fields),
        )

    await emit_all(writer, event(0, process_key="100-1", predecessor_key="claude:a"))
    assert view.sessions["claude:b"].process_key == "100-1"
    assert view.sessions["claude:b"].predecessor_key == "claude:a"
    await emit_all(writer, event(2))
    stored = rows(
        db_path,
        "SELECT process_key, predecessor_key FROM sessions WHERE session_key = 'claude:b'",
    )
    assert tuple(stored[0]) == ("100-1", "claude:a")


async def test_sessions_merge_end_and_reopen(writer: Writer, db_path: Path, view: LiveView) -> None:
    def listing(seconds: float, *ids: str, name: str | None = None) -> SessionSnapshot:
        return SessionSnapshot(
            source="claude",
            entity_id="registry",
            observed_at=at(seconds),
            complete=True,
            items=tuple(
                SnapshotItem(
                    entity_id=f"claude:{sid}",
                    payload=SessionPayload(kind="claude", native_id=sid, pid=100, name=name),
                )
                for sid in ids
            ),
        )

    await emit_all(writer, listing(0, "a", "b", name="first"))
    await emit_all(writer, listing(2, "a", "b"))  # a None name keeps the stored one
    assert view.sessions["claude:a"].name == "first"
    assert view.sessions["claude:a"].last_seen == at(2)
    version = view.version
    await emit_all(writer, listing(4, "a", "b"))  # only last_seen moves: no live-view change
    assert view.version == version
    activity = SessionActivityEvent(
        source="claude",
        entity_id="claude:b",
        observed_at=at(5),
        payload=SessionActivity(state="busy", current_tool="Edit", title="Fix it"),
    )
    await emit_all(writer, activity)
    assert view.activity["claude:b"].last_tool == "Edit"
    assert view.sessions["claude:b"].state == "busy"
    assert view.sessions["claude:b"].state_since == at(5)
    await emit_all(writer, listing(6, "a"))  # b ended
    assert set(view.sessions) == {"claude:a"}
    assert "claude:b" not in view.activity
    ended = rows(db_path, "SELECT ended_at FROM sessions WHERE session_key = 'claude:b'")
    assert ended[0][0] == db.to_db_time(at(6))
    assert count(db_path, "session_snapshots", "valid_to IS NULL") == 0
    await emit_all(writer, listing(5.5, "a", "b"))  # a late listing never reopens it
    assert "claude:b" not in view.sessions
    after_end = SessionActivityEvent(
        source="claude",
        entity_id="claude:b",
        observed_at=at(7),
        payload=SessionActivity(state="busy"),
    )
    await emit_all(writer, after_end)  # activity of an ended session is not recorded
    assert count(db_path, "session_snapshots", "session_key = 'claude:b'") == 1
    await emit_all(writer, listing(8, "a", "b"))  # listed again later: alive again
    assert "claude:b" in view.sessions
    stopped = SessionEvent(
        source="claude",
        entity_id="claude:a",
        observed_at=at(9),
        payload=SessionPayload(kind="claude", native_id="a", ended_at=at(9)),
    )
    await emit_all(writer, stopped)
    assert "claude:a" not in view.sessions


async def test_an_event_of_another_source_links_a_session_without_taking_it_over(
    writer: Writer, db_path: Path, view: LiveView
) -> None:
    """norn nests the agent sessions it drives: its event sets ``parent_key`` and nothing else."""
    listing = SessionSnapshot(
        source="claude",
        entity_id="registry",
        observed_at=at(0),
        complete=True,
        items=(
            SnapshotItem(
                entity_id="claude:a",
                payload=SessionPayload(kind="claude", native_id="a", pid=100, cwd="/w"),
            ),
        ),
    )
    await emit_all(writer, listing)
    link = SessionEvent(
        source="norn",
        entity_id="claude:a",
        observed_at=at(1),
        payload=SessionPayload(kind="claude", native_id="a", parent_key="norn:p@000000:1"),
    )
    await emit_all(writer, link)
    row = view.sessions["claude:a"]
    assert (row.parent_key, row.source, row.pid, row.cwd) == (
        "norn:p@000000:1",
        "claude",
        100,
        "/w",
    )
    await emit_all(writer, listing.model_copy(update={"observed_at": at(2), "dedup_key": ""}))
    again = view.sessions["claude:a"]  # the registry's next listing keeps the link
    assert (again.parent_key, again.source) == ("norn:p@000000:1", "claude")
    # an event for a session nobody else listed creates it, owned by the linking source
    alone = link.model_copy(
        update={
            "entity_id": "claude:b",
            "observed_at": at(3),
            "dedup_key": "",
            "payload": SessionPayload(kind="claude", native_id="b", parent_key="norn:p@000000:1"),
        }
    )
    await emit_all(writer, alone)
    assert view.sessions["claude:b"].source == "norn"


async def test_session_activity_intervals(writer: Writer, db_path: Path, view: LiveView) -> None:
    def activity(seconds: float, **fields: Any) -> SessionActivityEvent:
        return SessionActivityEvent(
            source="claude",
            entity_id="claude:h1",
            observed_at=at(seconds),
            payload=SessionActivity(**fields),
        )

    await emit_all(writer, activity(0, state="working", last_prompt="p" * 300))
    assert view.sessions["claude:h1"].kind == "claude"  # hooks-only session row was created
    assert view.sessions["claude:h1"].native_id == "h1"
    await emit_all(writer, activity(1, state="working", last_prompt="p" * 300))
    assert count(db_path, "session_snapshots") == 1
    await emit_all(writer, activity(2, state="running_tool", current_tool="Bash"))
    assert view.sessions["claude:h1"].state_since == at(2)
    await emit_all(writer, activity(3, state="running_tool", current_tool="Read"))
    assert view.sessions["claude:h1"].state_since == at(2)  # the state itself did not change
    await emit_all(writer, activity(2.5, state="idle"))  # stale
    assert view.sessions["claude:h1"].state == "running_tool"
    staged = activity(4, state="running", stage={"name": "build", "index": 2, "total": 5})
    await emit_all(writer, staged)
    assert view.activity["claude:h1"].stage == {"name": "build", "index": 2, "total": 5}
    assert count(db_path, "session_snapshots") == 4
    snapshot = SessionActivitySnapshot(
        source="claude",
        entity_id="all",
        observed_at=at(10),
        complete=True,
        items=(SnapshotItem(entity_id="bare", payload=SessionActivity(state="idle")),),
    )
    await emit_all(writer, snapshot)
    assert "claude:h1" not in view.activity  # closed by the complete snapshot
    assert view.sessions["claude:bare"].native_id == "bare"  # key without a kind


async def test_projects_builds_and_usage(writer: Writer, db_path: Path, view: LiveView) -> None:
    project = ProjectEvent(
        source="project",
        entity_id="/p",
        observed_at=at(0),
        payload=ProjectPayload(path="/p", branch="main", dirty=True, last_commit_at=at(-60)),
    )
    update = ProjectEvent(
        source="project",
        entity_id="/p",
        observed_at=at(1),
        payload=ProjectPayload(path="/p", ahead=2),
    )
    await emit_all(writer, project, update)
    row = view.projects["/p"]
    assert (row.branch, row.dirty, row.ahead, row.last_commit_at) == ("main", True, 2, at(-60))
    build = BuildSnapshot(
        source="project",
        entity_id="builds",
        observed_at=at(2),
        complete=True,
        items=(
            SnapshotItem(
                entity_id="/q",
                payload=BuildPayload(state="fail", head_sha="abc", project_path=None),
            ),
            SnapshotItem(
                entity_id="ignored", payload=BuildPayload(state="pass", project_path="/p")
            ),
        ),
    )
    await emit_all(writer, build)
    assert view.projects["/p"].build_state == "pass"
    assert view.projects["/q"].build == {"state": "fail", "head_sha": "abc", "workflows": []}

    def usage(seconds: float, *items: tuple[str, str | None, float]) -> UsageSnapshot:
        return UsageSnapshot(
            source="claude",
            entity_id="usage",
            observed_at=at(seconds),
            complete=True,
            items=tuple(
                SnapshotItem(
                    entity_id=f"{provider}:{window}",
                    payload=UsagePayload(
                        provider=provider, window=window, used_pct=pct, resets_at=at(3600)
                    ),
                )
                for provider, window, pct in items
            ),
        )

    await emit_all(writer, usage(0, ("claude", "5h", 10.0), ("claude", None, 1.0)))
    await emit_all(writer, usage(10, ("claude", "5h", 10.0), ("claude", None, 1.0)))
    assert count(db_path, "usage_snapshots") == 2
    await emit_all(writer, usage(20, ("claude", "5h", 25.0)))
    assert view.usage["claude:claude:5h"].used_pct == 25.0
    assert view.usage["claude:claude:5h"].source == "claude"
    assert "claude:claude:" not in view.usage
    assert count(db_path, "usage_snapshots", "valid_to IS NULL") == 1


async def test_unprojected_kinds_and_focus(writer: Writer, db_path: Path, view: LiveView) -> None:
    todos = TodoSnapshot(
        source="todo_md",
        entity_id="TODO.md",
        observed_at=at(0),
        complete=True,
        items=(SnapshotItem(entity_id="1", payload=TodoPayload(text="write tests")),),
    )
    presence = PresenceEvent(
        source="presence", entity_id="me", observed_at=at(0), payload=PresencePayload(state="idle")
    )
    focus_old = FocusEvent(
        source="aero", entity_id="w1", observed_at=at(1), payload=FocusPayload(window_id="w1")
    )
    focus_new = FocusEvent(
        source="aero",
        entity_id="w2",
        observed_at=at(2),
        payload=FocusPayload(window_id="w2", workspace="3", title="editor"),
    )
    await emit_all(writer, todos, presence, focus_new, focus_old)
    kinds = rows(db_path, "SELECT record_type, entity_kind FROM events ORDER BY id")
    assert [tuple(r) for r in kinds] == [
        ("snapshot", "todo"),
        ("event", "presence"),
        ("event", "focus"),
        ("event", "focus"),
    ]
    assert count(db_path, "todos") == 0  # todo reconciliation is not this step's job
    assert view.focused is not None
    assert (view.focused.window_id, view.focused.workspace) == ("w2", "3")  # newest wins


async def test_health_rows_reach_the_store_and_view(
    writer: Writer, db_path: Path, view: LiveView
) -> None:
    gate = gate_for(writer, plugin("alpha", Capability.WINDOWS))
    from fylgja.collectors.supervisor import CollectorHealth

    gate.observe_health(CollectorHealth("alpha", HealthState.OK, since=T0, last_ok_at=T0))
    await writer.flush()
    stored = rows(db_path, "SELECT state, last_ok_at FROM collector_health WHERE name = 'alpha'")
    assert tuple(stored[0]) == ("ok", db.to_db_time(T0))
    assert view.collectors["alpha"].state == "ok"


async def test_live_view_rebuilds_from_the_store(
    writer: Writer, db_path: Path, view: LiveView
) -> None:
    await emit_all(
        writer,
        windows("aero", 0, "w1", "w2"),
        SessionActivityEvent(
            source="claude",
            entity_id="claude:s1",
            observed_at=at(1),
            payload=SessionActivity(state="busy"),
        ),
        FocusEvent(source="aero", entity_id="w1", observed_at=at(2), payload=FocusPayload()),
    )
    fresh = LiveView()
    reader = db.connect_reader(db_path)
    try:
        assert fresh.rebuild(reader) is not None
    finally:
        reader.close()
    assert fresh.snapshot().windows == view.snapshot().windows
    assert fresh.sessions == view.sessions
    assert fresh.activity == view.activity
    assert fresh.focused == view.focused


async def test_a_workspace_event_moves_the_focus(writer: Writer, view: LiveView) -> None:
    listing = WorkspaceSnapshot(
        source="aero",
        entity_id="all",
        observed_at=at(0),
        complete=True,
        items=tuple(
            SnapshotItem(entity_id=name, payload=WorkspacePayload(name=name, focused=name == "1"))
            for name in ("1", "2")
        ),
    )
    moved = WorkspaceEvent(
        source="aero",
        entity_id="2",
        observed_at=at(1),
        payload=WorkspacePayload(name="2", focused=True),
    )
    await emit_all(writer, listing, moved)
    assert view.workspaces["2"].focused
    assert not view.workspaces["1"].focused


# --------------------------------------------------------------------------------------------
# Failure paths and migration edges
# --------------------------------------------------------------------------------------------


async def test_a_failed_commit_fails_the_whole_batch(writer: Writer, db_path: Path) -> None:
    def violate_on_commit(tx: Tx) -> None:
        tx.connection.execute("PRAGMA defer_foreign_keys = ON")
        tx.connection.execute(
            "INSERT INTO session_snapshots (session_key, source, valid_from) "
            "VALUES ('nobody', 's', 't')"
        )

    record = asyncio.create_task(writer.submit_committed(window("alpha", "w1")))
    job = asyncio.create_task(writer.execute(violate_on_commit))
    results = await asyncio.gather(record, job, return_exceptions=True)
    assert all(isinstance(result, sqlite3.IntegrityError) for result in results)
    assert writer.stats.failed_transactions == 1
    assert count(db_path, "events") == 0  # rolled back with its batch
    assert await writer.submit_committed(window("alpha", "w2")) is Outcome.STORED


async def test_jobs_outside_a_transaction_run_alone(writer: Writer) -> None:
    seen: list[bool] = []

    def outside(tx: Tx) -> str:
        seen.append(tx.connection.in_transaction)
        return "done"

    def failing(tx: Tx) -> None:
        raise LookupError("nope")

    writer.submit(window("alpha", "w1"))
    job = asyncio.create_task(writer.execute(outside, transaction=False))
    assert await job == "done"
    assert seen == [False]
    with pytest.raises(LookupError, match="nope"):
        await writer.execute(failing, transaction=False)
    sizes = list(writer.stats.batch_sizes)
    assert sizes[-2:] == [1, 1]  # each exclusive job ran in a transaction of its own


async def test_a_failing_live_view_does_not_lose_the_commit(db_path: Path) -> None:
    def broken(changes: object) -> None:
        raise RuntimeError("live view bug")

    instance = Writer(db_path, clock=FixedClock(), on_commit=broken)
    await instance.start()
    try:
        assert await instance.submit_committed(window("alpha", "w1")) is Outcome.STORED
    finally:
        await instance.stop()
    assert count(db_path, "events") == 1


async def test_a_payload_of_the_wrong_model_is_refused(writer: Writer) -> None:
    bad = WindowEvent.model_construct(
        source="alpha",
        entity_id="w1",
        observed_at=T0,
        payload=WorkspacePayload(name="1"),
        dedup_key="bad",
    )
    with pytest.raises(TypeError, match="WindowPayload"):
        await writer.submit_committed(bad)


async def test_cancelling_the_writer_task_fails_waiters_and_stop_still_closes(
    db_path: Path,
) -> None:
    instance = Writer(db_path, clock=FixedClock())
    await instance.start()
    entered = threading.Event()
    release = threading.Event()

    def blocking(tx: Tx) -> None:
        entered.set()
        release.wait(10)

    try:
        job = asyncio.create_task(instance.execute(blocking))
        assert await asyncio.to_thread(entered.wait, 10)
        queued = asyncio.create_task(instance.submit_committed(window("alpha", "w1")))
        await asyncio.sleep(0)
        (task,) = [t for t in asyncio.all_tasks() if t.get_name() == "fylgja-writer"]
        task.cancel()
        with pytest.raises(WriterClosed):
            await job
    finally:
        release.set()
    await instance.stop()  # waits for the thread, then fails what is still queued
    with pytest.raises(WriterClosed):
        await queued
    assert not instance.running


def test_load_migrations_from_a_folder(isolated_env: Path) -> None:
    folder = isolated_env / "migrations"
    folder.mkdir()
    (folder / "0001_first.sql").write_text("CREATE TABLE first (x INTEGER);")
    (folder / "README.md").write_text("not a migration")
    assert [m.name for m in db.load_migrations(folder)] == ["first"]
    (folder / "0003_third.sql").write_text("CREATE TABLE third (x INTEGER);")
    with pytest.raises(MigrationError, match="0002"):
        db.load_migrations(folder)


def test_a_database_from_a_newer_fylgja_is_refused(db_path: Path) -> None:
    connection = db.open_store(db_path)
    connection.execute("PRAGMA user_version = 99")
    connection.close()
    with pytest.raises(MigrationError, match="newer"):
        db.open_store(db_path)
