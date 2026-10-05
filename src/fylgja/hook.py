"""``fylgja hook <Event>``: the stdlib-only, silent, bounded inbox writer (WP2.1, A2–A5).

Claude Code runs this as an async command hook on every event (``PLAN.md`` §4.3). It must stay
cheap and harmless:

- it imports nothing outside the stdlib (``fylgja.paths`` is stdlib-only too);
- it never writes to stdout or stderr: :func:`main` points fds 1 and 2 at ``/dev/null`` first;
- it always terminates within 3 s: a ``signal.alarm`` backstop calls ``os._exit(0)``;
- it always exits 0;
- it writes one mode-0600 envelope file atomically into ``hook-inbox/<source>/``.

``fylgja hook StatusLine`` instead replaces ``statusline/<session_id>.json`` with the latest
snapshot (A4). The hook exits at once when ``FYLGJA_SUMMARY`` or ``LIDSKJALV_TRACK_SUMMARY`` is
set, so summarizer sessions are never ingested.

The envelope is a stable contract (``docs/api/inbox-envelope.md``): the norn and OpenCode
writers produce it too. The logic is plain functions that tests call in-process; only
:func:`main` adds the fd redirect and the alarm around :func:`run`.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import select
import signal
import stat
import time
import uuid

from fylgja import paths

# ``typing`` costs a few milliseconds to import, and the hook pays that on every event. Every
# annotation is a string (``from __future__ import annotations``), so the names are only needed
# by the type checker, which treats a bare ``TYPE_CHECKING`` as true.
TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path
    from types import FrameType
    from typing import Any, Final, NoReturn

__all__ = [
    "ENV_ALLOWLIST",
    "FYLGJA_VERSION",
    "SCHEMA",
    "StdinRead",
    "backlog_full",
    "build_envelope",
    "cap_payload",
    "capture_env",
    "deliver_event",
    "encode_envelope",
    "ensure_private_dir",
    "increment_dropped",
    "main",
    "parse_args",
    "read_stdin",
    "run",
    "scan_backlog",
    "summary_guard_active",
    "write_atomic",
    "write_statusline",
]

SCHEMA: Final = "fylgja.hook/1"
"""The envelope contract version (``docs/api/inbox-envelope.md``)."""

FYLGJA_VERSION: Final = "0.1.0"
"""The package version, kept as a constant because ``importlib.metadata`` costs about 20 ms.
``tests/unit/test_hook_envelope.py`` keeps it equal to the installed distribution's version."""

DEFAULT_SOURCE: Final = "claude"
STATUSLINE_EVENT: Final = "StatusLine"
SUMMARY_GUARD_VARS: Final = ("FYLGJA_SUMMARY", "LIDSKJALV_TRACK_SUMMARY")

ENV_ALLOWLIST: Final = (
    # The session id, the pid and the messaging socket path (PLAN.md §4.3).
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_PID",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    # The terminal identifiers of A3.
    "CMUX_WORKSPACE_ID",
    "CMUX_SURFACE_ID",
    "CMUX_TAB_ID",
    "CMUX_PANEL_ID",
    "ITERM_SESSION_ID",
    "TERM_SESSION_ID",
    "TERM_PROGRAM",
    "CLAUDE_CODE_BRIDGE_SESSION_ID",
)
"""The only environment variables an envelope may carry (A3). Never the messaging token."""

ENV_VALUE_MAX_CHARS: Final = 256
EVENT_ARG_MAX_CHARS: Final = 64
SOURCE_MAX_CHARS: Final = 64

ALARM_SECONDS: Final = 3
STDIN_DEADLINE_SECONDS: Final = 2.0
READ_CHUNK_BYTES: Final = 64 * 1024
VERBATIM_LIMIT_BYTES: Final = 256 * 1024
"""Input up to this size is kept verbatim."""
STRING_LIMIT_BYTES: Final = 4 * 1024
"""Above :data:`VERBATIM_LIMIT_BYTES`, longer string values become ``{"_truncated": n}``."""
READ_LIMIT_BYTES: Final = 4 * 1024 * 1024
"""Input beyond this is drained and discarded, and the payload becomes the oversize marker."""

