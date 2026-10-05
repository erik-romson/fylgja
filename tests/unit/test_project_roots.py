"""Git root and draupnir group lookup through the filesystem seam (WP1.8)."""

from __future__ import annotations

from fylgja.core.project_roots import MAX_CACHE, ProjectFact, ProjectLocator
from fylgja.testing.fakes import FakeFS


def _fs() -> FakeFS:
    fs = FakeFS()
    fs.add_dir("/p/plain/.git")
    fs.add_dir("/p/ws/main/.git")
    fs.add_file("/p/ws/draupnir.toml", "[workspace]")
    fs.add_file("/p/ws/clone-a/.git", "gitdir: ../main/.git/worktrees/a")
    fs.add_dir("/p/ws/clone-a/src/deep")
    fs.add_dir("/p/bare/dir")
    return fs


def test_the_cwd_is_its_own_git_root() -> None:
    assert ProjectLocator(_fs()).locate("/p/plain") == ProjectFact(git_root="/p/plain")


def test_a_subdirectory_finds_the_root_above_it() -> None:
    assert ProjectLocator(_fs()).locate("/p/plain/a/b") == ProjectFact(git_root="/p/plain")


def test_a_draupnir_clone_belongs_to_the_group_of_its_workspace() -> None:
    locator = ProjectLocator(_fs())
    assert locator.locate("/p/ws/main") == ProjectFact(git_root="/p/ws/main", group="ws")
    assert locator.locate("/p/ws/clone-a/src/deep") == ProjectFact(
        git_root="/p/ws/clone-a", group="ws"
    )


def test_the_workspace_directory_itself_names_the_group() -> None:
    assert ProjectLocator(_fs()).locate("/p/ws") == ProjectFact(group="ws")


def test_a_directory_in_no_repository_has_no_root_and_no_group() -> None:
    assert ProjectLocator(_fs()).locate("/p/bare/dir") == ProjectFact()


def test_a_relative_cwd_is_not_looked_up() -> None:
    fs = _fs()
    assert ProjectLocator(fs).locate("relative/dir") == ProjectFact()


def test_answers_are_remembered_until_forgotten() -> None:
    fs = _fs()
    locator = ProjectLocator(fs)
    assert locator.locate("/p/bare/dir") == ProjectFact()
    fs.add_dir("/p/bare/.git")
    assert locator.locate("/p/bare/dir") == ProjectFact()
    locator.forget()
    assert locator.locate("/p/bare/dir") == ProjectFact(git_root="/p/bare")


def test_an_unreadable_path_counts_as_absent() -> None:
    class _Raising(FakeFS):
        def exists(self, path: object) -> bool:
            raise PermissionError("denied")

    assert ProjectLocator(_Raising()).locate("/p/plain") == ProjectFact()


def test_the_cache_is_bounded() -> None:
    locator = ProjectLocator(_fs())
    for index in range(MAX_CACHE + 5):
        locator.locate(f"/p/bare/dir{index}")
    assert len(locator._cache) <= MAX_CACHE
