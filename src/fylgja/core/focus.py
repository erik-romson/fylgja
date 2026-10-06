"""Focus actions: a window, or a session down to its tab (WP3.5; A1, decision 0025).

Every command goes through the injected :class:`~fylgja.collectors.seams.ProcessRunner`, so the
sequences are testable with :class:`~fylgja.testing.fakes.FakeProcessRunner`. Nothing here ever runs
``osascript`` outside a runner.

* **Window:** ``aerospace focus --window-id <id>``.
* **Session:** ``aerospace workspace <ws>``, ``aerospace focus --window-id <id>``, then a host step:

  - cmux: the AppleScript ``focus`` of the terminal whose id is the session's cmux surface id
    (``[collectors.cmux] applescript_focus``, on by default), then the optional CLI
    ``select-workspace`` and ``focus-panel`` (``[collectors.cmux] cli_focus``, off by default: the
    CLI refuses a launchd daemon in the default socket mode, decision 0025);
  - JetBrains: window level only (``PLAN.md`` §14);
  - iTerm2: an AppleScript select by session UUID, only with
    ``[actions.applescript] iterm2 = true``.

The result is ``{focused: "window" | "tab", via}``. A failing host step never fails the focus: the
result is window level and ``via`` says why. ids are passed to ``osascript`` as arguments, never
spliced into the script text, and only well-formed ids are used at all.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal

from fylgja.collectors.seams import ProcessResult, ProcessRunner
from fylgja.config import Config

__all__ = [
    "AEROSPACE_TIMEOUT",
    "APPLESCRIPT_TIMEOUT",
    "CMUX_CLI",
    "OSASCRIPT",
    "FocusActions",
    "FocusFailed",
    "FocusResult",
]

OSASCRIPT: Final = "/usr/bin/osascript"
CMUX_CLI: Final = "/Applications/cmux.app/Contents/Resources/bin/cmux"
APPLESCRIPT_TIMEOUT: Final = 3.0
"""The plan's timeout for every AppleScript and cmux CLI step."""
AEROSPACE_TIMEOUT: Final = 5.0

CMUX_SCRIPT: Final = (
    "on run argv\n"
    '  tell application "cmux" to focus (first terminal whose id is item 1 of argv)\n'
    "end run"
)
ITERM2_SCRIPT: Final = (
    "on run argv\n"
    "  set wanted to item 1 of argv\n"
    '  tell application "iTerm2"\n'
    "    repeat with w in windows\n"
    "      repeat with t in tabs of w\n"
    "        repeat with s in sessions of t\n"
    "          if id of s is wanted then\n"
    "            select t\n"
    "            select s\n"
    '            return "ok"\n'
    "          end if\n"
    "        end repeat\n"
    "      end repeat\n"
    "    end repeat\n"
    '    return ""\n'
    "  end tell\n"
    "end run"
)

_WINDOW_ID: Final = re.compile(r"[0-9]{1,12}")
_WORKSPACE: Final = re.compile(r"[^\s\-][^\s]{0,63}")
_CMUX_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_-]{0,63}")
_UUID: Final = re.compile(
    r"[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}"
)

Level = Literal["window", "tab"]


class FocusFailed(Exception):
    """Even window-level focus failed. The message is a short reason, never command output."""


@dataclass(frozen=True)
class FocusResult:
    focused: Level
    via: str

    def as_json(self) -> dict[str, str]:
        return {"focused": self.focused, "via": self.via}


TerminalIds = Callable[[str, str], str | None]
"""``(session key, variable name)`` to the terminal id a hook event of the session carried."""


def _no_terminal_ids(_key: str, _name: str) -> str | None:
    return None


