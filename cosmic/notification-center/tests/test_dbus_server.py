import json
import unittest

from gi.repository import Gio, GLib

from notifyd.dbus_server import (
    CTL_IFACE,
    CTL_PATH,
    FDO_IFACE,
    FDO_PATH,
    PROPS_IFACE,
    NotificationServer,
)
from notifyd.model import CloseReason, State


class FakeStore:
    def __init__(self):
        self.items = {}
        self._next = 0
        self.read = set()
        self.calls = []

    def clear_snooze(self, id):
        pass

    def unseen_count(self, since):
        return 0

    def next_id(self):
        self._next += 1
        return self._next

    def record(self, n):
        self.items[n.id] = n

    def get(self, id):
        return self.items.get(id)

    def mark_closed(self, id, reason, at):
        n = self.items[id]
        n.closed_at, n.close_reason = at, reason
        self.calls.append(("mark_closed", id, reason, at))

    def mark_read(self, ids, at):
        for i in ids:
            if i in self.items:
                self.items[i].read_at = at
        self.calls.append(("mark_read", list(ids), at))

    def delete(self, ids):
        for i in ids:
            self.items.pop(i, None)
        self.calls.append(("delete", list(ids)))

    def clear(self):
        self.items.clear()
        self.calls.append(("clear",))

    def unread_count(self):
        return sum(1 for n in self.items.values() if n.read_at is None)

    def history(self, query="", offset=0, limit=50):
        self.calls.append(("history", query, offset, limit))
        rows = [n for n in self.items.values() if query in n.summary]
        return rows[offset : offset + limit]


class FakeDisplay:
    def __init__(self):
        self.shown = []
        self.closed = []

    def show(self, n):
        self.shown.append(n.id)

    def close(self, id):
        self.closed.append(id)


def pump_until(cond, timeout=3.0):
    ctx = GLib.MainContext.default()
    end = GLib.get_monotonic_time() + int(timeout * 1e6)
    while not cond():
        if GLib.get_monotonic_time() > end:
            raise AssertionError("timed out")
        ctx.iteration(False)


def pump_briefly(ms=100):
    ctx = GLib.MainContext.default()
    end = GLib.get_monotonic_time() + ms * 1000
    while GLib.get_monotonic_time() < end:
        ctx.iteration(False)


class BusCase(unittest.TestCase):
    process = None

    @classmethod
    def setUpClass(cls):
        cls.bus = Gio.TestDBus()
        cls.bus.up()
        addr = cls.bus.get_bus_address()
        flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        flags |= Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
        cls.server_conn = Gio.DBusConnection.new_for_address_sync(addr, flags, None, None)
        cls.client = Gio.DBusConnection.new_for_address_sync(addr, flags, None, None)

    @classmethod
    def tearDownClass(cls):
        cls.client.close_sync(None)
        cls.server_conn.close_sync(None)
        cls.bus.down()

    def setUp(self):
        self.store = FakeStore()
        self.display = FakeDisplay()
        self.toggles = []
        self.shows = []
        self.details = []
        self.server = NotificationServer(
            self.server_conn,
            self.store,
            self.display,
            clock=lambda: 1000.0,
            on_toggle_center=lambda: self.toggles.append(1),
            on_show_center=lambda: self.shows.append(1),
            on_show_notification=self.details.append,
            process=type(self).process,
        )
        self.signals = []
        self.subs = [
            self.client.signal_subscribe(
                self.server_conn.get_unique_name(),
                None,
                None,
                None,
                None,
                Gio.DBusSignalFlags.NONE,
                lambda c, s, p, i, n, params: self.signals.append((i, n, params.unpack())),
            )
        ]

    def tearDown(self):
        for s in self.subs:
            self.client.signal_unsubscribe(s)
        self.server.unregister()

    def call(self, path, iface, method, args=None, reply="()"):
        done = []
        self.client.call(
            self.server_conn.get_unique_name(),
            path,
            iface,
            method,
            args,
            GLib.VariantType(reply) if reply else None,
            Gio.DBusCallFlags.NONE,
            3000,
            None,
            lambda src, res: done.append(res),
        )
        pump_until(lambda: done)
        return self.client.call_finish(done[0])

    def fdo(self, method, args=None, reply="()"):
        return self.call(FDO_PATH, FDO_IFACE, method, args, reply)

    def ctl(self, method, args=None, reply="()"):
        return self.call(CTL_PATH, CTL_IFACE, method, args, reply)

    def notify(self, summary="Hi", body="b", replaces=0, hints=None, actions=None, expire=-1):
        args = GLib.Variant(
            "(susssasa{sv}i)",
            ("App", replaces, "icon", summary, body, actions or [], hints or {}, expire),
        )
        return self.fdo("Notify", args, "(u)").unpack()[0]

    def seen(self, iface, name):
        pump_briefly()
        return [p for i, n, p in self.signals if i == iface and n == name]

    def get_prop(self, name):
        args = GLib.Variant("(ss)", (CTL_IFACE, name))
        return self.call(CTL_PATH, PROPS_IFACE, "Get", args, "(v)").unpack()[0]


