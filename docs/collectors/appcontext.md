# Application context: the `appcontext` collector (WP5.3)

Capability `context`. Built-in, enabled by default; the AppleScript switches stay off. Decision
`docs/decisions/0043-appcontext-second-tier.md`; revalidation `docs/revalidation/2026-10-06-appcontext.md` (formats and A15
markers: `2026-10-06-app-titles.md`).

## Tier 1: titles, on every window snapshot

`fylgja.collectors.appcontext.titles` turns a window title into a `TitleContext` (`kind`, `fields`, `unread`, `confidence`). It
is pure. The collector reads the open windows from the store (read-only) every `poll_interval_seconds` (0.5 s) and attaches a
`title` context item to each window that parses. An item is emitted when it differs from the last one for that window and
withdrawn when the title stops parsing.

| Bundle id | Kind | Fields | Unread (A15) |
|---|---|---|---|
| `com.cmuxterm.app` | terminal | `workspace` (weak hint, low confidence) | never |
| JetBrains IDE ids (`com.jetbrains.intellij`, `…pycharm`, `…WebStorm`, …) | ide | `project`, `path`, `file`, `file_path`, `module`, `view` | never |
| `com.jetbrains.datagrip` | database | `project`, `path`, `console` | never |
| `com.google.Chrome` | browser | `page_title`, `profile` | never |
| `com.microsoft.teams2` | chat | `section`, `context`, `chat_partner` | never (no marker observed) |
| `md.obsidian` | notes | `note`, `vault` | never |
| `com.microsoft.Outlook` | mail | `folder`, `account` | never |
| `com.tinyspeck.slackmacgap` | chat | `channel`, `channel_type`, `workspace` | `! ` prefix and `<n> new items` |

Unknown bundle ids and empty titles give no item.

## Tier 2: on a focus change only

A new focus event on an open window runs that app's second tier once. Nothing runs between focus changes.

| App | Item (`kind`) | Source | Switch |
|---|---|---|---|
| cmux | `cmux_tab`: `cmux_workspace`, `cmux_surface`, `cwd`, `lifecycle` | `~/.cmuxterm/claude-hook-sessions.json` (`activeSessionsByWorkspace`), read-only. The selection is guessed: `confidence` is `medium` (title names the cwd, or one workspace) or `low` | none |
| Chrome | `tab`: `url` (scheme, host, path only), `profile` | `osascript`, 3 s timeout | `[actions.applescript] chrome` |
| Chrome history | `browser_history`: up to `history_limit` recent `visits` | `History` as `file:<path>?mode=ro&immutable=1` | `[collectors.appcontext] chrome_history` |
| JetBrains, DataGrip | `ide_project`: `path`, `project`, `via` | the title's `[path]`, else `options/recentProjects.xml` of the newest product directory of that IDE | none |
| Obsidian | `note`: `vault`, `vault_path`, `note`, `link` (`obsidian://open?vault=…&file=…`) | the title, and `~/Library/Application Support/obsidian/obsidian.json` | none |
| iTerm2 | `terminal_session`: `path`, `job` | `osascript`, 3 s timeout (best effort) | `[actions.applescript] iterm2` |

Never read: `~/Library/Containers`, `Group Containers`, and any Teams, Slack or Outlook store.

## AppleScript switches

Both are off by default, and a switch that is off never produces an `osascript` command line. **A timeout disables only its own
switch**: it reads as "a permission dialog is probably pending", the collector reports `degraded` with the switch's name, and
the switch is not called again until `fylgja doctor` has been rerun and answered. Every other source keeps reporting.

`fylgja doctor` runs each switch that is on with a 3 s timeout (`applescript:chrome`, `applescript:iterm2`) and records the
result in `<state dir>/applescript-switches.json`; the collector only reads that file. To enable a switch again: answer the
permission dialog (System Settings > Privacy & Security > Automation), then run `fylgja doctor`.

## `/now`

`focused.context[]` and `workspaces[].windows[].context[]` carry the items (`kind`, `source`, `title`, `data`, `unread`,
`confidence`, `link`, `observed_at`). The title-tier item's `title` is a one-line summary of the window; the workspace that
holds the focused window uses it as the third source of its one-liner (after a waiting session and the most recent session).
Items of a collector that is not available are absent. The schema is unchanged.

## Configuration

```toml
[collectors.appcontext]
enabled = true
poll_interval_seconds = 0.5   # 0.1 to 5
chrome_history = false
history_limit = 10            # 1 to 50
history_window_seconds = 3600

[actions.applescript]
chrome = false
iterm2 = false
```
