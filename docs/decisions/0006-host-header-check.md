# 0006: Every endpoint checks the Host header

- Status: accepted
- Amendment: A6
- Amends: PLAN.md §6
- Answers: —
- Relates to: A7
- Date: 2026-10-05

## Context

A loopback bind does not stop DNS rebinding from reading "unauthenticated" read endpoints, and window titles contain customer names.

## Decision

**Every endpoint checks the Host header.** Only `127.0.0.1:<port>` and `localhost:<port>` are accepted, and no CORS headers are ever sent. Mutating endpoints also require `Authorization: Bearer`, an allowed Origin (or no Origin together with a valid bearer, for the CLI) and `Content-Type: application/json`.

## Consequences

- A security layer in `api/security.py` applies to all routes, including read endpoints and SSE.
- No CORS headers are sent. Mutating endpoints also need a bearer token, an allowed Origin and a JSON content type.
- API tests cover rejected Host headers and DNS-rebinding style requests.
