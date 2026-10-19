"""The ``appcontext`` collector (``PLAN-final-plan.md`` WP5.3; ``PLAN.md`` §4.7 and §11).

One loop polls the store's open windows and latest focus every ``poll_interval_seconds`` (0.5 s,
so context follows focus within a second) and works in two tiers:

* **Tier 1, on every window snapshot.** Each window's title goes through
  :func:`~fylgja.collectors.appcontext.titles.parse_title` and becomes a ``title`` context item
  attached to the window. An item is emitted when it differs from the last one emitted for that
  window, and withdrawn when the title stops parsing. The windows come from the store, so the
  titles are already redacted. **JetBrains** windows also get their project path here: from the
  title's ``[path]`` or from a cached ``recentProjects.xml`` (re-stat at most every 30 s, re-parsed
  only when its location or mtime changes), so every IDE window has a path, not only the focused
  one.
* **Tier 2, on a focus change only.** A new focus event on a window that is open costs one run of
  that app's second tier: one file read per source, and one ``osascript`` call per switch that is
  on. Nothing runs between focus changes.

  ============ ===================================================================
  cmux         the selected workspace and tab with its cwd (cmux's session store)
  Chrome       the active tab URL (AppleScript, ``[actions.applescript] chrome``);
               optional history backfill (``[collectors.appcontext] chrome_history``)
  JetBrains    the project path again, with a forced re-stat of ``recentProjects.xml``
  Obsidian     the vault path (``obsidian.json``), the note and an ``obsidian://`` link
  iTerm2       ``path`` and ``jobName`` (AppleScript, ``[actions.applescript] iterm2``)
  ============ ===================================================================

**Failures are per source.** A source that fails is named in :meth:`AppContextCollector.doctor`
and logged once; the other sources keep reporting. An AppleScript **timeout disables only its own
switch** (§4.4): the switch is marked degraded and is not called again until ``fylgja doctor`` is
rerun (:mod:`~fylgja.collectors.appcontext.switches`). With a switch off, no ``osascript`` command
line is ever built.

Nothing is logged except source names, short failure codes and counts: no title, URL or path.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from fylgja.collectors.appcontext import adapter, parse, switches
from fylgja.collectors.appcontext.adapter import AppContextInputs, InputError
from fylgja.collectors.appcontext.parse import Item, WindowFacts
from fylgja.collectors.appcontext.titles import (
    BUNDLE_CHROME,
    BUNDLE_CMUX,
    BUNDLE_OBSIDIAN,
    TitleContext,
    parse_title,
)
from fylgja.collectors.base import CollectorContext
from fylgja.models import ContextItemEvent, HealthReport, HealthState

__all__ = ["AppContextCollector", "AppContextSettings"]

KEY_CMUX: Final = "cmux"
KEY_CHROME: Final = "chrome"
KEY_HISTORY: Final = "history"
KEY_JETBRAINS: Final = "jetbrains"
KEY_OBSIDIAN: Final = "obsidian"
KEY_ITERM2: Final = "iterm2"

RECENT_PROJECTS_RECHECK: Final = 30.0
"""Seconds between re-stats of ``recentProjects.xml`` in tier 1."""

_CONFIDENCE_RANK: Final = {"low": 0, "medium": 1, "high": 2}

SOURCE_STORE: Final = "store"
"""The pseudo source of the windows and focus themselves."""


class AppContextSettings(BaseModel):
    """``[collectors.appcontext]``. The AppleScript switches live in ``[actions.applescript]``."""

    model_config = ConfigDict(extra="forbid")

    poll_interval_seconds: float = Field(default=0.5, ge=0.1, le=5.0)
    """How often the store is read for windows and focus."""
    chrome_history: bool = False
    """Chrome history backfill (off by default): read-only SQLite on the ``History`` file."""
    history_limit: int = Field(default=10, ge=1, le=50)
    history_window_seconds: float = Field(default=3600.0, gt=0)
    """How far back the backfill looks."""
    database: str | None = None
    """The store the windows and focus are read from (read-only); default the daemon's own."""


