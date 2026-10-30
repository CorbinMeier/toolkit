import sqlite3
import tempfile
import unittest
from pathlib import Path

from notifyd.model import Notification
from notifyd.store import Store

V1_SCHEMA = Path(__file__).with_name("data") / "schema_v1.sql"  # the exact v1 schema.sql

BUTTONS = [
    {"id": "b0", "label": "Open", "run": ["/bin/true"], "builtin": None, "minutes": None,
     "close": True},
    {"id": "b1", "label": "Later é", "run": None, "builtin": "snooze", "minutes": 10,
     "close": False},
]  # fmt: skip


def mk(id, summary="s", body="", at=100.0, **kw):
    n = Notification.from_notify(id, "App", 0, "", summary, body, [], {}, -1, at)
    for k, v in kw.items():
        setattr(n, k, v)
    return n


def make_v1(path):
    db = sqlite3.connect(path)
    db.executescript(V1_SCHEMA.read_text(encoding="utf-8"))
    for i, word in ((1, "alpha"), (2, "beta"), (3, "gamma")):
        db.execute(
            "INSERT INTO notifications (id, app_name, summary, body, tags, received_at, read_at)"
            " VALUES (?, 'App', ?, ?, '[\"t\"]', ?, ?)",
            (i, f"sum {word}", f"body {word}", 10.0 * i, 5.0 if i == 2 else None),
        )
    db.commit()
    db.close()


class Migration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "h.db"

    def tearDown(self):
        self._tmp.cleanup()

    def version(self, s):
        return s._db.execute("PRAGMA user_version").fetchone()[0]

    def test_fresh_is_v2(self):
        s = Store(self.path)
        self.assertEqual(self.version(s), 2)
        cols = [r[1] for r in s._db.execute("PRAGMA table_info(notifications)")]
        self.assertIn("buttons", cols)
        tables = {r[0] for r in s._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"runs", "snoozes"} <= tables)

    def test_v1_migrates_intact(self):
        make_v1(self.path)
        s = Store(self.path)
        self.assertEqual(self.version(s), 2)
        self.assertEqual([n.id for n in s.query()], [3, 2, 1])
        n2 = s.get(2)
        self.assertEqual(
            (n2.summary, n2.body, n2.tags, n2.read_at), ("sum beta", "body beta", ["t"], 5.0)
        )
        self.assertEqual(n2.buttons, [])
        self.assertEqual([n.id for n in s.query("gamma")], [3])  # FTS still answers
        # new rows after migration: FTS triggers, buttons, runs and cascade all work
        s.record(mk(4, summary="delta", buttons=BUTTONS))
        self.assertEqual([n.id for n in s.query("delta")], [4])
        self.assertEqual(s.get(4).buttons, BUTTONS)
        rid = s.add_run(1, rule="r", trigger="rule", cmd="x", started_at=1.0)
        s.snooze(1, 50.0)
        s.delete([1])
        self.assertEqual(s.runs_for(1), [])
        self.assertEqual(s.snoozed_ids(), [])
        self.assertEqual(s.query("alpha"), [])
        self.assertGreater(rid, 0)
        s.close()

    def test_idempotent_on_reopen(self):
        make_v1(self.path)
        Store(self.path).close()
        s = Store(self.path)  # would raise "duplicate column" if it migrated twice
        self.assertEqual(self.version(s), 2)
        self.assertEqual(len(s.query()), 3)
        s.close()
        self.assertEqual(self.version(Store(self.path)), 2)

    def test_failed_migration_rolls_back(self):
        make_v1(self.path)
        db = sqlite3.connect(self.path)
        db.execute("CREATE TABLE snoozes (x)")  # collides with the migration's last-but-two step
        db.commit()
        db.close()
        with self.assertRaises(sqlite3.OperationalError):
            Store(self.path)
        db = sqlite3.connect(self.path)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
        cols = [r[1] for r in db.execute("PRAGMA table_info(notifications)")]
        self.assertNotIn("buttons", cols)
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
        self.assertNotIn("runs", names)


