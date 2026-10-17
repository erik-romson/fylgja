# The norn collector

`fylgja.collectors.norn` (capability `SESSIONS`) shows every norn pipeline run with its pipeline, current stage,
waiting-for-input state and liveness. Sources, in order of trust (A10, decision 0037):

1. **The plugin.** norn's own `fylgja` event subscriber (WP3.3(a), in the norn repository) writes `fylgja.hook/1`
   envelopes with `source` `norn` into `hook-inbox/norn/` (`docs/api/inbox-envelope.md`). The collector drains that inbox
   with the shared inbox drainer, in consumer mode, and keeps the run state in memory.
2. **The files.** Every 30 s the collector looks for norn's `<stem>.history` files (next to a `<stem>.checkpoint` or a
   `<stem>.runs/`) under `roots` and in the directories of runs the plugin reported. The last line of each run id wins;
   `<stem>.runs/<n>/run.json` gives the header and the tail of `events.jsonl` the stage in flight, a pending
   `WaitingInput` and the end. Nothing in them says whether a run is alive.
3. **The process table**, for an unfinished run the plugin never reported: `ps -A -o pid=,args=` and `lsof -d cwd`
   match its `argv` and working directory. No match: `stale`.

## Configuration (`[collectors.norn]`)

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | |
| `roots` | `["~/projects"]` | Directories walked for `*.history` files. |
| `max_depth` | `3` | How deep below a root. |
| `scan_interval_seconds` | `30` | How often the files and processes are read. |
| `poll_interval_seconds` | `2` | How often the state is evaluated (a heartbeat that stops is seen within this). |
| `heartbeat_timeout_seconds` | `90` | A plugin run whose newest event is older is `stale`. |
| `max_age_seconds` | `21600` | Files older than this are not read; a stale run is listed this long. |
| `finished_seconds` | `900` | A finished run stays listed this long. |
| `heartbeat_seconds` | `30` | An unchanged snapshot is emitted again after this. |
| `ps_command`, `lsof_command`, `command_timeout_seconds` | `/bin/ps`, `/usr/sbin/lsof`, `5` | |
| `inbox_poll_seconds`, `inbox_watch` | `1`, `auto` | The inbox poll, and whether kqueue watches it. |

The inbox is drained whenever it exists; there is no switch for it besides `enabled`.

## The envelope payload

`payload_text` is a JSON object. Every event carries the run's identity, so a run is rebuilt from any one event (a
daemon that restarted mid-run recovers at the next heartbeat). Unknown keys and unknown event names are tolerated (an
unknown event still proves life). Only ids, names, statuses, counts and times are sent: never a prompt, an output or an
excerpt.

| Key | Type | On | Meaning |
|---|---|---|---|
| `event` | string | all | `RunStarted`, `StageStarted`, `StageFinished`, `CallingAgent`, `TurnEvent`, `WaitingInput`, `RunPaused`, `RunResumed`, `RunCancelled`, `RunFinished` or `Heartbeat`. The envelope's `event_arg` is the fallback. |
| `pipeline` | string | all | The pipeline's name. |
| `state_key` | string | all | The state key (a path, absolute or relative to `cwd`). |
| `history_run_id` | integer | all | The run's id in the `.history` file; the key's run id. |
| `run_id` | string | all | norn's uuid of the run (used when there is no history id). |
| `pid`, `cwd` | integer, string | all | The norn process and its working directory. |
| `argv` | array of strings | `RunStarted` | The arguments norn saw, as in `run.json`. |
| `provider`, `stage_total` | string, integer | `RunStarted` | `claude-code` (only Claude runs nest sessions); the number of top-level stages, when known. |
| `stage`, `stage_index`, `attempt` | string, integer, integer | stage events, `Heartbeat` | The stage's name and its 1-based position. |
| `status`, `success` | string, boolean | `StageFinished` | `passed`, `failed`, `skipped`, `skipped_condition`, `cached` or `cancelled`. |
| `session_id` | string | `CallingAgent`, `TurnEvent` | The agent session the run drives. |
| `kind` | string | `WaitingInput` | `agent`, `budget`, `step`, `failure_recovery` or `timeout_recovery`. |
| `success` | boolean | `RunFinished` | `false` is `failed`. |

## What it emits

Complete `session` and `session_activity` snapshots of the listed runs (key `norn:<stem>@<h>:<run id>`, kind `norn`,
the pipeline as `name` and `title`), so a run no longer listed is ended in the store; one `session` event per agent session
of a live Claude run, with `parent_key` set to the run (`/now` shows them in `children[]`). `SessionActivity.state` is
`running`, `waiting_input` (with `waiting_reason`: "agent question", "budget exceeded", "step gate", "failure recovery" or
"timeout recovery"), `paused`, `stale`, `passed`, `failed` or `cancelled`; `stage` has the name, the index and, when norn
said so, the total. The one-liner (rule 2): "`<pipeline>` · `<stage>` (`<index>`/`<total>`)", or "`<pipeline>` `<state>`"
for a finished or stale run; a run waiting for input reads "Waiting for input: `<reason>`" (rule 1).

A run waiting for input is an `input` entry of `waiting[]`; a finished run is a `run_finished` entry until its project
gains focus (decision 0056). The Overview lists a run on the workspace card of the agent session it drives, right
above that session; without one, on the card of its project's workspace.

## Failure modes

| Situation | Result |
|---|---|
| No scan root exists (and no known run directory) | `degraded`, `roots_missing`. |
| A state directory cannot be read | `degraded`, `state_unreadable`; the other directories are still read. |
| `ps` or `lsof` missing, failing or timing out | `degraded`, `ps_missing`/`ps_failed`/`ps_timeout` (or the `lsof_` kinds); the last match is kept. |
| The inbox is not a directory or cannot be committed | `degraded`, from the drainer (`inbox_not_a_directory`, ...). |
| An envelope that is not valid, names no run, or whose source is not `norn` | Moved to `hook-inbox/norn/quarantine/`. |
| The heartbeat stops (90 s) or the pid is gone, no finish event | `stale`. |
| No plugin and no matching process for an unfinished run | `stale`. |

By hand (needs the norn plugin of WP3.3(a)): start a norn run and watch
`curl -s 127.0.0.1:7319/now | jq '.sessions[] | select(.kind=="norn")'`; the stage changes within 2 s, a `WaitingInput`
shows in `.one_liner`, and after `kill -9` of the norn process the state is `stale` within 90 s.
