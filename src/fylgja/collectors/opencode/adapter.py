"""I/O seams of the OpenCode collector (``PLAN-final-plan.md`` WP3.2).

The collector never starts a process itself and never writes:

- ``pgrep``, ``lsof`` and ``ps`` go through a :class:`~fylgja.collectors.seams.ProcessRunner`.
  The command helpers, the real runner and the filesystem are the ones of the Codex collector's
  adapter (:mod:`fylgja.collectors.codex.adapter`): the process scan is the same, so there is one
  copy of it;
- ``opencode.db`` is opened by :class:`OpenCodeDb`, read-only through a ``mode=ro`` URI with
  ``query_only`` on, so no statement of ours can write and no file is created. The queries use
  fixed table and column names; ids travel as parameters. Message and part rows are reduced in SQL
  to roles, times, tool names and a few short tool-input fields: prompt and reply text is never
  read. ``auth.json`` and the other files of the OpenCode data directory are never opened.

:func:`default_seams` builds the real runner and filesystem; tests replace it and use
:class:`~fylgja.testing.fakes.FakeProcessRunner`.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self

from fylgja.collectors.codex.adapter import (
    CommandFailure,
    LocalFileSystem,
    Seams,
    SubprocessRunner,
    list_open_files,
    list_pids,
    read_process_table,
)
from fylgja.collectors.opencode import parse

__all__ = [
    "CommandFailure",
    "LocalFileSystem",
    "OpenCodeDb",
    "OpenCodeDbError",
    "Seams",
    "SubprocessRunner",
    "default_db_path",
    "default_seams",
    "list_open_files",
    "list_pids",
    "read_process_table",
]

BUSY_TIMEOUT_SECONDS: Final = 1.0
MESSAGES_READ: Final = 8
"""How many of a session's newest messages are read to find its newest assistant message."""


def default_db_path() -> Path:
    """``$XDG_DATA_HOME/opencode/opencode.db``, else ``~/.local/share/opencode/opencode.db``.

    The environment is read at call time. A relative ``XDG_DATA_HOME`` is ignored, as the XDG
    specification says.
    """
    base = os.environ.get("XDG_DATA_HOME", "")
    root = Path(base) if base and os.path.isabs(base) else Path.home() / ".local" / "share"  # noqa: PTH117
    return root / "opencode" / "opencode.db"


def default_seams() -> Seams:
    """The real process runner and filesystem."""
    return Seams(runner=SubprocessRunner(), fs=LocalFileSystem())


class OpenCodeDbError(Exception):
    """The database could not be read. ``kind`` is a short code and ``detail`` is safe to show."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def _valid(column: str, path: str) -> str:
    """``json_extract`` of ``path`` in ``column``, or NULL when the JSON is not valid."""
    return f"CASE WHEN json_valid({column}) THEN json_extract({column}, '{path}') END"


_MESSAGE_SQL: Final = f"""
SELECT id, time_created, time_updated,
       {_valid("data", "$.role")} AS role,
       {_valid("data", "$.time.completed")} AS completed,
       CASE WHEN json_valid(data) AND json_extract(data, '$.error') IS NOT NULL
            THEN 1 ELSE 0 END AS errored
FROM message WHERE session_id = ?
ORDER BY time_created DESC, id DESC LIMIT {MESSAGES_READ}
"""  # noqa: S608 - fixed text; the only parameter is bound

_INPUT_COLUMNS: Final = ", ".join(
    f"{_valid('data', '$.state.input.' + name)} AS {name}" for name in parse.INPUT_FIELDS
)

_PART_SQL: Final = f"""
SELECT time_updated,
       {_valid("data", "$.type")} AS type,
       {_valid("data", "$.tool")} AS tool,
       {_valid("data", "$.state.status")} AS status,
       {_INPUT_COLUMNS}
FROM part WHERE message_id = ?
ORDER BY id DESC LIMIT {parse.MAX_CHAIN_PARTS}
"""  # noqa: S608 - fixed text; the only parameter is bound


class OpenCodeDb:
    """A read-only view of ``opencode.db``.

    Opened through ``file:<path>?mode=ro``: SQLite refuses every write and creates no file, also
    when the database is in WAL mode and OpenCode is writing to it.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._connection.row_factory = sqlite3.Row

    @classmethod
    def open(cls, path: Path | str) -> Self:
        uri = Path(path).absolute().as_uri() + "?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_SECONDS)
            connection.execute("PRAGMA query_only = ON")
        except sqlite3.Error as exc:
            raise OpenCodeDbError(
                "db_unreadable", f"cannot open the OpenCode database ({type(exc).__name__})"
            ) from exc
        return cls(connection)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def _query(self, sql: str, params: Sequence[object] = ()) -> list[sqlite3.Row]:
        try:
            return self._connection.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise OpenCodeDbError(
                "db_unreadable", f"cannot read the OpenCode database ({type(exc).__name__})"
            ) from exc

    @staticmethod
    def _mapping(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in row.keys()}  # noqa: SIM118 - sqlite3.Row has no items()

    def columns(self) -> dict[str, list[str]]:
        """The column names of the tables the collector reads; an absent table is absent."""
        found: dict[str, list[str]] = {}
        for table in parse.REQUIRED_COLUMNS:
            names = [str(row["name"]) for row in self._query(f"PRAGMA table_info({table})")]
            if names:
                found[table] = names
        return found

    def check_schema(self) -> dict[str, list[str]]:
        """The columns found. ``schema_mismatch`` when a column the collector needs is missing."""
        found = self.columns()
        missing = parse.missing_columns(found)
        if missing:
            raise OpenCodeDbError(
                "schema_mismatch", "the OpenCode database lacks columns: " + ", ".join(missing)
            )
        return found

    def sessions(
        self, directories: Collection[str], columns: Mapping[str, Sequence[str]]
    ) -> dict[str, list[parse.SessionRow]]:
        """The top-level, not archived sessions of ``directories``, by directory, newest first."""
        if not directories:
            return {}
        have = set(columns.get("session", ()))
        wanted = [
            *parse.REQUIRED_COLUMNS["session"],
            *(c for c in parse.OPTIONAL_SESSION_COLUMNS if c in have),
        ]
        marks = ",".join("?" for _ in directories)
        rows = self._query(
            f"SELECT {', '.join(wanted)} FROM session"  # noqa: S608
            f" WHERE parent_id IS NULL AND time_archived IS NULL AND directory IN ({marks})"
            " ORDER BY time_updated DESC, id",
            sorted(directories),
        )
        found: dict[str, list[parse.SessionRow]] = {}
        for raw in rows:
            row = parse.session_row(self._mapping(raw))
            if row is not None:
                found.setdefault(row.directory, []).append(row)
        return found

    def messages(self, session_id: str) -> list[parse.MessageRow]:
        """The newest messages of the session, newest first (roles and times only)."""
        rows = (
            parse.message_row(self._mapping(r)) for r in self._query(_MESSAGE_SQL, [session_id])
        )
        return [row for row in rows if row is not None]

    def tool_parts(self, message_id: str) -> list[parse.PartRow]:
        """The newest tool parts of a message, newest first."""
        rows = (parse.part_row(self._mapping(r)) for r in self._query(_PART_SQL, [message_id]))
        return [row for row in rows if row is not None]

    def todos(self, session_id: str) -> list[parse.TodoRow]:
        """The session's task list in position order."""
        rows = self._query(
            "SELECT content, status, position FROM todo WHERE session_id = ? ORDER BY position",
            [session_id],
        )
        parsed = (parse.todo_row(self._mapping(r)) for r in rows)
        return [row for row in parsed if row is not None]
