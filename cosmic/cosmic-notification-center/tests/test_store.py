import tempfile
import unittest
from pathlib import Path

from notifyd.model import CloseReason, Notification, State, Urgency
from notifyd.store import Store


def mk(id, summary="s", body="", app="App", at=100.0, state=State.SHOWN, **kw):
    n = Notification.from_notify(id, app, 0, "", summary, body, [], {}, -1, at)
    n.state = state
    for k, v in kw.items():
        setattr(n, k, v)
    return n


def fts_rows(store):
    return store._db.execute("SELECT COUNT(*) FROM notifications_fts").fetchone()[0]


def fts_ids(store, term):
    q = "SELECT rowid FROM notifications_fts WHERE notifications_fts MATCH ?"
    return [r[0] for r in store._db.execute(q, (term,))]


class UnseenCount(unittest.TestCase):
    def test_counts_shade_visible_rows_after_the_watermark(self):
        s = Store(":memory:")
        s.record(mk(1, at=10.0))
        s.record(mk(2, at=20.0, state=State.DND))
        s.record(mk(3, at=30.0, state=State.BLOCKED))
        s.record(mk(4, at=30.0, state=State.STORE_ONLY))
        s.record(mk(5, at=40.0))
        s.record(mk(6, at=50.0))
        self.assertEqual(s.unseen_count(0.0), 4)
        self.assertEqual(s.unseen_count(10.0), 3)  # strictly after
        self.assertEqual(s.unseen_count(50.0), 0)
        s.mark_closed(5, CloseReason.DISMISSED, 60.0)
        s.mark_closed(6, CloseReason.EXPIRED, 60.0)  # expired stays on the shade
        self.assertEqual(s.unseen_count(0.0), 3)
        s.mark_read([1], 70.0)  # reading does not matter
        self.assertEqual(s.unseen_count(0.0), 3)


class RoundTrip(unittest.TestCase):
    def test_every_field(self):
        s = Store(":memory:")
        n = Notification(
            id=5,
            app_name="Mail é",
            replaces_id=3,
            app_icon="mail-icon",
            summary="Sum ☃",
            body='<b>Body</b> "q"',
            actions=[("a", "A"), ("b", "B")],
            hints={"urgency": 2, "n": {"x": [1, 2.5, None, True]}, "image-path": "/p"},
            expire_timeout=1234,
            urgency=Urgency.CRITICAL,
            category="email.arrived",
            desktop_entry="org.x",
            received_at=1234.5,
            state=State.DND,
            tags=["t1", "t2"],
            classes=["c1"],
            highlights=[{"pattern": "x", "class": "urgent"}],
            timeout_ms=None,
            sound="/a.ogg",
            matched_rules=["r1", "r2"],
            read_at=2000.5,
            closed_at=2001.5,
            close_reason=CloseReason.DISMISSED,
        )
        s.record(n)
        got = s.get(5)
        self.assertEqual(got, n)
        self.assertIs(type(got.urgency), Urgency)
        self.assertIs(type(got.state), State)
        self.assertIs(type(got.close_reason), CloseReason)
        self.assertEqual(got.actions, [("a", "A"), ("b", "B")])

    def test_defaults_and_unknown(self):
        s = Store(":memory:")
        n = mk(1)
        s.record(n)
        self.assertEqual(s.get(1), n)
        self.assertIsNone(s.get(99))


class Replace(unittest.TestCase):
    def test_in_place_with_fts(self):
        s = Store(":memory:")
        s.record(mk(1, summary="alpha", body="one"))
        s.record(mk(1, summary="beta", body="two"))
        self.assertEqual(s._db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 1)
        self.assertEqual(s.get(1).summary, "beta")
        self.assertEqual(fts_rows(s), 1)
        self.assertEqual(s.history("alpha"), [])
        self.assertEqual([n.id for n in s.history("beta")], [1])
        self.assertEqual(fts_ids(s, "alpha"), [])