class FocusActions:
    """Builds and runs the focus command sequences."""

    def __init__(
        self,
        runner: ProcessRunner,
        config: Config,
        *,
        terminal_id: TerminalIds = _no_terminal_ids,
        osascript: str = OSASCRIPT,
        cmux_cli: str = CMUX_CLI,
    ) -> None:
        self._runner = runner
        self._aerospace = str(config.paths.aerospace)
        self._iterm2 = config.actions.applescript.iterm2
        cmux = config.collectors.get("cmux", {})
        self._cmux_applescript = bool(cmux.get("applescript_focus", True))
        self._cmux_cli = bool(cmux.get("cli_focus", False))
        self._terminal_id = terminal_id
        self._osascript = osascript
        self._cmux_path = cmux_cli

    # -- commands ---------------------------------------------------------------------------

    async def _run(self, argv: list[str], timeout: float) -> str | None:  # noqa: ASYNC109
        """``None`` when the command succeeded, else a short reason."""
        result = await self._attempt(argv, timeout)
        if isinstance(result, str):
            return result
        return None if result.returncode == 0 else f"exit {result.returncode}"

    async def _attempt(
        self,
        argv: list[str],
        timeout: float,  # noqa: ASYNC109
    ) -> ProcessResult | str:
        try:
            return await self._runner.run(argv, timeout=timeout)
        except FileNotFoundError:
            return "not installed"
        except TimeoutError:
            return "timeout"
        except OSError:
            return "could not run"

    async def _aerospace_focus(self, window_id: str) -> None:
        if _WINDOW_ID.fullmatch(window_id) is None:
            raise FocusFailed("invalid window id")
        reason = await self._run(
            [self._aerospace, "focus", "--window-id", window_id], AEROSPACE_TIMEOUT
        )
        if reason is not None:
            raise FocusFailed(f"aerospace focus: {reason}")

    # -- public -----------------------------------------------------------------------------

    async def focus_window(self, window_id: str) -> FocusResult:
        """Focus an AeroSpace window."""
        await self._aerospace_focus(window_id)
        return FocusResult("window", "aerospace")

    async def focus_session(
        self,
        *,
        key: str,
        window_id: str,
        workspace: str | None,
        host_kind: str | None,
        cmux_surface: str | None = None,
        cmux_workspace: str | None = None,
    ) -> FocusResult:
        """The AeroSpace workspace, the window, then the host step; see the module docstring."""
        if workspace is not None and _WORKSPACE.fullmatch(workspace) is not None:
            # A failed workspace switch is not fatal: focusing the window switches it too.
            await self._run([self._aerospace, "workspace", workspace], AEROSPACE_TIMEOUT)
        await self._aerospace_focus(window_id)
        if host_kind == "cmux":
            return await self._cmux(cmux_surface, cmux_workspace)
        if host_kind == "iterm2":
            return await self._iterm2_step(key)
        return FocusResult("window", "aerospace")

    # -- host steps -------------------------------------------------------------------------

    @staticmethod
    def _window_level(host: str, reason: str) -> FocusResult:
        return FocusResult("window", f"aerospace ({host}: {reason})")

    async def _cmux(self, surface: str | None, workspace: str | None) -> FocusResult:
        if surface is None or _CMUX_ID.fullmatch(surface) is None:
            return self._window_level("cmux", "no surface id")
        if not (self._cmux_applescript or self._cmux_cli):
            return self._window_level("cmux", "tab focus disabled")
        reason = "tab focus disabled"
        if self._cmux_applescript:
            reason = (
                await self._run([self._osascript, "-e", CMUX_SCRIPT, surface], APPLESCRIPT_TIMEOUT)
                or ""
            )
            if not reason:
                return FocusResult("tab", "cmux-applescript")
        if self._cmux_cli:
            cli = await self._cmux_cli_select(surface, workspace)
            if cli is None:
                return FocusResult("tab", "cmux-cli")
            reason = cli if not self._cmux_applescript else f"{reason}; cli {cli}"
        return self._window_level("cmux", reason)

    async def _cmux_cli_select(self, surface: str, workspace: str | None) -> str | None:
        """``None`` on success, else a short reason."""
        scoped: list[str] = []
        if workspace is not None and _CMUX_ID.fullmatch(workspace) is not None:
            reason = await self._run(
                [self._cmux_path, "select-workspace", "--workspace", workspace],
                APPLESCRIPT_TIMEOUT,
            )
            if reason is not None:
                return reason
            scoped = ["--workspace", workspace]
        return await self._run(
            [self._cmux_path, "focus-panel", "--panel", surface, *scoped], APPLESCRIPT_TIMEOUT
        )

    async def _iterm2_step(self, key: str) -> FocusResult:
        if not self._iterm2:
            return self._window_level("iterm2", "applescript disabled")
        raw = self._terminal_id(key, "ITERM_SESSION_ID")
        uuid = None if raw is None else raw.partition(":")[2] or raw
        if uuid is None or _UUID.fullmatch(uuid) is None:
            return self._window_level("iterm2", "no session id")
        result = await self._attempt(
            [self._osascript, "-e", ITERM2_SCRIPT, uuid], APPLESCRIPT_TIMEOUT
        )
        if isinstance(result, str):
            return self._window_level("iterm2", result)
        if result.returncode != 0:
            return self._window_level("iterm2", f"exit {result.returncode}")
        if result.stdout.strip() != b"ok":
            return self._window_level("iterm2", "session not found")
        return FocusResult("tab", "iterm2-applescript")
