# Statusline payloads for the usage tests

Synthetic payloads in the shape Claude Code documents for the status line (docs `statusline`,
fetched 2026-10-06): `cost.total_cost_usd`, `context_window.used_percentage` and, for claude.ai
Pro and Max subscribers or behind a Claude apps gateway, `rate_limits.<window>.used_percentage` and
`resets_at` (Unix epoch seconds). No snapshot from a real session was available when they were
written (`docs/revalidation/2026-10-06-usage.md`), so none of these is recorded output.
