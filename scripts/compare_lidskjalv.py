"""Compare Fylgja's session state transitions with Lidskjalv's cache (WP2.7, dual-run gate).

Dev-only and run by hand during the Phase 2 dual run; neither ``src/fylgja`` nor ``tests/`` imports
it (A21)::

    uv run --frozen python scripts/compare_lidskjalv.py --since 24h [--until now] [--json]
    uv run --frozen python scripts/compare_lidskjalv.py --self-check

What it reads, and only reads:

* Fylgja's database, opened ``mode=ro`` (default ``$FYLGJA_DATA_DIR/fylgja.db``, else
  ``~/.local/share/fylgja/fylgja.db``): ``session_snapshots`` joined with ``sessions`` for the
  transitions, and a count of ``hook_event`` rows in ``events``.
* Lidskjalv's ``sessions/<sid>.json`` (``state``, ``since``) and ``lanes/*/lane.json``
  (``session_id``, ``ended_at``) under ``--lidskjalv-dir`` (default ``~/.cache/lidskjalv``), or
  the two directories given with ``--sessions-dir`` and ``--lanes-dir``.

Lidskjalv keeps one record per session, so it holds only the *latest* transition of a session
(``state`` entered at ``since``). Each record whose ``since`` lies in the window is checked
against Fylgja's transitions of the same session: a transition into the same state within
``--tolerance`` seconds is a match. Earlier Fylgja transitions have no Lidskjalv counterpart and
are reported as ``not_comparable``. A lane that ended in the window is checked against the end
of the Fylgja session (``--lane-tolerance``, wider because Lidskjalv notices a dead process only
at its next sweep).

States of both sides are put on Lidskjalv's vocabulary first (``docs/api/lidskjalv-mapping.md``,
"State vocabulary"): ``running``, ``blocked``, ``failed`` and ``idle``. ``ended`` and
``cancelled`` are not live states and are not compared as transitions.

Mismatch reasons: ``missing_in_fylgja``, ``missing_in_lidskjalv``, ``state_differs`` and
``timing_outside_tolerance``. The gate is a match rate of at least 99 %: exit status 0 when it
holds, 1 when it does not (or nothing could be compared), 2 for unusable input.

Never prints titles, prompts or paths: only session ids, states and times.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

GATE_PERCENT = 99.0
DEFAULT_TOLERANCE = 5.0
DEFAULT_LANE_TOLERANCE = 60.0

MATCH = "match"
MISSING_IN_FYLGJA = "missing_in_fylgja"
MISSING_IN_LIDSKJALV = "missing_in_lidskjalv"
STATE_DIFFERS = "state_differs"
TIMING = "timing_outside_tolerance"
REASONS = (MISSING_IN_FYLGJA, MISSING_IN_LIDSKJALV, STATE_DIFFERS, TIMING)

# Both sides are mapped onto Lidskjalv's record vocabulary. ``None`` means "not a live state".
_STATE_MAP: dict[str, str | None] = {
    "busy": "running",
    "working": "running",
    "running_tool": "running",
    "running": "running",
    "compacting": "running",
    "waiting": "blocked",
    "waiting_permission": "blocked",
    "waiting_input": "blocked",
    "blocked": "blocked",
    "idle": "idle",
    "turn_done": "idle",
    "seen": "idle",
    "passed": "idle",
    "paused": "idle",
    "stale": "idle",
    "unknown": "idle",
    "error": "failed",
    "failed": "failed",
    "ended": None,
    "cancelled": None,
}


def normalize_state(state: str | None) -> str | None:
    """A state on Lidskjalv's vocabulary, or ``None`` for an ended or unusable state."""
    if state is None:
        return None
    return _STATE_MAP.get(state, "idle")


# --------------------------------------------------------------------------------------------
# Data shapes (what the loaders produce and the comparison consumes)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Transition:
    """Session ``session_id`` entered ``state`` (raw, as stored) at epoch second ``at``."""

    at: float
    state: str


@dataclass(frozen=True)
class FylgjaData:
    transitions: Mapping[str, Sequence[Transition]]  # session id -> transitions, oldest first
    ended_at: Mapping[str, float | None]  # session id -> end time, ``None`` while live
    hook_events: int = 0


@dataclass(frozen=True)
class LidskjalvSession:
    state: str
    since: float


@dataclass(frozen=True)
class LidskjalvLane:
    session_id: str
    ended_at: float | None


