# Codex plan import — Plan: Codex `update_plan` steps as read-only session todos

## Goal

Import the plan Codex keeps for a session (its `update_plan` tool calls, recorded in the rollout JSONL) as a
read-only TODOS list, source `codex-plan`, so Codex plans appear on the page's Todos list as "from codex", grouped
under the session, next to `claude-tasks` and `opencode-todo`. The target: a live Codex session that called
`update_plan` has exactly its newest plan as `session`-scoped todos (`pending`→open, `in_progress`→next,
`completed`→done). An ended session's plan is `source_absent`. A rollout that has not been read whole changes
nothing.

Scope constraints:

- `core/todo_import.py`, `collectors/todo_lists.py`, the writer, the store, the API and the page do not change. They
  are already source-agnostic.
- The busy/idle tail (`read_rollout_tail` plus `analyze_rollout`) is left as it is.
- The CI gate from `AGENTS.md` stays green after every phase. The branch-coverage gate is 85%.

## Empirical findings (shapes and counts only)

All checks were read-only: `jq`, `grep -c` and `stat` over `~/.codex/sessions/**/rollout-*.jsonl`, and `strings` over
the installed Codex binary. No plan text, prompt, path or id is recorded here.

| Question | Observed |
|---|---|
| Rollouts in sample | 117 files; size median 646 KiB, p90 1685 KiB, max 3626 KiB; longest line 592 KiB |
| Rollouts with `update_plan` | 7 files, 23 calls, all from CLI versions 0.47.0–0.116.0 |
| Newer rollouts | **0 calls in 89 rollouts from 0.120.0–0.154.0.** No `update_plan` inside the code-mode `exec`/`custom_tool_call` inputs either |
| Installed Codex 0.154.0 binary | still defines `update_plan` (48 string hits): params `plan` (steps `step`, `status`) and optional `explanation`; statuses `pending`, `in_progress`, `completed`; "At most one step can be in_progress at a time"; the system prompt says "call `update_plan` … mark all steps as `completed`" |
| Record type of a call | top-level `type: "response_item"`, `payload.type: "function_call"`, `payload.name: "update_plan"`, payload keys exactly `arguments, call_id, name, type` (23/23) |
| `arguments` | a JSON **string** that decodes to an object with keys `plan` (15/23) or `explanation, plan` (8/23); `explanation` is a string |
| `plan` items | array of objects with keys exactly `status, step` (23/23). **No item id** |
| Status values seen | `pending`, `in_progress`, `completed` only; at most one `in_progress` per call |
| Plan size | 3–6 steps per call; longest `step` 173 characters |
| Whole-list rewrite | yes. Every call carries the full list. In 7/7 rollouts the step texts were identical across all calls of the rollout, and only statuses changed. Position keys are stable in practice |
| Pairing | each call has one `response_item` / `function_call_output` with the same `call_id` (23/23). `output` is a string; all 23 equal Codex's fixed 12-character acknowledgement `Plan updated`. Nothing else in the output |
| Other plan records | `event_msg` `plan_update` is **not** persisted (0 lines). One `event_msg` / `item_completed` item of type `Plan` (`id`, `text`, `type`: free-text plan-mode proposal, 0.130.0). No plan fields in `world_state` (71 records) |
| Where the last call sits | bytes after the newest `update_plan`: 6, 48, 181, 322, 734, 919, 1704 KiB. **4 of 7 are beyond the 256 KiB tail.** The tail only widens (to 4 MiB) until a turn event shows, so the existing tail cannot see the plan reliably |
| Resume | 0 of 117 rollouts carry more than one `session_meta` line, so no resumed rollout was observed |

**Verdict.** The shape is verified against real rollouts for Codex 0.47.0–0.116.0. It is **unverified on 0.154.0**:
the tool and its field names are still in the binary, but no 0.154.0 rollout records a call. Phase 1 adds a live
check (`tests/live/test_codex_plan_live.py`) to run by hand once a current rollout contains one. Note that current
Codex has called `update_plan` rarely or never in this sample, so the import may stay quiet until a model uses the
tool.

## Where the work goes

