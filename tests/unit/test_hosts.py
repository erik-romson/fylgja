"""Host adapters (WP1.8, decision 0025): cmux, JetBrains, iTerm2 and the generic window level.

The cmux source and the JetBrains loader run against :class:`FakeProcessRunner` and :class:`FakeFS`;
the joins are pure functions over window rows.
"""

from __future__ import annotations

import json
import logging
import plistlib
from pathlib import Path
from typing import Any

import pytest
from resolver_support import CMUX, INTELLIJ, ITERM, PYCHARM, SAFARI, window

from fylgja.core import hosts
from fylgja.core.hosts import (
    CmuxData,
    CmuxHost,
    CmuxIds,
    CmuxSource,
    GenericHost,
    HostSession,
    Iterm2Host,
    JetBrainsHost,
    JetBrainsProject,
    cmux_ids_from_env,
)
from fylgja.testing.fakes import FakeFS, FakeProcessRunner

HOME = "/Users/user"
CLI = hosts.CMUX_CLI
STORE = f"{HOME}/.cmuxterm/claude-hook-sessions.json"
CMUX_VERSION = "cmux 0.64.25 (106) [b685a275c]\n"


def _session(
    session_id: str = "s1",
    *,
    pid: int | None = 100,
    cwd: str = "/work/project",
    name: str | None = None,
    ids: CmuxIds | None = None,
) -> HostSession:
    return HostSession(session_id=session_id, pid=pid, cwd=cwd, name=name, cmux_ids=ids)


# --- host_kind and the adapter choice ----------------------------------------------------------


@pytest.mark.parametrize(
    ("bundle", "kind"),
    [
        (CMUX, "cmux"),
        (PYCHARM, "jetbrains"),
        (INTELLIJ, "jetbrains"),
        (ITERM, "iterm2"),
        (SAFARI, "other"),
        ("", "other"),
    ],
)
def test_host_kind(bundle: str, kind: str) -> None:
    assert hosts.host_kind(bundle) == kind


def test_adapter_for_picks_by_bundle_id_and_falls_back_to_generic() -> None:
    adapters = hosts.default_adapters(hosts.HostData(home=HOME))
    assert [hosts.adapter_for(b, adapters).kind for b in (CMUX, PYCHARM, ITERM, SAFARI)] == [
        "cmux",
        "jetbrains",
        "iterm2",
        "other",
    ]


# --- the generic host -------------------------------------------------------------------------


def test_an_unknown_host_with_one_window_is_window_level() -> None:
    join = GenericHost().join(_session(), [window("7", "3", SAFARI, 10)])
    assert (join.host, join.via, join.confidence) == ("other", "parent-chain", "window-only")
    assert (join.window_id, join.workspace, join.unresolved) == ("7", "3", None)


def test_an_unknown_host_with_windows_on_one_workspace_resolves_the_workspace() -> None:
    windows = [window("7", "3", SAFARI, 10), window("8", "3", SAFARI, 10)]
    join = GenericHost().join(_session(), windows)
    assert (join.window_id, join.workspace, join.confidence) == (None, "3", "low")
    assert "2 windows of the host app" in join.note


def test_an_unknown_host_with_windows_on_two_workspaces_is_unresolved_with_a_reason() -> None:
    windows = [window("7", "3", SAFARI, 10), window("8", "4", SAFARI, 10)]
    join = GenericHost().join(_session(), windows)
    assert join.unresolved is not None
    assert "different AeroSpace workspaces" in join.unresolved
    assert (join.via, join.confidence, join.workspace) == ("none", "none", None)


# --- cmux: window level, store and hook ids ----------------------------------------------------


def _store_text(*records: dict[str, Any], version: int = 1) -> str:
    return json.dumps({"version": version, "sessions": {r["sessionId"]: r for r in records}})


def _record(
    session_id: str = "s1",
    pid: int = 100,
    workspace: str = "cw1",
    surface: str = "sf1",
    **more: Any,
) -> dict[str, Any]:
    return {
        "sessionId": session_id,
        "pid": pid,
        "workspaceId": workspace,
        "surfaceId": surface,
        "cwd": "/work/project",
        "agentLifecycle": "idle",
        **more,
    }


