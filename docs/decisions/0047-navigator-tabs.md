# 0047: Navigator tabs

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A6, A19
- Date: 2026-10-13

## Context

WP7.1 (`PLAN-final-plan.md` §WP7.1) defines the navigator page as one view: the Next strip, the workspace cards and the
todos. Further views (live data, todos, collectors, data flow, explain) need room without a second page, a build step or
a CSP change.

## Decision

- **Hash-routed tabs in the same file.** `src/fylgja/api/static/index.html` stays one page. `#overview` (the default) is
  today's view; `#live` is the Live data tab. An unknown hash falls back to the Overview. `GET /` stays the only HTML
  route, so its CSP, no-store headers and secret injection do not change.
- **Only the visible tab loads.** `TABS` maps each tab to one loader. `refresh()` shows the current tab and awaits its
  loader; the SSE `now.changed` event reloads the visible tab, and a hidden tab keeps its old DOM until it is shown. There
  is no `setInterval`.
- **Live data holds the raw `/now` views.** Its first part is the session table: every live session in `/now` order,
  children indented, one row each. A row click focuses the session.
- **The session table omits `facts`.** Prompts, replies and file names never reach the table.
- **Shared helpers.** `fmtTime` formats an instant as local time and does no arithmetic; `fillTable(table, head, rows)`
  builds every table, with every string entering the DOM as text.
- **The daemon computes, the page renders.** The page may render what a route returns in the order it returns it,
  format an instant (`fmtTime`) or a number of seconds (`fmtDuration`), map a fixed id to a label (`SOURCE_LABELS`),
  show and hide tabs, keep UI choices in the hash or `localStorage`, debounce input and throttle reloads. It may not
  sort, filter, group, cap, count across items, subtract times, score or decide a click target. A tab that needs more
  than formatting gets a page-shaped view model at `GET /view/<tab>` (`fylgja.view.<tab>/1`), built by a pure function
  in `api/views.py`; `/now` stays the frozen data contract. The Overview is the first: `GET /view/overview`
  (`docs/api/views.md`) carries the cards, the Next strip and the todo groups with their caps, order, titles and click
  targets, and the page makes that one call.

## Consequences

- Later tabs add a section with `data-tab`, a loader and a key inside the `TABS` literal; a test checks that every
  `data-tab` is a key.
- Tab switching has no JavaScript runtime in CI, so it is checked by hand in a browser.
- The Overview's rules (caps, waiting sessions first, which workspaces get a card, click targets, todo groups and
  titles) are unit-tested Python instead of untested JavaScript. `test_the_page_computes_nothing` bans `.sort(`,
  `.filter(`, `.reduce(`, `.find(`, `.some(`, `.every(`, `.slice(`, `.flatMap(`, `const MAX_`, `Date.now() -` and
  `- new Date(` in the script; an allowed exception is listed verbatim in its `ALLOWED_COMPUTATION`.
- The data-contract test resolves each variable table against its own schema: `/now` fields against
  `docs/api/now.schema.json`, view-model fields against the model's in-memory JSON Schema.
