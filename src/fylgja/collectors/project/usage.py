"""Provider quota and spend from the CodexBar CLI (WP5.4, §4.7).

``usage.providers[]`` and ``usage.cost`` of ``/now`` have two sources. **Claude's** rate limits
come from the statusline snapshot, which the Claude collector reads (:mod:`fylgja.collectors.claude.
statusline`: ``rate_limits.five_hour`` and ``seven_day``, a used percentage and a reset time in
epoch seconds). **Everything else** comes from here: the project collector asks the CodexBar CLI on
a slow timer (``[paths] codexbar``; §3.1 lists no separate usage collector):

* ``codexbar usage --provider <p> --json`` for each of ``usage_providers`` (default ``codex`` and
  ``copilot``), every ``usage_interval_seconds`` (5 min). A provider's ``usage`` object has up to
  three windows, ``primary``, ``secondary`` and ``tertiary``, each with ``usedPercent``,
  ``windowMinutes`` and ``resetsAt``. A window is named by its length: 300 minutes is
  ``five_hour`` and 10080 is ``seven_day``, the names the statusline uses, any other length
  ``<n>m``, and a window without a length keeps its slot name.
* ``codexbar cost --provider <p> --json`` for each of ``cost_providers`` (default ``codex`` and
  ``claude``), every ``cost_interval_seconds`` (15 min). It scans the local agent logs, so it needs
  no login. ``last30DaysCostUSD`` is the spend, period ``30d``. The sum over the providers is
  ``usage.cost``; CodexBar prices at list price, so it is an estimate.

CodexBar exits non-zero when any provider it asked fails, and still prints the JSON array, so the
output is read whatever the exit status. An entry with an ``error`` object, a missing binary, a
timeout and unparsable output are each a **failed attempt** with a short code (``codexbar_missing``,
``codexbar_timeout``, ``codexbar_not_authenticated``, ...); no error text is ever kept. A failed
attempt leaves the provider's last reading in place and marks it ``unavailable`` in ``/now``. It
does not degrade the collector, as an absent ``gh`` does not.

:class:`UsageEngine` is driven by the collector. Each :meth:`UsageEngine.poll` runs what is due and
returns one **complete** ``UsageSnapshot`` of everything it knows (readings and failures), because a
complete snapshot is what closes a row that is no longer reported. Nothing here touches the store,
and nothing about a provider's account is read or logged: the CLI's ``accountEmail``, credits and
reset-credit ids are never looked at.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from fylgja.collectors.base import Clock
from fylgja.collectors.seams import ProcessRunner
from fylgja.config import ConfigError, load_config, load_defaults
from fylgja.models import Record, SnapshotItem, UsagePayload, UsageSnapshot

__all__ = [
    "CODEXBAR_SOURCE",
    "COST_PERIOD",
    "COST_PERIOD_DAYS",
    "Failure",
    "ProviderCost",
    "ProviderUsage",
    "UsageEngine",
    "UsageParseError",
    "UsageSettings",
    "Window",
    "cost_argv",
    "parse_cost",
    "parse_usage",
    "resolve_binary",
    "usage_argv",
    "window_name",
]

CODEXBAR_SOURCE: Final = "codexbar"
SOURCE: Final = "project"
"""The collector the records are emitted as (the engine lives in the project collector)."""
COST_PERIOD: Final = "30d"
COST_PERIOD_DAYS: Final = 30
SLOT_NAMES: Final = ("primary", "secondary", "tertiary")
MAX_PROVIDERS: Final = 16

CODEXBAR_MISSING: Final = "codexbar_missing"
CODEXBAR_TIMEOUT: Final = "codexbar_timeout"
CODEXBAR_FAILED: Final = "codexbar_failed"
CODEXBAR_PARSE: Final = "codexbar_parse"
CODEXBAR_NO_DATA: Final = "codexbar_no_data"
CODEXBAR_UNSUPPORTED: Final = "codexbar_unsupported"
CODEXBAR_NO_STRATEGY: Final = "codexbar_no_strategy"
CODEXBAR_NOT_AUTHENTICATED: Final = "codexbar_not_authenticated"
CODEXBAR_PROVIDER_ERROR: Final = "codexbar_provider_error"

_WINDOW_NAMES: Final = {300: "five_hour", 10080: "seven_day"}

# Phrases of CodexBar's error messages, matched in lower case. The message itself is dropped.
_ERROR_PHRASES: Final = (
    (("not supported",), CODEXBAR_UNSUPPORTED),
    (("no available fetch strategy",), CODEXBAR_NO_STRATEGY),
    (
        (
            "credentials not found",
            "not found",
            "log in",
            "login",
            "sign in",
            "session",
            "unauthorized",
            "authenticate",
            "auth ",
            "token",
        ),
        CODEXBAR_NOT_AUTHENTICATED,
    ),
    (("timed out", "timeout"), CODEXBAR_TIMEOUT),
)


class UsageSettings(BaseModel):
    """The CodexBar part of ``[collectors.project]``."""

    model_config = ConfigDict(extra="forbid")

    usage_enabled: bool = True
    """Ask CodexBar at all. Off, ``/now`` has the Claude statusline's rate limits only."""
    codexbar_command: str | None = None
    """The CLI; default ``[paths] codexbar``."""
    usage_providers: list[str] = Field(default_factory=lambda: ["codex", "copilot"])
    """Providers asked for their quota windows (``codexbar usage --provider``)."""
    cost_providers: list[str] = Field(default_factory=lambda: ["codex", "claude"])
    """Providers asked for their local spend (``codexbar cost --provider``)."""
    usage_interval_seconds: float = Field(default=300.0, gt=0)
    cost_interval_seconds: float = Field(default=900.0, gt=0)
    codexbar_timeout_seconds: float = Field(default=45.0, gt=0)


