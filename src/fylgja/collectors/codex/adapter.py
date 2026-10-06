"""I/O seams of the Codex collector (``PLAN-final-plan.md`` WP3.1).

The collector never starts a process or opens a file itself:

- ``pgrep``, ``lsof`` and ``ps`` go through a :class:`~fylgja.collectors.seams.ProcessRunner`;
- rollout files are read through a :class:`~fylgja.collectors.seams.FileSystem` (the tail only, when
  the filesystem can read a range);
- the state database is opened by :class:`StateDb`, read-only through a ``mode=ro`` URI with
  ``query_only`` on, so no statement of ours can write.

:func:`default_seams` builds the real runner and filesystem; tests replace it and use
:class:`~fylgja.testing.fakes.FakeProcessRunner`. Nothing here writes, and no secret is read:
``auth.json``, ``history.jsonl`` and the logs databases of the Codex directory are never opened.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self

from fylgja.collectors.codex import parse
from fylgja.collectors.seams import (
    FileInfo,
    FileSystem,
    ProcessResult,
    ProcessRunner,
    RangeReader,
)
from fylgja.collectors.supervisor import terminate_process_group

__all__ = [
    "CommandFailure",
    "LocalFileSystem",
    "RolloutRead",
    "Seams",
    "StateDb",
    "StateDbError",
    "SubprocessRunner",
    "default_codex_home",
    "default_seams",
    "list_open_files",
    "list_pids",
    "read_rollout_tail",
    "read_process_table",
    "real_path",
]

BUSY_TIMEOUT_SECONDS: Final = 1.0


def default_codex_home() -> Path:
    """``~/.codex``, with ``HOME`` read at call time."""
    return Path.home() / ".codex"


def real_path(path: Path | str) -> str:
    """The path with symlinks resolved (``lsof`` reports real paths)."""
    return os.path.realpath(path)


class LocalFileSystem:
    """The real filesystem."""

    def read_bytes(self, path: Path | str) -> bytes:
        with open(path, "rb") as handle:  # noqa: PTH123
            return handle.read()

    def read_range(self, path: Path | str, offset: int, length: int) -> bytes:
        with open(path, "rb") as handle:  # noqa: PTH123
            handle.seek(offset)
            return handle.read(length)

    def read_text(self, path: Path | str, encoding: str = "utf-8") -> str:
        return self.read_bytes(path).decode(encoding)

    def exists(self, path: Path | str) -> bool:
        return os.path.exists(path)  # noqa: PTH110

    def is_dir(self, path: Path | str) -> bool:
        return os.path.isdir(path)  # noqa: PTH112

    def listdir(self, path: Path | str) -> list[str]:
        return sorted(os.listdir(path))

    def stat(self, path: Path | str) -> FileInfo:
        info = os.stat(path)  # noqa: PTH116
        return FileInfo(
            size=info.st_size,
            mtime=info.st_mtime,
            is_dir=os.path.isdir(path),  # noqa: PTH112
            ino=info.st_ino,
            mtime_ns=info.st_mtime_ns,
        )

    def unlink(self, path: Path | str) -> None:
        os.unlink(path)  # noqa: PTH108


class SubprocessRunner:
    """Runs a command to completion; a missing binary raises ``FileNotFoundError``."""

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - mirrors subprocess timeouts
        stdin: bytes | None = None,
    ) -> ProcessResult:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL if stdin is None else asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout)
        except BaseException:  # timeout or cancellation: leave nothing running
            await terminate_process_group(process)
            raise
        code = -1 if process.returncode is None else process.returncode
        return ProcessResult(tuple(argv), code, stdout, stderr)


@dataclass(frozen=True)
class Seams:
    runner: ProcessRunner
    fs: FileSystem


def default_seams() -> Seams:
    """The real process runner and filesystem."""
    return Seams(runner=SubprocessRunner(), fs=LocalFileSystem())


# --------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------


class CommandFailure(Exception):
    """A command could not tell. ``kind`` is a short code and ``detail`` is safe to show."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


async def _run(
    runner: ProcessRunner,
    label: str,
    argv: Sequence[str],
    *,
    timeout: float,  # noqa: ASYNC109 - a subprocess timeout
) -> ProcessResult:
    """Run ``argv``; exit status 0 and 1 are answers (``pgrep`` and ``lsof`` exit 1 for "none")."""
    try:
        result = await runner.run(argv, timeout=timeout)
    except FileNotFoundError as exc:
        raise CommandFailure(f"{label}_missing", f"{label} not found: {argv[0]}") from exc
    except PermissionError as exc:
        raise CommandFailure(f"{label}_not_executable", f"{label} is not executable") from exc
    except TimeoutError as exc:
        raise CommandFailure(f"{label}_timeout", f"{label} timed out") from exc
    except OSError as exc:
        raise CommandFailure(
            f"{label}_failed", f"cannot run {label}: {type(exc).__name__}"
        ) from exc
    if result.returncode not in (0, 1):
        raise CommandFailure(f"{label}_failed", f"{label} exited with status {result.returncode}")
    return result


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


