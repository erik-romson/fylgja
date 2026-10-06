"""Helpers shared by the todo import tests (WP4.3; the ``tests/`` tree has no packages).

:class:`Store` is a real writer and live view over a database under the test's own directory;
:class:`WriterSink` lets a real collector emit straight into that writer (no ingest gate, which
the gate's own tests cover). :class:`TodoFile` edits a ``TODO.md`` on a
:class:`~fylgja.testing.fakes.FakeFS` the way an editor does: every write gets a new mtime.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fylgja.core.live_view import LiveView
from fylgja.core.writer import Tx, Writer
from fylgja.models import Record
from fylgja.store import db, queries
from fylgja.testing.fakes import FakeClock, FakeFS

T0 = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)


@dataclass
class Store:
    writer: Writer
    view: LiveView
    clock: FakeClock
    database: Path

    async def apply(self, *records: Record) -> None:
        for record in records:
            await self.writer.submit_committed(record)

    async def add_project(self, path: str, root: str | None) -> None:
        stamp = db.to_db_time(self.clock.now())

        def job(tx: Tx) -> None:
            queries.upsert_project(tx.connection, path=path, values={"repo_root": root}, at=stamp)

        await self.writer.execute(job)

    def query(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(sql, params)]

    def rows(self, source: str | None = None) -> list[dict[str, Any]]:
        """Every todo row (deleted ones too), in insertion order."""
        if source is None:
            return self.query("SELECT * FROM todos ORDER BY rowid")
        return self.query("SELECT * FROM todos WHERE source = ? ORDER BY rowid", source)

    def present(self, source: str) -> list[dict[str, Any]]:
        """The rows of ``source`` that are not deleted."""
        return [row for row in self.rows(source) if row["deleted_at"] is None]

    def shown(self) -> dict[str, tuple[str, str]]:
        """The live view's todos: ``{id: (text, status)}``."""
        return {key: (row.text, row.status) for key, row in self.view.todos.items()}


@asynccontextmanager
async def open_store(directory: Path, *, clock: FakeClock | None = None) -> AsyncIterator[Store]:
    clock = FakeClock(T0) if clock is None else clock
    view = LiveView()
    database = directory / "fylgja.db"
    writer = Writer(database, clock=clock, on_commit=view.apply)
    await writer.start()
    try:
        yield Store(writer, view, clock, database)
    finally:
        await writer.stop()


class WriterSink:
    """A record sink that queues straight into a writer."""

    def __init__(self, writer: Writer) -> None:
        self._writer = writer
        self.records: list[Record] = []

    def emit(self, record: Record) -> None:
        self.records.append(record)
        self._writer.submit(record)

    async def emit_committed(self, record: Record) -> None:
        self.records.append(record)
        await self._writer.submit_committed(record)


@dataclass
class TodoFile:
    """A ``TODO.md`` on a fake filesystem; every write is a new modification time."""

    fs: FakeFS
    path: str
    mtime: float = 1_000.0
    history: list[str] = field(default_factory=list)

    def write(self, text: str | bytes) -> None:
        self.mtime += 1.0
        self.fs.add_file(self.path, text, mtime=self.mtime)
        self.history.append(text if isinstance(text, str) else "<bytes>")

    def remove(self) -> None:
        self.fs.unlink(self.path)