TRUNCATED_KEY: Final = "_truncated"
OVERSIZE_KEY: Final = "_malformed_oversize"

BACKLOG_CHECK_INTERVAL_NS: Final = 60 * 1_000_000_000
BACKLOG_MAX_FILES: Final = 100_000
BACKLOG_MAX_BYTES: Final = 512 * 1024 * 1024
BACKLOG_STAMP: Final = ".backlog-stamp"
DROPPED_COUNT: Final = "dropped.count"
STATUSLINE_MIN_INTERVAL_NS: Final = 2 * 1_000_000_000

_VERDICT_FULL: Final = b"full\n"
_VERDICT_OK: Final = b"ok\n"
_PRIVATE_FILE_FLAGS: Final = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC
_SESSION_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


# --------------------------------------------------------------------------------------------
# Arguments, guard and environment
# --------------------------------------------------------------------------------------------


def parse_args(argv: Sequence[str]) -> tuple[str, str | None, bool]:
    """``(source, event, help_requested)`` from the arguments after ``hook``.

    ``--source NAME`` or ``--source=NAME`` picks the inbox (default ``claude``). The first
    positional argument is the event. Other flags are ignored so that newer installers can pass
    flags this version does not know.
    """
    source = DEFAULT_SOURCE
    event: str | None = None
    help_requested = False
    args = iter(argv)
    for arg in args:
        if arg == "--source":
            source = next(args, "")
        elif arg.startswith("--source="):
            source = arg.removeprefix("--source=")
        elif arg in ("-h", "--help"):
            help_requested = True
        elif arg.startswith("-"):
            continue
        elif event is None:
            event = arg[:EVENT_ARG_MAX_CHARS]
    return source, event, help_requested


def summary_guard_active(environ: Mapping[str, str]) -> bool:
    """True in a summarizer session: ``FYLGJA_SUMMARY`` or ``LIDSKJALV_TRACK_SUMMARY`` is set."""
    return any(environ.get(name) for name in SUMMARY_GUARD_VARS)