class UsageParseError(Exception):
    """CodexBar's output is not what it documents. ``kind`` is a short code."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True)
class Window:
    """One quota window of a provider."""

    name: str
    used_pct: float | None
    resets_at: datetime | None


@dataclass(frozen=True)
class ProviderUsage:
    provider: str
    windows: tuple[Window, ...]


@dataclass(frozen=True)
class ProviderCost:
    provider: str
    usd: float
    period: str = COST_PERIOD


@dataclass(frozen=True)
class Failure:
    """The attempt for a provider failed; ``kind`` is a short code."""

    provider: str
    kind: str


# --------------------------------------------------------------------------------------------
# Command lines
# --------------------------------------------------------------------------------------------


def usage_argv(command: str, provider: str) -> tuple[str, ...]:
    return (command, "usage", "--provider", provider, "--json", "--no-color")


def cost_argv(command: str, provider: str) -> tuple[str, ...]:
    return (command, "cost", "--provider", provider, "--json", "--no-color")


# --------------------------------------------------------------------------------------------
# Parsing (pure)
# --------------------------------------------------------------------------------------------


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def window_name(slot: str, minutes: object) -> str:
    """The name of a window: by its length when known (``five_hour``, ``seven_day``, ``<n>m``),
    else the slot's own name."""
    length = _number(minutes)
    if length is None or length <= 0 or length != int(length):
        return slot
    return _WINDOW_NAMES.get(int(length), f"{int(length)}m")


def classify_error(error: object) -> str:
    """A short code for a CodexBar ``error`` object. The message is only matched, never kept."""
    message = error.get("message") if isinstance(error, Mapping) else None
    text = message.lower() if isinstance(message, str) else ""
    for phrases, kind in _ERROR_PHRASES:
        if any(phrase in text for phrase in phrases):
            return kind
    return CODEXBAR_PROVIDER_ERROR


def _entries(stdout: bytes) -> list[Mapping[str, Any]]:
    try:
        data = json.loads(stdout)
    except (ValueError, RecursionError) as exc:
        raise UsageParseError(CODEXBAR_PARSE) from exc
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise UsageParseError(CODEXBAR_PARSE)
    return [entry for entry in data if isinstance(entry, dict)]


def _provider_of(entry: Mapping[str, Any]) -> str | None:
    provider = entry.get("provider")
    return provider.lower() if isinstance(provider, str) and provider else None