@dataclass(frozen=True)
class LidskjalvData:
    sessions: Mapping[str, LidskjalvSession]
    lanes: Mapping[str, LidskjalvLane] = field(default_factory=dict)


@dataclass(frozen=True)
class Result:
    kind: str  # "transition" or "lane_end"
    session_id: str
    verdict: str  # MATCH or one of REASONS
    lidskjalv: str | None = None
    fylgja: str | None = None
    delta_seconds: float | None = None


@dataclass
class Report:
    since: float
    until: float
    tolerance: float
    lane_tolerance: float
    results: list[Result] = field(default_factory=list)
    not_comparable: int = 0
    out_of_window: int = 0
    hook_events: int = 0

    @property
    def compared(self) -> int:
        return len(self.results)

    @property
    def matched(self) -> int:
        return sum(1 for r in self.results if r.verdict == MATCH)

    @property
    def mismatches(self) -> list[Result]:
        return [r for r in self.results if r.verdict != MATCH]

    @property
    def percent(self) -> float | None:
        return None if not self.results else 100.0 * self.matched / len(self.results)

    @property
    def gate_passed(self) -> bool:
        percent = self.percent
        return percent is not None and percent >= GATE_PERCENT

    def by_reason(self) -> dict[str, int]:
        counts = Counter(r.verdict for r in self.mismatches)
        return {reason: counts[reason] for reason in REASONS if counts[reason]}


# --------------------------------------------------------------------------------------------
# The comparison (pure)
# --------------------------------------------------------------------------------------------


def _live_timeline(transitions: Sequence[Transition]) -> list[tuple[float, str]]:
    timeline = []
    for item in transitions:
        state = normalize_state(item.state)
        if state is not None:
            timeline.append((item.at, state))
    return timeline


def _state_at(
    timeline: Sequence[tuple[float, str]], moment: float, slack: float
) -> tuple[float, str]:
    """The transition in force at ``moment`` (one up to ``slack`` later still counts).

    ``timeline`` must not be empty."""
    current = timeline[0]
    for entry in timeline:
        if entry[0] <= moment + slack:
            current = entry
        else:
            break
    return current


def _compare_transitions(fylgja: FylgjaData, lid: LidskjalvData, report: Report) -> None:
    window = (report.since, report.until)
    seen_in_lidskjalv = set(lid.sessions)
    anchors: dict[str, float] = {}
    for sid, record in sorted(lid.sessions.items()):
        wanted = normalize_state(record.state)
        if wanted is None:
            continue
        if not window[0] <= record.since <= window[1]:
            report.out_of_window += 1
            continue
        timeline = _live_timeline(fylgja.transitions.get(sid, ()))
        if not timeline:
            report.results.append(Result("transition", sid, MISSING_IN_FYLGJA, lidskjalv=wanted))
            continue
        same = [(abs(at - record.since), at) for at, state in timeline if state == wanted]
        if same:
            delta, at = min(same)
            anchors[sid] = at
            verdict = MATCH if delta <= report.tolerance else TIMING
            report.results.append(
                Result(
                    "transition", sid, verdict, lidskjalv=wanted, fylgja=wanted, delta_seconds=delta
                )
            )
        else:
            at, state = _state_at(timeline, record.since, report.tolerance)
            anchors[sid] = at
            report.results.append(
                Result("transition", sid, STATE_DIFFERS, lidskjalv=wanted, fylgja=state)
            )
    for sid, items in sorted(fylgja.transitions.items()):
        timeline = _live_timeline(items)
        in_window = [(at, state) for at, state in timeline if window[0] <= at <= window[1]]
        if not in_window:
            continue
        if sid in seen_in_lidskjalv:
            report.not_comparable += sum(1 for at, _ in in_window if at != anchors.get(sid))
            continue
        ended = fylgja.ended_at.get(sid)
        if ended is not None and ended <= window[1]:
            # Lidskjalv sweeps the records of ended sessions, so a missing file is expected.
            report.not_comparable += len(in_window)
            continue
        report.results.append(
            Result("transition", sid, MISSING_IN_LIDSKJALV, fylgja=in_window[-1][1])
        )