| Region | Today | Root cause | Change | Phase |
|---|---|---|---|---|
| `codex/parse.py` (630 LoC) | no plan parsing | — | `PLAN_SOURCE`, `PlanStep`, `plan_steps`, `PlanScan`, `plan_snapshot` (+≈90 LoC) | 1 |
| `codex/collector.py` `_facts` L303–330 | tail ≤ 4 MiB, widened only until a turn event | plan may be MiB before EOF | separate incremental whole-file reader per live rollout | 2 |
| `codex/adapter.py` (375 LoC) | `read_rollout_tail` only | — | `RolloutPlan` wrapping `claude.transcript.TranscriptTail` + `parse.PlanScan` (+≈45 LoC) | 2 |
| `codex/collector.py` `poll` L137–165 | emits sessions + activity | — | `_plan(...)` in `_listed`, `_emit_plans(live)` after `_emit` (+≈60 LoC) | 2 |
| `codex/plugin.py` L38 | `{SESSIONS}` | contract check 3 rejects undeclared kinds | `{SESSIONS, TODOS}` | 2 |
| `tests/unit/test_codex_collector.py` L617–619 | asserts `{"sessions"}` | — | asserts `{"sessions", "todos"}` | 2 |
| `docs/decisions/` | 0001–0046 | — | `0047-codex-plan-import.md`, `Amendment: none` | 2 |
| `tests/unit/test_agent_task_import.py` (478 LoC) | Claude + OpenCode end to end | — | Codex end-to-end case through real writer and store | 3 |

## Phase 1 — Pure plan parser, fixture, revalidation log and live shape check (sonnet)

**Adds ≈ 90 LoC production (pure), ≈ 140 LoC tests, 1 fixture file, 1 revalidation log. 0 behaviour change.**

### What changes

`src/fylgja/collectors/codex/parse.py` gains pure functions only. Nothing calls them yet:

```python
PLAN_SOURCE: Final = "codex-plan"
"""The ``todos.source`` of an imported Codex plan step (decision 0047)."""
PLAN_TOOL: Final = "update_plan"
PLAN_TEXT_CHARS: Final = 1000          # == core.todo_import.MAX_TEXT_CHARS
PLAN_STATUSES: Final = frozenset({"pending", "in_progress", "completed"})

@dataclass(frozen=True)
class PlanStep:
    key: str      # "step-<n>", n = 0-based position in the call's plan list
    status: str   # Codex's own status, one of PLAN_STATUSES
    text: str     # raw step text cut to PLAN_TEXT_CHARS (may be blank)

def plan_steps(arguments: object) -> tuple[PlanStep, ...] | None: ...
def plan_call(entry: Mapping[str, Any]) -> tuple[PlanStep, ...] | None: ...   # None = not a valid call

class PlanScan:
    """The newest valid plan of the rollout read so far (pure fold, no I/O)."""
    steps: tuple[PlanStep, ...]   # () until a valid call is seen
    calls: int                    # valid update_plan calls folded
    ignored: int                  # update_plan calls whose arguments did not parse
    def feed(self, entries: Iterable[Mapping[str, Any]]) -> None: ...

def plan_snapshot(key: str, steps: Iterable[PlanStep], observed_at: datetime) -> TodoSnapshot: ...
```

`plan_snapshot` mirrors `claude/parse.py` `task_snapshot` (L450–471). It calls
`todo_list_snapshot(SOURCE, PLAN_SOURCE, key, …)` with
`TodoPayload(text=step.text, status=agent_status(step.status), scope_kind="session", scope_ref=key, link=key)` and
leaves out steps with blank text.

### Why this is the right call

- It mirrors the Claude `TodoWrite` keying (`todo-<n>`, decision 0041). The data shows no ids and whole-list
  rewrites. A reorder rewrites texts under the same keys, which 0041 already accepts for `TodoWrite` and OpenCode.
- A strict parse (an unknown status or a non-string `step` rejects the **whole call**) copies Codex's own behaviour.
  Codex deserializes `StepStatus` strictly and keeps its old plan on a bad call. So the last valid call is the plan
  Codex itself holds. This is why `agent_status`'s lenient "unknown is open" is not applied to raw input.
- The `function_call_output` is not needed. All 23 outputs are the fixed acknowledgement, and the only failure path
  is bad arguments, which the strict parse already rejects. Pairing would add state for no information.
- Landing the parser alone keeps the phase pure and fully unit-tested. Nothing emits, so contract check 3 is
  unaffected.

### Actions

