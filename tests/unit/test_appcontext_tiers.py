"""The two tiers of the ``appcontext`` collector (WP5.3).

Tier 1 parses the title of every window on every snapshot. Tier 2 runs on a focus change only: the
tests count the fake runner's calls and the filesystem's reads to prove it. The cmux, JetBrains and
Obsidian second tiers run from file fixtures, and the Chrome history backfill opens SQLite with a
read-only, immutable URI.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from appcontext_support import (
    APPLE_SUPPORT,
    CHROME,
    CHROME_LOCAL_STATE,
    CMUX,
    CMUX_STORE,
    HOME,
    INTELLIJ,
    ITERM2,
    OBSIDIAN,
    OBSIDIAN_JSON,
    PYCHARM,
    SLACK,
    T0,
    World,
    chrome_history_db,
    chrome_local_state,
    cmux_store,
    jetbrains_xml_path,
    obsidian_config,
    recent_projects,
    store_with_windows,
    window,
)

from fylgja.collectors.appcontext import adapter, parse, switches
from fylgja.collectors.appcontext.adapter import (
    ChromeHistory,
    InputError,
    SourceError,
    StoreWindowSource,
    Visit,
)
from fylgja.collectors.appcontext.titles import parse_title
from fylgja.models import ContextItemEvent, HealthState

# --- tier 1 ---------------------------------------------------------------------------------------


async def test_tier_one_attaches_title_context_to_every_window() -> None:
    world = World()
    world.source.windows = [
        window("1", PYCHARM, "proj – main.py [mod]"),
        window("2", SLACK, "! general (Channel) - Team - 3 new items - Slack"),
        window("3", "com.example.unknown", "whatever"),
        window("4", CHROME, None),
    ]
    await world.collector.poll()

    items = world.items("title")
    assert {e.entity_id for e in items} == {"1:title", "2:title"}  # unknown app, no title: none
    by_id = {e.payload.data["window_id"]: e for e in items}
    ide = by_id["1"]
    assert ide.payload.context_kind == "ide"
    assert ide.payload.app_bundle_id == PYCHARM
    assert ide.payload.title == "proj · main.py"
    assert ide.payload.data["tier"] == 1
    assert ide.payload.data["fields"] == {"project": "proj", "file": "main.py", "module": "mod"}
    chat = by_id["2"]
    assert chat.payload.data["unread"] == 3  # the count of A15's explicit marker
    assert chat.payload.title == "general"
    assert world.collector.tier2_runs == 0  # no focus yet


async def test_tier_one_emits_a_change_once_and_withdraws_what_stops_parsing() -> None:
    world = World()
    world.source.windows = [window("1", PYCHARM, "proj – a.py")]
    await world.collector.poll()
    await world.collector.poll()
    assert len(world.items("title")) == 1  # the same title again emits nothing

    world.source.windows = [window("1", PYCHARM, "proj – b.py")]
    await world.collector.poll()
    assert len(world.items("title")) == 2
    assert world.fields("title")["file"] == "b.py"

    world.source.windows = [window("1", PYCHARM, None)]
    await world.collector.poll()
    assert len(world.gone("title")) == 1
    world.source.windows = [window("1", PYCHARM, "")]
    await world.collector.poll()
    assert len(world.gone("title")) == 1  # nothing left to withdraw


async def test_a_closed_window_is_forgotten_without_a_record() -> None:
    world = World()
    world.source.windows = [window("1", PYCHARM, "proj")]
    await world.collector.poll()
    seen = len(world.sink.records)
    world.source.windows = []
    await world.collector.poll()
    assert len(world.sink.records) == seen  # the live view forgets the window's items itself
    world.source.windows = [window("1", PYCHARM, "proj")]
    await world.collector.poll()
    assert len(world.items("title")) == 2  # a new window with the same id starts afresh


# --- tier 2 runs on focus changes only ------------------------------------------------------------


async def test_tier_two_runs_once_per_focus_change_and_never_between() -> None:
    world = World(chrome=True)
    world.runner.script(switches.OSASCRIPT, stdout=b"https://example.org/a?q=secret#frag\n")
    world.source.windows = [window("1", CHROME, "Page - Google Chrome - Person 1")]
    world.source.focus_on("1")

    for _ in range(5):
        await world.collector.poll()
    assert world.collector.tier2_runs == 1
    assert len(world.osascript_calls) == 1  # one call for one focus change

    world.source.focus_on("1")  # a new focus event on the same window is a focus change
    await world.collector.poll()
    await world.collector.poll()
    assert world.collector.tier2_runs == 2
    assert len(world.osascript_calls) == 2

    world.source.windows.append(window("2", PYCHARM, "proj"))
    for _ in range(4):
        await world.collector.poll()  # snapshots without a focus change cost nothing
    assert len(world.osascript_calls) == 2


async def test_focus_on_a_window_not_open_yet_is_retried() -> None:
    world = World()
    world.source.focus_on("9")
    await world.collector.poll()
    assert world.collector.tier2_runs == 0
    world.source.windows = [window("9", PYCHARM, "proj")]
    await world.collector.poll()
    assert world.collector.tier2_runs == 1


async def test_no_focused_window_runs_no_second_tier() -> None:
    world = World()
    world.source.windows = [window("1", PYCHARM, "proj")]
    world.source.focus_on(None)
    await world.collector.poll()
    assert world.collector.tier2_runs == 0


async def test_an_app_without_a_second_tier_costs_nothing() -> None:
    world = World(chrome=True, iterm2=True)
    world.source.windows = [window("1", SLACK, "general (Channel) - Team - Slack")]
    world.source.focus_on("1")
    await world.collector.poll()
    assert world.collector.tier2_runs == 1
    assert world.runner.calls == []
    assert world.fs.reads == []


# --- cmux -----------------------------------------------------------------------------------------


def _cmux_world(store: str | None, title: str | None = "alpha") -> World:
    world = World()
    if store is not None:
        world.fs.add_file(CMUX_STORE, store)
    world.source.windows = [window("7", CMUX, title)]
    world.source.focus_on("7")
    return world


async def test_cmux_second_tier_picks_the_workspace_the_title_names() -> None:
    store = cmux_store(
        {
            "ws-a": ("surface-a", "/work/alpha", 100.0),
            "ws-b": ("surface-b", "/work/beta", 200.0),
        }
    )
    world = _cmux_world(store)
    await world.collector.poll()

    [item] = world.items("cmux")
    assert item.payload.context_kind == "cmux_tab"
    assert item.payload.data["confidence"] == "medium"
    assert item.payload.title == "alpha"
    assert world.fields("cmux") == {
        "cmux_workspace": "ws-a",
        "cmux_surface": "surface-a",
        "via": "cmux-session-store",
        "cwd": "/work/alpha",
        "lifecycle": "running",
    }
    assert world.runner.calls == []  # a file read: no cmux CLI, which is refused outside cmux
    dumped = item.model_dump_json()
    assert "hunter2" not in dumped
    assert "never-read" not in dumped  # launch arguments and transcript path are never read


async def test_cmux_without_a_title_match_guesses_the_latest_with_low_confidence() -> None:
    store = cmux_store({"ws-a": ("sa", "/w/alpha", 100.0), "ws-b": ("sb", "/w/beta", 200.0)})
    world = _cmux_world(store, title="gamma")
    await world.collector.poll()
    assert world.fields("cmux")["cmux_workspace"] == "ws-b"
    assert world.items("cmux")[0].payload.data["confidence"] == "low"


async def test_cmux_with_one_workspace_is_that_workspace() -> None:
    world = _cmux_world(cmux_store({"ws-a": ("sa", "/w/alpha", 100.0)}), title="gamma")
    await world.collector.poll()
    assert world.items("cmux")[0].payload.data["confidence"] == "medium"


async def test_cmux_store_missing_gives_no_item_and_no_failure() -> None:
    world = _cmux_world(None)
    await world.collector.poll()
    assert world.items("cmux") == []
    assert (await world.collector.doctor()).state is HealthState.OK


@pytest.mark.parametrize("text", ["not json", cmux_store({"w": ("s", "/x", 1.0)}, version=2)])
async def test_cmux_unrecognized_store_degrades_only_that_source(text: str) -> None:
    world = _cmux_world(text)
    world.source.windows.append(window("8", PYCHARM, "proj"))
    await world.collector.poll()
    report = await world.collector.doctor()
    assert report.state is HealthState.DEGRADED
    assert report.error_kind == "cmux_unrecognized_format"
    assert len(world.items("title")) == 2  # tier 1 keeps reporting


async def test_cmux_store_recovers_and_withdraws_a_vanished_item() -> None:
    world = _cmux_world("not json")
    await world.collector.poll()
    assert (await world.collector.doctor()).state is HealthState.DEGRADED
    world.fs.add_file(CMUX_STORE, cmux_store({"ws-a": ("sa", "/w/alpha", 1.0)}))
    world.source.focus_on("7")
    await world.collector.poll()
    assert (await world.collector.doctor()).state is HealthState.OK
    assert len(world.items("cmux")) == 1
    world.fs.add_file(CMUX_STORE, cmux_store({}))
    world.source.focus_on("7")
    await world.collector.poll()
    assert len(world.gone("cmux")) == 1


async def test_cmux_unreadable_store_is_a_failure_with_a_code_not_a_path() -> None:
    world = _cmux_world(cmux_store({"w": ("s", "/x", 1.0)}))
    world.fs.fail(CMUX_STORE, PermissionError("denied"))
    await world.collector.poll()
    report = await world.collector.doctor()
    assert report.error_kind == "cmux_permission_denied"
    assert str(CMUX_STORE) not in (report.detail or "")


# --- JetBrains ------------------------------------------------------------------------------------


def _jetbrains_world(title: str, **files: str) -> World:
    world = World()
    for product, text in files.items():
        world.fs.add_file(jetbrains_xml_path(product), text, mtime=float(len(product)))
    world.source.windows = [window("3", PYCHARM, title)]
    world.source.focus_on("3")
    return world


async def test_jetbrains_second_tier_finds_the_project_path_in_recent_projects() -> None:
    xml = recent_projects([("projects/acme/api", None, 5), ("projects/other", "Other", 9)])
    world = _jetbrains_world("api – main.py", PyCharm2026_1=xml)
    await world.collector.poll()
    assert world.fields("jetbrains") == {
        "path": f"{HOME}/projects/acme/api",
        "via": "recentProjects.xml",
        "project": "api",
    }
    assert world.items("jetbrains")[0].payload.data["confidence"] == "high"
    assert world.items("jetbrains")[0].payload.context_kind == "ide_project"


async def test_jetbrains_uses_the_newest_product_directory() -> None:
    old = recent_projects([("old/api", None, 1)])
    new = recent_projects([("new/api", None, 1)])
    world = World()
    world.fs.add_file(jetbrains_xml_path("PyCharm2025.1"), old, mtime=1.0)
    world.fs.add_file(jetbrains_xml_path("PyCharm2026.1"), new, mtime=2.0)
    world.fs.add_file(jetbrains_xml_path("IntelliJIdea2026.2"), old, mtime=9.0)  # another IDE
    world.source.windows = [window("3", PYCHARM, "api")]
    world.source.focus_on("3")
    await world.collector.poll()
    assert world.fields("jetbrains")["path"] == f"{HOME}/new/api"


async def test_jetbrains_title_with_a_path_needs_no_xml() -> None:
    world = _jetbrains_world("proj [~/code/proj] – main.py")
    await world.collector.poll()
    assert world.fields("jetbrains")["path"] == f"{HOME}/code/proj"
    assert world.fields("jetbrains")["via"] == "title"
    assert world.fs.reads == []


async def test_jetbrains_two_projects_with_one_name_pick_the_latest_with_medium_confidence() -> (
    None
):
    xml = recent_projects([("a/api", None, 1), ("b/api", None, 5)])
    world = _jetbrains_world("api", PyCharm2026_1=xml)
    await world.collector.poll()
    assert world.fields("jetbrains")["path"] == f"{HOME}/b/api"
    assert world.items("jetbrains")[0].payload.data["confidence"] == "medium"


async def test_tier_one_resolves_the_project_path_of_every_ide_window() -> None:
    xml = recent_projects([("work/alpha", None, 5), ("work/beta", None, 9)])
    world = World()
    world.fs.add_file(jetbrains_xml_path("PyCharm2026_1"), xml, mtime=1.0)
    world.source.windows = [window("1", PYCHARM, "alpha – a.py"), window("2", PYCHARM, "beta")]
    await world.collector.poll()  # no focus event
    assert world.collector.tier2_runs == 0
    by_window = {e.payload.data["window_id"]: e for e in world.items("jetbrains")}
    assert set(by_window) == {"1", "2"}
    assert by_window["1"].payload.data["fields"]["path"] == f"{HOME}/work/alpha"
    assert by_window["2"].payload.data["fields"]["path"] == f"{HOME}/work/beta"
    assert by_window["1"].payload.context_kind == "ide_project"


async def test_recent_projects_is_read_again_only_when_its_mtime_changes() -> None:
    path = jetbrains_xml_path("PyCharm2026_1")
    world = World()
    world.fs.add_file(path, recent_projects([("work/alpha", None, 5)]), mtime=1.0)
    world.source.windows = [window("1", PYCHARM, "alpha")]
    await world.collector.poll()
    assert world.fs.reads.count(PurePosixPath(str(path))) == 1

    await world.clock.advance(31)  # past the re-stat interval
    await world.collector.poll()
    assert world.fs.reads.count(PurePosixPath(str(path))) == 1  # same mtime: not parsed again

    world.fs.replace_file(path, recent_projects([("work/alpha", None, 6)]), mtime=2.0)
    await world.collector.poll()
    assert world.fs.reads.count(PurePosixPath(str(path))) == 1  # within the interval: no re-stat
    await world.clock.advance(31)
    await world.collector.poll()
    assert world.fs.reads.count(PurePosixPath(str(path))) == 2


async def test_an_open_project_wins_over_closed_ones_with_the_same_name() -> None:
    xml = recent_projects([("a/main", None, 9, False), ("b/main", None, 1, True)])
    world = World()
    world.fs.add_file(jetbrains_xml_path("PyCharm2026_1"), xml, mtime=1.0)
    world.source.windows = [window("1", PYCHARM, "main")]
    await world.collector.poll()
    assert world.fields("jetbrains") == {
        "path": f"{HOME}/b/main",
        "via": "recentProjects.xml:opened",
        "project": "main",
    }
    assert world.items("jetbrains")[0].payload.data["confidence"] == "high"


async def test_the_frame_title_breaks_a_tie_between_open_projects_with_one_name() -> None:
    xml = recent_projects([("a/main", "main – x.py", 9), ("b/main", "main – y.py", 1)])
    world = World()
    world.fs.add_file(jetbrains_xml_path("PyCharm2026_1"), xml, mtime=1.0)
    world.source.windows = [window("1", PYCHARM, "main – y.py")]
    await world.collector.poll()
    assert world.fields("jetbrains")["path"] == f"{HOME}/b/main"
    assert world.fields("jetbrains")["via"] == "recentProjects.xml:frameTitle"
    assert world.items("jetbrains")[0].payload.data["confidence"] == "medium"


async def test_a_title_names_the_project_its_saved_frame_title_names() -> None:
    xml = recent_projects(
        [("work/fylgja/main", "fylgja main – notes.md", 9), ("work/b/main", None, 1)]
    )
    world = World()
    world.fs.add_file(jetbrains_xml_path("PyCharm2026_1"), xml, mtime=1.0)
    world.source.windows = [window("1", PYCHARM, "fylgja main – report.md [main]")]
    await world.collector.poll()
    assert world.fields("jetbrains") == {
        "path": f"{HOME}/work/fylgja/main",
        "via": "recentProjects.xml",
        "project": "fylgja main",
    }
    assert world.items("jetbrains")[0].payload.data["confidence"] == "high"


async def test_a_title_of_parent_and_directory_names_that_directory() -> None:
    # The saved frame title is stale and names only "main"; the window shows "cbslink main".
    xml = recent_projects(
        [("work/cbslink/main", "main – old.txt", 9, False), ("work/x/main", None, 1)]
    )
    world = World()
    world.fs.add_file(jetbrains_xml_path("IntelliJIdea2026.2"), xml, mtime=1.0)
    world.source.windows = [window("1", INTELLIJ, "cbslink main – README.md [cbslink-main]")]
    await world.collector.poll()
    assert world.fields("jetbrains")["path"] == f"{HOME}/work/cbslink/main"
    assert world.items("jetbrains")[0].payload.data["confidence"] == "high"


async def test_a_window_keeps_a_high_confidence_path_when_a_later_reading_is_weaker() -> None:
    path = jetbrains_xml_path("PyCharm2026_1")
    world = World()
    world.fs.add_file(path, recent_projects([("a/main", None, 1)]), mtime=1.0)
    world.source.windows = [window("1", PYCHARM, "main")]
    await world.collector.poll()
    assert world.fields("jetbrains")["path"] == f"{HOME}/a/main"

    xml = recent_projects([("a/main", None, 1), ("b/main", None, 5)])  # now ambiguous: medium
    world.fs.replace_file(path, xml, mtime=2.0)
    await world.clock.advance(31)
    await world.collector.poll()
    assert world.fields("jetbrains")["path"] == f"{HOME}/a/main"
    assert len(world.items("jetbrains")) == 1  # nothing new was emitted

    world.source.windows = [window("1", PYCHARM, "other")]  # a new project name replaces it
    await world.collector.poll()
    assert world.gone("jetbrains")


async def test_a_closed_window_forgets_its_remembered_path() -> None:
    path = jetbrains_xml_path("PyCharm2026_1")
    world = World()
    world.fs.add_file(path, recent_projects([("a/main", None, 1)]), mtime=1.0)
    world.source.windows = [window("1", PYCHARM, "main")]
    await world.collector.poll()
    assert world.collector._ide_paths
    assert world.collector._ide_seen

    world.source.windows = []
    await world.collector.poll()
    assert not world.collector._ide_paths
    assert not world.collector._ide_seen

    xml = recent_projects([("a/main", None, 1), ("b/main", None, 5)])
    world.fs.replace_file(path, xml, mtime=2.0)
    await world.clock.advance(31)
    world.source.windows = [window("1", PYCHARM, "main")]  # a new window with the same id
    await world.collector.poll()
    assert world.fields("jetbrains")["path"] == f"{HOME}/b/main"  # nothing remembered: medium


async def test_jetbrains_unknown_project_gives_no_item() -> None:
    world = _jetbrains_world("missing", PyCharm2026_1=recent_projects([("a/api", None, 1)]))
    await world.collector.poll()
    assert world.items("jetbrains") == []


async def test_jetbrains_no_product_directory_is_not_a_failure() -> None:
    world = _jetbrains_world("api")
    await world.collector.poll()
    assert world.items("jetbrains") == []
    assert (await world.collector.doctor()).state is HealthState.OK


async def test_jetbrains_garbled_xml_degrades_only_that_source() -> None:
    world = _jetbrains_world("api", PyCharm2026_1="<application><oops")
    await world.collector.poll()
    report = await world.collector.doctor()
    assert report.state is HealthState.DEGRADED
    assert report.error_kind == "jetbrains_unrecognized_format"
    assert len(world.items("title")) == 1


async def test_datagrip_uses_its_own_product_directory() -> None:
    world = World()
    world.fs.add_file(
        jetbrains_xml_path("DataGrip2024.3"), recent_projects([("db/warehouse", None, 1)])
    )
    world.source.windows = [window("3", "com.jetbrains.datagrip", "warehouse – console")]
    world.source.focus_on("3")
    await world.collector.poll()
    assert world.fields("jetbrains")["path"] == f"{HOME}/db/warehouse"


# --- Obsidian -------------------------------------------------------------------------------------


def _obsidian_world(config: str | None, title: str = "Daily - Notes - Obsidian v1.13.7") -> World:
    world = World()
    if config is not None:
        world.fs.add_file(OBSIDIAN_JSON, config)
    world.source.windows = [window("5", OBSIDIAN, title)]
    world.source.focus_on("5")
    return world


async def test_obsidian_second_tier_gives_the_vault_path_the_note_and_a_link() -> None:
    world = _obsidian_world(obsidian_config({"abc": ("/vaults/Notes", 5), "def": ("/vaults/x", 1)}))
    await world.collector.poll()
    [item] = world.items("obsidian")
    assert item.payload.context_kind == "note"
    assert item.payload.title == "Daily"
    assert item.payload.data["link"] == "obsidian://open?vault=Notes&file=Daily"
    assert world.fields("obsidian") == {
        "vault": "Notes",
        "vault_path": "/vaults/Notes",
        "note": "Daily",
    }
    assert item.payload.data["confidence"] == "high"


async def test_obsidian_unknown_vault_still_links_with_medium_confidence() -> None:
    world = _obsidian_world(obsidian_config({"abc": ("/vaults/Other", 5)}))
    await world.collector.poll()
    assert "vault_path" not in world.fields("obsidian")
    assert world.items("obsidian")[0].payload.data["confidence"] == "medium"


async def test_obsidian_without_a_config_file_still_links() -> None:
    world = _obsidian_world(None, title="Notes - Obsidian v1.13.7")
    await world.collector.poll()
    assert world.items("obsidian")[0].payload.data["link"] == "obsidian://open?vault=Notes"


async def test_obsidian_garbled_config_degrades_only_that_source() -> None:
    world = _obsidian_world("{ nope")
    await world.collector.poll()
    assert (await world.collector.doctor()).error_kind == "obsidian_unrecognized_format"
    assert len(world.items("title")) == 1


async def test_obsidian_title_without_a_vault_has_no_item() -> None:
    world = _obsidian_world(obsidian_config({}), title="no such shape")
    await world.collector.poll()
    assert world.items("obsidian") == []


# --- Chrome and iTerm2 (switches on) --------------------------------------------------------------


async def test_chrome_tab_url_loses_its_query_and_fragment() -> None:
    world = World(chrome=True)
    world.runner.script(switches.OSASCRIPT, stdout=b"https://user:pw@example.org:8443/a/b?q=1#x\n")
    world.source.windows = [window("1", CHROME, "A page - Google Chrome - Person 1")]
    world.source.focus_on("1")
    await world.collector.poll()
    [item] = world.items("chrome")
    assert item.payload.context_kind == "tab"
    assert item.payload.title == "A page"
    assert world.fields("chrome") == {"url": "https://example.org:8443/a/b", "profile": "Person 1"}
    assert world.runner.timeouts == [3.0]  # every osascript has the 3 s timeout
    assert "pw" not in item.model_dump_json()


async def test_chrome_internal_page_withdraws_the_tab_item() -> None:
    world = World(chrome=True)
    world.runner.script(switches.OSASCRIPT, stdout=b"https://example.org/\n", times=1)
    world.runner.script(switches.OSASCRIPT, stdout=b"chrome://settings/\n")
    world.source.windows = [window("1", CHROME, "A page - Google Chrome")]
    world.source.focus_on("1")
    await world.collector.poll()
    assert len(world.items("chrome")) == 1
    world.source.focus_on("1")
    await world.collector.poll()
    assert len(world.gone("chrome")) == 1


async def test_iterm2_path_and_job() -> None:
    world = World(iterm2=True)
    world.runner.script(switches.OSASCRIPT, stdout=b"/Users/erik/code\nvim\n")
    world.source.windows = [window("2", ITERM2, "whatever")]
    world.source.focus_on("2")
    await world.collector.poll()
    [item] = world.items("iterm2")
    assert item.payload.context_kind == "terminal_session"
    assert item.payload.title == "vim"
    assert world.fields("iterm2") == {"path": "/Users/erik/code", "job": "vim"}


async def test_iterm2_with_only_a_path_titles_it_by_its_last_component() -> None:
    world = World(iterm2=True)
    world.runner.script(switches.OSASCRIPT, stdout=b"/Users/erik/code\n")
    world.source.windows = [window("2", ITERM2, "x")]
    world.source.focus_on("2")
    await world.collector.poll()
    assert world.items("iterm2")[0].payload.title == "code"


async def test_iterm2_empty_reply_gives_no_item() -> None:
    world = World(iterm2=True)
    world.runner.script(switches.OSASCRIPT, stdout=b"")
    world.source.windows = [window("2", ITERM2, "x")]
    world.source.focus_on("2")
    await world.collector.poll()
    assert world.items("iterm2") == []


# --- Chrome history backfill ----------------------------------------------------------------------


def _history_world(
    *, history: bool = True, profiles: dict[str, tuple[str, str]] | None = None
) -> World:
    world = World(chrome_history=history, history_limit=3)
    world.fs.add_file(
        CHROME_LOCAL_STATE,
        chrome_local_state(profiles or {"Default": ("Person 1", "p1@example.org")}),
    )
    world.history.visits = [
        Visit("https://example.org/a?x=1", "Title A", T0 - timedelta(minutes=5)),
        Visit("chrome://history", "skipped", T0),
        Visit("https://example.org/b", None, T0 - timedelta(minutes=9)),
    ]
    world.source.windows = [window("1", CHROME, "Page - Google Chrome - Person 1")]
    world.source.focus_on("1")
    return world


async def test_history_backfill_is_off_by_default() -> None:
    world = _history_world(history=False)
    await world.collector.poll()
    assert world.history.calls == []
    assert world.items("history") == []


async def test_history_backfill_attaches_recent_visits_without_queries() -> None:
    world = _history_world()
    await world.collector.poll()
    [call] = world.history.calls
    assert call[0] == "Default"
    assert call[1] == T0 - timedelta(hours=1)
    assert call[2] == 3
    [item] = world.items("history")
    assert item.payload.context_kind == "browser_history"
    assert item.payload.title == "2 recent visits"
    visits = world.fields("history")["visits"]
    assert [v["url"] for v in visits] == ["https://example.org/a", "https://example.org/b"]
    assert "x=1" not in item.model_dump_json()


async def test_history_with_two_profiles_needs_a_title_that_names_one() -> None:
    profiles = {"Default": ("Person 1", "a@example.org"), "Profile 2": ("work", "b@example.org")}
    world = _history_world(profiles=profiles)
    world.source.windows = [window("1", CHROME, "Page - Google Chrome - person-2 (work)")]
    await world.collector.poll()
    assert world.history.calls[0][0] == "Profile 2"

    other = _history_world(profiles=profiles)
    other.source.windows = [window("1", CHROME, "Page - Google Chrome")]
    await other.collector.poll()
    assert other.history.calls == []  # which profile is unknowable: nothing is guessed


async def test_history_failures_degrade_only_the_history_source() -> None:
    world = _history_world()
    world.history.error = InputError("history_read_failed")
    await world.collector.poll()
    report = await world.collector.doctor()
    assert report.error_kind == "history_history_read_failed"
    assert len(world.items("title")) == 1


async def test_history_with_an_unrecognized_local_state_degrades() -> None:
    world = _history_world()
    world.fs.add_file(CHROME_LOCAL_STATE, "not json")
    await world.collector.poll()
    assert (await world.collector.doctor()).error_kind == "history_unrecognized_format"


def test_the_history_uri_is_read_only_and_immutable(tmp_path: Path) -> None:
    target = tmp_path / "Application Support" / "History"
    uri = adapter.history_uri(target)
    assert uri.startswith("file:///")
    assert uri.endswith("?mode=ro&immutable=1")
    assert "Application%20Support" in uri


def test_chrome_history_reads_a_real_database_read_only(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = adapter.chrome_root(home) / "Profile 2" / "History"
    chrome_history_db(
        path,
        [
            ("https://example.org/old", "Old", T0 - timedelta(days=3)),
            ("https://example.org/new", "New", T0 - timedelta(minutes=1)),
            ("https://example.org/newer", "", T0),
        ],
    )
    visits = ChromeHistory(home).recent("Profile 2", T0 - timedelta(hours=1), 10)
    assert [v.url for v in visits] == ["https://example.org/newer", "https://example.org/new"]
    assert visits[0].title is None
    assert visits[1].at == T0 - timedelta(minutes=1)
    before = path.read_bytes()
    ChromeHistory(home).recent("Profile 2", T0 - timedelta(hours=1), 10)
    assert path.read_bytes() == before  # nothing was written


def test_chrome_history_refuses_bad_directories_and_missing_files(tmp_path: Path) -> None:
    reader = ChromeHistory(tmp_path)
    assert reader.recent("Default", T0, 5) == []  # no file
    for bad in ("../Containers", "..", "a/b", ""):
        with pytest.raises(InputError):
            reader.recent(bad, T0, 5)


def test_chrome_history_that_is_not_a_database_is_an_input_error(tmp_path: Path) -> None:
    path = adapter.chrome_root(tmp_path) / "Default" / "History"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a database" * 100)
    with pytest.raises(InputError) as caught:
        ChromeHistory(tmp_path).recent("Default", T0, 5)
    assert caught.value.kind == "history_read_failed"


def test_the_history_connection_cannot_write(tmp_path: Path) -> None:
    path = adapter.chrome_root(tmp_path) / "Default" / "History"
    chrome_history_db(path, [])
    connection = sqlite3.connect(adapter.history_uri(path), uri=True)
    with pytest.raises(sqlite3.OperationalError):
        connection.execute("INSERT INTO urls (url) VALUES ('x')")
    connection.close()


# --- protected paths ------------------------------------------------------------------------------


def test_containers_are_never_read() -> None:
    for protected in (
        HOME / "Library" / "Containers" / "x" / "y",
        HOME / "Library" / "Group Containers" / "z",
    ):
        with pytest.raises(InputError) as caught:
            adapter.check_readable(protected, HOME)
        assert caught.value.kind == "protected_path"
    adapter.check_readable(APPLE_SUPPORT / "obsidian" / "obsidian.json", HOME)
    adapter.check_readable(Path("/elsewhere/Containers"), HOME)


def test_read_input_maps_errors_to_codes() -> None:
    from fylgja.testing.fakes import FakeFS

    fs = FakeFS({"/ok": "text"})
    assert adapter.read_input(fs, Path("/ok"), HOME) == "text"
    assert adapter.read_input(fs, Path("/missing"), HOME) is None
    fs.fail("/denied", PermissionError("x"))
    fs.fail("/dir", IsADirectoryError("x"))
    fs.fail("/boom", OSError("x"))
    fs.add_file("/binary", b"\xff\xfe\x00")
    for path, kind in (
        ("/denied", "permission_denied"),
        ("/dir", "is_a_directory"),
        ("/boom", "os_error"),
        ("/binary", "not_text"),
        (str(HOME / "Library" / "Containers" / "a"), "protected_path"),
    ):
        with pytest.raises(InputError) as caught:
            adapter.read_input(fs, Path(path), HOME)
        assert caught.value.kind == kind


# --- the store ------------------------------------------------------------------------------------


def test_the_store_source_reads_windows_and_focus(tmp_path: Path) -> None:
    database = store_with_windows(
        tmp_path / "fylgja.db",
        [("1", PYCHARM, "proj"), ("2", CHROME, "Page - Google Chrome")],
        focus="2",
    )
    snapshot = StoreWindowSource(database).read()
    assert [(w.window_id, w.bundle_id, w.title) for w in snapshot.windows] == [
        ("1", PYCHARM, "proj"),
        ("2", CHROME, "Page - Google Chrome"),
    ]
    assert snapshot.focus is not None
    assert snapshot.focus.window_id == "2"


def test_the_store_source_without_a_store_has_nothing(tmp_path: Path) -> None:
    snapshot = StoreWindowSource(tmp_path / "none.db").read()
    assert snapshot.windows == ()
    assert snapshot.focus is None


def test_the_store_source_without_focus(tmp_path: Path) -> None:
    database = store_with_windows(tmp_path / "f.db", [("1", PYCHARM, "p")], focus=None)
    assert StoreWindowSource(database).read().focus is None


def test_an_unreadable_store_is_a_source_error(tmp_path: Path) -> None:
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a database" * 64)
    with pytest.raises(SourceError) as caught:
        StoreWindowSource(broken).read()
    assert caught.value.kind.startswith("store_")


async def test_a_failing_source_degrades_and_recovers() -> None:
    world = World()
    world.source.error = SourceError("store_OperationalError")
    await world.collector.poll()
    report = await world.collector.doctor()
    assert report.state is HealthState.DEGRADED
    assert report.error_kind == "store_store_OperationalError"
    world.source.error = None
    world.source.windows = [window("1", PYCHARM, "proj")]
    await world.collector.poll()
    assert (await world.collector.doctor()).state is HealthState.OK


async def test_the_run_loop_polls_on_the_clock_and_survives_a_crash() -> None:
    import asyncio

    world = World(poll_interval_seconds=1.0)
    world.source.windows = [window("1", PYCHARM, "proj")]
    task = asyncio.create_task(world.collector.run())
    try:
        await world.clock.advance(3.0)
        assert world.collector.polls >= 3
        assert (await world.collector.doctor()).state is HealthState.OK

        def explode() -> Any:
            raise RuntimeError("boom")

        world.source.read = explode  # type: ignore[method-assign]
        await world.clock.advance(2.0)
        report = await world.collector.doctor()
        assert report.state is HealthState.DEGRADED
        assert report.error_kind == "poll_failed"
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.wait({task})


async def test_doctor_before_the_first_poll_says_so() -> None:
    world = World()
    report = await world.collector.doctor()
    assert report.state is HealthState.OK
    assert report.detail == "not polled yet"


# --- the pure helpers -----------------------------------------------------------------------------


def test_title_summaries_per_kind() -> None:
    cases = [
        (CHROME, "A - B - Google Chrome - Person 1", "A - B"),
        (PYCHARM, "proj – a.py", "proj · a.py"),
        ("com.jetbrains.datagrip", "wh – console", "wh · console"),
        ("com.microsoft.Outlook", 'Inbox "me@example.org"', "Inbox"),
        (OBSIDIAN, "Daily - Notes - Obsidian v1.13.7", "Daily · Notes"),
        (CMUX, "alpha", "alpha"),
        ("com.microsoft.teams2", "Chat | Doe, Jane | Microsoft Teams", "Doe, Jane"),
        (SLACK, "general (Channel) - Team - Slack", "general"),
    ]
    for bundle, title, expected in cases:
        context = parse_title(bundle, title)
        assert context is not None
        assert parse.title_summary(context) == expected, title
    bare = parse_title("com.microsoft.teams2", "Microsoft Teams")
    assert bare is not None
    assert parse.title_summary(bare) is None
    welcome = parse_title(PYCHARM, "Welcome to PyCharm")
    assert welcome is not None
    assert parse.title_summary(welcome) is None


def test_long_summaries_are_clipped() -> None:
    context = parse_title(CHROME, f"{'x' * 400} - Google Chrome")
    assert context is not None
    summary = parse.title_summary(context)
    assert summary is not None
    assert len(summary) == parse.MAX_SUMMARY_CHARS
    assert summary.endswith("…")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://example.org/a/b?q=1#f", "https://example.org/a/b"),
        ("http://u:p@example.org:81/x", "http://example.org:81/x"),
        ("HTTPS://Example.ORG", "https://example.org"),
        ("chrome://settings", None),
        ("about:blank", None),
        ("javascript:alert(1)", None),
        ("https://", None),
        ("https://bad host/x", None),
        ("http://[::1", None),
        ("", None),
        ("https://example.org:99999/x", None),
        ("https://example.org/" + "a" * 2000, None),
    ],
)
def test_sanitize_url(raw: str, expected: str | None) -> None:
    assert parse.sanitize_url(raw) == expected


def test_sanitize_url_clips_a_long_path() -> None:
    shown = parse.sanitize_url("https://example.org/" + "a" * 500)
    assert shown is not None
    assert len(shown) == parse.MAX_URL_CHARS


def test_cmux_parsing_edge_cases() -> None:
    assert parse.parse_cmux_tabs("[]") is None
    assert parse.parse_cmux_tabs('{"version": 1, "sessions": []}') is None
    assert parse.parse_cmux_tabs('{"version": 1, "sessions": {}}') == []
    assert parse.parse_cmux_tabs("x" * (parse.MAX_CONFIG_BYTES + 1)) is None
    odd = (
        '{"version":1,"sessions":{"s":{"surfaceId":"x","workspaceId":"other"},"t":{}},'
        '"activeSessionsByWorkspace":{"w":{"sessionId":"s"},"v":{"sessionId":"t"},'
        '"u":{"sessionId":"gone"},"z":"no"}}'
    )
    assert parse.parse_cmux_tabs(odd) == []  # wrong workspace, no surface, unknown, not a mapping
    assert parse.pick_cmux_tab([], "x") is None


def test_pick_cmux_tab_with_two_title_matches_is_low() -> None:
    tabs = [
        parse.CmuxTab("a", "sa", "/w/alpha", None, 1.0),
        parse.CmuxTab("b", "sb", "/v/alpha", None, 2.0),
    ]
    picked = parse.pick_cmux_tab(tabs, "Alpha")
    assert picked is not None
    assert (picked[0].workspace_id, picked[1]) == ("b", "low")


def test_jetbrains_parsing_edge_cases() -> None:
    assert parse.parse_jetbrains_projects("<nope", "/h") is None
    assert parse.parse_jetbrains_projects("<other/>", "/h") is None
    assert parse.parse_jetbrains_projects("x" * (parse.MAX_CONFIG_BYTES + 1), "/h") is None
    xml = (
        '<application><entry key="relative/dir"/><entry key="/abs/proj">'
        '<RecentProjectMetaInfo displayName="Shown"><option name="activationTimestamp" value="x"/>'
        "</RecentProjectMetaInfo></entry><entry/></application>"
    )
    [project] = parse.parse_jetbrains_projects(xml, "/h") or []
    assert project.path == "/abs/proj"
    assert project.names == ("Shown", "proj", "abs proj")
    assert project.opened_ms == 0
    assert parse.resolve_jetbrains_project({}, [project], "/h") is None
    assert parse.resolve_jetbrains_project({"project": "shown"}, [project], "/h") == (
        "/abs/proj",
        "high",
        "recentProjects.xml",
    )
    tilde = parse.resolve_jetbrains_project({"path": "~"}, [], "/h")
    assert tilde == ("/h", "high", "title")
    assert parse.jetbrains_product_prefixes("com.example") == ()
    assert parse.jetbrains_product_prefixes("com.google.android.studio") == ()


def test_a_project_opened_after_the_last_save_is_found_next_to_its_siblings() -> None:
    listed = [
        parse.JetBrainsProject("/w/cbslink/main", ("cbslink main",)),
        parse.JetBrainsProject("/w/cbslink/CBS-1", ("cbslink CBS-1",)),
        parse.JetBrainsProject("/w/other/main", ("other main",)),
    ]
    dirs = {"/w/cbslink/CBS-2", "/w/other/CBS-2"}
    fields = {"project": "cbslink CBS-2"}
    assert parse.resolve_jetbrains_project(fields, listed, "/h", is_dir=dirs.__contains__) == (
        "/w/cbslink/CBS-2",
        "medium",
        "recentProjects.xml:sibling",
    )
    assert parse.resolve_jetbrains_project(fields, listed, "/h") is None  # nothing to check with
    missing = {"project": "cbslink CBS-3"}
    assert parse.resolve_jetbrains_project(missing, listed, "/h", is_dir=dirs.__contains__) is None
    bare = {"project": "CBS-2"}  # in two folders
    assert parse.resolve_jetbrains_project(bare, listed, "/h", is_dir=dirs.__contains__) is None


def test_a_bare_directory_title_is_found_in_the_one_folder_that_holds_it() -> None:
    listed = [
        parse.JetBrainsProject("/w/norn/main", ("norn main",)),
        parse.JetBrainsProject("/w/cbslink/main", ("cbslink main",)),
    ]
    dirs = {"/w/norn/refactor", "/w/norn/main", "/w/cbslink/main"}
    found = parse.resolve_jetbrains_project(
        {"project": "refactor"}, listed, "/h", is_dir=dirs.__contains__
    )
    assert found == ("/w/norn/refactor", "medium", "recentProjects.xml:sibling")
    for name in ("other", "..", "norn/refactor"):
        fields = {"project": name}
        assert (
            parse.resolve_jetbrains_project(fields, listed, "/h", is_dir=dirs.__contains__) is None
        )


def test_obsidian_parsing_edge_cases() -> None:
    assert parse.parse_obsidian_vaults("[]") is None
    assert parse.parse_obsidian_vaults('{"vaults": []}') is None
    assert parse.parse_obsidian_vaults("x" * (parse.MAX_CONFIG_BYTES + 1)) is None
    text = '{"vaults": {"a": {"path": "rel"}, "b": 1, "c": {"path": "/v/N/", "ts": "x"}}}'
    [vault] = parse.parse_obsidian_vaults(text) or []
    assert (vault.path, vault.name, vault.opened_ms) == ("/v/N", "N", 0)
    assert parse.resolve_vault([vault], None) is None
    assert parse.resolve_vault([vault], "other") is None
    twice = [parse.Vault("/a/N", 1), parse.Vault("/b/N", 2)]
    best = parse.resolve_vault(twice, "N")
    assert best is not None
    assert best.path == "/b/N"
    assert parse.obsidian_link("My Vault", "A/B note") == (
        "obsidian://open?vault=My%20Vault&file=A%2FB%20note"
    )


def test_chrome_profile_matching() -> None:
    assert parse.parse_chrome_profiles("[]") is None
    assert parse.parse_chrome_profiles('{"profile": {}}') is None
    assert parse.parse_chrome_profiles("x" * (parse.MAX_CONFIG_BYTES + 1)) is None
    profiles = parse.parse_chrome_profiles(
        '{"profile": {"info_cache": {"Default": {"name": "Person 1"}, "P2": {"name": "work",'
        ' "user_name": "me@example.org"}, "bad": 1, "P3": {"name": "work"}}}}'
    )
    assert profiles is not None
    assert parse.chrome_profile_dir(profiles, "Person 1") == "Default"
    assert parse.chrome_profile_dir(profiles, "me@example.org") == "P2"
    assert parse.chrome_profile_dir(profiles, "x (Person 1)") == "Default"
    assert parse.chrome_profile_dir(profiles, "work") is None  # two profiles are named "work"
    assert parse.chrome_profile_dir(profiles, None) is None
    assert parse.chrome_profile_dir(profiles, "nobody") is None


def test_iterm2_output_parsing() -> None:
    assert parse.parse_iterm2_output("/a\nvim\n") == ("/a", "vim")
    assert parse.parse_iterm2_output("/a") == ("/a", None)
    assert parse.parse_iterm2_output("") == (None, None)


def test_chrome_time_round_trips() -> None:
    moment = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
    assert parse.unix_from_chrome_time(parse.chrome_time_from_unix(moment)) == moment


def test_an_item_fingerprint_follows_its_content() -> None:
    base = parse.Item(key="k", kind="x", tier=2, title="t", fields={"a": "1"})
    same = parse.Item(key="k", kind="x", tier=2, title="t", fields={"a": "1"})
    other = parse.Item(key="k", kind="x", tier=2, title="t", fields={"a": "2"})
    assert base.fingerprint() == same.fingerprint() != other.fingerprint()


async def test_records_carry_the_window_and_validate() -> None:
    world = World()
    world.source.windows = [window("1", PYCHARM, "proj – a.py")]
    await world.collector.poll()
    [record] = world.events
    assert isinstance(record, ContextItemEvent)
    assert record.source == "appcontext"
    assert record.entity_id == "1:title"
    assert record.observed_at == T0
    gone = parse.gone_record("1", "title", PYCHARM, T0)
    assert gone.payload.data == {"window_id": "1", "key": "title", "gone": True}
