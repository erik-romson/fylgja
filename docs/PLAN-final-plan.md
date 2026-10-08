# Fylgja — implementation plan

Written 2026-10-05. It implements the design baseline in [`PLAN.md`](PLAN.md), which is also dated 2026-10-05.
Revised three times the same day, after Erik answered three rounds of questions (Q1–Q12). §10 lists each answer
and where it is applied. No questions remain open. Revised a fourth time after the plan review
([`PLAN-plan-review.md`](PLAN-plan-review.md), F1–F9); the response to each finding is in
[`PLAN-plan-review-response.md`](PLAN-plan-review-response.md).

`PLAN.md` remains the record of product and architecture decisions. This file turns it into work packages that can
be built and checked. Each work package names the research to read, a revalidation task wherever it depends on
something outside Fylgja (another tool, file format, repository or OS facility), what to build, the tests, and a
measurable done condition. Where this plan fills a gap or settles a conflict in `PLAN.md`, it says so
explicitly as an amendment (§2). The questions that needed Erik's input, and his answers, are in
[`PLAN-plan-questions.md`](PLAN-plan-questions.md). All of them are answered and applied (§10).

---

## 0. Working rules

- **Authority order** (from `PLAN.md`): explicit decisions and acceptance criteria in `PLAN.md`, then the amendments
  in §2 of this plan, then current official documentation, then freshly reproduced local behavior, then dated
  research. Revalidation that contradicts the plan produces a decision record, not a silent workaround.
- **Decision log.** `docs/decisions/NNNN-<slug>.md` holds the status, context, decision, consequences, the
  amendment ID and the `PLAN.md` section it amends. WP0.2 seeds it with every amendment in §2 (A1–A24), and a test
  keeps the two in step.
- **Revalidation log.** `docs/revalidation/YYYY-MM-DD-<topic>.md` is one table per run, with the columns *claim
  (with research link) · command run · observed · verdict (holds / changed / unknown) · action*. Each work package's
  "Revalidate" item produces one of these before its first production commit. Anything that changed and becomes a
  regression requirement is sanitized into `tests/fixtures/`. A work package that touches only Fylgja's own code
  has no Revalidate item; every package that reads an external tool, file format, OS facility or another
  repository has one.
- **Done conditions.** Every work package ends in a "Done when" that can be checked independently of later work
  packages. Its PR merges only when that condition is met (or, for observation gates, when the observation has
  started and the tooling for it is merged).
- **Fixture hygiene.** Fixtures are sanitized captures. Production tests never read the live home directory. The
  test `tests/unit/test_fixture_hygiene.py` scans `tests/fixtures/` and fails on the following patterns:
  - `/Users/erik`
  - the email domains in use
  - the customer names in a gitignored local deny-list
  - socket key files (`*.key`)
  - anything shaped like `CLAUDE_CODE_MESSAGING_TOKEN` or `CMUX_SOCKET_CAPABILITY`
- **One PR per work package** into `develop`. A PR cannot merge until the CI commands in `PLAN.md` §10.5 pass.
  Work packages marked "Lidskjalv repo" or "norn repo" are PRs in those repositories, each run against that
  repository's own test suite. Changes to `~/dotfiles` are made by Fylgja's installer (A22), not by hand-edited PRs.
- **Sizes** are rough guides: S is a day or less, M is 2–3 days, L is about a week.

---

## 1. Revalidation snapshot (done while writing this plan, 2026-10-05)

These checks were rerun today against the research. Each row ends in an amendment (§2), a work package, or a
question.

| # | Research / plan claim | Observed 2026-10-05 | Consequence |
|---|---|---|---|
| R1 | Lidskjalv lives at `~/projects/cc-tracking` (PLAN §2, research 01) | Moved to `~/projects/erik-romson/lidskjalv/main`, branch `first-draft`, last commit 2026-10-05 11:01. `~/.claude/lidskjalv.sh` points to `~/dotfiles/claude/.claude/lidskjalv.sh`, which is a symlink to `/Users/erik/projects/cc-tracking/bin/lidskjalv.sh`. **That target no longer exists.** All 14 Lidskjalv hook registrations currently fail, and `~/.cache/lidskjalv/sessions` was last written at 10:50. | WP0.1; doctor check (WP1.10); Phase 2 entry gate. **Q1 answered:** Erik repairs the link before Phase 2, and `~/projects/erik-romson/lidskjalv/main` is the canonical checkout. The link was still dangling when rechecked later the same day. |
| R2 | ratatosk at `~/projects/ratatosk` has four fixtures | They are now under `~/projects/erik-romson/ratatosk/main/tests/fixtures/` (probe, interrupt, stopfailure, clear-model). **They are untracked in git** and were captured from Claude 2.1.178. The hook NDJSON files hold key summaries, not full payloads. | WP0.1 preserves them; WP1.5 sanitizes them; fresh hook payload captures come from WP2.1 |
| R3 | norn keeps run history under "norn's data dir" (PLAN §4.6) | Repo is `~/projects/erik.romson/norn/main`. **There is no global data dir.** Files sit next to a state key in the run's cwd: `<pipeline>.checkpoint` (JSON), `<pipeline>.history` (JSONL, last line per run wins) and `<pipeline>.runs/<n>/{run.json, events.jsonl, stages/}`. Nothing marks a run as live: no pid, lock or heartbeat, and the formats are unversioned. norn drives Claude through `claude-agent-sdk`, and those sessions appear in the Claude registry with `entrypoint: "sdk-py"`. Their session ids are recorded in `events.jsonl` TurnEvents and `stage_log[].session_id`. | Amendment A10; WP3.3. **Q6 answered:** a norn plugin that pushes run events to Fylgja is in scope |
| R4 | 12 of 13 Claude sessions run in JetBrains terminals; join rules cover iTerm2 and JetBrains (PLAN §3) | There are 11 live registry rows: **7 under cmux** (`com.cmuxterm.app` 0.64.25), 3 under PyCharm, 1 norn-spawned under IntelliJ, and 0 under iTerm2. cmux exports `CMUX_WORKSPACE_ID`, `CMUX_SURFACE_ID`, `CMUX_TAB_ID` and `CMUX_PANEL_ID`, and bundles a `cmux` CLI under `/Applications/cmux.app/Contents/Resources/bin/`. It also starts Claude with its own `--settings` temp file. | Amendment A1; WP0.4, WP1.8, WP3.5, WP5.3. **Q4 answered:** cmux is the focus |
| R5 | Registry fields `pid, sessionId, cwd, …, status (idle\|busy)` | New fields: `nameSince`, `nameSource`, `peerProtocol` (=1), `pidDomain`, `updatedAt`. **`status` now also takes `waiting`.** `bridgeSessionId` is absent unless Remote Control is on. Per-session `version` (2.1.284 or 2.1.289) can differ from the installed CLI (2.1.289). Each row has an undocumented `<pid>.<hash>.key` file (mode 0600) beside it. | Amendments A9, A13; WP1.7 |
| R6 | Sockets live under "Claude's private socket directory" | The sockets are in `/tmp/cc-socks` (mode 0700, owner erik). The docs name `/tmp/cc-socks-<uid>` as the fallback when the default directory is unacceptable. | Amendment A14 |
| R7 | `~/.claude/tasks/<sessionId>/*.json` holds the session task list | **`~/.claude/tasks/` does not exist.** New directories `daemon/`, `jobs/` and `state/` exist under `~/.claude`. | WP1.7 revalidation, with a fallback to transcript task records |
| R8 | Hooks call `~/.claude/lidskjalv.sh` "on SessionStart, … and more" | 14 sync registrations, all with `timeout: 2`: SessionStart, PostToolUse, UserPromptSubmit, PermissionRequest, Notification (`permission_prompt`, `idle_prompt`), PreCompact, PostCompact, Stop, StopFailure, SessionEnd (`clear` and the other reasons), TaskCreated and TaskCompleted. None is registered for PreToolUse or CwdChanged. `~/.claude/settings.json` and `statusline.sh` are both symlinks into `~/dotfiles` (a git repo). `statusline.sh` is not Lidskjalv's; it renders showy-quota. | Amendments A2, A4, A22; WP2.4. **Q5 answered:** changes must be persisted in `~/dotfiles`, whose `dotfiles.conf` lists each linked file as `package:path` |
| R9 | Async hooks: timeout not enforced; killed when `claude -p` exits (research 03) | Current docs: the timeout is ignored, output is discarded, and async hooks *continue* after the session exits. `asyncRewake` exists. New Notification types: `agent_needs_input`, `agent_completed`, `elicitation_dialog`, among others. | The design is robust either way; WP2.3 uses the new types; `asyncRewake` is never used |
| R10 | Unauthenticated socket messages are subject to `crossSessionInbound` | The docs add a default rule for when no `crossSessionInbound` value applies: **a receiver that bypasses permissions holds every message from a sender that does not identify as bypassing.** It shows an approval dialog that expires after `dialogExpiry` (default 5 min). Other limits: a complete line must arrive within 30 s; at most 50 queued and 100 held messages. The frame schema is still undocumented. Erik's user settings set no `crossSessionInbound`, and his interactive sessions run with `--dangerously-skip-permissions`. | WP3.6. **Q7 answered:** build it as recommended |
| R11 | Tooling | uv is **0.7.19** (2025-07); its `uv init` has no uv build-backend option, while brew stable is 0.12.23. Python 3.13.5. **macOS 27.0.1**, whereas research 04 was verified on Darwin 25.6. AeroSpace 0.21.3-Beta, Codex 0.154.0, OpenCode 1.18.26 and gh 2.92.0 are unchanged or current. | WP0.1; TCC revalidation in WP5.x |
| R12 | Repository state | `erik-romson/fylgja` is **private** and its default branch is `develop`. `PLAN.md` and `research/` are untracked. Leftover norn run files (`plan_with_review.*`) sit in the repo root. | WP0.2 |
| R13 | Obsidian PARA notes "whose frontmatter names a project path" (PLAN §8) | The `create-para-project` skill writes `1 - Projects/{repo} - {task}/` containing `Master Note - …` and `Dev Log - …`. The dev log records `Working directory:`. **No frontmatter key names a project path.** | **Q8 answered:** Obsidian import is on hold and listed under the Phase 4 todo work (A23) |
| R14 | Lidskjalv "can read the API behind a switch" | Lidskjalv has **no backend switch** and makes no HTTP calls. The read sites are enumerated in WP2.6. The Swift panel reads only stdin from `lidskjalv.sh panel --json`, so changing `_panel_json` covers the panel. BATS: 13 files, 722 tests. | WP1.11, WP2.6, WP2.8, WP5.5, WP8.3. **Q3 answered:** Fylgja eventually replaces builds, costs, quota and stories, but both must work for now (A24) |
| R15 | Lidskjalv's `UserPromptSubmit` inject path is a todo fast-entry route (PLAN §8) | That path prints reminders to stdout from a *sync* hook. A silent async Fylgja hook cannot inject anything. | Amendment A18 |

I could not fetch the Blake Crosley protocol survey in this session. WP3.6 rechecks it before relying on it.

---

## 2. Amendments: decisions this plan adds to `PLAN.md`

Each amendment becomes a decision record in WP0.2. A1–A20 fill gaps, settle internal conflicts, or apply the
revalidation above; none of them changes a product decision in `PLAN.md`. A21–A24, and the cmux priority in A1 and
the norn plugin in A10, record Erik's answers to the questions (Q1–Q12). They are product decisions, and
their decision records cite the question they answer.

