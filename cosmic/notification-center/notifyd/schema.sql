-- History store schema. Applied once on an empty database (user_version 0 -> 2). Version 1 databases are migrated
-- by store.py (_MIGRATE_V2), which must stay equivalent to the v2 additions below.
-- id is the notification id handed to the client (INTEGER PRIMARY KEY, no AUTOINCREMENT):
-- the store must seed the id counter from MAX(id)+1 at startup. A Notify with replaces_id
-- updates the row of that id in place (hence the update trigger).
-- JSON columns hold the JSON text of the matching Notification field.
-- State is one of shown|blocked|dnd|store_only. Timestamps are epoch seconds (REAL).
CREATE TABLE notifications (
    id              INTEGER PRIMARY KEY,
    app_name        TEXT    NOT NULL DEFAULT '',
    replaces_id     INTEGER NOT NULL DEFAULT 0,
    app_icon        TEXT    NOT NULL DEFAULT '',
    summary         TEXT    NOT NULL DEFAULT '',
    body            TEXT    NOT NULL DEFAULT '',
    actions         TEXT    NOT NULL DEFAULT '[]',  -- JSON [[key, label], ...]
    hints           TEXT    NOT NULL DEFAULT '{}',  -- JSON object
    expire_timeout  INTEGER NOT NULL DEFAULT -1,
    urgency         INTEGER NOT NULL DEFAULT 1,
    category        TEXT    NOT NULL DEFAULT '',
    desktop_entry   TEXT    NOT NULL DEFAULT '',
    received_at     REAL    NOT NULL,
    state           TEXT    NOT NULL DEFAULT 'shown'
                    CHECK (state IN ('shown', 'blocked', 'dnd', 'store_only')),
    tags            TEXT    NOT NULL DEFAULT '[]',  -- JSON [str]
    classes         TEXT    NOT NULL DEFAULT '[]',  -- JSON [str]
    highlights      TEXT    NOT NULL DEFAULT '[]',  -- JSON [{"pattern", "class"}]
    timeout_ms      INTEGER,                        -- NULL = never expires
    sound           TEXT,
    matched_rules   TEXT    NOT NULL DEFAULT '[]',  -- JSON [str]
    read_at         REAL,
    closed_at       REAL,
    close_reason    INTEGER,                        -- CloseReason value
    buttons         TEXT    NOT NULL DEFAULT '[]'   -- JSON [{id, label, run, builtin, ...}] (v2)
);

CREATE INDEX idx_notifications_received_at ON notifications (received_at);
CREATE INDEX idx_notifications_app_name    ON notifications (app_name);
CREATE INDEX idx_notifications_state       ON notifications (state);

CREATE VIRTUAL TABLE notifications_fts USING fts5(
    app_name, summary, body,
    content='notifications', content_rowid='id'
);

CREATE TRIGGER notifications_ai AFTER INSERT ON notifications BEGIN
    INSERT INTO notifications_fts (rowid, app_name, summary, body)
    VALUES (new.id, new.app_name, new.summary, new.body);
END;

CREATE TRIGGER notifications_ad AFTER DELETE ON notifications BEGIN
    INSERT INTO notifications_fts (notifications_fts, rowid, app_name, summary, body)
    VALUES ('delete', old.id, old.app_name, old.summary, old.body);
END;

CREATE TRIGGER notifications_au AFTER UPDATE ON notifications BEGIN
    INSERT INTO notifications_fts (notifications_fts, rowid, app_name, summary, body)
    VALUES ('delete', old.id, old.app_name, old.summary, old.body);
    INSERT INTO notifications_fts (rowid, app_name, summary, body)
    VALUES (new.id, new.app_name, new.summary, new.body);
END;

-- v2: script runs, snoozes, and cascade deletes.
CREATE TABLE runs (
    id              INTEGER PRIMARY KEY,
    notification_id INTEGER,
    rule            TEXT,
    trigger         TEXT CHECK (trigger IN ('rule', 'button')),
    button_label    TEXT,
    cmd             TEXT,                           -- JSON (str or [str])
    started_at      REAL,
    finished_at     REAL,
    exit_code       INTEGER,
    signal          INTEGER,
    duration_ms     INTEGER,
    output_tail     TEXT,
    skipped_reason  TEXT
);

CREATE INDEX idx_runs_notification_id ON runs (notification_id);

CREATE TABLE snoozes (
    notification_id INTEGER PRIMARY KEY,
    until           REAL NOT NULL
);

CREATE TRIGGER notifications_runs_ad AFTER DELETE ON notifications BEGIN
    DELETE FROM runs WHERE notification_id = old.id;
    DELETE FROM snoozes WHERE notification_id = old.id;
END;

PRAGMA user_version = 2;