class NotifyTests(BusCase):
    def test_id_allocation_and_replace(self):
        a = self.notify("one")
        b = self.notify("two")
        self.assertEqual((a, b), (1, 2))
        again = self.notify("one v2", replaces=a)
        self.assertEqual(again, a)
        self.assertEqual(self.store.items[a].summary, "one v2")
        self.assertEqual(self.notify("x", replaces=99), 3)  # unknown id gets a fresh one
        self.assertEqual(self.display.shown, [1, 2, 1, 3])

    def test_added_signal_and_state(self):
        nid = self.notify("Sum", body="<b>raw</b>")
        added = self.seen(CTL_IFACE, "NotificationAdded")
        self.assertEqual(len(added), 1)
        got_id, blob = added[0]
        data = json.loads(blob)
        self.assertEqual(got_id, nid)
        self.assertEqual(data["summary"], "Sum")
        self.assertEqual(data["body"], [{"text": "raw", "b": True}])
        self.assertEqual(self.store.items[nid].state, State.SHOWN)
        self.assertEqual(self.store.items[nid].received_at, 1000.0)

    def test_hints_round_trip(self):
        hints = {
            "urgency": GLib.Variant("y", 2),
            "category": GLib.Variant("s", "email.arrived"),
            "desktop-entry": GLib.Variant("s", "org.x"),
            "image-path": GLib.Variant("s", "/tmp/i.png"),
            "image-data": GLib.Variant("(iiibiiay)", (1, 1, 4, True, 8, 4, b"\0\0\0\0")),
            "x-custom": GLib.Variant("as", ["a", "b"]),
        }
        nid = self.notify(hints=hints, actions=["k", "Label", "dangling"], expire=0)
        n = self.store.items[nid]
        self.assertEqual(n.urgency, 2)
        self.assertEqual(n.hints["urgency"], 2)
        self.assertEqual(n.category, "email.arrived")
        self.assertEqual(n.desktop_entry, "org.x")
        self.assertEqual(n.hints["image-path"], "/tmp/i.png")
        self.assertEqual(n.hints["x-custom"], ["a", "b"])
        self.assertNotIn("image-data", n.hints)
        self.assertEqual(n.actions, [("k", "Label")])
        self.assertIsNone(n.timeout_ms)

    def test_unread_count_changes(self):
        self.assertEqual(self.get_prop("UnreadCount"), 0)
        self.notify()
        pump_briefly()
        changes = [p for i, n, p in self.signals if n == "PropertiesChanged"]
        self.assertEqual(changes[-1][1], {"UnreadCount": 1})
        self.assertEqual(self.get_prop("UnreadCount"), 1)

    def test_bad_args_error(self):
        with self.assertRaises(GLib.Error) as cm:
            self.fdo("Notify", GLib.Variant("(s)", ("only one",)), "(u)")
        self.assertIn("InvalidArgs", cm.exception.message)
        with self.assertRaises(GLib.Error) as cm:
            self.ctl("GetHistory", GLib.Variant("(suu)", ("", 0, 100000)), "(s)")
        self.assertIn("InvalidArgs", cm.exception.message)

    def test_handler_exception_becomes_failed(self):
        def boom():
            raise RuntimeError("kaput")

        self.store.next_id = boom
        with self.assertLogs("notifyd.dbus", "ERROR"), self.assertRaises(GLib.Error) as cm:
            self.notify()
        self.assertIn("Error.Failed", cm.exception.message)


