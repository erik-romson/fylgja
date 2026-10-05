"""``scripts/sanitize_fixture.py`` replaces what the hygiene test forbids."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "sanitize_fixture.py"


@pytest.fixture(scope="module")
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sanitize_fixture", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["sanitize_fixture"] = module
    spec.loader.exec_module(module)
    return module


def _sanitizer(script: ModuleType) -> object:
    return script.Sanitizer(["corp.example"], ["Acme", "Beta Co"], "alice")


def test_text_replacements(script: ModuleType) -> None:
    sanitizer = _sanitizer(script)
    text = (
        "cwd /Users/alice/work and /Users/bob/x, slug -Users-alice-work, mail a.b@corp.example, "
        "host jira.corp.example, ACME and beta co, "
        "CLAUDE_CODE_MESSAGING_TOKEN=s3cr3t, key sk-" + "a" * 24 + ", Bearer " + "b" * 20
    )
    result = sanitizer.text(text)  # type: ignore[attr-defined]
    assert "/Users/user/work" in result
    assert "/Users/user/x" in result
    assert "-Users-user-work" in result
    assert "alice" not in result
    assert "user@example.com" in result
    assert "host example.com" in result
    assert "customer-1" in result
    assert "customer-2" in result
    assert "s3cr3t" not in result
    assert "<redacted-secret-env>" in result
    assert result.count("<redacted-token>") == 2


def test_shared_users_directory_is_kept(script: ModuleType) -> None:
    sanitizer = script.Sanitizer([], [], "")
    assert sanitizer.text("/Users/Shared/x") == "/Users/Shared/x"


def test_sanitize_tree_drops_keys_keeps_modes_and_marks_empty_dirs(
    script: ModuleType, tmp_path: Path
) -> None:
    source = tmp_path / "in"
    (source / "a" / "empty").mkdir(parents=True)
    (source / "a" / "run.sh").write_text("echo /Users/alice\n", encoding="utf-8")
    (source / "a" / "run.sh").chmod(0o755)
    (source / "a" / "1.key").write_text("secret", encoding="utf-8")
    (source / "a" / "blob.bin").write_bytes(b"\xff\xfe\x00")
    (source / ".DS_Store").write_text("x", encoding="utf-8")
    target = tmp_path / "out"
    written, dropped = script.sanitize_tree(source, target, _sanitizer(script))
    assert (written, dropped) == (2, 2)
    assert (target / "a" / "run.sh").read_text(encoding="utf-8") == "echo /Users/user\n"
    assert (target / "a" / "run.sh").stat().st_mode & 0o777 == 0o755
    assert not (target / "a" / "1.key").exists()
    assert (target / "a" / "blob.bin").read_bytes() == b"\xff\xfe\x00"
    assert (target / "a" / "empty" / ".gitkeep").exists()


def test_main_reports_a_missing_source(script: ModuleType, tmp_path: Path) -> None:
    status = script.main(
        [
            "nothing",
            "--staging",
            str(tmp_path),
            "--out",
            str(tmp_path / "o"),
            "--denylist",
            str(tmp_path / "none.txt"),
        ]
    )
    assert status == 1


def test_main_copies_and_reads_the_denylist(script: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "stage" / "x").mkdir(parents=True)
    (tmp_path / "stage" / "x" / "f.txt").write_text("Acme at /Users/alice\n", encoding="utf-8")
    deny = tmp_path / "deny.txt"
    deny.write_text("# comment\n\nAcme\n", encoding="utf-8")
    status = script.main(
        [
            "x",
            "--staging",
            str(tmp_path / "stage"),
            "--out",
            str(tmp_path / "out"),
            "--denylist",
            str(deny),
            "--home-name",
            "alice",
        ]
    )
    assert status == 0
    assert (tmp_path / "out" / "x" / "f.txt").read_text(encoding="utf-8") == (
        "customer-1 at /Users/user\n"
    )


def test_missing_denylist_means_no_names(script: ModuleType, tmp_path: Path) -> None:
    assert script.read_denylist(tmp_path / "nope.txt") == []
