"""Builders for the daemon and API tests (the ``tests/`` tree has no packages).

Collectors here are synthetic plugins behind synthetic entry points, named for what they do in a
test (``wm``, ``agents``), never after a built-in: the daemon must work with any plugin. Records
are pushed through each collector's ingest-gate sink, so they take the same path as a real
collector's: validation, privacy filter, writer, live view, resolver.
"""

from __future__ import annotations

import asyncio
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from fylgja.api.app import create_app
from fylgja.collectors.base import (
    COLLECTOR_API_MAJOR,
    Capability,
    Collector,
    CollectorContext,
    CollectorPlugin,
)
from fylgja.collectors.registry import EntryPointLike
from fylgja.config import Config, parse_config
from fylgja.core.daemon import Daemon
from fylgja.core.hosts import HostData
from fylgja.models import (
    FocusEvent,
    FocusPayload,
    HealthReport,
    HealthState,
    ProcessRef,
    SessionActivity,
    SessionActivitySnapshot,
    SessionPayload,
    SessionSnapshot,
    SnapshotItem,
    WindowPayload,
    WindowSnapshot,
    WorkspacePayload,
    WorkspaceSnapshot,
)
from fylgja.testing.fakes import FakeClock, FakeFS

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
PORT = 7319
BASE_URL = f"http://127.0.0.1:{PORT}"
HOME = "/home/tester"


# --------------------------------------------------------------------------------------------
# Synthetic collectors
# --------------------------------------------------------------------------------------------


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass
class Synthetic:
    """A plugin a test controls: what ``doctor`` reports, whether ``run`` crashes, and an
    optional ``emit`` hook run once at start."""

    name: str
    capabilities: frozenset[Capability]
    builtin: bool = True
    doctor_state: HealthState = HealthState.OK
    crash: bool = False
    emit: Callable[[CollectorContext], None] | None = None
    contexts: list[CollectorContext] = field(default_factory=list)

    def plugin(self) -> CollectorPlugin:
        return CollectorPlugin(
            name=self.name,
            api_major=COLLECTOR_API_MAJOR,
            capabilities=self.capabilities,
            config_model=Settings,
            factory=self._factory,
        )

    def _factory(self, settings: BaseModel, context: CollectorContext) -> Collector:
        self.contexts.append(context)
        return _Run(self, context)


class _Run:
    def __init__(self, spec: Synthetic, context: CollectorContext) -> None:
        self._spec = spec
        self._context = context

    async def run(self) -> None:
        if self._spec.crash:
            raise RuntimeError("synthetic crash")
        if self._spec.emit is not None:
            self._spec.emit(self._context)
        await asyncio.Event().wait()

    async def doctor(self) -> HealthReport:
        state = self._spec.doctor_state
        kind = None if state is HealthState.OK else "synthetic"
        return HealthReport(state=state, error_kind=kind, checked_at=self._context.clock.now())


@dataclass(frozen=True)
class Dist:
    name: str
    version: str = "1.0.0"


@dataclass
class EntryPoint:
    name: str
    value: str
    dist: Dist | None
    target: object

    def load(self) -> object:
        return self.target


def entry_points(*specs: Synthetic) -> Callable[[], list[EntryPointLike]]:
    eps: list[EntryPointLike] = [
        EntryPoint(
            name=spec.name,
            value=f"synthetic.{spec.name}:plugin",
            dist=Dist("fylgja" if spec.builtin else "fylgja-synthetic"),
            target=spec.plugin(),
        )
        for spec in specs
    ]
    return lambda: list(eps)


def windows_collector(name: str = "wm") -> Synthetic:
    return Synthetic(name, frozenset({Capability.WINDOWS}))


def sessions_collector(name: str = "agents") -> Synthetic:
    return Synthetic(name, frozenset({Capability.SESSIONS}))


def config(user: Mapping[str, Any] | None = None) -> Config:
    return parse_config(dict(user or {}), source="test-config.toml")


async def no_hosts() -> HostData:
    return HostData(home=HOME)


def make_daemon(
    *specs: Synthetic,
    user_config: Mapping[str, Any] | None = None,
    clock: FakeClock | None = None,
    fs: FakeFS | None = None,
    host_loader: Callable[[], Awaitable[HostData]] | None = None,
    **options: Any,
) -> Daemon:
    """A daemon over ``specs`` on a fake clock, a fake filesystem and no host data."""
    return Daemon(
        config(user_config),
        entry_points=entry_points(*specs),
        clock=FakeClock(T0) if clock is None else clock,
        fs=FakeFS() if fs is None else fs,
        host_loader=no_hosts if host_loader is None else host_loader,
        home=HOME,
        resolve_debounce=options.pop("resolve_debounce", 0.0),
        **options,
    )


