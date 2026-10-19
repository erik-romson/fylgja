"""The AeroSpace collector (``PLAN-final-plan.md`` WP1.6, §3.3; ``PLAN.md`` §4.2).

Three cooperating loops run under one task group:

- the stream loop keeps ``aerospace subscribe --all`` open and restarts it when it ends, with the
  supervisor's backoff (1 s doubling, jittered, reset after a healthy stretch);
- the debounce loop takes a snapshot 250 ms after the last focus event;
- the timer loop takes a snapshot immediately and then every ``snapshot_interval_seconds``.

Each snapshot lists windows and workspaces and emits two **complete** snapshots, so a closed window
is closed in the store. A missing or broken AeroSpace never crashes the collector: the loops log the
first failure, keep retrying, and :meth:`AerospaceCollector.doctor` reports ``degraded``.
"""

from __future__ import annotations

import asyncio
import random
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from fylgja.collectors.aerospace import parse
from fylgja.collectors.aerospace.adapter import Seams
from fylgja.collectors.base import CollectorContext
from fylgja.collectors.seams import LineStream
from fylgja.collectors.supervisor import Backoff, BackoffPolicy, RandomSource
from fylgja.models import HealthReport, HealthState

__all__ = ["AerospaceCollector", "AerospaceSettings", "StreamStats"]

STREAM_POLICY: Final = BackoffPolicy(initial=1.0, maximum=60.0, reset_after=60.0)
MAX_DEBOUNCE_WAIT: Final = 1.0
"""A burst of focus events delays the snapshot by at most this many seconds."""


class AerospaceSettings(BaseModel):
    """``[collectors.aerospace]``. The binary comes from ``[paths] aerospace``."""

    model_config = ConfigDict(extra="forbid")

    snapshot_interval_seconds: float = Field(default=30.0, gt=0)
    focus_debounce_ms: int = Field(default=250, ge=0)
    command_timeout_seconds: float = Field(default=5.0, gt=0)


class StreamStats:
    """Counters of what the stream delivered. Never holds line contents."""

    def __init__(self) -> None:
        self.events = 0
        self.unknown_events = 0
        self.malformed: dict[str, int] = {}
        self.restarts = 0

    @property
    def malformed_total(self) -> int:
        return sum(self.malformed.values())


