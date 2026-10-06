"""I/O of the presence collector (``PLAN-final-plan.md`` WP5.1, A17). It reads, never writes.

Every signal sits behind :class:`PresenceSignals` and fails on its own with a
:class:`SignalError` that names the signal and carries a short code:

* ``idle``: ``ioreg -r -c IOHIDSystem -d 1`` through the process runner, ``HIDIdleTime`` parsed by
  :mod:`~fylgja.collectors.presence.parse`;
* ``lock``: ``CGSessionCopyCurrentDictionary`` (CoreGraphics) and its ``CGSSessionScreenIsLocked``
  key, read with CoreFoundation. The key exists only while the screen is locked;
* ``microphone``: CoreAudio ``kAudioDevicePropertyDeviceIsRunningSomewhere`` on the default input
  device;
* ``camera``: CoreMediaIO ``kCMIODevicePropertyDeviceIsRunningSomewhere`` on every video device;
* ``focus``: whether the store's latest focus event names a window, read-only
  (:func:`fylgja.store.db.connect_reader`). This is not a presence signal: it gates the idle poll.

The ``ctypes`` calls need no permission and no helper process: they read device state, never
audio, video or window contents. A framework that cannot be loaded, a missing symbol, a non-zero
``OSStatus`` and a platform other than macOS are all failures of that signal, never a guess.

Frameworks are loaded through a ``loader`` (``ctypes.CDLL`` by default), so tests substitute fake
libraries and need no hardware.
"""

from __future__ import annotations

import ctypes
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable

from fylgja.collectors.aerospace.adapter import SubprocessRunner
from fylgja.collectors.presence import parse
from fylgja.collectors.seams import ProcessRunner
from fylgja.store import db, queries

__all__ = [
    "IOREG",
    "SIGNALS",
    "Loader",
    "PresenceSignals",
    "SignalError",
    "SystemSignals",
    "default_signals",
]

IOREG: Final = "/usr/sbin/ioreg"
IOREG_TIMEOUT: Final = 5.0

SIGNALS: Final = ("idle", "lock", "microphone", "camera", "focus")
"""Every signal the adapter reads, in the order doctor names them."""

COREFOUNDATION: Final = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
COREGRAPHICS: Final = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
COREAUDIO: Final = "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
COREMEDIAIO: Final = "/System/Library/Frameworks/CoreMediaIO.framework/CoreMediaIO"

LOCK_KEY: Final = b"CGSSessionScreenIsLocked"
K_CF_STRING_ENCODING_UTF8: Final = 0x08000100
MAX_VIDEO_DEVICES: Final = 64

Loader = Callable[[str], Any]
"""Loads a framework by path. ``ctypes.CDLL`` by default; raises ``OSError`` when it is missing."""


def _four_cc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


K_OBJECT_SYSTEM: Final = 1  # kAudioObjectSystemObject and kCMIOObjectSystemObject
SCOPE_GLOBAL: Final = _four_cc("glob")
ELEMENT_MAIN: Final = 0
AUDIO_DEFAULT_INPUT: Final = _four_cc("dIn ")  # kAudioHardwarePropertyDefaultInputDevice
CMIO_DEVICES: Final = _four_cc("dev#")  # kCMIOHardwarePropertyDevices
RUNNING_SOMEWHERE: Final = _four_cc("gone")  # ...DeviceIsRunningSomewhere (audio and CMIO)
UNKNOWN_PROPERTY: Final = _four_cc("who?")  # kAudioHardwareUnknownPropertyError


class _PropertyAddress(ctypes.Structure):
    """``AudioObjectPropertyAddress`` and ``CMIOObjectPropertyAddress`` (the same layout)."""

    _fields_ = (
        ("selector", ctypes.c_uint32),
        ("scope", ctypes.c_uint32),
        ("element", ctypes.c_uint32),
    )


class SignalError(Exception):
    """One signal could not be read. ``signal`` names it and ``kind`` is a short, safe code."""

    def __init__(self, signal: str, kind: str) -> None:
        super().__init__(f"{signal}: {kind}")
        self.signal = signal
        self.kind = kind