@dataclass(frozen=True)
class _Disabled:
    reason: str
    at: datetime


@dataclass
class _Recent:
    """The cached ``recentProjects.xml`` of one product-prefix tuple."""

    located: Path | None
    mtime: float
    projects: list[parse.JetBrainsProject]
    checked_at: datetime
    generation: int
    """Bumps when ``(located, mtime)`` changes."""
    error: str | None = None
    """The code of the last failed re-stat or parse, raised again until the next re-stat."""


Tier2 = Callable[[WindowFacts, datetime], Awaitable[None]]


class AppContextCollector:
    def __init__(
        self,
        settings: AppContextSettings,
        context: CollectorContext,
        *,
        inputs: AppContextInputs,
    ) -> None:
        self._settings = settings
        self._context = context
        self._inputs = inputs
        self._emitted: dict[tuple[str, str], str] = {}
        """``(window id, item key)`` to the fingerprint of the item last emitted for it."""
        self._focus_marker: tuple[str, datetime] | None = None
        self._recent: dict[tuple[str, ...], _Recent] = {}
        """Per product-prefix tuple, the cached ``recentProjects.xml``."""
        self._ide_paths: dict[str, tuple[str, str, str, str]] = {}
        """Window id to ``(project name, path, confidence, via)`` last resolved for it."""
        self._ide_seen: dict[str, tuple[str | None, int]] = {}
        """Window id to the ``(title, generation)`` tier 1 last resolved."""
        self._failures: dict[str, str] = {}
        """Source name to the code of its last failure; cleared by the next good reading."""
        self._disabled: dict[str, _Disabled] = {}
        """AppleScript switches that are disabled until doctor is rerun."""
        self._loop_failure: str | None = None
        self._polled = False
        self.polls = 0
        self.tier2_runs = 0
        """Focus changes that ran a second tier."""
        self.records = 0
        """Records that reached the sink."""
        self._tier2: dict[str, Tier2] = {
            BUNDLE_CMUX: self._cmux,
            BUNDLE_CHROME: self._chrome,
            BUNDLE_OBSIDIAN: self._obsidian,
            parse.BUNDLE_ITERM2: self._iterm2,
        }

    # -- failures ---------------------------------------------------------------------------

    def _failed(self, source: str, kind: str) -> None:
        if self._failures.get(source) != kind:
            self._context.logger.warning("appcontext source %s failed: %s", source, kind)
        self._failures[source] = kind

    def _recovered(self, source: str) -> None:
        if self._failures.pop(source, None) is not None:
            self._context.logger.info("appcontext source %s recovered", source)

    # -- emitting ---------------------------------------------------------------------------

    def _apply(self, window: WindowFacts, key: str, item: Item | None, now: datetime) -> None:
        """Emit ``item`` for ``window`` when it changed; withdraw the key when ``item`` is None."""
        slot = (window.window_id, key)
        if item is None:
            if slot in self._emitted:
                del self._emitted[slot]
                self._emit(parse.gone_record(window.window_id, key, window.bundle_id, now))
            return
        fingerprint = item.fingerprint()
        if self._emitted.get(slot) == fingerprint:
            return
        self._emitted[slot] = fingerprint
        self._emit(parse.to_record(item, window, now))

    def _emit(self, record: ContextItemEvent) -> None:
        self._context.sink.emit(record)
        self.records += 1

    # -- tier 1 -----------------------------------------------------------------------------

    def _tier1(self, windows: tuple[WindowFacts, ...], now: datetime) -> None:
        present = {window.window_id for window in windows}
        for slot in [slot for slot in self._emitted if slot[0] not in present]:
            del self._emitted[slot]  # a closed window: the live view forgets its context itself
        for gone in [wid for wid in self._ide_paths if wid not in present]:
            del self._ide_paths[gone]
        for gone in [wid for wid in self._ide_seen if wid not in present]:
            del self._ide_seen[gone]
        for window in windows:
            context: TitleContext | None = None
            if window.bundle_id and window.title:
                context = parse_title(window.bundle_id, window.title)
            self._apply(
                window, parse.KEY_TITLE, None if context is None else parse.title_item(context), now
            )
            if parse.jetbrains_product_prefixes(window.bundle_id or ""):
                self._resolve_jetbrains(window, now, force=False)

    # -- AppleScript switches ---------------------------------------------------------------

    def _doctor_record(self, switch: str) -> switches.SwitchRecord | None:
        try:
            text = adapter.read_input(self._inputs.fs, switches.state_file(), self._inputs.home)
        except InputError:
            return None
        return None if text is None else switches.parse_records(text).get(switch)

    def _switch_on(self, switch: str) -> bool:
        """Whether ``switch`` is configured on and not disabled. Off means no osascript, ever."""
        if not self._inputs.switches.enabled(switch):
            return False
        record = self._doctor_record(switch)
        disabled = self._disabled.get(switch)
        if disabled is not None:
            if record is not None and record.ok and record.at > disabled.at:
                del self._disabled[switch]
                self._failures.pop(f"applescript:{switch}", None)
                self._context.logger.info("appcontext switch %s enabled again", switch)
                return True
            return False
        if record is not None and not record.ok:
            reason = f"doctor_{record.reason or 'failed'}"
            self._disabled[switch] = _Disabled(reason, record.at)
            self._failed(f"applescript:{switch}", reason)
            return False
        return True

    def _disable(self, switch: str, reason: str, now: datetime) -> None:
        self._disabled[switch] = _Disabled(reason, now)
        self._failed(f"applescript:{switch}", reason)

    async def _script(self, switch: str, now: datetime) -> str | None:
        """The output of ``switch``'s script, or ``None`` when the switch is off or disabled or the
        script failed. A timeout disables this switch and no other."""
        if not self._switch_on(switch):
            return None
        try:
            output = await adapter.run_applescript(self._inputs.runner, switch)
        except adapter.AppleScriptError as error:
            if error.kind == "timeout":
                self._disable(switch, "applescript_timeout", now)
            else:
                self._failed(f"applescript:{switch}", f"applescript_{error.kind}")
            return None
        self._failures.pop(f"applescript:{switch}", None)
        return output

    # -- tier 2 -----------------------------------------------------------------------------

    async def _second_tier(self, window: WindowFacts, now: datetime) -> None:
        self.tier2_runs += 1
        for handler in self._handlers(window):
            await handler(window, now)

    def _handlers(self, window: WindowFacts) -> list[Tier2]:
        bundle = window.bundle_id or ""
        if bundle in self._tier2:
            handlers = [self._tier2[bundle]]
            if bundle == BUNDLE_CHROME and self._settings.chrome_history:
                handlers.append(self._chrome_history)
            return handlers
        if parse.jetbrains_product_prefixes(bundle):
            return [self._jetbrains]
        return []

    def _failed_input(self, source: str, error: InputError) -> None:
        self._failed(source, error.kind)

    def _title_fields(self, window: WindowFacts) -> dict[str, str]:
        if not window.bundle_id or not window.title:
            return {}
        context = parse_title(window.bundle_id, window.title)
        return {} if context is None else dict(context.fields)

    async def _cmux(self, window: WindowFacts, now: datetime) -> None:
        try:
            text = adapter.read_input(
                self._inputs.fs, adapter.cmux_store(self._inputs.home), self._inputs.home
            )
            item: Item | None = None
            if text is not None:
                tabs = parse.parse_cmux_tabs(text)
                if tabs is None:
                    raise InputError("unrecognized_format")
                picked = parse.pick_cmux_tab(tabs, window.title)
                if picked is not None:
                    tab, confidence = picked
                    fields: dict[str, JsonValue] = {
                        "cmux_workspace": tab.workspace_id,
                        "cmux_surface": tab.surface_id,
                        "via": "cmux-session-store",
                    }
                    if tab.cwd:
                        fields["cwd"] = tab.cwd
                    if tab.lifecycle:
                        fields["lifecycle"] = tab.lifecycle
                    item = Item(
                        key=KEY_CMUX,
                        kind="cmux_tab",
                        tier=parse.TIER_FOCUS,
                        title=(PurePosixPath(tab.cwd).name or None) if tab.cwd else None,
                        fields=fields,
                        confidence=confidence,
                    )
        except InputError as error:
            self._failed_input(KEY_CMUX, error)
            return
        self._recovered(KEY_CMUX)
        self._apply(window, KEY_CMUX, item, now)

    def _recent_projects(self, prefixes: tuple[str, ...], now: datetime, *, force: bool) -> _Recent:
        """The cached ``recentProjects.xml`` for ``prefixes``. It is re-stat only when ``force`` is
        set or :data:`RECENT_PROJECTS_RECHECK` seconds have passed, and re-parsed only when its
        location or mtime changed. Raises :class:`InputError` while the last attempt failed."""
        cached = self._recent.get(prefixes)
        if (
            cached is not None
            and not force
            and (now - cached.checked_at).total_seconds() < RECENT_PROJECTS_RECHECK
        ):
            if cached.error is not None:
                raise InputError(cached.error)
            return cached
        generation = 0 if cached is None else cached.generation
        try:
            located = adapter.jetbrains_recent_projects(
                self._inputs.fs, self._inputs.home, prefixes
            )
            mtime = -1.0
            if located is not None:
                try:
                    mtime = self._inputs.fs.stat(located).mtime
                except FileNotFoundError:
                    located = None
                except OSError as exc:
                    raise InputError("os_error") from exc
            if (
                cached is not None
                and cached.error is None
                and (cached.located, cached.mtime) == (located, mtime)
            ):
                cached.checked_at = now
                return cached
            projects: list[parse.JetBrainsProject] = []
            if located is not None:
                text = adapter.read_input(self._inputs.fs, located, self._inputs.home)
                if text is not None:
                    parsed = parse.parse_jetbrains_projects(text, str(self._inputs.home))
                    if parsed is None:
                        raise InputError("unrecognized_format")
                    projects = parsed
        except InputError as error:
            self._recent[prefixes] = _Recent(
                None if cached is None else cached.located,
                -1.0 if cached is None else cached.mtime,
                [] if cached is None else cached.projects,
                now,
                generation,
                error.kind,
            )
            raise
        fresh = _Recent(located, mtime, projects, now, generation + 1)
        self._recent[prefixes] = fresh
        return fresh

    def _resolve_jetbrains(self, window: WindowFacts, now: datetime, *, force: bool) -> None:
        """Resolve the project path of a JetBrains window and emit the ``jetbrains`` item.

        Tier 1 calls it for every IDE window and skips a window whose ``(title, generation)`` is
        unchanged; a focus change forces a re-stat of ``recentProjects.xml``. A new reading for
        the same project never replaces a remembered one of higher confidence."""
        fields = self._title_fields(window)
        name = fields.get("project") or ""
        home = str(self._inputs.home)
        try:
            projects: list[parse.JetBrainsProject] = []
            generation = 0
            if "project" in fields and "path" not in fields:
                recent = self._recent_projects(
                    parse.jetbrains_product_prefixes(window.bundle_id or ""), now, force=force
                )
                projects = recent.projects
                generation = recent.generation
            seen = (window.title, generation)
            if not force and self._ide_seen.get(window.window_id) == seen:
                return
            self._ide_seen[window.window_id] = seen
            resolved = parse.resolve_jetbrains_project(
                fields, projects, home, title=window.title, is_dir=self._inputs.fs.is_dir
            )
        except InputError as error:
            self._failed_input(KEY_JETBRAINS, error)
            return
        self._recovered(KEY_JETBRAINS)
        remembered = self._ide_paths.get(window.window_id)
        if remembered is not None and remembered[0] == name:
            if resolved is None or (
                _CONFIDENCE_RANK.get(resolved[1], 0) < _CONFIDENCE_RANK.get(remembered[2], 0)
            ):
                resolved = (remembered[1], remembered[2], remembered[3])
        item: Item | None = None
        if resolved is None:
            self._ide_paths.pop(window.window_id, None)
        else:
            path, confidence, via = resolved
            self._ide_paths[window.window_id] = (name, path, confidence, via)
            shown: dict[str, JsonValue] = {"path": path, "via": via}
            if fields.get("project"):
                shown["project"] = fields["project"]
            item = Item(
                key=KEY_JETBRAINS,
                kind="ide_project",
                tier=parse.TIER_FOCUS,
                title=fields.get("project") or PurePosixPath(path).name or None,
                fields=shown,
                confidence=confidence,
            )
        self._apply(window, KEY_JETBRAINS, item, now)

    async def _jetbrains(self, window: WindowFacts, now: datetime) -> None:
        self._resolve_jetbrains(window, now, force=True)

    async def _obsidian(self, window: WindowFacts, now: datetime) -> None:
        fields = self._title_fields(window)
        vault_name = fields.get("vault")
        note = fields.get("note")
        item: Item | None = None
        try:
            if vault_name:
                text = adapter.read_input(
                    self._inputs.fs,
                    adapter.obsidian_config(self._inputs.home),
                    self._inputs.home,
                )
                vault: parse.Vault | None = None
                if text is not None:
                    vaults = parse.parse_obsidian_vaults(text)
                    if vaults is None:
                        raise InputError("unrecognized_format")
                    vault = parse.resolve_vault(vaults, vault_name)
                shown: dict[str, JsonValue] = {"vault": vault_name}
                if vault is not None:
                    shown["vault_path"] = vault.path
                if note:
                    shown["note"] = note
                item = Item(
                    key=KEY_OBSIDIAN,
                    kind="note",
                    tier=parse.TIER_FOCUS,
                    title=note or vault_name,
                    fields=shown,
                    confidence="high" if vault is not None else "medium",
                    link=parse.obsidian_link(vault_name, note),
                )
        except InputError as error:
            self._failed_input(KEY_OBSIDIAN, error)
            return
        self._recovered(KEY_OBSIDIAN)
        self._apply(window, KEY_OBSIDIAN, item, now)

    async def _chrome(self, window: WindowFacts, now: datetime) -> None:
        if not self._inputs.switches.enabled(switches.CHROME):
            return
        output = await self._script(switches.CHROME, now)
        item: Item | None = None
        url = None if output is None else parse.sanitize_url(output)
        if url is not None:
            title_fields = self._title_fields(window)
            shown: dict[str, JsonValue] = {"url": url}
            if title_fields.get("profile"):
                shown["profile"] = title_fields["profile"]
            item = Item(
                key=KEY_CHROME,
                kind="tab",
                tier=parse.TIER_FOCUS,
                title=title_fields.get("page_title"),
                fields=shown,
                confidence="high",
                link=url,
            )
        self._apply(window, KEY_CHROME, item, now)

    async def _chrome_history(self, window: WindowFacts, now: datetime) -> None:
        item: Item | None = None
        try:
            local_state = adapter.read_input(
                self._inputs.fs,
                adapter.chrome_root(self._inputs.home) / "Local State",
                self._inputs.home,
            )
            directory: str | None = None
            if local_state is not None:
                profiles = parse.parse_chrome_profiles(local_state)
                if profiles is None:
                    raise InputError("unrecognized_format")
                directory = parse.chrome_profile_dir(
                    profiles, self._title_fields(window).get("profile")
                )
                if directory is None and len(profiles) == 1:
                    directory = profiles[0].directory
            if directory is not None:
                since = now - timedelta(seconds=self._settings.history_window_seconds)
                visits = self._inputs.history.recent(directory, since, self._settings.history_limit)
                shown: list[JsonValue] = []
                for visit in visits:
                    url = parse.sanitize_url(visit.url)
                    if url is None:
                        continue
                    entry: dict[str, JsonValue] = {"url": url, "at": visit.at.isoformat()}
                    if visit.title:
                        entry["title"] = visit.title
                    shown.append(entry)
                if shown:
                    item = Item(
                        key=KEY_HISTORY,
                        kind="browser_history",
                        tier=parse.TIER_FOCUS,
                        title=f"{len(shown)} recent visits",
                        fields={"visits": shown},
                        confidence="medium",
                    )
        except InputError as error:
            self._failed_input(KEY_HISTORY, error)
            return
        self._recovered(KEY_HISTORY)
        self._apply(window, KEY_HISTORY, item, now)

    async def _iterm2(self, window: WindowFacts, now: datetime) -> None:
        if not self._inputs.switches.enabled(switches.ITERM2):
            return
        output = await self._script(switches.ITERM2, now)
        item: Item | None = None
        if output:
            path, job = parse.parse_iterm2_output(output)
            if path or job:
                shown: dict[str, JsonValue] = {}
                if path:
                    shown["path"] = path
                if job:
                    shown["job"] = job
                item = Item(
                    key=KEY_ITERM2,
                    kind="terminal_session",
                    tier=parse.TIER_FOCUS,
                    title=job or (PurePosixPath(path).name if path else None) or None,
                    fields=shown,
                    confidence="medium",
                )
        self._apply(window, KEY_ITERM2, item, now)

    # -- one poll ---------------------------------------------------------------------------

    async def poll(self) -> None:
        """Read the windows and focus once: tier 1 for every window, tier 2 on a focus change."""
        self.polls += 1
        try:
            snapshot = self._inputs.source.read()
        except adapter.SourceError as error:
            self._failed(SOURCE_STORE, error.kind)
            return
        self._recovered(SOURCE_STORE)
        now = self._context.clock.now()
        self._tier1(snapshot.windows, now)
        focus = snapshot.focus
        self._polled = True
        if focus is None or focus.window_id is None:
            return
        marker = (focus.window_id, focus.observed_at)
        if marker == self._focus_marker:
            return
        window = next((w for w in snapshot.windows if w.window_id == focus.window_id), None)
        if window is None:
            return  # not an open window yet: look again on the next poll
        self._focus_marker = marker
        await self._second_tier(window, now)

    # -- loop -------------------------------------------------------------------------------

    async def run(self) -> None:
        while True:
            try:
                await self.poll()
            except Exception as exc:
                kind = type(exc).__name__
                if self._loop_failure != kind:
                    self._context.logger.error("appcontext poll failed: %s", kind)
                self._loop_failure = kind
            else:
                self._loop_failure = None
            await self._context.clock.sleep(self._settings.poll_interval_seconds)

    # -- health -----------------------------------------------------------------------------

    async def doctor(self) -> HealthReport:
        """Name the failing sources and the disabled switches. No side effects."""
        now = self._context.clock.now()
        problems: list[tuple[str, str]] = [(name, kind) for name, kind in self._failures.items()]
        for switch, disabled in self._disabled.items():
            if (f"applescript:{switch}", disabled.reason) not in problems:
                problems.append((f"applescript:{switch}", disabled.reason))
        if problems:
            detail = "; ".join(f"{name}: {kind}" for name, kind in problems)
            hint = (
                " (permission dialog likely pending: rerun `fylgja doctor` to enable again)"
                if self._disabled
                else ""
            )
            name, kind = problems[0]
            return HealthReport(
                state=HealthState.DEGRADED,
                detail=f"failing sources: {detail}{hint}",
                error_kind=f"{name.replace(':', '_')}_{kind}",
                checked_at=now,
            )
        if self._loop_failure is not None:
            return HealthReport(
                state=HealthState.DEGRADED,
                detail=f"a poll failed: {self._loop_failure}",
                error_kind="poll_failed",
                checked_at=now,
            )
        detail = "titles parsed; second tier on focus changes" if self._polled else "not polled yet"
        return HealthReport(state=HealthState.OK, detail=detail, checked_at=now)
