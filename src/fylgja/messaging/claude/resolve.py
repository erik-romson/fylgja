"""Target resolution for a local Claude session message (``PLAN.md`` §4.3.1, A9, A13, A14).

A message goes only to a session Fylgja can prove is the live owner of a safe socket:

1. **Registry row.** The native session id is looked up in the live registry
   (``~/.claude/sessions``, or ``[collectors.claude] registry_dir``). Only names matching the A13
   glob ``^[0-9]+\\.json$`` are opened; ``*.key`` files and everything else are never read. Rows
   are parsed by the collector's strict parser (:func:`fylgja.collectors.claude.parse.parse_row`).
2. **Liveness.** The row's pid must belong to a running process whose start time equals the row's
   ``procStart`` (within a second). A dead pid is a stale row (``unreachable``); a different start
   time is a reused pid (``refused-locally``). A row without ``procStart`` cannot be proven and is
   refused. Several rows with the same session id are resolved by liveness; two live ones are
   refused as ambiguous.
3. **A9 gates, per target.** The row's own ``version`` must parse and be at least 2.1.224, and its
   ``peerProtocol`` must be one Fylgja knows (:data:`KNOWN_PEER_PROTOCOLS`). Anything else is
   ``unsupported``. The installed CLI's version is never used.
4. **A14 socket rules.** The socket path must be absolute and normalized, its directory must be one
   of the allowed roots (``/tmp/cc-socks`` or ``/tmp/cc-socks-<uid>``, injectable so tests never
   touch them), that directory must be a real directory (``lstat``: not a symlink) owned by the uid
   with mode 0700, the socket itself must be a socket (``lstat``: not a symlink) owned by the uid,
   and the path must equal the registry row's path.
5. **Right before connecting**, :meth:`Resolver.confirm` reads the row again and repeats the
   liveness check and the socket rules, so a row rewritten, a process gone or a socket swapped since
   resolution stops the send.

A socket path never comes from the caller: the API refuses any path-like field
(:func:`supplied_socket_fields`) and nothing here takes one.

Failures raise :class:`Refusal` with the receipt state and a short ``error_kind``; no message text,
file content or path is put in them.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from fylgja.collectors.claude import parse
from fylgja.collectors.claude.adapter import PsFailure, ps_start_times
from fylgja.collectors.seams import FileSystem, ProcessRunner
from fylgja.messaging.claude.encoders import Version
from fylgja.messaging.claude.receipts import ReceiptState

__all__ = [
    "KNOWN_PEER_PROTOCOLS",
    "MIN_VERSION",
    "PS",
    "Refusal",
    "Resolver",
    "StartTime",
    "Target",
    "check_socket",
    "default_roots",
    "parse_version",
    "ps_start_time",
    "read_row",
    "read_rows",
    "supplied_socket_fields",
    "version_gate",
]

MIN_VERSION: Final[Version] = (2, 1, 224)
"""The first Claude Code with the documented session inbox on macOS and Linux."""
KNOWN_PEER_PROTOCOLS: Final = frozenset({1})
"""``peerProtocol`` values Fylgja has seen documented in registry rows (R5)."""
PS: Final = "/bin/ps"
PS_TIMEOUT: Final = 1.0
SOCKET_DIR_MODE: Final = 0o700

_VERSION: Final = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?")
_PATH_FIELD: Final = re.compile(r"(?i)sock|path|address|peer")

StartTime = Callable[[int], Awaitable[float | None]]
"""The start time (epoch seconds) of the process with that pid, or ``None`` when there is none.
Raises :class:`Refusal` when it cannot tell."""
Lstat = Callable[[str], os.stat_result]


class Refusal(Exception):
    """Fylgja will not send to this target. ``kind`` is a short code, safe to store and show."""

    def __init__(self, state: ReceiptState, kind: str) -> None:
        super().__init__(f"{state.value}: {kind}")
        self.state = state
        self.kind = kind


def default_roots(uid: int | None = None) -> tuple[str, ...]:
    """The A14 socket directories: ``/tmp/cc-socks`` and ``/tmp/cc-socks-<uid>``."""
    owner = os.getuid() if uid is None else uid
    return ("/tmp/cc-socks", f"/tmp/cc-socks-{owner}")  # noqa: S108 - Claude Code's own paths


def parse_version(text: str | None) -> Version | None:
    """``"2.1.289"`` (optionally with a pre-release or build suffix) as ``(2, 1, 289)``."""
    if text is None:
        return None
    match = _VERSION.fullmatch(text.strip())
    if match is None:
        return None
    return int(match[1]), int(match[2]), int(match[3])


def version_gate(version: str | None, peer_protocol: int | None) -> tuple[Version, int]:
    """The A9 gate of one target: its own version and peer protocol, or an ``unsupported``
    :class:`Refusal`."""
    parsed = parse_version(version)
    if parsed is None:
        raise Refusal(ReceiptState.UNSUPPORTED, "version_unknown")
    if parsed < MIN_VERSION:
        raise Refusal(ReceiptState.UNSUPPORTED, "version_too_old")
    if peer_protocol is None or peer_protocol not in KNOWN_PEER_PROTOCOLS:
        raise Refusal(ReceiptState.UNSUPPORTED, "peer_protocol_unknown")
    return parsed, peer_protocol


def supplied_socket_fields(body: Mapping[str, object]) -> list[str]:
    """The fields of an API body that look like a socket path or address; the API refuses them."""
    return sorted(key for key in body if _PATH_FIELD.search(key))


# --------------------------------------------------------------------------------------------
# A14
# --------------------------------------------------------------------------------------------


def _refused(kind: str) -> Refusal:
    return Refusal(ReceiptState.REFUSED_LOCALLY, kind)


def check_socket(
    path: str,
    *,
    registry_path: str | None,
    roots: Iterable[str],
    uid: int,
    lstat: Lstat = os.lstat,
) -> None:
    """Apply the A14 rules to ``path``; raise :class:`Refusal` on the first that fails."""
    if registry_path is None or path != registry_path:
        raise _refused("socket_path_mismatch")
    if not os.path.isabs(path) or os.path.normpath(path) != path:
        raise _refused("socket_path_not_normal")
    parent = os.path.dirname(path)
    if parent not in {os.path.normpath(root) for root in roots}:
        raise _refused("socket_outside_allowed_roots")
    try:
        directory = lstat(parent)
    except FileNotFoundError:
        raise Refusal(ReceiptState.UNREACHABLE, "socket_dir_missing") from None
    except OSError:
        raise _refused("socket_dir_unreadable") from None
    if stat.S_ISLNK(directory.st_mode):
        raise _refused("socket_dir_symlink")
    if not stat.S_ISDIR(directory.st_mode):
        raise _refused("socket_dir_not_a_directory")
    if directory.st_uid != uid:
        raise _refused("socket_dir_owner")
    if stat.S_IMODE(directory.st_mode) != SOCKET_DIR_MODE:
        raise _refused("socket_dir_mode")
    try:
        target = lstat(path)
    except FileNotFoundError:
        raise Refusal(ReceiptState.UNREACHABLE, "socket_missing") from None
    except OSError:
        raise _refused("socket_unreadable") from None
    if stat.S_ISLNK(target.st_mode):
        raise _refused("socket_symlink")
    if not stat.S_ISSOCK(target.st_mode):
        raise _refused("not_a_socket")
    if target.st_uid != uid:
        raise _refused("socket_owner")


# --------------------------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------------------------


def read_row(fs: FileSystem, directory: Path, pid: int) -> parse.RegistryRow | None:
    """``<directory>/<pid>.json`` as a usable row, or ``None``."""
    try:
        data = fs.read_bytes(directory / f"{pid}.json")
    except OSError:
        return None
    row = parse.parse_row(data, file_pid=pid)
    return row if isinstance(row, parse.RegistryRow) else None


def read_rows(fs: FileSystem, directory: Path) -> list[parse.RegistryRow]:
    """Every usable row of the registry directory. Only A13 names (``^[0-9]+\\.json$``) are
    opened; ``*.key`` files and anything else are never read. An unreadable directory has none."""
    try:
        names = fs.listdir(directory)
    except OSError:
        return []
    found: list[parse.RegistryRow] = []
    for name in names:
        pid = parse.registry_pid(name)
        if pid is None:
            continue
        row = read_row(fs, directory, pid)
        if row is not None:
            found.append(row)
    return found


# --------------------------------------------------------------------------------------------
# Liveness
# --------------------------------------------------------------------------------------------


def ps_start_time(runner: ProcessRunner, ps: str = PS) -> StartTime:
    """A :data:`StartTime` that asks ``ps -o pid=,lstart= -p <pid>``."""

    async def start_time(pid: int) -> float | None:
        try:
            starts = await ps_start_times(runner, ps, [pid], timeout=PS_TIMEOUT)
        except PsFailure:
            raise _refused("liveness_unverified") from None
        return starts.get(pid)

    return start_time


def _alive(row: parse.RegistryRow, started_at: float | None) -> None:
    """Raise unless ``row``'s process is the one that wrote it."""
    if row.proc_start is None:
        raise _refused("no_proc_start")
    liveness = parse.check_liveness(row, started_at)
    if liveness is parse.Liveness.DEAD:
        raise Refusal(ReceiptState.UNREACHABLE, "stale_row")
    if liveness is parse.Liveness.REUSED:
        raise _refused("pid_reused")


