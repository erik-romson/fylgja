"""Typed configuration (``PLAN-final-plan.md`` §4.3).

The packaged ``defaults.toml`` is deep-merged under the user's ``config.toml`` and validated with
Pydantic. ``[collectors.<name>]`` tables stay raw here: the registry validates each one with that
plugin's ``config_model``. ``enabled`` in such a table belongs to the core.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Final

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from fylgja import paths

__all__ = [
    "LOOPBACK_HOST",
    "ActionsConfig",
    "ApiConfig",
    "Config",
    "ConfigError",
    "HooksConfig",
    "ItemsConfig",
    "PathsConfig",
    "PrivacyConfig",
    "SummariesConfig",
    "deep_merge",
    "load_config",
    "load_defaults",
    "parse_config",
]


class ConfigError(Exception):
    """The configuration is invalid. The message names the file and the key."""

    def __init__(
        self, source: Path | str, key: str, reason: str, *, message: str | None = None
    ) -> None:
        self.source = str(source)
        self.key = key
        self.reason = reason
        text = f"{self.source}: {key}: {reason}" if key else f"{self.source}: {reason}"
        super().__init__(message or text)


def _regex(value: str) -> str:
    try:
        re.compile(value)
    except re.error as exc:
        raise ValueError(f"invalid regular expression: {exc}") from exc
    return value


def _absolute(value: Path) -> Path:
    if not value.is_absolute():
        raise ValueError("must be an absolute path")
    return value


def _loopback(value: str) -> str:
    if value != LOOPBACK_HOST:
        raise ValueError(f"must be {LOOPBACK_HOST}: the API binds to the loopback address only")
    return value


Pattern = Annotated[str, AfterValidator(_regex)]
AbsolutePath = Annotated[Path, AfterValidator(_absolute)]

LOOPBACK_HOST: Final = "127.0.0.1"
"""The only address the API binds to (``PLAN.md`` §6, A6)."""


class _Section(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ApiConfig(_Section):
    host: Annotated[str, AfterValidator(_loopback)]
    """Always ``127.0.0.1``; validation refuses any other value."""
    port: int = Field(ge=0, le=65535)
    allowed_origins: list[str]


class PrivacyConfig(_Section):
    redact_patterns: list[Pattern]
    ignore_bundle_ids: list[str]
    ignore_title_patterns: list[Pattern]


class MessageActionConfig(_Section):
    enabled: bool
    max_chars: int = Field(gt=0)


class AppleScriptActionConfig(_Section):
    chrome: bool
    iterm2: bool


class ActionsConfig(_Section):
    message: MessageActionConfig
    applescript: AppleScriptActionConfig


class SummariesConfig(_Section):
    enabled: bool
    ttl_seconds: int = Field(gt=0)
    max_concurrent: int = Field(gt=0)


class SpansConfig(_Section):
    idle_threshold_seconds: float = Field(gt=0)
    """The idle threshold of the presence signal. An ``idle.start`` event is stamped when the
    threshold was crossed, so the idle period began this many seconds earlier and a focus span
    ends there. Keep it equal to the presence collector's ``idle_threshold_seconds``."""


_PREFIX = re.compile(r"[A-Z][A-Z0-9]{0,9}")
_RANKER_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")


class ItemsConfig(_Section):
    ticket_prefixes: list[str]
    """Ticket key prefixes: a key is ``<PREFIX>-<digits>``. An empty list turns detection off."""
    default_ranker: str
    """The ranker used when a request names none. Whether it is loaded is checked at runtime."""
    work_session_break_minutes: float = Field(gt=0, le=1440)
    """A not-working gap this long splits two work sessions."""

    @field_validator("ticket_prefixes")
    @classmethod
    def _prefixes(cls, value: list[str]) -> list[str]:
        for prefix in value:
            if _PREFIX.fullmatch(prefix) is None:
                raise ValueError(
                    f"{prefix!r} is not a ticket prefix (A-Z, then A-Z or 0-9, up to 10)"
                )
        if len(set(value)) != len(value):
            raise ValueError("ticket prefixes must not repeat")
        return value

    @field_validator("default_ranker")
    @classmethod
    def _ranker_name(cls, value: str) -> str:
        if _RANKER_NAME.fullmatch(value) is None:
            raise ValueError(f"{value!r} is not a ranker name")
        return value


