"""UnseenCount: the "new since you last looked" badge (real Store, fake windows, private bus)."""

import tempfile
import unittest
from pathlib import Path

from gi.repository import GLib

from notifyd.__main__ import Config, Runtime, build_daemon
from notifyd.dbus_server import CTL_IFACE, CTL_PATH, PROPS_IFACE
from notifyd.model import CloseReason
from notifyd.rules import RuleSet
from notifyd.seen import SeenMark
from notifyd.sound import NullPlayer
from tests.test_integration import pump_briefly
from tests.test_wiring import WiringCase

RULES = (
    '[[rule]]\nname = "b"\nsummary = "block"\nblock = true\n'
    '[[rule]]\nname = "s"\nsummary = "only"\nstore_only = true\n'
)


class Window:
    """A fake shade/center with a visibility flag, as the real ones have."""

    def __init__(self):
        self.visible = False
        self.calls = []

    def is_visible(self):
        return self.visible

    def show(self):
        self.calls.append("show")
        self.visible = True

    def hide(self):
        self.visible = False

    def toggle(self):
        self.calls.append("toggle")
        self.visible = not self.visible

    def show_notification(self, id):
        self.calls.append(("show_notification", id))
        self.visible = True

    def notify_added(self, n=None):
        pass

    def refresh(self):
        pass

    def set_dnd(self, on):
        pass

    def set_user_css(self, css):
        pass


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        self.t += 1.0
        return self.t