def _compare_lanes(fylgja: FylgjaData, lid: LidskjalvData, report: Report) -> None:
    for lane_id, lane in sorted(lid.lanes.items()):
        if lane.ended_at is None:
            continue
        if not report.since <= lane.ended_at <= report.until:
            report.out_of_window += 1
            continue
        sid = lane.session_id
        if sid not in fylgja.transitions and sid not in fylgja.ended_at:
            report.results.append(Result("lane_end", sid or lane_id, MISSING_IN_FYLGJA))
            continue
        ended = fylgja.ended_at.get(sid)
        if ended is None:
            report.results.append(
                Result("lane_end", sid, STATE_DIFFERS, lidskjalv="ended", fylgja="live")
            )
            continue
        delta = abs(ended - lane.ended_at)
        verdict = MATCH if delta <= report.lane_tolerance else TIMING
        report.results.append(
            Result("lane_end", sid, verdict, lidskjalv="ended", fylgja="ended", delta_seconds=delta)
        )


def compare(
    fylgja: FylgjaData,
    lid: LidskjalvData,
    *,
    since: float,
    until: float,
    tolerance: float = DEFAULT_TOLERANCE,
    lane_tolerance: float = DEFAULT_LANE_TOLERANCE,
) -> Report:
    """Compare the two sides over ``[since, until]`` (epoch seconds)."""
    report = Report(
        since=since,
        until=until,
        tolerance=tolerance,
        lane_tolerance=lane_tolerance,
        hook_events=fylgja.hook_events,
    )
    _compare_transitions(fylgja, lid, report)
    _compare_lanes(fylgja, lid, report)
    return report


# --------------------------------------------------------------------------------------------
# Loaders (read-only I/O)
# --------------------------------------------------------------------------------------------

_TIME_FORMATS = ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ")


def db_time_to_epoch(value: str) -> float:
    """Epoch seconds of a stored Fylgja time (``YYYY-MM-DDTHH:MM:SS.ffffffZ``, UTC)."""
    for pattern in _TIME_FORMATS:
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=UTC).timestamp()
        except ValueError:
            continue
    raise ValueError(f"unrecognised stored time {value!r}")


def epoch_to_db_time(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).strftime(_TIME_FORMATS[0])


def default_db_path() -> Path:
    override = os.environ.get("FYLGJA_DATA_DIR")
    base = Path(override).expanduser() if override else Path.home() / ".local/share/fylgja"
    return base / "fylgja.db"


def load_fylgja(db_path: Path, since: float, until: float) -> FylgjaData:
    """Read transitions of Claude sessions around the window from ``db_path`` (``mode=ro``)."""
    uri = f"file:{db_path}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        connection.execute("PRAGMA query_only = ON")
        rows = connection.execute(
            "SELECT s.native_id, s.ended_at, ss.valid_from, ss.state "
            "FROM session_snapshots ss JOIN sessions s ON s.session_key = ss.session_key "
            "WHERE s.kind = 'claude' AND ss.state IS NOT NULL AND ss.valid_from <= ? "
            "AND (ss.valid_to IS NULL OR ss.valid_to >= ?) "
            "ORDER BY s.native_id, ss.valid_from",
            (epoch_to_db_time(until), epoch_to_db_time(since)),
        ).fetchall()
        ended_rows = connection.execute(
            "SELECT native_id, ended_at FROM sessions WHERE kind = 'claude'"
        ).fetchall()
        hook_events = connection.execute(
            "SELECT count(*) FROM events WHERE entity_kind = 'hook_event' "
            "AND observed_at >= ? AND observed_at <= ?",
            (epoch_to_db_time(since), epoch_to_db_time(until)),
        ).fetchone()[0]
    finally:
        connection.close()
    transitions: dict[str, list[Transition]] = {}
    for native_id, _ended, valid_from, state in rows:
        transitions.setdefault(str(native_id), []).append(
            Transition(db_time_to_epoch(valid_from), str(state))
        )
    ended_at = {
        str(native_id): (None if ended is None else db_time_to_epoch(ended))
        for native_id, ended in ended_rows
    }
    return FylgjaData(transitions, ended_at, int(hook_events))


