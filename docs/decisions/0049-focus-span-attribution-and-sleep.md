# 0049: Focus spans: sleep and window folders

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A12, A17
- Date: 2026-10-13

## Context

A focus span stays open until a focus change, idle, a lock or shutdown. No sleep signal exists: the presence
collector reports a lock only after the machine wakes, stamped at wake time. A suspended Mac therefore keeps the open
span running, and the sleep counts as focus time.

A span is also attributed to a project only through a session the resolver placed in its window. Two gaps follow:

1. **IDE windows usually had no folder.** Tier 1 parsed only the title, which gives a project *name*, and JetBrains
   names a project after its directory, so every worktree checkout is called `main`. The path came from
   `recentProjects.xml` in tier 2, which runs only when the window gains focus, and a shared name could resolve to the
   wrong folder.
2. **IDE and terminal focus time was not counted for any folder.** Time in an IDE or a terminal window without a
   placed session was stored with `project_path = NULL`, so nothing could tell which repository it was spent in.

## Decision

- **Sleep.** The daemon's tick loop compares the wall clock between two ticks. A jump of more than
  `tick + idle_threshold` (330 s by default) means the machine slept. The open span is closed at the last tick with
  `end_reason = idle`, and a new span for the still-focused window opens at the first tick after the jump, unless idle
  or lock still hold. A shorter jump is not worth a split.
- **Tier-1 IDE paths.** The appcontext collector resolves the JetBrains project path in tier 1 for every IDE window,
  from the title's `[path]` or a cached `recentProjects.xml`, preferring open projects and an exact `frameTitle`
  match, and keeps the last strong answer per window. So an IDE window has a folder before it is focused.
- **Window folders by context kind.** When no live session is placed in the focused window, `Daemon._attribute` falls
  back to the window's folder (`core/window_folders.py`): the absolute `data.path` or `data.cwd` of a context item of
  kind `ide_project`, `cmux_tab` or `terminal_session`. Tier 2 wins over tier 1, then confidence high > medium > low,
  then the newest observation, then the item key. The rule names context kinds, never a collector, so any plugin that
  attaches such an item counts. A `note` item (a vault path) is not a working folder, and a `~/…` or relative value is
  not used.
- **The git root is the key.** The folder is mapped to its git root with the daemon's one `ProjectLocator`, which the
  resolver shares, so session, IDE and terminal time land on the same `project_path`. A folder outside a git
  repository gives no attribution.
- **The session keeps precedence.** A live session placed in the window is the most exact signal: its project and
  session key win, and the window folder is used only without one. A window-folder span carries no session key. A span
  opened before its folder was known is filled in when it closes, as for a session.

## Consequences

- No migration: `idle` is an existing `end_reason`, and it now also covers a suspended machine.
- History totals exclude the time the machine slept.
- Tests move the wall clock without moving monotonic time with `FakeClock.suspend`.
- `/history` project totals now include IDE and terminal time. That changes the resolver's project → workspace input
  (`focus_time_by_project`): a project's IDE and terminal focus now counts for the workspace it was spent in.
- Spans recorded earlier are not rewritten; only spans written after the daemon restarts carry window folders.
- A span that opened with a window folder and whose window gains a session before it closes keeps its folder and
  gains the session key, as `SpanTracker` completes any span at close.