def _cmux_host(*records: dict[str, Any]) -> CmuxHost:
    store = hosts.parse_cmux_store(_store_text(*records))
    return CmuxHost(CmuxData(store=store))


def test_cmux_ids_come_from_the_hook_environment_ids() -> None:
    assert cmux_ids_from_env({"CMUX_WORKSPACE_ID": "w", "CMUX_SURFACE_ID": "s"}) == CmuxIds(
        "w", "s"
    )
    assert cmux_ids_from_env({"CMUX_SURFACE_ID": "s", "TERM_PROGRAM": "x"}) == CmuxIds(None, "s")
    assert cmux_ids_from_env({"CMUX_WORKSPACE_ID": "w", "CMUX_SURFACE_ID": ""}) == CmuxIds(
        "w", None
    )
    assert cmux_ids_from_env({"CMUX_TAB_ID": "t", "TERM_PROGRAM": "ghostty"}) is None
    assert cmux_ids_from_env({}) is None
    assert cmux_ids_from_env(None) is None


def test_cmux_without_any_id_falls_back_to_window_level() -> None:
    join = CmuxHost().join(_session(), [window("66", "1", CMUX, 10)])
    assert (join.host, join.via, join.confidence) == ("cmux", "parent-chain", "window-only")
    assert (join.window_id, join.workspace) == ("66", "1")
    assert join.cmux_workspace_id is None
    assert join.cmux_surface_id is None
    assert join.note == "cmux workspace and surface not resolved"


def test_cmux_store_match_on_session_id_and_pid_is_high() -> None:
    join = _cmux_host(_record()).join(_session(), [window("66", "1", CMUX, 10)])
    assert (join.via, join.confidence) == ("cmux-session-store", "high")
    assert (join.cmux_workspace_id, join.cmux_surface_id, join.host_ref) == (
        "cw1",
        "sf1",
        "cw1/sf1",
    )
    assert (join.window_id, join.workspace) == ("66", "1")


def test_cmux_store_match_on_session_id_alone_is_medium() -> None:
    host = _cmux_host(_record(pid=999))
    join = host.join(_session(), [window("66", "1", CMUX, 10)])
    assert (join.via, join.confidence, join.cmux_surface_id) == (
        "cmux-session-store",
        "medium",
        "sf1",
    )


def test_cmux_store_prefers_the_record_with_the_same_pid() -> None:
    store = hosts.CmuxStore(
        (
            hosts.CmuxRecord("s1", "cw", "sf-other", pid=999),
            hosts.CmuxRecord("s1", "cw", "sf-mine", pid=100),
        )
    )
    join = CmuxHost(CmuxData(store=store)).join(_session(), [window("66", "1", CMUX, 10)])
    assert (join.cmux_surface_id, join.confidence) == ("sf-mine", "high")


def test_cmux_store_ignores_another_session_and_a_dead_pid() -> None:
    other = _cmux_host(_record(session_id="other"))
    assert other.join(_session(), [window("66", "1", CMUX, 10)]).cmux_surface_id is None
    dead = CmuxHost(
        CmuxData(
            store=hosts.CmuxStore((hosts.CmuxRecord("s1", "cw", "sf", 100, pid_exists=False),))
        )
    )
    assert dead.join(_session(), [window("66", "1", CMUX, 10)]).via == "parent-chain"


def test_cmux_hook_ids_win_over_the_store() -> None:
    ids = CmuxIds(workspace_id="hw", surface_id="hs")
    join = _cmux_host(_record()).join(_session(ids=ids), [window("66", "1", CMUX, 10)])
    assert (join.via, join.confidence) == ("cmux-hook-env", "high")
    assert (join.cmux_workspace_id, join.cmux_surface_id, join.host_ref) == ("hw", "hs", "hw/hs")


