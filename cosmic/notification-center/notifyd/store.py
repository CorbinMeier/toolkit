"""SQLite history store, schema v2 (v1 databases migrate on open).

Single-threaded use from the GLib main loop: no locking.

Decisions where the API was silent:
- `record` overwrites every column from the given Notification (read_at/closed_at included).
- `mark_closed` overwrites an earlier close; `next_id` keeps counting after `clear()`/`delete()`
  within one process (ids are not reused until a reopen of an emptied database).
- A non-blank query with no searchable token (e.g. `"` or `*`) matches nothing.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from notifyd.model import CloseReason, Notification, State, Urgency

SCHEMA = Path(__file__).with_name("schema.sql")

_UPSERT = """
INSERT INTO notifications (
    id, app_name, replaces_id, app_icon, summary, body, actions, hints, expire_timeout,
    urgency, category, desktop_entry, received_at, state, tags, classes, highlights,
    timeout_ms, sound, matched_rules, read_at, closed_at, close_reason, buttons
) VALUES (
    :id, :app_name, :replaces_id, :app_icon, :summary, :body, :actions, :hints, :expire_timeout,
    :urgency, :category, :desktop_entry, :received_at, :state, :tags, :classes, :highlights,
    :timeout_ms, :sound, :matched_rules, :read_at, :closed_at, :close_reason, :buttons
) ON CONFLICT(id) DO UPDATE SET
    app_name=excluded.app_name, replaces_id=excluded.replaces_id, app_icon=excluded.app_icon,
    summary=excluded.summary, body=excluded.body, actions=excluded.actions,
    hints=excluded.hints, expire_timeout=excluded.expire_timeout, urgency=excluded.urgency,
    category=excluded.category, desktop_entry=excluded.desktop_entry,
    received_at=excluded.received_at, state=excluded.state, tags=excluded.tags,
    classes=excluded.classes, highlights=excluded.highlights, timeout_ms=excluded.timeout_ms,
    sound=excluded.sound, matched_rules=excluded.matched_rules, read_at=excluded.read_at,
    closed_at=excluded.closed_at, close_reason=excluded.close_reason, buttons=excluded.buttons