| ID | Amendment | Why | Amends |
|---|---|---|---|
| A1 | **Host-generic session-to-window join, with cmux as the primary host** (Q4). Walk the session pid's parent chain until a pid matches an AeroSpace window's `app-pid`; that gives the host app. Then a host adapter picks the window and, where possible, the tab. **cmux** (first priority, joined down to the exact cmux workspace and surface): use the method the WP0.4 spike chooses. That is the cmux CLI if it can list surfaces with their tty, pid or cwd, and otherwise the allowlisted `CMUX_*` ids captured at hook time (A3); window level is only the fallback. **JetBrains**: the project path contains the cwd; window level. **iTerm2**: kept as specified (title equals the ai-title, or `id of window`), but best effort and not part of any done criterion. Every join records `via` and `confidence`. Fylgja's model and UI always say *cmux workspace* or *tab*, so these are never confused with AeroSpace workspaces. | R4: today most sessions run in cmux, and the iTerm2/JetBrains rules alone would join 3 of 11. | §3 join rule 2 |
| A2 | **The Fylgja hook event set is a superset of Lidskjalv's.** It covers SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, PostToolUseFailure, PermissionRequest, PermissionDenied, Notification (matcher `""`, type read from the payload), Stop, StopFailure, SubagentStart, SubagentStop, TaskCreated, TaskCompleted, PreCompact, PostCompact, PostModelSwitch, CwdChanged and SessionEnd (matcher `""`, reason read from the payload). High-rate events such as `MessageDisplay` and `FileChanged` are excluded. The set is configurable, and the installer registers only events the installed Claude version supports. | Phase 2 requires that "both systems receive equivalent events" (R8). | §4.3 event list |
| A3 | **Allowlist of environment variables in the hook envelope.** Beyond the session id, pid and `CLAUDE_CODE_MESSAGING_SOCKET`, the envelope may copy these terminal identifiers: `CMUX_WORKSPACE_ID`, `CMUX_SURFACE_ID`, `CMUX_TAB_ID`, `CMUX_PANEL_ID`, `ITERM_SESSION_ID`, `TERM_SESSION_ID`, `TERM_PROGRAM`, `CLAUDE_CODE_BRIDGE_SESSION_ID`. Nothing else is copied. Values are length-capped. | This gives exact host joins without scraping other processes' environments (`ps eww` exposes secrets such as `CMUX_SOCKET_CAPABILITY`). | §4.3 envelope |
| A4 | **The statusline is a coalesced latest snapshot, not an event stream.** `fylgja hook StatusLine` atomically replaces `statusline/<session_id>.json`, and the daemon reads it when its mtime changes. A one-line snippet in `statusline.sh` runs it in the background with output discarded, at most once per 2 s per session. | Claude allows only one statusLine command, and it renders often. Appending would flood the inbox. | §4.3 statusline forwarder |
| A5 | **Hook payloads are minimized before persistence.** The spool keeps the raw (size-capped) event only until commit. SQLite stores normalized, allowlisted fields: session id, event, tool name, notification type, reason, cwd, permission mode, edited file paths, and the prompt truncated to 300 characters. Tool input and response bodies are dropped. | Privacy (§11). Derived state does not need those bodies. | §5, §11 |
| A6 | **Every endpoint checks the Host header.** Only `127.0.0.1:<port>` and `localhost:<port>` are accepted, and no CORS headers are ever sent. Mutating endpoints also require `Authorization: Bearer`, an allowed Origin (or no Origin together with a valid bearer, for the CLI) and `Content-Type: application/json`. | A loopback bind does not stop DNS rebinding from reading "unauthenticated" read endpoints, and window titles contain customer names. | §6 |
| A7 | **The CLI is an HTTP client of the daemon** (stdlib `urllib`, no new runtime dependency). The exceptions are `hook`, `serve`, `doctor` and `install`. The daemon is the only SQLite writer. Read commands exit with code 3 and a clear message when the daemon is down. | Keeps a single writer and avoids running collectors in two processes. | §6, §10.1 "sharing application services" |
| A8 | **CLI and API additions.** CLI: `install config\|hooks\|statusline\|skill\|keybinding\|launchd\|secret [--apply]` (the first five write into `~/dotfiles` per A22), `history [--since] [--story]`, `next --feedback good\|bad`, `story session <key>\|ws <n>` and `todo` scoping flags. `--json` on every read command. API (§4.2): `POST /next/feedback`, and `GET`/`POST /summaries/{scope}/{ref}` for session, workspace and day summaries. Every one of these CLI commands goes through the daemon (A7), and model calls run only inside the daemon. | `PLAN.md` requires doctor to verify registrations, but nothing installs them. Phases 5, 6 and 8 need these commands, and the endpoints behind them, to meet their done criteria. | §6 CLI and API lists |
| A9 | **Messaging is gated per target session**, using that session's registry `version` (≥ 2.1.224) and `peerProtocol`, not the installed CLI version. An unknown `peerProtocol` disables sending to that target. | R5: running sessions use different versions. | §4.3.1 |
| A10 | **norn pushes; Fylgja scans as the fallback** (Q6). A norn-side plugin (WP3.3, in the norn repo) uses the hook envelope format to write into `hook-inbox/norn/`. It writes run lifecycle events, stage changes, `WaitingInput`, agent session ids, and a heartbeat with pid every 30 s. A run with a heartbeat no older than 90 s and a live pid is `running`. The scan of configured roots and known project cwds (depth-limited) for `*.history` and `*.runs/` stays for backfill and for runs started without the plugin. For those runs, liveness falls back to matching process argv and cwd against `run.json`, and an unfinished run with no match is `stale`. `sdk-py` Claude sessions whose ids appear in a run are nested under that run instead of being listed as independent sessions. | R3. The push gives exact liveness, the live stage, and norn's waiting-for-input signal for "waiting on me". | §4.6 |
| A11 | **Session continuity record.** Each session carries a `process_key` (pid + process start time) and a `predecessor` link, set when `/clear` rotates the session id. `/now` exposes `lane_key` = `process_key` for Lidskjalv. | Lidskjalv's lanes survive `/clear`; Fylgja's model has to as well. | §3 Session |
| A12 | **Rebuildability is scoped to the raw-event retention window** (30 days). Derived rows older than that (spans, snapshots) are retained archives and are not rebuildable. | §5 says both "everything derived is rebuildable" and "raw events 30 days". | §5 |
| A13 | **Strict registry reads.** Glob exactly `^[0-9]+\.json$` in `~/.claude/sessions/`, never open `*.key`, and parse unknown fields tolerantly. | R5, plus the rule in §4.3.1 that undocumented key files are never read. | §4.3 |
| A14 | **Socket path allowlist.** A target socket must be in `/tmp/cc-socks` or `/tmp/cc-socks-<uid>`. The parent directory must be owned by the uid with mode 0700, neither path may be a symlink (checked with `lstat`), and the path must equal the registry row's path. | R6 | §4.3.1 |
| A15 | **Unread chat suggestions come only from explicit title markers** (for example `(3)` or `*` prefixes), confirmed per app during revalidation. Where no marker exists, nothing is inferred. | §7 ranking item 6 versus §13.1 "no claim of an authoritative unread count". | §7 |
| A16 | **Concrete paths and identities.** These defaults can be overridden with `FYLGJA_CONFIG_DIR`, `FYLGJA_DATA_DIR` and `FYLGJA_STATE_DIR`; tests always override them. | `PLAN.md` leaves these unspecified. | §5, §10 |
| A17 | **A separate `presence` collector** handles idle (`ioreg`), screen lock and mic/camera in use. Lock and in-use flags are read through stdlib `ctypes` calls to CoreGraphics, CoreAudio and CoreMediaIO, polled every 5–10 s. There is no Swift helper. | The "one job per collector" rule, and §10 keeps Swift out of the daemon. | §4.2 idle placement, §4.7 call flags |
| A18 | **Fylgja never injects into model context.** Todo fast entry from Claude uses a user-level slash command that runs `fylgja todo add --session …`. Lidskjalv's reminder injection stays in Lidskjalv's own synchronous `UserPromptSubmit` hook, independent of Fylgja (Q9, A21). Fylgja neither depends on that hook nor takes it over, so removing Lidskjalv also removes reminder injection (Q12). Fylgja adds no synchronous hook; its session-scoped todos, shown in `/now`, the CLI and the page, are the replacement. Because Lidskjalv will eventually use Fylgja (Q12), it may later store its reminders as Fylgja session todos while its own hook keeps doing the injection (A24). | R15: the Fylgja hook is async and silent by design. | §8 fast entry |
| A19 | **Phase 4 ships a minimal page at `/`** with a todo list, add and done. Phase 7 grows it into the navigator. | Phase 4's done criterion requires "terminal and page", but the navigator page arrives only in Phase 7. | §12 |
| A20 | **Windows and session snapshots are stored as change intervals** (`valid_from`/`valid_to`), not full copies per tick. | 40 windows × every 30 s comes to about 115k rows a day for no added information. | §5 table semantics |
| A21 | **Fylgja and Lidskjalv stay independent permanently** (Q9, which refines Q2). Fylgja never depends on Lidskjalv, at runtime or in its tests: not on its hook, its cache, its repository or its installation. Removing Lidskjalv (its hook entries, its `dotfiles.conf` line, the SketchyBar plugins, the panel and the cache) leaves Fylgja fully working. Fylgja's own direct async hook registrations are therefore permanent, not just for the dual run. Lidskjalv's hook never forwards events to Fylgja, because Fylgja's own hooks already receive every event and forwarding would duplicate them. "Thin" (Q2) therefore applies only to Lidskjalv's side. After the dual-run gate (WP2.7), Lidskjalv reads its session data from Fylgja by default (WP2.8). Lidskjalv's hook shrinks to what Lidskjalv itself still needs, such as reminder injection (A18), once Lidskjalv retires its own session source. That retirement is Lidskjalv's decision and is not scheduled here. Erik confirmed this reading of "thin forwarder" (Q11). | Erik wants to be able to remove Lidskjalv without leaving any dependency behind. The round-1 version of A21, which removed Fylgja's hooks and made Fylgja rely on Lidskjalv's forwarder, contradicts that and is withdrawn. | §2, §4.3 |
| A22 | **All user configuration Fylgja installs is persisted in `~/dotfiles`** (Q5), following its `dotfiles.conf` convention: each entry is `package:path`, the file lives in `~/dotfiles/<package>/<path>`, and it is symlinked into `$HOME`. This covers:<br>• hook entries in `claude/.claude/settings.json`<br>• the statusline block in `claude/.claude/statusline.sh`<br>• the todo skill in `claude/.claude/skills/fylgja-todo/`<br>• the AeroSpace keybinding in `aerospace/.aerospace.toml`<br>• Fylgja's own `config.toml`, as the new file entry `fylgja:.config/fylgja/config.toml`. It is linked as a file, not a directory, so the API secret beside it never enters dotfiles.<br>The installer refuses to write a regular file where dotfiles expects a link. Installed text blocks are delimited (`# >>> fylgja >>>` … `# <<< fylgja <<<`), and JSON hook entries are recognized by their `fylgja hook` command string, so reruns are idempotent. Dotfiles may be shared with machines that have no Fylgja, so every registered command is guarded to exit 0 silently when `~/.local/bin/fylgja` is missing. Backups go to `~/.local/state/fylgja/backups/`, never into the dotfiles tree. The installer never commits (Q10). After `--apply` it prints the diff and a suggested `git -C ~/dotfiles commit` command limited to the files it touched, and doctor warns while Fylgja's changes are uncommitted. The launchd plist and the API secret are machine-specific and stay out of dotfiles. | Erik's dotfiles repo is the source of truth for this configuration. | §4.3, §6, §8 |
| A23 | **Obsidian todo import is on hold** (Q8). It leaves Phase 4 and is kept as an on-hold item in the Phase 4 todo work, together with the unresolved mapping from a PARA note to a repository path. The `TODO.md` and agent task-list imports go ahead. Obsidian window-title context (WP5.3) is not affected. | Erik's answer. | §8 import sources |
| A24 | **Lidskjalv keeps two data sources per module for now** (Q3). Lidskjalv's builds, costs, quota and ARC/NOW stories keep running on their own code paths. As Fylgja gains equivalents (builds, costs and quota from WP5.4, stories from WP8.1), Lidskjalv gets one switch per module, `LIDSKJALV_SOURCE_<MODULE>=lidskjalv\|fylgja`, defaulting to `lidskjalv`. A switch flips only after a parity check. Session data follows the same pattern (WP2.6). Its default flips to `fylgja` after the dual-run gate (WP2.8), and the `lidskjalv` session source keeps working until Lidskjalv retires it. Reminders may follow the same pattern later (Q12): Lidskjalv would store them as Fylgja session-scoped todos through the todo API and keep injecting them from its own synchronous hook. That is not scheduled here. If it happens, the fields it needs (an inject flag and a delivered timestamp) are designed at that point as a Fylgja todo-API change, without adding any Fylgja hook. Retiring Lidskjalv's own implementations is a later decision and is not scheduled here. | Fylgja will eventually replace these parts of Lidskjalv, but both must work for the time being. | §2, §9 |

The A16 defaults:

| What | Default |
|---|---|
| Configuration | `~/.config/fylgja/config.toml` |
| API secret | `~/.config/fylgja/api-secret` (mode 0600) |
| Database | `~/.local/share/fylgja/fylgja.db` |
| Hook inbox | `~/.local/share/fylgja/hook-inbox/<source>/`, with `quarantine/` |
| Statusline snapshots | `~/.local/share/fylgja/statusline/` |
| Log | `~/.local/state/fylgja/fylgja.log` |
| Summarizer cwd | `~/.local/state/fylgja/summarizer/` |
| API port | `127.0.0.1:7319` |
| launchd label | `io.github.erik-romson.fylgja` |

---

## 3. Architecture

### 3.1 Process model

```text
Claude Code ──async hook──▶ fylgja hook (stdlib-only) ──atomic file──▶ hook-inbox/claude/
statusline.sh ─background─▶ fylgja hook StatusLine ──atomic replace──▶ statusline/<sid>.json
  (Lidskjalv's own hooks run beside these and write only Lidskjalv's cache; Fylgja never reads them, A21)
norn run ─norn.event_subscribers plugin (A10)─▶ atomic file ──▶ hook-inbox/norn/

launchd ─▶ fylgja serve (one asyncio process)
             ├─ PluginRegistry (entry points) ─▶ Supervisor ─▶ collector tasks
             │     aerospace · presence · claude · codex · opencode · norn · appcontext · project
             │       └─ emit(Event|Snapshot) ─▶ CollectorContext.sink
             ├─ Ingest gate: validate → privacy filter → dedup ─▶ Writer task (single SQLite writer, batched)
             ├─ State engine: resolver (joins) · session state machine · spans · labels · ranking
             ├─ Live view cache (/now model, rebuilt on change, published on SSE)
             └─ FastAPI/Uvicorn on 127.0.0.1:7319 (read API, SSE, guarded actions, page at /)

fylgja <cmd> (Typer) ─HTTP─▶ daemon      fylgja doctor ─▶ local checks + daemon /collectors
Lidskjalv (bash/jq, Swift panel) ─HTTP─▶ /now
```

### 3.2 Package layout (extends `PLAN.md` §10.2)

```text
src/fylgja/
  __init__.py            # intentionally empty: importing it must stay cheap for the hook path
  _entry.py              # console script: dispatches "hook" before importing Typer/FastAPI/Pydantic
  hook.py                # stdlib-only ingress (WP2.1)
  py.typed
  paths.py               # stdlib-only path resolution shared by hook and daemon
  config.py  models.py  privacy.py
  api/      app.py security.py routes/*.py sse.py static/index.html
  cli/      app.py client.py render.py install.py doctor.py
  core/     daemon.py ingest.py writer.py resolver.py sessions.py spans.py labels.py
            one_liner.py ranking.py summaries.py focus.py retention.py live_view.py
  collectors/ base.py registry.py supervisor.py context.py
            aerospace/ presence/ claude/ codex/ opencode/ norn/ appcontext/ project/
            (each: adapter.py = I/O, parse.py = pure functions, plugin.py = descriptor + factory)
  messaging/claude/ resolve.py transport.py encoders/v1.py receipts.py
  store/    db.py migrations/0001_init.sql … queries.py
  testing/  contract.py fakes.py fixtures.py   # public, typed, ships in the wheel
tests/ unit/ contract/ integration/ live/ fixtures/ fake_plugins/fylgja_fake/
docs/ decisions/ revalidation/ spikes/ api/
spikes/  (not packaged)
```

### 3.3 Collector API v1 (`fylgja.collectors.base`)

```python
COLLECTOR_API_MAJOR = 1
class Capability(StrEnum): WINDOWS, SESSIONS, TODOS, CONTEXT, PROJECT_STATE
@dataclass(frozen=True)
class CollectorPlugin:
    name: str                       # unique, [a-z0-9_-]+
    api_major: int
    capabilities: frozenset[Capability]
    config_model: type[BaseModel]   # namespaced under [collectors.<name>]
    factory: Callable[[BaseModel, CollectorContext], Collector]
class Collector(Protocol):
    async def run(self) -> None: ...        # lives until cancelled
    async def doctor(self) -> HealthReport  # side-effect free
@dataclass(frozen=True)
class CollectorContext:
    sink: RecordSink     # emit(record) -> None ; emit_committed(record) -> awaits durable commit
    clock: Clock
    logger: LoggerAdapter
    paths: ReadOnlyPaths # data/state dirs the plugin may read (inbox subdir for its source)
```

