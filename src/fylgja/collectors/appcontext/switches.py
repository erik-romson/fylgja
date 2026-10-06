"""The AppleScript switches of application context (``PLAN-final-plan.md`` §4.3, §4.4, WP5.3).

``[actions.applescript]`` has two switches, ``chrome`` and ``iterm2``, both off by default. This
module is the only place an ``osascript`` command line is made (:func:`osascript_argv`), so a
switch that is off can never produce one.

**A timeout disables only its own switch** (§4.4). A timeout is read as "a permission (TCC) dialog
is probably pending". The collector then marks that one switch degraded and stops calling it; every
other context source keeps reporting. The switch stays disabled **until doctor is rerun**:

* ``fylgja doctor`` runs each enabled switch's probe with a 3 s timeout and records the outcome in
  one small state file under the state directory (:func:`state_file`). Doctor is the only writer.
* The collector only reads that file. A record with ``ok = false`` disables the switch (also after a
  daemon restart). A switch the collector itself disabled comes back when the file holds an
  ``ok = true`` record newer than that moment, which is what a successful doctor run writes.

The file holds switch names, a boolean, a short reason code and a time. Nothing else.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from fylgja import paths

__all__ = [
    "APPLESCRIPT_TIMEOUT",
    "CHROME",
    "ITERM2",
    "OSASCRIPT",
    "SWITCHES",
    "SwitchRecord",
    "osascript_argv",
    "parse_records",
    "probe_argv",
    "state_file",
    "write_record",
]

CHROME: Final = "chrome"
ITERM2: Final = "iterm2"
SWITCHES: Final = (CHROME, ITERM2)
"""The switches of ``[actions.applescript]``, in the order doctor reports them."""

OSASCRIPT: Final = "/usr/bin/osascript"
APPLESCRIPT_TIMEOUT: Final = 3.0
"""Every osascript runs with this timeout (§4.4)."""

MAX_STATE_BYTES: Final = 16_384
STATE_NAME: Final = "applescript-switches.json"

# Each script first asks ``is running``, which neither launches the app nor needs a permission; the
# ``tell`` block that follows is what raises the Automation prompt the first time.
CHROME_SCRIPT: Final = (
    'if application "Google Chrome" is running then\n'
    '  tell application "Google Chrome"\n'
    "    if (count of windows) > 0 then return URL of active tab of front window\n"
    "  end tell\n"
    "end if\n"
    'return ""'
)
ITERM2_SCRIPT: Final = (
    'if application "iTerm2" is running then\n'
    '  tell application "iTerm2"\n'
    "    if (count of windows) > 0 then\n"
    "      tell current session of current window\n"
    '        return (variable named "path") & linefeed & (variable named "jobName")\n'
    "      end tell\n"
    "    end if\n"
    "  end tell\n"
    "end if\n"
    'return ""'
)

_SCRIPTS: Final[Mapping[str, str]] = {CHROME: CHROME_SCRIPT, ITERM2: ITERM2_SCRIPT}


def osascript_argv(switch: str) -> list[str]:
    """The ``osascript`` command line of ``switch``. The only one this package ever builds."""
    return [OSASCRIPT, "-e", _SCRIPTS[switch]]


def probe_argv(switch: str) -> list[str]:
    """What doctor runs for ``switch``: the same read the collector does."""
    return osascript_argv(switch)


# --------------------------------------------------------------------------------------------
# The state file
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SwitchRecord:
    """Doctor's last word on one switch."""

    ok: bool
    at: datetime
    reason: str | None = None


def state_file() -> Path:
    """``<state dir>/applescript-switches.json`` (resolved through :mod:`fylgja.paths`)."""
    return paths.state_dir() / STATE_NAME


def parse_records(text: str) -> dict[str, SwitchRecord]:
    """The records of the state file. Anything that is not the known shape is ignored."""
    try:
        data = json.loads(text[:MAX_STATE_BYTES])
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    found: dict[str, SwitchRecord] = {}
    for switch in SWITCHES:
        item = data.get(switch)
        if not isinstance(item, dict):
            continue
        ok = item.get("ok")
        stamp = item.get("at")
        if not isinstance(ok, bool) or not isinstance(stamp, str):
            continue
        try:
            at = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        reason = item.get("reason")
        found[switch] = SwitchRecord(ok, at, reason if isinstance(reason, str) else None)
    return found


def write_record(
    switch: str, *, ok: bool, at: datetime, reason: str | None = None, path: Path | None = None
) -> None:
    """Record doctor's outcome for ``switch`` (doctor only). The write is atomic, mode 0600."""
    target = state_file() if path is None else path
    existing: dict[str, SwitchRecord] = {}
    try:
        existing = parse_records(target.read_text(encoding="utf-8"))
    except OSError:
        pass
    existing[switch] = SwitchRecord(ok, at, reason)
    body = {
        name: {
            "ok": record.ok,
            "at": record.at.astimezone(UTC).isoformat(),
            **({} if record.reason is None else {"reason": record.reason}),
        }
        for name, record in sorted(existing.items())
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(body, sort_keys=True, indent=2) + "\n")
    os.replace(temporary, target)
