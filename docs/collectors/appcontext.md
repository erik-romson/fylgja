# Application context: title parsers (WP5.3 first tier)

`fylgja.collectors.appcontext.titles` turns a window title into a `TitleContext` (`kind`, `fields`, `unread`,
`confidence`). It is pure: step 40 adds the collector, the adapter and the second tier. Formats and the A15 unread
markers are recorded in `docs/revalidation/2026-10-06-app-titles.md`.

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

Order: the parser runs on the raw title in the collector, then the ingest gate redacts the title and every string
field before anything is stored or sent. Unknown bundle ids and empty titles give `None`.
