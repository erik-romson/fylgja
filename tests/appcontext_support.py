"""Support for the ``appcontext`` collector's tests: a scripted window source, fake inputs,
fixture file builders and a real store with windows and focus."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fylgja.collectors.appcontext import adapter, parse, switches
from fylgja.collectors.appcontext.adapter import (
    AppContextInputs,
    AppleScriptSwitches,
    FocusFacts,
    Visit,
    WindowSnapshot,
)
from fylgja.collectors.appcontext.collector import AppContextCollector, AppContextSettings
from fylgja.collectors.base import CollectorContext
from fylgja.collectors.context import build_context
from fylgja.models import ContextItemEvent
from fylgja.store import db, queries
from fylgja.testing.fakes import FakeClock, FakeFS, FakeProcessRunner, RecordingSink

HOME = Path("/home/erik")
T0 = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)

CMUX = "com.cmuxterm.app"
CHROME = "com.google.Chrome"
PYCHARM = "com.jetbrains.pycharm"
OBSIDIAN = "md.obsidian"
ITERM2 = "com.googlecode.iterm2"
SLACK = "com.tinyspeck.slackmacgap"


def window(window_id: str, bundle: str | None, title: str | None) -> parse.WindowFacts:
    return parse.WindowFacts(window_id=window_id, bundle_id=bundle, title=title, workspace="1")


class FakeSource:
    """A :class:`WindowSource` whose snapshot the test sets."""

    def __init__(self) -> None:
        self.windows: list[parse.WindowFacts] = []
        self.focus_window: str | None = None
        self.focus_at: datetime = T0
        self.error: adapter.SourceError | None = None
        self.reads = 0

    def focus_on(self, window_id: str | None, at: datetime | None = None) -> None:
        self.focus_window = window_id
        self.focus_at = self.focus_at + timedelta(seconds=1) if at is None else at

    def read(self) -> WindowSnapshot:
        self.reads += 1
        if self.error is not None:
            raise self.error
        focus = (
            None
            if self.focus_window is None
            else FocusFacts(self.focus_window, self.focus_at, None, None)
        )
        return WindowSnapshot(tuple(self.windows), focus)


class FakeHistory:
    def __init__(self) -> None:
        self.visits: list[Visit] = []
        self.calls: list[tuple[str, datetime, int]] = []
        self.error: adapter.InputError | None = None

    def recent(self, profile_dir: str, since: datetime, limit: int) -> list[Visit]:
        self.calls.append((profile_dir, since, limit))
        if self.error is not None:
            raise self.error
        return self.visits[:limit]


class World:
    """A collector with every seam faked."""

    def __init__(
        self,
        *,
        chrome: bool = False,
        iterm2: bool = False,
        home: Path = HOME,
        **settings: Any,
    ) -> None:
        self.clock = FakeClock(T0)
        self.sink = RecordingSink()
        self.source = FakeSource()
        self.runner = FakeProcessRunner()
        self.fs = FakeFS()
        self.history = FakeHistory()
        self.home = home
        self.inputs = AppContextInputs(
            source=self.source,
            runner=self.runner,
            fs=self.fs,
            home=home,
            history=self.history,
            switches=AppleScriptSwitches(chrome=chrome, iterm2=iterm2),
        )
        context: CollectorContext = build_context("appcontext", self.sink, clock=self.clock)
        self.collector = AppContextCollector(
            AppContextSettings(**settings), context, inputs=self.inputs
        )

    @property
    def events(self) -> list[ContextItemEvent]:
        return [r for r in self.sink.records if isinstance(r, ContextItemEvent)]

    def items(self, key: str | None = None) -> list[ContextItemEvent]:
        found = [e for e in self.events if not e.payload.data.get("gone")]
        return found if key is None else [e for e in found if e.payload.data.get("key") == key]

    def gone(self, key: str) -> list[ContextItemEvent]:
        return [
            e for e in self.events if e.payload.data.get("gone") and e.payload.data["key"] == key
        ]

    def fields(self, key: str) -> dict[str, Any]:
        last = self.items(key)[-1]
        fields = last.payload.data["fields"]
        assert isinstance(fields, dict)
        return fields

    @property
    def osascript_calls(self) -> list[tuple[str, ...]]:
        return [c for c in self.runner.calls if c and c[0] == switches.OSASCRIPT]


# --- fixture files ------------------------------------------------------------------------------


def cmux_store(workspaces: dict[str, tuple[str, str, float]], *, version: int = 1) -> str:
    """cmux's session store: ``{workspace id: (surface id, cwd, updatedAt)}``, one session each."""
    sessions: dict[str, Any] = {}
    active: dict[str, Any] = {}
    for index, (workspace, (surface, cwd, updated)) in enumerate(sorted(workspaces.items())):
        session_id = f"session-{index}"
        sessions[session_id] = {
            "sessionId": session_id,
            "workspaceId": workspace,
            "surfaceId": surface,
            "cwd": cwd,
            "agentLifecycle": "running",
            "updatedAt": updated,
            "launchCommand": {"arguments": ["--secret-flag", "hunter2"]},
            "transcriptPath": "/tmp/never-read.jsonl",
        }
        active[workspace] = {"sessionId": session_id, "updatedAt": updated}
    return json.dumps(
        {
            "version": version,
            "sessions": sessions,
            "activeSessionsByWorkspace": active,
            "activeSessionsBySurface": {},
        }
    )


