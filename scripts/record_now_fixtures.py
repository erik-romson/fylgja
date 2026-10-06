"""Record deterministic ``GET /now`` payloads into ``tests/fixtures/now/`` (WP1.11, Fylgja side).

Dev-only; the tests only read the fixtures. Run it by hand after a deliberate change of the
read model:

    uv run --frozen python scripts/record_now_fixtures.py [<file>.json ...]

With file names it rewrites only those; without, all of them.

It builds the app in-process (the real daemon, ingest gate, writer, resolver and API) over
synthetic collector sources, a fixed clock and a fake filesystem, pushes records through each
collector's ingest sink, and writes what ``GET /now`` answers. Nothing on the machine is read:
no AeroSpace, no cmux, no Claude registry. Four scenarios, one file each:

``states-and-hosts.json``
    Every Phase 1 session state (``idle``, ``busy``, ``waiting``) and every host kind (cmux with
    its exact surface, JetBrains, iTerm2, and a session whose host is unresolved).
``clear-predecessor.json``
    A session that replaced another in the same process (``/clear``): the same ``lane_key`` and
    a ``predecessor_key``.
``degraded-sessions.json``
    The sessions collector is degraded, so ``sessions[]`` and ``projects[]`` are absent while
    ``workspaces[]`` is still there and ``degraded`` names the collector.
``phase5-usage-builds.json``
    The §4.7 payloads (WP5.4): a Claude session with ``tokens``, ``cost_usd`` and ``context_pct``,
    a project with a failing ``build`` of two workflows (folded from the sanitized runs fixture),
    and ``usage`` with Claude's windows, Codex's windows, Copilot ``unavailable`` and the spend.
``phase3-agents.json``
    The Phase 3 acceptance payload (WP3.6): Claude, Codex, OpenCode and norn sessions, each from
    its own collector run over its sanitized fixtures (``tests/phase3_support.py``, shared with
    ``tests/integration/test_phase3_agents.py``), including a norn run waiting for input with a
    nested ``sdk-py`` session.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "fixtures" / "now"

CMUX = "com.cmuxterm.app"
JETBRAINS = "com.jetbrains.intellij"
ITERM = "com.googlecode.iterm2"
BROWSER = "com.apple.Safari"

FACTS: dict[str, Any] = {
    "title": "Fix the flaky build",
    "prompt": "make the integration build green again",
    "said": "The failing test is isolated; running the suite once more.",
    "mode": "default",
    "branch": "feature/build-fix",
    "version": "2.1.289",
    "model": "claude-opus-5",
    "turns": 4,
    "started": 1790000000,
    "last_at": 1790000540,
    "prompts": ["make the integration build green again", "run it once more"],
    "tools": {"Bash": 3, "Edit": 1},
    "files": ["/work/alpha/src/build.py"],
    "tasks": [
        {"status": "completed", "content": "isolate the failing test"},
        {"status": "in_progress", "content": "fix the build"},
    ],
}

IDLE_FACTS: dict[str, Any] = {
    "title": "Review the release notes",
    "prompt": "summarise the release notes",
    "said": "Done. The notes are summarised above.",
    "mode": "default",
    "branch": "main",
    "version": "2.1.289",
    "model": "claude-opus-5",
    "turns": 2,
    "started": 1790000100,
    "last_at": 1790000160,
    "prompts": ["summarise the release notes"],
    "tools": {},
    "files": [],
    "tasks": [],
}


def _api() -> Any:
    """``tests/api_support.py``: the builders the daemon tests use."""
    sys.path.insert(0, str(ROOT / "tests"))
    import api_support

    return api_support


async def _now(daemon: Any, api: Any) -> dict[str, Any]:
    async with api.client(daemon) as http:
        response = await http.get("/now")
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    return payload


def _host_loader(api: Any) -> Callable[[], Awaitable[Any]]:
    from fylgja.core.hosts import CmuxData, CmuxRecord, CmuxStore, HostData, JetBrainsProject

    data = HostData(
        home=api.HOME,
        cmux=CmuxData(store=CmuxStore((CmuxRecord("c-1", "ws-aaaa", "surface-1", pid=501),))),
        jetbrains={"intellijidea": (JetBrainsProject("/work/gamma", "gamma"),)},
    )

    async def load() -> Any:
        return data

    return load


def _windows(api: Any, daemon: Any) -> None:
    wm = daemon.sink("wm")
    wm.emit(api.workspace_snapshot("wm", ["1", "2", "3", "4", "5"], api.T0, focused="1"))
    wm.emit(
        api.window_snapshot(
            "wm",
            [
                api.Win("100", "1", "cmux", 500, "Shell", CMUX),
                api.Win("101", "2", "IntelliJ IDEA", 600, "gamma – Main.java", JETBRAINS),
                api.Win("102", "3", "iTerm2", 700, "Shell", ITERM),
                api.Win("103", "4", "Safari", 800, "Release notes", BROWSER),
            ],
            api.T0,
        )
    )
    wm.emit(api.focus_event("wm", "100", "1", api.T0))


async def _states_and_hosts(api: Any) -> dict[str, Any]:
    from fylgja.models import SessionActivity
    from fylgja.testing.fakes import FakeFS

    fs = FakeFS()
    for project in ("alpha", "beta", "gamma", "delta"):
        fs.add_dir(f"/work/{project}/.git")
    daemon = api.make_daemon(
        api.windows_collector(),
        api.sessions_collector(),
        fs=fs,
        host_loader=_host_loader(api),
    )
    await daemon.start()
    try:
        _windows(api, daemon)
        agents = daemon.sink("agents")
        agents.emit(
            api.session_snapshot(
                "agents",
                [
                    # cmux: busy, with the exact surface from the cmux store
                    api.session(
                        "claude",
                        "c-1",
                        pid=501,
                        cwd="/work/alpha/src",
                        host_pid=500,
                        process_key="501-1790000000",
                        entrypoint="cli",
                        version="2.1.289",
                        model="claude-opus-5",
                        branch="feature/build-fix",
                    ),
                    # JetBrains: waiting
                    api.session(
                        "claude",
                        "j-2",
                        pid=601,
                        cwd="/work/gamma/app",
                        host_pid=600,
                        process_key="601-1790000050",
                        entrypoint="cli",
                        version="2.1.289",
                        model="claude-opus-5",
                        branch="main",
                    ),
                    # iTerm2: idle
                    api.session(
                        "claude",
                        "i-3",
                        pid=701,
                        cwd="/work/beta",
                        host_pid=700,
                        process_key="701-1790000100",
                        entrypoint="cli",
                        version="2.1.289",
                        model="claude-opus-5",
                        branch="main",
                    ),
                    # no window: the host is unresolved
                    api.session(
                        "claude",
                        "u-4",
                        pid=901,
                        cwd="/work/delta",
                        process_key="901-1790000200",
                        entrypoint="cli",
                        version="2.1.289",
                    ),
                ],
                api.T0,
            )
        )
        agents.emit(
            api.activity_snapshot(
                "agents",
                {
                    "claude:c-1": SessionActivity(
                        state="busy",
                        current_tool="Bash",
                        current_target="pytest",
                        title="Fix the flaky build",
                        facts=FACTS,
                    ),
                    "claude:j-2": SessionActivity(
                        state="waiting",
                        waiting_reason="permission",
                        current_tool="Edit",
                        current_target="/work/gamma/app/Main.java",
                        title="Refactor the main class",
                    ),
                    "claude:i-3": SessionActivity(
                        state="idle",
                        title="Review the release notes",
                        facts=IDLE_FACTS,
                    ),
                    "claude:u-4": SessionActivity(state="idle"),
                },
                api.T0,
            )
        )
        await daemon.settle()
        return await _now(daemon, api)
    finally:
        await daemon.stop()


async def _clear_predecessor(api: Any) -> dict[str, Any]:
    from fylgja.models import SessionActivity
    from fylgja.testing.fakes import FakeFS

    fs = FakeFS()
    fs.add_dir("/work/alpha/.git")
    daemon = api.make_daemon(api.windows_collector(), api.sessions_collector(), fs=fs)
    await daemon.start()
    try:
        _windows(api, daemon)
        agents = daemon.sink("agents")
        # the first session of the process, then /clear rotates the session id
        agents.emit(
            api.session_snapshot(
                "agents",
                [
                    api.session(
                        "claude",
                        "old-1",
                        pid=501,
                        cwd="/work/alpha",
                        host_pid=500,
                        process_key="501-1790000000",
                        entrypoint="cli",
                    )
                ],
                api.T0,
            )
        )
        await daemon.settle()
        agents.emit(
            api.session_snapshot(
                "agents",
                [
                    api.session(
                        "claude",
                        "new-2",
                        pid=501,
                        cwd="/work/alpha",
                        host_pid=500,
                        process_key="501-1790000000",
                        predecessor_key="claude:old-1",
                        entrypoint="cli",
                    )
                ],
                api.T0,
            )
        )
        agents.emit(
            api.activity_snapshot(
                "agents", {"claude:new-2": SessionActivity(state="idle", facts=IDLE_FACTS)}, api.T0
            )
        )
        await daemon.settle()
        return await _now(daemon, api)
    finally:
        await daemon.stop()


async def _degraded_sessions(api: Any) -> dict[str, Any]:
    from fylgja.models import HealthState
    from fylgja.testing.fakes import FakeClock, FakeFS

    clock = FakeClock(api.T0)
    fs = FakeFS()
    fs.add_dir("/work/alpha/.git")
    agents = api.sessions_collector()
    daemon = api.make_daemon(api.windows_collector(), agents, clock=clock, fs=fs)
    await daemon.start()
    try:
        _windows(api, daemon)
        daemon.sink("agents").emit(
            api.session_snapshot(
                "agents",
                [api.session("claude", "c-1", pid=501, cwd="/work/alpha", host_pid=500)],
                api.T0,
            )
        )
        await daemon.settle()
        agents.doctor_state = HealthState.DEGRADED
        await clock.advance(1.0)  # the supervisor's first doctor probe
        await daemon.settle()
        return await _now(daemon, api)
    finally:
        await daemon.stop()


async def _usage_and_builds(api: Any) -> dict[str, Any]:
    """The §4.7 payloads, as the Claude and project collectors emit them (WP5.4)."""
    from datetime import timedelta

    from api_support import Synthetic

    from fylgja.collectors.base import Capability
    from fylgja.collectors.project import builds
    from fylgja.models import (
        BuildEvent,
        ProjectEvent,
        ProjectPayload,
        SessionActivity,
        SnapshotItem,
        TokenCounts,
        UsagePayload,
        UsageSnapshot,
    )
    from fylgja.testing.fakes import FakeFS

    reset_5h = api.T0 + timedelta(hours=4)
    reset_7d = api.T0 + timedelta(days=6)

    def window(
        provider: str, name: str, used: float, source: str, interval: float
    ) -> SnapshotItem[UsagePayload]:
        return SnapshotItem(
            entity_id=f"{provider}:{name}",
            payload=UsagePayload(
                provider=provider,
                window=name,
                used_pct=used,
                resets_at=reset_5h if name == "five_hour" else reset_7d,
                usage_source=source,
                poll_interval_s=interval,
            ),
        )

    def cost(provider: str, usd: float) -> SnapshotItem[UsagePayload]:
        return SnapshotItem(
            entity_id=f"{provider}:cost",
            payload=UsagePayload(
                provider=provider,
                cost_usd=usd,
                cost_period="30d",
                usage_source="codexbar",
                poll_interval_s=900.0,
            ),
        )

    def emit_state(context: Any) -> None:
        path = "/work/alpha"
        fold = builds.fold_runs_body(
            (ROOT / "tests/fixtures/project/gh/actions-fail-multi.json").read_bytes(),
            "example/repo",
        )
        context.sink.emit(
            ProjectEvent(
                source="ps",
                entity_id=path,
                observed_at=api.T0,
                payload=ProjectPayload(
                    path=path,
                    repo_root=path,
                    remote_url="https://github.com/example/repo",
                    repo_name="repo",
                    branch="feature/build-fix",
                    dirty=True,
                    ahead=2,
                    behind=0,
                    last_commit_at=api.T0 - timedelta(hours=2),
                    pr_number=17,
                    pr_state="OPEN",
                    review_decision="REVIEW_REQUIRED",
                ),
            )
        )
        context.sink.emit(
            BuildEvent(
                source="ps",
                entity_id=path,
                observed_at=api.T0,
                payload=fold.to_payload(path, poll_interval_s=300.0),
            )
        )
        context.sink.emit(
            UsageSnapshot(
                source="ps",
                entity_id="codexbar",
                observed_at=api.T0,
                complete=True,
                items=(
                    window("codex", "five_hour", 0, "codexbar", 300.0),
                    window("codex", "seven_day", 3, "codexbar", 300.0),
                    SnapshotItem(
                        entity_id="copilot:usage",
                        payload=UsagePayload(
                            provider="copilot",
                            usage_source="codexbar",
                            error_kind="codexbar_no_strategy",
                            poll_interval_s=300.0,
                        ),
                    ),
                    cost("codex", 24.28),
                    cost("claude", 3039.78),
                ),
            )
        )

    fs = FakeFS()
    fs.add_dir("/work/alpha/.git")
    state = Synthetic("ps", frozenset({Capability.PROJECT_STATE}), emit=emit_state)
    # the Claude collector holds both capabilities: sessions, and the usage rows of the statusline
    claude = Synthetic("agents", frozenset({Capability.SESSIONS, Capability.PROJECT_STATE}))
    daemon = api.make_daemon(api.windows_collector(), claude, state, fs=fs)
    await daemon.start()
    try:
        _windows(api, daemon)
        agents = daemon.sink("agents")
        agents.emit(
            api.session_snapshot(
                "agents",
                [
                    api.session(
                        "claude",
                        "c-1",
                        pid=501,
                        cwd="/work/alpha/src",
                        host_pid=500,
                        process_key="501-1790000000",
                        entrypoint="cli",
                        version="2.1.291",
                        model="claude-opus-5",
                        branch="feature/build-fix",
                    )
                ],
                api.T0,
            )
        )
        agents.emit(
            api.activity_snapshot(
                "agents",
                {
                    "claude:c-1": SessionActivity(
                        state="busy",
                        current_tool="Bash",
                        current_target="pytest",
                        title="Fix the flaky build",
                        facts=FACTS,
                        tokens=TokenCounts(
                            input=1234, output=5678, cache_read=910111, cache_write=21314
                        ),
                        context_pct=42.5,
                        cost_usd=1.2345,
                    )
                },
                api.T0,
            )
        )
        # Claude's rate limits come from the statusline, read by the Claude collector (here the
        # sessions collector), which is not the collector that asks CodexBar
        agents.emit(
            UsageSnapshot(
                source="agents",
                entity_id="claude",
                observed_at=api.T0,
                complete=False,
                items=(
                    window("claude", "five_hour", 23.5, "statusline", 150.0),
                    window("claude", "seven_day", 41.2, "statusline", 150.0),
                ),
            )
        )
        await daemon.settle()
        return await _now(daemon, api)
    finally:
        await daemon.stop()


def _write(name: str, payload: dict[str, Any]) -> None:
    from fylgja.api.now_models import Now

    Now.model_validate(payload)
    OUT.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    (OUT / name).write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT) / name}")


async def record(only: tuple[str, ...] = ()) -> None:
    """Write every fixture, or only the named files (``phase3-agents.json``)."""
    api = _api()
    import phase3_support

    builders: dict[str, Callable[[], Awaitable[dict[str, Any]]]] = {
        "states-and-hosts.json": lambda: _states_and_hosts(api),
        "clear-predecessor.json": lambda: _clear_predecessor(api),
        "degraded-sessions.json": lambda: _degraded_sessions(api),
        "phase5-usage-builds.json": lambda: _usage_and_builds(api),
        "phase3-agents.json": phase3_support.build_now,
    }
    unknown = [name for name in only if name not in builders]
    if unknown:
        raise SystemExit(f"unknown fixture {unknown[0]!r}; known: {', '.join(builders)}")
    for name, build in builders.items():
        if not only or name in only:
            _write(name, await build())


def _isolate(root: Path) -> None:
    """Point the home and the Fylgja directories at ``root``: the daemons built here (and the
    norn inbox) must never touch the real ones."""
    os.environ["HOME"] = str(root / "home")
    os.environ["FYLGJA_CONFIG_DIR"] = str(root / "config")
    os.environ["FYLGJA_DATA_DIR"] = str(root / "data")
    os.environ["FYLGJA_STATE_DIR"] = str(root / "state")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fylgja-record-") as root:
        _isolate(Path(root))
        asyncio.run(record(tuple(sys.argv[1:])))


if __name__ == "__main__":
    main()