1. In `src/fylgja/collectors/codex/parse.py`:
   - Add the constants and `PlanStep` above to a new section `# Plans` placed before `# Records` (currently L543).
     Add the new names to `__all__` (L45–78).
   - `plan_steps(arguments)` takes `arguments`, which must be a `str` that `json.loads` to a `dict`. Its `plan` must
     be a `list`, and every element a `dict` with a `str` `step` and a `status` in `PLAN_STATUSES`. Anything else
     returns `None`. On success it returns
     `tuple(PlanStep(f"step-{n}", status, step[:PLAN_TEXT_CHARS]) for n, …)`. `explanation` is never read. Catch
     `ValueError` and `RecursionError`, as `claude.facts.decode_line` does.
   - `plan_call(entry)` returns `plan_steps(payload["arguments"])` only when `entry["type"] == "response_item"`,
     `payload["type"] == "function_call"` and `payload["name"] == PLAN_TOOL`. Otherwise it returns `None`, and the
     caller counts it as "not a call" rather than "ignored". To tell the two apart, it returns a sentinel or a small
     enum. Pick one and keep it private.
   - `PlanScan.feed` replaces `steps` with each valid call and counts `calls` and `ignored`. `plan_snapshot` is as
     above, with `from fylgja.collectors.todo_lists import agent_status, todo_list_snapshot`, the same import
     `opencode/parse.py` L47 uses.
   - Extend the module docstring with a `**The plan**` paragraph covering the source, the keys, the statuses, the
     strict parse and the fact that the output is not read.
