"""Day replay (WP5.2): a synthetic workday of focus, idle, lock, a crash and a clean shutdown goes
through the span engine and the real writer, and the stored spans and the per-workspace,
per-project and per-app totals are exactly the expected ones
(``tests/fixtures/history/day-replay``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from fylgja import paths
from fylgja.core.spans import History, SpanEngine, recover_stale, summarize
from fylgja.core.writer import Writer
from fylgja.models import (
    ContextItemEvent,
    ContextItemPayload,
    FocusEvent,
    FocusPayload,
    PresenceEvent,
    PresencePayload,
    Record,
)
from fylgja.store import db, queries
from fylgja.testing.fakes import FakeClock

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "history" / "day-replay"


def moment(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


def render(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def record_of(op: dict[str, Any]) -> Record:
    at = moment(op["at"])
    kind = op["op"]
    if kind == "focus":
        return FocusEvent(
            source="wm",
            entity_id="focus",
            observed_at=at,
            payload=FocusPayload(
                window_id=op["window_id"],
                workspace=op.get("workspace"),
                app_bundle_id=op.get("app_bundle_id"),
                title=op.get("title"),
            ),
        )
    if kind == "heartbeat":
        return ContextItemEvent(
            source="ctx",
            entity_id="tick",
            observed_at=at,
            payload=ContextItemPayload(context_kind="tick"),
        )
    signal, _, edge = kind.partition(".")
    states = {"idle": ("idle", "active"), "lock": ("locked", "active")}[signal]
    return PresenceEvent(
        source="pres",
        entity_id=signal,
        observed_at=at,
        payload=PresencePayload(state=states[0] if edge == "start" else states[1]),  # type: ignore[arg-type]
    )


class Daemon:
    """What the day needs of a daemon: a writer and an engine that a restart replaces."""

    def __init__(self, spec: dict[str, Any], writer: Writer) -> None:
        self.spec = spec
        self.writer = writer
        self.engine = self.start()

    def start(self) -> SpanEngine:
        known = self.spec["attribution"]

        def attribute(window_id: str) -> tuple[str | None, str | None]:
            found = known.get(window_id)
            return (None, None) if found is None else (found["project_path"], found["session_key"])

        return SpanEngine(
            self.writer.enqueue,
            attribute,
            idle_threshold_seconds=self.spec["idle_threshold_seconds"],
        )

    async def restart(self, at: datetime) -> None:
        await self.writer.flush()
        self.engine = self.start()
        await self.writer.execute(lambda tx: recover_stale(tx, at))


async def replay(spec: dict[str, Any], writer: Writer) -> None:
    daemon = Daemon(spec, writer)
    for op in spec["ops"]:
        at = moment(op["at"])
        if op["op"] == "crash":
            await daemon.restart(at)  # nothing closed the open span: recovery does
        elif op["op"] == "shutdown":
            daemon.engine.stop(at)
            await daemon.restart(at)
        else:
            record = record_of(op)
            writer.submit(record)
            daemon.engine.observe(record)
    await writer.flush()


def history_of(path: Path, since: datetime, until: datetime, now: datetime) -> History:
    connection = db.connect_reader(path)
    try:
        spans = queries.load_spans(
            connection, since=db.to_db_time(since), until=db.to_db_time(until)
        )
    finally:
        connection.close()
    return History(summarize(spans, since, until, now), ())


def shown(history: History) -> dict[str, Any]:
    summary = history.summary
    spans = []
    for span in summary.spans:
        item: dict[str, Any] = {
            "window_id": span.window_id,
            "workspace": span.workspace,
            "app_bundle_id": span.app_bundle_id,
            "project_path": span.project_path,
            "session_key": span.session_key,
            "start": render(span.started_at),
            "end": render(span.ended_at),
            "end_reason": span.end_reason,
            "open": span.open,
        }
        spans.append({k: v for k, v in item.items() if v is not None})
    return {
        "total_seconds": summary.total_seconds,
        "workspaces": [{"key": t.key, "seconds": t.seconds} for t in summary.workspaces],
        "projects": [{"key": t.key, "seconds": t.seconds} for t in summary.projects],
        "apps": [{"key": t.key, "seconds": t.seconds} for t in summary.apps],
        "spans": spans,
    }


@pytest.fixture
def spec() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((FIXTURE / "events.json").read_text(encoding="utf-8"))
    return loaded


@pytest.fixture
async def replayed(spec: dict[str, Any]) -> Path:
    path = paths.database_file()
    writer = Writer(path, clock=FakeClock(moment(spec["now"])))
    await writer.start()
    try:
        await replay(spec, writer)
    finally:
        await writer.stop()
    return path


async def test_the_day_yields_exactly_the_expected_spans_and_totals(
    spec: dict[str, Any], replayed: Path
) -> None:
    expected = json.loads((FIXTURE / "expected.json").read_text(encoding="utf-8"))
    window = spec["window"]
    history = history_of(
        replayed, moment(window["since"]), moment(window["until"]), moment(spec["now"])
    )
    assert shown(history) == expected


async def test_the_store_holds_no_overlap_no_zero_length_span_and_one_open_span(
    replayed: Path,
) -> None:
    connection = db.connect_reader(replayed)
    try:
        rows = [
            (r["started_at"], r["ended_at"])
            for r in connection.execute("SELECT * FROM focus_spans ORDER BY started_at, id")
        ]
    finally:
        connection.close()
    assert len(rows) == 14  # the 11:41 span of the first window had no length: never stored
    open_rows = [r for r in rows if r[1] is None]
    assert [r[0] for r in open_rows] == [db.to_db_time(moment("2026-10-05T15:40:00Z"))]
    closed = [(s, e) for s, e in rows if e is not None]
    assert all(e > s for s, e in closed)
    for (_, earlier_end), (later_start, _) in zip(rows, rows[1:], strict=False):
        assert earlier_end is not None
        assert earlier_end <= later_start


@pytest.mark.parametrize(
    ("since", "until", "workspaces", "spans"),
    [
        # inside an idle period and its edge: both spans are clipped
        (
            "2026-10-05T09:00:00Z",
            "2026-10-05T10:00:00Z",
            {"ws1": 2400.0},
            [("09:00:00", "09:35:00", True), ("09:55:00", "10:00:00", True)],
        ),
        # a gap, a window-less focus and the span that ended where the day's zero-length one began
        (
            "2026-10-05T11:30:00Z",
            "2026-10-05T11:42:00Z",
            {"ws3": 360.0, "ws2": 300.0},
            [
                ("11:30:00", "11:35:00", False),
                ("11:35:00", "11:40:00", False),
                ("11:41:00", "11:42:00", True),
            ],
        ),
        # a window that ends exactly where a span starts takes nothing of it
        ("2026-10-05T11:00:00Z", "2026-10-05T11:30:00Z", {}, []),
    ],
)
async def test_since_and_until_clip_the_replayed_day(
    spec: dict[str, Any],
    replayed: Path,
    since: str,
    until: str,
    workspaces: dict[str, float],
    spans: list[tuple[str, str, bool]],
) -> None:
    history = history_of(replayed, moment(since), moment(until), moment(spec["now"]))
    summary = history.summary
    assert {t.key: t.seconds for t in summary.workspaces} == workspaces
    assert [
        (s.started_at.strftime("%H:%M:%S"), s.ended_at.strftime("%H:%M:%S"), s.clipped)
        for s in summary.spans
    ] == spans