"""


SCHEMA_VERSION = 2

# v1 -> v2, run in one transaction by Store._migrate. Keep equivalent to the v2 parts of schema.sql.
_MIGRATE_V2 = (
    "ALTER TABLE notifications ADD COLUMN buttons TEXT NOT NULL DEFAULT '[]'",
    """CREATE TABLE runs (
        id              INTEGER PRIMARY KEY,
        notification_id INTEGER,
        rule            TEXT,
        trigger         TEXT CHECK (trigger IN ('rule', 'button')),
        button_label    TEXT,
        cmd             TEXT,
        started_at      REAL,
        finished_at     REAL,
        exit_code       INTEGER,
        signal          INTEGER,
        duration_ms     INTEGER,
        output_tail     TEXT,
        skipped_reason  TEXT
    )""",
    "CREATE INDEX idx_runs_notification_id ON runs (notification_id)",
    "CREATE TABLE snoozes (notification_id INTEGER PRIMARY KEY, until REAL NOT NULL)",
    """CREATE TRIGGER notifications_runs_ad AFTER DELETE ON notifications BEGIN
        DELETE FROM runs WHERE notification_id = old.id;
        DELETE FROM snoozes WHERE notification_id = old.id;
    END""",
    "PRAGMA user_version = 2",
)


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _fts_query(text: str) -> str | None:
    """Quote each whitespace token as a phrase with a prefix star. None: nothing searchable."""
    parts = []
    for tok in text.split():
        if any(c.isalnum() for c in tok):
            parts.append('"' + tok.replace('"', '""') + '"*')
    return " ".join(parts) or None


def _from_row(r: sqlite3.Row) -> Notification:
    n = Notification(
        id=r["id"],
        app_name=r["app_name"],
        replaces_id=r["replaces_id"],
        app_icon=r["app_icon"],
        summary=r["summary"],
        body=r["body"],
        actions=[(k, v) for k, v in json.loads(r["actions"])],
        hints=json.loads(r["hints"]),
        expire_timeout=r["expire_timeout"],
        urgency=Urgency(r["urgency"]),
        category=r["category"],
        desktop_entry=r["desktop_entry"],
        received_at=r["received_at"],
        state=State(r["state"]),
        tags=json.loads(r["tags"]),
        classes=json.loads(r["classes"]),
        highlights=json.loads(r["highlights"]),
        timeout_ms=r["timeout_ms"],
        sound=r["sound"],
        matched_rules=json.loads(r["matched_rules"]),
        read_at=r["read_at"],
        closed_at=r["closed_at"],
        close_reason=None if r["close_reason"] is None else CloseReason(r["close_reason"]),
    )
    # Set by attribute: the field lives on Notification only once the rules work is merged.
    n.buttons = json.loads(r["buttons"])
    return n


class Store:
    def __init__(self, path: str | os.PathLike) -> None:
        spec = os.fspath(path)
        if spec != ":memory:":
            Path(spec).expanduser().parent.mkdir(parents=True, exist_ok=True)
            spec = str(Path(spec).expanduser())
        self._db = sqlite3.connect(spec)
        self._db.row_factory = sqlite3.Row
        if spec != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            self._db.executescript(SCHEMA.read_text(encoding="utf-8"))
        elif version == 1:
            self._migrate_v2()
        row = self._db.execute("SELECT MAX(id) FROM notifications").fetchone()
        self._next = max(1, (row[0] or 0) + 1)

    def _migrate_v2(self) -> None:
        """v1 -> v2 atomically: any failure rolls back and leaves the v1 database untouched."""
        db = self._db
        db.isolation_level = None  # manual transaction control
        try:
            db.execute("BEGIN IMMEDIATE")
            for stmt in _MIGRATE_V2:
                db.execute(stmt)
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.isolation_level = ""

    def next_id(self) -> int:
        n = self._next
        self._next += 1
        return n

    def record(self, n: Notification) -> None:
        params = {
            "id": n.id,
            "app_name": n.app_name,
            "replaces_id": n.replaces_id,
            "app_icon": n.app_icon,
            "summary": n.summary,
            "body": n.body,
            "actions": _dumps([[k, v] for k, v in n.actions]),
            "hints": _dumps(n.hints),
            "expire_timeout": n.expire_timeout,
            "urgency": int(n.urgency),
            "category": n.category,
            "desktop_entry": n.desktop_entry,
            "received_at": n.received_at,
            "state": State(n.state).value,
            "tags": _dumps(n.tags),
            "classes": _dumps(n.classes),
            "highlights": _dumps(n.highlights),
            "timeout_ms": n.timeout_ms,
            "sound": n.sound,
            "matched_rules": _dumps(n.matched_rules),
            "read_at": n.read_at,
            "closed_at": n.closed_at,
            "close_reason": None if n.close_reason is None else int(n.close_reason),
            "buttons": _dumps(getattr(n, "buttons", [])),
        }
        with self._db:
            self._db.execute(_UPSERT, params)
        # A recorded id from outside next_id() must not be handed out again.
        if n.id >= self._next:
            self._next = n.id + 1

    def get(self, id: int) -> Notification | None:
        r = self._db.execute("SELECT * FROM notifications WHERE id = ?", (id,)).fetchone()
        return None if r is None else _from_row(r)

    def existing_ids(self, ids: Iterable[int]) -> set[int]:
        """Which of `ids` are still stored."""
        ids = list(ids)
        found: set[int] = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ",".join("?" * len(chunk))
            rows = self._db.execute(f"SELECT id FROM notifications WHERE id IN ({marks})", chunk)
            found.update(r[0] for r in rows)
        return found

    def set_hint(self, id: int, key: str, value: object) -> bool:
        """Set one key inside the stored `hints` JSON (no migration). False for an unknown id."""
        r = self._db.execute("SELECT hints FROM notifications WHERE id = ?", (id,)).fetchone()
        if r is None:
            return False
        hints = json.loads(r["hints"])
        hints[key] = value
        with self._db:
            self._db.execute("UPDATE notifications SET hints = ? WHERE id = ?", (_dumps(hints), id))
        return True

    def mark_closed(self, id: int, reason: CloseReason, at: float) -> None:
        with self._db:
            self._db.execute(
                "UPDATE notifications SET closed_at = ?, close_reason = ? WHERE id = ?",
                (at, int(reason), id),
            )

    def mark_read(self, ids: Iterable[int], at: float) -> None:
        with self._db:
            self._db.executemany(
                "UPDATE notifications SET read_at = ? WHERE id = ? AND read_at IS NULL",
                [(at, i) for i in ids],
            )

    def delete(self, ids: Iterable[int]) -> None:
        with self._db:
            self._db.executemany("DELETE FROM notifications WHERE id = ?", [(i,) for i in ids])

    def clear(self) -> None:
        with self._db:
            self._db.execute("DELETE FROM notifications")

    def unread_count(self) -> int:
        return self._db.execute(
            "SELECT COUNT(*) FROM notifications WHERE read_at IS NULL AND state IN ('shown', 'dnd')"
        ).fetchone()[0]

    def unseen_count(self, since: float) -> int:
        """Notifications that arrived after `since` and are still on the shade: state shown or
        dnd, not dismissed (open, or closed only by expiry) and not snoozed, like `shade_items`."""
        return self._db.execute(
            "SELECT COUNT(*) FROM notifications WHERE received_at > ?"
            " AND state IN ('shown', 'dnd')"
            " AND (closed_at IS NULL OR close_reason = ?)"
            " AND id NOT IN (SELECT notification_id FROM snoozes)",
            (since, int(CloseReason.EXPIRED)),
        ).fetchone()[0]

    def history(self, query: str = "", offset: int = 0, limit: int = 50) -> list[Notification]:
        return self.query(query, offset, limit)

    def query(
        self,
        query: str = "",
        offset: int = 0,
        limit: int = 50,
        *,
        app: str | None = None,
        state: str | None = None,
        tag: str | None = None,
        unread_only: bool = False,
    ) -> list[Notification]:
        """Newest first. `query` is full-text (FTS5); the filters combine with AND.

        `app` is an exact app name, `state` one of State, `tag` one exact tag, and `unread_only`
        means what the badge counts: unread and in state shown or dnd. Falsy filters are off.
        """
        where: list[str] = []
        args: list[object] = []
        if query.strip():
            match = _fts_query(query)
            if match is None:
                return []
            where.append(
                "id IN (SELECT rowid FROM notifications_fts WHERE notifications_fts MATCH ?)"
            )
            args.append(match)
        if app:
            where.append("app_name = ?")
            args.append(app)
        if state:
            where.append("state = ?")
            args.append(state)
        if tag:
            where.append("EXISTS (SELECT 1 FROM json_each(notifications.tags) WHERE value = ?)")
            args.append(tag)
        if unread_only:
            where.append("read_at IS NULL AND state IN ('shown', 'dnd')")
        sql = "SELECT * FROM notifications"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY received_at DESC, id DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        try:
            return [_from_row(r) for r in self._db.execute(sql, args)]
        except sqlite3.OperationalError:
            return []

    def shade_items(self, limit: int = 200) -> list[Notification]:
        """What the dashboard shade lists, newest first: state shown or dnd, and not closed or
        closed only by expiry (an expired toast stays until it is dismissed). Snoozed notifications
        are left out until their snooze is cleared."""
        rows = self._db.execute(
            "SELECT * FROM notifications WHERE state IN ('shown', 'dnd')"
            " AND (closed_at IS NULL OR close_reason = ?)"
            " AND id NOT IN (SELECT notification_id FROM snoozes)"
            " ORDER BY received_at DESC, id DESC LIMIT ?",
            (int(CloseReason.EXPIRED), limit),
        )
        return [_from_row(r) for r in rows]

    def mark_dismissed_quiet(self, ids: Iterable[int], at: float) -> None:
        """Turn rows already closed by EXPIRED into DISMISSED (they leave the shade). No bus
        signal: the expiry was signalled already. Open and otherwise-closed rows are untouched."""
        with self._db:
            self._db.executemany(
                "UPDATE notifications SET close_reason = ?, closed_at = ?"
                " WHERE id = ? AND closed_at IS NOT NULL AND close_reason = ?",
                [(int(CloseReason.DISMISSED), at, i, int(CloseReason.EXPIRED)) for i in ids],
            )

    def apps(self) -> list[str]:
        """Distinct app names, case-insensitively sorted (for the window's filter)."""
        rows = self._db.execute(
            "SELECT DISTINCT app_name FROM notifications ORDER BY app_name COLLATE NOCASE, app_name"
        )
        return [r[0] for r in rows]

    def tags(self) -> list[str]:
        """Distinct tags across all notifications, sorted."""
        rows = self._db.execute(
            "SELECT DISTINCT j.value FROM notifications, json_each(notifications.tags) AS j"
            " ORDER BY j.value COLLATE NOCASE, j.value"
        )
        return [r[0] for r in rows]

    def unread_ids(self) -> list[int]:
        """Ids counted by `unread_count`."""
        rows = self._db.execute(
            "SELECT id FROM notifications WHERE read_at IS NULL AND state IN ('shown', 'dnd')"
        )
        return [r[0] for r in rows]

    def prune(self, retention_days: int | None, now: float) -> int:
        if retention_days is None:
            return 0
        with self._db:
            cur = self._db.execute(
                "DELETE FROM notifications WHERE received_at < ?",
                (now - retention_days * 86400,),
            )
        return cur.rowcount

    # --- script runs (schema v2) ---

    def add_run(
        self,
        notification_id: int,
        *,
        rule: str | None,
        trigger: str,
        cmd: str | list[str],
        started_at: float,
        button_label: str | None = None,
        finished_at: float | None = None,
        exit_code: int | None = None,
        signal: int | None = None,
        duration_ms: int | None = None,
        output_tail: str | None = None,
        skipped_reason: str | None = None,
    ) -> int:
        with self._db:
            cur = self._db.execute(
                "INSERT INTO runs (notification_id, rule, trigger, button_label, cmd, started_at,"
                " finished_at, exit_code, signal, duration_ms, output_tail, skipped_reason)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    notification_id,
                    rule,
                    trigger,
                    button_label,
                    _dumps(cmd),
                    started_at,
                    finished_at,
                    exit_code,
                    signal,
                    duration_ms,
                    output_tail,
                    skipped_reason,
                ),
            )
        return int(cur.lastrowid)

    def finish_run(
        self,
        id: int,
        *,
        finished_at: float,
        exit_code: int | None = None,
        signal: int | None = None,
        duration_ms: int | None = None,
        output_tail: str | None = None,
    ) -> None:
        with self._db:
            self._db.execute(
                "UPDATE runs SET finished_at = ?, exit_code = ?, signal = ?, duration_ms = ?,"
                " output_tail = ? WHERE id = ?",
                (finished_at, exit_code, signal, duration_ms, output_tail, id),
            )

    def runs_for(self, notification_id: int) -> list[dict]:
        """Runs of one notification, oldest first; `cmd` is decoded from its JSON."""
        rows = self._db.execute(
            "SELECT * FROM runs WHERE notification_id = ? ORDER BY id", (notification_id,)
        )
        out = []
        for r in rows:
            d = dict(r)
            d["cmd"] = None if d["cmd"] is None else json.loads(d["cmd"])
            out.append(d)
        return out

    # --- snoozes (schema v2) ---

    def snooze(self, id: int, until: float) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO snoozes (notification_id, until) VALUES (?, ?)"
                " ON CONFLICT(notification_id) DO UPDATE SET until = excluded.until",
                (id, until),
            )

    def due_snoozes(self, now: float) -> list[int]:
        rows = self._db.execute(
            "SELECT notification_id FROM snoozes WHERE until <= ? ORDER BY until, notification_id",
            (now,),
        )
        return [r[0] for r in rows]

    def clear_snooze(self, id: int) -> None:
        with self._db:
            self._db.execute("DELETE FROM snoozes WHERE notification_id = ?", (id,))

    def snoozed_ids(self) -> list[int]:
        rows = self._db.execute("SELECT notification_id FROM snoozes ORDER BY notification_id")
        return [r[0] for r in rows]

    def close(self) -> None:
        self._db.close()