def test_cmux_hook_surface_without_a_workspace_id() -> None:
    join = CmuxHost().join(_session(ids=CmuxIds(surface_id="hs")), [window("66", "1", CMUX, 10)])
    assert (join.host_ref, join.cmux_workspace_id) == ("hs", None)


def test_cmux_hook_ids_without_a_surface_are_not_exact() -> None:
    join = CmuxHost().join(_session(ids=CmuxIds(workspace_id="hw")), [window("66", "1", CMUX, 10)])
    assert join.via == "parent-chain"


def test_cmux_with_several_windows_on_one_workspace_keeps_the_exact_ids() -> None:
    windows = [window("66", "1", CMUX, 10), window("67", "1", CMUX, 10)]
    join = _cmux_host(_record()).join(_session(), windows)
    assert (join.window_id, join.workspace, join.confidence) == (None, "1", "low")
    assert (join.cmux_surface_id, join.via) == ("sf1", "cmux-session-store")


def test_cmux_with_windows_on_different_workspaces_is_unresolved_but_keeps_the_ids() -> None:
    windows = [window("66", "1", CMUX, 10), window("67", "2", CMUX, 10)]
    join = _cmux_host(_record()).join(_session(), windows)
    assert join.unresolved is not None
    assert "cmux windows" in join.unresolved
    assert join.cmux_surface_id == "sf1"


def test_cmux_window_titles_are_never_a_join_key() -> None:
    windows = [window("66", "1", CMUX, 10, title="s1"), window("67", "2", CMUX, 10, title="x")]
    join = CmuxHost().join(_session(session_id="s1", name="s1"), windows)
    assert join.window_id is None
    assert join.unresolved is not None


# --- the cmux store parser ---------------------------------------------------------------------


def test_parse_cmux_store_keeps_only_the_allowlisted_fields() -> None:
    record = _record(
        launch_arguments=["--token", "secret"],
        stored_pid_arguments=["x"],
        transcriptPath="/t.jsonl",
        launchCommand="secret",
    )
    store = hosts.parse_cmux_store(_store_text(record))
    assert store is not None
    assert store.records == (
        hosts.CmuxRecord("s1", "cw1", "sf1", pid=100, cwd="/work/project", lifecycle="idle"),
    )
    assert "secret" not in repr(store)


def test_parse_cmux_store_accepts_snake_case_and_a_list() -> None:
    text = json.dumps(
        {
            "version": 1,
            "sessions": [
                {
                    "session_id": "a",
                    "workspace_id": "w",
                    "surface_id": "s",
                    "pid": 5,
                    "agent_lifecycle": "running",
                    "stored_pid_exists": True,
                }
            ],
        }
    )
    store = hosts.parse_cmux_store(text)
    assert store is not None
    assert store.records == (
        hosts.CmuxRecord("a", "w", "s", pid=5, lifecycle="running", pid_exists=True),
    )


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        json.dumps({"sessions": {}}),
        json.dumps({"version": 2, "sessions": {}}),
        json.dumps({"version": 1}),
        json.dumps({"version": 1, "sessions": "x"}),
    ],
)
def test_parse_cmux_store_rejects_an_unknown_shape(text: str) -> None:
    assert hosts.parse_cmux_store(text) is None


def test_parse_cmux_store_skips_incomplete_records() -> None:
    text = json.dumps(
        {
            "version": 1,
            "sessions": {
                "a": {"sessionId": "a", "workspaceId": "w"},
                "b": "text",
                "c": {"sessionId": "c", "workspaceId": "w", "surfaceId": "s", "pid": True},
            },
        }
    )
    store = hosts.parse_cmux_store(text)
    assert store is not None
    assert [r.session_id for r in store.records] == ["c"]
    assert store.records[0].pid is None


# --- version detection and the source ---------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("cmux 0.64.25 (106) [b685a275c]\n", ("0.64.25", "106")),
        ("cmux 1.0", ("1.0", None)),
        ("something else", None),
        ("", None),
    ],
)
def test_parse_cmux_version(text: str, parsed: tuple[str, str | None] | None) -> None:
    assert hosts.parse_cmux_version(text) == parsed