class DndTests(BusCase):
    def test_dnd_suppresses_display_but_records(self):
        self.call(
            CTL_PATH,
            PROPS_IFACE,
            "Set",
            GLib.Variant("(ssv)", (CTL_IFACE, "DoNotDisturb", GLib.Variant("b", True))),
            "()",
        )
        self.assertTrue(self.get_prop("DoNotDisturb"))
        nid = self.notify()
        self.assertEqual(self.display.shown, [])
        self.assertEqual(self.store.items[nid].state, State.DND)
        self.assertEqual(len(self.seen(CTL_IFACE, "NotificationAdded")), 1)
        pc = [p for i, n, p in self.signals if n == "PropertiesChanged"]
        self.assertIn({"DoNotDisturb": True}, [p[1] for p in pc])


class ProcessHookTests(BusCase):
    @staticmethod
    def process(n):
        if n.app_name == "App" and n.summary == "block":
            n.state = State.BLOCKED
        elif n.summary == "store":
            n.state = State.STORE_ONLY
        n.tags.append("seen")
        return n

    def test_blocked_and_store_only_not_displayed(self):
        a = self.notify("block")
        b = self.notify("store")
        c = self.notify("show")
        self.assertEqual(self.store.items[a].state, State.BLOCKED)
        self.assertEqual(self.store.items[b].state, State.STORE_ONLY)
        self.assertEqual(self.store.items[c].state, State.SHOWN)
        self.assertEqual(self.display.shown, [c])
        self.assertEqual(self.store.items[c].tags, ["seen"])


class CloseTests(BusCase):
    def test_close_notification(self):
        nid = self.notify()
        self.fdo("CloseNotification", GLib.Variant("(u)", (nid,)))
        self.assertEqual(self.display.closed, [nid])
        self.assertEqual(self.seen(FDO_IFACE, "NotificationClosed"), [(nid, 3)])
        self.assertEqual(self.store.items[nid].close_reason, CloseReason.CLOSED)
        self.assertEqual(self.store.items[nid].closed_at, 1000.0)

    def test_unknown_id_is_noop(self):
        self.fdo("CloseNotification", GLib.Variant("(u)", (42,)))
        self.assertEqual(self.display.closed, [])
        self.assertEqual(self.seen(FDO_IFACE, "NotificationClosed"), [])

    def test_on_closed_is_idempotent(self):
        nid = self.notify()
        self.server.on_closed(nid, CloseReason.EXPIRED)
        self.server.on_closed(nid, CloseReason.DISMISSED)
        self.assertEqual(self.seen(FDO_IFACE, "NotificationClosed"), [(nid, 1)])

    def test_on_action(self):
        nid = self.notify()
        self.server.on_action(nid, "open")
        self.assertEqual(self.seen(FDO_IFACE, "ActionInvoked"), [(nid, "open")])