class Buttons(unittest.TestCase):
    def test_round_trip_get_and_query(self):
        s = Store(":memory:")
        s.record(mk(1, buttons=BUTTONS))
        s.record(mk(2))
        self.assertEqual(s.get(1).buttons, BUTTONS)
        self.assertEqual(s.get(2).buttons, [])
        self.assertEqual({n.id: n.buttons for n in s.query()}, {1: BUTTONS, 2: []})
        s.record(mk(1, buttons=[]))  # upsert overwrites
        self.assertEqual(s.get(1).buttons, [])


class Runs(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")
        self.s.record(mk(1))
        self.s.record(mk(2))

    def test_add_finish_runs_for(self):
        rid = self.s.add_run(
            1, rule="r", trigger="button", cmd=["/bin/x", "a"], started_at=5.0, button_label="Go"
        )
        row = self.s.runs_for(1)[0]
        self.assertEqual(row["id"], rid)
        self.assertEqual(row["cmd"], ["/bin/x", "a"])
        self.assertEqual((row["trigger"], row["button_label"]), ("button", "Go"))
        self.assertIsNone(row["finished_at"])
        self.s.finish_run(rid, finished_at=6.0, exit_code=2, duration_ms=1000, output_tail="oops")
        row = self.s.runs_for(1)[0]
        self.assertEqual(
            (
                row["finished_at"],
                row["exit_code"],
                row["signal"],
                row["duration_ms"],
                row["output_tail"],
            ),
            (6.0, 2, None, 1000, "oops"),
        )
        self.s.finish_run(rid, finished_at=7.0, signal=9)
        self.assertEqual(self.s.runs_for(1)[0]["signal"], 9)
        self.assertEqual(self.s.runs_for(2), [])

    def test_order_and_skipped(self):
        a = self.s.add_run(1, rule="r", trigger="rule", cmd="a", started_at=1.0)
        b = self.s.add_run(
            1, rule="r", trigger="rule", cmd="a", started_at=2.0, skipped_reason="busy"
        )
        rows = self.s.runs_for(1)
        self.assertEqual([r["id"] for r in rows], [a, b])
        self.assertEqual(rows[1]["skipped_reason"], "busy")

    def test_trigger_check(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.add_run(1, rule="r", trigger="bogus", cmd="a", started_at=1.0)


class Snoozes(unittest.TestCase):
    def test_lifecycle(self):
        s = Store(":memory:")
        s.record(mk(1))
        s.record(mk(2))
        s.snooze(1, 100.0)
        s.snooze(2, 200.0)
        self.assertEqual(s.snoozed_ids(), [1, 2])
        self.assertEqual(s.due_snoozes(99.9), [])
        self.assertEqual(s.due_snoozes(100.0), [1])
        self.assertEqual(s.due_snoozes(500.0), [1, 2])
        s.snooze(1, 300.0)  # re-snooze replaces
        self.assertEqual(s.due_snoozes(250.0), [2])
        s.clear_snooze(2)
        s.clear_snooze(99)  # unknown: no error
        self.assertEqual(s.snoozed_ids(), [1])


class Cascade(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")
        for i in (1, 2, 3):
            self.s.record(mk(i, at=float(i)))
            self.s.add_run(i, rule="r", trigger="rule", cmd="c", started_at=1.0)
            self.s.snooze(i, 9.0)

    def left(self):
        return (
            [
                r[0]
                for r in self.s._db.execute("SELECT DISTINCT notification_id FROM runs ORDER BY 1")
            ],
            self.s.snoozed_ids(),
        )

    def test_delete(self):
        self.s.delete([1, 3])
        self.assertEqual(self.left(), ([2], [2]))

    def test_clear(self):
        self.s.clear()
        self.assertEqual(self.left(), ([], []))

    def test_prune(self):
        self.assertEqual(self.s.prune(1, 2.5 + 86400), 2)  # ids 1 and 2 are older than the cutoff
        self.assertEqual(self.left(), ([3], [3]))


if __name__ == "__main__":
    unittest.main()