2. Fixture (synthetic, written by hand in the observed shapes; **never copied from a real rollout**):
   - Write `target/fixture-staging/codex/rollouts/plan.jsonl`. It holds `session_meta` (cwd `/work/example/app`),
     `turn_context`, `task_started`, then three `update_plan` calls each followed by its `function_call_output`
     (`"Plan updated"`): call 1 is 3 steps all `pending`; call 2 has an `explanation` and statuses
     `completed, in_progress, pending`; call 3 is invalid (`status: "blocked"`) and must be ignored. Then a step
     whose text is blank, an `exec_command` call with output, and `task_complete`. Step texts are invented ("Read
     the parser", "Write the tests", "Update the docs").
   - Run `uv run --frozen python scripts/sanitize_fixture.py codex/rollouts`. It copies additively, so only
     `plan.jsonl` lands in `tests/fixtures/codex/rollouts/`.
   - Add a `plan` row to the rollout list in `tests/fixtures/codex/README.md`.
3. Tests in `tests/unit/test_codex_parse.py`. Add a `# Plans` section:
   - `test_plan_steps_are_keyed_by_position_and_keep_codex_statuses`
   - `test_a_call_with_an_unknown_status_or_shape_is_ignored_whole` (parametrized: non-string arguments, bad JSON,
     `plan` not a list, item without `step`, `status: "blocked"`, item not an object)
   - `test_the_newest_valid_call_wins_and_explanation_is_never_kept` (fold `plan.jsonl`: steps from call 2,
     `calls == 2`, `ignored == 1`)
   - `test_other_function_calls_and_outputs_do_not_touch_the_plan`
   - `test_a_rollout_without_update_plan_has_an_empty_plan`
   - `test_a_long_step_is_cut_and_a_blank_step_is_left_out_of_the_snapshot`
   - `test_plan_snapshot_is_one_complete_session_list` (`entity_id == "codex-plan:codex:<id>"`, item ids
     `step-0…`, statuses `done/next/open`, scope and link equal to the key, `source == "codex"`)
   - `test_the_plan_fixture_does_not_change_the_busy_state_rules` (`analyze_rollout(plan.jsonl)` is idle with no
     pending call)
4. Revalidation log `docs/revalidation/2026-10-08-codex-plan.md`, in the format of `2026-10-06-codex.md` (header
   paragraph plus a `Claim | Command run | Observed | Verdict | Action` table). It holds the findings table above:
   shapes and counts only, verdict `holds` for 0.47–0.116 and `unknown` for 0.154.0.
5. Live shape check `tests/live/test_codex_plan_live.py` (`pytestmark = pytest.mark.live`). It reads
   `adapter.default_codex_home() / "sessions"` read-only, starts nothing and needs no daemon. It asserts that every
   `update_plan` `function_call` line parses with `parse.plan_steps` and that every call has one paired output.
   Failure messages hold counts and CLI versions only, never text or paths. It skips when no rollout holds a call.
   Run it by hand: `FYLGJA_RUN_LIVE=1 uv run --frozen pytest -m live tests/live/test_codex_plan_live.py`.

### Test command for this phase

```sh
uv run --frozen ruff format --check . && uv run --frozen ruff check . && uv run --frozen mypy src/fylgja && \
uv run --frozen pytest -q -m "not live" tests/unit/test_codex_parse.py tests/unit/test_one_liner.py \
  tests/unit/test_fixture_hygiene.py tests/live/test_codex_plan_live.py
```

(The live file is collected and deselected. That catches import errors without reading the home directory.)

Commit: `feat: parse Codex update_plan calls`

## Phase 2 — Collector emits the plan list; plugin declares TODOS; decision 0047; docs (opus)

**Adds ≈ 105 LoC production, ≈ 170 LoC tests, 1 decision record. Behaviour: Codex sessions now carry a
`codex-plan` list.**

### What changes

Before: `CodexCollector.poll()` (L137–165) emits sessions and activity only, and `plugin.py` L38 declares
`{Capability.SESSIONS}`.

After:

```python
# adapter.py
class RolloutPlan:
    """Reads one rollout from byte 0, incrementally, and folds its update_plan calls."""
    def __init__(self, fs: FileSystem, path: Path, *, max_read: int) -> None:
        self._tail = TranscriptTail(fs, path, max_read=max_read)   # fylgja.collectors.claude.transcript
        self.scan = parse.PlanScan()
        self.backlog = False                       # bytes left to read
        self.present = True
    def update(self) -> None:                      # OSError propagates to the collector
        update = self._tail.poll()
        if update.reset or not update.exists:
            self.scan = parse.PlanScan()
        self.present, self.backlog = update.exists, update.more
        self.scan.feed(update.entries)
    @property
    def whole(self) -> bool:
        return self.present and not self.backlog

# collector.py, poll()
emitted = self._emit(listed)
self._emit_plans({key for key, _, _ in listed})
return emitted
```

### Why this is the right call

- **A separate incremental reader, not a wider tail.** 4 of 7 real rollouts have their newest plan beyond 256 KiB,
  and the tail stops widening as soon as a turn event shows. Only a read from byte 0 can say "this is the whole
  plan". `TranscriptTail` already does that: it carries torn lines, resets on shrink, inode change or rewrite, caps a
  poll at 8 MiB (`more` = backlog), and skips lines over 16 MiB. Max rollout seen is 3.6 MiB, so one poll reads a
  new rollout whole, and later polls read only appended bytes.
- **Reuse, don't move.** Cross-collector imports are an existing pattern: `opencode/collector.py` L56 and
  `norn/collector.py` L48 import `claude.inbox`. Moving `TranscriptTail` to a shared module would touch Claude
  tests (`test_claude_transcript.py`, `test_costs_parity.py`) for no gain here.
- **The busy/idle tail stays as it is.** It costs at most one extra read of appended bytes per changed rollout per
  poll. Folding facts into the incremental reader would be a refactor of decision 0035's code path, which is out of
  scope.
- **Emission rules are copied from Claude** (`claude/collector.py` L360–376) and OpenCode (L307–322), so all
  three importers behave the same. A list is emitted when it differs from the last emitted one. A live session
  emits once with `()` on its first whole read. An ended session gets one complete empty snapshot.
- The capability must flip in **the same commit** that first emits TODOS. Contract check 3
  (`testing/contract.py` L355) fails on an undeclared kind.

### Actions

1. `src/fylgja/collectors/codex/adapter.py`: add `RolloutPlan` as sketched, under the `# Rollout files` section
   (after L270). Add `"RolloutPlan"` to `__all__`. Import `TranscriptTail` from
   `fylgja.collectors.claude.transcript`, and `Path` is already imported. Update the module docstring: the rollout
   is also read whole and incrementally for the plan.
2. `src/fylgja/collectors/codex/collector.py`:
   - `PollStats` (L97–106): add `plans` (listed sessions with a non-empty plan), `plan_backlog` (listed sessions
     whose rollout is not read whole yet) and `plan_calls_ignored` (sum of `scan.ignored`).
   - `__init__` (L110–133): add `self._plans: dict[str, RolloutPlan] = {}` (by rollout path),
     `self._read_plans: dict[str, tuple[parse.PlanStep, ...]] = {}` (by session key, reset at the start of
     `_listed`), `self._plan_lists: dict[str, tuple[parse.PlanStep, ...]] = {}` (last emitted, by session key) and
     `self.plan_emissions = 0`.
   - `_listed` (L271–301): after `facts = self._facts(path)`, call `self._plan(parse.session_key(thread_id), path)`.
     Prune `self._plans` to `live_paths` next to `self._tails` (L300).
   - Add the module constant `PLAN_MAX_READ: Final = transcript.DEFAULT_MAX_READ` (8 MiB) next to
     `DEFAULT_TAIL_BYTES` (L69).
   - New `_plan(key, path)`: get or create a
     `RolloutPlan(self._seams.fs, Path(path), max_read=PLAN_MAX_READ)`, then call `update()`. On
     `OSError`, drop the reader and return, so there is no list (a failed read). Store
     `self._read_plans[key] = reader.scan.steps` only when `reader.whole`. Count a backlog otherwise.
   - New `_emit_plans(live: set[str]) -> int`: a line-for-line mirror of `claude/collector.py` `_emit_task_lists`
     (L360–376), using `parse.plan_snapshot` and `self._plan_lists`, and incrementing `self.plan_emissions`.
   - `poll()` (L165): `emitted = self._emit(listed)`, then `self._emit_plans(...)`, then `return emitted`. The early
     return on `CommandFailure` (L147–149) emits no plan, so the failed-read rule holds.
   - `doctor()` (L403–409): append `; {plans} plans, {plan_backlog} still reading, {plan_calls_ignored} plan calls
     ignored` to the OK detail. Counts only.
   - Module docstring: add a `**Plans (WP4.3, decision 0047).**` paragraph after step 5 of "One poll".
3. `src/fylgja/collectors/codex/plugin.py` L38: `capabilities=frozenset({Capability.SESSIONS, Capability.TODOS})`,
   with the comment style of `opencode/plugin.py` L38
   (`# SESSIONS: sessions and activity. TODOS: each session's plan (WP4.3).`).
4. `tests/codex_support.py`: add `PLAN_ROLLOUT = ROLLOUT_FIXTURES / "plan.jsonl"`. `ROLLOUT_OF_THREAD` (L32–40)
   stays as it is. Tests copy `plan.jsonl` over thread 1's rollout path when they need it.
5. `tests/unit/test_codex_collector.py`:
   - Change `test_the_plugin_declares_only_session_capability` (L617–619) to
     `test_the_plugin_declares_sessions_and_todos`, which expects `{"sessions", "todos"}`.
   - Add a `# Plans` section, using `Rig`, `FakeProcessRunner` and `FakeClock` only:
     - `test_a_session_with_a_plan_emits_it_as_one_complete_list`
     - `test_a_session_without_a_plan_emits_one_empty_list_then_nothing`
     - `test_an_unchanged_plan_is_not_re_emitted_but_a_status_change_is` (append a new call to the copied file,
       `await rig.clock.advance(2.0)`, poll)
     - `test_a_plan_far_before_the_end_of_a_long_rollout_is_found` (pad with 1 MiB of `token_count` events after the
       call; settings `tail_bytes=1024`)
     - `test_a_rollout_read_over_several_polls_emits_only_when_whole`: monkeypatch
       `fylgja.collectors.codex.collector.PLAN_MAX_READ` to 4 KiB. A list must appear only after the backlog drains.
       This only works if `RolloutPlan.__init__` takes `max_read: int` and passes it to `TranscriptTail`, and if
       `_plan` passes the module constant `PLAN_MAX_READ: Final = transcript.DEFAULT_MAX_READ` at call time. A
       default argument is bound when the function is defined, so monkeypatching it would not work.
     - `test_a_torn_last_line_is_completed_on_the_next_poll`
     - `test_a_replaced_rollout_restarts_the_plan` (shrink or rewrite)
     - `test_an_unreadable_or_missing_rollout_emits_no_list` (`chmod 000`, or delete the file while the process is
       listed)
     - `test_a_session_that_ends_emits_one_empty_list` (rescript `pgrep` with `returncode=1`, as OpenCode's test
       does)
     - `test_a_failing_lsof_emits_no_plan`
     - `test_a_resumed_rollout_keeps_the_newest_plan_from_its_earlier_run` (append a second `session_meta`, a new
       turn and no call: the earlier plan stands)
     - `test_doctor_counts_plans_without_text`
6. `tests/contract/test_codex_contract.py`: no code change is expected. Check 3 now sees TODOS records, and they
   must pass the schema, `dedup_key` and stability assertions. If check 3 fails, fix the collector, not the contract.
7. `docs/decisions/0047-codex-plan-import.md`, using the header of `0041`:

   ```text
   # 0047: Codex plan import: `update_plan` steps as session todos
   - Status: accepted
   - Amendment: none
   - Amends: —
   - Answers: —
   - Relates to: A5, A20
   - Date: <commit date>
   ## Context / ## Decision / ## Consequences
   ```

   Decision bullets come from "Locked decisions" 1–11 below. Context cites `docs/revalidation/2026-10-08-codex-plan.md`
   and the 0.154.0 gap. `Amendment: none` means `docs/PLAN-final-plan.md` §2 (A1–A24, checked by
   `test_plan_lists_a1_to_a24_contiguously`) needs **no** edit, and so does WP4.3's text. The record is the
   refinement, as 0041 was for `claude-tasks` and `opencode-todo`.
8. `docs/collectors/codex.md`:
   - L3: change the capability text to "capabilities `sessions` and `todos`".
   - "What it reads" table: add a row "the whole rollout, read incrementally from byte 0 (at most 8 MiB per poll) |
     the session's plan (`update_plan` calls)".
   - Add a `- **Plan.**` paragraph in the style of `opencode.md` L28–30 (source `codex-plan`, keys `step-<n>`,
     statuses, ended session empties its list, a partial read emits nothing, see decision 0047).
   - Tests section: add `tests/live/test_codex_plan_live.py`.
9. `src/fylgja/defaults.toml` L97–98: extend the comment with "and the session's plan (read-only todos)". No new
   setting.

### Test command for this phase

```sh
uv run --frozen ruff format --check . && uv run --frozen ruff check . && uv run --frozen mypy src/fylgja && \
uv run --frozen pytest -q -m "not live" tests/unit/test_codex_parse.py tests/unit/test_codex_collector.py \
  tests/contract/test_codex_contract.py tests/unit/test_one_liner.py tests/unit/test_decision_log.py \
  tests/unit/test_claude_transcript.py tests/integration/test_phase3_agents.py tests/unit/test_ingest_gate.py
```

Commit: `feat: import the Codex session plan as read-only todos`

## Phase 3 — End to end through the writer, the store and the daemon (sonnet)

**Adds ≈ 80 LoC tests. 0 production change.**

### What changes

`tests/unit/test_agent_task_import.py` covers Claude and OpenCode end to end (for example
`test_opencode_todo_rows_map_to_the_session_scope`, L343–399), but not Codex. This phase adds the same proof for
`codex-plan`, using the real `CodexCollector` (the `codex_support.Rig`), the real writer and the store from
`todo_import_support.open_store`.

### Why this is the right call

The Phase 2 unit tests prove what the collector emits. Only a store-backed test proves the reconciliation rules of
`core/todo_import.py` hold for this source: ownership (`codex` owns `codex-plan`), update in place under position
keys, `source_absent` on session end, restore, and an unread rollout leaving rows untouched. Tests only, so the
phase is independently mergeable.

### Actions

1. Update the module docstring of `tests/unit/test_agent_task_import.py`: add Codex's plan (`codex-plan`).
2. Add `CODEX = "codex-plan"` and a helper `_codex_poll(rig, store, seen)`, mirroring `_opencode_poll`.
3. Add `test_codex_plan_steps_map_to_the_session_scope_and_update_in_place`:
   - copy `plan.jsonl` to thread 1's rollout and poll;
   - check the rows `codex:<id>#step-0..2` with statuses `done/next/open`, and that scope and link equal the key;
   - append a call that completes step 1 and poll again: same row id, status `done`, still 3 rows;
   - make the rollout unreadable (`chmod 000`) and poll: rows unchanged;
   - restore it, then end the process (`pgrep` returns 1): every row `source_absent`;
   - check `rig.collector.plan_emissions`.
4. Add `test_a_codex_plan_shows_under_its_session_and_is_read_only`. The daemon path already covers this through the
   `Synthetic("agents", …)` test at L428. Extend it with a parametrized source `codex-plan` and key `codex:t-1`,
   instead of adding a second near-copy. Assertions: `/now` session `todos[0].source == "codex-plan"`, and `PATCH`
   gets 409.

### Test command for this phase

```sh
uv run --frozen ruff format --check . && uv run --frozen ruff check . && \
uv run --frozen pytest -q -m "not live" tests/unit/test_agent_task_import.py tests/unit/test_todo_reconcile.py
```

Commit: `test: cover the Codex plan import end to end`

## Sequencing & merge boundaries

- **Phase 1** adds pure functions, a fixture, a doc and a live-only test. Nothing calls the parser, so every existing
  test is untouched and green.
- **Phase 2** depends on Phase 1's parser. It flips the capability in the same commit as the first TODOS emission,
  so contract check 3 stays green, and it ships the decision record and docs with the behaviour.
- **Phase 3** depends on Phase 2's emission. It is tests only and changes no production code.

The done-gate after Phase 3 (and before every commit) is the CI list from `AGENTS.md`:

```sh
uv lock --check
uv sync --frozen --all-groups
uv run --frozen ruff format --check .
uv run --frozen ruff check .
uv run --frozen mypy src/fylgja
uv run --frozen pytest -m "not live" --cov=fylgja --cov-branch --cov-fail-under=85
```

Then run `FYLGJA_RUN_SLOW=1` once, and by hand
`FYLGJA_RUN_LIVE=1 uv run --frozen pytest -m live tests/live/test_codex_plan_live.py` (read-only).

## Locked decisions

1. **Source** `codex-plan`. **List ref** = the session key `codex:<thread id>`. **Entity id**
   `codex-plan:codex:<thread id>`. **Stored `source_ref`** `codex:<thread id>#step-<n>`.
2. **Item key** `step-<n>`, with `n` the 0-based position in the call's `plan` array. Steps have no ids and every
   call rewrites the whole list (verified 23/23). A blank step is left out, and the numbering of the others does not
   shift.
3. **Status** uses `todo_lists.agent_status`: `pending`→`open`, `in_progress`→`next`, `completed`→`done`. Raw input
   is parsed strictly first: an unknown status, a missing or non-string `step`, a non-list `plan` or undecodable
   `arguments` rejects the **whole call**, and the previous plan stands (counted in `plan_calls_ignored`).
4. **Scope** `session`, `scope_ref` = `link` = the session key. No `created_at` or `done_at` from the source: the
   core stamps them.
5. **Text** is `step` cut to 1000 characters in `parse.py`. The core collapses whitespace (`clean_text`) and the
   ingest gate redacts. `explanation` and the call output are never read, kept or logged.
6. **Only `response_item` / `function_call` / `name == "update_plan"` counts.** `function_call_output`, the
   `item_completed` `Plan` item (plan-mode free text) and `custom_tool_call` are not plan sources.
7. **Complete vs not.** A list is emitted only when the rollout has been read from byte 0 to its current end since
   the last reset (`RolloutPlan.whole`). A backlog, a missing file, an `OSError` or a failed `pgrep`/`lsof` poll
   emits nothing for that session. A whole read with no valid call is a complete, **empty** plan.
8. **Session end.** A session that was emitted and is no longer listed gets one complete, empty snapshot, and its
   reader is dropped. The writer's `close_session_lists` also covers a session that ended while the daemon was down.
9. **Resume.** The plan of a session is the newest valid call anywhere in its rollout, including calls from an
   earlier run of the same thread.
10. **Reader.** `fylgja.collectors.claude.transcript.TranscriptTail` is reused unchanged (default 8 MiB per poll,
    16 MiB max line). No new settings in `CodexSettings` or `defaults.toml`.
11. **Decision record** `docs/decisions/0047-codex-plan-import.md` with `Amendment: none`. No edit to
    `PLAN-final-plan.md` or to decision 0041.
12. If Phase 1's live check or a future rollout shows a different `update_plan` shape on 0.154+ (ids on steps, a new
    status, a record type other than `function_call`), the implementer **stops and asks** before changing keys or
    the status map. Keys are part of stored `source_ref`s.

