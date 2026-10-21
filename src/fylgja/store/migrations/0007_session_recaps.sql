-- 0007_session_recaps: the recaps an agent writes of its session (docs/decisions/0064).
--
-- Claude Code writes an away_summary line when the person comes back to a session. A recap is an
-- event, not session state, so it gets its own table instead of opening activity intervals. A
-- row is keyed by the session and the line's time, so reading the same line again changes
-- nothing.

CREATE TABLE session_recaps (
    session_key  TEXT NOT NULL REFERENCES sessions (session_key) ON DELETE CASCADE,
    at           TEXT NOT NULL,
    branch       TEXT,
    text         TEXT NOT NULL CHECK (length(text) <= 600),
    PRIMARY KEY (session_key, at)
);
