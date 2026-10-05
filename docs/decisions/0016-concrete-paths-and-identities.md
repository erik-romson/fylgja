# 0016: Concrete paths and identities

- Status: accepted
- Amendment: A16
- Amends: PLAN.md §5, §10
- Answers: —
- Relates to: A21, A22
- Date: 2026-10-05

## Context

`PLAN.md` leaves these unspecified.

## Decision

**Concrete paths and identities.** These defaults can be overridden with `FYLGJA_CONFIG_DIR`, `FYLGJA_DATA_DIR` and `FYLGJA_STATE_DIR`; tests always override them.

## Consequences

- All paths are resolved only through `fylgja.paths`, which honours `FYLGJA_CONFIG_DIR`, `FYLGJA_DATA_DIR` and `FYLGJA_STATE_DIR`.
- The test isolation fixture overrides all three and `HOME`, so tests never touch the live home directory.
- The API listens on `127.0.0.1:7319` and the launchd label is `io.github.erik-romson.fylgja`.
