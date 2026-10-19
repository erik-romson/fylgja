# Findings from the Raycast client

The Raycast extension (`~/projects/erik-romson/fylgja-raycast/main`) ran its live contract check against the daemon on
2026-10-19. These are daemon-side issues it found. Nothing here blocks the extension.

- [ ] Every live session lacked a process chain (`no process chain recorded for the session`, decision 0029), so no
      session could be focused: `POST /focus/session` answered 409 "session has no window".
- [ ] `go` and `go_agent` point at sessions without a window, so a client's first Go on such an item always fails.
- [ ] 17 pairs of app items share a name (a bundle-id key against a name key).
- [ ] `summary` is empty on most app items, so the Raycast subtitle falls back to a window title.
- [ ] The API docs do not describe the FastAPI 422 error body with an array `detail` (it echoes `input`). Clients have
      to handle it next to the string `detail`.
- [ ] Wishes from the Raycast brief: `bundle_id` and the host app on `ItemPlace`, and a short headline per item.