_SESSION_FILE = re.compile(r"[0-9A-Za-z_-]+\.json")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def load_lidskjalv(sessions_dir: Path, lanes_dir: Path) -> LidskjalvData:
    """Read Lidskjalv's session records and lane files; unreadable files are skipped."""
    sessions: dict[str, LidskjalvSession] = {}
    if sessions_dir.is_dir():
        for path in sorted(sessions_dir.iterdir()):
            if _SESSION_FILE.fullmatch(path.name) is None:
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict):
                continue
            sid = record.get("session_id") or path.stem
            since = _number(record.get("since"))
            if since is None:
                since = _number(record.get("updated"))
            state = record.get("state") or "idle"
            if since is not None and isinstance(sid, str) and isinstance(state, str):
                sessions[sid] = LidskjalvSession(state, since)
    lanes: dict[str, LidskjalvLane] = {}
    if lanes_dir.is_dir():
        for lane_json in sorted(lanes_dir.glob("*/lane.json")):
            try:
                record = json.loads(lane_json.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict):
                continue
            session_id = record.get("session_id")
            lanes[lane_json.parent.name] = LidskjalvLane(
                session_id if isinstance(session_id, str) else "",
                _number(record.get("ended_at")),
            )
    return LidskjalvData(sessions, lanes)


# --------------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------------

_RELATIVE = re.compile(r"(\d+(?:\.\d+)?)([smhdw])")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_moment(text: str, now: datetime) -> float:
    """Epoch seconds of ``now``, an ISO-8601 time, epoch seconds, or a span ago (``24h``)."""
    text = text.strip()
    if text == "now":
        return now.timestamp()
    relative = _RELATIVE.fullmatch(text)
    if relative:
        return (now - timedelta(seconds=float(relative[1]) * _UNITS[relative[2]])).timestamp()
    try:
        return float(text)
    except ValueError:
        pass
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.timestamp()


def format_report(report: Report) -> str:
    def stamp(value: float) -> str:
        return epoch_to_db_time(value)

    percent = report.percent
    lines = [
        f"window     {stamp(report.since)} .. {stamp(report.until)}",
        f"compared   {report.compared} (matched {report.matched}, "
        f"mismatched {len(report.mismatches)})",
        "match      " + ("n/a" if percent is None else f"{percent:.2f} %"),
        f"gate       >= {GATE_PERCENT:g} %: " + ("PASS" if report.gate_passed else "FAIL"),
        f"not comparable {report.not_comparable}, outside window {report.out_of_window}, "
        f"fylgja hook events {report.hook_events}",
    ]
    for reason, count in report.by_reason().items():
        lines.append(f"  {reason}: {count}")
    for item in report.mismatches:
        detail = f"lidskjalv={item.lidskjalv or '-'} fylgja={item.fylgja or '-'}"
        if item.delta_seconds is not None:
            detail += f" delta={item.delta_seconds:.1f}s"
        lines.append(f"  {item.verdict:<26} {item.kind:<10} {item.session_id}  {detail}")
    return "\n".join(lines)


def report_to_json(report: Report) -> dict[str, Any]:
    return {
        "since": epoch_to_db_time(report.since),
        "until": epoch_to_db_time(report.until),
        "tolerance_seconds": report.tolerance,
        "lane_tolerance_seconds": report.lane_tolerance,
        "compared": report.compared,
        "matched": report.matched,
        "mismatched": len(report.mismatches),
        "match_percent": report.percent,
        "gate_percent": GATE_PERCENT,
        "gate_passed": report.gate_passed,
        "not_comparable": report.not_comparable,
        "out_of_window": report.out_of_window,
        "fylgja_hook_events": report.hook_events,
        "mismatches_by_reason": report.by_reason(),
        "mismatches": [asdict(item) for item in report.mismatches],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="compare_lidskjalv.py",
        description="Compare Fylgja's session state transitions with Lidskjalv's cache "
        "(Phase 2 dual-run gate: at least 99 % must match).",
    )
    parser.add_argument(
        "--since", default="24h", help="window start: ISO-8601, epoch seconds or a span ago (24h)"
    )
    parser.add_argument("--until", default="now", help="window end (same forms; default now)")
    parser.add_argument("--db", type=Path, help="Fylgja database (default: the data dir's)")
    parser.add_argument(
        "--lidskjalv-dir",
        type=Path,
        default=Path.home() / ".cache" / "lidskjalv",
        help="Lidskjalv cache directory (default ~/.cache/lidskjalv)",
    )
    parser.add_argument("--sessions-dir", type=Path, help="override <lidskjalv-dir>/sessions")
    parser.add_argument("--lanes-dir", type=Path, help="override <lidskjalv-dir>/lanes")
    parser.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_TOLERANCE,
        help=f"seconds two transitions may differ by (default {DEFAULT_TOLERANCE:g})",
    )
    parser.add_argument(
        "--lane-tolerance",
        type=float,
        default=DEFAULT_LANE_TOLERANCE,
        help=f"seconds a lane end may differ by (default {DEFAULT_LANE_TOLERANCE:g})",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="run the comparison on synthetic in-memory inputs and exit (reads nothing)",
    )
    return parser