- **Record models** are frozen Pydantic models with a discriminated union on `entity_kind`. Every record carries
  `source`, `entity_kind`, `entity_id`, `observed_at` and `dedup_key`. A `Snapshot` carries `items` and `complete:
  bool`. A complete snapshot lets the core close entities that are missing from it, such as closed windows or ended
  sessions.
- **The ingest gate** rejects a record when:
  - `source` does not equal the plugin name;
  - its `entity_kind` is not allowed by the plugin's declared capabilities;
  - its payload fails the schema;
  - its timestamp is outside the window [now − 30 d, now + 5 min].

  Rejections are counted. More than 20 in a minute marks the plugin degraded.
- **`emit_committed`** is how a plugin implements delete-after-commit (hook inbox) without writing SQLite itself.

---

## 4. Cross-cutting specifications

### 4.1 Store (migration `0001_init.sql`, `PRAGMA user_version`)

| Table | Key columns |
|---|---|
| `events` | id, `dedup_key UNIQUE`, source, entity_kind, entity_id, observed_at, received_at, payload JSON (minimized) |
| `windows_snapshot` | window_id, workspace, app_bundle_id, app_name, app_pid, title (redacted), valid_from, valid_to |
| `focus_spans` | window_id, workspace, app_bundle_id, title, project_path, session_key, start, end, end_reason (focus/idle/lock/shutdown) |
| `sessions` | session_key (`kind:native_id`), kind, native_id, pid, process_key, cwd, project_path, host_kind, host_ref, window_id, workspace, name, title, model, branch, state, state_since, waiting_reason, parent_key, predecessor_key, remote_url, first_seen, last_seen, ended_at |
| `session_snapshots` | session_key, valid_from, valid_to, state, last_tool, last_prompt (≤300), last_reply (≤800), one_liner (≤160, §4.6), context_pct, cost_usd, tokens_input, tokens_output, tokens_cache_read, tokens_cache_write (§4.7) |
| `projects` | path, repo_root, remote_url, repo_name, group (draupnir workspace), branch, dirty, ahead, behind, pr_number, pr_state, review_decision, build_state, build (JSON, §4.7), build_observed_at, build_error_kind, last_commit_at, updated_at |
| `usage_snapshots` | provider, window, used_pct, resets_at, cost_usd, cost_period, source (statusline/codexbar), valid_from, valid_to (§4.7) |
| `workspaces` | name, manual_label, manual_label_at, inferred_label, inferred_at |
| `todos` | id (uuid), scope_kind, scope_ref, text, status (open/next/done), created_at, done_at, source, source_ref, link, updated_at, deleted_at, deleted_reason (user/source_absent); `UNIQUE(source, source_ref)` for imported rows |
| `suggestions_log` | id, list_id, generated_at, rank, kind, target_ref, score, reason, feedback, feedback_at, acted_at |
| `message_receipts` | message_id, session_key, created_at, state, state_at, bytes, error_kind (no text body is stored) |
| `summaries` | scope_kind (session/workspace/day), scope_ref, kind (arc/now/text), text, input_digest, model, generated_at, error_kind; `UNIQUE(scope_kind, scope_ref, kind)` |
| `collector_health` | name, state, since, last_ok_at, restarts, last_error_kind |

Settings: WAL, `synchronous=NORMAL`, `foreign_keys=ON` and `busy_timeout=2000`. There is a single writer task. It
writes batches of up to 500 records or 200 ms per transaction, through `asyncio.to_thread`. Readers use separate
read-only connections. A nightly maintenance job applies retention (`PLAN.md` §5: raw events 30 d, spans and
session and usage snapshots 1 y, todos forever), clears quarantine older than 7 d, runs `PRAGMA optimize`, and runs
`wal_checkpoint(TRUNCATE)` when idle.

### 4.2 API (all served on `127.0.0.1:7319`, Host-checked per A6)

- **Read, no auth:** `GET /now`, `/workspaces`, `/workspaces/{name}`, `/sessions`, `/sessions/{key}`, `/projects`,
  `/todos`, `/next`, `/history?since=&until=`, `/summaries/{scope}/{ref}`, `/collectors`, `/health`, SSE
  `/events`, and the page at `/`.
- **Mutating** (bearer + Origin + JSON content type + size limit):
  - `POST /focus/window/{id}`
  - `POST /focus/session/{key}`
  - `POST /sessions/{key}/message` (disabled by default)
  - `POST /todos`, `PATCH /todos/{id}`, `DELETE /todos/{id}`
  - `PUT /workspaces/{name}/label` and `DELETE /workspaces/{name}/label`
  - `POST /next/feedback`, body `{list_id, rating: "good"|"bad", suggestion_id?}`. It records the rating on that
    list's top item (or on `suggestion_id`) in `suggestions_log`. Every `/next` response carries its `list_id` and
    a `suggestion_id` per item. `fylgja next --feedback` rates the most recent list the CLI showed today.
  - `POST /summaries/{scope}/{ref}`, where `scope` is `session` (ref = session key), `workspace` (ref = name) or
    `day` (ref = `YYYY-MM-DD`). It requests generation and returns the same model as the `GET` (WP8.1).
- **Summary responses** (`GET` and `POST /summaries/{scope}/{ref}`): `{scope, ref, state, arc?, now?, text?,
  generated_at?, input_digest?, error_kind?}`, where `state` is `fresh`, `stale` (cached, inputs changed, inside
  the TTL), `pending` (a job is queued or running), `missing`, `failed` or `disabled`. The `POST` returns 200 with
  the cached result when it is fresh, otherwise 202 with `pending`, and 409 with `disabled` while
  `[summaries] enabled = false`. Concurrent requests for the same scope and ref share one job. A finished job
  publishes `summary.ready` on SSE.
- **`/now`** carries `schema: "fylgja.now/1"`. The JSON Schema is generated from the Pydantic models into
  `docs/api/now.schema.json` and checked in CI with a "schema unchanged unless version bumped" test. Top-level keys:
  `generated_at`, `focused`, `workspaces[]` (with `one_liner`, §4.6, and a cached `story`, WP8.1),
  `sessions[]` (with a Lidskjalv-compatible `facts` block, `lane_key`, `one_liner`, `tokens` and a cached
  `story`), `projects[]` (with `build`, §4.7), `usage` (§4.7), `waiting[]`, `next[]`, `collectors[]` and
  `degraded`. Every section and field that a later work package fills (`one_liner` for non-Claude kinds, `tokens`,
  `build`, `usage`, `story`) is part of the `fylgja.now/1` models from WP1.9 onwards as an optional, absent
  value, so filling it later does not change the schema. A collector that is disabled or unavailable shows up as
  an *absent* section, never as an error that breaks `/now` (`PLAN.md` §13).
- **Secret.** `fylgja install secret` creates 32 random bytes encoded url-safe, mode 0600. The page at `/` receives
  the secret in a `<meta>` tag; this is safe because of the Host check and the absence of CORS. `--rotate`
  replaces the secret.
- **Performance budgets** (measured in CI with fixtures and live in the opt-in smoke tests):
  - `/now` p95 ≤ 50 ms with 15 sessions and 60 windows;
  - daemon idle CPU ≤ 1 % average;
  - RSS ≤ 150 MB;
  - `fylgja hook` wall time p95 ≤ 60 ms and peak RSS ≤ 25 MB.

### 4.3 Configuration

The configuration is TOML parsed with `tomllib` and validated by Pydantic. Packaged `defaults.toml` sets the
built-in collector defaults. User `config.toml` overrides them. External plugins are imported and started only when
they have an explicit `[collectors.<name>] enabled = true` (`PLAN.md` §4.1). Until then Fylgja knows them only by
their entry-point metadata and never loads their code (WP1.2). Other sections:

- `[api]`: port and allowed origins
- `[privacy]`: `redact_patterns`, `ignore_bundle_ids`, `ignore_title_patterns`
- `[actions.message]`: `enabled = false` and `max_chars = 8000`
- `[actions.applescript]`: `chrome = false`, `iterm2 = false`
- `[summaries]`: `enabled = false`, `ttl_seconds = 300` and `max_concurrent = 3`
- `[paths]`: absolute paths to `aerospace`, `gh`, `git` and `codexbar`, because launchd's PATH is minimal

### 4.4 Privacy (`PLAN.md` §11)

- **Before persistence:** the redaction list is applied to titles and prompt text, and records whose bundle id is
  on the ignore list are dropped before they reach SQLite or SSE.
- **Never read:** `~/Library/Containers`, `~/Library/Group Containers`, Claude `*.key` files, or any process
  environment other than the hook's own allowlisted variables.
- **Doctor:**
  - asserts that no configured path resolves under the FDA-protected roots;
  - runs every osascript with a 3 s timeout;
  - treats a timeout as "permission dialog likely pending": the collector is marked degraded and the switch is
    disabled until doctor is rerun.
- **Model calls:** only through the local `claude` CLI (Phase 8), run in the summarizer cwd. That cwd is on the
  default `ignore_cwd` list, so summary sessions never show up as user sessions.

### 4.5 Logging

Logging uses stdlib `logging` with a `RotatingFileHandler` (5 × 5 MB) and structured context through
`LoggerAdapter` (`collector=`, `session=`). Messages never contain prompt text, titles at INFO level, tokens or
environment dumps. A test sets a canary token and asserts that it appears in no log, spool file, database row or
API response.

### 4.6 Session and workspace one-liners (`PLAN.md` §1 question 2, §7, §12 Phase 3)

Every session of every kind has a deterministic `one_liner`: what it is doing, in one sentence, with no model
call. It is computed in the state engine by a pure function, `core/one_liner.py`, from a normalized
`SessionActivity` record that each session collector fills. It is redacted like titles, capped at 160 characters,
stored in `session_snapshots.one_liner`, and exposed in `/now` as `sessions[].one_liner` together with
`one_liner_from`, the rule that produced it.

- **`SessionActivity` fields** (all optional): `state`, `waiting_reason`, `current_tool`, `current_target` (a file
  path, command head or URL host), `open_task` (the in-progress task or todo), `stage` (`name`, `index`, `total`;
  norn only), `last_prompt`, `last_reply`, `title` and `branch`.
- **Rules**, in order; the first one that yields text wins:
  1. waiting: "Waiting for permission: `<tool>` `<target>`" or "Waiting for input: `<reason>`";
  2. a norn stage: "`<pipeline>` · `<stage>` (`<index>`/`<total>`)", or "`<pipeline>` `<terminal status>`" when the
     run has finished;
  3. working with a current tool: "`<tool>` `<target>`";
  4. working with an open task: the task text;
  5. the title, followed by ": " and the last prompt;
  6. the title alone, then the last reply's first sentence;
  7. "—".
- **Sources per kind:**
  - Claude: the `facts` block (WP1.7) and the hook state (WP2.3);
  - Codex: the thread title from `state_N.sqlite`, plus the last user message and the current function call from
    the rollout tail (WP3.1);
  - OpenCode: the session title, the in-progress `todo` row, and the tool of the incomplete assistant message
    (WP3.2);
  - norn: the pipeline, current stage, `WaitingInput` and the finish events (WP3.3).
- **Workspace one-liner** (`PLAN.md` §7): `workspaces[].one_liner` is the effective label, then " — " and the most
  relevant live context: the one-liner of a waiting session in that workspace, else that of the most recently
  active session, else the focused window's parsed title context (WP5.3), else nothing.
- **Tests:** golden fixtures for every rule and every session kind; a property test that any `SessionActivity`
  yields a non-empty string of at most 160 characters; redaction applied before the value is stored.

### 4.7 Builds, tokens and quota (`/now` contract for A24)

These are the Fylgja payloads that Lidskjalv's builds, costs and quota switches consume (WP5.5). They are part of
the `fylgja.now/1` models from WP1.9 as optional sections, filled from WP5.4. Every section carries `source`,
`observed_at` and `freshness`: `fresh` within twice the source's poll interval, `stale` after that, and
`unavailable` together with `error_kind` when the last attempt failed. A section with no data yet is absent.

| `/now` path | Shape | Source | Lidskjalv module it replaces |
|---|---|---|---|
| `projects[].build` | `{state: pass\|fail\|running\|cancelled\|none, head_sha, url, workflows[]: {workflow, state, status, conclusion, run_id, url, run_started_at, run_duration_s, typical_duration_s}}` | `gh api repos/{owner}/{repo}/actions/runs?branch=…&per_page=30`, folded by a Python port of Lidskjalv's `_BUILDS_FOLD_JQ` (newest head sha, re-runs collapsed, skipped/neutral dropped, typical duration = median of successful runs) | `lib/builds.sh` (`builds/*.json`) |
| `sessions[].tokens` | `{input, output, cache_read, cache_write}` summed over the transcript's assistant `message.usage` | the Claude transcript tail, by a Python port of Lidskjalv's `_COSTS_JQ` | `lib/costs.sh` (`costs/costs.json`, keyed by session id) |
| `sessions[].cost_usd` | number | the statusline snapshot (A4) | none today; additive |
| `usage.providers[]` | `{provider, window, used_pct, resets_at}` | Claude `rate_limits` from the statusline snapshot; other providers (Codex, Copilot) from the CodexBar CLI | `plugins/lidskjalv_quota_*.sh` (showy-quota providers `claude,copilot,codex`) |
| `usage.cost` | `{period, usd}` | `codexbar cost --json` | none today; additive |

- **Storage:** `projects.build` holds the latest fold and `projects.build_state` its aggregate state;
  `session_snapshots` holds the token sums; `usage_snapshots` holds provider and cost rows as change intervals
  (A20).
- **Budget:** the actions-runs call uses ETags and a calls-per-hour budget (default 30, as in Lidskjalv), and it is
  made only for projects with a live session or focus in the last hour.
- **Mapping:** WP5.4 extends `docs/api/lidskjalv-mapping.md` (WP1.11) with one row per field each Lidskjalv module
  reads, and the mapping test covers these paths.
- **Parity:** the build fold and the token sums are checked against goldens generated once from Lidskjalv's jq
  programs by a dev-only script in `spikes/`, as in WP1.7, so Fylgja's tests never run or need Lidskjalv (A21).
  Quota parity can only be shown per provider that both showy-quota and Fylgja's sources cover. WP5.5 revalidates
  that, and a provider Fylgja cannot supply keeps the `lidskjalv` source.

