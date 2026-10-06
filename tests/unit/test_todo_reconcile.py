"""Reconciliation of read-only todo imports (WP4.3).

The ``TODO.md`` cases run end to end: the real ``project`` collector reads a ``TODO.md`` on a fake
filesystem, finds the project in a real store and emits into the real writer, whose live view
``/now`` is built from. Every WP4.3 case is here: dedup when the file changes, a checkbox ticked
and unticked, a text edit, a deleted line, a duplicated line, a deleted and re-created file, a
reappearing item restored rather than duplicated, an unreadable file leaving the rows untouched,
and three consecutive edits leaving no duplicates or stale rows. The reconciler's own rules
(ownership, refusals, staleness, incomplete reads, read-only rows) follow.
"""

from __future__ import annotations

import errno
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from todo_import_support import T0, Store, TodoFile, WriterSink, open_store

from fylgja.collectors.context import build_context
from fylgja.collectors.project import adapter, parse
from fylgja.collectors.project.collector import ProjectCollector, ProjectSettings
from fylgja.collectors.todo_lists import list_entity_id, owns, split_list_entity_id
from fylgja.core import todo_import
from fylgja.core.todos import NATIVE_SOURCE, TodoNotFound, TodoReadOnly, TodoService
from fylgja.models import HealthState, SnapshotItem, TodoPayload, TodoSnapshot
from fylgja.testing.fakes import FakeFS

ROOT = "/work/app"
PATH = "/work/app/TODO.md"
SOURCE = "project-todo-md"


class ShakyFS(FakeFS):
    """A fake filesystem whose reads can race a writer: the file changes or vanishes mid-read."""

    def __init__(self) -> None:
        super().__init__()
        self.grow_on_read = False
        self.vanish_on_read = False

    def read_bytes(self, path: Path | str) -> bytes:
        data = super().read_bytes(path)
        if self.grow_on_read:
            self.add_file(path, data + b"- [ ] appended\n", mtime=9_999.0)
        if self.vanish_on_read:  # replaced under the reader: the inode it opened is gone
            raise FileNotFoundError(errno.ENOENT, "gone", str(path))
        return data


@dataclass
class Rig:
    store: Store
    fs: ShakyFS
    file: TodoFile
    collector: ProjectCollector

    async def poll(self) -> int:
        await self.store.clock.advance(1.0)
        emitted = self.collector.poll()
        await self.store.writer.flush()
        return emitted

    async def edit(self, text: str | bytes) -> int:
        self.file.write(text)
        return await self.poll()

    def present(self) -> dict[str, dict[str, Any]]:
        """The rows of the file that are not deleted, by ``source_ref``."""
        return {row["source_ref"]: row for row in self.store.present(SOURCE)}

    def ids(self) -> dict[str, str]:
        """``{text: id}`` of the rows that are not deleted (texts must be unique)."""
        return {row["text"]: row["id"] for row in self.store.present(SOURCE)}


def _collector(store: Store, fs: FakeFS, database: Path | None = None) -> ProjectCollector:
    return ProjectCollector(
        ProjectSettings(),
        build_context("project", WriterSink(store.writer), clock=store.clock),
        database=store.database if database is None else database,
        fs=fs,
    )


@pytest.fixture
async def store(isolated_env: Path) -> AsyncIterator[Store]:
    async with open_store(isolated_env) as opened:
        yield opened


@pytest.fixture
async def rig(store: Store) -> Rig:
    await store.add_project(ROOT, ROOT)
    fs = ShakyFS()
    return Rig(store, fs, TodoFile(fs, PATH), _collector(store, fs))


def ref(key: str) -> str:
    return f"{PATH}#{key}"


def check_matches_file(rig: Rig, text: str) -> None:
    """The stored list is exactly the file: no duplicates, no stale rows, the live view agrees."""
    items = parse.parse_todo_md(text)
    expected = {ref(item.key): (item.text, item.status) for item in items}
    present = rig.present()
    assert {r: (row["text"], row["status"]) for r, row in present.items()} == expected
    every = rig.store.rows(SOURCE)
    assert len({row["source_ref"] for row in every}) == len(every)  # one row per key, ever
    for row in every:
        if row["source_ref"] not in expected:
            assert (row["deleted_reason"], row["deleted_at"] is not None) == ("source_absent", True)
        else:
            assert (row["deleted_reason"], row["deleted_at"]) == (None, None)
        assert row["scope_kind"] == "project"
        assert row["scope_ref"] == ROOT
        assert row["link"] == PATH
    shown = {
        row["id"]: (row["text"], row["status"])
        for row in present.values()
        if row["status"] != "done"
    }
    assert rig.store.shown() == shown