# --------------------------------------------------------------------------------------------
# Self-check (synthetic, in-memory, no disk)
# --------------------------------------------------------------------------------------------


def self_check() -> list[str]:
    """Return a list of failures; empty when the comparison logic reports correctly."""
    failures: list[str] = []

    def expect(label: str, actual: object, wanted: object) -> None:
        if actual != wanted:
            failures.append(f"{label}: got {actual!r}, wanted {wanted!r}")

    base = 1_000_000.0
    fylgja = FylgjaData(
        transitions={
            # Lidskjalv's single record matches the last transition (idle after turn_done).
            "s-match": [Transition(base + 10, "working"), Transition(base + 100, "turn_done")],
            # Same state, 30 s apart: timing.
            "s-late": [Transition(base + 130, "waiting_permission")],
            # Different state.
            "s-state": [Transition(base + 50, "working")],
            # Fylgja only, still live: missing in Lidskjalv.
            "s-fylgja-only": [Transition(base + 60, "working")],
            # Fylgja only, ended: expected, not counted.
            "s-fylgja-ended": [Transition(base + 70, "working"), Transition(base + 90, "ended")],
            # A Lidskjalv record before the window: ignored.
            "s-old": [Transition(base - 5000, "idle")],
            # Lidskjalv record uses a vocabulary alias: working == running.
            "s-alias": [Transition(base + 200, "running_tool")],
            # Lane ended close to Fylgja's end.
            "s-lane-ok": [Transition(base + 5, "working"), Transition(base + 300, "ended")],
            "s-lane-late": [Transition(base + 5, "working"), Transition(base + 300, "ended")],
            "s-lane-live": [Transition(base + 5, "working")],
        },
        ended_at={
            "s-fylgja-ended": base + 90,
            "s-lane-ok": base + 300,
            "s-lane-late": base + 300,
            "s-lane-live": None,
        },
        hook_events=3,
    )
    lid = LidskjalvData(
        sessions={
            "s-match": LidskjalvSession("idle", base + 102),
            "s-late": LidskjalvSession("blocked", base + 100),
            "s-state": LidskjalvSession("idle", base + 52),
            "s-missing": LidskjalvSession("running", base + 20),
            "s-old": LidskjalvSession("idle", base - 5000),
            "s-alias": LidskjalvSession("running", base + 201),
            "s-ended-record": LidskjalvSession("ended", base + 10),
        },
        lanes={
            "pid-1": LidskjalvLane("s-lane-ok", base + 330),
            "pid-2": LidskjalvLane("s-lane-late", base + 500),
            "pid-3": LidskjalvLane("s-lane-live", base + 400),
            "pid-4": LidskjalvLane("s-lane-ghost", base + 400),
            "pid-5": LidskjalvLane("s-lane-ok", None),
        },
    )
    report = compare(fylgja, lid, since=base, until=base + 1000)
    verdicts = {(r.kind, r.session_id): r.verdict for r in report.results}
    expect(
        "verdicts",
        verdicts,
        {
            ("transition", "s-match"): MATCH,
            ("transition", "s-late"): TIMING,
            ("transition", "s-state"): STATE_DIFFERS,
            ("transition", "s-missing"): MISSING_IN_FYLGJA,
            ("transition", "s-alias"): MATCH,
            ("transition", "s-fylgja-only"): MISSING_IN_LIDSKJALV,
            ("transition", "s-lane-live"): MISSING_IN_LIDSKJALV,
            ("lane_end", "s-lane-ok"): MATCH,
            ("lane_end", "s-lane-late"): TIMING,
            ("lane_end", "s-lane-live"): STATE_DIFFERS,
            ("lane_end", "s-lane-ghost"): MISSING_IN_FYLGJA,
        },
    )
    expect("compared", report.compared, 11)
    expect("matched", report.matched, 3)
    expect(
        "by_reason",
        report.by_reason(),
        {MISSING_IN_FYLGJA: 2, MISSING_IN_LIDSKJALV: 2, STATE_DIFFERS: 2, TIMING: 2},
    )
    percent = report.percent
    expect("percent", None if percent is None else round(percent, 4), round(300 / 11, 4))
    expect("gate fails", report.gate_passed, False)
    # The earlier transition of s-match, the working of the ended s-fylgja-ended, and the opening
    # transition of each ended lane session.
    expect("not comparable", report.not_comparable, 4)
    expect("out of window", report.out_of_window, 1)
    expect("hook events", report.hook_events, 3)
    differs = next(r for r in report.results if r.session_id == "s-state")
    expect("state differs detail", (differs.lidskjalv, differs.fylgja), ("idle", "running"))
    late = next(r for r in report.results if r.session_id == "s-late")
    expect("timing delta", late.delta_seconds, 30.0)

    # A clean pair passes the gate; a wider tolerance turns a timing mismatch into a match.
    clean = compare(
        FylgjaData({"a": [Transition(base + 1, "working")]}, {"a": None}),
        LidskjalvData({"a": LidskjalvSession("running", base + 3)}),
        since=base,
        until=base + 10,
    )
    expect("clean percent", clean.percent, 100.0)
    expect("clean gate", clean.gate_passed, True)
    wide = compare(fylgja, lid, since=base, until=base + 1000, tolerance=60.0, lane_tolerance=300.0)
    expect("wide tolerance matches", wide.matched, 5)
    expect(
        "nothing compared",
        compare(FylgjaData({}, {}), LidskjalvData({}), since=base, until=base + 1).gate_passed,
        False,
    )

    # Exactly 99 % passes, just under fails.
    ids = [f"p{i}" for i in range(100)]
    pairs = FylgjaData({i: [Transition(base, "idle")] for i in ids[:99]}, {})
    records = LidskjalvData({i: LidskjalvSession("idle", base) for i in ids})
    edge = compare(pairs, records, since=base - 1, until=base + 1)
    expect("99 % edge", (edge.compared, edge.matched, edge.gate_passed), (100, 99, True))
    two = compare(
        FylgjaData({i: [Transition(base, "idle")] for i in ids[:98]}, {}),
        records,
        since=base - 1,
        until=base + 1,
    )
    expect("98 % fails", two.gate_passed, False)

    # Helpers.
    now = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    expect("relative", parse_moment("2h", now), now.timestamp() - 7200)
    expect("now", parse_moment("now", now), now.timestamp())
    expect("iso", parse_moment("2026-10-06T10:00:00Z", now), now.timestamp() - 7200)
    expect("epoch", parse_moment("1000000", now), 1_000_000.0)
    expect(
        "db time round trip",
        db_time_to_epoch(epoch_to_db_time(1_234_567.25)),
        1_234_567.25,
    )
    expect("normalize ended", normalize_state("ended"), None)
    expect("normalize alias", normalize_state("waiting_input"), "blocked")
    expect("normalize unknown vocabulary", normalize_state("brand_new"), "idle")
    payload = json.dumps(report_to_json(report))
    expect("json round trip", json.loads(payload)["compared"], 11)
    return failures


