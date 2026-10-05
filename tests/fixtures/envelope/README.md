# Hook inbox envelope golden

- `fylgja.hook-1.json`: one `fylgja.hook/1` envelope for a Claude `Stop` event. It is hand-written, not
  captured: every id, pid, path and terminal id is a placeholder. It is pretty-printed for reading; real envelopes
  are one compact ASCII line.

`docs/api/inbox-envelope.md` is the contract this golden illustrates. `tests/unit/test_envelope_golden.py` checks
that an envelope built by `fylgja.hook` has the same keys, in the same order and with the same types. It also
checks that the contract page lists exactly these keys and the hook's environment allowlist. External writers
(the norn and OpenCode plugins) test their output against the same file.

Changing a key or its type means a new schema version: add a `fylgja.hook-2.json` beside this file and keep this
one.
