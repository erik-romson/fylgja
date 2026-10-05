# 0031: Hook ingress details: allowlist names, size markers, backlog verdict and statusline snapshots

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A2, A3, A4, A5
- Date: 2026-10-05

## Context

WP2.1 specifies `fylgja hook` step by step: the redirect, the alarm, the stdin deadline, the size caps, the
envelope, the atomic write, the inbox checks and the backlog limit. Building it and revalidating the hooks
documentation (`docs/revalidation/2026-10-05-hooks.md`) left some details open:

- A3 allows "the session id and the pid" from the hook environment, but does not name the variables.
- The spec names the `malformed_oversize` marker, but neither its shape nor the bound on how much stdin is read
  at all.
- It says when the backlog is checked (at most every 60 s, by a stamp's mtime), but not what the hook does
  between checks.
- A4 says the statusline is a replaced snapshot, but not what that file contains.
- A version lookup through `importlib.metadata` costs about 20 ms, a third of the 60 ms budget.

## Decision

- **Allowlist names.** The session id is `CLAUDE_CODE_SESSION_ID`, which equals the payload's `session_id` and
  follows `/clear`. The pid is `CLAUDE_PID`, Claude's own pid. Both are documented for hook subprocesses. Together
  with `CLAUDE_CODE_MESSAGING_SOCKET` and the eight A3 terminal ids they make up `fylgja.hook.ENV_ALLOWLIST`.
  Only values that are set and non-empty are copied, each cut to 256 characters.
- **Size markers.**
  - A string over 4 KiB in an oversize payload becomes `{"_truncated": <utf-8 bytes>}`.
  - A payload that cannot be kept becomes `{"_malformed_oversize": <bytes read>}`, both as the whole
    `payload_text`.
  - The hook keeps at most 4 MiB of stdin. It reads and discards the rest until end-of-file or the 2 s deadline,
    so Claude never blocks on a full pipe. Input beyond 4 MiB always becomes the marker.
  - `truncated` is `true` whenever `payload_text` is not the complete input, including input cut by the stdin
    deadline.
- **Text encoding.** A payload up to 256 KiB is decoded as UTF-8, with invalid bytes replaced. The envelope is
  written as one line of ASCII-only JSON, so lone surrogates and any other text round-trip.
- **Backlog verdict.**
  - `.backlog-stamp` holds the last scan's verdict (`ok` or `full`), and its mtime is the time of that scan.
    Within 60 s the hook trusts the verdict without scanning.
  - A stale stamp is rewritten with its old verdict before the scan, so concurrent hooks do not all scan.
  - The scan counts `*.json` envelope files, excluding dot files.
  - `dropped.count` is a decimal counter, incremented under `flock`.
  - Any error in the backlog check counts as "not full": failing open loses no events.
- **Statusline snapshots.** `statusline/<session_id>.json` holds a full `fylgja.hook/1` envelope with
  `event_arg` `"StatusLine"`, so the daemon parses one format.
  - The session id comes from the payload, with `CLAUDE_CODE_SESSION_ID` as the fallback, and must be a plain id.
  - The snapshot is replaced by rename, and skipped while the existing one is under 2 s old.
  - The statusline directory gets the same 0700, owner and symlink checks as the inbox.
- **Process details.**
  - `fylgja.hook.FYLGJA_VERSION` is a constant, and a unit test keeps it equal to the distribution version.
  - `hook.py` does not import `typing` at run time.
  - Unknown flags are ignored, so newer installers stay compatible, and `-h` or `--help` writes nothing.
  - `received_at_ns` is taken when the hook starts, and `main()` ends with `os._exit(0)`.
- **The budget gate is passed.** `scripts/bench_hook.py` measured p95 27–31 ms and a peak RSS of 20.5 MB
  (`docs/spikes/0002-hook-cost.md`). PreToolUse stays in `[hooks] events`.

## Consequences

- `docs/api/inbox-envelope.md` records all of the above as the `fylgja.hook/1` contract.
  `tests/unit/test_envelope_golden.py` keeps the page, the golden and the code in step.
- The inbox drain (WP2.2) must:
  - read only names that match `^[0-9]+-[0-9a-f-]{36}\.json$`;
  - treat both markers as valid payloads that carry no fields;
  - take the session id from `env.CLAUDE_CODE_SESSION_ID` when the payload was replaced;
  - delete `.tmp-*` debris older than a minute.
- External writers (the norn and OpenCode plugins) produce the same envelope and honour a `full` stamp.
- A change to the Python version or to the hook's imports means rerunning the benchmark.
