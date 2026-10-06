# Spike 0004: local messaging frame, enqueue record and policy (WP3.6 spike, read-only part)

- Date: 2026-10-06
- Versions: installed Claude Code 2.1.291. The live registry rows hold 2.1.284 (6 rows), 2.1.289 (7), 2.1.290 (1) and
  2.1.291 (1), all with `peerProtocol` 1. The step targets 2.1.284 and 2.1.289 (A9 gates per row, not per CLI).
- Companion file: `docs/revalidation/2026-10-06-messaging.md` (one row per claim, with the `unknown` rows and their
  by-hand commands).
- Method: read-only. Two `curl` fetches of documentation, one `claude --version` and a structural read of existing
  transcripts. No `claude` session was started, no socket was opened, no `*.key` file was read, no setting changed. Raw
  copies of the pages stay under the gitignored `target/fixture-staging/msg/`. This note holds field names, counts and
  versions, and no message content, names, paths, ids or tokens.

## 1. Frame

Sources, fetched 2026-10-06 with `curl`:

- Anthropic, "Message your other Claude Code sessions"
  (`https://code.claude.com/docs/en/cross-session-messaging`, markdown form, 200 OK), and the settings reference
  (`https://code.claude.com/docs/en/settings-reference`) for `crossSessionInbound` and `dialogExpiry`.
- Blake Crosley, "Cross-Session Messaging in Claude Code" (`https://blakecrosley.com/blog/claude-code-cross-session-messaging`,
  200 OK; published 2026-08-08, updated 2026-08-25, verified there against Claude Code 2.1.246).

What the **official documentation** specifies:

- Requires Claude Code 2.1.224 or later on macOS and Linux (2.1.234 on native Windows; the survey says 2.1.239 from the
  release notes). A session binds an inbox socket when messaging is enabled; bare mode binds none.
- The socket is a Unix domain socket on macOS and Linux, mode restricted to the OS user. If the default directory is not
  acceptable (for example owned by another user) Claude Code uses `/tmp/cc-socks-<uid>` (A14). The path is shown in
  `/status` (`Peer address`) and in the registry row, and exported to hooks and Bash commands as
  `CLAUDE_CODE_MESSAGING_SOCKET`.
- A per-session `CLAUDE_CODE_MESSAGING_TOKEN` is exported as well. A script posting **to its own session** may send the
  first line `{"type":"auth","token":"<token>"}`. On macOS and Linux the line is optional; on native Windows it is
  required.
- "Claude Code closes a connection that hasn't sent a complete line within 30 seconds." The transport is therefore
  line-oriented, and the docs call the unit "a complete line".
- Every message passes the inbound controls (section 3). Own-child verification is by process evidence (macOS: only while
  the posting process still runs) or by the token in the auth line.

What the official documentation does **not** specify: the JSON object that carries a user message (its `type` value,
the text field, the sender fields, any message id, the acknowledgement or error reply). The only JSON object shown is
the auth line. The page says "complete line" and nothing else about the body. The survey describes the feature through
the `ListAgents` and `SendMessage` tools, the registry listing and the traps, and shows no wire frame either, so it
does not fill the gap. `research/03-agent-sessions.md` item 9 says the same: socket, auth line and trust behaviour are
documented, a stable schema reference for the complete user-message frame is not.

**Verdict on this question: the official documentation does not specify the user-message frame** (re-checked on
2026-10-06 against the pages above; this matches R10, "The frame schema is still undocumented"). The frame accepted by
2.1.284 and 2.1.289 is therefore `unknown` until the by-hand capture (section 4) is run.

## 2. Enqueue record (`queue-operation`)

Read-only structural scan of the 400 most recently modified transcripts under `~/.claude/projects/` (340 held at least
one such record; 1,608 records in all). Only key names and value classes were read; no content was copied.

| Operation | Records | Keys | Notes |
|---|---:|---|---|
| `enqueue` | 804 | `type`, `operation`, `sessionId`, `timestamp`, `content` | `content` is a string: 540 plain text, 264 starting with a `<task-notification>` element |
| `dequeue` | 655 | `type`, `operation`, `sessionId`, `timestamp` | no `content` |
| `remove` | 149 | `type`, `operation`, `sessionId`, `timestamp`, `content`, `reason`; 12 also `commandUuid`, `deliveryId` | `reason` seen: `absorbed_mid_turn` (145), `delivered_to_agent` (4) |

Findings:

- **No `enqueue` record has any id-like key**, and none has a sender field, so no field of an `enqueue` record can
  carry a sender-supplied message id. The only id-shaped keys are `commandUuid` and `deliveryId` (both 36-character
  UUID strings), and they appear on 12 `remove` records, all for task notifications that Claude Code itself generates.
  They are not sender-supplied and they are not on the enqueue record.
- A sender-supplied id could still survive inside the `content` string of an enqueue record. The scan found no enqueue
  content that is a peer message, so how a socket message is rendered there is `unknown` (no peer message was in the
  scanned transcripts, or none was recognisable without reading content).
- Consequence for WP3.6: `observed-enqueued` stays **unconfirmed**. It may only be built if the by-hand capture shows a
  generated message id (or a nonce in the text) reappearing in an `enqueue` record's `content`, and then it would match
  on that content marker, not on a record field. Until then step 31 produces no `observed-enqueued` receipt.

## 3. Policy (`crossSessionInbound`, `dialogExpiry`)

Documented behaviour (sources in section 1), for a message arriving on the socket:

| Setting | Behaviour |
|---|---|
| `accept` | Claude Code delivers each message to Claude (limit: at most 50 accepted messages queued for Claude to read; per-sender rate limiting; identical repeats within a short window are dropped) |
| `hold` | Claude Code shows a notice and does not deliver. An explicit `hold` never expires; a later `accept` releases the held messages |
| `refuse` | Claude Code drops each message without delivering it. A refusing session looks no different in `/status` or in other sessions' listings, so a sender cannot tell |
| unset (default rule) | Decided per message from the permission-mode classes. Receiver prompts for permissions: delivered, unless the sender identifies as bypassing, in which case held. **Receiver bypasses permissions: held for approval; delivered only when the sender identifies as also bypassing** |

- The default hold opens an approval dialog in an interactive terminal session (Approve delivers one message, Deny or
  dismiss drops it). If unanswered past `dialogExpiry`, the dialog closes and the message is **dropped**. `dialogExpiry`
  takes `"60s"`, `"5m"`, `"10m"` or `"never"`, scope user or managed, default `"5m"`.
- At most 100 messages are held; beyond that the oldest is dropped.
- A Fylgja message has no auth line (A9/§4.3.1) and no permission class, so it is "any other message that asserts no
  permission class": **in Erik's bypass-permissions sessions every Fylgja message is held behind the approval dialog
  and dropped after 5 minutes unless Erik approves** (R10; Q7 accepted this). With `-p` sessions the message is kept
  for the same deadline and reported expired to a sender "it can reach", which Fylgja is not.
- Precedence: managed settings, then `--settings`, then user settings, first value wins; `refuse` > `hold` > `accept`;
  a project or local `hold` or `refuse` applies when no trusted source sets a value. An unrecognised value holds.
- Erik's user settings set no `crossSessionInbound` (R10, checked in the baseline log).

**Observed behaviour under explicit `accept`, `hold`, `refuse` and under the default rule with a bypass receiver:
`unknown`.** It needs two live sessions and is in section 4.

## 4. By-hand capture (for Erik; never run unattended)

Goal: find the frame a session emits and accepts, whether the enqueue record shows a message marker, and the policy
behaviour. **Use only disposable sessions in temporary directories. Never put the proxy at a live session's socket
path, and never use a live session as sender or target.** The proxy script is not committed; paste it where shown.

1. Prepare directories and three settings files (the key is the only setting; no other is changed):

   ```sh
   W=$(mktemp -d /tmp/fylgja-spike.XXXXXX); mkdir "$W/target" "$W/sender"
   for p in accept hold refuse; do printf '{"crossSessionInbound":"%s"}\n' "$p" > "$W/$p.json"; done
   claude --version
   ```

2. Start the **target** in a terminal of its own, first under `accept` (repeat the whole procedure with `hold`,
   `refuse`, and with `--dangerously-skip-permissions` and no `crossSessionInbound` for the default rule):

   ```sh
   cd "$W/target" && claude --settings "$W/accept.json"
   ```

   Give it a distinct name with `/rename spike-target`. In a second shell of your own find its row and socket path
   (field `messagingSocketPath`; the row is selected by `cwd`, not by name):

   ```sh
   T=$(jq -r --arg d "$W/target" 'select(.cwd==$d) | .messagingSocketPath' ~/.claude/sessions/[0-9]*.json)
   jq -c 'select(.cwd=="'"$W"'/target") | {pid,version,peerProtocol,status}' ~/.claude/sessions/[0-9]*.json
   ```