# --- the WP4.3 cases ------------------------------------------------------------------------


async def test_dedup_when_the_file_changes(rig: Rig) -> None:
    first_text = "- [ ] alpha\n- [ ] beta\n- [x] gamma\n"
    assert await rig.edit(first_text) == 1
    first = rig.ids()
    assert sorted(first) == ["alpha", "beta", "gamma"]
    check_matches_file(rig, first_text)

    second_text = "# Todo\n\n- [ ] beta\n- [ ] alpha\n- [x] gamma\n- [ ] delta\n"
    assert await rig.edit(second_text) == 1
    second = rig.ids()
    assert {text: second[text] for text in first} == first  # the same rows, moved or not
    assert len(rig.store.rows(SOURCE)) == 4  # one new row; nothing duplicated
    check_matches_file(rig, second_text)

    assert await rig.poll() == 0  # unchanged: nothing emitted, nothing written
    assert len(rig.store.rows(SOURCE)) == 4


async def test_a_checkbox_ticked_and_unticked_updates_the_same_row(rig: Rig) -> None:
    await rig.edit("- [ ] ship it\n")
    (row,) = rig.store.present(SOURCE)
    assert (row["status"], row["done_at"]) == ("open", None)
    assert rig.store.shown() == {row["id"]: ("ship it", "open")}

    await rig.edit("- [x] ship it\n")
    (ticked,) = rig.store.present(SOURCE)
    assert ticked["id"] == row["id"]
    assert ticked["status"] == "done"
    assert ticked["done_at"] is not None
    assert ticked["updated_at"] > row["updated_at"]
    assert ticked["created_at"] == row["created_at"]
    assert rig.store.shown() == {}  # the live view holds open and next todos only

    await rig.edit("- [ ] ship it\n")
    (unticked,) = rig.store.present(SOURCE)
    assert (unticked["id"], unticked["status"], unticked["done_at"]) == (row["id"], "open", None)

    await rig.edit("- [ ] next: ship it\n")
    (marked,) = rig.store.present(SOURCE)
    assert (marked["id"], marked["status"]) == (row["id"], "next")
    assert rig.store.shown() == {row["id"]: ("ship it", "next")}
    assert len(rig.store.rows(SOURCE)) == 1


async def test_a_text_edit_is_a_removal_plus_a_new_item(rig: Rig) -> None:
    await rig.edit("- [ ] write the docs\n- [ ] keep me\n")
    before = rig.ids()
    await rig.edit("- [ ] write the user docs\n- [ ] keep me\n")
    rows = {row["text"]: row for row in rig.store.rows(SOURCE)}
    old, new = rows["write the docs"], rows["write the user docs"]
    assert (old["id"], old["deleted_reason"]) == (before["write the docs"], "source_absent")
    assert old["deleted_at"] is not None
    assert new["id"] not in before.values()
    assert new["deleted_at"] is None
    assert rows["keep me"]["id"] == before["keep me"]
    assert set(rig.store.shown()) == {new["id"], before["keep me"]}


async def test_a_deleted_line_is_source_absent(rig: Rig) -> None:
    await rig.edit("- [ ] alpha\n- [ ] beta\n")
    beta = rig.ids()["beta"]
    await rig.edit("- [ ] alpha\n")
    (gone,) = [row for row in rig.store.rows(SOURCE) if row["id"] == beta]
    assert gone["deleted_reason"] == "source_absent"
    assert gone["deleted_at"] == gone["updated_at"]
    assert beta not in rig.store.shown()
    assert list(rig.ids()) == ["alpha"]