## Risks

- *Risk*: format drift between Codex versions. The shape is unverified on 0.154.0, and a new status or step ids could
  appear. *Mitigation*: the strict parse ignores unknown shapes, so the last good plan stays and nothing is wiped;
  `plan_calls_ignored` shows in `fylgja doctor`; `tests/live/test_codex_plan_live.py` fails on any unparsable real
  call; decision 12 forces a stop-and-ask.
- *Risk*: current Codex never calls `update_plan` (0 of 89 rollouts since 0.120.0), so the feature is dormant.
  *Mitigation*: this is stated in decision 0047 and the revalidation log. The cost is one incremental read per live
  rollout, and nothing breaks.
- *Risk*: large rollouts make the first read slow. *Mitigation*: the read is capped at 8 MiB per poll (max seen
  3.6 MiB) and drains over several polls, with no list until whole. Later polls read only appended bytes. Phase 2
  tests the multi-poll backlog path.
- *Risk*: a session resumed from an old rollout shows a stale plan. *Mitigation*: this is locked as intended
  (decision 9), since it is the plan Codex last recorded for the thread. Completed steps are `done` and leave the
  live view; a new `update_plan` replaces the plan at once. Phase 2 tests the resumed case.
- *Risk*: concurrent writes, such as a torn last line or a rollout replaced or rewritten. *Mitigation*:
  `TranscriptTail` carries torn lines and resets on shrink, inode change or a changed head. A reset clears
  `PlanScan` and waits for a whole read. Phase 2 tests the torn and replaced cases.
