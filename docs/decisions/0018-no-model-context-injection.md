# 0018: Fylgja never injects into model context

- Status: accepted
- Amendment: A18
- Amends: PLAN.md §8 fast entry
- Answers: Q9, Q12
- Relates to: A21, A24
- Date: 2026-10-05

## Context

R15: the Fylgja hook is async and silent by design.

## Decision

**Fylgja never injects into model context.** Todo fast entry from Claude uses a user-level slash command that runs `fylgja todo add --session …`. Lidskjalv's reminder injection stays in Lidskjalv's own synchronous `UserPromptSubmit` hook, independent of Fylgja (Q9, A21). Fylgja neither depends on that hook nor takes it over, so removing Lidskjalv also removes reminder injection (Q12). Fylgja adds no synchronous hook; its session-scoped todos, shown in `/now`, the CLI and the page, are the replacement. Because Lidskjalv will eventually use Fylgja (Q12), it may later store its reminders as Fylgja session todos while its own hook keeps doing the injection (A24).

## Consequences

- Todo fast entry from Claude is a user-level slash command that runs `fylgja todo add --session …`.
- Fylgja registers no synchronous hook, and its session-scoped todos are the replacement for reminder injection.
- Lidskjalv keeps its own injection hook, which disappears together with Lidskjalv.