async def list_pids(
    runner: ProcessRunner,
    pgrep: str,
    name: str,
    *,
    timeout: float,  # noqa: ASYNC109 - a subprocess timeout
) -> list[int]:
    """The pids of this user's processes whose name is exactly ``name`` (``pgrep -x``)."""
    argv = [pgrep, "-x", "-u", str(os.geteuid()), name]
    return parse.parse_pgrep(_decode((await _run(runner, "pgrep", argv, timeout=timeout)).stdout))


async def list_open_files(
    runner: ProcessRunner,
    lsof: str,
    pids: Collection[int],
    *,
    timeout: float,  # noqa: ASYNC109 - a subprocess timeout
) -> dict[int, parse.OpenFiles]:
    """The working directory and open rollouts of ``pids`` (``lsof -Fpfn``, mapped files left out).

    A pid that has exited since ``pgrep`` is simply absent from the answer.
    """
    if not pids:
        return {}
    argv = [
        lsof,
        "-nP",
        "-a",
        "-p",
        ",".join(str(pid) for pid in sorted(pids)),
        "-d",
        "^txt,^mem",
        "-Fpfn",
    ]
    return parse.parse_lsof_fields(
        _decode((await _run(runner, "lsof", argv, timeout=timeout)).stdout)
    )


async def read_process_table(
    runner: ProcessRunner,
    ps: str,
    *,
    timeout: float,  # noqa: ASYNC109 - a subprocess timeout
) -> dict[int, parse.ProcessEntry]:
    """Every process with its parent, start time and name from one ``ps`` call."""
    argv = [ps, "-A", "-o", "pid=,ppid=,lstart=,comm="]
    return parse.parse_process_table(
        _decode((await _run(runner, "ps", argv, timeout=timeout)).stdout)
    )


# --------------------------------------------------------------------------------------------
# Rollout files
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RolloutRead:
    """The end of a rollout file as lines, oldest first."""

    lines: list[bytes]
    size: int
    mtime_ns: int
    complete: bool
    """Whether the lines start at the beginning of the file."""


def read_rollout_tail(fs: FileSystem, path: Path | str, limit: int) -> RolloutRead:
    """The last ``limit`` bytes of ``path`` as whole lines.

    When the file is longer, the partial first line is dropped. A filesystem that cannot read a
    range is read whole and sliced.
    """
    info = fs.stat(path)
    if info.size <= limit:
        data = fs.read_bytes(path)
        return RolloutRead(data.split(b"\n"), len(data), info.mtime_ns, complete=True)
    if isinstance(fs, RangeReader):
        data = fs.read_range(path, info.size - limit, limit)
    else:
        data = fs.read_bytes(path)[-limit:]
    _, newline, rest = data.partition(b"\n")
    lines = rest.split(b"\n") if newline else []
    return RolloutRead(lines, info.size, info.mtime_ns, complete=False)


# --------------------------------------------------------------------------------------------
# The state database
# --------------------------------------------------------------------------------------------


class StateDbError(Exception):
    """The state database could not be read. ``detail`` is safe to show."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


class StateDb:
    """A read-only view of ``state_N.sqlite``.

    Opened through ``file:<path>?mode=ro``: SQLite refuses every write and creates no file. The
    collector queries only fixed table and column names; thread ids travel as parameters.
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
            raise StateDbError(
                "state_db_unreadable",
                f"cannot open the Codex state database ({type(exc).__name__})",
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
            raise StateDbError(
                "state_db_unreadable",
                f"cannot read the Codex state database ({type(exc).__name__})",
            ) from exc

    def columns(self, table: str = "threads") -> list[str]:
        """The column names of ``table``; empty when the table does not exist."""
        return [str(row["name"]) for row in self._query(f"PRAGMA table_info({table})")]

    def threads(self, thread_ids: Collection[str]) -> dict[str, Mapping[str, Any]]:
        """The ``threads`` rows of ``thread_ids`` by id.

        :class:`StateDbError` of kind ``state_schema_mismatch`` when a column the collector needs is
        missing: the version number matched but the table is not the one that was revalidated.
        """
        if not thread_ids:
            return {}
        present = self.columns()
        missing = parse.missing_columns(present)
        if missing:
            raise StateDbError(
                "state_schema_mismatch", "the threads table lacks columns: " + ", ".join(missing)
            )
        wanted = [
            *parse.REQUIRED_THREAD_COLUMNS,
            *(c for c in parse.OPTIONAL_THREAD_COLUMNS if c in present),
        ]
        marks = ",".join("?" for _ in thread_ids)
        rows = self._query(
            f"SELECT {', '.join(wanted)} FROM threads WHERE id IN ({marks})",  # noqa: S608
            sorted(thread_ids),
        )
        return {str(row["id"]): {key: row[key] for key in row.keys()} for row in rows}  # noqa: SIM118

    def spawn_children(self, thread_ids: Collection[str]) -> set[str]:
        """The ids among ``thread_ids`` that are sub-agent threads of another thread.

        A database without the ``thread_spawn_edges`` table has none.
        """
        if not thread_ids or "child_thread_id" not in self.columns("thread_spawn_edges"):
            return set()
        marks = ",".join("?" for _ in thread_ids)
        rows = self._query(
            f"SELECT child_thread_id FROM thread_spawn_edges WHERE child_thread_id IN ({marks})",  # noqa: S608
            sorted(thread_ids),
        )
        return {str(row[0]) for row in rows}
