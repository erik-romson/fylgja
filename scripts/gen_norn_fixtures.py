#!/usr/bin/env python3
"""Stage the norn collector's fixtures (``PLAN-final-plan.md`` WP3.3(b)). Dev-only; not packaged.

Two kinds of fixture, both under ``tests/fixtures/norn/``:

``files/`` (staged under ``target/fixture-staging/norn/files/``, then sanitized):
    norn's own run files, reduced to the keys the collector reads. Sources (read-only):

    * ``plan_with_review.*`` in this repository's root: a finished run that drove three agent
      sessions. Three variants are written: ``finished`` (the whole run), ``crashed`` (the history
      cut after the 14th line, ``run.json`` without finish metadata, ``events.jsonl`` ending in a
      stage that never finished) and ``waiting`` (cut at the first ``WaitingInput``);
    * ``implement_features_v2.*`` of the *norn checkout* (never this repository's running copy): a
      finished, failed run (history id 14);
    * ``stub.*`` of the norn checkout: a finished, passed run (history id 180).

    Prompts, outputs, errors and parameters never reach a fixture: ``params`` values, ``error``
    texts, ``prompt_excerpt`` and ``summary`` become placeholders, ``CommandOutput`` events are
    dropped and a ``TurnEvent`` keeps only its session id. Then run, by hand::

        uv run --frozen python scripts/sanitize_fixture.py norn/files

``inbox/`` (synthetic, written straight into ``tests/fixtures/norn/inbox/``):
    ``fylgja.hook/1`` envelopes with ``source`` ``norn`` as norn's event subscriber writes them
    (``docs/collectors/norn.md``): ``lifecycle`` (RunStarted to RunFinished, with a wait and a
    heartbeat), ``failed`` and ``cancelled``.

Usage::

    uv run --frozen python scripts/gen_norn_fixtures.py [--norn ~/projects/erik.romson/norn/main]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
STAGING = REPO / "target" / "fixture-staging" / "norn" / "files"
INBOX = REPO / "tests" / "fixtures" / "norn" / "inbox"

KEEP_EVENTS = {
    "StageStarted",
    "StageFinished",
    "CallingAgent",
    "GotReply",
    "WaitingInput",
    "LoopAttempt",
    "LoopSuccess",
    "LoopExhausted",
    "RunFinished",
    "RunError",
    "UsageUpdated",
    "RunPaused",
    "RunCancelled",
}


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_ALIASES: dict[str, str] = {}


def alias_ids(text: str) -> str:
    """Replace every uuid (session ids, norn's run uuid) by a numbered placeholder."""

    def alias(match: re.Match[str]) -> str:
        found = match.group(0)
        if found not in _ALIASES:
            _ALIASES[found] = f"00000000-0000-4000-8000-{0xB00000 + len(_ALIASES):012x}"
        return _ALIASES[found]

    return _UUID.sub(alias, text)


def reduce_history_line(line: str) -> str:
    data = json.loads(line)
    for stage in data.get("stage_log", []):
        if stage.get("error"):
            stage["error"] = "<error>"
    return alias_ids(json.dumps(data, separators=(", ", ": ")))


def reduce_header(path: Path, *, unfinished: bool) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    data["params"] = {key: "<param>" for key in data.get("params", {})}
    if unfinished:
        for key in ("finished_at", "success", "failed_stage", "duration_ms", "totals"):
            data.pop(key, None)
    return data  # type: ignore[no-any-return]


def reduce_events(
    path: Path, *, stop_at: str | None = None, cut_after_stage: int | None = None
) -> str:
    out: list[str] = []
    last_session = None
    started = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        kind = item.get("type")
        if kind == "TurnEvent":
            session = (item.get("event") or {}).get("session_id")
            if not session or session == last_session:
                continue
            last_session = session
            item = {"type": kind, "key": item["key"], "event": {"session_id": session}}
        elif kind in KEEP_EVENTS:
            if kind == "StageFinished":
                item["artifacts"] = []
                if item.get("error"):
                    item["error"] = "<error>"
            if kind == "WaitingInput":
                item["prompt_excerpt"] = "<excerpt>"
            if kind == "RunError":
                item["detail"] = "<detail>"
            if kind == "RunFinished":
                item["summary"] = ""
        else:
            continue
        if kind == "StageStarted":
            started += 1
        out.append(alias_ids(json.dumps(item, separators=(", ", ": "))))
        if stop_at is not None and kind == stop_at:
            break
        if kind == "StageStarted" and cut_after_stage is not None and started >= cut_after_stage:
            break
    return "\n".join(out) + "\n"


def stage_case(
    name: str,
    *,
    stem: str,
    source_dir: Path,
    history_id: int,
    journal_id: int,
    keep: slice | None = None,
    last_line: int | None = None,
    unfinished: bool = False,
    stop_at: str | None = None,
    cut_after_stage: int | None = None,
) -> None:
    target = STAGING / name
    if target.exists():
        shutil.rmtree(target)
    runs = target / f"{stem}.runs" / str(journal_id)
    runs.mkdir(parents=True)
    lines = [
        line
        for line in (source_dir / f"{stem}.history").read_text(encoding="utf-8").splitlines()
        if json.loads(line)["run_id"] == history_id
    ]
    if last_line is not None:
        lines = lines[: last_line + 1]
    if keep is not None:
        lines = lines[keep]
    (target / f"{stem}.history").write_text(
        "\n".join(reduce_history_line(line) for line in lines) + "\n", encoding="utf-8"
    )
    (target / f"{stem}.checkpoint").write_text("{}\n", encoding="utf-8")
    run_dir = source_dir / f"{stem}.runs" / str(journal_id)
    header = reduce_header(run_dir / "run.json", unfinished=unfinished)
    (runs / "run.json").write_text(json.dumps(header, indent=2) + "\n", encoding="utf-8")
    (runs / "events.jsonl").write_text(
        reduce_events(run_dir / "events.jsonl", stop_at=stop_at, cut_after_stage=cut_after_stage),
        encoding="utf-8",
    )


def stage_files(norn: Path) -> None:
    here = REPO
    stage_case(
        "finished",
        stem="plan_with_review",
        source_dir=here,
        history_id=1,
        journal_id=1,
        keep=None,
    )
    # keep a few intermediate lines, the first and the final one: "the last line wins"
    path = STAGING / "finished" / "plan_with_review.history"
    lines = path.read_text(encoding="utf-8").splitlines()
    chosen = [lines[0], lines[1], lines[7], lines[-2], lines[-1]]
    path.write_text("\n".join(chosen) + "\n", encoding="utf-8")
    stage_case(
        "crashed",
        stem="plan_with_review",
        source_dir=here,
        history_id=1,
        journal_id=1,
        last_line=14,
        unfinished=True,
        cut_after_stage=9,
    )
    stage_case(
        "waiting",
        stem="plan_with_review",
        source_dir=here,
        history_id=1,
        journal_id=1,
        last_line=5,
        unfinished=True,
        stop_at="WaitingInput",
    )
    stage_case(
        "failed",
        stem="implement_features_v2",
        source_dir=norn,
        history_id=14,
        journal_id=12,
    )
    stage_case(
        "passed",
        stem="stub",
        source_dir=norn,
        history_id=180,
        journal_id=1,
    )


# --------------------------------------------------------------------------------------------
# Inbox envelopes
# --------------------------------------------------------------------------------------------

BASE_NS = int(datetime(2026, 10, 5, 14, 0, 0, tzinfo=UTC).timestamp()) * 1_000_000_000
STATE_KEY = "/Users/user/projects/example/plan_with_review"
CWD = "/Users/user/projects/example"
PID = 51234
SESSIONS = ("00000000-0000-4000-8000-00000000a001", "00000000-0000-4000-8000-00000000a002")


def envelope(
    scenario: str, number: int, at_s: float, event: str, **payload: Any
) -> tuple[str, str]:
    received = BASE_NS + int(at_s * 1_000_000_000)
    ident = str(uuid.uuid5(uuid.NAMESPACE_URL, f"fylgja-norn-fixture/{scenario}/{number}"))
    body = {
        "event": event,
        "pipeline": "plan_with_review",
        "state_key": STATE_KEY,
        "history_run_id": payload.pop("history_run_id"),
        "run_id": payload.pop("run_uuid"),
        "pid": PID,
        "cwd": CWD,
        **payload,
    }
    document = {
        "schema": "fylgja.hook/1",
        "id": ident,
        "received_at_ns": received,
        "source": "norn",
        "event_arg": event,
        "ppid": 51200,
        "env": {"TERM_PROGRAM": "ghostty"},
        "payload_text": json.dumps(body, separators=(",", ":")),
        "truncated": False,
        "fylgja_version": "norn-fylgja-subscriber/1",
    }
    return f"{received}-{ident}.json", json.dumps(document, indent=2) + "\n"


def write_inbox() -> None:
    if INBOX.exists():
        shutil.rmtree(INBOX)
    argv = ["/Users/user/projects/norn/norn/__main__.py", "ui"]
    scenarios: dict[str, list[tuple[float, str, dict[str, Any]]]] = {}
    run = {"history_run_id": 7, "run_uuid": "5f0c2b1e-7d3a-4a52-9b86-0d1f6c1d7e01"}
    scenarios["lifecycle"] = [
        (0, "RunStarted", {**run, "argv": argv, "provider": "claude-code", "stage_total": 5}),
        (1, "StageStarted", {**run, "stage": "preflight", "stage_index": 1, "attempt": 1}),
        (
            2,
            "StageFinished",
            {**run, "stage": "preflight", "stage_index": 1, "status": "passed", "success": True},
        ),
        (3, "StageStarted", {**run, "stage": "draft plan", "stage_index": 2, "attempt": 1}),
        (4, "CallingAgent", {**run, "stage": "draft plan", "provider": "claude-code"}),
        (5, "TurnEvent", {**run, "stage": "draft plan", "session_id": SESSIONS[0]}),
        (30, "Heartbeat", {**run, "stage": "draft plan"}),
        (40, "WaitingInput", {**run, "stage": "draft plan", "kind": "agent"}),
        (60, "StageStarted", {**run, "stage": "review", "stage_index": 3, "attempt": 1}),
        (61, "TurnEvent", {**run, "stage": "review", "session_id": SESSIONS[1]}),
        (
            90,
            "StageFinished",
            {**run, "stage": "review", "stage_index": 3, "status": "passed", "success": True},
        ),
        (91, "RunFinished", {**run, "success": True}),
    ]
    failed = {"history_run_id": 8, "run_uuid": "5f0c2b1e-7d3a-4a52-9b86-0d1f6c1d7e02"}
    scenarios["failed"] = [
        (0, "RunStarted", {**failed, "argv": argv, "provider": "claude-code", "stage_total": 5}),
        (1, "StageStarted", {**failed, "stage": "preflight", "stage_index": 1, "attempt": 1}),
        (
            2,
            "StageFinished",
            {
                **failed,
                "stage": "preflight",
                "stage_index": 1,
                "status": "failed",
                "success": False,
            },
        ),
        (3, "RunFinished", {**failed, "success": False}),
    ]
    cancelled = {"history_run_id": 9, "run_uuid": "5f0c2b1e-7d3a-4a52-9b86-0d1f6c1d7e03"}
    scenarios["cancelled"] = [
        (0, "RunStarted", {**cancelled, "argv": argv, "provider": "claude-code"}),
        (1, "StageStarted", {**cancelled, "stage": "draft plan", "stage_index": 1, "attempt": 1}),
        (5, "RunPaused", {**cancelled}),
        (9, "RunCancelled", {**cancelled}),
    ]
    for scenario, events in scenarios.items():
        directory = INBOX / scenario
        directory.mkdir(parents=True)
        for number, (at_s, name, payload) in enumerate(events):
            filename, text = envelope(scenario, number, at_s, name, **payload)
            (directory / filename).write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--norn", type=Path, default=Path.home() / "projects/erik.romson/norn/main")
    args = parser.parse_args()
    stage_files(args.norn)
    write_inbox()
    print(f"staged files under {STAGING}; inbox written to {INBOX}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