---

## 5. Phases and work packages

The phase table in `PLAN.md` §12 is kept. Done criteria are made measurable where the original wording was
qualitative.

### Phase 0: prerequisites and spikes (3–4 days)

**WP0.1 Environment prerequisites (S)**
- Research: [01](research/01-local-findings.md), [03](research/03-agent-sessions.md).
- Revalidate: record R1–R12 in `docs/revalidation/2026-10-05-baseline.md`. This is mostly a copy of §1 with the
  commands that were run.
- Do:
  - upgrade uv (`uv self update`; the install lives in `~/.local/bin`) and record the version;
  - copy the ratatosk fixtures **unchanged** into a gitignored `target/fixture-staging/ratatosk/` so they cannot be
    lost. Sanitized copies are committed in WP1.5. Recommend committing them in ratatosk as well;
  - copy Lidskjalv's test transcripts and stubs (WP2.6 list) into the same staging area.
- Outside Fylgja (Q1, confirmed): Erik repairs Lidskjalv's hook link by re-running Lidskjalv's `install.sh` from
  the canonical checkout `~/projects/erik-romson/lidskjalv/main`. Fylgja never touches that link. Phase 2 is
  blocked until `fylgja doctor` (or `test -e ~/.claude/lidskjalv.sh`) confirms the repair. All Lidskjalv work
  packages (WP1.11, WP2.6, WP2.8, WP5.5, WP8.3) target that checkout. WP1.11 renders from recorded fixtures only,
  so it does not wait for the repair.
- Done when: the revalidation log is committed, `uv --version` is ≥ the version pinned in WP0.2, and the staged
  fixtures are present.

**WP0.2 Repository scaffold (M)**
- Research: `PLAN.md` §10 only.
- Do:
  - add `pyproject.toml` with the `uv_build` backend, pinned to the current uv minor version range as uv's docs
    recommend;
  - set `requires-python = ">=3.13"`, add `.python-version`, and add the runtime dependencies `fastapi`,
    `pydantic`, `typer` and `uvicorn` with `uv add`;
  - set up the `dev` group exactly as in §10.3;
  - configure Ruff (line length 100, rule sets from §10.3), strict mypy, pytest (markers `live` and `slow`,
    `timeout = 30`) and coverage;
  - add `.pre-commit-config.yaml` with the official `uv-lock`, `ruff-check --fix` and `ruff-format` hooks;
  - add `.github/workflows/ci.yml` (§7);
  - extend `.gitignore` with `.idea/`, `target/`, `*.checkpoint`, `*.history` and `*.runs/` (leftover norn
    artifacts);
  - commit `PLAN.md`, `research/` and this plan;
  - seed `docs/decisions/` with one record per amendment, A1–A24. The records for A21–A24, and for the cmux
    priority in A1 and the norn plugin in A10, cite the question they answer (§10).
- Tests:
  - an empty test suite passes all §10.5 commands, and `uv build` produces a wheel that contains `py.typed` and
    `fylgja/testing`;
  - `tests/unit/test_decision_log.py` collects every `A<n>` ID from the §2 table of this plan and asserts that
    exactly one record in `docs/decisions/` names it, so an amendment added later without a record fails CI.
- Done when: CI is green on Linux and macOS, and the decision-log test passes with 24 records.

**WP0.3 Join spike (S–M)** (this is `PLAN.md` Phase 0)
- Research: [01](research/01-local-findings.md) AeroSpace and registry sections,
  [03](research/03-agent-sessions.md) process level, [04](research/04-app-activity.md) JetBrains and iTerm2.
- Revalidate:
  - the exact `aerospace list-windows --monitor all --json --format …` invocation and its fields;
  - `subscribe --all` event names;
  - registry fields;
  - the parent-chain walk to the host app;
  - JetBrains `recentProjects.xml` under the newest product directory;
  - the cmux window and title shape.
- Do: `spikes/join_table.py` (stdlib only, read-only) prints *session name · kind/entrypoint · state · project ·
  host app · window-id · workspace · join via/confidence*. It also writes `docs/spikes/0001-join.md` with the join
  hit-rate per host and any unresolved rows with reasons.
- Done when: **every live registry row** (11 today, not the 13 in `PLAN.md`) appears with workspace, project and
  state, or with an explicit "unresolved: <reason>". The AeroSpace workspace must resolve for 100 % of cmux- and
  JetBrains-hosted rows. These findings, together with WP0.4's, settle A1 before WP1.8 starts.

**WP0.4 cmux spike (S)** (Q4: cmux is the focus)
- Research: R4 in §1, and the CLI bundled with cmux (`/Applications/cmux.app/Contents/Resources/bin/cmux`,
  version 0.64.25 on 2026-10-05) with its documentation.
- Find out the following. Everything is read-only, and the results go in `docs/spikes/0003-cmux.md`:
  1. which CLI commands list cmux windows, workspaces, tabs and surfaces, and whether they report each surface's
     tty, pid, cwd or title;
  2. whether the CLI works from outside a cmux terminal without the `CMUX_SOCKET_CAPABILITY` token that cmux gives
     its own child processes, in particular from a launchd agent. Fylgja must never read that token from other
     processes;
  3. which commands select a cmux workspace and surface, and whether selecting one also raises the right macOS
     window;
  4. the cmux window title format, and whether it follows the selected tab;
  5. what cmux's own Claude hooks (injected through `--settings`) already record, so Fylgja does not duplicate
     them.
- Done when: the join method (CLI or hook-time ids) and the focus method for cmux are recorded as a decision. If the
  CLI cannot be used from launchd, the decision says so: the join then relies on hook-time ids from Phase 2, at
  window level until then, and focus needs a documented alternative before WP3.5.

### Phase 1: daemon and store

**WP1.1 Core models and collector API (M)**
- Build §3.3: `models.py` and `collectors/base.py`, with mypy strict and full typing of the public API.
- Tests: model round-trips, the discriminated union, rejection of naive datetimes and dedup-key stability.
- Done when: `fylgja.collectors.base` and `fylgja.models` are importable with no import of FastAPI or Typer
  (asserted by a test).

**WP1.2 Plugin registry and configuration (M)**
- Build:
  - discover the `fylgja.collectors` entry-point group from metadata only (`EntryPoint.name`, `.value` and
    `.dist` name and version), without calling `EntryPoint.load()`;
  - reject duplicate names from that metadata, before anything is imported;
  - import, then check `api_major` and validate the config namespace with `config_model`, **only** for built-ins
    and for externals with an explicit `enabled = true` (`PLAN.md` §4.1). An import error in an enabled plugin is
    reported as `failed(import)` health. A disabled external is never imported, so it can have no import error;
  - enable built-ins from the packaged defaults and require explicit enabling for externals.
- Tests:
  - synthetic entry points covering an external plugin, a duplicate, an incompatible version, an import error in an
    enabled plugin, and enable/disable (`PLAN.md` §4.9 core registry tests);
  - **no import before opt-in:** a disabled external entry point whose module raises on import and writes a sentinel
    file. Registry discovery, `collectors list`, `doctor` and a full `serve` start-up and shutdown run with it
    installed; the test asserts that the sentinel file does not exist and the module is not in `sys.modules`.
- Done when: `fylgja collectors list --json` shows, for loaded plugins, name, origin (built-in/external), API
  version, capabilities, enabled state and health; and for disabled externals only name, origin, distribution name
  and version, and `disabled` (API version, capabilities and health are reported as `not_loaded`). The no-import
  test passes.

**WP1.3 Supervisor (M)**
- Build:
  - one task per collector;
  - exponential backoff from 1 s up to 5 min with ±20 % jitter, reset after 10 min healthy;
  - health states `starting/ok/degraded/failed/disabled`;
  - cancellation helpers that kill subprocess groups, close SQLite connections and stop file watchers;
  - graceful shutdown on SIGTERM within 5 s.
- Tests: a crashing collector does not stop its neighbours; cancellation releases every fake resource; a hung
  collector is detected by `pytest-timeout`.
- Done when: the crash-isolation and cancellation tests pass on both CI operating systems in 20 consecutive local
  runs, with no hang caught by `pytest-timeout`.

**WP1.4 Store and ingest gate (M)**
- Build §4.1:
  - migrations runner;
  - writer task;
  - validation, privacy filter and dedup;
  - `emit_committed` futures;
  - retention job and live view cache.
- Tests:
  - migration from empty and idempotent re-run;
  - concurrent fake collectors, where only validated records reach SQLite (`PLAN.md` §4.9);
  - retention boundaries;
  - redaction applied before the row is written.
- Done when: the store integration test passes and generated databases live under `target/tests/` and are cleaned
  up afterwards.

**WP1.5 Contract suite and fixtures (M)**
- Build `fylgja.testing`:
  - `CollectorContract` (the five checks in `PLAN.md` §4.9, parametrized by plugin name);
  - `FakeClock`, `FakeProcessRunner`, `FakeFS`, `FakeLineStream` and `RecordingSink`.
- Build `tests/fake_plugins/fylgja_fake`: a real distribution with its own entry point, added as a dev path
  dependency. It also ships a second entry point, `fake_import_bomb`, whose module raises on import and writes a
  sentinel file. It is never enabled, so every test run that installs the fake plugin also checks the WP1.2
  no-import rule against a real distribution.
- Sanitize the staged fixtures into `tests/fixtures/{ratatosk,lidskjalv}/` with `scripts/sanitize_fixture.py`
  (dev-only). Add the fixture-hygiene test.
- Done when: the fake plugin passes the contract, and the pluggability acceptance test (install, enable, observe via
  `/now`, stop, test, with no core edits) is written. It goes green after WP1.9.

**WP1.6 AeroSpace collector (M)**
- Research: [01](research/01-local-findings.md) AeroSpace section.
- Revalidate: WP0.3 results; whether `subscribe` restarts when AeroSpace restarts; the snapshot cost (expected
  ≈ 50 ms).
- Build:
  - a long-lived `aerospace subscribe --all` line reader that restarts when the stream ends;
  - snapshots on every focus event (debounced 250 ms) and every 30 s;
  - pure parsers for events and snapshots;
  - workspace list including empty and persistent workspaces;
  - a complete snapshot for each window list.
- Tests: contract; parser fixtures for each event type; a torn line; AeroSpace not installed (degraded, the rest
  keep running).
- Done when: the contract passes and a live smoke test (`-m live`) shows focus changes reaching `/now` within 1 s.

**WP1.7 Claude collector, read path (L)**
- Research: [03](research/03-agent-sessions.md) Claude section, [01](research/01-local-findings.md) Claude section.
- Revalidate:
  - registry fields and status values (R5);
  - **how Claude writes registry files** on 2.1.284 and 2.1.289: an atomic replace (new inode, which changes the
    directory mtime) or an in-place rewrite (same inode, which does not). Observe it with `stat` on inode, file
    mtime and directory mtime across a status change, a rename and a `/clear`;
  - transcript line types on 2.1.284 and 2.1.289;
  - **the task-list location (R7)**: find where TaskCreate tasks persist now, falling back to TaskCreate/TaskUpdate
    `tool_use` records in the transcript;
  - `history.jsonl` shape;
  - `claude agents --json` parity.
- Build:
  - registry poll every 2 s: list the directory with the A13 glob and `stat` every matching file. A file is
    reparsed when its `(st_ino, st_mtime_ns, st_size)` changes, and added or removed files come from the listing.
    The directory mtime is never used as a gate, because an in-place rewrite does not change it. A full reparse of
    every row runs every 60 s as a backstop. At about 11 rows this costs a few dozen `stat` calls every 2 s;
  - liveness check: pid alive and start time equal to `procStart`;
  - incremental transcript tail per live session, resuming from a byte offset and tolerating torn lines;
  - a Python port of Lidskjalv's `_SUMMARY_JQ` as a pure function producing the `facts` block (title, prompt,
    said, mode, branch, version, model, turns, prompts, tools, files, tasks), updated from TodoWrite to the task
    tools;
  - the Claude `SessionActivity` record built from the facts block, which feeds the §4.6 `one_liner`;
  - the `ignore_cwd` filter, defaulting to `CodexBar/ClaudeProbe` plus the summarizer cwd;
  - a Remote Control link from `bridgeSessionId`;
  - tagging of the `entrypoint` (`cli`, `sdk-py`, and so on);
  - the A11 continuity record;
  - state taken from registry `status` (`idle/busy/waiting`) until hooks arrive.
- Tests:
  - contract;
  - **parity with Lidskjalv**: for each Lidskjalv/ratatosk transcript fixture, the Python facts must equal the
    output of Lidskjalv's jq program, stored as golden JSON. A dev-only script in `spikes/` generates the goldens
    once and they are committed, so the tests never run or need Lidskjalv (A21);
  - a torn tail;
  - `/clear` rotation (ratatosk `clear-model-session`);
  - stale registry rows where the pid was reused;
  - **in-place registry update:** an existing registry file is rewritten in place with a new `status` and `name`,
    and the directory mtime is set back with `os.utime`. The next poll must observe both changes. The same test
    runs for an atomic replace;
  - the Claude one-liner rules from §4.6 against the transcript fixtures.
- Done when: the contract, parity and in-place-update tests pass, and `fylgja sessions` lists every live row with
  the same names as `claude agents --json`, each with a non-empty one-liner.

**WP1.8 Resolver and inferred labels (M)**
- Research: [01](research/01-local-findings.md), [04](research/04-app-activity.md) JetBrains, iTerm2 and generic
  sections.
- Build A1:
  - session → project, using the cwd's git root or the cwd itself; draupnir clones are grouped through the parent's
    `draupnir.toml`;
  - session → host → window → tab through the parent-chain walk, then the host adapter. cmux sessions resolve to
    their cmux workspace and surface when WP0.4 found a usable CLI method. Otherwise they resolve at window level
    until hook-time ids arrive in Phase 2;
  - window → workspace;
  - project → workspace, by window count now and by focus time from Phase 5;
  - deterministic inferred workspace label: dominant project or repo name, then the dominant app plus title
    keyword, then "—".

  Every join records `via` and `confidence`.
- Tests: table-driven fixtures for each host kind, ambiguous JetBrains windows (the longest project-path prefix
  wins), and the unresolved path.
- Done when: the spike table and `/now` agree on 100 % of rows in a recorded live fixture.