@runtime_checkable
class PresenceSignals(Protocol):
    """The readings the collector polls. Each raises :class:`SignalError` on its own."""

    async def idle_seconds(self) -> float:
        """Seconds since the last keyboard or pointer input."""
        ...

    def screen_locked(self) -> bool: ...

    def microphone_in_use(self) -> bool: ...

    def camera_in_use(self) -> bool: ...

    def focus_open(self) -> bool:
        """Whether the live view has a focused window (a focus span is open)."""
        ...


class SystemSignals:
    """The real signals: ``ioreg``, the four frameworks and the store."""

    def __init__(
        self,
        runner: ProcessRunner,
        database: Path,
        *,
        loader: Loader | None = None,
        platform: str | None = None,
        ioreg: str = IOREG,
    ) -> None:
        self._runner = runner
        self._database = database
        self._loader: Loader = ctypes.CDLL if loader is None else loader
        self._platform = sys.platform if platform is None else platform
        self._ioreg = ioreg

    # -- helpers ------------------------------------------------------------------------------

    def _framework(self, signal: str, path: str) -> Any:
        if self._platform != "darwin":
            raise SignalError(signal, "unsupported_platform")
        try:
            return self._loader(path)
        except OSError as exc:
            raise SignalError(signal, "framework_missing") from exc

    @staticmethod
    def _symbol(signal: str, library: Any, name: str) -> Any:
        try:
            return getattr(library, name)
        except AttributeError as exc:
            raise SignalError(signal, "symbol_missing") from exc

    @staticmethod
    def _call(signal: str, function: Any, *args: Any) -> Any:
        """Call a ``ctypes`` function; any error it raises is a failure of ``signal``."""
        try:
            return function(*args)
        except (ctypes.ArgumentError, OSError, ValueError, TypeError) as exc:
            raise SignalError(signal, "call_failed") from exc

    # -- idle ---------------------------------------------------------------------------------

    async def idle_seconds(self) -> float:
        try:
            result = await self._runner.run(
                [self._ioreg, "-r", "-c", "IOHIDSystem", "-d", "1"], timeout=IOREG_TIMEOUT
            )
        except FileNotFoundError as exc:
            raise SignalError("idle", "ioreg_missing") from exc
        except PermissionError as exc:
            raise SignalError("idle", "ioreg_not_executable") from exc
        except TimeoutError as exc:
            raise SignalError("idle", "ioreg_timeout") from exc
        except OSError as exc:
            raise SignalError("idle", "ioreg_os_error") from exc
        if result.returncode != 0:
            raise SignalError("idle", "ioreg_failed")
        try:
            return parse.parse_hid_idle_seconds(result.stdout)
        except parse.IdleParseError as exc:
            raise SignalError("idle", exc.kind) from exc

    # -- lock ---------------------------------------------------------------------------------

    def screen_locked(self) -> bool:
        graphics = self._framework("lock", COREGRAPHICS)
        foundation = self._framework("lock", COREFOUNDATION)
        copy_dictionary = self._symbol("lock", graphics, "CGSessionCopyCurrentDictionary")
        get_value = self._symbol("lock", foundation, "CFDictionaryGetValue")
        make_string = self._symbol("lock", foundation, "CFStringCreateWithCString")
        get_boolean = self._symbol("lock", foundation, "CFBooleanGetValue")
        release = self._symbol("lock", foundation, "CFRelease")
        copy_dictionary.restype = ctypes.c_void_p
        copy_dictionary.argtypes = []
        get_value.restype = ctypes.c_void_p
        get_value.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        make_string.restype = ctypes.c_void_p
        make_string.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        get_boolean.restype = ctypes.c_ubyte
        get_boolean.argtypes = [ctypes.c_void_p]
        release.restype = None
        release.argtypes = [ctypes.c_void_p]

        session = self._call("lock", copy_dictionary)
        if not session:  # no GUI session: the lock state is unknowable, not "unlocked"
            raise SignalError("lock", "no_session_dictionary")
        key = None
        try:
            key = self._call("lock", make_string, None, LOCK_KEY, K_CF_STRING_ENCODING_UTF8)
            if not key:
                raise SignalError("lock", "call_failed")
            value = self._call("lock", get_value, session, key)
            # The key is only there while the screen is locked.
            return bool(value) and bool(self._call("lock", get_boolean, value))
        finally:
            if key:
                release(key)
            release(session)

    # -- microphone ---------------------------------------------------------------------------

    def microphone_in_use(self) -> bool:
        audio = self._framework("microphone", COREAUDIO)
        get_data = self._symbol("microphone", audio, "AudioObjectGetPropertyData")
        get_data.restype = ctypes.c_int32
        get_data.argtypes = [
            ctypes.c_uint32,
            ctypes.POINTER(_PropertyAddress),
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]

        def read(object_id: int, selector: int) -> int:
            address = _PropertyAddress(selector, SCOPE_GLOBAL, ELEMENT_MAIN)
            size = ctypes.c_uint32(ctypes.sizeof(ctypes.c_uint32))
            value = ctypes.c_uint32(0)
            status = self._call(
                "microphone",
                get_data,
                object_id,
                ctypes.byref(address),
                0,
                None,
                ctypes.byref(size),
                ctypes.byref(value),
            )
            if status != 0:
                raise SignalError("microphone", "os_status_error")
            return int(value.value)

        device = read(K_OBJECT_SYSTEM, AUDIO_DEFAULT_INPUT)
        if device == 0:  # kAudioObjectUnknown: this Mac has no input device
            return False
        return read(device, RUNNING_SOMEWHERE) != 0

    # -- camera -------------------------------------------------------------------------------

    def camera_in_use(self) -> bool:
        media = self._framework("camera", COREMEDIAIO)
        get_size = self._symbol("camera", media, "CMIOObjectGetPropertyDataSize")
        get_data = self._symbol("camera", media, "CMIOObjectGetPropertyData")
        get_size.restype = ctypes.c_int32
        get_size.argtypes = [
            ctypes.c_uint32,
            ctypes.POINTER(_PropertyAddress),
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        get_data.restype = ctypes.c_int32
        get_data.argtypes = [
            ctypes.c_uint32,
            ctypes.POINTER(_PropertyAddress),
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]

        def read(object_id: int, selector: int, buffer: Any, size: int) -> int:
            address = _PropertyAddress(selector, SCOPE_GLOBAL, ELEMENT_MAIN)
            used = ctypes.c_uint32(0)
            status = self._call(
                "camera",
                get_data,
                object_id,
                ctypes.byref(address),
                0,
                None,
                size,
                ctypes.byref(used),
                ctypes.byref(buffer),
            )
            return int(status)

        list_address = _PropertyAddress(CMIO_DEVICES, SCOPE_GLOBAL, ELEMENT_MAIN)
        list_size = ctypes.c_uint32(0)
        status = self._call(
            "camera",
            get_size,
            K_OBJECT_SYSTEM,
            ctypes.byref(list_address),
            0,
            None,
            ctypes.byref(list_size),
        )
        if status != 0:
            raise SignalError("camera", "os_status_error")
        count = min(list_size.value // ctypes.sizeof(ctypes.c_uint32), MAX_VIDEO_DEVICES)
        if count == 0:
            return False
        devices = (ctypes.c_uint32 * count)()
        if read(K_OBJECT_SYSTEM, CMIO_DEVICES, devices, count * 4) != 0:
            raise SignalError("camera", "os_status_error")
        failed = False
        for device in devices:
            running = ctypes.c_uint32(0)
            code = read(device, RUNNING_SOMEWHERE, running, ctypes.sizeof(running))
            if code == 0:
                if running.value != 0:
                    return True  # one running device settles it, whatever the others say
            elif code != UNKNOWN_PROPERTY:  # a virtual device without the property
                failed = True
        if failed:
            raise SignalError("camera", "os_status_error")
        return False

    # -- focus --------------------------------------------------------------------------------

    def focus_open(self) -> bool:
        """The store's latest focus event names a window. A store not created yet has none."""
        try:
            if not self._database.exists():
                return False
            connection = db.connect_reader(self._database)
            try:
                row = queries.latest_focus(connection)
            finally:
                connection.close()
        except (OSError, sqlite3.Error, KeyError, ValueError) as exc:
            raise SignalError("focus", f"store_{type(exc).__name__}") from exc
        return row is not None and row.window_id is not None


def default_signals(database: Path) -> PresenceSignals:
    """The real signals. The plugin looks this up at call time so tests can replace it."""
    return SystemSignals(SubprocessRunner(), database)
