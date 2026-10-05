# Spike 0002: what `fylgja hook` costs per event (WP2.1 budget gate)

- Date: 2026-10-05
- Script: `scripts/bench_hook.py` (dev-only, not packaged). Command lines used:
  `uv run --frozen python scripts/bench_hook.py -n 300 --rebuild`, then `-n 500`, then `-n 300` again after
  CPU time was added to the output.
- Install under test: a throwaway virtualenv `target/bench-venv` (gitignored), made with `uv venv` and a regular,
  non-editable `uv pip install .`. That is the same layout a `uv tool` virtualenv has. `uv tool install` was not
  used.
- Versions: macOS 27.0.1 on arm64 (12 CPUs), Python 3.13.5, uv 0.7.19, Fylgja 0.1.0. Installed Claude Code is
  2.1.289; Claude is not involved in the measurement.
- Companion revalidation log: `docs/revalidation/2026-10-05-hooks.md`.
- **Budget** (`PLAN-final-plan.md` §4.2): wall-time p95 ≤ 60 ms and peak RSS ≤ 25 MB.

## Method

Each command runs N times in sequence, after 10 warm-up runs. Every run starts a fresh process through `Popen`.
The script writes the payload to stdin and closes it, then reaps the process with `os.wait4`:

- **wall time:** from just before `Popen` until `wait4` returns;
- **peak RSS:** `ru_maxrss` (bytes on macOS);
- **CPU time:** `ru_utime + ru_stime`.

The payload is a 300-byte `Stop` event shaped like Claude's. The environment carries six allowlisted variables
with synthetic values, so the A3 capture runs too. Every write goes to `target/bench-data`; the home directory is
never touched.

The commands:

| Command | Why |
|---|---|
| `python -c pass` in the same venv | Python start-up cost: the floor under any Python hook |
| `fylgja hook Stop` (the venv's console script) | The measured hook: one envelope written, with fsync and rename |
| `/bin/sh -c 'f=…; [ -x "$f" ] \|\| exit 0; exec "$f" hook Stop'` | The guarded form an installer registers (A22), shell included |
| `fylgja hook StatusLine` | The statusline path. After the first write, the 2 s coalescing skips most runs |

The machine was in normal use during every run, with Erik's Claude sessions working. The load average was 4.5 to
9.2. The tails (p99, max) reflect that load, not the hook.

## Results

Final run (`-n 300`, load average 9.22 / 7.49 / 6.88 at the start). All 620 `hook Stop` runs, measured and
warm-up, wrote exactly one envelope each.

| Command | Runs | Exit | p50 ms | p95 ms | p99 ms | max ms | CPU ms (mean) | RSS MB |
|---|---|---|---|---|---|---|---|---|
| `python -c pass` (same venv) | 300 | 0 | 16.5 | 20.4 | 148.5 | 635.3 | 13.7 | 17.5 |
| `fylgja hook Stop` | 300 | 0 | 23.8 | 27.3 | 34.2 | 42.3 | 19.8 | 20.5 |
| `sh -c` guard, then `fylgja hook Stop` | 300 | 0 | 28.5 | 41.4 | 72.0 | 256.7 | 23.1 | 20.5 |
| `fylgja hook StatusLine` | 300 | 0 | 22.8 | 27.9 | 33.1 | 42.1 | 19.7 | 20.5 |

`fylgja hook Stop` across the three runs:

| Run | Load (1 min) | p50 ms | p95 ms | p99 ms | Peak RSS MB |
|---|---|---|---|---|---|
| `-n 300 --rebuild` | 4.49 | 23.9 | 30.8 | 40.4 | 20.5 |
| `-n 500` | 8.03 | 23.9 | 27.4 | 77.0 | 20.5 |
| `-n 300` (final) | 9.22 | 23.8 | 27.3 | 34.2 | 20.5 |

**Verdict: the budget is met.** The worst p95 was 30.8 ms against 60 ms, and the peak RSS was 20.5 MB against
25 MB. PreToolUse therefore stays in the default event set (`[hooks] events` in `defaults.toml`), and no
fallback decision record is needed.

## Where the time goes

- **Interpreter start-up** is most of it: 16.5 ms median and 17.5 MB RSS for `python -c pass` in the same venv.
  The venv's `.pth` processing is included.
- **The hook adds about 7 ms and 3 MB** on top. `-X importtime` puts the hook's imports at about 8 ms with the
  profiler's own overhead. Of that, `json` is about 5 ms, because it pulls in `re` and `enum`, which `fylgja.paths`
  needs anyway. `fylgja.paths` with `pathlib` is about 2 ms. `typing` is never imported: `hook.py` guards its
  annotations behind a local `TYPE_CHECKING`. `importlib.metadata` is avoided too, since a version lookup cost
  about 20 ms; `fylgja.hook.FYLGJA_VERSION` is a constant, and a test keeps it equal to the distribution version.
- **The guarded shell form** adds about 5 ms at the median for `/bin/sh` itself.
- **CPU** is about 20 ms per event.

As a rough load estimate, take a busy session firing about two hook events per tool call, at ten tool calls a
minute. That is 20 events a minute, or about 0.4 s of CPU a minute, which is roughly 0.7 % of one core. Eleven
sessions that busy at once would use about 7 % of one core, under 1 % of this 12-core machine.

## Notes

- **Tail latency.** Under load the tails reach hundreds of milliseconds. That is the machine scheduling a new
  process, as the bare interpreter's own 635 ms maximum shows, not the hook waiting on anything. Claude does not
  wait for an async hook, and the hook bounds itself at 3 s with its alarm.
- **No further optimisation is needed.** If the budget is ever missed, there are two levers left. `-I` or `-S` on
  the console script's interpreter would skip `site` and the `.pth` files. Moving `json` behind the size cap
  would avoid parsing in the common case, but the envelope still needs `json.dumps`.
- **Rerun** this measurement when the Python version, the hook's imports or the venv layout changes. The command
  is at the top of this note.
