# 0038: Mutation guard details

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A6, A22
- Date: 2026-10-06

## Context

WP3.4 requires a bearer, an Origin check, a JSON content type and body limits on every mutating route,
and a way to create the secret. Several details are open: where the guard sits, in which order it
checks, what happens without a secret, and how `install secret` prints.

## Decision

- `MutationGuard` is an ASGI middleware keyed on the HTTP method (`POST`, `PUT`, `PATCH`, `DELETE`),
  not on a router, so a route added later cannot skip it. It sits inside `HostGuard`.
- Checks run in this order: bearer (401), Origin (403), content type (415), body size (413). The
  bearer is first so that a caller without it learns nothing about the other rules. A missing
  `Origin` is accepted, but only after the bearer has passed.
- The body is read in the middleware up to the limit (16 KiB; 64 KiB for `/sessions/{key}/message`)
  and replayed to the route, so the limit holds for chunked bodies and before any JSON parsing.
- `create_app` reads the secret once at start-up. With no secret file, every mutation gets 401.
- `fylgja install secret` writes at once (no `--apply`: it is machine-specific and not part of
  dotfiles), keeps an existing secret, replaces it with `--rotate`, refuses a location inside
  `~/dotfiles`, and prints the secret only with `--show`.
- The CLI client's `send_json` sends the bearer and `Content-Type: application/json`, never an Origin.

## Consequences

A rotated secret takes effect when the daemon restarts. A `DELETE` from the CLI sends a `{}` body
so the content-type rule holds without exceptions. `tests/integration/test_mutation_guards.py`
enumerates the mutating routes and covers every later route automatically.
