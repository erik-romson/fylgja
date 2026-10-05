"""A16 path defaults and overrides; no I/O on import."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from fylgja import paths


def test_environment_overrides_decide_every_location(isolated_env: Path) -> None:
    config, data, state = isolated_env / "config", isolated_env / "data", isolated_env / "state"
    assert paths.config_dir() == config
    assert paths.config_file() == config / "config.toml"
    assert paths.api_secret_file() == config / "api-secret"
    assert paths.database_file() == data / "fylgja.db"
    assert paths.hook_inbox_root() == data / "hook-inbox"
    assert paths.inbox_dir("claude") == data / "hook-inbox" / "claude"
    assert paths.quarantine_dir("claude") == data / "hook-inbox" / "claude" / "quarantine"
    assert paths.statusline_dir() == data / "statusline"
    assert paths.log_file() == state / "fylgja.log"
    assert paths.summarizer_dir() == state / "summarizer"
    assert paths.backups_dir() == state / "backups"


def test_defaults_live_under_home(monkeypatch: pytest.MonkeyPatch, isolated_env: Path) -> None:
    for variable in (paths.CONFIG_DIR_ENV, paths.DATA_DIR_ENV, paths.STATE_DIR_ENV):
        monkeypatch.delenv(variable)
    home = isolated_env / "home"
    assert paths.config_file() == home / ".config/fylgja/config.toml"
    assert paths.api_secret_file() == home / ".config/fylgja/api-secret"
    assert paths.database_file() == home / ".local/share/fylgja/fylgja.db"
    assert paths.inbox_dir("claude") == home / ".local/share/fylgja/hook-inbox/claude"
    assert paths.statusline_dir() == home / ".local/share/fylgja/statusline"
    assert paths.log_file() == home / ".local/state/fylgja/fylgja.log"
    assert paths.summarizer_dir() == home / ".local/state/fylgja/summarizer"
    assert paths.backups_dir() == home / ".local/state/fylgja/backups"


def test_empty_override_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, isolated_env: Path
) -> None:
    monkeypatch.setenv(paths.DATA_DIR_ENV, "")
    assert paths.data_dir() == isolated_env / "home" / ".local/share/fylgja"


def test_override_expands_tilde(monkeypatch: pytest.MonkeyPatch, isolated_env: Path) -> None:
    monkeypatch.setenv(paths.STATE_DIR_ENV, "~/elsewhere")
    assert paths.state_dir() == isolated_env / "home" / "elsewhere"


def test_overrides_are_read_on_every_call(
    monkeypatch: pytest.MonkeyPatch, isolated_env: Path
) -> None:
    first = paths.database_file()
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(isolated_env / "other"))
    assert paths.database_file() == isolated_env / "other" / "fylgja.db"
    assert first != paths.database_file()


@pytest.mark.parametrize("source", ["", ".", "..", "../x", "a/b", "-x", "a b"])
def test_inbox_rejects_unsafe_source_names(source: str) -> None:
    with pytest.raises(ValueError, match="inbox source"):
        paths.inbox_dir(source)
    with pytest.raises(ValueError, match="inbox source"):
        paths.quarantine_dir(source)


def test_functions_create_nothing(isolated_env: Path) -> None:
    before = sorted(isolated_env.rglob("*"))
    paths.database_file()
    paths.inbox_dir("claude")
    paths.quarantine_dir("claude")
    paths.backups_dir()
    assert sorted(isolated_env.rglob("*")) == before
    assert not paths.hook_inbox_root().exists()


def test_import_creates_nothing(tmp_path: Path) -> None:
    probe = (
        "import os, sys\n"
        f"os.environ['HOME'] = {str(tmp_path / 'home')!r}\n"
        "for v in ('FYLGJA_CONFIG_DIR','FYLGJA_DATA_DIR','FYLGJA_STATE_DIR'):\n"
        "    os.environ.pop(v, None)\n"
        "import fylgja.paths\n"
    )
    (tmp_path / "home").mkdir()
    subprocess.run([sys.executable, "-c", probe], check=True, timeout=25)
    assert list((tmp_path / "home").iterdir()) == []