**WP1.9 Read API and `/now` (M)**
- Build:
  - FastAPI app with the Host middleware (A6);
  - read endpoints for workspaces, sessions, projects, collectors and health;
  - the `/now` model and generated schema, including every optional section and field that later work packages
    fill (§4.2, §4.6, §4.7), so `fylgja.now/1` does not change when they arrive;
  - Uvicorn bound only to 127.0.0.1.
- Tests:
  - HTTPX ASGI transport;
  - Host rejection (DNS-rebinding case);
  - no CORS headers;
  - schema-version guard;
  - `/now` with a degraded collector (its section is absent and the response is 200).
- Done when: the pluggability acceptance test from WP1.5 is green and the `/now` p95 budget is met with fixtures.

**WP1.10 CLI, doctor and launchd (M)**
- Revalidate on macOS 27: `launchctl bootstrap gui/$UID` and `bootout`, `KeepAlive` and `ThrottleInterval`
  behaviour, and the PATH a LaunchAgent receives.
- Build:
  - `serve`, `now`, `ws [n]`, `sessions` and `collectors list`, each with `--json`;
  - `doctor`, covering config, plugins, sources, the FDA-path guard, daemon reachability, **dangling hook command
    paths in `~/.claude/settings.json`** (R1), port conflicts, and the A22 dotfiles layout (every file Fylgja
    installed is a symlink into `~/dotfiles` and listed in `dotfiles.conf`);
  - `install config [--apply]`: creates the user `config.toml` at `~/dotfiles/fylgja/.config/fylgja/config.toml`,
    adds the `fylgja:.config/fylgja/config.toml` entry to `dotfiles.conf` and links the file into place (A22);
  - `install launchd [--apply]`: writes a plist that runs `~/.local/bin/fylgja serve` with an explicit PATH,
    `KeepAlive`, `ThrottleInterval=10` and logs in the state dir, then runs `launchctl bootstrap
    gui/$UID`. The plist is machine-specific and is not stored in dotfiles.
- Tests: CLI against a fake HTTP server; plist generation as a golden file; each doctor check with fakes.
- Done when: `uv tool install .` followed by `fylgja install launchd --apply` gives a daemon that survives a reboot,
  and `fylgja now` prints the workspace/session table.

**WP1.11 Lidskjalv renders from `/now` (S–M; Fylgja, plus a PR in the Lidskjalv repo)**
- Research: the Lidskjalv read-site inventory listed under WP2.6.
- Revalidate: those read sites and line numbers against the current `first-draft` branch of the canonical
  checkout.
- Build in Fylgja: `docs/api/lidskjalv-mapping.md`, which maps each field Lidskjalv reads (`sessions/<sid>.json`,
  `lane.json`, `summaries/<sid>.json` and `roster.json`) to a `/now` path, and a set of recorded `/now` fixtures
  that covers every session state and host kind available in Phase 1.
- Build in Lidskjalv: the transform from `/now` to Lidskjalv's own shapes, as a jq program in a new
  `lib/fylgja_now.sh`. It produces the roster, the session records, the lanes and the summary facts that the
  SketchyBar renderer and `_panel_json` already consume. It is reachable only through an explicit offline input,
  `LIDSKJALV_NOW_FILE=<path>`, read by `plugins/lidskjalv_sessions.sh` (chips) and by `lidskjalv.sh panel --json`
  (panel), and it changes no default and no live path. WP2.6 later wires the same transform behind `LIDSKJALV_SOURCE_SESSIONS`,
  with the live fetch and fallback.
- Tests:
  - in Fylgja: a mapping test asserts that every mapped `/now` path exists in the schema and in the recorded `/now`
    fixtures;
  - in Lidskjalv: BATS that render chips and panel JSON from each `/now` fixture, compared with the output rendered
    from the equivalent Lidskjalv cache fixture. The full suite (722 tests) stays green.
- Done when (**`PLAN.md` Phase 1 "Lidskjalv can render from `/now`"**): Lidskjalv renders the same chips and panel
  JSON from the recorded `/now` fixtures as from its own cache for the same sessions, covering every session-chip
  and panel field in scope for `LIDSKJALV_SOURCE_SESSIONS` (sessions, lanes, summary facts and the roster; A24); and
  both built-in plugins pass the contract. This needs no hook and does not depend on the Phase 2 entry gate (R1).

Phase 1 exit check: the daemon runs under launchd for 24 h within the §4.2 budgets, with no collector stuck in
`failed`.

### Phase 2: dual-run Lidskjalv and Fylgja

**WP2.1 `fylgja hook` ingress (M)**
- Research: [03](research/03-agent-sessions.md) hooks; the Claude hooks docs.
- Revalidate: the hook event list and stdin fields on the installed version, async semantics (R9), the
  `CLAUDE_CODE_MESSAGING_SOCKET` export, and Python start-up cost from a `uv tool` virtualenv.
- Build per `PLAN.md` §4.3 together with A3:
  1. **Entry point.** `_entry.main` dispatches `hook` before any non-stdlib import.
  2. **Silence and runtime bound.** First redirect fds 1 and 2 to `/dev/null`, then arm a 3 s `signal.alarm`
     backstop that calls `os._exit(0)`.
  3. **Reading stdin.** Read with a 2 s deadline using `select` and `os.read`.
  4. **Size caps.** Raw input up to 256 KiB is kept verbatim. Above that, the JSON is parsed and string fields over
     4 KiB are replaced with `{"_truncated": n}`. Input that cannot be parsed becomes a `malformed_oversize`
     marker.
  5. **Envelope.** `{schema:"fylgja.hook/1", id:uuid4, received_at_ns, source, event_arg, ppid,
     env:{allowlist}, payload_text, truncated, fylgja_version}`. The envelope is a stable, versioned contract documented in `docs/api/inbox-envelope.md` with a golden fixture,
     because writers outside this package also produce it: the norn plugin (WP3.3) and the OpenCode plugin
     (WP3.2).
  6. **Atomic write.** Open `.tmp-<id>` with `O_CREAT|O_EXCL|O_WRONLY`, mode 0600, fsync it, then rename it to
     `<received_ns>-<id>.json`.
  7. **Inbox checks.** Create the inbox with mode 0700. Refuse to write (and exit silently) if the inbox path is a
     symlink or has the wrong owner or mode.
  8. **Backlog limit.** Check the backlog at most once every 60 s, tracked by a stamp file's mtime. Over 100 000
     files or 512 MiB, drop the event and increment `dropped.count`.
  9. **Exit.** Always exit 0.

  `--source` (default `claude`) lets Codex and OpenCode reuse the same path. `StatusLine` follows A4. The hook
  exits immediately when `FYLGJA_SUMMARY` or `LIDSKJALV_TRACK_SUMMARY` is set, so summarizer sessions are never
  ingested. This only reads an environment variable; it does not depend on Lidskjalv.
- Tests (`PLAN.md` §4.9 hook-ingress list):
  - 200 concurrent invocations yield 200 distinct complete files;
  - permissions;
  - nothing on stdout or stderr, even when stdin is invalid or the inbox is unwritable;
  - stdin that never closes still terminates within 3 s;
  - oversize input;
  - an import-guard test that runs `python -X importtime` and asserts no third-party module is imported;
  - the canary token never appears in the spool.
- Done when: all hook tests pass, and the measured p95 is ≤ 60 ms (`scripts/bench_hook.py`, results recorded in
  `docs/spikes/0002-hook-cost.md`). If the budget is missed, A2 drops PreToolUse by default and the decision is
  recorded.

**WP2.2 Inbox drain and hook normalization (M)**
- Build:
  - the Claude plugin's inbox adapter: a directory watch with `select.kqueue` and a 1 s polling fallback;
  - files processed in name order;
  - the normalizer to minimized core events (A5), committed with `emit_committed`, then the file is unlinked;
  - files that fail parsing go to `quarantine/`;
  - a statusline reader for `statusline/*.json`, triggered by mtime (context %, cost, PR, rate limits).
- Tests:
  - delete-after-commit;
  - a crash between commit and unlink, replayed idempotently thanks to the envelope-id dedup key;
  - a backlog of 10 000 files drained within 30 s;
  - quarantine;
  - the canary token never appears in the database.
- Done when: the simulated-crash replay test passes.

**WP2.3 Session state machine (M)**
- Research: the status layering in [03](research/03-agent-sessions.md), plus R9 Notification types.
- Build these state rules:

  | Signal | State |
  |---|---|
  | UserPromptSubmit | `working` |
  | PreToolUse | `running_tool(name)` |
  | PermissionRequest or `Notification.permission_prompt` | `waiting_permission` |
  | `agent_needs_input` or `elicitation_dialog` | `waiting_input` |
  | Stop | `turn_done` |
  | `idle_prompt` | `idle` |
  | StopFailure | `error` |
  | SessionEnd | `ended` |

  Registry `waiting/busy/idle` is the fallback when no hook has arrived. `turn_done` becomes `seen` when the
  session's window gains focus after the Stop event (this feeds Phase 6). A `/clear` rotation links the
  predecessor session (A11).
- Tests: table-driven sequences built from sanitized hook captures; out-of-order events (ordered by
  `received_at_ns`, then `observed_at`); a session that has hooks only and no registry row.
- Done when: the state fixtures pass for every ratatosk scenario and for fresh captures.

**WP2.4 Hook and statusline installation, persisted in dotfiles (M)** (A2, A4, A22)
- Revalidate:
  - the `dotfiles.conf` format, and the current links for `settings.json` and `statusline.sh` (both are listed
    under the `claude` package on 2026-10-05);
  - that Claude Code's settings validation accepts the guarded command form.
- Build:
  - `fylgja install hooks [--apply]` adds the A2 event set with `"async": true` and the guarded command
    `"$HOME/.local/bin/fylgja" hook <Event>`, which exits 0 silently when the executable is missing (A22). These
    registrations are permanent and independent of Lidskjalv's (A21). Fylgja entries are recognized by their
    command string. The installer:
    - resolves `~/.claude/settings.json` to its file in `~/dotfiles/claude/.claude/` and edits that file, after a
      timestamped backup under `~/.local/state/fylgja/backups/`;
    - refuses to write when `~/.claude/settings.json` is not a symlink into `~/dotfiles`, and explains how to
      restore the link;
    - is idempotent and never edits or reorders non-Fylgja entries, Lidskjalv's in particular;
    - prints a unified diff without `--apply`. After `--apply` it never commits (Q10). It prints
      `git -C ~/dotfiles diff --stat` for the files it touched and a suggested `git -C ~/dotfiles commit` command
      limited to those files, so your other uncommitted dotfiles changes are left alone;
  - `fylgja install statusline [--apply]` inserts the A4 snippet as a marked block into
    `~/dotfiles/claude/.claude/statusline.sh`. It is idempotent, with the same backup and diff behaviour;
  - doctor checks:
    - executable path;
    - every Fylgja registration has `async: true`;
    - only if Lidskjalv's entries are present: they are untouched and their target exists. This check is
      informational, and its result never affects Fylgja's health (A21);
    - inbox owner and mode;
    - backlog age;
    - `dropped.count`;
    - the installed Claude version;
    - Fylgja's dotfiles changes are committed (a warning only, per Q10).
- Tests:
  - the configuration test from `PLAN.md` §4.9, run against fixtures of the current `settings.json` shape including
    `SessionEnd` matchers. It checks that every Fylgja hook is async, and that the installer leaves Lidskjalv's
    entries exactly as they were;
  - **independence:** the same tests, and doctor, pass against a settings fixture with no Lidskjalv entries at all;
  - a settings file that is a regular file rather than a symlink into dotfiles is refused;
  - a malformed settings file is refused without being touched;
  - the statusline block is inserted once across reruns;
  - the guarded command exits 0 when the executable is missing.
- Done when: running `--apply` against a copy of the dotfiles tree yields a diff that adds only Fylgja entries and
  the statusline block, and a second run changes nothing.

**WP2.5 SSE `/events` (S)**
- Build: an SSE stream of change notifications (`now.changed` with a version counter, `session.state`,
  `focus.changed`), a 15 s heartbeat comment, and a client limit of 16.
- Tests: ASGI streaming test, disconnect cleanup.
- Done when: a subscribed client receives `now.changed` within 1 s of a committed change and a heartbeat every 15 s
  (with a fake clock); the 17th client is refused with 503; and after 100 connect/disconnect cycles the daemon's
  task count and open file descriptors return to their baseline.

**WP2.6 Lidskjalv session source switch (M; in the Lidskjalv repo)** (A24)
- Research: the Lidskjalv survey:
  - `plugins/lidskjalv_sessions.sh` reads the roster at 452–470, stories at 600–607, summaries at 615–624, lanes at
    626–700 and records at 1048–1069;
  - `bin/lidskjalv.sh` builds `_panel_json` at 1418–1569 and runs `_roster_live_sessions` at 496.
- Build `LIDSKJALV_SOURCE_SESSIONS=lidskjalv|fylgja` (default `lidskjalv` during the dual run). It covers the
  roster, session records, lanes and summary facts. In `fylgja` mode Lidskjalv fetches
  `curl -sf --max-time 0.3 http://127.0.0.1:7319/now` and passes it through the WP1.11 transform
  (`lib/fylgja_now.sh`) into the record and lane shapes the renderer and `_panel_json` already consume. If that
  fails, it falls back to its own cache for that render and
  logs to `debug.log`. Builds, costs, quota and stories keep Lidskjalv's own paths; they get their own switches in
  WP5.5 and WP8.3 (A24). Reminder injection stays in Lidskjalv (A18).
- Tests: new BATS files with a stub `curl` serving `/now` fixtures, plus the fallback path. The full suite (722
  tests) stays green.
- Done when: SketchyBar and the panel render the same chips with either source for a recorded fixture set.

**WP2.7 Dual-run verification (S, then 1–2 weeks of observation)**
- Build `scripts/compare_lidskjalv.py`. It compares per-session state transitions in Fylgja (`events` and
  `sessions`) with the files in `~/.cache/lidskjalv/sessions/*.json` and `lanes/*` over a time window, and reports
  mismatches with reasons. It is a dual-run tool only: the package and the test suite never import or need it
  (A21).
- Opt-in live tests:
  1. **Claude does not wait for Fylgja.** A disposable `claude -p` run in a temp dir, using `--settings` with a
     Fylgja hook built with an injected 5 s delay, shows the same turn latency (± 10 %) as a run without it.
  2. **Downtime replay.** Run `launchctl kill TERM` on the daemon, run a session for 10 min, restart the daemon, and
     confirm that every envelope is ingested exactly once.