def client(daemon: Daemon, *, base_url: str = BASE_URL) -> httpx.AsyncClient:
    """An HTTPX client over the ASGI app (no lifespan: the test starts the daemon)."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(daemon)), base_url=base_url
    )


@asynccontextmanager
async def lifespan(app: Any) -> AsyncIterator[None]:
    """Run ``app``'s ASGI lifespan as a server does (through its middleware), around the body."""
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}, "state": {}}
    task = asyncio.create_task(app(scope, inbox.get, outbox.put))
    await inbox.put({"type": "lifespan.startup"})
    started = await asyncio.wait_for(outbox.get(), 30)
    assert started["type"] == "lifespan.startup.complete", started
    try:
        yield
    finally:
        await inbox.put({"type": "lifespan.shutdown"})
        stopped = await asyncio.wait_for(outbox.get(), 30)
        assert stopped["type"] == "lifespan.shutdown.complete", stopped
        await asyncio.wait_for(task, 30)


# --------------------------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Win:
    window_id: str
    workspace: str
    app: str
    pid: int
    title: str = ""
    bundle: str = "com.example.app"


def window_snapshot(source: str, windows: Iterable[Win], at: datetime) -> WindowSnapshot:
    return WindowSnapshot(
        source=source,
        entity_id="all",
        observed_at=at,
        complete=True,
        items=tuple(
            SnapshotItem(
                entity_id=w.window_id,
                payload=WindowPayload(
                    workspace=w.workspace,
                    app_bundle_id=w.bundle,
                    app_name=w.app,
                    app_pid=w.pid,
                    title=w.title,
                ),
            )
            for w in windows
        ),
    )


def workspace_snapshot(
    source: str, names: Sequence[str], at: datetime, *, focused: str | None = None
) -> WorkspaceSnapshot:
    return WorkspaceSnapshot(
        source=source,
        entity_id="all",
        observed_at=at,
        complete=True,
        items=tuple(
            SnapshotItem(
                entity_id=name, payload=WorkspacePayload(name=name, focused=name == focused)
            )
            for name in names
        ),
    )


def focus_event(source: str, window_id: str, workspace: str, at: datetime) -> FocusEvent:
    return FocusEvent(
        source=source,
        entity_id="focus",
        observed_at=at,
        payload=FocusPayload(window_id=window_id, workspace=workspace),
    )


def chain(*links: tuple[int, int, str]) -> tuple[ProcessRef, ...]:
    return tuple(ProcessRef(pid=pid, ppid=ppid, comm=comm) for pid, ppid, comm in links)


def session(
    kind: str,
    native_id: str,
    *,
    pid: int | None = None,
    cwd: str | None = None,
    host_pid: int | None = None,
    **extra: Any,
) -> SessionPayload:
    """A session payload; ``host_pid`` builds the chain session → shell → host app."""
    ancestry = None
    if pid is not None and host_pid is not None:
        ancestry = chain((pid, pid - 1, kind), (pid - 1, host_pid, "zsh"), (host_pid, 1, "host"))
    return SessionPayload(
        kind=kind, native_id=native_id, pid=pid, cwd=cwd, ancestry=ancestry, **extra
    )


def session_snapshot(
    source: str, payloads: Iterable[SessionPayload], at: datetime
) -> SessionSnapshot:
    return SessionSnapshot(
        source=source,
        entity_id="all",
        observed_at=at,
        complete=True,
        items=tuple(SnapshotItem(entity_id=f"{p.kind}:{p.native_id}", payload=p) for p in payloads),
    )


def activity_snapshot(
    source: str, items: Mapping[str, SessionActivity], at: datetime
) -> SessionActivitySnapshot:
    return SessionActivitySnapshot(
        source=source,
        entity_id="all",
        observed_at=at,
        complete=True,
        items=tuple(SnapshotItem(entity_id=key, payload=a) for key, a in items.items()),
    )


# --------------------------------------------------------------------------------------------
# JSON helpers
# --------------------------------------------------------------------------------------------


def nulls(value: Any, path: str = "$") -> Iterator[str]:
    """The JSON paths whose value is ``null``."""
    if value is None:
        yield path
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from nulls(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from nulls(item, f"{path}[{index}]")


def by_key(items: Iterable[Mapping[str, Any]], key: str) -> dict[str, Mapping[str, Any]]:
    return {str(item[key]): item for item in items}