3. Put a logging proxy at the **disposable target's** path only. Rename the bound socket aside (the listener keeps
   working under the new name) and listen at the old path, forwarding each connection byte for byte and logging it
   hex-and-text to `"$W/frames.log"`:

   ```sh
   test "$(dirname "$T")" = /tmp/cc-socks -o "$(dirname "$T")" = "/tmp/cc-socks-$(id -u)"   # A14 shape
   case "$T" in "$W"/*|/tmp/cc-socks*/*) ;; *) echo "refuse: not a disposable target"; exit 1;; esac
   mv "$T" "$T.real"
   python3 -I "$W/proxy.py" "$T" "$T.real" "$W/frames.log" &
   ```

   `proxy.py` (about 30 lines, stdlib `socket` and `threading`): `bind` a listening `AF_UNIX` socket at argv[1] with
   mode 0600, `accept` in a loop, per connection open `AF_UNIX` to argv[2], run two threads that copy each direction,
   append every chunk to argv[3] as `<monotonic-seconds> c2s|s2c <hex> <repr>`, and close both ends on EOF. The sender
   is the only client; no token is involved (auth line absent).

4. Start the **sender** in another terminal, with `crossSessionInbound` irrelevant to it:

   ```sh
   cd "$W/sender" && claude --settings "$W/accept.json"
   ```

   Ask it, in plain words: "List your agents, then send the exact text `FYLGJA-SPIKE-NONCE-1` to `spike-target`."
   The sender's `SendMessage` goes through the proxy. Repeat with an auth-less `printf` replay once the frame is known
   (step 6).

5. Record, from `"$W/frames.log"` and the two sessions:

   - the number of connections and lines per message, the line terminator, and whether an auth line was sent;
   - the JSON of each client-to-server line with the nonce replaced by `<nonce>`: key names, `type` value, value types,
     and any generated id, sender, permission-class or version field;
   - the server-to-client bytes: acknowledgement, error and close behaviour, and for `hold` and `refuse` whether the
     sender is told anything;
   - for each policy, what the target shows (delivered, notice, nothing, approval dialog), and the dialog expiry time;
   - the sender's `/status` `Peer address` row and the target's version.

6. **Replay and policy checks** (disposable target only). With the frame from step 5 written as one line to a file
   `"$W/frame.line"` (nonce `FYLGJA-SPIKE-NONCE-2`), send it without an auth line and without the proxy:

   ```sh
   python3 -I -c 'import socket,sys; s=socket.socket(socket.AF_UNIX); s.settimeout(2); s.connect(sys.argv[1]); s.sendall(open(sys.argv[2],"rb").read()); print(s.recv(4096))' "$T.real" "$W/frame.line"
   ```

   Do this under `accept`, `hold`, `refuse` and under the default rule with the target started with
   `--dangerously-skip-permissions`; note delivered, noticed, dropped, or dialog-then-drop after 5 min. Also test one
   line sent in two writes 35 s apart (expect the 30 s close) and one over-long line.

7. **Enqueue record.** After each delivered message find the target's transcript and look at the `queue-operation`
   records near the time (structural check only; do not copy content):

   ```sh
   f=$(ls -t ~/.claude/projects/*"$(basename "$W")"*target*/*.jsonl | head -1)
   jq -c 'select(.type=="queue-operation") | {operation, keys: keys, has_nonce: ((.content // "") | contains("FYLGJA-SPIKE-NONCE"))}' "$f"
   ```

   Record whether the nonce or any generated id appears in an `enqueue` record's `content`, and in which wrapper
   element, so `observed-enqueued` can be decided.

8. Clean up: quit both sessions, `kill %1`, `rm -rf "$W"`. Commit only a sanitized frame (nonce, ids and names as
   placeholders) under `tests/fixtures/` and update this note's verdict.

## 5. Verdict

The official documentation specifies the socket, the optional auth line, the line framing limit (a complete line
within 30 s) and the inbound policy, but **not** the user-message frame, and the survey does not either. By the
authority order (official documentation above local behaviour) and the step's rule, `go` needs the official docs to
specify the frame, so the verdict is no-go, pending the by-hand capture in section 4.

Consequences for step 31 (WP3.6 build): the encoder adapter (`messaging/claude/encoders/v1.py`) stays empty; sending
reports `unsupported`; no receipt is `observed-enqueued`; resolution, A14 path checks, A9 gates, `max_chars`, the
size and 2 s deadline rules, doctor's explanation ("messages to this session will be held for approval") and
focus-and-copy as the action are built as planned. If Erik's capture finds the frame, the encoder can be filled in by a
later change that sets the verdict to `go` and adds the sanitized fixture.

Verdict: no-go
