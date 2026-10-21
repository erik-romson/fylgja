-- 0006_journal_resumed: a session that goes on after waiting for the person (docs/decisions/0063).
--
-- journal_entries.trigger takes 'resumed', and journal_items.closed_by takes 'fylgja': the daemon
-- closes a blocked item itself once its session goes on. SQLite cannot change a CHECK, so both
-- tables are rebuilt with their rows, ids and indexes. The new items table refers to the new
-- entries table, the old tables are dropped child first, and the renames rewrite that reference
-- to journal_entries. The migrations runner wraps this file in a transaction and sets
-- PRAGMA user_version, so a failure leaves the database at version 5.

CREATE TABLE journal_entries_new (
    id            INTEGER PRIMARY KEY,
    thread_id     TEXT NOT NULL REFERENCES journal_threads (thread_id),
    started_at    TEXT NOT NULL,
    ended_at      TEXT NOT NULL,
    trigger       TEXT NOT NULL CHECK (
        trigger IN (
            'turn_done', 'resumed', 'session_end', 'run_finished', 'focus_left', 'branch_changed',
            'manual'
        )
    ),
    headline      TEXT NOT NULL,
    did           TEXT NOT NULL CHECK (json_valid(did)),
    sources       TEXT NOT NULL CHECK (json_valid(sources)),
    input_digest  TEXT NOT NULL,
    model         TEXT NOT NULL,
    generated_at  TEXT NOT NULL
);
INSERT INTO journal_entries_new (
    id, thread_id, started_at, ended_at, trigger, headline, did, sources, input_digest, model,
    generated_at
)
SELECT id, thread_id, started_at, ended_at, trigger, headline, did, sources, input_digest, model,
       generated_at
FROM journal_entries;

CREATE TABLE journal_items_new (
    id              TEXT PRIMARY KEY,
    thread_id       TEXT NOT NULL REFERENCES journal_threads (thread_id),
    seq             INTEGER NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('next', 'remember', 'blocked')),
    text            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'dropped')),
    pinned          INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
    edited          INTEGER NOT NULL DEFAULT 0 CHECK (edited IN (0, 1)),
    source          TEXT NOT NULL CHECK (json_valid(source)),
    created_at      TEXT NOT NULL,
    created_entry   INTEGER REFERENCES journal_entries_new (id),
    closed_at       TEXT,
    closed_by       TEXT CHECK (closed_by IS NULL OR closed_by IN ('model', 'user', 'fylgja')),
    closed_entry    INTEGER REFERENCES journal_entries_new (id),
    todo_id         TEXT,
    how_text        TEXT,
    how_digest      TEXT,
    how_at          TEXT,
    how_error_kind  TEXT,
    UNIQUE (thread_id, seq)
);
INSERT INTO journal_items_new (
    id, thread_id, seq, kind, text, status, pinned, edited, source, created_at, created_entry,
    closed_at, closed_by, closed_entry, todo_id, how_text, how_digest, how_at, how_error_kind
)
SELECT id, thread_id, seq, kind, text, status, pinned, edited, source, created_at, created_entry,
       closed_at, closed_by, closed_entry, todo_id, how_text, how_digest, how_at, how_error_kind
FROM journal_items;

DROP TABLE journal_items;
DROP TABLE journal_entries;
ALTER TABLE journal_entries_new RENAME TO journal_entries;
ALTER TABLE journal_items_new RENAME TO journal_items;

CREATE INDEX journal_entries_thread ON journal_entries (thread_id, ended_at);
CREATE INDEX journal_items_thread ON journal_items (thread_id, status);
CREATE INDEX journal_items_todo ON journal_items (todo_id) WHERE todo_id IS NOT NULL;