- *Risk*: a rolled-back turn (`thread_rolled_back`, 4 events in the sample) leaves a plan that Codex discarded.
  *Mitigation*: this is documented as a known limit in 0047, and the next `update_plan` corrects it. Rollback
  replay is out of scope.
- *Risk*: emitting TODOS breaks the contract suite or the existing Codex tests. *Mitigation*: the capability flips
  in the same commit. The existing tests assert per kind (`of_kind`) and `emissions` counts sessions only.
  `rig.sink.records == []` holds on failure paths because a failed poll emits no plan.
- *Risk*: plan text leaks into logs or docs. *Mitigation*: the collector logs only kinds and counts, and doctor
  shows only counts. Fixtures are synthetic, pass through `sanitize_fixture.py`, and are checked by
  `test_fixture_hygiene.py`.

## What is explicitly out of scope

- `src/fylgja/api/static/index.html`, the navigator tests and `README.md`. They have uncommitted local changes:
  do not touch or stage them. `sourceLabel` (L252) already shows `codex-plan` as "from codex", and the README lists
  no import sources.
- `core/todo_import.py`, `collectors/todo_lists.py`, the writer, the store schema and migrations, and the API.
- Codex activity: no `facts.tasks` and no "open task" one-liner rule for Codex. This is a possible follow-up.
- Rewriting `read_rollout_tail` or `analyze_rollout`, and moving `TranscriptTail` out of the Claude package.
- The `item_completed` `Plan` item (plan mode), hook or `notify` ingestion, and sub-agent threads' plans (only the
  listed main thread per process).