@dataclass(frozen=True)
class Target:
    """A resolved, gated and checked message target."""

    session_id: str
    pid: int
    socket_path: str
    proc_start: datetime
    """The row's ``procStart`` (naive UTC)."""
    version: Version
    peer_protocol: int

    @property
    def proc_start_epoch(self) -> float:
        return self.proc_start.replace(tzinfo=UTC).timestamp()


class Resolver:
    """Resolves native session ids against the registry directory (see the module docstring)."""

    def __init__(
        self,
        registry_dir: Path,
        *,
        fs: FileSystem,
        start_time: StartTime,
        roots: Iterable[str] | None = None,
        uid: int | None = None,
        lstat: Lstat = os.lstat,
    ) -> None:
        self.registry_dir = registry_dir
        self._fs = fs
        self._start_time = start_time
        self.uid = os.getuid() if uid is None else uid
        self.roots = tuple(default_roots(self.uid) if roots is None else roots)
        self._lstat = lstat

    # -- the registry ---------------------------------------------------------------------------

    def _read(self, pid: int) -> parse.RegistryRow | None:
        return read_row(self._fs, self.registry_dir, pid)

    def rows(self) -> list[parse.RegistryRow]:
        """Every usable registry row (A13 names only); an unreadable directory gives none."""
        return read_rows(self._fs, self.registry_dir)

    async def _live_row(self, session_id: str) -> parse.RegistryRow:
        candidates = [row for row in self.rows() if row.session_id == session_id]
        if not candidates:
            raise Refusal(ReceiptState.UNREACHABLE, "no_live_row")
        if len(candidates) == 1:
            row = candidates[0]
            _alive(row, await self._start_time(row.pid))
            return row
        live: list[parse.RegistryRow] = []
        for row in candidates:
            try:
                _alive(row, await self._start_time(row.pid))
            except Refusal:
                continue
            live.append(row)
        if not live:
            raise Refusal(ReceiptState.UNREACHABLE, "stale_row")
        if len(live) > 1:
            raise _refused("ambiguous_target")
        return live[0]

    # -- resolution ---------------------------------------------------------------------------

    def check_socket(self, path: str, registry_path: str | None) -> None:
        check_socket(
            path, registry_path=registry_path, roots=self.roots, uid=self.uid, lstat=self._lstat
        )

    async def resolve(self, session_id: str) -> Target:
        """The live, gated, A14-checked target of ``session_id``; raises :class:`Refusal`."""
        row = await self._live_row(session_id)
        if row.messaging_socket_path is None:
            raise Refusal(ReceiptState.UNREACHABLE, "no_inbox_socket")
        version, protocol = version_gate(row.version, row.peer_protocol)
        self.check_socket(row.messaging_socket_path, row.messaging_socket_path)
        assert row.proc_start is not None  # _alive refused a row without it
        return Target(
            session_id=row.session_id,
            pid=row.pid,
            socket_path=row.messaging_socket_path,
            proc_start=row.proc_start,
            version=version,
            peer_protocol=protocol,
        )

    async def confirm(self, target: Target) -> None:
        """Immediately before connecting: the row, the process and the socket are unchanged."""
        row = self._read(target.pid)
        if row is None:
            raise Refusal(ReceiptState.UNREACHABLE, "stale_row")
        if row.session_id != target.session_id:
            raise Refusal(ReceiptState.UNREACHABLE, "session_moved")
        if row.proc_start != target.proc_start:
            raise _refused("pid_reused")
        _alive(row, await self._start_time(target.pid))
        self.check_socket(target.socket_path, row.messaging_socket_path)
