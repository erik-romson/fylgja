"""Configuration: defaults, merge, validation messages, raw collector tables."""

from __future__ import annotations

from pathlib import Path

import pytest

from fylgja import paths
from fylgja.config import ConfigError, deep_merge, load_config, load_defaults, parse_config


def _write(text: str) -> Path:
    target = paths.config_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def test_defaults_load_without_a_user_file() -> None:
    config = load_config()
    assert config.api.port == 7319
    assert config.api.allowed_origins
    assert config.actions.message.enabled is False
    assert config.actions.message.max_chars == 8000
    assert config.actions.applescript.chrome is False
    assert config.actions.applescript.iterm2 is False
    assert config.summaries.enabled is False
    assert (config.summaries.ttl_seconds, config.summaries.max_concurrent) == (300, 3)
    assert config.paths.aerospace == Path("/opt/homebrew/bin/aerospace")
    assert config.paths.git == Path("/usr/bin/git")
    assert config.privacy.redact_patterns
    assert config.privacy.ignore_bundle_ids
    assert config.privacy.ignore_title_patterns
    assert config.collectors == {
        "aerospace": {"enabled": True, "snapshot_interval_seconds": 30},
        "claude": {"enabled": True, "ignore_cwd": ["CodexBar/ClaudeProbe"]},
        "codex": {"enabled": True},
        "opencode": {"enabled": True},
        "norn": {"enabled": True, "roots": ["~/projects"], "max_depth": 3},
        "project": {"enabled": True, "poll_interval_seconds": 5},
        "presence": {
            "enabled": True,
            "idle_threshold_seconds": 300,
            "idle_poll_interval_seconds": 15,
            "poll_interval_seconds": 5,
        },
    }


def test_every_default_path_is_absolute() -> None:
    config = load_config()
    for value in (config.paths.aerospace, config.paths.gh, config.paths.git, config.paths.codexbar):
        assert value.is_absolute()


def test_user_file_overrides_and_merges_deeply() -> None:
    _write(
        """
[api]
port = 8123

[actions.message]
enabled = true

[privacy]
ignore_bundle_ids = ["com.example.secret"]
"""
    )
    config = load_config()
    assert config.api.port == 8123
    assert config.api.allowed_origins == load_config(Path("/nonexistent")).api.allowed_origins
    assert config.actions.message.enabled is True
    assert config.actions.message.max_chars == 8000
    assert config.actions.applescript.chrome is False
    assert config.privacy.ignore_bundle_ids == ["com.example.secret"]
    assert config.privacy.redact_patterns == load_defaults()["privacy"]["redact_patterns"]


def test_deep_merge_does_not_mutate_inputs() -> None:
    base = {"a": {"b": 1, "c": 2}, "d": [1]}
    override = {"a": {"b": 3}, "d": [2], "e": 5}
    assert deep_merge(base, override) == {"a": {"b": 3, "c": 2}, "d": [2], "e": 5}
    assert base == {"a": {"b": 1, "c": 2}, "d": [1]}


def test_unknown_key_names_file_and_key() -> None:
    target = _write("[api]\nprot = 1\n")
    with pytest.raises(ConfigError) as info:
        load_config()
    assert str(target) in str(info.value)
    assert "api.prot" in str(info.value)
    assert info.value.key == "api.prot"
    assert info.value.source == str(target)


def test_unknown_section_is_rejected() -> None:
    _write("[bogus]\nx = 1\n")
    with pytest.raises(ConfigError, match=r"bogus"):
        load_config()


def test_invalid_value_names_key() -> None:
    target = _write("[summaries]\nttl_seconds = 0\n")
    with pytest.raises(ConfigError) as info:
        load_config()
    assert info.value.key == "summaries.ttl_seconds"
    assert str(target) in str(info.value)


def test_invalid_regex_names_list_index() -> None:
    _write('[privacy]\nredact_patterns = ["ok", "("]\n')
    with pytest.raises(ConfigError) as info:
        load_config()
    assert info.value.key == "privacy.redact_patterns[1]"
    assert "regular expression" in str(info.value)


def test_relative_tool_path_is_rejected() -> None:
    _write('[paths]\ngh = "gh"\n')
    with pytest.raises(ConfigError, match="absolute"):
        load_config()


def test_wrong_type_is_rejected() -> None:
    _write('[api]\nport = "high"\n')
    with pytest.raises(ConfigError, match=r"api\.port"):
        load_config()


def test_several_errors_are_all_reported() -> None:
    _write("[api]\nport = 99999\nnope = 1\n")
    with pytest.raises(ConfigError) as info:
        load_config()
    text = str(info.value)
    assert "api.port" in text
    assert "api.nope" in text
    assert text.count("\n") == 1


def test_invalid_toml_names_file() -> None:
    target = _write("[api\n")
    with pytest.raises(ConfigError, match="invalid TOML") as info:
        load_config()
    assert str(target) in str(info.value)


def test_unreadable_file_is_a_config_error() -> None:
    target = paths.config_file()
    target.mkdir(parents=True)
    with pytest.raises(ConfigError, match="cannot read"):
        load_config()


def test_explicit_path_is_used(tmp_path: Path) -> None:
    other = tmp_path / "other.toml"
    other.write_text("[api]\nport = 9000\n", encoding="utf-8")
    assert load_config(other).api.port == 9000


def test_collector_tables_stay_raw() -> None:
    _write(
        """
[collectors.sample]
poll_seconds = 2
nested = { a = [1, 2] }

[collectors.vendor_thing]
enabled = true
anything = "goes"
"""
    )
    config = load_config()
    assert config.collectors["sample"] == {"poll_seconds": 2, "nested": {"a": [1, 2]}}
    assert config.collector_enabled("sample") is None
    assert config.collector_enabled("vendor_thing") is True
    assert config.collector_enabled("missing") is None
    assert config.collector_settings("vendor_thing") == {"anything": "goes"}
    assert config.collector_settings("missing") == {}


def test_collector_enabled_must_be_boolean() -> None:
    _write('[collectors.vendor_thing]\nenabled = "yes"\n')
    with pytest.raises(ConfigError, match=r"vendor_thing\.enabled"):
        load_config()


def test_defaults_table_errors_name_defaults_file() -> None:
    defaults = load_defaults()
    defaults["api"]["port"] = -1
    with pytest.raises(ConfigError) as info:
        parse_config({}, source="user.toml", defaults=defaults)
    assert info.value.source == "defaults.toml"


A2_EVENTS = [
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PermissionRequest",
    "PermissionDenied",
    "Notification",
    "Stop",
    "StopFailure",
    "SubagentStart",
    "SubagentStop",
    "TaskCreated",
    "TaskCompleted",
    "PreCompact",
    "PostCompact",
    "PostModelSwitch",
    "CwdChanged",
    "SessionEnd",
]


def test_default_hook_events_are_the_a2_set() -> None:
    events = load_config().hooks.events
    assert events == A2_EVENTS
    assert "MessageDisplay" not in events
    assert "FileChanged" not in events


def test_user_hook_events_replace_the_default_list() -> None:
    _write('[hooks]\nevents = ["Stop", "SessionEnd"]\n')
    assert load_config().hooks.events == ["Stop", "SessionEnd"]


@pytest.mark.parametrize(
    ("events", "fragment"),
    [('["stop"]', "not a hook event name"), ('["Stop", "Stop"]', "must not repeat")],
)
def test_bad_hook_events_are_refused(events: str, fragment: str) -> None:
    _write(f"[hooks]\nevents = {events}\n")
    with pytest.raises(ConfigError, match=fragment) as info:
        load_config()
    assert info.value.key == "hooks.events"
