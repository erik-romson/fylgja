# 0065: A workspace without a placed session is labelled by its windows' folders

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: 0049, 0062
- Date: 2026-10-21

## Context

The inferred workspace label (WP1.8) is the dominant project of the sessions placed in the workspace, then the
dominant app plus a title word. An IDE window with no agent session in its terminal therefore gave a label such as
"PyCharm pipeline-helpers-…", although its `ide_project` context named the checkout with high confidence and the
navigator already showed that checkout's window in the workspace (decision 0049).

## Decision

- The resolver takes the git root and draupnir group of each window's working folder, the way the focus spans read it
  (`WindowFolders`: an `ide_project`, `cmux_tab` or `terminal_session` context item).
- A workspace with **no placed session** that has a project is labelled by the dominant project of those window
  folders, named as a session's project is (`project_name`), with rule `project`.
- A workspace with a placed session keeps the label of its sessions' projects; window folders do not count there.
- The folders only label workspaces. Project placement and `/now` `workspaces[].projects` still come from sessions.

## Consequences

- A workspace that holds only an IDE window shows the checkout, or its draupnir group, instead of the app and a word of
  the file name.
- A context item that arrives after the window is picked up by the next resolver refresh, at most one maintenance tick
  later.
