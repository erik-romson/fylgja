"""I/O seams of the norn collector (``PLAN-final-plan.md`` WP3.3(b)).

The collector reads and never writes:

- the **state files** norn leaves next to a state key (R3): ``<stem>.history``, the sibling
  ``<stem>.runs/<n>/run.json`` and the end of ``<stem>.runs/<n>/events.jsonl``. They are found by
  walking the configured roots (:func:`find_state_files`) to a fixed depth, and read through a
  :class:`~fylgja.collectors.seams.FileSystem`: only the end of a ``.history`` file is read (a
  long run's file is tens of megabytes), and only a file changed within the age limit is opened;
- the **process table** (``ps -A -o pid=,args=``) and the working directories of the candidates
  (``lsof``), through a :class:`~fylgja.collectors.seams.ProcessRunner`. This runs only for
  unfinished runs the plugin never reported: nothing in the files says whether such a run lives;
- **pid liveness** (``kill(pid, 0)``), a seam so tests need no process.

Prompts and outputs are never read: ``stages/*.json`` is never opened, and the parsers keep names,
statuses, ids and times only. :func:`default_seams` builds the real seams; tests replace it.
"""

from __future__ import annotations

import os
import posixpath
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from fylgja.collectors.codex.adapter import (
    CommandFailure,
    LocalFileSystem,
    SubprocessRunner,
    list_open_files,
)
from fylgja.collectors.norn import parse
from fylgja.collectors.norn.state import Run, ScanProcess, argv_matches
from fylgja.collectors.seams import FileSystem, PathResolver, ProcessRunner, RangeReader
from fylgja.models import ReportSection

__all__ = [
    "CommandFailure",
    "LocalFileSystem",
    "Seams",
    "StateFile",
    "default_seams",
    "find_state_files",
    "list_candidates",
    "parse_ps_args",
    "pid_alive",
    "read_events_tail",
    "read_history",
    "read_reports",
    "read_run_header",
    "read_tail",
    "run_dirs",
]

HISTORY_SUFFIX: Final = ".history"
RUNS_SUFFIX: Final = ".runs"
CHECKPOINT_SUFFIX: Final = ".checkpoint"
SKIP_DIRECTORIES: Final = frozenset(
    {"node_modules", "target", "dist", "build", "__pycache__", "venv", "Library", "Pods"}
)
"""Directories the walk never enters (besides hidden ones): they are large and hold no state."""
MAX_ENTRIES: Final = 50_000
"""The most directory entries one walk lists; a walk that reaches it stops (and says so)."""
HISTORY_TAIL_BYTES: Final = 512 * 1024
HISTORY_TAIL_MAX_BYTES: Final = 8 * 1024 * 1024
EVENTS_TAIL_BYTES: Final = 128 * 1024
RUN_JSON_MAX_BYTES: Final = 256 * 1024
REPORT_MAX_BYTES: Final = 256 * 1024