- Done when (**`PLAN.md` Phase 2**):
  - Claude never waits for Fylgja (test 1 above);
  - over 7 consecutive days of dual-run, ≥ 99 % of transitions match, and every remaining mismatch is explained;
  - the downtime replay passes;
  - Lidskjalv BATS stay green.

  Passing this gate starts WP2.8.

**WP2.8 Lidskjalv defaults to Fylgja, and Lidskjalv can be removed (S–M; Lidskjalv repo and Fylgja)** (Phase 2b,
A21)
- Precondition: WP2.7 is done.
- Build in Lidskjalv:
  - `LIDSKJALV_SOURCE_SESSIONS` defaults to `fylgja`. The `lidskjalv` source keeps working, because Lidskjalv's
    hook still writes its own cache (A24), and it is the fallback for any render where Fylgja is unreachable.
  - Lidskjalv's hook does **not** forward to Fylgja (A21). Shrinking it to what Lidskjalv still needs, such as
    reminder injection (A18), comes only when Lidskjalv retires its own session source. That is Lidskjalv's
    decision and is not scheduled here.
  - BATS: the new default, the fallback, and every subcommand (`show`, `panel`, `story`, `builds`, `costs`,
    `remind`) still working.
- Build in Fylgja:
  - **Independence suite.** A CI job sets up a test home directory with no Lidskjalv in it: no hook entries, no
    `~/.cache/lidskjalv`, no repository and no `lidskjalv.sh` link. The full Fylgja test suite and `fylgja doctor`
    must pass there. Fylgja's tests use only committed fixtures, including the Lidskjalv parity goldens from WP1.7,
    and never need a Lidskjalv checkout.
  - **Removal runbook.** `docs/runbooks/remove-lidskjalv.md` lists what removing Lidskjalv involves:
    - its hook entries in `~/dotfiles/claude/.claude/settings.json`;
    - the `claude:.claude/lidskjalv.sh` line in `dotfiles.conf` and its link;
    - the SketchyBar plugins and the panel;
    - `~/.cache/lidskjalv`.

    It also lists the checks showing Fylgja is unaffected: doctor stays green, `/now` is unchanged, and hook events
    keep arriving.
- Done when:
  - Lidskjalv has run on the Fylgja source by default for 3 days with no regressions;
  - the independence suite is green in CI;
  - the runbook has been dry-run against a copy of the dotfiles tree, with Fylgja's checks unchanged.

### Phase 3: all agents and actions

**WP3.1 Codex plugin (M)**
- Research: [03](research/03-agent-sessions.md) Codex section; codex-trace (see [02](research/02-similar-tools.md))
  as a reference parser.
- Revalidate: the `state_5.sqlite` schema (the file name carries a version number), rollout line types, the `lsof`
  rollout join and `notify`/hooks on 0.154+.
- Build:
  - `pgrep` plus `lsof` to find the open rollout and its thread id;
  - read-only SQLite access with a URI using `mode=ro`;
  - a rollout tail that compares `task_started` with `task_complete`;
  - schema-version detection: an unknown `state_N` marks the plugin degraded and does not guess;
  - the Codex `SessionActivity` record (§4.6): thread title, last user message and current function call;
  - optional `notify`/hooks that call `fylgja hook --source codex <Event>`, documented and off by default.
- Tests: contract; SQLite fixtures built from sanitized DDL and rows; rollout fixtures; one-liner goldens; an
  unknown `state_N` fixture gives `degraded`, not a crash.
- Done when: the contract and fixture tests pass, and a live smoke test (`-m live`) shows every running Codex
  process in `/now` with the correct busy/idle state, its project and a non-empty one-liner, with state changes
  visible within one poll interval.

**WP3.2 OpenCode plugin (M)**
- Research: [03](research/03-agent-sessions.md) OpenCode section; the opencode-monitor pattern in
  [02](research/02-similar-tools.md).
- Revalidate: the `opencode.db` schema on the current version and the plugin event names.
- Build:
  - process scan and cwd to the newest session row (`parent_id is null`, not archived);
  - an incomplete assistant message means busy;
  - the `todo` table is exposed as the session's own list;
  - the OpenCode `SessionActivity` record (§4.6): session title, in-progress `todo` row and the incomplete
    assistant message's tool;
  - an optional `~/.config/opencode/plugins/fylgja.ts` that writes envelopes straight into
    `hook-inbox/opencode/` with the same atomic-rename and 0600 rules. This is the only TypeScript allowed by
    `PLAN.md` §10.
- Tests: contract; DB fixtures; one-liner goldens; envelope compatibility between the TS writer and the Python
  reader, using a golden file.
- Done when: the contract, fixture and envelope tests pass, and a live smoke test shows every running OpenCode
  session in `/now` with the correct busy/idle state and a non-empty one-liner, within 2 s with the TS plugin
  installed and within one poll interval without it.

**WP3.3 norn integration (M in the norn repo + M in Fylgja)** (A10, Q6)
- Research:
  - [03](research/03-agent-sessions.md) and [01](research/01-local-findings.md) norn notes, plus R3;
  - in `~/projects/erik.romson/norn/main`:
    - `norn/event_sink.py`: `EventSink`, with a single `on_event` subscriber;
    - `norn/journal.py`: `JournalSink` wraps the inner sink and journals redacted events;
    - `norn/events.py`: typed events with a `Delivery` class, including `WaitingInput`;
    - `norn/registry.py`: the existing `norn.stages` entry-point group, which is the pattern to copy.
- Revalidate:
  - the current `run.json`, `.history` and `events.jsonl` keys;
  - the stage status values (`passed`, `failed`, `skipped`, `skipped_condition`, `cached`, `cancelled`);
  - how runs started from the CLI and from `norn ui` wire their sinks (`norn/runner.py` around line 1188, and
    `norn/tui/app.py`).
- **(a) In the norn repo: an event-subscriber plugin.**
  - Add a `norn.event_subscribers` entry-point group, read the same way as `norn.stages`. Each subscriber receives
    the same redacted events the journal sees, whether the run was started from the CLI or the TUI. Subscribers
    are isolated: an exception or slow subscriber is logged and never fails, blocks or slows a run (warn and
    continue, as journal writes already do). This is the only change to norn's core.
  - Ship a built-in `fylgja` subscriber in norn, registered under that group. It is stdlib-only, so it works in
    norn's own environment without Fylgja installed there. It writes `fylgja.hook/1` envelopes with
    `source: "norn"` atomically into `hook-inbox/norn/`, for these events:
    - `RunStarted`, with pid, cwd, pipeline, state key, history run id and argv;
    - stage start and finish;
    - `CallingAgent` and `TurnEvent` session ids;
    - `WaitingInput`;
    - `RunPaused`, `RunCancelled` and `RunFinished`;
    - a heartbeat every 30 s.

    It forwards ids, names, statuses, costs and timestamps only, never prompts or outputs.
  - It is on whenever Fylgja's inbox directory exists, and `NORN_FYLGJA=0` turns it off.
  - Tests in norn: a temp inbox, a subscriber that raises, a slow subscriber, and the envelope checked against
    Fylgja's golden fixture (`docs/api/inbox-envelope.md`).
- **(b) In Fylgja: the norn collector.**
  - Inbox-driven state per A10:
    - `running` while the heartbeat is fresh and the pid is alive;
    - `waiting_input` on `WaitingInput`, which feeds "waiting on me" in WP6.1;
    - terminal states from the finish events;
    - `stale` when the heartbeat stops without a finish event.
  - The file scan as fallback and backfill: roots from config plus known project cwds, depth-limited and gated by
    mtime, with the last `.history` line per `run_id` winning. Runs with no plugin events are checked by matching
    processes.
  - `sdk-py` Claude sessions are nested under their run through the session ids;
  - the norn `SessionActivity` record (§4.6): pipeline, current stage with index and total, `WaitingInput`, and the
    terminal status.
- Tests:
  - the contract suite;
  - inbox fixtures;
  - one-liner goldens for a running stage, a run waiting for input and a finished run;
  - file fixtures sanitized from `plan_with_review.*` in this repo and from norn's own run files;
  - a crashed run shows as `stale`, both with and without the plugin.
- Done when:
  - a live norn run shows in `/now` with its current stage within 2 s of each stage change;
  - a run waiting for input appears in `waiting[]`;
  - killing the run turns it `stale` within 90 s.

**WP3.4 Mutation security layer (S)**
- Build A6 bearer, Origin and content-type checks, request size limits (16 KiB, or 64 KiB for messages), and
  `install secret`.
- Tests: missing or wrong bearer; a foreign Origin; `text/plain` form CSRF; oversize; a valid CLI request with no
  Origin.
- Done when: a parametrized test enumerates every route registered on the app with a mutating method (`POST`,
  `PUT`, `PATCH`, `DELETE`) and asserts that each one rejects a missing bearer, a foreign Origin, a non-JSON content
  type and an oversize body. A mutating route added later without the guards therefore fails CI.

**WP3.5 Focus actions (M)** (Q4: cmux first)
- Research: the WP0.4 cmux decision (`docs/spikes/0003-cmux.md`); [04](research/04-app-activity.md) JetBrains
  and iTerm2 sections; Lidskjalv's `plugins/lidskjalv_focus.sh` (the `aerospace workspace`, `focus --window-id`
  and iTerm2 UUID-select sequence).
- Revalidate: the WP0.4 cmux commands against the cmux version installed at the time.
- Build:
  - `POST /focus/window/{id}` (`aerospace focus --window-id`);
  - `POST /focus/session/{key}`, which goes to the AeroSpace workspace, then the window, then a host-specific step:
    - **cmux (primary):** select the session's cmux workspace and surface using the WP0.4 method;
    - JetBrains: the project window only (`PLAN.md` §14);
    - iTerm2 (best effort, behind its switch): AppleScript select by session UUID;
  - return `{focused: window|tab, via}`.
- Tests: fake process-runner command sequences for each host, and the fallback to window level when an adapter
  fails.
- Done when: a live smoke test focuses a cmux-hosted session at tab level and a JetBrains-hosted session at window
  level. iTerm2 is not required.

**WP3.6 Local Claude messaging (L)**
- Decision (Q7, "as recommended"): build it as specified, disabled by default and with honest receipts. Fylgja
  never changes `crossSessionInbound`. In Erik's bypass-permissions sessions, Claude's default rule holds each
  message behind an approval dialog that expires after 5 minutes. That is expected behaviour, which doctor and the
  receipt explain; it is not a failure.
- Research: `PLAN.md` §4.3.1, Anthropic's cross-session messaging docs (rechecked today, R10), the Blake Crosley
  survey and [03](research/03-agent-sessions.md) item 9.
- **Spike first** (`docs/spikes/0004-message-frame.md`), go or no-go:
  1. Find the user-message frame that Claude 2.1.284 and 2.1.289 accept. Sources are the survey and a capture in a
     disposable environment. The capture puts a logging proxy socket at a *disposable* target session's registry
     path and has a second disposable session message it.
  2. Find whether a `queue-operation` transcript record carries a sender-supplied message id.
  3. Observe behaviour under explicit `accept`, `hold` and `refuse` (set with the disposable sessions'
     `--settings`), and under the default rule when the target runs bypass permissions.

  No-go means the encoder adapter stays empty, sending reports `unsupported`, and focus-and-copy is the action.
- Build:
  - **target resolution:** native session id → live registry row; pid and `procStart` checked immediately before
    connecting; A14 path rules; A9 gates; any socket path supplied through the API is rejected;
  - **transport:** connect and write with a 2 s deadline, a single attempt, no retry on ambiguity, and plain text
    capped at `max_chars` (default 8 000), with a Fylgja provenance label and a generated message id;
  - **receipts:** `submitted`, `observed-enqueued` (only if spike step 2 confirms it), `unsupported`,
    `unreachable`, `refused-locally`, `timed-out` and `unknown`;
  - **interfaces:** the API endpoint (disabled by default) and `fylgja session message <id> "text"`, which asks for
    confirmation unless `--yes` is passed;
  - **doctor:** version compatibility, registry and socket safety, and the effective inbound setting when it can be
    determined from user, project/local, managed and process `--settings` sources plus the target's permission
    mode. Doctor states plainly, for example, "messages to this session will be held for approval".
- Tests: the full list in `PLAN.md` §4.9 (fake Unix sockets, symlink and owner rejection, pid reuse, partial
  writes, timeout, cancellation, size limits, version gates, single attempt, truthful receipts, no token anywhere).
  Protocol fixtures cover the minimum and current supported versions. The opt-in live test sends a nonce to
  disposable sessions under `accept`, `hold` and `refuse`.
- Phase 3 acceptance test (`tests/integration/test_phase3_agents.py`): a recorded `/now` fixture holding Claude,
  Codex, OpenCode and norn sessions, rebuilt from the collectors' fixtures. Every item in `sessions[]` must have a
  non-empty `state` and `one_liner` (§4.6). A live variant (`-m live`) runs the same assertion against the daemon.
- Done when (**`PLAN.md` Phase 3**, together with WP3.1–3.5):
  - every live agent is listed with its state and one sentence (the acceptance test above, fixture and live);
  - adding the three plugins needed no change to daemon dispatch (shown by the diff);
  - messaging passes the safety, policy and truthful-receipt tests, with focus/copy as the fallback.

### Phase 4: todos and labels

**WP4.1 Todo core (M)**
- Build:
  - the `todos` store and service;
  - API (`GET`, `POST`, `PATCH`, `DELETE`);
  - CLI `todo add|done|rm|ls [--ws n|--project p|--session id]`; `add` defaults to the focused workspace taken from
    the live view;
  - statuses `open`, `next` and `done`;
  - soft delete.
- Tests: scoping, the focused-workspace default, idempotent `done`, and auth on writes.
- Done when: those tests pass, and an integration test against a running daemon with a fake live view does
  `fylgja todo add "x"`, `todo ls`, `todo done <id>` and `todo rm <id>`, with the item scoped to the focused
  workspace and every step visible in `GET /todos` and `/now`.

**WP4.2 Manual workspace labels (S)**
- Build `label ws n "text"` and `label ws n --clear`, plus the PUT and DELETE endpoints. The manual label always
  wins until it is cleared, and clearing restores the inferred label (`PLAN.md` §7).