async def test_duplicated_lines_are_kept_apart_by_their_ordinal(rig: Rig) -> None:
    await rig.edit("- [ ] run the tests\n- [ ] deploy\n- [ ] run the tests\n")
    first, deploy, second = rig.store.present(SOURCE)
    assert (first["text"], deploy["text"], second["text"]) == (
        "run the tests",
        "deploy",
        "run the tests",
    )
    assert first["source_ref"].endswith(":0")
    assert second["source_ref"].endswith(":1")
    assert first["source_ref"].rpartition(":")[0] == second["source_ref"].rpartition(":")[0]

    await rig.edit("- [ ] run the tests\n- [ ] deploy\n- [x] run the tests\n")
    status = {row["id"]: row["status"] for row in rig.store.present(SOURCE)}
    assert (status[first["id"]], status[second["id"]]) == ("open", "done")

    await rig.edit("- [ ] run the tests\n- [ ] deploy\n")
    rows = {row["id"]: row for row in rig.store.rows(SOURCE)}
    assert rows[first["id"]]["deleted_at"] is None
    assert rows[second["id"]]["deleted_reason"] == "source_absent"
    assert len(rows) == 3


async def test_a_deleted_and_recreated_file_restores_its_rows(rig: Rig) -> None:
    text = "- [ ] alpha\n- [x] beta\n"
    await rig.edit(text)
    ids = rig.ids()
    (beta_before,) = [row for row in rig.store.present(SOURCE) if row["text"] == "beta"]

    rig.file.remove()
    assert await rig.poll() == 1  # no file is a complete, empty read
    assert rig.store.present(SOURCE) == []
    assert {row["deleted_reason"] for row in rig.store.rows(SOURCE)} == {"source_absent"}
    assert rig.store.shown() == {}
    assert await rig.poll() == 0  # still missing: nothing more to say

    await rig.edit(text)
    assert rig.ids() == ids  # the same rows came back
    assert len(rig.store.rows(SOURCE)) == 2
    (beta_after,) = [row for row in rig.store.present(SOURCE) if row["text"] == "beta"]
    assert beta_after["done_at"] == beta_before["done_at"]  # done once, stamped once
    check_matches_file(rig, text)


async def test_a_reappearing_item_is_restored_not_duplicated(rig: Rig) -> None:
    await rig.edit("- [ ] alpha\n- [ ] beta\n")
    beta = rig.ids()["beta"]
    await rig.edit("- [ ] alpha\n")
    await rig.edit("- [ ] alpha\n- [ ] next: beta\n")
    (row,) = [row for row in rig.store.rows(SOURCE) if row["text"] == "beta"]
    assert row["id"] == beta
    assert (row["deleted_at"], row["deleted_reason"], row["status"]) == (None, None, "next")
    assert len(rig.store.rows(SOURCE)) == 2
    assert rig.store.shown()[beta] == ("beta", "next")


def _deny(rig: Rig) -> None:
    rig.fs.fail(PATH, PermissionError(errno.EACCES, "permission denied", PATH))


def _not_utf8(rig: Rig) -> None:
    rig.file.write(b"- [ ] caf\xe9\n")


def _binary(rig: Rig) -> None:
    rig.file.write(b"- [ ] a\x00b\n")


def _directory(rig: Rig) -> None:
    rig.file.remove()
    rig.fs.add_dir(PATH)