- `docs/PLAN-final-plan.md` and decision 0041 stay unedited. The existing Codex fixtures and `state_5.rows.json` are
  not modified.
- Running the daemon, `fylgja install`, or starting Codex to produce a 0.154 rollout.

## Expected end state

```text
src/fylgja/collectors/codex/
  parse.py      + PLAN_SOURCE, PLAN_TOOL, PLAN_TEXT_CHARS, PLAN_STATUSES, PlanStep, plan_steps, plan_call,
                  PlanScan, plan_snapshot                                        (≈ 720 LoC)
  adapter.py    + RolloutPlan (TranscriptTail + PlanScan)                        (≈ 420 LoC)
  collector.py  + _plans/_read_plans/_plan_lists, _plan(), _emit_plans(), stats (≈ 470 LoC)
  plugin.py     capabilities {SESSIONS, TODOS}
tests/fixtures/codex/rollouts/plan.jsonl          (synthetic, sanitized)
tests/unit/test_codex_parse.py                    + # Plans (8 tests)
tests/unit/test_codex_collector.py                + # Plans (12 tests), capability test updated
tests/unit/test_agent_task_import.py              + Codex end to end
tests/live/test_codex_plan_live.py                (live, read-only)
docs/decisions/0047-codex-plan-import.md
docs/revalidation/2026-10-08-codex-plan.md
docs/collectors/codex.md                          capabilities, reads table, Plan paragraph
```

One poll with a live session whose rollout holds the fixture's plan emits, after the session and activity
snapshots:

```python
TodoSnapshot(
    source="codex",
    entity_id="codex-plan:codex:<thread id>",
    complete=True,
    items=(
        SnapshotItem("step-0", TodoPayload(text="Read the parser", status="done",
                     scope_kind="session", scope_ref="codex:<thread id>", link="codex:<thread id>")),
        SnapshotItem("step-1", TodoPayload(text="Write the tests", status="next", ...)),
        SnapshotItem("step-2", TodoPayload(text="Update the docs", status="open", ...)),
    ),
)
```

On the page the open and next steps appear under the Codex session as "from codex", read-only. When the process
ends, the next poll emits the same `entity_id` with `items=()` and the rows go `source_absent`.
