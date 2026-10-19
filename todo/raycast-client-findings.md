# Findings from the Raycast client

Daemon-side issues found through the Raycast extension (`~/projects/erik-romson/fylgja-raycast/main`), from its live
contract check against the daemon and from using the extension, both on 2026-10-19. Nothing here blocks the extension.

- [x] Every live session lacked a process chain (`no process chain recorded for the session`, decision 0029), so no
      session could be focused: `POST /focus/session` answered 409 "session has no window". Cause: the resolver
      pruned every chain on any collector's complete snapshot, so the Claude, Codex and OpenCode snapshots wiped each
      other's chains until the next emission. It now prunes only the sending collector's.
- [x] `go` and `go_agent` point at sessions without a window, so a client's first Go on such an item always fails.
      Go now skips a session without a window (decision 0060).
- [x] 17 pairs of app items share a name (a bundle-id key against a name key). Spans of a window focused before the
      window list showed it had no bundle id; the span now takes it from the list, and migration 0005 repairs the
      stored ones (decision 0061).
- [ ] `summary` is empty on most app items, so the Raycast subtitle falls back to a window title.
- [x] The API docs do not describe the FastAPI 422 error body with an array `detail` (it echoes `input`). Clients have
      to handle it next to the string `detail`. The array now drops `input` and `ctx`, and `docs/api/README.md`
      describes both shapes.
- [ ] A journal thread for a checkout without a git remote is named after its generic directory: `fylgja-raycast/main`
      gets the name `main`, while `fylgja/main` (with a remote) gets `fylgja`. The cause: with no remote,
      `collectors/project/state.py:310` falls back to the directory's basename for `repo_name`, and
      `core/labels.py:project_name` returns `repo_name` before its `GENERIC_DIRECTORIES` rule, so that rule never
      applies. The fallback should be left to `project_name` (keep `repo_name` `None` without a remote). Threads also
      keep the name they were created with (`JournalService._name` returns the stored row name), so existing threads
      need a rename or migration after the fix. The Raycast client now shows `folder` instead, so it is not affected.
- [ ] Wishes from the Raycast brief: `bundle_id` and the host app on `ItemPlace`, and a short headline per item.