_HOOK_EVENT = re.compile(r"[A-Z][A-Za-z]*")


class HooksConfig(_Section):
    events: list[str]
    """The Claude Code hook events the installer registers (A2)."""

    @field_validator("events")
    @classmethod
    def _event_names(cls, value: list[str]) -> list[str]:
        for event in value:
            if _HOOK_EVENT.fullmatch(event) is None:
                raise ValueError(f"{event!r} is not a hook event name")
        if len(set(value)) != len(value):
            raise ValueError("hook events must not repeat")
        return value


class PathsConfig(_Section):
    aerospace: AbsolutePath
    gh: AbsolutePath
    git: AbsolutePath
    codexbar: AbsolutePath


class Config(_Section):
    api: ApiConfig
    privacy: PrivacyConfig
    actions: ActionsConfig
    summaries: SummariesConfig
    hooks: HooksConfig
    paths: PathsConfig
    spans: SpansConfig = SpansConfig(idle_threshold_seconds=300.0)
    items: ItemsConfig = ItemsConfig(
        ticket_prefixes=["CBS"], default_ranker="frecency", work_session_break_minutes=30
    )
    collectors: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("collectors")
    @classmethod
    def _enabled_is_boolean(cls, value: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        for name, table in value.items():
            if "enabled" in table and not isinstance(table["enabled"], bool):
                raise ValueError(f"{name}.enabled must be true or false")
        return value

    def collector_enabled(self, name: str) -> bool | None:
        """The explicit ``enabled`` flag of ``[collectors.<name>]``, or ``None`` when absent."""
        flag = self.collectors.get(name, {}).get("enabled")
        return flag if isinstance(flag, bool) else None

    def collector_settings(self, name: str) -> dict[str, Any]:
        """The raw ``[collectors.<name>]`` table without the core-owned ``enabled`` key."""
        return {k: v for k, v in self.collectors.get(name, {}).items() if k != "enabled"}


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Merge tables key by key; any other value in ``override`` replaces the one in ``base``."""
    merged: dict[str, Any] = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def load_defaults() -> dict[str, Any]:
    """Parse the packaged ``defaults.toml``."""
    text = resources.files("fylgja").joinpath("defaults.toml").read_text(encoding="utf-8")
    return tomllib.loads(text)


def _format_loc(loc: tuple[int | str, ...]) -> str:
    key = ""
    for part in loc:
        key += f"[{part}]" if isinstance(part, int) else (f".{part}" if key else part)
    return key


def _present(data: Any, loc: tuple[int | str, ...]) -> bool:
    node = data
    for part in loc:
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        elif isinstance(node, list) and isinstance(part, int) and part < len(node):
            node = node[part]
        else:
            return False
    return True


def parse_config(
    user_data: Mapping[str, Any], *, source: Path | str, defaults: Mapping[str, Any] | None = None
) -> Config:
    """Validate ``user_data`` merged over the defaults. ``source`` names the user's file."""
    base = load_defaults() if defaults is None else defaults
    try:
        return Config.model_validate(deep_merge(base, user_data))
    except ValidationError as exc:
        errors = [
            ConfigError(
                source if _present(user_data, error["loc"]) else "defaults.toml",
                _format_loc(error["loc"]),
                error["msg"],
            )
            for error in exc.errors(include_url=False, include_input=False)
        ]
        first = errors[0]
        if len(errors) == 1:
            raise first from None
        raise ConfigError(
            first.source, first.key, first.reason, message="\n".join(str(e) for e in errors)
        ) from None


def load_config(path: Path | None = None) -> Config:
    """Load ``config.toml`` (default: ``paths.config_file()``) over the packaged defaults.

    A missing file means defaults only.
    """
    target = paths.config_file() if path is None else path
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return parse_config({}, source=target)
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(target, "", f"cannot read file: {exc}") from exc
    try:
        user_data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(target, "", f"invalid TOML: {exc}") from exc
    return parse_config(user_data, source=target)
