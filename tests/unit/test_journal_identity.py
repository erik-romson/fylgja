"""Thread identity (J1): one checkout plus its current branch."""

from __future__ import annotations

from datetime import UTC, datetime

from fylgja.core.items import item_id
from fylgja.core.journal import ThreadKey, item_seq_id, thread_id, thread_key
from fylgja.store.rows import ProjectRow


def project(path: str, repo_root: str | None) -> ProjectRow:
    return ProjectRow(
        path=path,
        repo_root=repo_root,
        remote_url=None,
        repo_name=None,
        group_name=None,
        branch="main",
        dirty=None,
        ahead=None,
        behind=None,
        pr_number=None,
        pr_state=None,
        review_decision=None,
        build_state=None,
        build=None,
        build_observed_at=None,
        build_error_kind=None,
        last_commit_at=None,
        updated_at=datetime(2026, 10, 18, tzinfo=UTC),
    )


def identity(path: str, repo_root: str | None, branch: str | None) -> str | None:
    key = thread_key(project(path, repo_root), branch)
    return None if key is None else thread_id(key)


def test_two_clones_on_one_branch_are_two_threads() -> None:
    first = identity("/w/a/repo", "/w/a/repo", "main")
    second = identity("/w/b/repo", "/w/b/repo", "main")
    assert first is not None
    assert second is not None
    assert first != second


def test_a_worktree_has_its_own_thread() -> None:
    assert identity("/w/repo-wt", "/w/repo-wt", "main") != identity("/w/repo", "/w/repo", "main")


def test_one_root_on_two_branches_is_two_threads() -> None:
    assert identity("/w/repo", "/w/repo", "main") != identity("/w/repo", "/w/repo", "feature")


def test_no_branch_means_no_thread() -> None:
    assert thread_key(project("/w/repo", "/w/repo"), None) is None
    assert thread_key(project("/w/repo", "/w/repo"), "") is None


def test_a_path_that_is_not_a_checkout_has_no_thread() -> None:
    assert thread_key(project("/w/repo", None), "main") is None
    assert thread_key(project("/w/repo/sub", "/w/repo"), "main") is None


def test_the_key_and_the_ids() -> None:
    key = thread_key(project("/w/repo", "/w/repo"), "main")
    assert key == ThreadKey("/w/repo", "main")
    assert key is not None
    assert thread_id(key) == item_id("thread\0/w/repo\0main")
    assert len(thread_id(key)) == 16
    assert item_seq_id("abc", 3) == item_id("journal-item\0abc\x003")
    assert item_seq_id("abc", 3) != item_seq_id("abc", 4)