class UnseenCase(WiringCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.clock = FakeClock()

    def tearDown(self):
        super().tearDown()
        self.tmp.cleanup()

    def build(self, rules_text=RULES, state=True):
        rules = RuleSet.from_toml(rules_text)
        self.seen = SeenMark(self.state if state else None, self.clock())
        self.shade, self.center_w = Window(), Window()
        self.runtime = Runtime(
            self.state / "config.toml", self.display, Config(), player=NullPlayer()
        )
        self.server = build_daemon(
            self.server_conn,
            self.store,
            self.display,
            lambda server: self.center_w,
            lambda server, runtime: self.shade,
            self.runtime,
            process=rules.apply,
            clock=self.clock,
            seen=self.seen,
        )
        return self.server

    def unseen(self):
        r = self.c.call(
            CTL_PATH, PROPS_IFACE, "Get", GLib.Variant("(ss)", (CTL_IFACE, "UnseenCount")), "(v)"
        )
        return r.unpack()[0]

    def unseen_signals(self):
        pump_briefly(50)
        return [
            p[1]["UnseenCount"]
            for n, p in self.signals
            if n == "PropertiesChanged" and "UnseenCount" in p[1]
        ]


class UnseenSnoozed(unittest.TestCase):
    def test_snoozed_rows_are_not_unseen_until_the_snooze_clears(self):
        from notifyd.store import Store
        from tests.test_store import mk

        s = Store(":memory:")
        s.record(mk(1, at=10.0))
        s.record(mk(2, at=20.0))
        s.snooze(1, 999.0)  # the shade hides it, so the badge must not count it
        self.assertEqual(s.unseen_count(0.0), 1)
        s.clear_snooze(1)
        self.assertEqual(s.unseen_count(0.0), 2)


class UnseenTests(UnseenCase):
    def test_new_notifications_increment_but_blocked_and_store_only_do_not(self):
        self.build()
        self.assertEqual(self.unseen(), 0)
        self.notify("a")
        self.notify("b")
        self.notify("block me")
        self.notify("only me")
        self.assertEqual(self.unseen(), 2)
        self.assertEqual(self.unseen_signals(), [1, 2])

    def test_opening_the_shade_resets(self):
        self.build()
        self.notify("a")
        self.ctl("ToggleShade")
        self.assertTrue(self.shade.visible)
        self.assertEqual(self.unseen(), 0)
        self.assertEqual(self.unseen_signals(), [1, 0])
        self.notify("b")
        self.assertEqual(self.unseen(), 1)
        self.ctl("ToggleShade")  # closing does not count as looking
        self.assertFalse(self.shade.visible)
        self.assertEqual(self.unseen(), 1)
        self.ctl("ShowShade")
        self.assertEqual(self.unseen(), 0)

    def test_center_entry_points_reset(self):
        self.build()
        for call in (
            lambda: self.ctl("ShowCenter"),
            lambda: self.ctl("ToggleCenter"),
            lambda: self.ctl("ShowNotification", GLib.Variant("(u)", (1,))),
            lambda: self.center_w.show(),  # what the shade's "Open notification center" calls
        ):
            self.center_w.hide()
            self.notify("x")
            self.assertEqual(self.unseen(), 1)
            call()
            self.assertEqual(self.unseen(), 0)

    def test_shade_callbacks_reach_the_wrapped_center(self):
        self.build()
        self.assertEqual(self.server.center.show, self.center_w.show)
        self.notify("x")
        self.display.on_details(1)
        self.assertEqual(self.unseen(), 0)

    def test_mark_seen_method(self):
        self.build()
        self.notify("a")
        self.assertEqual(self.unseen(), 1)
        self.ctl("MarkSeen")
        self.assertEqual(self.unseen(), 0)
        self.assertEqual(self.unseen_signals(), [1, 0])
        self.ctl("MarkSeen")  # no change, no extra signal
        self.assertEqual(self.unseen_signals(), [1, 0])

    def test_unread_count_is_unchanged_by_seeing(self):
        self.build()
        self.notify("a")
        self.ctl("MarkSeen")
        self.assertEqual(self.unread(), 1)

    def test_dismissing_an_unseen_notification_decrements(self):
        self.build()
        a, _b = self.notify("a"), self.notify("b")
        self.assertEqual(self.unseen(), 2)
        self.ctl("Dismiss", GLib.Variant("(u)", (a,)))
        self.assertEqual(self.unseen(), 1)
        self.server.on_closed(_b, CloseReason.EXPIRED)  # expiry keeps it on the shade
        self.assertEqual(self.unseen(), 1)

    def test_clear_all_in_the_shade_zeroes_the_badge_but_keeps_rows_unread(self):
        from notifyd.__main__ import dismiss_from_shade

        self.build()
        ids = [self.notify(f"n{i}") for i in range(4)]
        self.server.on_closed(ids[0], CloseReason.EXPIRED)  # two expired, two still open
        self.server.on_closed(ids[1], CloseReason.EXPIRED)
        self.assertEqual(self.unseen(), 4)
        unread_before = self.unread()
        dismiss_from_shade(self.server, ids)  # the shade's "Clear all": mixed open + expired
        self.assertEqual(self.unseen(), 0)
        self.assertEqual(self.unseen_signals()[-1], 0)
        self.assertEqual(self.unread(), unread_before)
        for i in ids:
            self.assertIsNone(self.store.get(i).read_at)
            self.assertEqual(self.store.get(i).close_reason, CloseReason.DISMISSED)

    def test_expired_only_clear_all_signals(self):
        from notifyd.__main__ import dismiss_from_shade

        self.build()
        ids = [self.notify("a"), self.notify("b")]
        for i in ids:
            self.server.on_closed(i, CloseReason.EXPIRED)
        self.assertEqual(self.unseen(), 2)
        dismiss_from_shade(self.server, ids)
        self.assertEqual(self.unseen_signals()[-1], 0)

    def test_delete_and_clear_decrement(self):
        self.build()
        a = self.notify("a")
        self.notify("b")
        self.ctl("Delete", GLib.Variant("(au)", ([a],)))
        self.assertEqual(self.unseen(), 1)
        self.ctl("ClearHistory")
        self.assertEqual(self.unseen(), 0)

    def test_watermark_survives_a_restart(self):
        self.build()
        self.notify("old")
        self.ctl("MarkSeen")
        self.notify("new")
        self.server.unregister()
        self.build()  # a fresh SeenMark over the same state dir
        self.assertEqual(self.unseen(), 1)

    def test_first_start_counts_from_now(self):
        self.notify_before = None
        self.build(state=False)
        self.notify("a")
        self.server.unregister()
        # the db has history; a missing seen.json starts at "now", not at the epoch
        self.build()
        self.assertEqual(self.unseen(), 0)
        self.assertTrue((self.state / "seen.json").exists())


class SeenMarkTests(unittest.TestCase):
    def test_missing_and_corrupt_files_start_at_now(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(SeenMark(Path(d), 50.0).at, 50.0)
            self.assertEqual(SeenMark(Path(d), 99.0).at, 50.0)  # loaded
            (Path(d) / "seen.json").write_text("{nope")
            self.assertEqual(SeenMark(Path(d), 70.0).at, 70.0)

    def test_set_persists_without_leftovers(self):
        with tempfile.TemporaryDirectory() as d:
            m = SeenMark(Path(d), 1.0)
            m.set(5.0)
            self.assertEqual(SeenMark(Path(d), 9.0).at, 5.0)
            self.assertEqual([p.name for p in Path(d).iterdir()], ["seen.json"])


if __name__ == "__main__":
    unittest.main()