def _windows(usage: Mapping[str, Any]) -> tuple[Window, ...]:
    found: list[tuple[str, str, float | None, datetime | None]] = []
    for slot in SLOT_NAMES:
        entry = usage.get(slot)
        if not isinstance(entry, Mapping):
            continue
        used = _number(entry.get("usedPercent"))
        resets = _time(entry.get("resetsAt"))
        if used is not None or resets is not None:
            found.append((slot, window_name(slot, entry.get("windowMinutes")), used, resets))
    names = [name for _, name, _, _ in found]
    if len(set(names)) != len(names):  # two slots of one length: keep the slot names
        names = [slot for slot, _, _, _ in found]
    return tuple(
        Window(name, used, resets) for name, (_, _, used, resets) in zip(names, found, strict=True)
    )


def parse_usage(stdout: bytes, provider: str) -> ProviderUsage | Failure:
    """The quota windows of ``provider`` in ``codexbar usage --json`` output.

    Raises :class:`UsageParseError` when the output is not a JSON array of objects. An entry with
    an ``error`` object, no ``usage`` object or no window is a :class:`Failure`.
    """
    for entry in _entries(stdout):
        if _provider_of(entry) != provider:
            continue
        if "error" in entry and entry.get("usage") is None:
            return Failure(provider, classify_error(entry["error"]))
        usage = entry.get("usage")
        if not isinstance(usage, Mapping):
            return Failure(provider, CODEXBAR_NO_DATA)
        windows = _windows(usage)
        if not windows:
            return Failure(provider, CODEXBAR_NO_DATA)
        return ProviderUsage(provider, windows)
    return Failure(provider, CODEXBAR_NO_DATA)


def parse_cost(stdout: bytes, provider: str) -> ProviderCost | Failure:
    """The 30-day spend of ``provider`` in ``codexbar cost --json`` output (see
    :func:`parse_usage` for the failures)."""
    for entry in _entries(stdout):
        if _provider_of(entry) != provider:
            continue
        if "error" in entry and entry.get("last30DaysCostUSD") is None:
            return Failure(provider, classify_error(entry["error"]))
        usd = _number(entry.get("last30DaysCostUSD"))
        if usd is None or usd < 0:
            return Failure(provider, CODEXBAR_NO_DATA)
        return ProviderCost(provider, usd)
    return Failure(provider, CODEXBAR_NO_DATA)


# --------------------------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------------------------


def resolve_binary() -> str:
    """The CLI from ``[paths] codexbar``; an invalid config falls back to the defaults."""
    try:
        return str(load_config().paths.codexbar)
    except ConfigError:
        return str(load_defaults()["paths"]["codexbar"])