def _too_large(rig: Rig) -> None:
    rig.file.write("- [ ] x\n" * (parse.MAX_BYTES // 8 + 1))


def _too_many(rig: Rig) -> None:
    rig.file.write("".join(f"- [ ] task {n}\n" for n in range(parse.MAX_ITEMS + 1)))


def _changes_while_read(rig: Rig) -> None:
    rig.file.write("- [ ] alpha\n- [ ] gamma\n")
    rig.fs.grow_on_read = True


def _vanishes_while_read(rig: Rig) -> None:
    rig.file.write("- [ ] alpha\n- [ ] gamma\n")
    rig.fs.vanish_on_read = True


@pytest.mark.parametrize(
    ("breaks", "kind"),
    [
        (_deny, "todo_md_unreadable"),
        (_not_utf8, "todo_md_not_utf8"),
        (_binary, "todo_md_binary"),
        (_directory, "todo_md_not_a_file"),
        (_too_large, "todo_md_too_large"),
        (_too_many, "todo_md_too_many_items"),
        (_changes_while_read, "todo_md_partial_read"),
        (_vanishes_while_read, "todo_md_partial_read"),
    ],
)
async def test_an_unreadable_file_leaves_the_rows_untouched(
    rig: Rig, breaks: Any, kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    good = "- [ ] alpha\n- [x] beta\n"
    await rig.edit(good)
    before = rig.store.rows(SOURCE)
    shown = rig.store.shown()

    breaks(rig)
    with caplog.at_level(logging.WARNING):
        assert await rig.poll() == 0  # a failed read emits nothing
        assert await rig.poll() == 0
    assert rig.store.rows(SOURCE) == before
    assert rig.store.shown() == shown
    report = await rig.collector.doctor()
    assert report.state is HealthState.DEGRADED
    assert report.error_kind == kind
    assert report.detail is not None
    assert "alpha" not in report.detail
    assert PATH not in report.detail
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1  # logged once, not every poll
    assert kind in warnings[0]
    assert PATH not in warnings[0]

    rig.fs.heal(PATH)
    rig.fs.grow_on_read = rig.fs.vanish_on_read = False
    if rig.fs.is_dir(PATH):
        rig.fs = ShakyFS()
        rig.file = TodoFile(rig.fs, PATH, mtime=rig.file.mtime)
        rig.collector._fs = rig.fs  # the directory cannot be removed from a FakeFS
    fixed = "- [ ] alpha\n- [x] beta\n- [ ] gamma\n"
    await rig.edit(fixed)
    check_matches_file(rig, fixed)
    assert (await rig.collector.doctor()).state is HealthState.OK


async def test_three_consecutive_edits_leave_no_duplicates_or_stale_rows(rig: Rig) -> None:
    initial = "- [ ] alpha\n- [ ] beta\n- [ ] gamma\n"
    edits = [
        # tick alpha, rename beta, duplicate gamma
        "- [x] alpha\n- [ ] beta, renamed\n- [ ] gamma\n- [ ] gamma\n",
        # untick alpha and mark it next, drop the renamed beta and the second gamma, add delta
        "# Now\n\n- [ ] next: alpha\n- [ ] gamma\n  - [ ] delta\n",
        # beta is back under its first name, delta is done, gamma moved
        "- [ ] gamma\n- [ ] alpha #next\n- [ ] beta\n- [X] delta\n",
    ]
    await rig.edit(initial)
    check_matches_file(rig, initial)
    first = rig.ids()
    keys_seen = {ref(item.key) for item in parse.parse_todo_md(initial)}
    for text in edits:
        assert await rig.edit(text) == 1
        check_matches_file(rig, text)
        keys_seen |= {ref(item.key) for item in parse.parse_todo_md(text)}
        assert len(rig.store.rows(SOURCE)) == len(keys_seen)  # a row per key ever seen, no more
    final = rig.ids()
    assert {text: final[text] for text in ("alpha", "beta", "gamma")} == first  # the same rows
    assert final["delta"] not in first.values()
    assert len(rig.store.rows(SOURCE)) == 6  # alpha, beta, gamma, renamed beta, gamma #2, delta
    assert await rig.poll() == 0


# --- the collector around the file -----------------------------------------------------------


async def test_the_first_read_of_a_missing_file_is_an_empty_list(rig: Rig) -> None:
    assert await rig.poll() == 1
    assert rig.store.rows(SOURCE) == []
    assert rig.collector.emissions == 1
    report = await rig.collector.doctor()
    assert report.state is HealthState.OK
    assert report.detail == "1 projects; 0 TODO.md files with 0 tasks"


async def test_an_unchanged_file_is_not_read_again(rig: Rig) -> None:
    await rig.edit("- [ ] alpha\n")
    reads = len(rig.fs.reads)
    await rig.poll()
    await rig.poll()
    assert len(rig.fs.reads) == reads
    # a rewrite with the same tasks is read, but nothing is emitted
    rig.file.write("# Heading\n\n- [ ] alpha\n")
    assert await rig.poll() == 0
    assert len(rig.fs.reads) == reads + 1


async def test_projects_without_a_root_are_skipped_and_one_root_is_read_once(
    store: Store,
) -> None:
    await store.add_project("/work/plain", None)  # not a repository
    await store.add_project("/work/app/sub", "/work/app")
    await store.add_project("/work/app", "/work/app")
    fs = FakeFS()
    TodoFile(fs, PATH).write("- [ ] alpha\n")
    TodoFile(fs, "/work/plain/TODO.md").write("- [ ] not read\n")
    collector = _collector(store, fs)
    assert collector.poll() == 1
    await store.writer.flush()
    (row,) = store.rows(SOURCE)
    assert (row["scope_kind"], row["scope_ref"]) == ("project", "/work/app")
    assert [str(p) for p in fs.reads] == [PATH]
    assert adapter.known_projects(store.database) == [
        adapter.KnownProject(path="/work/app", root="/work/app")
    ]


async def test_a_project_the_store_no_longer_lists_keeps_its_rows(rig: Rig) -> None:
    await rig.edit("- [ ] alpha\n")
    before = rig.store.rows(SOURCE)

    def forget(tx: Any) -> None:
        tx.connection.execute("DELETE FROM projects WHERE path = ?", (ROOT,))

    await rig.store.writer.execute(forget)
    rig.file.remove()
    assert await rig.poll() == 0  # not read is not an empty read
    assert rig.store.rows(SOURCE) == before


async def test_a_store_that_cannot_be_read_emits_nothing(
    store: Store, isolated_env: Path, caplog: pytest.LogCaptureFixture
) -> None:
    broken = isolated_env / "broken.db"
    broken.write_bytes(b"not a database" * 100)
    collector = _collector(store, FakeFS(), database=broken)
    assert (await collector.doctor()).detail == "not polled yet"
    with caplog.at_level(logging.INFO):
        assert collector.poll() == 0
        assert collector.poll() == 0
        report = await collector.doctor()
        assert (report.state, report.error_kind) == (HealthState.DEGRADED, "store_unreadable")
        broken.unlink()  # no store yet: no projects, and that is fine
        assert collector.poll() == 0
    assert (await collector.doctor()).state is HealthState.OK
    messages = [r.getMessage() for r in caplog.records]
    assert sum("store read failed" in m for m in messages) == 1
    assert any("store read recovered" in m for m in messages)


async def test_an_unexpected_poll_error_is_reported_and_the_loop_goes_on(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    calls: list[int] = []
    real = rig.collector.poll

    def flaky() -> int:
        calls.append(1)
        if len(calls) <= 2:
            raise RuntimeError("boom")
        return real()

    monkeypatch.setattr(rig.collector, "poll", flaky)
    task = asyncio.create_task(rig.collector.run())
    try:
        await rig.store.clock.advance(0)
        report = await rig.collector.doctor()
        assert (report.state, report.error_kind) == (HealthState.DEGRADED, "poll_failed")
        await rig.store.clock.advance(10.0)
        assert len(calls) >= 3
        assert (await rig.collector.doctor()).state is HealthState.OK
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# --- the reconciler's own rules ------------------------------------------------------------


def snapshot(
    collector: str,
    entity_id: str,
    items: list[tuple[str, TodoPayload]],
    *,
    at: Any = T0,
    complete: bool = True,
) -> TodoSnapshot:
    return TodoSnapshot(
        source=collector,
        entity_id=entity_id,
        observed_at=at,
        complete=complete,
        items=tuple(SnapshotItem(entity_id=key, payload=payload) for key, payload in items),
    )


def item(text: str, status: str = "open", **extra: Any) -> TodoPayload:
    fields: dict[str, Any] = {"scope_kind": "project", "scope_ref": ROOT, **extra}
    return TodoPayload(text=text, status=status, **fields)  # type: ignore[arg-type]


LIST = list_entity_id(SOURCE, PATH)


def test_list_ids_and_ownership() -> None:
    assert LIST == f"{SOURCE}:{PATH}"
    assert split_list_entity_id(LIST) == (SOURCE, PATH)
    assert split_list_entity_id("claude-tasks:claude:s-1") == ("claude-tasks", "claude:s-1")
    for bad in ("all", "TODO.md", ":x", "Bad:x", "a:", "a: ", "a:x\ny", f"a:{'x' * 801}"):
        assert split_list_entity_id(bad) is None, bad
    assert owns("project", "project-todo-md")
    assert owns("project", "project")
    assert not owns("project", "projects-x")
    assert not owns("claude", "project-todo-md")
    assert not owns("fylgja", "fylgja")


@pytest.mark.parametrize(
    ("record", "refusal"),
    [
        (snapshot("project", "all", []), todo_import.Refusal.NOT_A_LIST),
        (snapshot("claude", LIST, [("k", item("x"))]), todo_import.Refusal.NOT_OWNED),
        (snapshot("fylgja", "fylgja:x", [("k", item("x"))]), todo_import.Refusal.NOT_OWNED),
        (
            snapshot("project", LIST, [("k", item("x")), ("k", item("y"))]),
            todo_import.Refusal.DUPLICATE_KEY,
        ),
        (snapshot("project", LIST, [("a#b", item("x"))]), todo_import.Refusal.BAD_ITEM),
        (snapshot("project", LIST, [("k", item("   "))]), todo_import.Refusal.BAD_ITEM),
        (
            snapshot("project", LIST, [("k", TodoPayload(text="x"))]),
            todo_import.Refusal.BAD_ITEM,
        ),
        (
            snapshot("project", LIST, [("k", item("x", scope_kind="galaxy"))]),
            todo_import.Refusal.BAD_ITEM,
        ),
        (
            snapshot("project", LIST, [("k", item("x", scope_ref="a\nb"))]),
            todo_import.Refusal.BAD_ITEM,
        ),
        (
            snapshot("project", LIST, [("k", item("x", link="l" * 2000))]),
            todo_import.Refusal.BAD_ITEM,
        ),
    ],
)
def test_snapshots_that_cannot_be_taken_as_a_whole_are_refused(
    record: TodoSnapshot, refusal: todo_import.Refusal
) -> None:
    assert todo_import.plan(record) is refusal


def test_too_many_items_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(todo_import, "MAX_ITEMS", 2)
    record = snapshot("project", LIST, [(str(n), item(f"t{n}")) for n in range(3)])
    assert todo_import.plan(record) is todo_import.Refusal.TOO_MANY


async def test_a_refused_snapshot_changes_nothing_and_says_why(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    await store.apply(snapshot("project", LIST, [("a", item("alpha"))]))
    before = store.rows()
    with caplog.at_level(logging.WARNING, logger="fylgja.writer"):
        await store.apply(
            snapshot("project", LIST, [("b", item("beta")), ("b", item("beta again"))])
        )
        await store.apply(snapshot("claude", LIST, []))  # someone else's list: never wiped
    assert store.rows() == before
    messages = [r.getMessage() for r in caplog.records]
    assert any("duplicate_key" in m for m in messages)
    assert any("not_owned" in m for m in messages)
    assert not any("alpha" in m or "beta" in m for m in messages)


async def test_a_snapshot_that_names_no_list_is_kept_as_an_event_only(store: Store) -> None:
    await store.apply(snapshot("project", "all", [("a", item("alpha"))]))
    assert store.rows() == []
    events = store.query("SELECT record_type, entity_kind, entity_id FROM events")
    assert events == [{"record_type": "snapshot", "entity_kind": "todo", "entity_id": "all"}]


async def test_an_older_read_never_overrides_a_newer_one(store: Store) -> None:
    later = T0 + timedelta(minutes=5)
    await store.apply(snapshot("project", LIST, [("a", item("alpha", "next"))], at=later))
    await store.apply(snapshot("project", LIST, [], at=T0))  # stale: refused as a whole
    (row,) = store.present(SOURCE)
    assert (row["text"], row["status"]) == ("alpha", "next")


async def test_an_incomplete_read_upserts_and_restores_but_never_removes(store: Store) -> None:
    await store.apply(snapshot("project", LIST, [("a", item("alpha")), ("b", item("beta"))]))
    await store.apply(snapshot("project", LIST, [("a", item("alpha"))]))  # complete: beta goes
    await store.apply(
        snapshot("project", LIST, [("b", item("beta")), ("c", item("gamma"))], complete=False)
    )
    assert sorted(row["text"] for row in store.present(SOURCE)) == ["alpha", "beta", "gamma"]
    assert len(store.rows(SOURCE)) == 3


async def test_the_lists_of_one_source_are_reconciled_apart(store: Store) -> None:
    other = list_entity_id(SOURCE, "/work/other/TODO.md")
    await store.apply(snapshot("project", LIST, [("a", item("alpha"))]))
    await store.apply(
        snapshot("project", other, [("a", item("other alpha", scope_ref="/work/other"))])
    )
    await store.apply(snapshot("project", LIST, []))  # empties this file only
    (row,) = store.present(SOURCE)
    assert (row["text"], row["source_ref"]) == ("other alpha", "/work/other/TODO.md#a")


async def test_a_list_ref_with_a_hash_is_not_confused_with_its_prefix(store: Store) -> None:
    # The rows "<PATH>#x" (list PATH, key x) and "<PATH>#x#y" (list "<PATH>#x", key y) share a
    # prefix; each list still reconciles only its own.
    tricky = list_entity_id(SOURCE, f"{PATH}#x")
    await store.apply(snapshot("project", LIST, [("x", item("plain"))]))
    await store.apply(snapshot("project", tricky, [("y", item("tricky"))]))
    assert sorted(row["source_ref"] for row in store.present(SOURCE)) == [
        f"{PATH}#x",
        f"{PATH}#x#y",
    ]
    await store.apply(snapshot("project", LIST, []))
    assert [row["text"] for row in store.present(SOURCE)] == ["tricky"]
    await store.apply(snapshot("project", tricky, []))
    await store.apply(snapshot("project", LIST, [("x", item("plain"))]))
    assert [row["text"] for row in store.present(SOURCE)] == ["plain"]


async def test_the_source_s_times_text_cap_and_scope_change(store: Store) -> None:
    created = T0 - timedelta(days=1)
    finished = T0 - timedelta(hours=1)
    long = "word " * 300
    await store.apply(
        snapshot(
            "project",
            LIST,
            [
                ("a", item("alpha", "done", created_at=created, done_at=finished)),
                ("b", item(long)),
            ],
        )
    )
    rows = {row["source_ref"]: row for row in store.present(SOURCE)}
    alpha, cut = rows[f"{PATH}#a"], rows[f"{PATH}#b"]
    assert alpha["created_at"] == "2026-10-05T09:00:00.000000Z"
    assert alpha["done_at"] == "2026-10-06T08:00:00.000000Z"
    assert alpha["updated_at"] == "2026-10-06T09:00:00.000000Z"
    assert len(cut["text"]) == todo_import.MAX_TEXT_CHARS
    assert cut["text"].endswith("…")
    await store.apply(snapshot("project", LIST, [("a", item("alpha", "done", scope_ref="/w"))]))
    (moved,) = store.present(SOURCE)
    assert (moved["id"], moved["scope_ref"]) == (alpha["id"], "/w")
    assert moved["done_at"] == alpha["done_at"]  # still done: the first stamp stays


async def test_imported_todos_are_read_only_for_the_service(store: Store) -> None:
    await store.apply(snapshot("project", LIST, [("a", item("alpha"))]))
    (row,) = store.present(SOURCE)
    service = TodoService(store.writer, store.view, clock=store.clock)
    with pytest.raises(TodoReadOnly, match="imported from project-todo-md"):
        await service.update(row["id"], status="done")
    with pytest.raises(TodoReadOnly, match="read-only"):
        await service.update(row["id"], text="alpha")  # even a change to the same text
    with pytest.raises(TodoReadOnly):
        await service.delete(row["id"])
    assert store.present(SOURCE) == [row]
    native = await service.add("mine", scope_kind="project", scope_ref=ROOT)
    assert native.source == NATIVE_SOURCE
    assert (await service.done(native.id)).status == "done"
    await service.delete(native.id)
    with pytest.raises(TodoNotFound):
        await service.delete(native.id)
    assert todo_import.is_imported(SOURCE)
    assert not todo_import.is_imported(NATIVE_SOURCE)


async def test_native_todos_are_never_touched_by_a_list(store: Store) -> None:
    service = TodoService(store.writer, store.view, clock=store.clock)
    native = await service.add("mine", scope_kind="project", scope_ref=ROOT)
    await store.apply(snapshot("project", LIST, [("a", item("alpha"))]))
    await store.apply(snapshot("project", LIST, []))
    assert [row["id"] for row in store.present(NATIVE_SOURCE)] == [native.id]
    assert native.id in store.shown()