- Tests: precedence and clear-restores-inference.
- Done when: those tests pass, and in an integration test `/now` `workspaces[].label` shows the manual label
  directly after the `PUT`, keeps it while the inferred label changes underneath, and shows the inferred label
  again directly after the `DELETE`.

**WP4.3 Importers, read-only (S–M)**
- Research: [03](research/03-agent-sessions.md) Claude tasks and the OpenCode `todo` table.
- Revalidate: the Claude task-list source found in WP1.7, and the OpenCode `todo` columns.
- Build:
  - **repo-root `TODO.md`:** checkbox lines (`- [ ]`, `- [x]`), with an optional `next` marker;
  - **agent task lists:** the Claude source from WP1.7 and the OpenCode `todo` table, shown as the session's own
    list.

  Imported items have `source` and `source_ref` set and are read-only in Fylgja, with a link back to the file path
  or session.
- **Reconciliation.** Each source is read as a whole, and every read reconciles all of that source's rows:
  - **keys:** an agent task is keyed by its native task or todo id. A `TODO.md` line has no id, so it is keyed by
    file path plus normalized text (checkbox and `next` marker stripped, whitespace collapsed) plus an ordinal
    among identical lines. A checkbox or `next` change is therefore an update, and a text edit to a `TODO.md` line
    is a removal plus a new item, which is documented;
  - **upsert:** present items update `text`, `status`, `link` and `updated_at` in place;
  - **absent items:** an item that is no longer in a complete read gets `deleted_at` with
    `deleted_reason = source_absent`. If the same key reappears, the row is restored (`deleted_at` cleared) instead
    of a new row being created;
  - **file or session gone:** a missing `TODO.md` (ENOENT), or an agent session that ended with its task list
    removed, counts as a complete, empty read. All its items become absent;
  - **failed reads never reconcile:** a parse error, permission error or partial read leaves the existing rows
    untouched and marks the importer degraded, so a transient failure cannot wipe a list.
- Tests: parser fixtures; dedup when a source file changes; a checkbox ticked and unticked; a text edit; a deleted
  line; a duplicated line; a deleted and re-created file; a reappearing item restored rather than duplicated; an
  unreadable file leaving rows untouched; an agent task renamed in place (same id) updated rather than replaced.
- Done when: the reconciliation tests pass, and a live check on Erik's machine shows every repo-root `TODO.md` in
  known projects and every live agent task list under the right scope with source links, with no duplicates or
  stale rows after three consecutive edits to one `TODO.md`.

**On hold in the todo work (Q8, A23)**
- **Obsidian PARA import.** This is not scheduled. Two things must be settled before it resumes:
  1. how a PARA project note maps to a repository path. The `create-para-project` skill writes no frontmatter
     path; it records `Working directory:` in the Dev Log and `[[repo]]` under `### Repos`;
  2. which vaults are in scope.

  The research inputs stay [01](research/01-local-findings.md) (vaults) and [04](research/04-app-activity.md)
  (`obsidian.json`). Fylgja would only read vaults, never write to them. Obsidian window-title context in WP5.3 is
  not affected.

**WP4.4 Fast entry and the minimal page (S–M)**
- Build:
  - an AeroSpace keybinding that runs `osascript display dialog` and then `fylgja todo add`. `fylgja install
    keybinding` prints it, and with `--apply` writes it as a marked block into
    `~/dotfiles/aerospace/.aerospace.toml` (A22);
  - a user-level Claude skill `fylgja-todo` that runs `fylgja todo add --session … "$ARGUMENTS"` (A18). `fylgja
    install skill --apply` installs it into `~/dotfiles/claude/.claude/skills/fylgja-todo/`, adds the
    `claude:.claude/skills/fylgja-todo/` entry to `dotfiles.conf` and creates the link, like the existing skills
    (A22). How the skill passes the session id is revalidated against the skills docs;
  - the minimal page at `/` (A19).
- Done when (**`PLAN.md` Phase 4**): a todo can be added and finished from the terminal and from the page, each in
  under 5 s of human time, timed in a recorded walkthrough.

### Phase 5: context

**WP5.1 Presence collector (M)** (A17)
- Research: [04](research/04-app-activity.md) Generic and Teams/call sections.
- Revalidate **on macOS 27**:
  - `ioreg HIDIdleTime`;
  - `CGSessionCopyCurrentDictionary` lock key;
  - CoreAudio `kAudioDevicePropertyDeviceIsRunningSomewhere`;
  - CMIO `DeviceIsRunningSomewhere`.

  All of these must work from the *launchd* context without prompts.
- Build:
  - idle poll every 15 s, only while a focus span is open;
  - lock state;
  - mic and camera in use through `ctypes`;
  - events `idle.start/end`, `lock.start/end` and `call.start/end`.
- Tests: contract; parser and threshold logic with a fake clock; a `ctypes` failure gives a degraded state.
- Done when: the contract passes; the macOS 27 revalidation shows each of the four signals working from the launchd
  agent without a prompt, or a failing signal is recorded as a decision and reported as degraded rather than
  guessed; and a live smoke test under launchd shows a screen lock and unlock, an idle period and a call start and
  end as events within 15 s of each transition.

**WP5.2 Focus spans and history (M)**
- Build: spans derived from AeroSpace focus events, closed on idle (threshold default 5 min, back-dated to the start
  of the idle period), on lock and on shutdown; each span is attributed to a project and session through the
  resolver. Then:
  - `/history?since=&until=` with per-workspace, per-project and per-app totals and an ordered span list;
  - `fylgja history`;
  - `history.jsonl` from Claude as a secondary "worked on" signal;
  - the project → workspace join switches to focus time over the last day (`PLAN.md` §3 rule 4).
- Tests: span algebra (overlaps, idle back-dating, a daemon restart mid-span) and a day-replay fixture.
- Done when (**`PLAN.md` Phase 5** "where have I been today is correct"): over 2 workdays, per-workspace totals are
  within ±10 min of Erik's manual log, and spans of 2 min or more are attributed to the correct project ≥ 90 % of
  the time.

**WP5.3 Application context (L)**
- Research: [04](research/04-app-activity.md) per-app sections.
- Revalidate:
  - **title formats on current app versions**, including cmux titles and the A15 unread markers;
  - Chrome Automation from launchd and its TCC attribution to the installed Python;
  - JetBrains `recentProjects.xml` location;
  - every `[U]` finding.
- Build:
  - pure title parsers per bundle id: cmux first (Q4), then JetBrains, Chrome with profile, Teams, Obsidian,
    Outlook, Slack and DataGrip;
  - a second tier on focus change only:
    - cmux: the selected cmux workspace and tab, with its cwd, using the WP0.4 method;
    - Chrome active tab URL (AppleScript, behind its switch, 3 s timeout);
    - JetBrains project path;
    - Obsidian vault path and an `obsidian://` link;
    - iTerm2 `path`/`jobName` (best effort, behind its switch, off by default);
  - context items attached to windows;
  - optional Chrome history backfill from `?immutable=1`, read-only, behind a switch.
- Tests: the shared collector contract (the `appcontext` collector is a built-in like any other, §6); title fixtures
  per app captured from the currently installed versions, with A15 marker fixtures; AppleScript timeouts degrade
  only that switch.
- Done when: the contract and fixture tests pass for every listed app; a live smoke test shows the cmux workspace
  and tab context of the focused cmux window in `/now` within 1 s of focus; and an AppleScript timeout disables only
  its own switch while every other context source keeps reporting.

**WP5.4 Project state, builds, tokens and quota (M–L)**
- Research: `PLAN.md` §4.8; draupnir CI handling as a reference; Lidskjalv `lib/builds.sh` (`_BUILDS_FOLD_JQ`,
  call budget and ETag handling) and `lib/costs.sh` (`_COSTS_JQ`).
- Revalidate:
  - the `gh pr view --json` fields and the `gh api …/actions/runs` response on the installed gh;
  - the CodexBar CLI's cost and usage JSON on the installed version, and which providers it covers;
  - the statusline `rate_limits` and `cost` fields on the installed Claude versions;
  - the Lidskjalv jq programs above, against the current checkout.
- Build, per known project on a slow timer (5 min, or sooner on focus):
  - git branch, dirty count, ahead/behind and last commit time, using `git --no-optional-locks`;
  - `gh pr view --json number,state,reviewDecision,statusCheckRollup`, rate-limited with backoff;
  - the draupnir group;
  - `projects[].build` per §4.7: the actions-runs call with ETag and budget, folded by the Python port of
    `_BUILDS_FOLD_JQ`.

  Also per §4.7:
  - the Claude collector's transcript tail accumulates `sessions[].tokens` with the Python port of `_COSTS_JQ`;
  - the `usage` section from statusline `rate_limits` and the CodexBar CLI;
  - `docs/api/lidskjalv-mapping.md` gains the builds, costs and quota rows.

  Lidskjalv keeps its own builds, costs and quota paths; WP5.5 adds the switches.
- Tests: contract; git, gh and CodexBar output fixtures; gh not authenticated gives `unavailable` with an
  `error_kind`; freshness transitions with a fake clock; the call budget is never exceeded; **parity goldens** for
  the build fold and the token sums (§4.7); the mapping test covers the new paths.
- Done when: the contract, parity and mapping tests pass; `/now` shows `build` for every known project with a
  GitHub remote and `tokens` for every live Claude session; `usage` is filled, or absent with the reason in
  `collectors[]`; and a 24 h live run stays inside the gh call budget.

**WP5.5 Lidskjalv builds, costs and quota from Fylgja, switchable (M; Lidskjalv repo)** (A24)
- Research: the Lidskjalv survey. Builds come from `lib/builds.sh` and `plugins/lidskjalv_build_open.sh`, costs from
  `lib/costs.sh`, and quota from `plugins/lidskjalv_quota_*.sh` through showy-quota.
- Precondition: WP5.4 is done, so the §4.7 payloads, their mapping rows and the parity goldens are committed.
- Revalidate: where Lidskjalv's cost and quota numbers come from today (quota through showy-quota, not CodexBar),
  so the parity check compares like with like, and which showy-quota providers Fylgja's §4.7 sources cover.
- Build `LIDSKJALV_SOURCE_BUILDS`, `LIDSKJALV_SOURCE_COSTS` and `LIDSKJALV_SOURCE_QUOTA`, each
  `lidskjalv|fylgja`, defaulting to `lidskjalv`. In `fylgja` mode the module reads the §4.7 paths of `/now`
  (`projects[].build`, `sessions[].tokens`, `usage.providers[]`) through the mapping, treats `stale` and
  `unavailable` like a failed fetch, and falls back to its own path when Fylgja is unreachable. A quota provider
  Fylgja does not cover keeps the `lidskjalv` source. Lidskjalv's own implementations stay as they are.
- Tests: BATS for both sources and the fallback, plus a parity script that compares both sources over 3 days.
- Done when: each switch can be flipped either way without visible regressions, and the parity report is committed.
  Changing the defaults is a separate decision.

### Phase 6: next

**WP6.1 Waiting-on-me (S)**
- Build: merge `waiting_permission`/`waiting_input` (Claude sessions, and norn runs through WP3.3), unseen
  `turn_done`, a failed build (`projects[].build.state = fail`, §4.7) or a review request on a project touched
  today, and A15 unread markers into `waiting[]` in `/now`. Each entry carries its kind, target, reason and since.
- Tests: fixtures for each source; one target counted once when it qualifies twice (waiting and unseen); ordering
  by the `PLAN.md` §7 rank and then age; `turn_done` leaving `waiting[]` when it becomes `seen`.
- Done when: those tests pass, and a live check shows a Claude permission prompt and a norn run waiting for input
  each appear in `waiting[]` within 2 s, and the finished turn leaves it within 2 s of its window gaining focus.

**WP6.2 Ranking (M)**
- Build:
  - one score per candidate, following the order in `PLAN.md` §7;
  - each candidate gets a reason string in words;
  - ties are broken by age;
  - `/next` and `fylgja next`; every response carries a `list_id` and a `suggestion_id` per item;
  - every generated list goes to `suggestions_log`;
  - `POST /next/feedback` (§4.2), and `fylgja next --feedback good|bad`, which posts it for the most recent list the
    CLI showed today and so records whether the top item was right. The daemon writes the feedback; the CLI never
    touches SQLite (A7).
- Tests: golden ranking fixtures for each rule and for combinations; feedback recorded against the right list and
  item; feedback for an unknown `list_id` gives 404; auth on the feedback endpoint.
- Done when: the ranking goldens pass for every rule in `PLAN.md` §7 and for combinations, every `/next` response
  is logged with its reasons, and an integration test runs `fylgja next` then `fylgja next --feedback good` and
  finds the rating on that list's top item in `suggestions_log`. This is the entry gate for WP6.3.

**WP6.3 Evaluation (2 weeks of observation)**
- Done when (**`PLAN.md` Phase 6** "the top suggestion is right most mornings"): the first `fylgja next` of the day
  is rated good on at least 7 of 10 workdays.

### Phase 7: navigator page

**WP7.1 Navigator (M)**
- Build: a single static HTML file with plain JS and no build step. It shows one card per non-empty workspace
  (label, projects, sessions with state, top context items and todos) and a "next" strip at the top. It updates
  live over SSE, and clicking a card calls the focus API.
- Tests: an API contract test for the data the page uses, and a Playwright-free smoke check that `/` renders and
  contains the expected element ids.
- Done when (**`PLAN.md` Phase 7**): first paint takes < 1 s with live data, and 5 consecutive workdays pass without
  manual workspace hunting (self-reported).

### Phase 8: narrative

**WP8.1 Summary service, session and workspace summaries (M)**
- Research: Lidskjalv `_story_one` (`bin/lidskjalv.sh`, 898–1016: the prompt, the facts block, the TTL of 300 s,
  skipping when the story is newer than the transcript, at most 10 concurrent jobs).
- Revalidate: the `claude -p` flags used below (`--model haiku`, `--strict-mcp-config`, output format) on the
  installed Claude version, and the `_story_one` line range against the current Lidskjalv checkout.
