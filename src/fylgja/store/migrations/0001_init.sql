-- 0001_init: the whole PLAN-final-plan.md §4.1 schema.
--
-- Conventions
--   * Timestamps are UTC text in one fixed format, 'YYYY-MM-DDTHH:MM:SS.ffffffZ'
--     (fylgja.store.db.to_db_time), so text order is time order.
--   * Change-interval tables (A20) hold one row per distinct state: valid_from opens it and
--     valid_to closes it. A partial unique index allows one open interval per entity.
--   * Columns beyond the §4.1 key columns are noted where they are added. Two §4.1 names are
--     SQL keywords and are spelled differently here: projects."group" is group_name, and
--     focus_spans start/end are started_at/ended_at.
--   * The migrations runner wraps this file in a transaction and sets PRAGMA user_version.

-- Raw records. Retention: 30 days (A12 scopes rebuildability to this window).
CREATE TABLE events (
    id           INTEGER PRIMARY KEY,
    dedup_key    TEXT NOT NULL UNIQUE,
    source       TEXT NOT NULL,
    record_type  TEXT NOT NULL CHECK (record_type IN ('event', 'snapshot')),  -- added
    entity_kind  TEXT NOT NULL,
    entity_id    TEXT NOT NULL,
    observed_at  TEXT NOT NULL,
    received_at  TEXT NOT NULL,
    payload      TEXT NOT NULL CHECK (json_valid(payload))  -- minimized and redacted (A5, §4.4)
);
CREATE INDEX events_observed_at ON events (observed_at);
CREATE INDEX events_kind_time ON events (entity_kind, observed_at);
CREATE INDEX events_entity ON events (entity_kind, entity_id, observed_at);
CREATE INDEX events_source_time ON events (source, observed_at);

-- Window states as change intervals (A20).
CREATE TABLE windows_snapshot (
    id             INTEGER PRIMARY KEY,
    source         TEXT NOT NULL,  -- added: the collector, so a complete snapshot closes its own
    window_id      TEXT NOT NULL,
    workspace      TEXT,
    app_bundle_id  TEXT,
    app_name       TEXT,
    app_pid        INTEGER,
    title          TEXT,           -- redacted
    valid_from     TEXT NOT NULL,
    valid_to       TEXT,
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);
CREATE UNIQUE INDEX windows_snapshot_open ON windows_snapshot (source, window_id)
    WHERE valid_to IS NULL;
CREATE INDEX windows_snapshot_window ON windows_snapshot (window_id, valid_from);
CREATE INDEX windows_snapshot_valid_to ON windows_snapshot (valid_to);

CREATE TABLE focus_spans (
    id             INTEGER PRIMARY KEY,
    window_id      TEXT,
    workspace      TEXT,
    app_bundle_id  TEXT,
    title          TEXT,  -- redacted
    project_path   TEXT,
    session_key    TEXT,
    started_at     TEXT NOT NULL,  -- §4.1 start
    ended_at       TEXT,           -- §4.1 end; NULL while the span is open
    end_reason     TEXT CHECK (end_reason IS NULL OR end_reason IN ('focus', 'idle', 'lock', 'shutdown')),
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);
CREATE INDEX focus_spans_started_at ON focus_spans (started_at);
CREATE INDEX focus_spans_ended_at ON focus_spans (ended_at);
CREATE INDEX focus_spans_window ON focus_spans (window_id, started_at);
CREATE INDEX focus_spans_project ON focus_spans (project_path, started_at);

CREATE TABLE sessions (
    session_key      TEXT PRIMARY KEY,  -- kind:native_id
    kind             TEXT NOT NULL,
    native_id        TEXT NOT NULL,
    source           TEXT NOT NULL,     -- added: the collector that reports the session
    pid              INTEGER,
    process_key      TEXT,
    cwd              TEXT,
    project_path     TEXT,
    host_kind        TEXT,
    host_ref         TEXT,
    window_id        TEXT,
    workspace        TEXT,
    name             TEXT,              -- redacted
    title            TEXT,              -- redacted
    model            TEXT,
    branch           TEXT,
    state            TEXT,
    state_since      TEXT,
    waiting_reason   TEXT,
    parent_key       TEXT,
    predecessor_key  TEXT,
    remote_url       TEXT,
    first_seen       TEXT NOT NULL,
    last_seen        TEXT NOT NULL,
    ended_at         TEXT,
    UNIQUE (kind, native_id)
);
CREATE INDEX sessions_source_live ON sessions (source, ended_at);
CREATE INDEX sessions_project ON sessions (project_path);
CREATE INDEX sessions_process ON sessions (process_key);
CREATE INDEX sessions_window ON sessions (window_id);

-- Session activity as change intervals (A20).
CREATE TABLE session_snapshots (
    id                  INTEGER PRIMARY KEY,
    session_key         TEXT NOT NULL REFERENCES sessions (session_key) ON DELETE CASCADE,
    source              TEXT NOT NULL,  -- added: the collector, so a complete snapshot closes its own
    valid_from          TEXT NOT NULL,
    valid_to            TEXT,
    state               TEXT,
    waiting_reason      TEXT,           -- added: SessionActivity input of the one-liner (§4.6)
    last_tool           TEXT,
    current_target      TEXT,           -- added (§4.6)
    open_task           TEXT,           -- added (§4.6)
    stage               TEXT CHECK (stage IS NULL OR json_valid(stage)),  -- added (§4.6)
    last_prompt         TEXT CHECK (last_prompt IS NULL OR length(last_prompt) <= 300),
    last_reply          TEXT CHECK (last_reply IS NULL OR length(last_reply) <= 800),
    one_liner           TEXT CHECK (one_liner IS NULL OR length(one_liner) <= 160),
    context_pct         REAL,
    cost_usd            REAL,
    tokens_input        INTEGER,
    tokens_output       INTEGER,
    tokens_cache_read   INTEGER,
    tokens_cache_write  INTEGER,
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);
CREATE UNIQUE INDEX session_snapshots_open ON session_snapshots (session_key)
    WHERE valid_to IS NULL;