@pytest.mark.parametrize(
    ("version", "expected"),
    [("0.64.25", True), ("0.64", True), ("0.640", False), ("0.65.0", False), (None, False)],
)
def test_version_is_tested(version: str | None, expected: bool) -> None:
    assert hosts.version_is_tested(version, ("0.64",)) is expected


def _source(runner: FakeProcessRunner, fs: FakeFS, **kwargs: Any) -> CmuxSource:
    return CmuxSource(runner, fs, home=HOME, **kwargs)


async def test_the_source_reads_the_version_and_the_store() -> None:
    runner = FakeProcessRunner()
    runner.script(CLI, "--version", stdout=CMUX_VERSION)
    fs = FakeFS({STORE: _store_text(_record())})
    data = await _source(runner, fs).load()
    assert data.status == hosts.CmuxStatus(
        version="0.64.25", build="106", tested=True, degraded=None, store_state="ok"
    )
    assert data.store is not None
    assert len(data.store.records) == 1
    assert runner.calls == [(CLI, "--version")], "only the listing command runs"


async def test_the_version_is_detected_once() -> None:
    runner = FakeProcessRunner()
    runner.script(CLI, "--version", stdout=CMUX_VERSION)
    source = _source(runner, FakeFS({STORE: _store_text(_record())}))
    await source.load()
    await source.load()
    assert len(runner.calls) == 1


async def test_an_unknown_version_degrades_with_one_warning_and_keeps_the_store(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = FakeProcessRunner()
    runner.script(CLI, "--version", stdout="cmux 0.99.1 (7) [abc]\n")
    source = _source(runner, FakeFS({STORE: _store_text(_record())}))
    with caplog.at_level(logging.WARNING, logger="fylgja.hosts"):
        first = await source.load()
        await source.load()
    assert first.status.degraded == "unknown cmux version"
    assert first.status.tested is False
    assert first.store is not None, "a store that passes the shape check is still used"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "0.99.1" in warnings[0].getMessage()


async def test_an_unparseable_version_is_unknown() -> None:
    runner = FakeProcessRunner()
    runner.script(CLI, "--version", stdout="garbage\n")
    data = await _source(runner, FakeFS()).load()
    assert (data.status.version, data.status.degraded) == (None, "unknown cmux version")
    assert data.status.store_state == "absent"
    assert data.store is None


async def test_a_store_of_an_unknown_shape_gives_window_level() -> None:
    runner = FakeProcessRunner()
    runner.script(CLI, "--version", stdout=CMUX_VERSION)
    fs = FakeFS({STORE: _store_text(_record(), version=2)})
    data = await _source(runner, fs).load()
    assert (data.store, data.status.store_state) == (None, "unrecognized")
    join = CmuxHost(data).join(_session(), [window("66", "1", CMUX, 10)])
    assert (join.via, join.confidence) == ("parent-chain", "window-only")


async def test_a_binary_or_huge_store_is_unrecognized() -> None:
    runner = FakeProcessRunner()
    runner.script(CLI, "--version", stdout=CMUX_VERSION)
    binary = _source(runner, FakeFS({STORE: b"\xff\xfe\x00"}))
    assert (await binary.load()).status.store_state == "unrecognized"
    huge = _source(runner, FakeFS({STORE: b"x" * (hosts.MAX_STORE_BYTES + 1)}))
    assert (await huge.load()).status.store_state == "unrecognized"


@pytest.mark.parametrize(
    "failure",
    [
        {"error": FileNotFoundError("no cmux")},
        {"timeout": True},
        {"returncode": 1},
    ],
)
async def test_the_version_falls_back_to_the_app_info_plist(failure: dict[str, Any]) -> None:
    runner = FakeProcessRunner()
    runner.script(CLI, "--version", **failure)
    plist = plistlib.dumps({"CFBundleShortVersionString": "0.64.30", "CFBundleVersion": "110"})
    fs = FakeFS({hosts.CMUX_INFO_PLIST: plist})
    data = await _source(runner, fs).load()
    assert (data.status.version, data.status.build, data.status.tested) == ("0.64.30", "110", True)


@pytest.mark.parametrize(
    "plist",
    [b"not a plist", plistlib.dumps({"other": 1}), plistlib.dumps(["x"]), None],
)
async def test_an_unusable_info_plist_leaves_the_version_unknown(plist: bytes | None) -> None:
    runner = FakeProcessRunner()  # no script: the command is missing
    fs = FakeFS({hosts.CMUX_INFO_PLIST: plist} if plist is not None else None)
    data = await _source(runner, fs).load()
    assert data.status.version is None
    assert data.status.degraded == "unknown cmux version"


async def test_a_build_that_is_not_text_is_dropped() -> None:
    runner = FakeProcessRunner()
    plist = plistlib.dumps({"CFBundleShortVersionString": "0.64.1", "CFBundleVersion": 3})
    data = await _source(runner, FakeFS({hosts.CMUX_INFO_PLIST: plist})).load()
    assert (data.status.version, data.status.build) == ("0.64.1", None)


# --- JetBrains --------------------------------------------------------------------------------


def _recent(*entries: tuple[str, bool, str]) -> str:
    items = "".join(
        f'<entry key="{path}"><value><RecentProjectMetaInfo opened="{str(opened).lower()}"'
        f' frameTitle="{title}"/></value></entry>'
        for path, opened, title in entries
    )
    head = '<application><component name="RecentProjectsManager"><map>'
    return f"{head}{items}</map></component></application>"


def _jb(*projects: JetBrainsProject) -> JetBrainsHost:
    return JetBrainsHost({"pycharm": projects}, home=HOME)


def test_parse_recent_projects_keeps_only_opened_projects_and_expands_the_home() -> None:
    xml = _recent(
        ("$USER_HOME$/a/", True, "A frame"),
        ("$USER_HOME$/b", False, ""),
        ("/abs/c", True, ""),
    )
    assert hosts.parse_recent_projects(xml, HOME) == [
        JetBrainsProject(f"{HOME}/a", "A frame"),
        JetBrainsProject("/abs/c", ""),
    ]


def test_parse_recent_projects_raises_on_bad_xml() -> None:
    with pytest.raises(hosts.ET.ParseError):
        hosts.parse_recent_projects("<application", HOME)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("proj – Main.py", "proj"),
        ("proj — Main.py", "proj"),
        ("proj - Main.py", "proj"),
        ("proj", "proj"),
        ("my-proj [~/x]", "my-proj [~/x]"),
    ],
)
def test_title_project(title: str, expected: str) -> None:
    assert hosts.title_project(title) == expected