def recent_projects(entries: list[tuple[str, str | None, int]]) -> str:
    """``recentProjects.xml``: ``(path under $USER_HOME$, frame title, open timestamp)``."""
    body = ""
    for path, title, opened in entries:
        meta = f' frameTitle="{title}"' if title else ""
        body += (
            f'<entry key="$USER_HOME$/{path}"><value><RecentProjectMetaInfo{meta} '
            f'opened="true"><option name="projectOpenTimestamp" value="{opened}"/>'
            "</RecentProjectMetaInfo></value></entry>"
        )
    return (
        '<application><component name="RecentProjectsManager"><option name="additionalInfo">'
        f"<map>{body}</map></option></component></application>"
    )


def obsidian_config(vaults: dict[str, tuple[str, int]]) -> str:
    return json.dumps(
        {
            "vaults": {vid: {"path": path, "ts": ts} for vid, (path, ts) in vaults.items()},
            "frame": {"x": 1},
        }
    )


def chrome_local_state(profiles: dict[str, tuple[str, str]]) -> str:
    """``{directory: (name, user_name)}``."""
    cache = {
        directory: {"name": name, "user_name": user, "gaia_name": f"gaia-{directory}"}
        for directory, (name, user) in profiles.items()
    }
    return json.dumps({"profile": {"info_cache": cache}})


APPLE_SUPPORT = HOME / "Library" / "Application Support"
CMUX_STORE = HOME / ".cmuxterm" / "claude-hook-sessions.json"
OBSIDIAN_JSON = APPLE_SUPPORT / "obsidian" / "obsidian.json"
CHROME_LOCAL_STATE = APPLE_SUPPORT / "Google" / "Chrome" / "Local State"


def jetbrains_xml_path(product: str) -> Path:
    return APPLE_SUPPORT / "JetBrains" / product / "options" / "recentProjects.xml"


def chrome_history_db(path: Path, rows: list[tuple[str, str, datetime]]) -> None:
    """A Chrome ``History`` database with a ``urls`` table (real SQLite, built under ``path``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT, "
            "last_visit_time INTEGER)"
        )
        for url, title, at in rows:
            connection.execute(
                "INSERT INTO urls (url, title, last_visit_time) VALUES (?, ?, ?)",
                (url, title, parse.chrome_time_from_unix(at)),
            )
        connection.commit()
    finally:
        connection.close()


# --- a real store -------------------------------------------------------------------------------


def store_with_windows(
    path: Path, windows: list[tuple[str, str | None, str | None]], focus: str | None
) -> Path:
    """A real store with the open ``(id, bundle id, title)`` windows and a focus event."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = db.open_store(path)
    try:
        connection.execute("BEGIN")
        for window_id, bundle, title in windows:
            queries.put_window(
                connection,
                source="aerospace",
                window_id=window_id,
                values={"workspace": "1", "app_bundle_id": bundle, "title": title},
                at="2026-01-01T00:00:00.000000Z",
            )
        if focus is not None:
            queries.insert_event(
                connection,
                dedup_key=f"focus-{focus}",
                source="aerospace",
                record_type="event",
                entity_kind="focus",
                entity_id="focus",
                observed_at="2026-01-01T00:00:01.000000Z",
                received_at="2026-01-01T00:00:01.000000Z",
                payload_json=json.dumps(
                    {"window_id": focus, "workspace": "1", "app_bundle_id": None, "title": None}
                ),
            )
        connection.execute("COMMIT")
    finally:
        connection.close()
    return path
