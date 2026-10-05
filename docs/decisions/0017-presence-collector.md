# 0017: A separate presence collector

- Status: accepted
- Amendment: A17
- Amends: PLAN.md §4.2 idle placement, §4.7 call flags
- Answers: —
- Relates to: —
- Date: 2026-10-05

## Context

The "one job per collector" rule, and §10 keeps Swift out of the daemon.

## Decision

**A separate `presence` collector** handles idle (`ioreg`), screen lock and mic/camera in use. Lock and in-use flags are read through stdlib `ctypes` calls to CoreGraphics, CoreAudio and CoreMediaIO, polled every 5–10 s. There is no Swift helper.

## Consequences

- A `presence` collector polls idle (`ioreg`), screen lock and mic/camera use every 5–10 s.
- Lock and in-use flags use stdlib `ctypes` calls to CoreGraphics, CoreAudio and CoreMediaIO; no Swift helper is added.
- Those tests carry `skipif(sys.platform != "darwin")`.
