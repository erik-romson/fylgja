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
| JetBrains, DataGrip | `ide_project`: `path`, `project`, `via` | tier 1 for every IDE window, from a cached `options/recentProjects.xml` (open projects first, then `frameTitle`); the title's `[path]` when present. A focus change re-reads it | none |
| Obsidian | `note`: `vault`, `vault_path`, `note`, `link` (`obsidian://open?vault=…&file=…`) | the title, and `~/Library/Application Support/obsidian/obsidian.json` | none |
| iTerm2 | `terminal_session`: `path`, `job` | `osascript`, 3 s timeout (best effort) | `[actions.applescript] iterm2` |

## JetBrains: the project path

Every JetBrains window gets its `ide_project` item in tier 1, not only the focused one. The collector keeps the
`options/recentProjects.xml` of the newest product directory per IDE in memory: it re-stats the file at most every 30 s
(and on a focus change) and parses it again only when its location or modification time changes. A window is resolved
again only when its title or the parsed file changes.

With **Settings → Appearance & Behavior → Appearance → UI Options → "Always show full path in window header"** turned on,
the title carries the path (`alpha [~/work/alpha/main]`), which is exact: `confidence` is `high` and `via` is `title`.
Without the setting, checkouts that share a directory name (`~/work/alpha/main` and `~/work/beta/main`) are told apart in
this order:

| Rule | `confidence` | `via` |
|---|---|---|
| the title carries `[path]` | high | `title` |
| exactly one entry has the project name | high | `recentProjects.xml` |
| several have it and exactly one is `opened` | high | `recentProjects.xml:opened` |
| several are open (or none is) and exactly one `frameTitle` equals the window title | medium | `recentProjects.xml:frameTitle` |
| otherwise the newest open entry (the newest of all when none is open) | medium | `recentProjects.xml` |
| no entry has it, the name is `<parent> <directory>`, and exactly one listed project's parent folder is called `<parent>` and holds `<directory>` (decision 0062) | medium | `recentProjects.xml:sibling` |

The project name in the title matches an entry by any name the IDE shows for it, ignoring case: its `displayName`, its
`frameTitle`, the project its `frameTitle` names (`alpha main` in `alpha main – notes.md`), its directory name (`main`)
or its parent and directory joined by a space (`alpha main`, the way the IDE titles a project whose name differs from
its directory). The IDE writes `recentProjects.xml` rarely, typically when a project closes, so a project opened since
then has no entry and gets no path until the file is written again, unless the title carries it.

A window remembers its path. A later, weaker reading for the same project name does not replace it; a new project name
does; a closed window forgets it. The setting's label was verified against `jetbrains.com/help/idea/settings-appearance.html`
on 2026-10-08.

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