def capture_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The non-empty A3 allowlisted variables, each cut to :data:`ENV_VALUE_MAX_CHARS`."""
    captured: dict[str, str] = {}
    for name in ENV_ALLOWLIST:
        value = environ.get(name)
        if value:
            captured[name] = value[:ENV_VALUE_MAX_CHARS]
    return captured


# --------------------------------------------------------------------------------------------
# Reading stdin and capping the payload
# --------------------------------------------------------------------------------------------


class StdinRead:
    """What :func:`read_stdin` got: the kept bytes, the total read, and whether EOF came."""

    __slots__ = ("complete", "data", "total")

    def __init__(self, data: bytes, total: int, complete: bool) -> None:
        self.data = data
        self.total = total
        """Bytes read, including any beyond :data:`READ_LIMIT_BYTES` that were discarded."""
        self.complete = complete
        """True when stdin reached EOF (or was unreadable) before the deadline."""

    def __repr__(self) -> str:
        return f"StdinRead(kept={len(self.data)}, total={self.total}, complete={self.complete})"


def read_stdin(
    fd: int = 0,
    *,
    deadline_seconds: float = STDIN_DEADLINE_SECONDS,
    limit: int = READ_LIMIT_BYTES,
) -> StdinRead:
    """Read ``fd`` until EOF or the deadline, with ``select`` and ``os.read``.

    At most ``limit`` bytes are kept. Anything beyond is still read and discarded, so the
    writer is never left blocked on a full pipe; only the count is kept.
    """
    chunks: list[bytes] = []
    kept = 0
    total = 0
    complete = False
    end = time.monotonic() + deadline_seconds
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        try:
            ready, _, _ = select.select([fd], [], [], remaining)
        except (OSError, ValueError):
            complete = True
            break
        if not ready:
            break
        try:
            chunk = os.read(fd, READ_CHUNK_BYTES)
        except BlockingIOError:
            continue
        except OSError:
            complete = True
            break
        if not chunk:
            complete = True
            break
        total += len(chunk)
        if kept < limit:
            piece = chunk[: limit - kept]
            chunks.append(piece)
            kept += len(piece)
    return StdinRead(b"".join(chunks), total, complete)


def _utf8_length(value: str) -> int:
    return len(value.encode("utf-8", errors="surrogatepass"))


def _cap_strings(value: Any) -> Any:
    """Replace every string value over :data:`STRING_LIMIT_BYTES` with ``{"_truncated": n}``."""
    if isinstance(value, str):
        # A character is at most 4 bytes, so short strings need no encoding to be measured.
        if len(value) * 4 <= STRING_LIMIT_BYTES:
            return value
        size = _utf8_length(value)
        return {TRUNCATED_KEY: size} if size > STRING_LIMIT_BYTES else value
    if isinstance(value, dict):
        return {key: _cap_strings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_cap_strings(item) for item in value]
    return value


def oversize_marker(size: int) -> str:
    """The payload text that replaces input too large to keep and impossible to parse."""
    return json.dumps({OVERSIZE_KEY: size}, separators=(",", ":"))


def cap_payload(read: StdinRead) -> tuple[str, bool]:
    """``(payload_text, truncated)`` for what was read from stdin.

    - Up to 256 KiB: the input verbatim (decoded as UTF-8, invalid bytes replaced), truncated
      only when stdin did not reach EOF before the deadline.
    - Above that: the parsed JSON with long strings replaced by ``{"_truncated": n}``.
    - Above that and unparseable (or beyond the read limit): ``{"_malformed_oversize": n}``.
    """
    if read.total > len(read.data):
        return oversize_marker(read.total), True
    raw = read.data
    if len(raw) <= VERBATIM_LIMIT_BYTES:
        return raw.decode("utf-8", errors="replace"), not read.complete
    try:
        capped = _cap_strings(json.loads(raw.decode("utf-8")))
        text = json.dumps(capped, separators=(",", ":"))
    except (ValueError, RecursionError):
        return oversize_marker(read.total), True
    return text, True


# --------------------------------------------------------------------------------------------
# The envelope
# --------------------------------------------------------------------------------------------


def build_envelope(
    *,
    source: str,
    event: str | None,
    payload_text: str,
    truncated: bool,
    environ: Mapping[str, str],
    received_at_ns: int,
    ppid: int | None = None,
    envelope_id: str | None = None,
) -> dict[str, Any]:
    """The ``fylgja.hook/1`` envelope (``docs/api/inbox-envelope.md``)."""
    return {
        "schema": SCHEMA,
        "id": envelope_id or str(uuid.uuid4()),
        "received_at_ns": received_at_ns,
        "source": source,
        "event_arg": event,
        "ppid": os.getppid() if ppid is None else ppid,
        "env": capture_env(environ),
        "payload_text": payload_text,
        "truncated": truncated,
        "fylgja_version": FYLGJA_VERSION,
    }


def encode_envelope(envelope: Mapping[str, Any]) -> bytes:
    """Compact, ASCII-only JSON: lone surrogates and any other text survive the round trip."""
    return json.dumps(envelope, separators=(",", ":")).encode("ascii") + b"\n"


# --------------------------------------------------------------------------------------------
# Directories and atomic files
# --------------------------------------------------------------------------------------------


def ensure_private_dir(path: Path) -> bool:
    """Create ``path`` (and missing parents) with mode 0700, then check it.

    False, and nothing written, when the path is a symlink, not a directory, owned by another
    user, or has any mode other than 0700.
    """
    missing: list[Path] = []
    probe = path
    try:
        while True:
            try:
                os.lstat(probe)
                break
            except FileNotFoundError:
                missing.append(probe)
                if probe.parent == probe:
                    break
                probe = probe.parent
        for directory in reversed(missing):
            try:
                os.mkdir(directory, 0o700)
            except FileExistsError:
                continue
            os.chmod(directory, 0o700)
        info = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o700
    )


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def write_atomic(directory: Path, name: str, data: bytes, *, tmp_name: str) -> Path | None:
    """Write ``directory/tmp_name`` (``O_CREAT|O_EXCL``, mode 0600), fsync it, rename to ``name``.

    The rename replaces an existing ``name``. Returns the final path, or None after removing
    the temporary file when any step fails.
    """
    tmp = directory / tmp_name
    try:
        fd = os.open(tmp, _PRIVATE_FILE_FLAGS, 0o600)
    except OSError:
        return None
    try:
        try:
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        target = directory / name
        os.rename(tmp, target)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None
    return target


# --------------------------------------------------------------------------------------------
# Backlog limit
# --------------------------------------------------------------------------------------------


def scan_backlog(inbox: Path) -> tuple[int, int]:
    """``(files, bytes)`` of the envelope files waiting in ``inbox`` (``*.json``, no dot files)."""
    files = 0
    size = 0
    with os.scandir(inbox) as entries:
        for entry in entries:
            name = entry.name
            if name.startswith(".") or not name.endswith(".json"):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                files += 1
                size += info.st_size
    return files, size


def _write_stamp(stamp: Path, verdict: bytes) -> None:
    fd = os.open(stamp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        _write_all(fd, verdict)
    finally:
        os.close(fd)


def _read_stamp(stamp: Path) -> bytes:
    try:
        fd = os.open(stamp, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return _VERDICT_OK
    try:
        return os.read(fd, 16)
    except OSError:
        return _VERDICT_OK
    finally:
        os.close(fd)


def backlog_full(
    inbox: Path,
    now_ns: int,
    *,
    max_files: int = BACKLOG_MAX_FILES,
    max_bytes: int = BACKLOG_MAX_BYTES,
) -> bool:
    """Whether the inbox holds more than ``max_files`` files or ``max_bytes`` bytes.

    The directory is scanned at most once every 60 s. The stamp file's mtime records the last
    scan and its content the verdict (``ok`` or ``full``), which later events reuse until the
    stamp is 60 s old. A stale stamp is refreshed before the scan so that concurrent hooks keep
    using the previous verdict instead of all scanning at once. Any error counts as not full.
    """
    stamp = inbox / BACKLOG_STAMP
    try:
        info: os.stat_result | None = os.lstat(stamp)
    except FileNotFoundError:
        info = None
    except OSError:
        return False
    if info is not None and 0 <= now_ns - info.st_mtime_ns < BACKLOG_CHECK_INTERVAL_NS:
        return _read_stamp(stamp) == _VERDICT_FULL
    try:
        _write_stamp(stamp, _read_stamp(stamp) if info is not None else _VERDICT_OK)
        files, size = scan_backlog(inbox)
        full = files > max_files or size > max_bytes
        _write_stamp(stamp, _VERDICT_FULL if full else _VERDICT_OK)
    except OSError:
        return False
    return full


def increment_dropped(inbox: Path) -> None:
    """Add one to the decimal counter in ``inbox/dropped.count``, under an exclusive ``flock``."""
    path = inbox / DROPPED_COUNT
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        text = os.read(fd, 32).decode("ascii", errors="replace").strip()
        count = int(text) if text.isdigit() else 0
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        _write_all(fd, f"{count + 1}\n".encode("ascii"))
    except OSError:
        return
    finally:
        os.close(fd)


# --------------------------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------------------------


def deliver_event(inbox: Path, envelope: Mapping[str, Any]) -> Path | None:
    """Write ``envelope`` into ``inbox`` as ``<received_at_ns>-<id>.json``.

    Nothing is written when the inbox is refused (:func:`ensure_private_dir`). When the backlog
    is over its limit the event is dropped and ``dropped.count`` goes up by one.
    """
    if not ensure_private_dir(inbox):
        return None
    received_at_ns = int(envelope["received_at_ns"])
    if backlog_full(inbox, received_at_ns):
        increment_dropped(inbox)
        return None
    envelope_id = str(envelope["id"])
    return write_atomic(
        inbox,
        f"{received_at_ns}-{envelope_id}.json",
        encode_envelope(envelope),
        tmp_name=f".tmp-{envelope_id}",
    )


def _statusline_session_id(payload_text: str, environ: Mapping[str, str]) -> str | None:
    try:
        payload = json.loads(payload_text)
    except (ValueError, RecursionError):
        payload = None
    candidate = payload.get("session_id") if isinstance(payload, dict) else None
    if not isinstance(candidate, str):
        candidate = environ.get("CLAUDE_CODE_SESSION_ID")
    if candidate and _SESSION_ID.fullmatch(candidate):
        return candidate
    return None


def write_statusline(
    envelope: Mapping[str, Any], environ: Mapping[str, str], directory: Path | None = None
) -> Path | None:
    """Replace ``statusline/<session_id>.json`` with ``envelope`` (A4).

    The session id comes from the payload (or ``CLAUDE_CODE_SESSION_ID``). Nothing is written
    when it is missing or not a plain id, when the directory is refused, or when the existing
    snapshot is under 2 s old.
    """
    session_id = _statusline_session_id(str(envelope["payload_text"]), environ)
    if session_id is None:
        return None
    target_dir = paths.statusline_dir() if directory is None else directory
    if not ensure_private_dir(target_dir):
        return None
    name = f"{session_id}.json"
    received_at_ns = int(envelope["received_at_ns"])
    try:
        age = received_at_ns - os.lstat(target_dir / name).st_mtime_ns
    except FileNotFoundError:
        age = None
    except OSError:
        return None
    if age is not None and 0 <= age < STATUSLINE_MIN_INTERVAL_NS:
        return None
    return write_atomic(
        target_dir, name, encode_envelope(envelope), tmp_name=f".tmp-{envelope['id']}"
    )


def run(
    argv: Sequence[str],
    *,
    stdin_fd: int = 0,
    environ: Mapping[str, str] | None = None,
    received_at_ns: int | None = None,
) -> Path | None:
    """The hook without the fd redirect and the alarm. Returns the file written, if any."""
    env: Mapping[str, str] = os.environ if environ is None else environ
    if summary_guard_active(env):
        return None
    received = time.time_ns() if received_at_ns is None else received_at_ns
    source, event, help_requested = parse_args(argv)
    if help_requested or len(source) > SOURCE_MAX_CHARS:
        return None
    try:
        inbox = paths.inbox_dir(source)
    except ValueError:
        return None
    payload_text, truncated = cap_payload(read_stdin(stdin_fd))
    envelope = build_envelope(
        source=source,
        event=event,
        payload_text=payload_text,
        truncated=truncated,
        environ=env,
        received_at_ns=received,
    )
    if event == STATUSLINE_EVENT:
        return write_statusline(envelope, env)
    return deliver_event(inbox, envelope)


# --------------------------------------------------------------------------------------------
# The process wrapper
# --------------------------------------------------------------------------------------------


def _silence_stdio() -> None:
    """Point fds 1 and 2 at ``/dev/null``, so nothing can reach Claude's terminal or transcript."""
    try:
        null = os.open(os.devnull, os.O_WRONLY | os.O_CLOEXEC)
    except OSError:
        return
    for fd in (1, 2):
        try:
            os.dup2(null, fd)
        except OSError:
            continue
    if null > 2:
        os.close(null)


def _on_alarm(signum: int, frame: FrameType | None) -> None:
    os._exit(0)


def _arm_backstop() -> None:
    """After :data:`ALARM_SECONDS`, end the process with exit code 0 whatever it is doing."""
    signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(ALARM_SECONDS)


def main(argv: Sequence[str]) -> NoReturn:
    """Run the hook silently and exit 0. ``argv`` is everything after ``hook``."""
    received_at_ns = time.time_ns()
    _silence_stdio()
    _arm_backstop()
    try:
        run(argv, received_at_ns=received_at_ns)
    except BaseException:
        # The hook must never fail visibly: whatever went wrong, the event is just not written.
        pass
    os._exit(0)