# --------------------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_check:
        failures = self_check()
        for failure in failures:
            print(f"self-check FAILED: {failure}", file=sys.stderr)
        if not failures:
            print("self-check ok")
        return 1 if failures else 0

    now = datetime.now(UTC)
    try:
        since = parse_moment(args.since, now)
        until = parse_moment(args.until, now)
    except ValueError as exc:
        print(f"compare_lidskjalv: bad time: {exc}", file=sys.stderr)
        return 2
    if since >= until:
        print("compare_lidskjalv: --since must be before --until", file=sys.stderr)
        return 2
    db_path = args.db or default_db_path()
    if not db_path.is_file():
        print(f"compare_lidskjalv: no Fylgja database at {db_path}", file=sys.stderr)
        return 2
    sessions_dir = args.sessions_dir or args.lidskjalv_dir / "sessions"
    lanes_dir = args.lanes_dir or args.lidskjalv_dir / "lanes"
    try:
        fylgja = load_fylgja(db_path, since, until)
    except (sqlite3.Error, ValueError) as exc:
        print(f"compare_lidskjalv: cannot read the Fylgja database: {exc}", file=sys.stderr)
        return 2
    report = compare(
        fylgja,
        load_lidskjalv(sessions_dir, lanes_dir),
        since=since,
        until=until,
        tolerance=args.tolerance,
        lane_tolerance=args.lane_tolerance,
    )
    if args.json:
        print(json.dumps(report_to_json(report), indent=2))
    else:
        print(format_report(report))
    return 0 if report.gate_passed else 1


if __name__ == "__main__":
    sys.exit(main())
