# Claude fixtures

`registry/` holds rows shaped like `~/.claude/sessions/<pid>.json` (Claude Code 2.1.284 and 2.1.289).
They were captured on 2026-10-05 into `target/fixture-staging/claude/registry/`, then pids, session ids,
names and working directories were replaced (`4101`–`4105`, `example-N`, `session-N`) and
`scripts/sanitize_fixture.py` ran over them. No `*.key` file is committed; tests that need one create it
at runtime.

| File | What it covers |
|---|---|
| `4101.json` | version 2.1.284, `idle`, `cli` |
| `4102.json` | version 2.1.289, `waiting` with `waitingFor` |
| `4103.json` | version 2.1.289, `busy`, entrypoint `sdk-py`, no `procStart` (the newest live row had none) |
| `4104.json` | version 2.1.289, `idle`, **synthetic** `bridgeSessionId` (no live row had Remote Control on) |
| `4105.json` | version 2.1.289, `idle`, a second row with `procStart` |

`procStart` is wall-clock text in UTC, whereas `ps -o lstart=` prints local time, so tests build their
`ps` output from the instant, never from the text.

## `transcripts/`

Synthetic transcripts (no live capture holds these shapes; the machine has no `TaskCreate` or
`TaskUpdate` record at all, see `docs/revalidation/2026-10-05-claude-transcripts.md`):

| File | What it covers |
|---|---|
| `task-tools-synthetic.jsonl` | `TaskCreate` ×3, then `TaskUpdate` (in progress, delete, rename, unknown id). **Synthetic shape** based on the documented task tools |
| `running-tool-synthetic.jsonl` | a `Read` with its result, then a `Bash` call with no result yet (the session is running it) |

The Lidskjalv and ratatosk transcripts used for the facts parity test live under
`tests/fixtures/lidskjalv/transcripts/` and `tests/fixtures/ratatosk/`; their jq output is under
`tests/fixtures/lidskjalv/facts/` (written by `spikes/gen_lidskjalv_facts_goldens.py`).