def _unique(names: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for name in names:
        lowered = name.strip().lower()
        if lowered and lowered not in seen and len(seen) < MAX_PROVIDERS:
            seen[lowered] = None
    return list(seen)


class UsageEngine:
    """Runs CodexBar on its timers and turns the answers into one complete usage snapshot."""

    def __init__(self, settings: UsageSettings, *, clock: Clock, runner: ProcessRunner) -> None:
        self._settings = settings
        self._clock = clock
        self._runner = runner
        self._command = settings.codexbar_command or "codexbar"
        self._usage_providers = _unique(settings.usage_providers)
        self._cost_providers = _unique(settings.cost_providers)
        self._usage_next = 0.0
        self._cost_next = 0.0
        self._usage: dict[str, ProviderUsage | Failure] = {}
        self._cost: dict[str, ProviderCost | Failure] = {}
        self.polled = False
        """At least one attempt has been made."""
        self.calls = 0
        """CodexBar processes started."""

    @property
    def enabled(self) -> bool:
        return self._settings.usage_enabled and bool(self._usage_providers or self._cost_providers)

    @property
    def failures(self) -> dict[str, str]:
        """The failure code of each failed attempt, keyed ``usage:<provider>`` or
        ``cost:<provider>``."""
        found = {f"usage:{p}": r.kind for p, r in self._usage.items() if isinstance(r, Failure)}
        found.update({f"cost:{p}": r.kind for p, r in self._cost.items() if isinstance(r, Failure)})
        return found

    def summary(self) -> str:
        """One clause for the collector's health detail: what ``usage`` has, or why it has not."""
        if not self.enabled:
            return "usage: disabled"
        if not self.polled:
            return "usage: no data yet (CodexBar not asked yet)"
        windows = sum(len(r.windows) for r in self._usage.values() if isinstance(r, ProviderUsage))
        spend = sum(1 for r in self._cost.values() if isinstance(r, ProviderCost))
        text = f"usage: {windows} quota windows, {spend} cost providers"
        failed = self.failures
        if failed:
            text += f"; CodexBar unavailable ({', '.join(sorted(set(failed.values())))})"
        return text

    async def poll(self) -> list[Record]:
        """Run what is due. Returns the complete snapshot when anything ran, else nothing."""
        if not self.enabled:
            return []
        mono = self._clock.monotonic()
        ran = False
        if self._usage_providers and mono >= self._usage_next:
            self._usage_next = mono + self._settings.usage_interval_seconds
            for provider in self._usage_providers:
                self._usage[provider] = await self._ask(
                    usage_argv(self._command, provider), provider, parse_usage
                )
            ran = True
        if self._cost_providers and mono >= self._cost_next:
            self._cost_next = mono + self._settings.cost_interval_seconds
            for provider in self._cost_providers:
                self._cost[provider] = await self._ask(
                    cost_argv(self._command, provider), provider, parse_cost
                )
            ran = True
        if not ran:
            return []
        self.polled = True
        return [self.snapshot(self._clock.now())]

    async def _ask[R](
        self,
        argv: tuple[str, ...],
        provider: str,
        parse: Callable[[bytes, str], R | Failure],
    ) -> R | Failure:
        self.calls += 1
        try:
            result = await self._runner.run(argv, timeout=self._settings.codexbar_timeout_seconds)
        except FileNotFoundError:
            return Failure(provider, CODEXBAR_MISSING)
        except TimeoutError:
            return Failure(provider, CODEXBAR_TIMEOUT)
        except OSError:
            return Failure(provider, CODEXBAR_FAILED)
        try:
            parsed: R | Failure = parse(result.stdout, provider)
        except UsageParseError as exc:
            return Failure(provider, exc.kind if result.returncode == 0 else CODEXBAR_FAILED)
        return parsed

    def snapshot(self, at: datetime) -> UsageSnapshot:
        """Everything known, as one complete snapshot: a row missing from it is closed."""
        items: list[SnapshotItem[UsagePayload]] = []
        usage_interval = self._settings.usage_interval_seconds
        cost_interval = self._settings.cost_interval_seconds
        for provider, usage in sorted(self._usage.items()):
            if isinstance(usage, Failure):
                items.append(
                    _item(
                        f"{provider}:usage",
                        UsagePayload(
                            provider=provider,
                            usage_source=CODEXBAR_SOURCE,
                            poll_interval_s=usage_interval,
                            error_kind=usage.kind,
                        ),
                    )
                )
                continue
            for window in usage.windows:
                items.append(
                    _item(
                        f"{provider}:{window.name}",
                        UsagePayload(
                            provider=provider,
                            window=window.name,
                            used_pct=window.used_pct,
                            resets_at=window.resets_at,
                            usage_source=CODEXBAR_SOURCE,
                            poll_interval_s=usage_interval,
                        ),
                    )
                )
        for provider, cost in sorted(self._cost.items()):
            if isinstance(cost, Failure):
                payload = UsagePayload(
                    provider=provider,
                    cost_period=COST_PERIOD,
                    usage_source=CODEXBAR_SOURCE,
                    poll_interval_s=cost_interval,
                    error_kind=cost.kind,
                )
            else:
                payload = UsagePayload(
                    provider=provider,
                    cost_usd=cost.usd,
                    cost_period=cost.period,
                    usage_source=CODEXBAR_SOURCE,
                    poll_interval_s=cost_interval,
                )
            items.append(_item(f"{provider}:cost", payload))
        return UsageSnapshot(
            source=SOURCE,
            entity_id=CODEXBAR_SOURCE,
            observed_at=at,
            complete=True,
            items=tuple(items),
        )


def _item(entity_id: str, payload: UsagePayload) -> SnapshotItem[UsagePayload]:
    return SnapshotItem(entity_id=entity_id, payload=payload)
