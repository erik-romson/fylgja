# 0001: Host-generic session-to-window join, cmux primary

- Status: accepted
- Amendment: A1
- Amends: PLAN.md §3 join rule 2
- Answers: Q4
- Relates to: A3, A14
- Date: 2026-10-05

## Context

R4: today most sessions run in cmux, and the iTerm2/JetBrains rules alone would join 3 of 11.

## Decision

**Host-generic session-to-window join, with cmux as the primary host** (Q4). Walk the session pid's parent chain until a pid matches an AeroSpace window's `app-pid`; that gives the host app. Then a host adapter picks the window and, where possible, the tab. **cmux** (first priority, joined down to the exact cmux workspace and surface): use the method the WP0.4 spike chooses. That is the cmux CLI if it can list surfaces with their tty, pid or cwd, and otherwise the allowlisted `CMUX_*` ids captured at hook time (A3); window level is only the fallback. **JetBrains**: the project path contains the cwd; window level. **iTerm2**: kept as specified (title equals the ai-title, or `id of window`), but best effort and not part of any done criterion. Every join records `via` and `confidence`. Fylgja's model and UI always say *cmux workspace* or *tab*, so these are never confused with AeroSpace workspaces.

## Consequences

- WP0.4 spike decides how cmux workspaces and surfaces are joined (CLI listing or the allowlisted `CMUX_*` ids from A3).
- Host adapters live behind a common interface, and every join records `via` and `confidence`.
- The model and UI say *cmux workspace* or *tab*, never AeroSpace workspace, for cmux concepts.
- iTerm2 support is best effort and is part of no done criterion.
