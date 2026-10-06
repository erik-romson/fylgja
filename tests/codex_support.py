"""Helpers shared by the Codex collector tests (the ``tests/`` tree has no packages).

The state database is never committed: :func:`build_state_db` creates it from the sanitized DDL and
rows in ``tests/fixtures/codex/`` inside the test's own directory. Rollouts are copied there too,
under the file names Codex gives them, and the scripted ``pgrep``/``lsof``/``ps`` answers are the
committed ``process/`` fixtures with the placeholder home replaced by the test's directory.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fylgja.collectors.codex import adapter
from fylgja.collectors.codex.collector import CodexCollector, CodexSettings
from fylgja.collectors.context import build_context
from fylgja.testing.fakes import FakeClock, FakeProcessRunner, RecordingSink

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "codex"
ROLLOUT_FIXTURES = FIXTURES / "rollouts"
PROCESS_FIXTURES = FIXTURES / "process"
PLACEHOLDER_HOME = "/work/example/codex-home"
THREAD_PREFIX = "019d0000-0000-7000-8000-00000000000"
PGREP = "/usr/bin/pgrep"
LSOF = "/usr/sbin/lsof"
PS = "/bin/ps"

ROLLOUT_OF_THREAD = {
    1: "idle.jsonl",
    2: "busy-tool.jsonl",
    3: "busy-patch.jsonl",
    4: "aborted.jsonl",
    6: "fresh.jsonl",
}
"""Fixture rollout file of thread ``N`` (``…0N``)."""


def thread_id(number: int) -> str:
    return f"{THREAD_PREFIX}{number}"


def rows_fixture() -> dict[str, Any]:
    loaded = json.loads((FIXTURES / "state_5.rows.json").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@dataclass(frozen=True)
class CodexHome:
    home: Path
    rollout_dir: Path

    def rollout(self, number: int) -> Path:
        """The rollout file of thread ``number`` (present when the home was built with it)."""
        return self.rollout_dir / f"rollout-{_stamp(number)}-{thread_id(number)}.jsonl"


def _stamp(number: int) -> str:
    template = next(r for r in rows_fixture()["threads"] if r["id"] == thread_id(number))
    name = Path(template["rollout_path"]).name
    return name.removeprefix("rollout-").removesuffix(f"-{thread_id(number)}.jsonl")


def build_state_db(path: Path, rollout_dir: Path, *, ddl: str | None = None) -> None:
    """Create ``state_N.sqlite`` at ``path`` from the committed DDL and rows."""
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            (FIXTURES / "state_5.schema.sql").read_text(encoding="utf-8") if ddl is None else ddl
        )
        data = rows_fixture()
        for row in data["threads"]:
            values = {
                key: value.replace("{rollouts}", str(rollout_dir))
                if isinstance(value, str)
                else value
                for key, value in row.items()
            }
            names = ", ".join(values)
            marks = ", ".join("?" for _ in values)
            connection.execute(
                f"INSERT INTO threads ({names}) VALUES ({marks})", list(values.values())
            )
        for edge in data["thread_spawn_edges"]:
            connection.execute(
                "INSERT INTO thread_spawn_edges (parent_thread_id, child_thread_id, status)"
                " VALUES (?, ?, ?)",
                [edge["parent_thread_id"], edge["child_thread_id"], edge["status"]],
            )
        connection.commit()
    finally:
        connection.close()


def make_home(
    root: Path, *, threads: tuple[int, ...] = (1, 2, 3, 4, 6), db: bool = True
) -> CodexHome:
    """A Codex state directory with the state database and the rollouts of ``threads``."""
    home = root / "codex-home"
    rollout_dir = home / "sessions" / "2026" / "10" / "06"
    rollout_dir.mkdir(parents=True)
    codex_home = CodexHome(home, rollout_dir)
    for number in threads:
        shutil.copy(ROLLOUT_FIXTURES / ROLLOUT_OF_THREAD[number], codex_home.rollout(number))
    if db:
        build_state_db(home / "state_5.sqlite", rollout_dir)
    return codex_home


def lsof_text(codex_home: CodexHome, *, pids: tuple[int, ...] = (41001, 41002)) -> str:
    """The committed ``lsof -Fpfn`` fixture for the processes among ``pids``, in ``codex_home``."""
    text = (PROCESS_FIXTURES / "lsof-fields.txt").read_text(encoding="utf-8")
    blocks = text.split("\np")
    kept = [block for block in blocks if int(block.removeprefix("p").splitlines()[0]) in pids]
    return ("\n".join(f"p{block.removeprefix('p')}" for block in kept) + "\n").replace(
        PLACEHOLDER_HOME, str(codex_home.home)
    )


def script_processes(
    runner: FakeProcessRunner, codex_home: CodexHome, *, pids: tuple[int, ...] = (41001, 41002)
) -> None:
    """Script ``pgrep``, ``lsof`` and ``ps`` the way the process fixtures show them."""
    runner.script(PGREP, stdout="".join(f"{pid}\n" for pid in pids))
    runner.script(LSOF, stdout=lsof_text(codex_home, pids=pids))
    runner.script(PS, stdout=(PROCESS_FIXTURES / "ps-table.txt").read_text(encoding="utf-8"))


class Rig:
    """One collector with fake time, a recording sink and a scripted process runner."""

    def __init__(
        self,
        codex_home: CodexHome,
        *,
        runner: FakeProcessRunner | None = None,
        **settings: Any,
    ) -> None:
        self.codex_home = codex_home
        self.clock = FakeClock()
        self.sink = RecordingSink()
        self.runner = FakeProcessRunner() if runner is None else runner
        self.settings = CodexSettings(codex_home=codex_home.home, **settings)
        seams = adapter.Seams(self.runner, adapter.LocalFileSystem())
        self.collector = CodexCollector(
            self.settings,
            build_context("codex", self.sink, clock=self.clock),
            codex_home=codex_home.home,
            seams=seams,
        )

    def swap_runner(self, runner: FakeProcessRunner) -> None:
        """Answer the next polls from ``runner`` (the process table and its cache stay)."""
        self.runner = runner
        self.collector._seams = adapter.Seams(runner, adapter.LocalFileSystem())
