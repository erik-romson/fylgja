"""Helpers shared by the OpenCode collector tests (the ``tests/`` tree has no packages).

``opencode.db`` is never committed: :func:`build_db` creates it from the sanitized DDL and rows in
``tests/fixtures/opencode/`` inside the test's own directory. The scripted ``pgrep``/``lsof``/``ps``
answers are the committed ``process/`` fixtures.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fylgja.collectors.context import build_context
from fylgja.collectors.opencode import adapter
from fylgja.collectors.opencode.collector import OpenCodeCollector, OpenCodeSettings
from fylgja.models import SessionActivitySnapshot
from fylgja.testing.fakes import FakeClock, FakeProcessRunner, RecordingSink

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "opencode"
PROCESS_FIXTURES = FIXTURES / "process"
PGREP = "/usr/bin/pgrep"
LSOF = "/usr/sbin/lsof"
PS = "/bin/ps"
CANARY = "CANARY-OPENCODE-TEXT"
TABLES = ("project", "session", "message", "part", "todo")


def session_id(number: int) -> str:
    return f"ses_00000000{number:02d}"


def rows_fixture() -> dict[str, Any]:
    loaded = json.loads((FIXTURES / "opencode.rows.json").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def now() -> datetime:
    """The instant the fixture rows are relative to."""
    return datetime.fromtimestamp(rows_fixture()["NOW_MS"] / 1000, UTC)


def _stored(value: Any) -> Any:
    return json.dumps(value, separators=(",", ":")) if isinstance(value, dict | list) else value


def build_db(
    path: Path,
    *,
    ddl: str | None = None,
    without: tuple[str, ...] = (),
    extra_sql: str = "",
) -> Path:
    """Create ``opencode.db`` at ``path`` from the committed DDL and rows.

    ``without`` names tables whose rows are left out; ``extra_sql`` runs last (a test's own rows).
    """
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            (FIXTURES / "opencode.schema.sql").read_text(encoding="utf-8") if ddl is None else ddl
        )
        data = rows_fixture()
        for table in TABLES:
            if table in without:
                continue
            for row in data[table]:
                values = {key: _stored(value) for key, value in row.items()}
                names = ", ".join(values)
                marks = ", ".join("?" for _ in values)
                connection.execute(
                    f"INSERT INTO {table} ({names}) VALUES ({marks})", list(values.values())
                )
        if extra_sql:
            connection.executescript(extra_sql)
        connection.commit()
    finally:
        connection.close()
    return path


def script_processes(runner: FakeProcessRunner, *, pids: tuple[int, ...] = (51001, 51002)) -> None:
    """Script ``pgrep``, ``lsof`` and ``ps`` the way the process fixtures show them."""
    runner.script(PGREP, stdout="".join(f"{pid}\n" for pid in pids))
    runner.script(LSOF, stdout=lsof_text(pids))
    runner.script(PS, stdout=(PROCESS_FIXTURES / "ps-table.txt").read_text(encoding="utf-8"))


def lsof_text(pids: tuple[int, ...] = (51001, 51002)) -> str:
    """The committed ``lsof -Fpfn`` fixture, limited to the processes among ``pids``."""
    text = (PROCESS_FIXTURES / "lsof-fields.txt").read_text(encoding="utf-8")
    blocks = text.split("\np")
    kept = [block for block in blocks if int(block.removeprefix("p").splitlines()[0]) in pids]
    return "\n".join(f"p{block.removeprefix('p')}" for block in kept) + "\n"


def lsof_cwds(cwds: dict[int, str]) -> str:
    """An ``lsof -Fpfn`` answer giving each pid in ``cwds`` that working directory."""
    return "".join(f"p{pid}\nfcwd\nn{cwd}\n" for pid, cwd in cwds.items())


class Rig:
    """One collector with fake time, a recording sink and a scripted process runner."""

    def __init__(
        self,
        db_path: Path,
        *,
        runner: FakeProcessRunner | None = None,
        clock: FakeClock | None = None,
        **settings: Any,
    ) -> None:
        self.clock = FakeClock(now()) if clock is None else clock
        self.sink = RecordingSink()
        self.runner = FakeProcessRunner() if runner is None else runner
        self.settings = OpenCodeSettings(db_path=db_path, inbox_watch="poll", **settings)
        self.seams = adapter.Seams(self.runner, adapter.LocalFileSystem())
        self.collector = OpenCodeCollector(
            self.settings,
            build_context("opencode", self.sink, clock=self.clock),
            db_path=db_path,
            seams=self.seams,
        )


def poll_activities(tmp_path: Path, cwds: dict[int, str]) -> dict[str, Any]:
    """One poll over the fixture database; the activity of every listed session by key.

    ``cwds`` maps a pid to the working directory ``lsof`` reports for it.
    """
    db_path = build_db(tmp_path / "opencode.db")
    rig = Rig(db_path)
    rig.runner.script(PGREP, stdout="".join(f"{pid}\n" for pid in cwds))
    rig.runner.script(LSOF, stdout=lsof_cwds(cwds))
    rig.runner.script(PS, stdout="")
    asyncio.run(rig.collector.poll())
    snapshot = next(r for r in rig.sink.records if isinstance(r, SessionActivitySnapshot))
    return {item.entity_id: item.payload for item in snapshot.items}
