# AeroSpace fixtures

Captured from AeroSpace 0.21.3-Beta on 2026-10-05 through `target/fixture-staging/aerospace/` and
`scripts/sanitize_fixture.py`. Window titles are replaced by placeholder words with the same
punctuation shape.

- `events.ndjson`: the four events `aerospace subscribe --all` sends on connect (captured).
- `windows.json`: `aerospace list-windows --monitor all --json --format '<WINDOW_FORMAT>'`.
- `workspaces.json`: `aerospace list-workspaces --all --json --format '…'` plus the extra
  variables `workspace-is-visible` and `monitor-name`, which the parser ignores.
- `events-synthetic.ndjson`: hand-written. `window-detected` and `binding-triggered` are accepted
  by `aerospace subscribe` but did not occur in the capture, so their field names are assumptions;
  the third line is an event name no version knows.
