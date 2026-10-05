"""Connections, migrations and the stored time format (``PLAN-final-plan.md`` §4.1).

* The **writer connection** (:func:`connect_writer`) uses WAL, ``synchronous=NORMAL``,
  ``foreign_keys=ON`` and ``busy_timeout=2000``. It runs in autocommit mode, so the writer issues
  ``BEGIN``/``COMMIT`` itself, and it may be used from a worker thread (the writer task calls it
  through ``asyncio.to_thread``, one call at a time).
* **Reader connections** (:func:`connect_reader`) open the file through a ``file:…?mode=ro`` URI
  and additionally set ``query_only``. They can never write.
* **Migrations** are the numbered ``migrations/NNNN_<name>.sql`` files. :func:`migrate` applies
  each file whose number is above ``PRAGMA user_version`` in its own transaction, together with the
  new ``user_version``, so a failed file leaves the database at the previous version and a rerun
  is a no-op.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Final

from fylgja import paths

__all__ = [
    "BUSY_TIMEOUT_MS",
    "Migration",
    "MigrationError",
    "connect_reader",
    "connect_writer",
    "from_db_time",
    "load_migrations",
    "migrate",
    "open_store",
    "schema_version",
    "to_db_time",
]

BUSY_TIMEOUT_MS: Final = 2000

_MIGRATION_NAME: Final = re.compile(r"(\d{4})_([a-z0-9_]+)\.sql")
_TIME_FORMAT: Final = "%Y-%m-%dT%H:%M:%S.%fZ"


class MigrationError(RuntimeError):
    """A migration file is malformed, missing or failed to apply."""


# --------------------------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------------------------


def to_db_time(value: datetime) -> str:
    """The stored form of an aware datetime: UTC, microseconds, ``Z`` suffix."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("stored times must be timezone-aware")
    return value.astimezone(UTC).strftime(_TIME_FORMAT)


def from_db_time(value: str) -> datetime:
    """Parse a stored time back into an aware UTC datetime."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


# --------------------------------------------------------------------------------------------
# Migrations
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Migration:
    number: int
    name: str
    sql: str


def load_migrations(folder: Traversable | None = None) -> tuple[Migration, ...]:
    """The migrations in number order (default: the packaged ones). Numbers must run 1, 2, 3, …
    without gaps; files not named ``NNNN_<name>.sql`` are ignored."""
    if folder is None:
        folder = resources.files("fylgja.store").joinpath("migrations")
    found: list[Migration] = []
    for entry in folder.iterdir():
        match = _MIGRATION_NAME.fullmatch(entry.name)
        if match is None:
            continue
        found.append(
            Migration(int(match.group(1)), match.group(2), entry.read_text(encoding="utf-8"))
        )
    found.sort(key=lambda m: m.number)
    for expected, migration in enumerate(found, start=1):
        if migration.number != expected:
            raise MigrationError(f"migration {expected:04d} is missing")
    return tuple(found)


def schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def migrate(connection: sqlite3.Connection, migrations: tuple[Migration, ...] | None = None) -> int:
    """Apply every migration above ``user_version``; returns the resulting version.

    The connection must be in autocommit mode (as :func:`connect_writer` opens it).
    """
    pending = load_migrations() if migrations is None else migrations
    current = schema_version(connection)
    latest = pending[-1].number if pending else 0
    if current > latest:
        raise MigrationError(f"database schema {current} is newer than this Fylgja ({latest})")
    for migration in pending:
        if migration.number <= current:
            continue
        if not sqlite3.complete_statement(migration.sql):
            raise MigrationError(f"migration {migration.number:04d} ends mid-statement")
        script = (
            "BEGIN IMMEDIATE;\n"
            f"{migration.sql}\n"
            f"PRAGMA user_version = {migration.number:d};\n"
            "COMMIT;\n"
        )
        try:
            connection.executescript(script)
        except sqlite3.Error as exc:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise MigrationError(
                f"migration {migration.number:04d}_{migration.name} failed: {exc}"
            ) from exc
        current = migration.number
    return current


# --------------------------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------------------------


def connect_writer(database: Path | None = None) -> sqlite3.Connection:
    """Open the single writer connection, creating the file and its directory if needed."""
    target = paths.database_file() if database is None else database
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target, autocommit=True, check_same_thread=False)
    try:
        connection.row_factory = sqlite3.Row
        mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise sqlite3.OperationalError(f"cannot switch to WAL (journal_mode={mode})")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS:d}")
    except BaseException:
        connection.close()
        raise
    return connection


def connect_reader(
    database: Path | None = None, *, check_same_thread: bool = True
) -> sqlite3.Connection:
    """Open a read-only connection (``file:…?mode=ro``). The database must already exist."""
    target = (paths.database_file() if database is None else database).resolve()
    connection = sqlite3.connect(
        f"{target.as_uri()}?mode=ro",
        uri=True,
        autocommit=True,
        check_same_thread=check_same_thread,
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS:d}")
    except BaseException:
        connection.close()
        raise
    return connection


def open_store(database: Path | None = None) -> sqlite3.Connection:
    """The writer connection with every migration applied."""
    connection = connect_writer(database)
    try:
        migrate(connection)
    except BaseException:
        connection.close()
        raise
    return connection
