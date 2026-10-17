# `waiting[]` in `/now` (WP6.1)

`waiting[]` lists everything that waits on the person, most urgent first. The field is part of
`fylgja.now/1` from the start; this work only fills it. It is present (possibly empty) while any
sessions, windows or project collector is available, and absent otherwise.

| `kind` | Source | `target` (`target_kind`) | Rank |
|---|---|---|---|
| `permission` | session state `waiting_permission` | session key (`session`) | 0 |
| `input` | session state `waiting_input` (Claude, norn) | session key (`session`) | 0 |
| `turn_done` | finished turn, not yet `seen` | session key (`session`) | 1 |
| `run_finished` | run (norn) `passed`, `failed` or `cancelled`, its project not focused since | session key (`session`) | 1 |
| `build_failed` | `projects[].build.state = fail`, project touched today | project path (`project`) | 2 |
| `review_requested` | open PR with `review_decision = REVIEW_REQUIRED`, project touched today | project path (`project`) | 2 |
| `unread` | explicit unread marker in a window title (A15) | window id (`window`) | 3 |

* Order: rank, then `since` oldest first (no `since` last).
* One target counts once: it keeps its highest-ranked entry.
* "Touched today": a focus span of the project or session activity since local midnight.
* A `turn_done` leaves when the session becomes `seen` (its window gained focus).
* A `run_finished` leaves when the run's project gains focus after the run's `state_since`, or when
  the run leaves the live view (`[collectors.norn] finished_seconds` after its end). Its `reason` is the
  run's name and state, for example `plan_with_review_v2 failed` (decision 0056).
* Changes publish `now.changed` over `/events`, including those of window context items and of the
  projects touched today, which the store does not hold (`LiveView.touch`).
* Logic: `src/fylgja/core/waiting.py` (pure); filled in `fylgja.api.now.build_now`.
