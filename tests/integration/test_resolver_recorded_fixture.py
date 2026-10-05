"""The resolver over a recorded live capture agrees with the join spike's own table (WP1.8).

``tests/fixtures/resolver/recorded/`` is a sanitized capture of the join inputs (see its README);
``expected.json`` is what ``spikes/join_table.py --from-capture`` printed for it. The resolver gets
the registry rows through the Claude parser, the ancestor chains through the Claude ``ps`` parser
and the host data through the host adapters' parsers, so the whole chain is exercised. The check is
on 100 % of rows for the workspace, the project, the state, the host and the window.

The same capture also goes through a running daemon (ingest gate, writer, live view, resolver) and
is read back from ``/now``: the spike table and ``/now`` agree on 100 % of rows (WP1.8's done
condition).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from api_support import (
    Win,
    activity_snapshot,
    client,
    make_daemon,
    session_snapshot,
    sessions_collector,
    window_snapshot,
    windows_collector,
)

from fylgja.collectors.claude import parse
from fylgja.core import hosts
from fylgja.core.hosts import CmuxData, CmuxStore, HostData, JetBrainsProject
from fylgja.core.resolver import Placement, Resolution, SessionInput, resolve
from fylgja.models import ProcessRef
from fylgja.store.rows import WindowRow
from fylgja.testing.fakes import FakeClock

CAPTURE = Path(__file__).resolve().parent.parent / "fixtures" / "resolver" / "recorded"
OBSERVED = datetime(2026, 10, 5, 19, 0, tzinfo=UTC)


class Capture:
    """The capture, parsed the way the daemon parses live input."""

    def __init__(self, directory: Path) -> None:
        self.meta: dict[str, Any] = json.loads((directory / "meta.json").read_text())
        self.expected: list[dict[str, Any]] = json.loads((directory / "expected.json").read_text())
        self.windows = [
            _window(item) for item in json.loads((directory / "windows.json").read_text())
        ]
        table = parse.parse_ps_table((directory / "ps.txt").read_text())
        self.rows: list[parse.RegistryRow] = []
        for item in json.loads((directory / "registry.json").read_text()):
            row = parse.parse_row(json.dumps(item).encode(), file_pid=item["pid"])
            assert isinstance(row, parse.RegistryRow), item["pid"]
            self.rows.append(row)
        self.chains: dict[int, tuple[ProcessRef, ...]] = {
            row.pid: parse.ancestor_chain(table, row.pid) for row in self.rows
        }
        self.cmux_store = hosts.parse_cmux_store((directory / "cmux-sessions.json").read_text())
        self.jetbrains: dict[str, list[JetBrainsProject]] = {}
        names = [p.name.removesuffix(".recentProjects.xml") for p in directory.glob("jetbrains/*")]
        for dirname in sorted(names):
            text = (directory / "jetbrains" / f"{dirname}.recentProjects.xml").read_text()
            self.jetbrains.setdefault(parse_stem(dirname), []).extend(
                hosts.parse_recent_projects(text, self.meta["home"])
            )

    def host_data(self, *, with_cmux_store: bool = True) -> HostData:
        cmux = CmuxData(store=self.cmux_store if with_cmux_store else CmuxStore())
        return HostData(
            home=self.meta["home"],
            cmux=cmux,
            jetbrains={stem: tuple(projects) for stem, projects in self.jetbrains.items()},
        )

    def sessions(self) -> list[SessionInput]:
        return [
            SessionInput(
                session_key=parse.session_key(row.session_id),
                native_id=row.session_id,
                pid=row.pid,
                cwd=row.cwd,
                name=row.name,
                ancestry=self.chains[row.pid],
            )
            for row in self.rows
        ]

    def resolve(self, *, with_cmux_store: bool = True) -> Resolution:
        return resolve(
            self.sessions(),
            self.windows,
            [],
            adapters=hosts.default_adapters(self.host_data(with_cmux_store=with_cmux_store)),
        )

    def placement_of(self, pid: int, resolution: Resolution) -> Placement:
        row = next(row for row in self.rows if row.pid == pid)
        return resolution.sessions[parse.session_key(row.session_id)]


def parse_stem(dirname: str) -> str:
    return hosts.newest_product_dirs([dirname]).popitem()[0]


def _window(item: Mapping[str, Any]) -> WindowRow:
    return WindowRow(
        window_id=str(item["window-id"]),
        source="aerospace",
        workspace=item["workspace"],
        app_bundle_id=item["app-bundle-id"],
        app_name=item["app-name"],
        app_pid=item["app-pid"],
        title=item["window-title"],
        valid_from=OBSERVED,
    )


@pytest.fixture(scope="module")
def capture() -> Capture:
    return Capture(CAPTURE)


def test_the_capture_has_the_rows_the_spike_table_has(capture: Capture) -> None:
    assert [row.pid for row in capture.rows] == [item["pid"] for item in capture.expected]
    assert len(capture.expected) == 14


def test_every_chain_ends_at_a_window_owner(capture: Capture) -> None:
    owners = {w.app_pid for w in capture.windows}
    assert all(any(ref.pid in owners for ref in chain) for chain in capture.chains.values())


@pytest.mark.parametrize("with_cmux_store", [True, False])
def test_resolver_agrees_with_the_spike_table_on_every_row(
    capture: Capture, with_cmux_store: bool
) -> None:
    resolution = capture.resolve(with_cmux_store=with_cmux_store)
    mismatches: list[str] = []
    for expected in capture.expected:
        placement = capture.placement_of(expected["pid"], resolution)
        row = next(row for row in capture.rows if row.pid == expected["pid"])
        got = {
            "workspace": placement.workspace,
            "project": os.path.basename(placement.project_path or ""),
            "state": parse.map_state(row.status),
            "host": placement.host_kind or "",
            "window": None if placement.window_id is None else int(placement.window_id),
        }
        want = {
            "workspace": expected["workspace"],
            "project": expected["project"],
            "state": parse.map_state(expected["state"]),
            "host": expected["host"],
            "window": expected["window_id"],
        }
        if got != want:
            mismatches.append(f"pid {expected['pid']}: got {got}, want {want}")
        assert (placement.unresolved is not None) == (expected["unresolved"] is not None)
    assert mismatches == []


def test_the_unresolved_rows_say_why(capture: Capture) -> None:
    resolution = capture.resolve()
    unresolved = [p for p in resolution.sessions.values() if p.unresolved]
    assert len(unresolved) == 3
    assert {p.host_kind for p in unresolved} == {"jetbrains"}
    assert all("window" in (p.unresolved or "") for p in unresolved)
    assert all(p.via == "none" and p.confidence == "none" for p in unresolved)


def test_cmux_rows_resolve_to_their_cmux_workspace_and_surface(capture: Capture) -> None:
    resolution = capture.resolve()
    cmux = [p for p in resolution.sessions.values() if p.host_kind == "cmux"]
    assert len(cmux) == 8
    assert {p.via for p in cmux} == {"cmux-session-store"}
    assert {p.confidence for p in cmux} == {"high"}
    assert all(p.cmux_workspace_id and p.cmux_surface_id for p in cmux)
    assert len({p.cmux_surface_id for p in cmux}) == 8, "every session has its own surface"
    assert len({p.window_id for p in cmux}) == 1, "the cmux window is shared"


def test_without_the_store_cmux_rows_stay_at_window_level(capture: Capture) -> None:
    resolution = capture.resolve(with_cmux_store=False)
    cmux = [p for p in resolution.sessions.values() if p.host_kind == "cmux"]
    assert {p.via for p in cmux} == {"parent-chain"}
    assert {p.confidence for p in cmux} == {"window-only"}
    assert all(p.cmux_surface_id is None for p in cmux)


def test_projects_are_placed_by_window_count(capture: Capture) -> None:
    resolution = capture.resolve()
    placed = [p for p in resolution.projects.values() if p.workspace is not None]
    assert placed
    assert all(p.via == "window-count" for p in placed)
    workspaces = {w.workspace for w in capture.windows}
    assert {p.workspace for p in placed} <= workspaces
    assert any(p.workspace is None and p.unresolved for p in resolution.projects.values())


def test_every_workspace_with_a_window_gets_a_label(capture: Capture) -> None:
    resolution = capture.resolve()
    assert set(resolution.labels) >= {w.workspace for w in capture.windows}
    assert all(label.text for label in resolution.labels.values())


def test_expected_rows_have_the_documented_shape(capture: Capture) -> None:
    keys: Sequence[str] = ("pid", "state", "project", "host", "window_id", "workspace", "via")
    assert all(set(keys) <= set(item) for item in capture.expected)


# --- the same capture through the daemon and /now (WP1.8's done condition) -----------------


async def _now_of(capture: Capture, *, with_cmux_store: bool = True) -> dict[str, Any]:
    """Feed the capture through a daemon's ingest gate, as its collectors would, and GET /now."""
    clock = FakeClock(OBSERVED)

    async def host_data() -> HostData:
        return capture.host_data(with_cmux_store=with_cmux_store)

    daemon = make_daemon(
        windows_collector(), sessions_collector(), clock=clock, host_loader=host_data
    )
    await daemon.start()
    try:
        windows = [
            Win(
                w.window_id,
                w.workspace or "",
                w.app_name or "",
                w.app_pid or 0,
                w.title or "",
                w.app_bundle_id or "",
            )
            for w in capture.windows
        ]
        daemon.sink("wm").emit(window_snapshot("wm", windows, OBSERVED))
        payloads = [
            parse.session_payload(
                row, process_key=None, predecessor_key=None, ancestry=capture.chains[row.pid]
            )
            for row in capture.rows
        ]
        daemon.sink("agents").emit(session_snapshot("agents", payloads, OBSERVED))
        daemon.sink("agents").emit(
            activity_snapshot(
                "agents",
                {parse.session_key(row.session_id): parse.activity_of(row) for row in capture.rows},
                OBSERVED,
            )
        )
        await daemon.settle()
        async with client(daemon) as http:
            response = await http.get("/now")
        assert response.status_code == 200
        now: dict[str, Any] = response.json()
        return now
    finally:
        await daemon.stop()


