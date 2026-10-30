import sqlite3
import unittest
from pathlib import Path

from gi.repository import GLib

from notifyd.model import (
    CloseReason,
    Notification,
    State,
    TimeoutPolicy,
    Urgency,
    hints_from_variants,
    resolve_timeout,
    to_web,
)

ROOT = Path(__file__).resolve().parent.parent


def make(expire=-1, hints=None, actions=None):
    return Notification.from_notify(
        7, "App", 0, "icon", "Sum", "<b>Body</b>", actions or [], hints or {}, expire, 100.0
    )


class Enums(unittest.TestCase):
    def test_values(self):
        self.assertEqual([u.value for u in Urgency], [0, 1, 2])
        self.assertEqual([r.value for r in CloseReason], [1, 2, 3, 4])
        self.assertEqual(State.STORE_ONLY, "store_only")


class Timeout(unittest.TestCase):
    def test_defaults_by_urgency(self):
        self.assertEqual(make(-1, {"urgency": 0}).timeout_ms, 5000)
        self.assertEqual(make(-1).timeout_ms, 10000)
        self.assertIsNone(make(-1, {"urgency": 2}).timeout_ms)

    def test_zero_never_and_explicit(self):
        self.assertIsNone(make(0).timeout_ms)
        self.assertEqual(make(1234).timeout_ms, 1234)
        self.assertEqual(make(1234).expire_timeout, 1234)

    def test_urgency_derivation(self):
        self.assertEqual(make(hints={"urgency": 2}).urgency, Urgency.CRITICAL)
        self.assertEqual(make(hints={"urgency": 9}).urgency, Urgency.NORMAL)
        self.assertEqual(make().urgency, Urgency.NORMAL)

    def test_category_and_desktop_entry(self):
        n = make(hints={"category": "email.arrived", "desktop-entry": "org.x"})
        self.assertEqual((n.category, n.desktop_entry), ("email.arrived", "org.x"))
        self.assertEqual((make().category, make().desktop_entry), ("", ""))


class Actions(unittest.TestCase):
    def test_pairing(self):
        n = make(actions=["a", "A", "b", "B"])
        self.assertEqual(n.actions, [("a", "A"), ("b", "B")])

    def test_dangling_dropped(self):
        self.assertEqual(make(actions=["a", "A", "b"]).actions, [("a", "A")])


class Hints(unittest.TestCase):
    def test_unpack_and_drop_images(self):
        v = {
            "urgency": GLib.Variant("y", 2),
            "category": GLib.Variant("s", "x"),
            "image-path": GLib.Variant("s", "/tmp/a.png"),
            "image-data": GLib.Variant("(iiibiiay)", (1, 1, 4, True, 8, 4, b"\0\0\0\0")),
            "image_data": GLib.Variant("(iiibiiay)", (1, 1, 4, True, 8, 4, b"\0\0\0\0")),
            "icon_data": GLib.Variant("(iiibiiay)", (1, 1, 4, True, 8, 4, b"\0\0\0\0")),
            "pos": GLib.Variant("(ii)", (1, 2)),
        }
        h = hints_from_variants(v)
        self.assertEqual(
            h, {"urgency": 2, "category": "x", "image-path": "/tmp/a.png", "pos": [1, 2]}
        )


class Web(unittest.TestCase):
    def test_shape(self):
        n = make(actions=["a", "A"], hints={"urgency": 0, "category": "c"})
        n.tags, n.classes = ["t"], ["k"]
        n.highlights = [{"pattern": "x", "class": "urgent"}]
        runs = [{"text": "Body", "b": True}]
        w = to_web(n, runs)
        self.assertEqual(
            w,
            {
                "id": 7,
                "app": "App",
                "summary": "Sum",
                "body": runs,
                "icon": "icon",
                "urgency": "low",
                "category": "c",
                "tags": ["t"],
                "classes": ["k"],
                "highlights": [{"pattern": "x", "class": "urgent"}],
                "actions": [{"key": "a", "label": "A"}],
                "timeout_ms": 5000,
                "received_at": 100.0,
                "buttons": [],
                "reply": None,
            },
        )


class Schema(unittest.TestCase):
    def test_load_and_fts(self):
        db = sqlite3.connect(":memory:")
        db.executescript((ROOT / "notifyd" / "schema.sql").read_text())
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
        db.execute(
            "INSERT INTO notifications (id, app_name, summary, body, received_at)"
            " VALUES (1, 'Mail', 'Hello world', 'quarterly report', 1.0)"
        )
        q = "SELECT rowid FROM notifications_fts WHERE notifications_fts MATCH ?"
        self.assertEqual(db.execute(q, ("quarterly",)).fetchall(), [(1,)])
        db.execute("UPDATE notifications SET body = 'weekly digest' WHERE id = 1")
        self.assertEqual(db.execute(q, ("quarterly",)).fetchall(), [])
        self.assertEqual(db.execute(q, ("digest",)).fetchall(), [(1,)])
        db.execute("DELETE FROM notifications WHERE id = 1")
        self.assertEqual(db.execute(q, ("digest",)).fetchall(), [])


if __name__ == "__main__":
    unittest.main()


class TimeoutPolicyTests(unittest.TestCase):
    def test_each_urgency_default(self):
        p = TimeoutPolicy(1000, 2000, 3000)
        self.assertEqual([p.resolve(-1, u) for u in Urgency], [1000, 2000, 3000])

    def test_never_default(self):
        p = TimeoutPolicy(None, None, None)
        self.assertEqual([p.resolve(-1, u) for u in Urgency], [None, None, None])

    def test_app_timeout_respected(self):
        p = TimeoutPolicy(1000, 2000, None, respect_app=True)
        self.assertEqual(p.resolve(7777, Urgency.CRITICAL), 7777)
        self.assertEqual(p.resolve(7777, Urgency.LOW), 7777)

    def test_app_timeout_ignored_when_not_respected(self):
        p = TimeoutPolicy(1000, 2000, None, respect_app=False)
        self.assertEqual(p.resolve(7777, Urgency.LOW), 1000)
        self.assertEqual(p.resolve(7777, Urgency.CRITICAL), None)

    def test_zero_always_never(self):
        for respect in (True, False):
            p = TimeoutPolicy(1000, 2000, 3000, respect_app=respect)
            self.assertIsNone(p.resolve(0, Urgency.NORMAL))

    def test_minus_one_uses_default(self):
        for respect in (True, False):
            p = TimeoutPolicy(1000, 2000, 3000, respect_app=respect)
            self.assertEqual(p.resolve(-1, Urgency.NORMAL), 2000)
            self.assertEqual(p.resolve(-5, Urgency.NORMAL), 2000)

    def test_from_notify_uses_policy(self):
        p = TimeoutPolicy(1, 2, 3)
        n = Notification.from_notify(1, "a", 0, "", "s", "b", [], {"urgency": 2}, -1, 0.0, p)
        self.assertEqual(n.timeout_ms, 3)

    def test_update_in_place(self):
        p = TimeoutPolicy()
        p.update(TimeoutPolicy(9, None, 8, False))
        self.assertEqual(p, TimeoutPolicy(9, None, 8, False))

    def test_old_resolve_timeout_signature(self):
        self.assertEqual(resolve_timeout(-1, Urgency.NORMAL), 10000)
        self.assertEqual(resolve_timeout(5, Urgency.NORMAL), 5)
        self.assertIsNone(resolve_timeout(0, Urgency.LOW))