class Ids(unittest.TestCase):
    def test_seed_across_reopen(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sub" / "dir" / "h.db"
            s = Store(path)
            self.assertEqual([s.next_id(), s.next_id()], [1, 2])
            s.record(mk(s.next_id()))
            s.record(mk(10))
            s.close()
            s = Store(path)
            self.assertEqual(s.next_id(), 11)
            self.assertEqual(s.next_id(), 12)
            self.assertEqual(s._db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            s.close()

    def test_empty_starts_at_one_and_monotonic_after_clear(self):
        s = Store(":memory:")
        self.assertEqual(s.next_id(), 1)
        s.record(mk(1))
        s.clear()
        self.assertEqual(s.next_id(), 2)


class Unread(unittest.TestCase):
    def test_semantics(self):
        s = Store(":memory:")
        s.record(mk(1, state=State.SHOWN))
        s.record(mk(2, state=State.DND))
        s.record(mk(3, state=State.BLOCKED))
        s.record(mk(4, state=State.STORE_ONLY))
        self.assertEqual(s.unread_count(), 2)
        s.mark_read([1, 3, 4, 404], 50.0)
        self.assertEqual(s.unread_count(), 1)
        s.mark_read([1], 99.0)
        self.assertEqual(s.get(1).read_at, 50.0)  # only where NULL
        s.mark_read([], 1.0)
        s.mark_read([2], 60.0)
        self.assertEqual(s.unread_count(), 0)

    def test_mark_closed(self):
        s = Store(":memory:")
        s.record(mk(1))
        s.mark_closed(1, CloseReason.EXPIRED, 7.0)
        s.mark_closed(404, CloseReason.EXPIRED, 7.0)
        n = s.get(1)
        self.assertEqual((n.closed_at, n.close_reason), (7.0, CloseReason.EXPIRED))
        self.assertIsNone(s.get(404))


class History(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")

    def test_order_and_paging(self):
        for i, at in [(1, 10.0), (2, 30.0), (3, 30.0), (4, 20.0)]:
            self.s.record(mk(i, at=at))
        self.assertEqual([n.id for n in self.s.history()], [3, 2, 4, 1])
        self.assertEqual([n.id for n in self.s.history(limit=2)], [3, 2])
        self.assertEqual([n.id for n in self.s.history(offset=2, limit=2)], [4, 1])
        self.assertEqual(self.s.history(offset=10), [])
        self.assertEqual(len(self.s.history("   ")), 4)

    def test_search(self):
        self.s.record(mk(1, app="Example App", summary="URGENT deploy", at=1.0))
        self.s.record(mk(2, app="Mail", summary="hello", body="meeting tomorrow", at=2.0))
        self.s.record(mk(3, app="Mail", summary="Re: urgent", at=3.0))
        ids = lambda q, **k: [n.id for n in self.s.history(q, **k)]  # noqa: E731
        self.assertEqual(ids("example urg"), [1])
        self.assertEqual(ids("URGENT"), [3, 1])
        self.assertEqual(ids("urg"), [3, 1])
        self.assertEqual(ids("MEET"), [2])
        self.assertEqual(ids("mail"), [3, 2])
        self.assertEqual(ids("mail", limit=1, offset=1), [2])
        self.assertEqual(ids("nomatchxyz"), [])
        self.assertEqual(ids("example nomatchxyz"), [])

    def test_hostile_queries(self):
        for i in range(3):
            self.s.record(mk(i + 1, summary=f"note {i}", body="plain"))
        hostile = ['"', "*", "AND (", "NEAR(", '""', "a OR", "( ) :", "summary:note"]
        hostile += ["-x", "^", "'"]
        for q in hostile:
            with self.subTest(q=q):
                res = self.s.history(q)
                self.assertLess(len(res), 3, q)
        self.s.record(mk(9, body="this and that near it"))
        self.assertEqual([n.id for n in self.s.history("AND (")], [9])  # words, not operators
        self.assertEqual([n.id for n in self.s.history("NEAR(")], [9])
        self.assertEqual(len(self.s.history('"note')), 3)


class Prune(unittest.TestCase):
    def test_prune(self):
        s = Store(":memory:")
        day = 86400.0
        now = 100 * day
        s.record(mk(1, at=now - 40 * day, summary="old"))
        s.record(mk(2, at=now - 10 * day))
        s.record(mk(3, at=now))
        self.assertEqual(s.prune(None, now), 0)
        self.assertEqual(s.prune(30, now), 1)
        self.assertIsNone(s.get(1))
        self.assertEqual(fts_rows(s), 2)
        self.assertEqual(s.history("old"), [])
        self.assertEqual(s.prune(30, now), 0)
        self.assertEqual(s.prune(5, now), 1)
        self.assertEqual([n.id for n in s.history()], [3])


class DeleteClear(unittest.TestCase):
    def test_fts_consistent(self):
        s = Store(":memory:")
        for i, w in [(1, "apple"), (2, "banana"), (3, "cherry")]:
            s.record(mk(i, summary=w))
        s.mark_read([2], 1.0)  # update trigger must not corrupt FTS
        s.delete([1, 404])
        self.assertIsNone(s.get(1))
        self.assertEqual(fts_ids(s, "apple"), [])
        self.assertEqual(fts_ids(s, "banana"), [2])
        self.assertEqual(fts_rows(s), 2)
        s.delete([])
        s.clear()
        self.assertEqual(s.history(), [])
        self.assertEqual(fts_rows(s), 0)
        self.assertEqual(fts_ids(s, "banana"), [])
        s.record(mk(1, summary="apple"))
        self.assertEqual([n.id for n in s.history("apple")], [1])
        s._db.execute("INSERT INTO notifications_fts(notifications_fts) VALUES('integrity-check')")


class FilteredQuery(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")
        self.s.record(mk(1, "build ok", app="Terminal", at=100.0, tags=["ci"]))
        self.s.record(mk(2, "build failed", app="Terminal", at=200.0, tags=["ci", "red"]))
        self.s.record(mk(3, "hello", app="Mail", at=300.0, read_at=301.0))
        self.s.record(mk(4, "spam", app="mail", at=400.0, state=State.BLOCKED, tags=["red"]))
        self.s.record(mk(5, "later", app="Mail", at=500.0, state=State.DND))

    def ids(self, *a, **kw):
        return [n.id for n in self.s.query(*a, **kw)]

    def test_no_filters_matches_history(self):
        self.assertEqual(self.ids(), [5, 4, 3, 2, 1])
        self.assertEqual(self.ids(offset=1, limit=2), [4, 3])
        self.assertEqual([n.id for n in self.s.history()], [5, 4, 3, 2, 1])

    def test_app_is_exact(self):
        self.assertEqual(self.ids(app="Terminal"), [2, 1])
        self.assertEqual(self.ids(app="Mail"), [5, 3])
        self.assertEqual(self.ids(app="Nope"), [])

    def test_state(self):
        self.assertEqual(self.ids(state="blocked"), [4])
        self.assertEqual(self.ids(state="dnd"), [5])
        self.assertEqual(self.ids(state="store_only"), [])

    def test_tag_is_exact_element(self):
        self.assertEqual(self.ids(tag="ci"), [2, 1])
        self.assertEqual(self.ids(tag="red"), [4, 2])
        self.assertEqual(self.ids(tag="re"), [])

    def test_unread_only_matches_the_badge(self):
        self.assertEqual(self.ids(unread_only=True), [5, 2, 1])
        self.assertEqual(sorted(self.s.unread_ids()), [1, 2, 5])
        self.assertEqual(len(self.s.unread_ids()), self.s.unread_count())

    def test_filters_combine_with_search_and_paging(self):
        self.assertEqual(self.ids("build", app="Terminal", tag="red"), [2])
        self.assertEqual(self.ids("build", unread_only=True, limit=1), [2])
        self.assertEqual(self.ids("build", unread_only=True, limit=1, offset=1), [1])
        self.assertEqual(self.ids("*", app="Mail"), [])  # unsearchable query matches nothing
        self.assertEqual(self.ids("zzz"), [])

    def test_facets(self):
        self.assertEqual(self.s.apps(), ["Mail", "mail", "Terminal"])
        self.assertEqual(self.s.tags(), ["ci", "red"])
        self.assertEqual(Store(":memory:").apps(), [])


if __name__ == "__main__":
    unittest.main()


class ShadeQueries(unittest.TestCase):
    def setUp(self):
        s = self.s = Store(":memory:")
        s.record(mk(1, at=10.0))  # open
        s.record(mk(2, at=20.0, state=State.DND))  # open, held back by DND
        s.record(mk(3, at=30.0, closed_at=31.0, close_reason=CloseReason.EXPIRED))
        s.record(mk(4, at=40.0, closed_at=41.0, close_reason=CloseReason.DISMISSED))
        s.record(mk(5, at=50.0, closed_at=51.0, close_reason=CloseReason.CLOSED))
        s.record(mk(6, at=60.0, state=State.BLOCKED))
        s.record(mk(7, at=70.0, state=State.STORE_ONLY))
        s.record(mk(8, at=80.0, read_at=81.0))  # read items still show

    def test_membership_and_order(self):
        self.assertEqual([n.id for n in self.s.shade_items()], [8, 3, 2, 1])

    def test_limit(self):
        self.assertEqual([n.id for n in self.s.shade_items(2)], [8, 3])

    def test_mark_dismissed_quiet_only_touches_expired(self):
        self.s.mark_dismissed_quiet([1, 3, 4, 5, 99], 500.0)
        self.assertEqual([n.id for n in self.s.shade_items()], [8, 2, 1])
        n3 = self.s.get(3)
        self.assertEqual((n3.close_reason, n3.closed_at), (CloseReason.DISMISSED, 500.0))
        self.assertIsNone(self.s.get(1).closed_at)  # open: untouched
        self.assertEqual(self.s.get(4).closed_at, 41.0)  # already dismissed: untouched
        self.assertEqual(self.s.get(5).close_reason, CloseReason.CLOSED)
        self.assertEqual(self.s.get(5).closed_at, 51.0)

    def test_empty_ids(self):
        self.s.mark_dismissed_quiet([], 1.0)
        self.assertEqual(len(self.s.shade_items()), 4)
