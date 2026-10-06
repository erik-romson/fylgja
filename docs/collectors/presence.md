# Presence collector

The built-in `presence` collector (`src/fylgja/collectors/presence/`, capability `context`) reports whether you are at
the machine: idle time, screen lock, and whether a microphone or camera is in use ("in a call"). It is enabled by
default (`[collectors.presence]` in `defaults.toml`). It has no Swift helper, starts no long-lived process, reads
nothing that needs a permission, and (A17) is a collector of its own, apart from AeroSpace.

## What it reads

| Signal | Source | Polled |
|---|---|---|
| `idle` | `/usr/sbin/ioreg -r -c IOHIDSystem -d 1`, key `HIDIdleTime` (nanoseconds) | every `idle_poll_interval_seconds` (15 s), **only while a focus span is open** |
| `lock` | `ctypes`: CoreGraphics `CGSessionCopyCurrentDictionary`, key `CGSSessionScreenIsLocked` (present only while locked) | every `poll_interval_seconds` (5 s; 5 to 10 allowed) |
| `microphone` | `ctypes`: CoreAudio `kAudioDevicePropertyDeviceIsRunningSomewhere` on the default input device | the same |
| `camera` | `ctypes`: CoreMediaIO `kCMIODevicePropertyDeviceIsRunningSomewhere` on every video device | the same |
| `focus` | the daemon's store, read-only (`mode=ro`, `query_only`): does the latest focus event name a window? | with the idle poll; gates it |

"A focus span is open" means the live view has a focused window: the latest focus event with a `window_id`. (The span
store of step 38 does not replace this gate: after the span engine closes a span at idle, the idle poll must go on to
see the first input.) With no focused window the collector reads no idle time and starts no process.

Only device state is read: never audio, video, window contents or titles. Logs name signals and short failure codes only.

## Events

| Event | Record | `entity_id` | Payload |
|---|---|---|---|
| `idle.start` / `idle.end` | `presence` event | `idle` | `state` `idle` / `active` |
| `lock.start` / `lock.end` | `presence` event | `lock` | `state` `locked` / `active` |
| `call.start` / `call.end` | `context_item` event | `call` | `context_kind` `call`, `data` `{event, microphone, camera}` |

The record schema has no field for an event name on a presence payload, so the name is the pair of `entity_id` and
`state` (a call's name is in `data.event`). The mapping is in `parse.to_record`.

- **Idle threshold:** `idle_threshold_seconds` (300). A reading at or above it is idle; below it is active.
- **Back-dated stamps:** `idle.start` carries the moment the threshold was crossed, so the idle period began
  `threshold` seconds before its `observed_at`; `idle.end` carries the moment of the first input. The focus-span engine
  (`core/spans.py`, decision 0042) closes a span at `idle.start.observed_at - threshold`, using `[spans]
  idle_threshold_seconds` (keep it equal to this collector's threshold), and starts one again at `idle.end`.
- **Baselines:** the first lock reading is a baseline: an unlocked screen emits nothing, a locked one emits
  `lock.start`. A call already running at start emits `call.start`.
- **Call:** the microphone or the camera running. There is no debounce; dictation also runs the microphone.

## Failure and health

A signal that cannot be read (a `ctypes` load or call error, a missing framework or symbol, a non-zero `OSStatus`,
`ioreg` missing or garbled, an unreadable store, or a platform other than macOS) makes the collector `degraded`.
`doctor()` names it, for example `failing signals: lock: no_session_dictionary; camera: os_status_error`, and the
`error_kind` is `<signal>_<code>`. The failing signal's state is left alone (an unreadable microphone never ends a call);
the other signals keep reporting, and the failing one is tried again at its next poll. Nothing is guessed.

An unreadable focus state stops the idle poll (the gate cannot be checked) and is named as `focus`.

## Checking by hand

`uv run --frozen python spikes/presence_probe.py` prints one line per signal. The launchd-context check, and the live
smoke test (`FYLGJA_RUN_LIVE=1`, `tests/live/test_presence_live.py`), are in
`docs/revalidation/2026-10-06-presence.md`.