class InfoTests(BusCase):
    def test_capabilities(self):
        (caps,) = self.fdo("GetCapabilities", None, "(as)").unpack()
        self.assertEqual(
            caps,
            [
                "body",
                "body-markup",
                "body-hyperlinks",
                "actions",
                "icon-static",
                "persistence",
                "sound",
            ],
        )

    def test_server_information(self):
        info = self.fdo("GetServerInformation", None, "(ssss)").unpack()
        self.assertEqual(info, ("cosmic-notification-center", "net.corbinmeier", "0.1.0", "1.2"))


class ControlTests(BusCase):
    def test_toggle_and_show_center(self):
        self.ctl("ToggleCenter")
        self.ctl("ShowCenter")
        self.assertEqual((self.toggles, self.shows), ([1], [1]))

    def test_show_notification_passes_id_through(self):
        nid = self.notify()
        self.ctl("ShowNotification", GLib.Variant("(u)", (nid,)))
        self.ctl("ShowNotification", GLib.Variant("(u)", (9999,)))  # unknown: the window says so
        self.assertEqual(self.details, [nid, 9999])

    def test_show_notification_is_introspectable(self):
        xml = self.call(
            CTL_PATH, "org.freedesktop.DBus.Introspectable", "Introspect", None, "(s)"
        ).unpack()[0]
        self.assertIn('name="ShowNotification"', xml)

    def test_callbacks_none_is_noop(self):
        self.server.unregister()
        self.server = NotificationServer(self.server_conn, self.store, self.display)
        self.ctl("ToggleCenter")
        self.ctl("ShowCenter")
        self.ctl("ShowNotification", GLib.Variant("(u)", (1,)))

    def test_mark_read_delete_clear_refresh_unread(self):
        ids = [self.notify(f"n{i}") for i in range(3)]
        self.assertEqual(self.get_prop("UnreadCount"), 3)
        self.ctl("MarkRead", GLib.Variant("(au)", (ids[:2],)))
        self.assertEqual(self.store.calls[-1], ("mark_read", ids[:2], 1000.0))
        self.assertEqual(self.get_prop("UnreadCount"), 1)
        self.ctl("Delete", GLib.Variant("(au)", ([ids[2]],)))
        self.assertEqual(self.get_prop("UnreadCount"), 0)
        self.assertEqual(self.store.calls[-1], ("delete", [ids[2]]))
        self.ctl("ClearHistory")
        self.assertEqual(self.store.calls[-1], ("clear",))
        pump_briefly()
        counts = [p[1]["UnreadCount"] for i, n, p in self.signals if n == "PropertiesChanged"]
        self.assertEqual(counts[-2:], [1, 0])  # clear changes nothing more: no extra signal

    def test_dismiss(self):
        nid = self.notify()
        self.ctl("Dismiss", GLib.Variant("(u)", (nid,)))
        self.assertEqual(self.display.closed, [nid])
        self.assertEqual(self.seen(FDO_IFACE, "NotificationClosed"), [(nid, 2)])
        self.assertEqual(self.store.items[nid].close_reason, CloseReason.DISMISSED)

    def test_get_history(self):
        a = self.notify("alpha", body="one")
        self.notify("beta")
        self.ctl("MarkRead", GLib.Variant("(au)", ([a],)))
        self.fdo("CloseNotification", GLib.Variant("(u)", (a,)))
        (blob,) = self.ctl("GetHistory", GLib.Variant("(suu)", ("alpha", 0, 10)), "(s)").unpack()
        rows = json.loads(blob)
        self.assertEqual(self.store.calls[-1], ("history", "alpha", 0, 10))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], a)
        self.assertEqual(row["body"], [{"text": "one"}])
        self.assertEqual(row["read_at"], 1000.0)
        self.assertEqual(row["closed_at"], 1000.0)
        self.assertEqual(row["close_reason"], 3)
        self.assertEqual(row["state"], "shown")
        (blob,) = self.ctl("GetHistory", GLib.Variant("(suu)", ("beta", 0, 10)), "(s)").unpack()
        self.assertIsNone(json.loads(blob)[0]["close_reason"])


if __name__ == "__main__":
    unittest.main()
