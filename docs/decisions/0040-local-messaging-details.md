# 0040: Local Claude messaging, details

- Status: accepted
- Amendment: none
- Amends: —
- Answers: Q7
- Relates to: A6, A9, A14
- Date: 2026-10-06

## Context

WP3.6 builds the opt-in message action of `PLAN.md` §4.3.1 on top of spike 0004, whose verdict is `no-go`: the
official documentation specifies the inbox socket, the optional auth line, the "complete line within 30 s" rule and the
inbound policy, but not the user-message frame, and no `queue-operation` enqueue record carries a sender id. The plan
leaves open how each receipt state is reached, what the API answers while messaging is off, how text limits behave,
where the registry is read, and how doctor learns a session's settings and permission mode without reading another
process's environment.

## Decision

- **Encoder.** `messaging/claude/encoders/v1.py` ships and supports no version (`SUPPORTED = ()`, `SPIKE_VERDICT =
  "no-go"`); a test keeps it in step with the spike note's `Verdict:` line. Every send to a live Claude session therefore
  ends `unsupported` (`no_supported_frame`) before any connect, and focus-and-copy is the action. Filling the encoder
  later needs the by-hand capture, protocol fixtures for 2.1.224 and the current versions, and the verdict set to `go`.
- **Receipt states**, each from one condition only, `bytes` being what the kernel accepted:
  - `submitted`: every byte of the one-line frame was written within the deadline (not "delivered");
  - `observed-enqueued`: reserved and unreachable (`OBSERVED_ENQUEUED_SUPPORTED = False`; `observe_enqueued` raises);
  - `unsupported`: no adapter for the session kind, version older than 2.1.224 or unparsable, `peerProtocol` not in
    `{1}`, or no encoder frame;
  - `unreachable`: no row, a dead pid (stale row), no inbox socket, a missing socket or socket directory, a refused
    connection, or a peer that closed before the first byte;
  - `refused-locally`: an A14 rule failed, a reused pid, a row without `procStart`, two live rows for one session, an
    unverifiable liveness check, empty text, text over `max_chars`, control characters, a malformed session key, a
    local error before any byte left, or a cancellation before any byte left;
  - `timed-out`: the 2 s connect-and-write deadline passed (`connect_timeout` or `write_timeout`);
  - `unknown`: the peer failed, or the send was cancelled, after part of the line was written. Never retried.
- **Text.** Plain text of at most `[actions.message] max_chars` characters; longer text is refused, never cut. C0
  control characters other than newline and tab, DEL and lone surrogates are refused (no terminal escapes into the
  target's screen). The body is the provenance label `[Fylgja message <uuid4>: sent from this Mac by Fylgja, not by a
  Claude session]`, a newline, then the text. No auth line is ever sent.
- **Resolution.** The registry is `[collectors.claude] registry_dir` or `~/.claude/sessions`, read at send time with the
  collector's strict parser and only A13 names. Liveness (pid alive, start time equal to `procStart` within 1 s) is
  checked at resolution and again in `confirm()` immediately before connecting, together with a fresh read of the row
  and the A14 rules. The A14 roots are compared as exact normalized strings, so `/tmp/cc-socks*` is only touched for a
  path that names it; tests inject their own `/tmp/fy-XXXX` roots.
- **API.** `POST /sessions/{key}/message` answers **409** while `enabled = false` (403 is the mutation guard's Origin
  answer, so a disabled route that answered 403 would look like a guard rejection), **400** for a body that is not JSON
  or holds any field whose name contains `sock`, `path`, `address` or `peer`, **422** for anything but
  `{"text": "<string>"}`, and otherwise **200** with the receipt plus a plain-language `detail`. The text is never echoed.
- **CLI.** `fylgja session message <id> "text"` takes a key, a native id or a unique prefix (resolved through
  `GET /sessions`), asks `[y/N]` unless `--yes`, exits 0 only for `submitted`, 1 otherwise and 3 when the daemon is down.
- **Doctor.** One `messaging` summary, a `messaging:registry` check (the directory is yours and not group- or
  world-writable) and one `messaging:<pid>` line per live session: A9 compatibility, A14 safety (a refused socket is a
  warning) and the effective inbound policy in one sentence. Sources: `/Library/Application Support/ClaudeCode/
  managed-settings.json`, the session's `--settings` value, `~/.claude/settings.json`, and `<cwd>/.claude/settings.json`
  and `settings.local.json`; server-managed and MDM policy and the per-session dialog-timeout variable are not visible
  and the line says so when a source cannot be read. The precedence follows the settings reference, including that an
  unrecognized value holds (managed: refuses) from 2.1.248 even under a trusted `accept`. The start permission mode comes
  from `--dangerously-skip-permissions` or `--permission-mode` in `ps -o pid=,args=`, else `permissions.defaultMode`.
  That `ps` line can carry other tools' secrets, so only those flags and the `--settings` value are parsed and nothing of
  it is shown, logged or stored. Doctor never connects to a socket.

## Consequences

- Until a frame is captured, enabling messaging changes nothing a target sees: doctor warns that every send reports
  `unsupported`, and the receipt says to focus and paste.
- The transport, receipts and gates are exercised in tests with a test-only encoder over fake sockets, so filling `v1`
  later only adds the frame and its fixtures.
- A disabled action records no receipt; every attempted message records exactly one, with no text body.
