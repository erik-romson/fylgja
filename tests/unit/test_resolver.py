"""The resolver (WP1.8, A1): table-driven joins per host kind, draupnir grouping, project to
workspace, the unresolved path with its reason, and writing the result through the writer."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

import pytest
from resolver_support import (
    CMUX,
    ITERM,
    PYCHARM,
    SAFARI,
    chain,
    session_input,
    window,
)

from fylgja import paths
from fylgja.collectors.claude import parse
from fylgja.core import hosts
from fylgja.core.hosts import (
    CmuxData,
    CmuxIds,
    CmuxRecord,
    CmuxStore,
    HostAdapter,
    HostData,
    JetBrainsProject,
)
from fylgja.core.live_view import LiveView
from fylgja.core.project_roots import ProjectFact, ProjectLocator
from fylgja.core.resolver import (
    Placement,
    Resolution,
    Resolver,
    SessionInput,
    _project_workspaces,
    apply_resolution,
    resolve,
)
from fylgja.core.writer import Writer
from fylgja.models import (
    HookEvent,
    HookEventPayload,
    ProcessRef,
    ProjectEvent,
    ProjectPayload,
    SessionEvent,
    SessionPayload,
    SessionSnapshot,
    SnapshotItem,
    WindowPayload,
    WindowSnapshot,
    WorkspacePayload,
    WorkspaceSnapshot,
)
from fylgja.store import db, queries
from fylgja.testing.fakes import FakeClock, FakeFS

HOME = "/Users/user"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def _adapters(
    *,
    cmux: Sequence[CmuxRecord] = (),
    jetbrains: dict[str, Sequence[JetBrainsProject]] | None = None,
) -> tuple[HostAdapter, ...]:
    data = HostData(
        home=HOME,
        cmux=CmuxData(store=CmuxStore(tuple(cmux))),
        jetbrains={k: tuple(v) for k, v in (jetbrains or {}).items()},
    )
    return hosts.default_adapters(data)


def _resolve(
    sessions: Sequence[SessionInput],
    windows: Sequence[hosts.WindowRow],
    *,
    adapters: Sequence[HostAdapter] | None = None,
    facts: dict[str, ProjectFact] | None = None,
    workspaces: Sequence[str] = (),
) -> Resolution:
    return resolve(sessions, windows, workspaces, adapters=adapters or _adapters(), facts=facts)


# --- session to host to window to workspace, per host kind ----------------------------------------


def test_cmux_session_resolves_to_its_cmux_workspace_and_surface() -> None:
    record = CmuxRecord("s1", "cw-1", "sf-1", pid=100)
    resolution = _resolve(
        [session_input("claude:s1")],
        [window("66", "1", CMUX, 10, app_name="cmux")],
        adapters=_adapters(cmux=[record]),
    )
    placement = resolution.sessions["claude:s1"]
    assert (placement.host_kind, placement.window_id, placement.workspace) == ("cmux", "66", "1")
    assert (placement.via, placement.confidence) == ("cmux-session-store", "high")
    assert (placement.cmux_workspace_id, placement.cmux_surface_id) == ("cw-1", "sf-1")
    assert placement.host_ref == "cw-1/sf-1"
    assert placement.unresolved is None


def test_cmux_session_without_ids_resolves_at_window_level() -> None:
    resolution = _resolve([session_input("claude:s1")], [window("66", "1", CMUX, 10)])
    placement = resolution.sessions["claude:s1"]
    assert (placement.via, placement.confidence) == ("parent-chain", "window-only")
    assert placement.cmux_surface_id is None


def test_hook_time_ids_make_the_cmux_join_exact() -> None:
    session = dataclasses.replace(session_input("claude:s1"), cmux_ids=CmuxIds("hw", "hs"))
    placement = _resolve([session], [window("66", "1", CMUX, 10)]).sessions["claude:s1"]
    assert (placement.via, placement.confidence) == ("cmux-hook-env", "high")
    assert placement.cmux_surface_id == "hs"


def test_jetbrains_session_resolves_to_the_window_of_its_project() -> None:
    projects = {"pycharm": [JetBrainsProject("/work/project")]}
    windows = [
        window("1", "2", PYCHARM, 10, title="project – a.py"),
        window("2", "3", PYCHARM, 10, title="other – b.py"),
    ]
    resolution = _resolve(
        [session_input("claude:s1", cwd="/work/project/src")],
        windows,
        adapters=_adapters(jetbrains=projects),
    )
    placement = resolution.sessions["claude:s1"]
    assert (placement.host_kind, placement.window_id, placement.workspace) == (
        "jetbrains",
        "1",
        "2",
    )
    assert placement.host_ref == "/work/project"
    assert (placement.via, placement.confidence) == ("parent-chain+project-path", "medium")


def test_the_longest_jetbrains_project_path_wins() -> None:
    projects = {"pycharm": [JetBrainsProject("/work"), JetBrainsProject("/work/project")]}
    windows = [
        window("1", "2", PYCHARM, 10, title="work"),
        window("2", "3", PYCHARM, 10, title="project"),
    ]
    placement = _resolve(
        [session_input("claude:s1", cwd="/work/project")],
        windows,
        adapters=_adapters(jetbrains=projects),
    ).sessions["claude:s1"]
    assert placement.window_id == "2"


def test_iterm2_session_is_found_by_title() -> None:
    windows = [window("1", "2", ITERM, 10, title="a"), window("2", "3", ITERM, 10, title="mine")]
    placement = _resolve([session_input("claude:s1", name="mine")], windows).sessions["claude:s1"]
    assert (placement.host_kind, placement.window_id, placement.via) == (
        "iterm2",
        "2",
        "parent-chain+title",
    )


def test_unknown_host_resolves_at_window_level() -> None:
    placement = _resolve([session_input("claude:s1")], [window("9", "4", SAFARI, 10)]).sessions[
        "claude:s1"
    ]
    assert (placement.host_kind, placement.window_id, placement.workspace) == ("other", "9", "4")
    assert placement.confidence == "window-only"


def test_the_first_ancestor_that_owns_a_window_is_the_host() -> None:
    # claude -> zsh (owns a window: the generic host) -> cmux (also owns one, further up)
    ancestry = chain((100, 50, "claude"), (50, 20, "zsh"), (20, 10, "login"), (10, 1, "cmux"))
    windows = [window("5", "9", SAFARI, 20), window("66", "1", CMUX, 10)]
    placement = _resolve([session_input("claude:s1", ancestry=ancestry)], windows).sessions[
        "claude:s1"
    ]
    assert (placement.host_kind, placement.window_id) == ("other", "5")


# --- the unresolved path and its reason -----------------------------------------------------------


@pytest.mark.parametrize(
    ("session", "reason"),
    [
        (session_input("claude:s1", pid=None), "session has no pid"),
        (session_input("claude:s1", ancestry=()), "no process chain recorded for the session"),
        (
            session_input("claude:s1", host_pid=777),
            "no ancestor process owns an AeroSpace window",
        ),
    ],
)
def test_a_session_without_a_host_is_unresolved_with_a_reason(
    session: SessionInput, reason: str
) -> None:
    placement = _resolve([session], [window("66", "1", CMUX, 10)]).sessions["claude:s1"]
    assert placement.unresolved == reason
    assert (placement.via, placement.confidence) == ("none", "none")
    assert (placement.host_kind, placement.window_id, placement.workspace) == (None, None, None)
    assert placement.project_path == "/work/project", "the project join is independent"


def test_a_host_join_that_fails_keeps_the_host_and_says_why() -> None:
    windows = [window("1", "2", CMUX, 10), window("2", "3", CMUX, 10)]
    placement = _resolve([session_input("claude:s1")], windows).sessions["claude:s1"]
    assert placement.host_kind == "cmux"
    assert placement.unresolved is not None
    assert "cmux windows" in placement.unresolved
    assert placement.window_id is None
    assert placement.workspace is None


def test_windows_without_an_app_pid_never_host_a_session() -> None:
    placement = _resolve([session_input("claude:s1")], [window("1", "1", CMUX, None)]).sessions[
        "claude:s1"
    ]
    assert placement.unresolved == "no ancestor process owns an AeroSpace window"


# --- session to project, and draupnir grouping ----------------------------------------------------


@pytest.mark.parametrize(
    ("cwd", "fact", "path", "via", "confidence", "group"),
    [
        ("/w/app/src", ProjectFact(git_root="/w/app"), "/w/app", "git-root", "high", None),
        ("/w/app", None, "/w/app", "cwd", "low", None),
        ("/w/app/", None, "/w/app", "cwd", "low", None),
        ("/w/tmp", ProjectFact(), "/w/tmp", "cwd", "medium", None),
        (
            "/w/ws/feature-x/src",
            ProjectFact(git_root="/w/ws/feature-x", group="ws"),
            "/w/ws/feature-x",
            "git-root",
            "high",
            "ws",
        ),
        ("/w/ws", ProjectFact(group="ws"), "/w/ws", "cwd", "medium", "ws"),
    ],
)
def test_session_to_project(
    cwd: str, fact: ProjectFact | None, path: str, via: str, confidence: str, group: str | None
) -> None:
    facts = None if fact is None else {cwd: fact}
    placement = _resolve(
        [session_input("claude:s1", cwd=cwd)], [window("1", "1", SAFARI, 10)], facts=facts
    ).sessions["claude:s1"]
    assert placement.project_path == path
    assert (placement.project_via, placement.project_confidence) == (via, confidence)
    assert placement.group_name == group


@pytest.mark.parametrize("cwd", [None, "", "/"])
def test_a_session_without_a_cwd_has_no_project(cwd: str | None) -> None:
    placement = _resolve(
        [session_input("claude:s1", cwd=cwd)], [window("1", "1", SAFARI, 10)]
    ).sessions["claude:s1"]
    assert placement.project_path is None
    assert placement.project_confidence == "none"


def test_draupnir_clones_share_a_group_through_the_parents_toml() -> None:
    fs = FakeFS()
    fs.add_file("/p/ws/draupnir.toml", "[workspace]")
    for clone in ("main", "feature-a", "feature-b"):
        fs.add_dir(f"/p/ws/{clone}/.git")
    locator = ProjectLocator(fs)
    cwds = {"a": "/p/ws/main", "b": "/p/ws/feature-a/src", "c": "/p/ws/feature-b"}
    sessions = [
        session_input(f"claude:{k}", cwd=cwd, pid=100 + i)
        for i, (k, cwd) in enumerate(cwds.items())
    ]
    resolution = _resolve(
        sessions,
        [window("1", "1", SAFARI, 10)],
        facts={cwd: locator.locate(cwd) for cwd in cwds.values()},
    )
    assert {p.group_name for p in resolution.sessions.values()} == {"ws"}
    assert sorted(resolution.projects) == ["/p/ws/feature-a", "/p/ws/feature-b", "/p/ws/main"]
    assert {p.name for p in resolution.projects.values()} == {"ws"}
    assert resolution.labels["1"].text == "ws"


# --- project to workspace, by window count --------------------------------------------------------


def _placement(
    key: str,
    cwd: str,
    window_id: str | None,
    workspace: str | None,
) -> Placement:
    return Placement(
        session_key=key,
        project_path=cwd,
        project_via="cwd",
        project_confidence="low",
        group_name=None,
        host_kind="other",
        host_ref=None,
        window_id=window_id,
        workspace=workspace,
        via="parent-chain",
        confidence="window-only",
    )


def test_a_project_goes_to_the_workspace_with_most_of_its_windows() -> None:
    placed = _project_workspaces(
        [
            _placement("a", "/p", "1", "ws1"),
            _placement("b", "/p", "2", "ws2"),
            _placement("c", "/p", "3", "ws2"),
            _placement("d", "/q", "4", "ws1"),
        ]
    )
    assert (placed["/p"].workspace, placed["/p"].via, placed["/p"].confidence) == (
        "ws2",
        "window-count",
        "medium",
    )
    assert (placed["/p"].sessions, placed["/p"].windows) == (3, 3)
    assert (placed["/q"].workspace, placed["/q"].confidence) == ("ws1", "high")


def test_sessions_that_share_a_window_count_it_once() -> None:
    placed = _project_workspaces(
        [
            _placement("a", "/p", "1", "ws1"),
            _placement("b", "/p", "1", "ws1"),
            _placement("c", "/p", "1", "ws1"),
            _placement("d", "/p", "2", "ws2"),
            _placement("e", "/p", "3", "ws2"),
        ]
    )
    assert placed["/p"].workspace == "ws2"
    assert placed["/p"].windows == 3


def test_a_tie_between_workspaces_goes_to_the_first_at_low_confidence() -> None:
    placed = _project_workspaces(
        [_placement("a", "/p", "1", "ws2"), _placement("b", "/p", "2", "ws1")]
    )
    assert (placed["/p"].workspace, placed["/p"].confidence) == ("ws1", "low")


def test_a_workspace_level_session_counts_as_one_window() -> None:
    placed = _project_workspaces(
        [
            _placement("a", "/p", None, "ws1"),
            _placement("b", "/p", None, "ws1"),
            _placement("c", "/p", "9", "ws2"),
        ]
    )
    assert placed["/p"].workspace == "ws1"


def test_a_project_whose_sessions_are_unplaced_says_why() -> None:
    placed = _project_workspaces([_placement("a", "/p", None, None)])
    assert placed["/p"].workspace is None
    assert placed["/p"].unresolved == "no session of the project is placed in a workspace"
    assert (placed["/p"].via, placed["/p"].confidence) == ("none", "none")


def test_project_to_workspace_through_resolve() -> None:
    windows = [
        window("1", "ws1", SAFARI, 10),
        window("2", "ws2", SAFARI, 11),
        window("3", "ws2", SAFARI, 12),
    ]
    sessions = [
        session_input("claude:a", cwd="/p", pid=100, host_pid=10),
        session_input("claude:b", cwd="/p", pid=101, host_pid=11),
        session_input("claude:c", cwd="/p", pid=102, host_pid=12),
    ]
    resolution = _resolve(sessions, windows)
    assert resolution.projects["/p"].workspace == "ws2"


# --- labels through the resolver ------------------------------------------------------------------


def test_labels_cover_every_workspace_with_a_session_a_window_or_a_name() -> None:
    windows = [window("1", "ws1", SAFARI, 10, title="Kubernetes docs", app_name="Safari")]
    resolution = _resolve(
        [session_input("claude:a", cwd="/code/fylgja/main", host_pid=10)],
        windows + [window("2", None, SAFARI, 12)],
        workspaces=["ws1", "empty"],
    )
    assert resolution.labels["ws1"].text == "fylgja"
    assert resolution.labels["ws1"].rule == "project"
    assert resolution.labels["empty"].text == "—"
    assert set(resolution.labels) == {"ws1", "empty"}


def test_a_window_only_workspace_is_labelled_by_app_and_title() -> None:
    windows = [window("1", "ws1", SAFARI, 10, title="Kubernetes docs", app_name="Safari")]
    resolution = _resolve([], windows)
    assert resolution.labels["ws1"].text == "Safari Kubernetes"


# --- determinism ----------------------------------------------------------------------------------


def test_the_resolution_does_not_depend_on_input_order() -> None:
    windows = [window(str(i), f"ws{i % 2}", CMUX, 10) for i in range(4)]
    sessions = [session_input(f"claude:{i}", cwd=f"/p/{i % 2}", pid=100 + i) for i in range(4)]
    forward = _resolve(sessions, windows)
    backward = _resolve(sessions[::-1], windows[::-1])
    assert forward.sessions == backward.sessions
    assert forward.projects == backward.projects
    assert forward.labels == backward.labels


# --- writing the result through the single writer -------------------------------------------------


@pytest.fixture
async def view() -> LiveView:
    return LiveView()


@pytest.fixture
async def writer(view: LiveView) -> AsyncIterator[Writer]:
    instance = Writer(paths.database_file(), clock=FakeClock(NOW), on_commit=view.apply)
    await instance.start()
    try:
        yield instance
    finally:
        await instance.stop()


def _session_snapshot(*keys: tuple[str, str, int]) -> SessionSnapshot:
    return SessionSnapshot(
        source="claude",
        entity_id="all",
        observed_at=NOW,
        complete=True,
        items=tuple(
            SnapshotItem(
                entity_id=f"claude:{sid}",
                payload=SessionPayload(
                    kind="claude",
                    native_id=sid,
                    pid=pid,
                    cwd=cwd,
                    ancestry=(
                        ProcessRef(pid=pid, ppid=50, comm="claude"),
                        ProcessRef(pid=50, ppid=10, comm="zsh"),
                        ProcessRef(pid=10, ppid=1, comm="cmux"),
                    ),
                ),
            )
            for sid, cwd, pid in keys
        ),
    )


async def _seed(writer: Writer, *sessions: tuple[str, str, int]) -> SessionSnapshot:
    snapshot = _session_snapshot(*sessions)
    writer.submit(snapshot)
    writer.submit(
        WindowSnapshot(
            source="aerospace",
            entity_id="all",
            observed_at=NOW,
            complete=True,
            items=(
                SnapshotItem(
                    entity_id="66",
                    payload=WindowPayload(
                        workspace="1",
                        app_bundle_id=CMUX,
                        app_name="cmux",
                        app_pid=10,
                        title="term",
                    ),
                ),
            ),
        )
    )
    writer.submit(
        WorkspaceSnapshot(
            source="aerospace",
            entity_id="all",
            observed_at=NOW,
            complete=True,
            items=(
                SnapshotItem(entity_id="1", payload=WorkspacePayload(name="1", focused=True)),
                SnapshotItem(entity_id="2", payload=WorkspacePayload(name="2")),
            ),
        )
    )
    await writer.flush()
    return snapshot


def _resolver(writer: Writer, view: LiveView, fs: FakeFS, clock: FakeClock) -> Resolver:
    async def load() -> HostData:
        return HostData(home=HOME)

    return Resolver(writer, view, hosts=load, locator=ProjectLocator(fs), clock=clock)


async def test_refresh_writes_the_columns_and_updates_the_live_view(
    writer: Writer, view: LiveView
) -> None:
    fs = FakeFS()
    fs.add_dir("/code/fylgja/main/.git")
    resolver = _resolver(writer, view, fs, FakeClock(NOW))
    snapshot = await _seed(writer, ("s1", "/code/fylgja/main/src", 100))
    resolver.observe(snapshot)

    resolution = await resolver.refresh()

    row = view.sessions["claude:s1"]
    assert (row.project_path, row.host_kind, row.window_id, row.workspace) == (
        "/code/fylgja/main",
        "cmux",
        "66",
        "1",
    )
    assert resolution.sessions["claude:s1"].window_id == "66"
    stored = db.connect_reader(paths.database_file())
    try:
        columns = stored.execute(
            "SELECT project_path, host_kind, window_id, workspace FROM sessions"
        ).fetchone()
        labels = dict(stored.execute("SELECT name, inferred_label FROM workspaces").fetchall())
        project = stored.execute("SELECT repo_root FROM projects").fetchone()
    finally:
        stored.close()
    assert tuple(columns) == ("/code/fylgja/main", "cmux", "66", "1")
    assert labels == {"1": "fylgja", "2": "—"}
    assert project["repo_root"] == "/code/fylgja/main"
    assert view.workspaces["1"].inferred_label == "fylgja"


async def test_a_second_refresh_changes_nothing(writer: Writer, view: LiveView) -> None:
    resolver = _resolver(writer, view, FakeFS(), FakeClock(NOW))
    resolver.observe(await _seed(writer, ("s1", "/code/app", 100)))
    await resolver.refresh()
    version = view.version
    await resolver.refresh()
    assert view.version == version


async def test_the_resolver_only_clears_what_it_wrote_when_a_join_stops_holding(
    writer: Writer, view: LiveView
) -> None:
    resolver = _resolver(writer, view, FakeFS(), FakeClock(NOW))
    snapshot = await _seed(writer, ("s1", "/code/app", 100))
    resolver.observe(snapshot)
    await resolver.refresh()
    assert view.sessions["claude:s1"].window_id == "66"
    resolver.forget("claude:s1")  # the chain is gone: the join no longer holds
    await resolver.refresh()
    row = view.sessions["claude:s1"]
    assert (row.window_id, row.workspace, row.host_kind) == (None, None, None)
    assert row.project_path == "/code/app"


async def test_hook_time_cmux_ids_reach_the_resolution(writer: Writer, view: LiveView) -> None:
    resolver = _resolver(writer, view, FakeFS(), FakeClock(NOW))
    resolver.observe(await _seed(writer, ("s1", "/code/app", 100)))
    resolver.observe_cmux_ids("claude:s1", CmuxIds("hw", "hs"))
    resolution = await resolver.refresh()
    assert resolution.sessions["claude:s1"].via == "cmux-hook-env"
    assert view.sessions["claude:s1"].host_ref == "hw/hs"


async def test_a_hook_event_feeds_its_cmux_ids_to_the_resolver(
    writer: Writer, view: LiveView
) -> None:
    resolver = _resolver(writer, view, FakeFS(), FakeClock(NOW))
    resolver.observe(await _seed(writer, ("s1", "/code/app", 100)))
    changed: list[int] = []
    resolver.on_ids_changed = lambda: changed.append(1)

    def hook_event(session: str, **ids: str) -> HookEvent:
        return HookEvent(
            source="claude",
            entity_id=f"claude:{session}",
            observed_at=NOW,
            payload=HookEventPayload(session_id=session, event="Stop", ids=ids or None),
        )

    resolver.observe(
        hook_event("s1", CMUX_WORKSPACE_ID="hw", CMUX_SURFACE_ID="hs", TERM_PROGRAM="x")
    )
    resolver.observe(hook_event("s1", CMUX_WORKSPACE_ID="hw", CMUX_SURFACE_ID="hs"))  # nothing new
    resolver.observe(hook_event("s2", TERM_PROGRAM="ghostty"))  # no cmux ids
    resolver.observe(hook_event("s3"))
    assert changed == [1]
    resolution = await resolver.refresh()
    assert resolution.sessions["claude:s1"].via == "cmux-hook-env"
    assert view.sessions["claude:s1"].host_ref == "hw/hs"
    resolver.forget("claude:s1")
    assert "claude:s1" not in resolver._cmux_ids


async def test_observe_keeps_chains_of_live_sessions_only(writer: Writer, view: LiveView) -> None:
    resolver = _resolver(writer, view, FakeFS(), FakeClock(NOW))
    resolver.observe(_session_snapshot(("a", "/x", 100), ("b", "/y", 101)))
    resolver.observe(_session_snapshot(("a", "/x", 100)))
    assert set(resolver._ancestry) == {"claude:a"}
    resolver.observe(
        SessionEvent(
            source="claude",
            entity_id="claude:c",
            observed_at=NOW,
            payload=SessionPayload(
                kind="claude", native_id="c", ancestry=(ProcessRef(pid=5, ppid=1, comm="x"),)
            ),
        )
    )
    assert set(resolver._ancestry) == {"claude:a", "claude:c"}
    resolver.observe(
        ProjectEvent(
            source="git", entity_id="/x", observed_at=NOW, payload=ProjectPayload(path="/x")
        )
    )
    assert set(resolver._ancestry) == {"claude:a", "claude:c"}


async def test_apply_resolution_keeps_a_stored_project_it_has_nothing_new_for(
    writer: Writer, view: LiveView
) -> None:
    writer.submit(
        ProjectEvent(
            source="git",
            entity_id="/code/app",
            observed_at=NOW,
            payload=ProjectPayload(path="/code/app", repo_root="/code/app", repo_name="app"),
        )
    )
    await writer.flush()
    resolver = _resolver(writer, view, FakeFS(), FakeClock(NOW))
    resolver.observe(await _seed(writer, ("s1", "/code/app", 100)))
    await resolver.refresh()
    version = view.version
    project = view.projects["/code/app"]
    assert (project.repo_root, project.repo_name) == ("/code/app", "app")
    await resolver.refresh()
    assert view.version == version


async def test_apply_resolution_is_one_writer_transaction(writer: Writer, view: LiveView) -> None:
    await _seed(writer, ("s1", "/code/app", 100))
    resolution = _resolve(
        [session_input("claude:s1", cwd="/code/app", host_pid=10)],
        [window("66", "1", CMUX, 10)],
        workspaces=["1"],
    )
    first = await writer.execute(
        lambda tx: apply_resolution(tx, resolution, at="2026-10-05T12:00:00.000000Z")
    )
    again = await writer.execute(
        lambda tx: apply_resolution(tx, resolution, at="2026-10-05T12:00:01.000000Z")
    )
    assert first >= 2
    assert again == 0


def test_the_claude_chain_parser_feeds_the_resolver() -> None:
    table = parse.parse_ps_table(
        "  100    50 claude\n   50    10 -zsh\n   10     1 /Apps/cmux.app/MacOS/cmux\n"
    )
    ancestry = parse.ancestor_chain(table, 100)
    placement = _resolve(
        [session_input("claude:s1", ancestry=ancestry)], [window("66", "1", CMUX, 10)]
    ).sessions["claude:s1"]
    assert placement.window_id == "66"


def test_queries_report_unchanged_and_missing_rows() -> None:
    connection = db.open_store(paths.database_file())
    try:
        assert (
            queries.set_session_placement(
                connection,
                session_key="claude:none",
                project_path=None,
                host_kind=None,
                host_ref=None,
                window_id=None,
                workspace=None,
            )
            is None
        )
        assert queries.set_inferred_label(connection, name="none", label="x", at="t") is None
        assert queries.get_project(connection, "/none") is None
    finally:
        connection.close()