@pytest.mark.parametrize("with_cmux_store", [True, False])
async def test_the_spike_table_and_now_agree_on_every_row(
    capture: Capture, with_cmux_store: bool
) -> None:
    now = await _now_of(capture, with_cmux_store=with_cmux_store)
    sessions = {s["native_id"]: s for s in now["sessions"]}
    assert len(sessions) == len(capture.expected) == 14
    mismatches: list[str] = []
    for expected in capture.expected:
        row = next(row for row in capture.rows if row.pid == expected["pid"])
        session = sessions[row.session_id]
        host = session.get("host", {})
        got = {
            "workspace": host.get("workspace"),
            "project": os.path.basename(session.get("project_path", "")),
            "state": session.get("state"),
            "host": host.get("kind", ""),
            "window": None if "window_id" not in host else int(host["window_id"]),
            "unresolved": "unresolved" in host,
            "name": session.get("name"),
            "entrypoint": session.get("entrypoint"),
            "version": session.get("version"),
            "pid": session.get("pid"),
        }
        want = {
            "workspace": expected["workspace"],
            "project": expected["project"],
            "state": parse.map_state(expected["state"]),
            "host": expected["host"],
            "window": expected["window_id"],
            "unresolved": expected["unresolved"] is not None,
            "name": expected["name"],
            "entrypoint": expected["entrypoint"],
            "version": expected["version"],
            "pid": expected["pid"],
        }
        if got != want:
            mismatches.append(f"pid {expected['pid']}: got {got}, want {want}")
    assert mismatches == []


async def test_now_reports_how_each_cmux_session_was_joined(capture: Capture) -> None:
    now = await _now_of(capture)
    cmux = [s for s in now["sessions"] if s.get("host", {}).get("kind") == "cmux"]
    assert len(cmux) == 8
    assert {s["host"]["via"] for s in cmux} == {"cmux-session-store"}
    assert {s["host"]["confidence"] for s in cmux} == {"high"}
    assert len({s["host"]["cmux_surface"] for s in cmux}) == 8
    assert all(s["host"]["ref"].endswith(s["host"]["cmux_surface"]) for s in cmux)
    jetbrains = [s for s in now["sessions"] if s.get("host", {}).get("kind") == "jetbrains"]
    assert {s["host"]["via"] for s in jetbrains if "unresolved" in s["host"]} == {"none"}
    windows = {w["id"] for ws in now["workspaces"] for w in ws["windows"]}
    assert {s["host"]["window_id"] for s in cmux} <= windows
