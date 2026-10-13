-- 0002_folder_summaries: summaries gain the folder scope (docs/decisions/0053).
--
-- A folder summary describes one navigation item; its scope_ref is the item id (16 hex digits).
-- SQLite cannot change a CHECK constraint in place, so the table is rebuilt: the new table is
-- created, every row is copied with its id, the old table is dropped and the new one renamed.
-- 0001 created no index, trigger or view on summaries and no foreign key points at it, so
-- nothing else needs recreating. The migrations runner wraps this file in a transaction and
-- sets PRAGMA user_version, so a failure leaves the database at version 1.

CREATE TABLE summaries_new (
    id            INTEGER PRIMARY KEY,
    scope_kind    TEXT NOT NULL CHECK (scope_kind IN ('session', 'workspace', 'day', 'folder')),
    scope_ref     TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('arc', 'now', 'text')),
    text          TEXT,
    input_digest  TEXT,
    model         TEXT,
    generated_at  TEXT,
    error_kind    TEXT,
    UNIQUE (scope_kind, scope_ref, kind)
);

INSERT INTO summaries_new (
    id, scope_kind, scope_ref, kind, text, input_digest, model, generated_at, error_kind
)
SELECT id, scope_kind, scope_ref, kind, text, input_digest, model, generated_at, error_kind
FROM summaries;

DROP TABLE summaries;

ALTER TABLE summaries_new RENAME TO summaries;