@pytest.mark.parametrize(
    ("project", "cwd", "expected"),
    [
        ("/a/b", "/a/b", True),
        ("/a/b", "/a/b/c", True),
        ("/a/b", "/a/bc", False),
        ("/a/b", "/a", False),
    ],
)
def test_path_contains(project: str, cwd: str, expected: bool) -> None:
    assert hosts.path_contains(project, cwd) is expected


def test_window_match_by_path_name_frame_title_and_ide_name() -> None:
    project = JetBrainsProject(f"{HOME}/code/app", frame_title="Framed – x", ide_name="IdeName")
    assert hosts.window_match("app [~/code/app] – f.py", project, HOME) == "path"
    assert hosts.window_match("app [~/code/other] – f.py", project, HOME) is None
    assert hosts.window_match(f"app [{HOME}/code/app]", project, HOME) == "path"
    assert hosts.window_match("app – f.py", project, HOME) == "name"
    assert hosts.window_match("Framed – f.py", project, HOME) == "name"
    assert hosts.window_match("IdeName", project, HOME) == "name"
    assert hosts.window_match("unrelated", project, HOME) is None


def test_jetbrains_longest_project_path_wins_among_ambiguous_projects() -> None:
    outer = JetBrainsProject(f"{HOME}/code")
    inner = JetBrainsProject(f"{HOME}/code/app")
    windows = [
        window("1", "2", PYCHARM, 10, title="code – a.py"),
        window("2", "3", PYCHARM, 10, title="app – b.py"),
    ]
    join = _jb(outer, inner).join(_session(cwd=f"{HOME}/code/app/src"), windows)
    assert (join.window_id, join.workspace, join.confidence) == ("2", "3", "medium")
    assert join.host_ref == f"{HOME}/code/app" == join.host_project
    assert join.via == "parent-chain+project-path"
    # Without the inner project the outer one is the only candidate.
    join = _jb(outer).join(_session(cwd=f"{HOME}/code/app/src"), windows)
    assert join.window_id == "1"