def pid_alive(pid: int) -> bool:
    """Whether a process with ``pid`` exists (``kill(pid, 0)``; another user's counts)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@dataclass(frozen=True)
class Seams:
    runner: ProcessRunner
    fs: FileSystem
    pid_alive: Callable[[int], bool] = pid_alive


def default_seams() -> Seams:
    """The real process runner, filesystem and pid probe."""
    return Seams(runner=SubprocessRunner(), fs=LocalFileSystem())


# --------------------------------------------------------------------------------------------
# State files
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StateFile:
    """A ``<stem>.history`` file of norn."""

    directory: str
    stem: str
    path: str
    mtime_ns: int
    size: int

    @property
    def runs_dir(self) -> str:
        return posixpath.join(self.directory, self.stem + RUNS_SUFFIX)


@dataclass
class WalkResult:
    files: list[StateFile]
    unreadable: int = 0
    roots_found: int = 0
    truncated: bool = False


def find_state_files(
    fs: FileSystem,
    roots: Iterable[str],
    directories: Iterable[str],
    *,
    max_depth: int,
    min_mtime: float,
    max_entries: int = MAX_ENTRIES,
) -> WalkResult:
    """The ``.history`` files under ``roots`` (to ``max_depth``) and directly in ``directories``.

    A file counts when a sibling ``.checkpoint`` or ``.runs`` shows it is norn's, and when it was
    modified at ``min_mtime`` or later. Hidden directories and :data:`SKIP_DIRECTORIES` are not
    entered, symbolic links to directories are listed like directories, and a directory that
    cannot be listed is counted and skipped.
    """
    result = WalkResult(files=[])
    seen: set[str] = set()
    budget = [max_entries]

    def visit(directory: str, depth: int, *, descend: bool) -> None:
        if directory in seen:
            return
        seen.add(directory)
        try:
            names = fs.listdir(directory)
        except FileNotFoundError:
            return
        except OSError:
            result.unreadable += 1
            return
        budget[0] -= len(names)
        present = set(names)
        for name in names:
            if name.endswith(HISTORY_SUFFIX) and len(name) > len(HISTORY_SUFFIX):
                stem = name[: -len(HISTORY_SUFFIX)]
                if (stem + CHECKPOINT_SUFFIX) in present or (stem + RUNS_SUFFIX) in present:
                    _add_file(directory, stem, name)
        if not descend or depth >= max_depth:
            return
        for name in names:
            if budget[0] <= 0:
                result.truncated = True
                return
            if name.startswith(".") or name in SKIP_DIRECTORIES or name.endswith(RUNS_SUFFIX):
                continue
            child = posixpath.join(directory, name)
            try:
                is_directory = fs.is_dir(child)
            except OSError:
                continue
            if is_directory:
                visit(child, depth + 1, descend=True)

    def _add_file(directory: str, stem: str, name: str) -> None:
        path = posixpath.join(directory, name)
        try:
            info = fs.stat(path)
        except OSError:
            result.unreadable += 1
            return
        if info.mtime >= min_mtime:
            result.files.append(
                StateFile(
                    directory=directory,
                    stem=stem,
                    path=path,
                    mtime_ns=info.mtime_ns or round(info.mtime * 1e9),
                    size=info.size,
                )
            )

    for root in roots:
        if fs.is_dir(root):
            result.roots_found += 1
            visit(root, 0, descend=True)
    for directory in directories:
        if fs.is_dir(directory):
            visit(directory, 0, descend=False)
    result.files.sort(key=lambda f: f.path)
    return result


def read_tail(fs: FileSystem, path: str, size: int, limit: int) -> tuple[bytes, bool]:
    """The last ``limit`` bytes of a file of ``size`` bytes, and whether that is all of it."""
    if size <= limit:
        return fs.read_bytes(path), True
    if isinstance(fs, RangeReader):
        return fs.read_range(path, size - limit, limit), False
    return fs.read_bytes(path)[-limit:], False


def read_history(fs: FileSystem, file: StateFile) -> dict[int, parse.HistoryRecord]:
    """The records of a history file. The tail grows until it holds a whole line (to a cap)."""
    limit = HISTORY_TAIL_BYTES
    while True:
        data, complete = read_tail(fs, file.path, file.size, limit)
        records = parse.parse_history_tail(data, complete=complete)
        if records or complete or limit >= HISTORY_TAIL_MAX_BYTES:
            return records
        limit *= 4


def run_dirs(fs: FileSystem, file: StateFile, *, newest: int) -> list[tuple[str, str]]:
    """``(name, path)`` of the ``newest`` highest-numbered run directories of a state key."""
    try:
        names = fs.listdir(file.runs_dir)
    except (FileNotFoundError, NotADirectoryError):
        return []
    numbered = sorted((n for n in names if n.isascii() and n.isdigit()), key=int, reverse=True)
    return [(name, posixpath.join(file.runs_dir, name)) for name in numbered[:newest]]


def read_run_header(fs: FileSystem, directory: str) -> parse.RunHeader | None:
    """The header of ``<directory>/run.json``; ``None`` when it is missing, large or not JSON."""
    path = posixpath.join(directory, "run.json")
    try:
        if fs.stat(path).size > RUN_JSON_MAX_BYTES:
            return None
        return parse.parse_run_json(fs.read_bytes(path))
    except OSError:
        return None


def read_events_tail(fs: FileSystem, directory: str) -> parse.EventsTail | None:
    """The summary of the end of ``<directory>/events.jsonl``; ``None`` when it is missing."""
    path = posixpath.join(directory, "events.jsonl")
    try:
        size = fs.stat(path).size
        data, complete = read_tail(fs, path, size, EVENTS_TAIL_BYTES)
    except OSError:
        return None
    return parse.parse_events_tail(data, complete=complete)


def _below(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _read_report_file(
    fs: FileSystem, resolver: PathResolver, root: str, path: str, *, newer_than: float | None
) -> str | None:
    """A report file's text, or ``None`` when it is missing, outside ``root``, large, older than
    ``newer_than`` (an earlier run's) or not text. Both sides of the containment are resolved."""
    try:
        if not _below(resolver.realpath(path), root):
            return None
        info = fs.stat(path)
        if info.is_dir or info.size > REPORT_MAX_BYTES:
            return None
        if newer_than is not None and info.mtime < newer_than:
            return None
        return fs.read_bytes(path).decode("utf-8", errors="replace")
    except OSError:
        return None


def read_reports(fs: FileSystem, run: Run) -> list[ReportSection]:
    """The report sections an ``implement_features*`` run left, cut as J21 says.

    Reads ``followups.md``, ``handoff.md`` and ``review.md`` of the plan directory and the run
    report its ``README.md`` or ``index.md`` names. A file counts when its real path is below the
    real path of the run's state directory and it is at most 256 KiB; the report files must also
    be newer than the run's start, so an earlier run's are left alone. A filesystem that cannot
    resolve paths reads nothing.
    """
    if not isinstance(fs, PathResolver) or run.started_at is None:
        return []
    state_dir = run.state_dir
    if state_dir is None and run.state_key is not None:
        state_dir = parse.state_dir_and_stem(run.state_key, run.cwd)[0]
    plan = parse.plan_dir(run, lambda path: fs.exists(posixpath.join(path, "index.md")))
    if state_dir is None or plan is None:
        return []
    try:
        root = fs.realpath(state_dir)
    except OSError:
        return []
    started = run.started_at.timestamp()
    texts: dict[str, str | None] = {
        name: _read_report_file(
            fs,
            fs,
            root,
            posixpath.join(plan, name),
            newer_than=None if name in _PLAN_FILES else started,
        )
        for name in (*_PLAN_FILES, *_REPORT_FILES)
    }
    sections: list[ReportSection] = []
    sections += _cut(plan, "followups.md", texts)
    sections += _cut(plan, "handoff.md", texts)
    sections += _cut(plan, "review.md", texts, has_followups=texts["followups.md"] is not None)
    relative = parse.report_path(texts["README.md"], texts["index.md"])
    if relative is not None and run.cwd:
        path = posixpath.normpath(posixpath.join(run.cwd, relative))
        text = _read_report_file(fs, fs, root, path, newer_than=started)
        if text is not None:
            sections += parse.cut_report(path, text)
    return parse.assemble(sections)


_PLAN_FILES: Final = ("README.md", "index.md")
_REPORT_FILES: Final = ("followups.md", "handoff.md", "review.md")


def _cut(
    plan: str, name: str, texts: dict[str, str | None], *, has_followups: bool = True
) -> list[ReportSection]:
    text = texts[name]
    if text is None:
        return []
    return parse.cut_report(posixpath.join(plan, name), text, has_followups=has_followups)


# --------------------------------------------------------------------------------------------
# Processes
# --------------------------------------------------------------------------------------------


def parse_ps_args(text: str) -> list[tuple[int, tuple[str, ...]]]:
    """``(pid, argv)`` of every line of ``ps -A -o pid=,args=`` output."""
    found: list[tuple[int, tuple[str, ...]]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isascii() and parts[0].isdigit():
            found.append((int(parts[0]), tuple(parts[1:])))
    return found


async def list_candidates(
    runner: ProcessRunner,
    *,
    ps: str,
    lsof: str,
    wanted: Sequence[Sequence[str]],
    timeout: float,  # noqa: ASYNC109 - a subprocess timeout
) -> list[ScanProcess]:
    """The processes whose command line holds one of the ``wanted`` argument vectors.

    ``ps`` lists every process with its arguments; only a process that matches an argument vector
    gets its working directory asked of ``lsof`` (one call for all of them).
    Raises :class:`CommandFailure`.
    """
    table = await _ps_table(runner, ps, timeout)
    candidates = [
        (pid, argv) for pid, argv in table if any(argv_matches(want, argv) for want in wanted)
    ]
    cwds = await _cwds(runner, lsof, [pid for pid, _ in candidates], timeout)
    return [ScanProcess(pid=pid, argv=argv, cwd=cwds.get(pid)) for pid, argv in candidates]


async def _ps_table(
    runner: ProcessRunner,
    ps: str,
    timeout: float,  # noqa: ASYNC109 - a subprocess timeout
) -> list[tuple[int, tuple[str, ...]]]:
    argv = [ps, "-A", "-o", "pid=,args="]
    try:
        result = await runner.run(argv, timeout=timeout)
    except FileNotFoundError as exc:
        raise CommandFailure("ps_missing", f"ps not found: {ps}") from exc
    except PermissionError as exc:
        raise CommandFailure("ps_not_executable", "ps is not executable") from exc
    except TimeoutError as exc:
        raise CommandFailure("ps_timeout", "ps timed out") from exc
    except OSError as exc:
        raise CommandFailure("ps_failed", f"cannot run ps: {type(exc).__name__}") from exc
    if result.returncode != 0:
        raise CommandFailure("ps_failed", f"ps exited with status {result.returncode}")
    return parse_ps_args(result.stdout.decode("utf-8", errors="replace"))


async def _cwds(
    runner: ProcessRunner,
    lsof: str,
    pids: Collection[int],
    timeout: float,  # noqa: ASYNC109 - a subprocess timeout
) -> dict[int, str]:
    files = await list_open_files(runner, lsof, pids, timeout=timeout)
    return {pid: item.cwd for pid, item in files.items() if item.cwd is not None}
