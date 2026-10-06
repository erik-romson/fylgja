"""Read-only probe of the four presence signals (``PLAN-final-plan.md`` WP5.1 revalidation).

Run by hand, from a shell or, for the launchd check, from a throwaway launchd agent::

    uv run --frozen python spikes/presence_probe.py

It prints one line per signal: ``<signal> ok <value>`` or ``<signal> FAILED <kind>``, and exits 0
when all four work. It reads device state only (no audio, video or window contents), needs no
permission and writes nothing. The ``focus`` signal is not probed: it reads Fylgja's own store.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from fylgja.collectors.aerospace.adapter import SubprocessRunner
from fylgja.collectors.presence.adapter import SignalError, SystemSignals


async def main() -> int:
    signals = SystemSignals(SubprocessRunner(), Path(tempfile.gettempdir()) / "no-such.db")
    failed = 0
    for name, read in (
        ("idle", signals.idle_seconds),
        ("lock", lambda: asyncio.sleep(0, signals.screen_locked())),
        ("microphone", lambda: asyncio.sleep(0, signals.microphone_in_use())),
        ("camera", lambda: asyncio.sleep(0, signals.camera_in_use())),
    ):
        try:
            value = await read()
        except SignalError as error:
            print(f"{name} FAILED {error.kind}")
            failed += 1
        else:
            print(f"{name} ok {value:.1f}" if isinstance(value, float) else f"{name} ok {value}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
