"""Fakes for the presence collector's tests: scripted signals and fake macOS frameworks.

``FakeSignals`` stands in for the adapter's ``PresenceSignals``. ``FakeFrameworks`` is a ``loader``
for ``SystemSignals`` whose libraries are plain Python functions that fill the ``ctypes``
out-parameters the way CoreAudio, CoreMediaIO, CoreGraphics and CoreFoundation do. No hardware and
no macOS are needed.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fylgja.collectors.base import CollectorContext
from fylgja.collectors.context import build_context
from fylgja.collectors.presence import adapter
from fylgja.collectors.presence.collector import PresenceCollector, PresenceSettings
from fylgja.store import db, queries
from fylgja.testing.fakes import FakeClock, RecordingSink


class FakeSignals:
    """Each reading is a value, a :class:`SignalError` to raise, or a callable giving either."""

    def __init__(self) -> None:
        self.idle: Any = 0.0
        self.locked: Any = False
        self.microphone: Any = False
        self.camera: Any = False
        self.focus: Any = True
        self.idle_reads = 0
        self.focus_reads = 0

    @staticmethod
    def _get(value: Any) -> Any:
        if callable(value):
            value = value()
        if isinstance(value, Exception):
            raise value
        return value

    async def idle_seconds(self) -> float:
        self.idle_reads += 1
        return float(self._get(self.idle))

    def screen_locked(self) -> bool:
        return bool(self._get(self.locked))

    def microphone_in_use(self) -> bool:
        return bool(self._get(self.microphone))

    def camera_in_use(self) -> bool:
        return bool(self._get(self.camera))

    def focus_open(self) -> bool:
        self.focus_reads += 1
        return bool(self._get(self.focus))


def make_collector(
    signals: FakeSignals | adapter.PresenceSignals,
    *,
    clock: FakeClock | None = None,
    sink: RecordingSink | None = None,
    **settings: Any,
) -> tuple[PresenceCollector, FakeClock, RecordingSink]:
    the_clock = FakeClock() if clock is None else clock
    the_sink = RecordingSink() if sink is None else sink
    context: CollectorContext = build_context("presence", the_sink, clock=the_clock)
    collector = PresenceCollector(PresenceSettings(**settings), context, signals=signals)
    return collector, the_clock, the_sink


def event_names(sink: RecordingSink) -> list[str]:
    """``idle.start`` and the like, recovered from the records as the docs describe them."""
    names: list[str] = []
    for record in sink.records:
        payload: Any = record.model_dump(mode="json")["payload"]  # type: ignore[attr-defined]
        if record.entity_id == "call":
            names.append(str(payload["data"]["event"]))
        else:
            edge = "start" if payload["state"] in {"idle", "locked"} else "end"
            names.append(f"{record.entity_id}.{edge}")
    return names


# --------------------------------------------------------------------------------------------
# Fake frameworks
# --------------------------------------------------------------------------------------------


class _Library:
    """A bag of functions. Plain functions take attributes, as ``ctypes`` functions do."""


class FakeFrameworks:
    """A loader of fake CoreAudio, CoreMediaIO, CoreGraphics and CoreFoundation."""

    def __init__(self) -> None:
        # CoreAudio
        self.default_input = 7
        self.input_running = False
        self.audio_status: dict[tuple[int, int], int] = {}
        # CoreMediaIO
        self.cameras: list[int] = [11]
        self.camera_running: dict[int, bool] = {}
        self.camera_status: dict[int, int] = {}
        self.list_size_status = 0
        self.list_data_status = 0
        # CoreGraphics and CoreFoundation
        self.session: bool = True
        self.locked: bool | None = None
        """``None``: the key is absent (an unlocked screen)."""
        self.string_ok = True
        self.released: list[int] = []
        # failures
        self.missing: set[str] = set()
        """Framework paths that cannot be loaded."""
        self.missing_symbols: set[str] = set()
        self.raising: dict[str, BaseException] = {}
        """Function name to what calling it raises."""
        self.loads: list[str] = []

    def __call__(self, path: str) -> Any:
        self.loads.append(path)
        if path in self.missing:
            raise OSError(f"{path}: image not found")
        library = _Library()
        builders: dict[str, Callable[[_Library], None]] = {
            adapter.COREAUDIO: self._audio,
            adapter.COREMEDIAIO: self._media,
            adapter.COREGRAPHICS: self._graphics,
            adapter.COREFOUNDATION: self._foundation,
        }
        builders[path](library)
        for name in self.missing_symbols:
            if hasattr(library, name):
                delattr(library, name)
        for name, error in self.raising.items():
            if hasattr(library, name):
                setattr(library, name, self._raiser(error))
        return library

    @staticmethod
    def _raiser(error: BaseException) -> Callable[..., Any]:
        def fail(*args: Any) -> Any:
            raise error

        return fail

    # -- CoreAudio ----------------------------------------------------------------------------

    def _audio(self, library: _Library) -> None:
        def get(object_id: int, address: Any, _qs: int, _q: Any, size: Any, out: Any) -> int:
            selector = address._obj.selector
            status = self.audio_status.get((object_id, selector), 0)
            if status != 0:
                return status
            if selector == adapter.AUDIO_DEFAULT_INPUT:
                out._obj.value = self.default_input
            elif selector == adapter.RUNNING_SOMEWHERE:
                out._obj.value = 1 if self.input_running else 0
            return 0

        library.AudioObjectGetPropertyData = get  # type: ignore[attr-defined]

    # -- CoreMediaIO --------------------------------------------------------------------------

    def _media(self, library: _Library) -> None:
        def get_size(object_id: int, address: Any, _qs: int, _q: Any, size: Any) -> int:
            if self.list_size_status:
                return self.list_size_status
            size._obj.value = 4 * len(self.cameras)
            return 0

        def get_data(
            object_id: int, address: Any, _qs: int, _q: Any, size: int, used: Any, out: Any
        ) -> int:
            selector = address._obj.selector
            if selector == adapter.CMIO_DEVICES:
                if self.list_data_status:
                    return self.list_data_status
                for index, device in enumerate(self.cameras):
                    out._obj[index] = device
                return 0
            status = self.camera_status.get(object_id, 0)
            if status != 0:
                return status
            out._obj.value = 1 if self.camera_running.get(object_id, False) else 0
            return 0

        library.CMIOObjectGetPropertyDataSize = get_size  # type: ignore[attr-defined]
        library.CMIOObjectGetPropertyData = get_data  # type: ignore[attr-defined]

    # -- CoreGraphics and CoreFoundation ------------------------------------------------------

    def _graphics(self, library: _Library) -> None:
        def copy_dictionary() -> int | None:
            return 100 if self.session else None

        library.CGSessionCopyCurrentDictionary = copy_dictionary  # type: ignore[attr-defined]

    def _foundation(self, library: _Library) -> None:
        def make_string(_alloc: Any, text: bytes, encoding: int) -> int | None:
            assert text == adapter.LOCK_KEY
            assert encoding == adapter.K_CF_STRING_ENCODING_UTF8
            return 200 if self.string_ok else None

        def get_value(session: int, key: int) -> int | None:
            assert (session, key) == (100, 200)
            return None if self.locked is None else 300

        def get_boolean(value: int) -> int:
            assert value == 300
            return 1 if self.locked else 0

        def release(handle: int) -> None:
            self.released.append(handle)

        library.CFStringCreateWithCString = make_string  # type: ignore[attr-defined]
        library.CFDictionaryGetValue = get_value  # type: ignore[attr-defined]
        library.CFBooleanGetValue = get_boolean  # type: ignore[attr-defined]
        library.CFRelease = release  # type: ignore[attr-defined]


def store_with_focus(path: Path, *, window_id: str | None) -> Path:
    """A real store whose latest focus event names ``window_id`` (or no window)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = db.open_store(path)
    try:
        connection.execute("BEGIN")
        queries.insert_event(
            connection,
            dedup_key="focus-1",
            source="aerospace",
            record_type="event",
            entity_kind="focus",
            entity_id="focus",
            observed_at="2026-01-01T00:00:00.000000Z",
            received_at="2026-01-01T00:00:00.000000Z",
            payload_json=json.dumps(
                {"window_id": window_id, "workspace": "1", "app_bundle_id": None, "title": None}
            ),
        )
        connection.execute("COMMIT")
    finally:
        connection.close()
    return path
