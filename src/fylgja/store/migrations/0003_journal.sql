-- 0003_journal: the workspace journal (docs/decisions/0057).
--
-- One thread per checkout and branch, its entries, and its ledger of open items. Rows are kept
-- forever: a model call cannot be repeated, so the retention job never touches these tables.
-- No foreign key points at sessions, projects or todos; a thread's identity is its (repo_root,
-- branch), and an item names a todo by id only. The migrations runner wraps this file in a
-- transaction and sets PRAGMA user_version, so a failure leaves the database at version 2.

CREATE TABLE journal_threads (
    thread_id        TEXT PRIMARY KEY,  -- item_id("thread\0" + repo_root + "\0" + branch)
    repo_root        TEXT NOT NULL,
    branch           TEXT NOT NULL,
    name             TEXT NOT NULL,
    first_at         TEXT NOT NULL,
    last_at          TEXT,              -- the end of the latest entry
    watermark        TEXT,              -- the end of the latest successful attempt
    where_text       TEXT,
    where_at         TEXT,
    last_attempt_at  TEXT,
    error_kind       TEXT,
    error_at         TEXT,
    UNIQUE (repo_root, branch)
);

CREATE TABLE journal_entries (
    id            INTEGER PRIMARY KEY,
    thread_id     TEXT NOT NULL REFERENCES journal_threads (thread_id),
    started_at    TEXT NOT NULL,
    ended_at      TEXT NOT NULL,
    trigger       TEXT NOT NULL CHECK (
        trigger IN ('turn_done', 'session_end', 'run_finished', 'focus_left', 'branch_changed', 'manual')
    ),
    headline      TEXT NOT NULL,
    did           TEXT NOT NULL CHECK (json_valid(did)),
    sources       TEXT NOT NULL CHECK (json_valid(sources)),
    input_digest  TEXT NOT NULL,
    model         TEXT NOT NULL,
    generated_at  TEXT NOT NULL
);
CREATE INDEX journal_entries_thread ON journal_entries (thread_id, ended_at);

CREATE TABLE journal_items (
    id              TEXT PRIMARY KEY,  -- item_id("journal-item\0" + thread_id + "\0" + seq)
    thread_id       TEXT NOT NULL REFERENCES journal_threads (thread_id),
    seq             INTEGER NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('next', 'remember', 'blocked')),
    text            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'dropped')),
    pinned          INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
    edited          INTEGER NOT NULL DEFAULT 0 CHECK (edited IN (0, 1)),
    source          TEXT NOT NULL CHECK (json_valid(source)),
    created_at      TEXT NOT NULL,
    created_entry   INTEGER REFERENCES journal_entries (id),
    closed_at       TEXT,
    closed_by       TEXT CHECK (closed_by IS NULL OR closed_by IN ('model', 'user')),
    closed_entry    INTEGER REFERENCES journal_entries (id),
    todo_id         TEXT,
    how_text        TEXT,
    how_digest      TEXT,
    how_at          TEXT,
    how_error_kind  TEXT,
    UNIQUE (thread_id, seq)
);
CREATE INDEX journal_items_thread ON journal_items (thread_id, status);
CREATE INDEX journal_items_todo ON journal_items (todo_id) WHERE todo_id IS NOT NULL;
