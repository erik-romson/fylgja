"""Configuration: defaults, merge, validation messages, raw collector tables."""

from __future__ import annotations

from pathlib import Path

import pytest

from fylgja import paths
from fylgja.config import (
    ConfigError,
    NotifyConfig,
    deep_merge,
    load_config,
    load_defaults,
    parse_config,
)


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
    notify = config.notify
    assert notify.enabled is False
    assert notify.kinds == ["permission", "input"]
    assert (notify.grace_seconds, notify.reminder_seconds, notify.timeout_seconds) == (20, 600, 5)
    assert (notify.desk_command, notify.away_command) == ([], [])
    assert (notify.desk_detail, notify.away_detail) == ("project", "kind")
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
        "project": {
            "enabled": True,
            "poll_interval_seconds": 5,
            "state_interval_seconds": 300,
            "focus_refresh_seconds": 30,
            "active_window_seconds": 3600,
            "gh_calls_per_hour": 30,
            "gh_pr_calls_per_hour": 120,
            "usage_enabled": True,
            "usage_providers": ["codex", "copilot"],
            "cost_providers": ["codex", "claude"],
            "usage_interval_seconds": 300,
            "cost_interval_seconds": 900,
            "codexbar_timeout_seconds": 45,
        },
        "presence": {
            "enabled": True,
            "idle_threshold_seconds": 300,
            "idle_poll_interval_seconds": 15,
            "poll_interval_seconds": 5,
        },
        "appcontext": {
            "enabled": True,
            "poll_interval_seconds": 0.5,
            "chrome_history": False,
            "history_limit": 10,
            "history_window_seconds": 3600,
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


def test_the_span_idle_threshold_matches_the_presence_default_and_is_validated() -> None:
    config = load_config()
    assert config.spans.idle_threshold_seconds == 300
    assert (
        config.spans.idle_threshold_seconds
        == config.collectors["presence"]["idle_threshold_seconds"]
    )
    _write("[spans]\nidle_threshold_seconds = 120\n")
    assert load_config().spans.idle_threshold_seconds == 120
    _write("[spans]\nidle_threshold_seconds = 0\n")
    with pytest.raises(ConfigError) as info:
        load_config()
    assert info.value.key == "spans.idle_threshold_seconds"


def test_the_items_defaults_load() -> None:
    items = load_config().items
    assert items.ticket_prefixes == ["CBS"]
    assert items.default_ranker == "frecency"
    assert items.work_session_break_minutes == 30


def test_a_user_ticket_prefix_list_replaces_the_default() -> None:
    _write('[items]\nticket_prefixes = ["CBS", "WSS"]\n')
    items = load_config().items
    assert items.ticket_prefixes == ["CBS", "WSS"]
    assert items.default_ranker == "frecency"


def test_an_empty_ticket_prefix_list_is_valid() -> None:
    _write("[items]\nticket_prefixes = []\n")
    assert load_config().items.ticket_prefixes == []


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ('ticket_prefixes = ["cbs"]', "items.ticket_prefixes"),
        ('ticket_prefixes = ["CBS-"]', "items.ticket_prefixes"),
        ('ticket_prefixes = ["CBS", "CBS"]', "items.ticket_prefixes"),
        ("work_session_break_minutes = 0", "items.work_session_break_minutes"),
        ("work_session_break_minutes = 1441", "items.work_session_break_minutes"),
        ('default_ranker = "Bad Name"', "items.default_ranker"),
    ],
)
def test_bad_items_values_are_refused_by_key(text: str, key: str) -> None:
    _write(f"[items]\n{text}\n")
    with pytest.raises(ConfigError) as info:
        load_config()
    assert info.value.key.startswith(key)


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


def test_ranker_tables_stay_raw() -> None:
    _write(
        """
[rankers.workspace]
enabled = false

[rankers.mine]
enabled = true
place_weight = 2.0
nested = { a = [1, 2] }
"""
    )
    config = load_config()
    assert config.rankers["mine"] == {"enabled": True, "place_weight": 2.0, "nested": {"a": [1, 2]}}
    assert config.ranker_enabled("workspace") is False
    assert config.ranker_enabled("mine") is True
    assert config.ranker_enabled("missing") is None
    assert config.ranker_settings("mine") == {"place_weight": 2.0, "nested": {"a": [1, 2]}}
    assert config.ranker_settings("workspace") == {}
    assert config.ranker_settings("missing") == {}
    assert config.collectors.get("mine") is None