CREATE INDEX session_snapshots_key ON session_snapshots (session_key, valid_from);
CREATE INDEX session_snapshots_valid_to ON session_snapshots (valid_to);

CREATE TABLE projects (
    path               TEXT PRIMARY KEY,
    repo_root          TEXT,
    remote_url         TEXT,
    repo_name          TEXT,
    group_name         TEXT,  -- §4.1 "group": the draupnir workspace
    branch             TEXT,
    dirty              INTEGER CHECK (dirty IS NULL OR dirty IN (0, 1)),
    ahead              INTEGER,
    behind             INTEGER,
    pr_number          INTEGER,
    pr_state           TEXT,
    review_decision    TEXT,
    build_state        TEXT CHECK (
        build_state IS NULL OR build_state IN ('pass', 'fail', 'running', 'cancelled', 'none')
    ),
    build              TEXT CHECK (build IS NULL OR json_valid(build)),  -- §4.7
    build_observed_at  TEXT,
    build_error_kind   TEXT,
    last_commit_at     TEXT,
    updated_at         TEXT NOT NULL
);
CREATE INDEX projects_repo_root ON projects (repo_root);

-- Provider usage and cost as change intervals (A20, §4.7).
CREATE TABLE usage_snapshots (
    id           INTEGER PRIMARY KEY,
    collector    TEXT NOT NULL,  -- added: the collector, so a complete snapshot closes its own
    provider     TEXT NOT NULL,
    window       TEXT NOT NULL DEFAULT '',
    used_pct     REAL,
    resets_at    TEXT,
    cost_usd     REAL,
    cost_period  TEXT,
    source       TEXT NOT NULL,  -- statusline, codexbar, ...
    valid_from   TEXT NOT NULL,
    valid_to     TEXT,
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);
CREATE UNIQUE INDEX usage_snapshots_open ON usage_snapshots (collector, provider, window)
    WHERE valid_to IS NULL;
CREATE INDEX usage_snapshots_provider ON usage_snapshots (provider, valid_from);
CREATE INDEX usage_snapshots_valid_to ON usage_snapshots (valid_to);

CREATE TABLE workspaces (
    name             TEXT PRIMARY KEY,
    manual_label     TEXT,
    manual_label_at  TEXT,
    inferred_label   TEXT,
    inferred_at      TEXT,
    source           TEXT,  -- added: the collector that reports the workspace
    monitor          TEXT,  -- added
    focused          INTEGER NOT NULL DEFAULT 0 CHECK (focused IN (0, 1)),  -- added
    first_seen       TEXT,  -- added
    last_seen        TEXT,  -- added
    closed_at        TEXT   -- added: set when a complete snapshot no longer lists it
);

-- Kept forever.
CREATE TABLE todos (
    id              TEXT PRIMARY KEY,  -- uuid
    scope_kind      TEXT,
    scope_ref       TEXT,
    text            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'next', 'done')),
    created_at      TEXT NOT NULL,
    done_at         TEXT,
    source          TEXT NOT NULL,
    source_ref      TEXT,
    link            TEXT,
    updated_at      TEXT NOT NULL,
    deleted_at      TEXT,
    deleted_reason  TEXT CHECK (deleted_reason IS NULL OR deleted_reason IN ('user', 'source_absent')),
    UNIQUE (source, source_ref)
);
CREATE INDEX todos_scope ON todos (scope_kind, scope_ref, status);
CREATE INDEX todos_status ON todos (status, updated_at);

CREATE TABLE suggestions_log (
    id            TEXT PRIMARY KEY,  -- the suggestion_id of /next
    list_id       TEXT NOT NULL,
    generated_at  TEXT NOT NULL,
    rank          INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    target_ref    TEXT,
    score         REAL,
    reason        TEXT,
    feedback      TEXT CHECK (feedback IS NULL OR feedback IN ('good', 'bad')),
    feedback_at   TEXT,
    acted_at      TEXT
);
CREATE INDEX suggestions_log_list ON suggestions_log (list_id, rank);
CREATE INDEX suggestions_log_generated_at ON suggestions_log (generated_at);

-- No text body is ever stored.
CREATE TABLE message_receipts (
    message_id   TEXT PRIMARY KEY,
    session_key  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    state        TEXT NOT NULL,
    state_at     TEXT NOT NULL,
    bytes        INTEGER NOT NULL CHECK (bytes >= 0),
    error_kind   TEXT
);
CREATE INDEX message_receipts_session ON message_receipts (session_key, created_at);

CREATE TABLE summaries (
    id            INTEGER PRIMARY KEY,
    scope_kind    TEXT NOT NULL CHECK (scope_kind IN ('session', 'workspace', 'day')),
    scope_ref     TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('arc', 'now', 'text')),
    text          TEXT,
    input_digest  TEXT,
    model         TEXT,
    generated_at  TEXT,
    error_kind    TEXT,
    UNIQUE (scope_kind, scope_ref, kind)
);

CREATE TABLE collector_health (
    name             TEXT PRIMARY KEY,
    state            TEXT NOT NULL CHECK (
        state IN ('starting', 'ok', 'degraded', 'failed', 'disabled')
    ),
    since            TEXT NOT NULL,
    last_ok_at       TEXT,
    restarts         INTEGER NOT NULL DEFAULT 0 CHECK (restarts >= 0),
    last_error_kind  TEXT
);
