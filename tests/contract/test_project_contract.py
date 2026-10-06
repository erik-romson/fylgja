"""The shared collector contract run against the built-in ``project`` collector (WP4.3).

The known projects come from a real store built under the test's own directory (the daemon's
``projects`` rows); their repo-root ``TODO.md`` files are real files there too. No repository,
editor or daemon is needed.

Project state (WP5.4) runs on the contract's process runner: ``git`` and ``gh`` are scripted, the
store has a live session in the project, so the PR and the build are looked up as well. The working
source thus emits ``todo``, ``project`` and ``build`` records. A ``git`` that is not installed is a
broken source too: it emits no project state and reports ``degraded``.

The broken sources are the failures WP4.3 names: a ``TODO.md`` that is not a file, one that is not
UTF-8 text, one that cannot be read for lack of permission (skipped as root, who reads anyway), and
a store that cannot be read. Each emits nothing for what failed and reports ``degraded``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from project_support import api_response, script_git, script_pr, script_runs, store_with_activity

from fylgja.collectors.project import adapter
from fylgja.store import db, queries
from fylgja.testing.contract import CollectorContract, ContractEnv, ContractSource

TODO_MD = """# TODO

- [ ] Write the contract test
- [x] Register the plugin
- [ ] next: Reconcile the rows
  - [ ] A nested task
"""


def _store(base: Path, roots: Sequence[Path]) -> Path:
    """A store whose ``projects`` lists ``roots`` (built once; later runs reuse it)."""
    path = base / "store" / "fylgja.db"
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = db.open_store(path)
    try:
        connection.execute("BEGIN")
        for root in roots:
            queries.upsert_project(
                connection,
                path=str(root),
                values={"repo_root": str(root)},
                at="2026-01-01T00:00:00.000000Z",
            )
        connection.execute("COMMIT")
    finally:
        connection.close()
    return path


def _runner(env: ContractEnv) -> None:
    """The collector's ``git`` and ``gh`` go through the contract's fake process runner."""
    env.monkeypatch.setattr(adapter, "default_runner", lambda: env.process_runner)


def _roots(base: Path) -> tuple[Path, Path]:
    """Two repository roots under ``base`` (each source has its own ``base``)."""
    app, other = base / "repos" / "app", base / "repos" / "other"
    app.mkdir(parents=True, exist_ok=True)
    other.mkdir(parents=True, exist_ok=True)
    return app, other


def _working(env: ContractEnv) -> Mapping[str, Any]:
    base = env.tmp_path / "working"
    app, other = _roots(base)
    (app / "TODO.md").write_text(TODO_MD, encoding="utf-8")  # `other` has no TODO.md
    _runner(env)
    script_git(env.process_runner, str(app))  # a GitHub remote: the PR and the build follow
    script_git(env.process_runner, str(other), status="status-dirty-ahead-behind.txt", remote=None)
    script_pr(env.process_runner)
    script_runs(env.process_runner, stdout=api_response("api-200.txt"))
    database = store_with_activity(
        base / "store" / "fylgja.db",
        env.clock.now(),
        projects=[str(app), str(other)],
        live=[str(app)],
    )
    return {"database": str(database)}


def _git_missing(env: ContractEnv) -> Mapping[str, Any]:
    base = env.tmp_path / "git-missing"
    app, _ = _roots(base)
    (app / "TODO.md").write_text(TODO_MD, encoding="utf-8")
    _runner(env)  # nothing is scripted: ``git`` is not installed
    return {"database": str(_store(base, (app,)))}


def _not_a_file(env: ContractEnv) -> Mapping[str, Any]:
    base = env.tmp_path / "not-a-file"
    app, _ = _roots(base)
    (app / "TODO.md").mkdir(exist_ok=True)
    _runner(env)
    return {"database": str(_store(base, (app,)))}


def _not_utf8(env: ContractEnv) -> Mapping[str, Any]:
    base = env.tmp_path / "not-utf8"
    app, _ = _roots(base)
    (app / "TODO.md").write_bytes(b"- [ ] caf\xe9\n")
    _runner(env)
    return {"database": str(_store(base, (app,)))}


def _no_permission(env: ContractEnv) -> Mapping[str, Any]:
    base = env.tmp_path / "no-permission"
    app, _ = _roots(base)
    target = app / "TODO.md"
    if not target.exists():
        target.write_text(TODO_MD, encoding="utf-8")
        target.chmod(0)
    _runner(env)
    return {"database": str(_store(base, (app,)))}


def _store_unreadable(env: ContractEnv) -> Mapping[str, Any]:
    path = env.tmp_path / "broken" / "fylgja.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"this is not a database" * 64)
    _runner(env)
    return {"database": str(path)}


class TestProjectContract(CollectorContract):
    @pytest.fixture
    def plugin_name(self) -> str:
        return "project"

    @pytest.fixture
    def working_source(self) -> ContractSource:
        return ContractSource(
            label="two projects, one TODO.md, scripted git and gh", arrange=_working
        )

    @pytest.fixture
    def broken_source(self) -> list[ContractSource]:
        sources = [
            ContractSource(label="TODO.md is a directory", arrange=_not_a_file),
            ContractSource(label="TODO.md is not UTF-8", arrange=_not_utf8),
            ContractSource(label="store unreadable", arrange=_store_unreadable),
            ContractSource(label="git is not installed", arrange=_git_missing),
        ]
        if os.geteuid() != 0:  # root reads a mode-000 file anyway
            sources.append(ContractSource(label="TODO.md not readable", arrange=_no_permission))
        return sources