def test_ranker_enabled_must_be_boolean() -> None:
    _write('[rankers.mine]\nenabled = "yes"\n')
    with pytest.raises(ConfigError, match=r"mine\.enabled") as info:
        load_config()
    assert info.value.key == "rankers"


def test_a_ranker_table_must_be_a_table() -> None:
    _write("[rankers]\nmine = 3\n")
    with pytest.raises(ConfigError, match=r"rankers\.mine"):
        load_config()


def test_the_default_ranker_tables_are_frecency_and_recent_and_set_no_flag() -> None:
    config = load_config()
    assert config.ranker_enabled("workspace") is None
    assert config.ranker_settings("workspace") == {}
    assert config.ranker_enabled("frecency") is None
    assert config.ranker_settings("frecency") == {
        "half_life_active_minutes": 120,
        "last_session_weight": 1.0,
    }
    assert config.ranker_enabled("recent") is None
    assert config.ranker_settings("recent") == {"min_peek_seconds": 20}


def test_a_user_frecency_table_overrides_one_default_and_keeps_the_other() -> None:
    _write("[rankers.frecency]\nhalf_life_active_minutes = 60\n")
    assert load_config().ranker_settings("frecency") == {
        "half_life_active_minutes": 60,
        "last_session_weight": 1.0,
    }


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


def test_a_user_notify_kinds_list_replaces_the_default() -> None:
    _write('[notify]\nkinds = ["turn_done"]\n')
    assert load_config().notify.kinds == ["turn_done"]


def test_notify_kinds_match_the_waiting_kinds() -> None:
    from typing import get_args

    from fylgja.core.waiting import KINDS

    kinds_field = NotifyConfig.model_fields["kinds"].annotation
    (literal,) = get_args(kinds_field)
    assert get_args(literal) == KINDS


@pytest.mark.parametrize(
    ("body", "key"),
    [
        ('kinds = ["bogus"]', "notify.kinds"),
        ("kinds = []", "notify.kinds"),
        ('kinds = ["input", "input"]', "notify.kinds"),
        ('desk_command = ["terminal-notifier"]', "notify.desk_command"),
        ('away_command = ["notify"]', "notify.away_command"),
        ('desk_detail = "full"', "notify.desk_detail"),
        ('away_detail = "full"', "notify.away_detail"),
        ("grace_seconds = -1", "notify.grace_seconds"),
        ("reminder_seconds = -1", "notify.reminder_seconds"),
        ("timeout_seconds = 0", "notify.timeout_seconds"),
        ("timeout_seconds = 61", "notify.timeout_seconds"),
        ("foo = 1", "notify.foo"),
    ],
)
def test_bad_notify_settings_are_refused(body: str, key: str) -> None:
    _write(f"[notify]\n{body}\n")
    with pytest.raises(ConfigError) as info:
        load_config()
    assert key in info.value.key


def test_a_notify_error_never_echoes_an_argv_element() -> None:
    _write('[notify]\naway_command = ["notify", "https://ntfy.sh/secret-topic-abc"]\n')
    with pytest.raises(ConfigError) as info:
        load_config()
    assert "secret-topic-abc" not in str(info.value)
    assert "secret-topic-abc" not in info.value.reason


def test_a_relative_command_error_never_echoes_it() -> None:
    _write('[notify]\ndesk_command = ["secret-topic-abc"]\n')
    with pytest.raises(ConfigError) as info:
        load_config()
    assert info.value.key == "notify.desk_command"
    assert "secret-topic-abc" not in str(info.value)


def test_enabled_notify_needs_a_command() -> None:
    _write("[notify]\nenabled = true\n")
    with pytest.raises(ConfigError, match="enabled needs desk_command or away_command") as info:
        load_config()
    assert info.value.key == "notify"


def test_enabled_notify_with_one_command_loads() -> None:
    _write('[notify]\nenabled = true\naway_command = ["/usr/bin/curl", "-s", "{message}"]\n')
    notify = load_config().notify
    assert notify.enabled is True
    assert notify.away_command == ["/usr/bin/curl", "-s", "{message}"]


def test_zero_reminder_seconds_is_valid() -> None:
    _write("[notify]\nreminder_seconds = 0\n")
    assert load_config().notify.reminder_seconds == 0