- Build:
  - **summary service in the daemon** (`core/summaries.py`). Model calls run only here, never in the CLI and never
    on a hook path, and results are written through the single writer. It is a job queue keyed by
    `(scope_kind, scope_ref)`: a second request for a pending key joins the existing job, and at most
    `[summaries] max_concurrent` jobs run at once;
  - **the runner:** `claude -p --model haiku --strict-mcp-config`, run in the summarizer cwd with `FYLGJA_SUMMARY=1`
    and `LIDSKJALV_TRACK_SUMMARY=1` set, with a 60 s timeout. The Fylgja hook exits immediately when
    `FYLGJA_SUMMARY` is set;
  - **the API** from §4.2: `GET` and `POST /summaries/{scope}/{ref}`, the `summary.ready` SSE event, and the cached
    `story` in `/now` (`sessions[].story = {arc, now, generated_at, state}` and `workspaces[].story = {text,
    generated_at, state}`, present only when cached);
  - **inputs, per scope:**
    - session: the facts block and the one-liner (§4.6), producing ARC and NOW as in `_story_one`;
    - workspace (`PLAN.md` §12 Phase 8 "per workspace"): the effective label, the workspace one-liner, its
      projects, its sessions' states and one-liners, the top context items and the open todos, producing one
      short paragraph;
  - **cache semantics:** a digest over the normalized inputs is stored as `input_digest`. A request whose digest
    matches the cached row returns it as `fresh`, with no model call. A changed digest regenerates, but at most once
    per `ttl_seconds` per key (300 s); inside the TTL the cached row is returned as `stale`. A session whose cached
    story is newer than its transcript is never regenerated;
  - **CLI:** `fylgja story session <key>|ws <n>` posts the request and waits for `summary.ready`, up to 60 s;
  - off by default (`[summaries] enabled = false` gives 409 `disabled`).
- Tests: a fake `claude` runner; TTL, digest and skip gates; joined concurrent requests (one runner call); the
  concurrency cap; timeout gives `failed` with an `error_kind`; the recursion guard (no summarizer session appears
  in `/now`); auth on the `POST`; 409 while disabled; the CLI waiting on SSE.
- Done when: those tests pass; with summaries disabled no `claude` process is ever spawned (asserted with the fake
  runner); and a live run with summaries enabled produces ARC/NOW for three live sessions and a summary for two
  workspaces, served from cache on the second request.

**WP8.2 Daily narrative (S)**
- Build: the `day` scope of the WP8.1 service. Its inputs are the day's `/history` totals and ordered spans, the
  sessions active that day with their one-liners, and the todos created or finished that day. A past day's
  narrative is regenerated only when its digest changes, for example after a late import. `fylgja history --story
  [--date YYYY-MM-DD]` posts `POST /summaries/day/{date}`, waits for the result and prints it.
- Tests: input assembly for a recorded day fixture; caching of a past day; the CLI path with the fake runner.
- Done when: one command, `fylgja history --story`, tells the story of the day, and a second run on the same day
  with no new activity returns the cached text without a model call.

**WP8.3 Lidskjalv stories from Fylgja, switchable (S; Lidskjalv repo)** (A24)
- Revalidate: the panel's story read site and `_story_one`'s output shape in the current Lidskjalv checkout.
- Build `LIDSKJALV_SOURCE_STORIES=lidskjalv|fylgja`, defaulting to `lidskjalv`. In `fylgja` mode the panel reads
  `sessions[].story` from `/now` and, when it is missing or stale, sends `POST /summaries/session/{key}` with the
  bearer secret instead of running `_story_one`. It falls back to `_story_one` when Fylgja is unreachable or
  summaries are disabled (409). Lidskjalv's own story generation remains available.
- Tests: BATS for both sources, the 409 path and the unreachable fallback, with a stub `curl`.
- Done when: those BATS pass, the full suite stays green, and in `fylgja` mode the panel shows Fylgja's ARC/NOW for
  a live session while switching back to `lidskjalv` restores `_story_one` output.

### Deferred: Teams depth (`PLAN.md` §13.1)

This is not scheduled. Its decision deadline arrives after WP5.3, WP5.2 and WP6.1 are done. The spike checklist and
the ship gates in `PLAN.md` §13.1 apply unchanged. Nothing in Phases 0–8 may depend on it.

---

## 6. Test plan summary

| Layer | Where | Built in |
|---|---|---|
| Unit: parsers, reducers, config, dedup, span algebra, ranking | `tests/unit` | every WP |
| Contract: five shared checks per built-in collector (aerospace, presence, claude, codex, opencode, norn, appcontext, project). `tests/contract/test_builtin_coverage.py` lists the built-in entry points and fails when one has no contract run | `tests/contract` via `fylgja.testing` | WP1.5, then each plugin WP |
| Integration: registry with synthetic entry points (including the no-import-before-opt-in check), supervisor isolation, migrations, API over ASGI | `tests/integration` | WP1.2–1.9, WP3.4 |
| One-liners, Phase 3 acceptance | `tests/unit/test_one_liner.py`, `tests/integration/test_phase3_agents.py` | WP1.7, 3.1–3.3, 3.6 |
| Summary service with a fake `claude` runner | `tests/integration/test_summaries_*.py` | WP8.1, 8.2 |
| Hook ingress (no daemon) | `tests/integration/test_hook_*.py` | WP2.1–2.2 |
| Messaging over fake sockets | `tests/integration/test_message_*.py` | WP3.6 |
| Fixture hygiene and token canary | `tests/unit/test_fixture_hygiene.py`, `test_no_token_leak.py` | WP1.5, WP2.1, WP3.6 |
| Live (`-m live`, opt-in, read-only on live agent data) | `tests/live` | WP1.6, 2.7, 3.5, 3.6, 5.1 |
| Lidskjalv BATS (in the Lidskjalv repo) | `bats/` | WP1.11, 2.6, 2.8, 5.5, 8.3 |
| Lidskjalv parity goldens (build fold, token sums, facts), generated once by dev-only scripts | `tests/fixtures/lidskjalv/` | WP1.7, 5.4 |
| Decision log in step with §2 | `tests/unit/test_decision_log.py` | WP0.2 |
| norn event-subscriber and Fylgja subscriber tests (in the norn repo) | norn `tests/` | WP3.3 |
| Installer tests against a copy of the dotfiles tree (A22) | `tests/integration/test_install_*.py` | WP1.10, 2.4, 4.4 |
| Independence suite: everything above, in a home directory with no Lidskjalv (A21) | CI job | WP2.4, 2.8 |

Generated data goes to `target/tests/` and is removed after the tests. Coverage is measured on branches, with a CI
gate of 85 % and direct tests for every contract and failure path. The gate is raised as the code stabilizes.

## 7. CI

- **GitHub Actions** on the private `erik-romson/fylgja`, triggered by pull requests to `develop` and pushes to
  `develop`:
  - a Linux job and a macOS job each run the six commands in `PLAN.md` §10.5;
  - the macOS job also runs the `slow` integration marks (launchd plist rendering, subprocess signal and kqueue
    tests);
  - `astral-sh/setup-uv` is pinned to the same uv minor version as WP0.2.
- **No AeroSpace or agent CLI in CI**, and `live` is never selected.
- **A separate workflow in the Lidskjalv repo** runs BATS for WP1.11, WP2.6, WP2.8, WP5.5 and WP8.3, and **norn's own test
  suite** covers its part of WP3.3. Both check the shared envelope against Fylgja's golden fixture.

## 8. Risks added by this plan (on top of `PLAN.md` §14)

| Risk | Mitigation |
|---|---|
| Lidskjalv's hooks are broken today (R1), which hides dual-run differences | Erik repairs them before Phase 2 (Q1); a Phase 2 entry gate; doctor flags dangling hook commands |
| cmux is now the main host (Q4), and cmux may change its env vars or CLI | The WP0.4 spike before WP1.8; A3 captures only ids; the cmux adapter detects the cmux version; window-level focus is the fallback |
| The cmux CLI may not work from a launchd agent without the capability token that Fylgja must not read | WP0.4 checks this first; joins then rely on hook-time ids, and focus needs a documented alternative before WP3.5 |
| Fylgja quietly gains a dependency on Lidskjalv (a shared file, a check or a test input), so Lidskjalv can no longer be removed cleanly (Q9) | A21 forbids it. The CI independence suite runs without Lidskjalv, the Lidskjalv doctor checks are informational only, and the removal runbook is dry-run in WP2.8 |
| Dotfiles are shared with machines that do not have Fylgja (A22) | Every registered command is guarded and exits 0 silently; machine-specific files (plist, secret) stay out of dotfiles |
| The norn subscriber slows or breaks a norn run | Subscribers are isolated (warn and continue), the Fylgja subscriber is stdlib-only and does one atomic file write per event, and norn's tests cover a raising and a slow subscriber |
| A Python spawn per hook event costs too much with about 11 sessions | The WP2.1 budget gate; drop PreToolUse by default if the budget is missed; the statusline is coalesced (A4) |
| TCC attributes Automation to the uv-managed Python, and the grant breaks when Python is upgraded | The doctor check; the 3 s timeout turns into degraded state; re-grant steps are documented |
| norn's file formats are unversioned | The norn plugin (A10) is the primary source and carries a versioned envelope. The file scan keeps tolerant parsers, fixtures and the `stale` state |
| Messaging is held by default for bypass sessions, so its value is low (R10) | Accepted (Q7): an explicit, documented behaviour; focus-and-copy remains the fallback |
| The ratatosk fixtures are untracked and could be lost | Staged in WP0.1, sanitized and committed in WP1.5 |
| Fylgja's quota sources (statusline, CodexBar) may not cover every provider showy-quota shows Lidskjalv | §4.7 marks quota parity per provider; WP5.5 keeps the `lidskjalv` source for any provider Fylgja cannot supply |

## 9. Traceability

| `PLAN.md` section | Work packages |
|---|---|
| §1 Purpose (five questions) | (1) where am I: WP1.6, 5.2; (2) agent sessions, "in one sentence": WP1.7, 2.3, 3.1–3.3, 3.6 (§4.6); (3) workspaces and todos: WP1.8, 4.1–4.3; (4) waiting on me: WP2.3, 3.3, 6.1; (5) what next: WP6.2 |
| §2 Where it sits | WP0.1, 0.4, 1.11, 2.4, 2.6, 2.8, 3.3, 5.4, 5.5, 8.3 (A21, A22, A24; §4.7) |
| §3 Core model and joins | WP0.3, 0.4, 1.1, 1.8, 5.2 (A1, A11) |
| §4.1 Plugin boundary | WP1.1–1.5 (external plugins imported only when enabled: WP1.2) |
| §4.2 AeroSpace | WP1.6, 5.1 (A17) |
| §4.3 Claude Code | WP1.7, 2.1–2.4, 2.8 (A2–A5, A13, A21, A22) |
| §4.3.1 Local messaging | WP3.4, 3.6 (A9, A14) |
| §4.4–4.6 Codex, OpenCode, norn | WP3.1–3.3 (A10; for norn, a plugin in the norn repo plus a Fylgja collector) |
| §4.7 Application context | WP5.1, 5.3 (A15, A17) |
| §4.8 Project state | WP5.4 (§4.7) |
| §4.9 Collector tests | WP1.5 plus each plugin WP; §6 |
| §5 Store | WP1.4 (A12, A20) |
| §6 API and CLI | WP1.9, 1.10, 2.4, 2.5, 3.4–3.6, 4.1–4.2, 4.4, 6.2, 8.1–8.2 (A6–A8, A22) |
| §7 Summaries and next | WP1.8, 4.2, 6.1–6.3, 8.1–8.2 (§4.6 one-liners; workspace summaries in WP8.1) |
| §8 Todos | WP4.1–4.4 (A18, A19); Obsidian import on hold (A23) |
| §9 Front ends | WP1.11, 2.6, 2.8, 4.4, 5.5, 7.1, 8.3 (A24) |
| §10 Stack, layout, tools, tests, CI | WP0.2, §6, §7 |
| §11 Permissions and privacy | §4.4, WP1.4, 1.10, 5.3 |
| §12 Phases | §5 (Phase 1 "Lidskjalv can render from `/now`": WP1.11; Phase 3 "a sentence": §4.6 and WP3.6; Phase 8 "per session, workspace and day": WP8.1–8.2) |
| §13 Open decisions | Deferred Teams depth |
| §14 Risks | §8 plus the WP-level mitigations |

## 10. Answers

Erik's answers from all three rounds (2026-10-05), and where each is applied. No questions remain open.

| Question | Answer | Applied in |
|---|---|---|
| Q1 Lidskjalv link and checkout | Yes. Erik repairs the link before Phase 2, and `~/projects/erik-romson/lidskjalv/main` is canonical. | R1, WP0.1, Phase 2 entry gate, §8 |
| Q2 End of the dual run | Thin forwarder, for the time being. Q9 later narrows this: Lidskjalv gets thin, but Fylgja does not take its events from Lidskjalv's hook (confirmed in Q11). | A21, WP2.8 |
| Q3 Scope of the Lidskjalv switch | Fylgja will eventually replace builds, costs, quota and stories, but both must work for the time being | A24, WP2.6, WP5.4, WP5.5, WP8.3 |
| Q4 cmux | cmux is the focus | A1, WP0.3, WP0.4, WP1.8, WP3.5, WP5.3, §8 |
| Q5 Dotfiles | Changes need to be persisted with `~/dotfiles` | A8, A22, WP1.10, WP2.4, WP4.4, §8 |
| Q6 norn changes | A norn plugin, if it improves the overview. It does: exact liveness, the live stage, and waiting-for-input. | A10, WP3.3, §8 |
| Q7 Messaging | As recommended | WP3.6, §8 |
| Q8 Obsidian mapping | Put on hold and added to the todo work | A23, WP4.3 ("On hold") |
| Q9 Reminder injection and Lidskjalv independence | Keep them independent, so Lidskjalv can be removed without leaving any dependency behind | A18, A21 (rewritten), WP1.7 goldens, WP2.4 doctor and tests, WP2.8 (rewritten), §6, §8 |
| Q10 Committing dotfiles changes | As recommended: write into the dotfiles working tree, never commit, and warn while changes are uncommitted | A22, WP2.4 |
| Q11 What "thin forwarder" means, given Q9 | Yes, as recommended. Fylgja keeps its own hooks permanently, and Lidskjalv's hook never forwards. "Thin" applies to Lidskjalv's side only. | A21, WP2.4, WP2.8 |
| Q12 Reminders when Lidskjalv is removed | Yes; Lidskjalv will eventually use Fylgja. Reminder injection stays in Lidskjalv's own hook and goes away with Lidskjalv. Fylgja adds no synchronous hook, and its session todos are the replacement. Lidskjalv may later store its reminders in Fylgja (not scheduled). | A18, A24 |