class _Failure(Exception):
    """A command failed. ``kind`` is a short code and ``detail`` is safe to show."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


class AerospaceCollector:
    def __init__(
        self,
        settings: AerospaceSettings,
        context: CollectorContext,
        *,
        binary: str,
        seams: Seams,
        rng: RandomSource | None = None,
    ) -> None:
        self._settings = settings
        self._context = context
        self._binary = binary
        self._seams = seams
        self._rng: RandomSource = random.Random() if rng is None else rng
        self._trigger = asyncio.Event()
        self._lock = asyncio.Lock()
        self._focus_workspace: str | None = None
        self._known_windows: dict[str, parse.WindowInfo] = {}
        self._snapshot_ok: bool | None = None
        self._stream_ok: bool | None = None
        self.stats = StreamStats()
        self.snapshots = 0
        """Snapshots that reached the sink (a window and a workspace snapshot count once)."""

    # -- commands ---------------------------------------------------------------------------

    async def _run(self, *args: str) -> bytes:
        """Run one command; raise :class:`_Failure` unless it exits 0."""
        try:
            result = await self._seams.runner.run(
                [self._binary, *args], timeout=self._settings.command_timeout_seconds
            )
        except FileNotFoundError as exc:
            raise _Failure("binary_missing", f"aerospace binary not found: {self._binary}") from exc
        except PermissionError as exc:
            raise _Failure(
                "binary_not_executable", f"aerospace binary is not executable: {self._binary}"
            ) from exc
        except TimeoutError as exc:
            raise _Failure("timeout", f"aerospace {args[0]} timed out") from exc
        except OSError as exc:
            raise _Failure("os_error", f"cannot run aerospace: {type(exc).__name__}") from exc
        if result.returncode != 0:
            raise _Failure(
                "server_unreachable",
                f"aerospace {args[0]} exited with status {result.returncode}",
            )
        return result.stdout

    # -- snapshots --------------------------------------------------------------------------

    async def snapshot(self) -> bool:
        """List windows and workspaces and emit both as complete snapshots.

        Returns whether both lists were emitted. Failures are logged on change, never raised.
        """
        async with self._lock:
            try:
                windows = parse.parse_windows(
                    await self._run(
                        "list-windows",
                        "--monitor",
                        "all",
                        "--json",
                        "--format",
                        parse.WINDOW_FORMAT,
                    )
                )
                workspaces = parse.parse_workspaces(
                    await self._run(
                        "list-workspaces", "--all", "--json", "--format", parse.WORKSPACE_FORMAT
                    )
                )
            except _Failure as failure:
                self._note_snapshot(failure.kind)
                return False
            except parse.SnapshotParseError:
                self._note_snapshot("bad_output")
                return False
            now = self._context.clock.now()
            self._known_windows = {w.window_id: w for w in windows.windows}
            self._context.sink.emit(parse.window_snapshot(windows.windows, now))
            self._context.sink.emit(parse.workspace_snapshot(workspaces.workspaces, now))
            self.snapshots += 1
            skipped = windows.skipped + workspaces.skipped
            if skipped:
                self._context.logger.debug("skipped %d unusable snapshot entries", skipped)
            self._note_snapshot(None)
            return True

    def _note_snapshot(self, failure_kind: str | None) -> None:
        ok = failure_kind is None
        if ok and self._snapshot_ok is False:
            self._context.logger.info("aerospace snapshots recovered")
        elif not ok and self._snapshot_ok is not False:
            self._context.logger.warning("aerospace snapshot failed: %s", failure_kind)
        self._snapshot_ok = ok

    # -- events -----------------------------------------------------------------------------

    def handle_line(self, line: bytes) -> None:
        """Parse one stream line, emit focus records and arm the snapshot debounce."""
        parsed = parse.parse_event_line(line)
        if parsed is None:
            return
        if isinstance(parsed, parse.MalformedLine):
            self.stats.malformed[parsed.reason] = self.stats.malformed.get(parsed.reason, 0) + 1
            return
        self.stats.events += 1
        match parsed:
            case parse.UnknownEvent():
                self.stats.unknown_events += 1
            case parse.FocusChanged():
                self._focus(parsed.window_id, parsed.workspace)
            case parse.FocusedWorkspaceChanged() | parse.FocusedMonitorChanged():
                # The focused window follows the workspace: a different workspace has none known.
                if parsed.workspace is not None and parsed.workspace != self._focus_workspace:
                    self._focus(None, parsed.workspace)
        if parse.triggers_snapshot(parsed):
            self._trigger.set()

    def _focus(self, window_id: str | None, workspace: str | None) -> None:
        known = None if window_id is None else self._known_windows.get(window_id)
        self._focus_workspace = workspace
        self._context.sink.emit(
            parse.focus_event(
                self._context.clock.now(),
                window_id=window_id,
                workspace=workspace,
                app_bundle_id=None if known is None else known.app_bundle_id,
                app_name=None if known is None else known.app_name,
                title=None if known is None else known.title,
            )
        )

    # -- loops ------------------------------------------------------------------------------

    async def run(self) -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(self._timer_loop(), name="aerospace:timer")
            group.create_task(self._debounce_loop(), name="aerospace:debounce")
            await self._stream_loop()

    async def _timer_loop(self) -> None:
        while True:
            await self.snapshot()
            await self._context.clock.sleep(self._settings.snapshot_interval_seconds)

    async def _debounce_loop(self) -> None:
        clock = self._context.clock
        delay = self._settings.focus_debounce_ms / 1000
        while True:
            await self._trigger.wait()
            first = clock.monotonic()
            while True:
                self._trigger.clear()
                await clock.sleep(delay)
                if not self._trigger.is_set() or clock.monotonic() - first >= MAX_DEBOUNCE_WAIT:
                    break
            await self.snapshot()

    async def _stream_loop(self) -> None:
        clock = self._context.clock
        backoff = Backoff(STREAM_POLICY, self._rng)
        argv = [self._binary, "subscribe", "--all"]
        while True:
            started = clock.monotonic()
            try:
                async with self._seams.open_stream(argv) as stream:
                    await self._read(stream)
                reason = "stream ended"
            except FileNotFoundError:
                reason = "binary missing"
            except (OSError, TimeoutError) as exc:
                reason = f"stream failed ({type(exc).__name__})"
            if clock.monotonic() - started >= STREAM_POLICY.reset_after:
                backoff.reset()
            delay = backoff.next_delay()
            self.stats.restarts += 1
            if self._stream_ok is not False:
                self._context.logger.warning(
                    "aerospace subscribe: %s; retry in %.1fs", reason, delay
                )
            self._stream_ok = False
            await clock.sleep(delay)

    async def _read(self, stream: LineStream) -> None:
        while True:
            line = await stream.readline()
            if not line:
                return
            self._stream_ok = True
            self.handle_line(line)

    # -- health -----------------------------------------------------------------------------

    async def doctor(self) -> HealthReport:
        """Binary present, version known, ``list-workspaces`` answers. No side effects."""
        now = self._context.clock.now()
        try:
            version = parse.parse_version(await self._run("--version"))
            await self._run("list-workspaces", "--all", "--count")
        except _Failure as failure:
            return HealthReport(
                state=HealthState.DEGRADED,
                detail=failure.detail,
                error_kind=failure.kind,
                checked_at=now,
            )
        detail = "aerospace ready" if version is None else f"aerospace {version}"
        return HealthReport(state=HealthState.OK, detail=detail, checked_at=now)