def test_jetbrains_path_title_beats_the_name_and_is_high() -> None:
    project = JetBrainsProject(f"{HOME}/a/main")
    windows = [
        window("1", "2", PYCHARM, 10, title="main [~/b/main] – x"),
        window("2", "3", PYCHARM, 10, title="main [~/a/main] – y"),
    ]
    join = _jb(project).join(_session(cwd=f"{HOME}/a/main"), windows)
    assert (join.window_id, join.confidence) == ("2", "high")


def test_jetbrains_two_windows_with_the_same_name_on_one_workspace_fix_the_workspace() -> None:
    windows = [
        window("1", "2", PYCHARM, 10, title="app – a.py"),
        window("2", "2", PYCHARM, 10, title="app – b.py"),
    ]
    join = _jb(JetBrainsProject(f"{HOME}/app")).join(_session(cwd=f"{HOME}/app"), windows)
    assert (join.window_id, join.workspace, join.confidence) == (None, "2", "low")
    assert "2 windows share the project name" in join.note


def test_jetbrains_two_windows_with_the_same_name_on_two_workspaces_are_unresolved() -> None:
    windows = [
        window("1", "2", PYCHARM, 10, title="app – a.py"),
        window("2", "3", PYCHARM, 10, title="app – b.py"),
    ]
    join = _jb(JetBrainsProject(f"{HOME}/app")).join(_session(cwd=f"{HOME}/app"), windows)
    assert join.unresolved is not None
    assert "share the project name" in join.unresolved
    assert join.host_ref == f"{HOME}/app", "the project is known even when the window is not"


def test_jetbrains_project_without_a_matching_title_uses_the_shared_workspace() -> None:
    windows = [window("1", "2", PYCHARM, 10, title="x"), window("2", "2", PYCHARM, 10, title="y")]
    join = _jb(JetBrainsProject(f"{HOME}/app")).join(_session(cwd=f"{HOME}/app"), windows)
    assert (join.workspace, join.window_id, join.confidence) == ("2", None, "low")


def test_jetbrains_project_without_a_matching_title_on_several_workspaces_is_unresolved() -> None:
    windows = [window("1", "2", PYCHARM, 10, title="x"), window("2", "3", PYCHARM, 10, title="y")]
    join = _jb(JetBrainsProject(f"{HOME}/app")).join(_session(cwd=f"{HOME}/app"), windows)
    assert join.unresolved is not None
    assert "no window title matches" in join.unresolved


def test_jetbrains_reasons_for_no_project() -> None:
    windows = [window("1", "2", PYCHARM, 10, title="x")]
    assert _jb().join(_session(cwd=""), windows).unresolved == "session has no cwd"
    none = _jb(JetBrainsProject("/other")).join(_session(cwd=f"{HOME}/app"), windows)
    assert none.unresolved == "no opened JetBrains project contains the cwd"


def test_jetbrains_uses_the_product_of_the_window() -> None:
    host = JetBrainsHost(
        {"pycharm": (JetBrainsProject("/py/app"),), "intellijidea": (JetBrainsProject("/ij/app"),)},
        home=HOME,
    )
    windows = [window("1", "2", INTELLIJ, 10, title="app")]
    assert host.join(_session(cwd="/ij/app"), windows).window_id == "1"
    assert host.join(_session(cwd="/py/app"), windows).unresolved is not None


