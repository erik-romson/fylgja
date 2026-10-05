# Recorded resolver fixture (step 13, WP1.8)

A sanitized capture of the live join inputs of 2026-10-05: 14 Claude registry rows, the AeroSpace windows of
cmux and JetBrains, the ancestor chains of the registry pids, the opened JetBrains projects and cmux's session
store. `expected.json` is the join spike's own table for this capture.

How it was made (read-only on the live machine; nothing under `~` was written):

1. `uv run --frozen python spikes/join_table.py --capture-out target/fixture-staging/join/2026-10-05 --json`
2. The capture was trimmed in `target/fixture-staging/resolver/recorded/`: the registry rows keep only the fields
   the tests read (no socket path, no bridge id) and have their session ids replaced; `windows.json` keeps the cmux
   and JetBrains windows; `ps.txt` keeps the ancestor chains of the registry pids; the JetBrains XML keeps the
   opened projects. `cmux-sessions.json` holds the allowlisted fields of cmux's session store
   (`~/.cmuxterm/claude-hook-sessions.json`, `version` 1), with the session, workspace and surface ids replaced.
3. `uv run --frozen python scripts/sanitize_fixture.py resolver/recorded`
4. `uv run --frozen python spikes/join_table.py --from-capture tests/fixtures/resolver/recorded --json >
   tests/fixtures/resolver/recorded/expected.json`

`tests/integration/test_resolver_recorded_fixture.py` runs the resolver over this capture and compares it with
`expected.json`.
