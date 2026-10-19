# 0062: JetBrains projects opened after the IDE last saved recentProjects.xml

- Status: accepted
- Amendment: none
- Amends: 0029
- Answers: —
- Relates to: 0029, 0049, 0051
- Date: 2026-10-19

## Context

Both the appcontext collector (a window's `ide_project` path) and the resolver's JetBrains join (decision 0029) find
a project's path in `options/recentProjects.xml`. The IDE writes that file only now and then, so a project opened
after the last save is not in it. Its window then had no folder and its Claude session no window: on the daily store a
worktree opened in the morning showed "never focused" in the navigator, although its IDE window had more than half an
hour of focus that day. The spans were credited to the IntelliJ app item, and the item's only place was a session
that could not be focused. The IDE titled the window `<parent> <directory>` (`cbslink feature-…`), a name the join did
not compare either.

## Decision

- **appcontext.** A title project name that no entry has, of the form `<parent> <directory>`, resolves to
  `<folder>/<directory>` when exactly one parent folder of a listed project is called `<parent>` and holds that
  directory (an `is_dir` check through the filesystem seam). Confidence `medium`, via `recentProjects.xml:sibling`.
- **Resolver.** When no listed project contains the session's cwd, the JetBrains join walks from the cwd up to (not
  including) the home directory and takes the nearest folder whose `<directory>` or `<parent> <directory>` one window
  title shows: `parent-chain+project-path` at `medium`, with that folder as `host_ref`. Several such windows give the
  workspace-level answer, as a shared name does today.
- **Window titles also match `<parent> <directory>`** of a listed project.

## Consequences

- A bare `<directory>` title of an unlisted project is still not resolved by appcontext; a session inside it still
  is, by the resolver.
- The check costs one `is_dir` per unmatched IDE title change or focus; nothing is read.