def test_jetbrains_ide_name_names_the_window_when_the_directory_does_not() -> None:
    project = JetBrainsProject(f"{HOME}/a/main", ide_name="real-name")
    windows = [window("1", "2", PYCHARM, 10, title="real-name – x.py")]
    join = _jb(project).join(_session(cwd=f"{HOME}/a/main"), windows)
    assert (join.window_id, join.confidence) == ("1", "medium")


def test_newest_product_dirs_and_stems() -> None:
    dirs = ["PyCharm2025.3", "PyCharm2026.1", "PyCharm2026.10", "IntelliJIdea2026.2", "CLion"]
    assert hosts.newest_product_dirs(dirs) == {
        "pycharm": "PyCharm2026.10",
        "intellijidea": "IntelliJIdea2026.2",
        "clion": "CLion",
    }
    assert hosts.jetbrains_stem_for("com.jetbrains.intellij") == "intellijidea"
    assert hosts.jetbrains_stem_for("com.jetbrains.pycharm") == "pycharm"


def test_load_jetbrains_reads_the_newest_directory_and_each_project_name() -> None:
    base = f"{HOME}/Library/Application Support/JetBrains"
    xml = _recent(("$USER_HOME$/a/main", True, ""), ("$USER_HOME$/b", True, ""))
    fs = FakeFS(
        {
            f"{base}/PyCharm2025.3/options/recentProjects.xml": _recent(("/old", True, "")),
            f"{base}/PyCharm2026.1/options/recentProjects.xml": xml,
            f"{base}/DataGrip2024.3/options/recentProjects.xml": "<broken",
            f"{HOME}/a/main/.idea/.name": "  ide-name \nsecond line\n",
            f"{HOME}/b/.idea/.name": "  \n",
        }
    )
    fs.add_dir(f"{base}/Toolbox")
    loaded = hosts.load_jetbrains(fs, HOME)
    assert set(loaded) == {"pycharm"}
    assert loaded["pycharm"] == (
        JetBrainsProject(f"{HOME}/a/main", "", "ide-name"),
        JetBrainsProject(f"{HOME}/b", "", ""),
    )


def test_load_jetbrains_without_the_product_directory_is_empty() -> None:
    assert hosts.load_jetbrains(FakeFS(), HOME) == {}


# --- iTerm2 -----------------------------------------------------------------------------------


def test_iterm2_title_equal_to_the_session_title_is_medium() -> None:
    windows = [window("1", "2", ITERM, 10, title="one"), window("2", "3", ITERM, 10, title="two")]
    join = Iterm2Host().join(_session(name="two"), windows)
    assert (join.window_id, join.via, join.confidence) == ("2", "parent-chain+title", "medium")


def test_iterm2_single_window_is_low() -> None:
    join = Iterm2Host().join(_session(name="x"), [window("1", "2", ITERM, 10, title="other")])
    assert (join.window_id, join.via, join.confidence) == ("1", "parent-chain", "low")


def test_iterm2_ambiguous_windows_share_a_workspace_or_stay_unresolved() -> None:
    same = [window("1", "2", ITERM, 10), window("2", "2", ITERM, 10)]
    assert Iterm2Host().join(_session(), same).workspace == "2"
    apart = [window("1", "2", ITERM, 10), window("2", "3", ITERM, 10)]
    assert Iterm2Host().join(_session(), apart).unresolved is not None


# --- load_host_data ----------------------------------------------------------------------------


async def test_load_host_data_reads_cmux_and_jetbrains(tmp_path: Path) -> None:
    runner = FakeProcessRunner()
    runner.script(CLI, "--version", stdout=CMUX_VERSION)
    base = f"{HOME}/Library/Application Support/JetBrains/PyCharm2026.1/options/recentProjects.xml"
    fs = FakeFS({STORE: _store_text(_record()), base: _recent(("$USER_HOME$/a", True, ""))})
    data = await hosts.load_host_data(_source(runner, fs), fs, home=HOME)
    assert data.cmux.store is not None
    assert data.jetbrains["pycharm"][0].path == f"{HOME}/a"
    bare = await hosts.load_host_data(_source(runner, fs), fs, home=HOME, with_jetbrains=False)
    assert bare.jetbrains == {}
