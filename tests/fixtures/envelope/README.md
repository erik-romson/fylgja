# Hook inbox envelope golden

- `fylgja.hook-1.json`: one `fylgja.hook/1` envelope for a Claude `Stop` event. It is hand-written, not
  captured: every id, pid, path and terminal id is a placeholder. It is pretty-printed for reading; real envelopes
  are one compact ASCII line.

`docs/api/inbox-envelope.md` is the contract this golden illustrates. `tests/unit/test_envelope_golden.py` checks
that an envelope built by `fylgja.hook` has the same keys, in the same order and with the same types. It also
checks that the contract page lists exactly these keys and the hook's environment allowlist. External writers
(the norn and OpenCode plugins) test their output against the same file.

- `opencode-session-status.json`: the envelope the OpenCode plugin (`src/fylgja/collectors/opencode/fylgja.ts`) writes
  for a `session.status` event. It is generated, not hand-written: `uv run --frozen python
  scripts/gen_opencode_envelope.py` runs the plugin in `bun` and replaces the id, stamp, pid and terminal ids with
  placeholders. `tests/unit/test_opencode_envelope_compat.py` checks it against the Python reader and against
  `fylgja.hook-1.json`.

Changing a key or its type means a new schema version: add a `fylgja.hook-2.json` beside this file and keep this
one.
