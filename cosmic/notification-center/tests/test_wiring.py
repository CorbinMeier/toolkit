"""Center wiring: build_daemon with a fake display and fake center, a real Store, a private bus."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from gi.repository import Gio, GLib

from notifyd.__main__ import NullCenter, Runtime, build_daemon, load_config
from notifyd.dbus_server import CTL_IFACE, CTL_PATH, FDO_IFACE, FDO_PATH, PROPS_IFACE
from notifyd.rules import RuleSet
from notifyd.store import Store
from tests.test_integration import Caller, connect, pump_briefly, pump_until
from tests.test_toasts import WEB, FakeHost, note


class FakeDisplay:
    def __init__(self):
        self.shown, self.closed, self.css = [], [], []
        self.on_action = self.on_closed = self.on_details = None

    def show(self, n):
        self.shown.append(n.id)

    def close(self, id):
        self.closed.append(id)

    def close_all_expired(self):
        return []

    def set_user_css(self, css):
        self.css.append(css)

    def set_hover_grace(self, ms):
        pass

    def set_hover_max_hold(self, ms):
        pass


class FakeCenter:
    """Records calls; the callbacks are the ones the real CenterWindow would get."""

    def __init__(self, server):
        self.server = server
        self.calls = []
        self.css = []

    def toggle(self):
        self.calls.append(("toggle",))

    def show(self):
        self.calls.append(("show",))

    def show_notification(self, id):
        self.calls.append(("show_notification", id))

    def notify_added(self, n):
        self.calls.append(("added", n.id, str(n.state)))

    def set_user_css(self, css):
        self.css.append(css)


class WiringCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bus = Gio.TestDBus()
        cls.bus.up()
        addr = cls.bus.get_bus_address()
        cls.server_conn = connect(addr)
        cls.client = connect(addr)

    @classmethod
    def tearDownClass(cls):
        cls.client.close_sync(None)
        cls.server_conn.close_sync(None)
        cls.bus.down()

    def setUp(self):
        self.store = Store(":memory:")
        self.display = FakeDisplay()
        self.signals = []
        self.c = Caller(self.client, self.server_conn.get_unique_name())
        self.sub = self.client.signal_subscribe(
            self.server_conn.get_unique_name(),
            None,
            None,
            None,
            None,
            Gio.DBusSignalFlags.NONE,
            lambda c, s, p, i, n, params: self.signals.append((n, params.unpack())),
        )
        self.server = None

    def tearDown(self):
        self.client.signal_unsubscribe(self.sub)
        if self.server is not None:
            self.server.unregister()
        self.store.close()

    def build(self, rules_text=""):
        rules = RuleSet.from_toml(rules_text)
        self.server = build_daemon(
            self.server_conn,
            self.store,
            self.display,
            FakeCenter,
            process=rules.apply,
        )
        self.center = self.server.center
        return self.server

    def notify(self, summary="Hi", actions=()):
        args = GLib.Variant("(susssasa{sv}i)", ("App", 0, "", summary, "b", list(actions), {}, -1))
        return self.c.call(FDO_PATH, FDO_IFACE, "Notify", args, "(u)").unpack()[0]

    def ctl(self, method, args=None, reply="()"):
        return self.c.call(CTL_PATH, CTL_IFACE, method, args, reply)

    def unread(self):
        r = self.c.call(
            CTL_PATH,
            PROPS_IFACE,
            "Get",
            GLib.Variant("(ss)", (CTL_IFACE, "UnreadCount")),
            "(v)",
        )
        return r.unpack()[0]

    def names(self, name):
        return [p for n, p in self.signals if n == name]

    def set_dnd(self, on):
        args = GLib.Variant("(ssv)", (CTL_IFACE, "DoNotDisturb", GLib.Variant("b", on)))
        self.c.call(CTL_PATH, PROPS_IFACE, "Set", args)


class CenterBusTests(WiringCase):
    def test_toggle_show_and_show_notification_reach_center(self):
        self.build()
        self.ctl("ToggleCenter")
        self.ctl("ShowCenter")
        self.ctl("ShowNotification", GLib.Variant("(u)", (42,)))
        self.assertEqual(self.center.calls, [("toggle",), ("show",), ("show_notification", 42)])

    def test_null_center_when_no_factory(self):
        self.server = build_daemon(self.server_conn, self.store, self.display)
        self.assertIsInstance(self.server.center, NullCenter)
        self.ctl("ToggleCenter")
        self.notify()  # observer is a no-op, nothing raises

    def test_display_details_goes_to_center(self):
        self.build()
        self.assertEqual(self.display.on_details, self.center.show_notification)
        self.display.on_details(7)
        self.assertEqual(self.center.calls, [("show_notification", 7)])

    def test_toast_body_click_opens_details_and_toast_stays(self):
        self.build()
        closed = []
        host = FakeHost(WEB, self.server.on_action, lambda i, r: closed.append(i))
        host.on_details = self.center.show_notification
        host.show(note(1, expire=1000))
        host._on_message({"type": "details", "id": 1})
        self.assertEqual(self.center.calls, [("show_notification", 1)])
        self.assertEqual(closed, [])
        self.assertIn(1, host._active)

    def test_notify_added_for_shown_blocked_dnd_and_store_only(self):
        self.build(
            '[[rule]]\nname = "b"\nsummary = "block"\nblock = true\n'
            '[[rule]]\nname = "s"\nsummary = "only"\nstore_only = true\n'
        )
        a = self.notify("plain")
        b = self.notify("block me")
        s = self.notify("only me")
        self.set_dnd(True)
        d = self.notify("during dnd")
        self.assertEqual(
            [c for c in self.center.calls if c[0] == "added"],
            [
                ("added", a, "shown"),
                ("added", b, "blocked"),
                ("added", s, "store_only"),
                ("added", d, "dnd"),
            ],
        )

    def test_style_goes_to_toasts_and_center(self):
        self.build()
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "style.css").write_text(".x{}")
            rt = Runtime(Path(tmp) / "config.toml", self.display, load_config(Path(tmp) / "c"))
            rt.center = self.center
            rt.apply_style()
        self.assertEqual((self.display.css, self.center.css), (["\n.x{}"], ["\n.x{}"]))


class InvokeActionTests(WiringCase):
    def test_open_notification_emits_action_and_closes_toast(self):
        self.build()
        nid = self.notify("act", actions=["go", "Go"])
        self.assertTrue(self.server.invoke_action(nid, "go"))
        pump_briefly()
        self.assertEqual(self.names("ActionInvoked"), [(nid, "go")])
        self.assertEqual(self.display.closed, [nid])
        self.assertIsNotNone(self.store.get(nid).closed_at)
        self.assertEqual(len(self.names("NotificationClosed")), 1)

    def test_closed_notification_is_false_without_signal(self):
        self.build()
        nid = self.notify("act", actions=["go", "Go"])
        self.ctl("Dismiss", GLib.Variant("(u)", (nid,)))
        pump_briefly()
        self.signals.clear()
        self.assertFalse(self.server.invoke_action(nid, "go"))
        self.assertFalse(self.server.invoke_action(9999, "go"))
        pump_briefly()
        self.assertEqual(self.names("ActionInvoked"), [])
        self.assertEqual(self.names("NotificationClosed"), [])


class CenterCallbackTests(WiringCase):
    """The center's mark_read / delete / clear / dismiss go through the bus paths."""

    def prop_changes(self):
        return [
            p[1]["UnreadCount"]
            for n, p in self.signals
            if n == "PropertiesChanged" and "UnreadCount" in p[1]
        ]

    def test_unread_count_follows_center_callbacks(self):
        self.build()
        ids = [self.notify(f"n{i}") for i in range(3)]
        self.assertEqual(self.unread(), 3)
        self.server.mark_read(ids[:1])
        self.assertEqual(self.unread(), 2)
        self.server.delete(ids[1:2])
        self.assertEqual(self.unread(), 1)
        self.server.clear_history()
        self.assertEqual(self.unread(), 0)
        pump_until(lambda: self.prop_changes()[-1:] == [0])

    def test_dismiss_closes_toast_and_signals(self):
        self.build()
        nid = self.notify()
        self.server.dismiss(nid)
        pump_briefly()
        self.assertEqual(self.display.closed, [nid])
        self.assertEqual(self.names("NotificationClosed"), [(nid, 2)])


class ShowCenterClientTests(unittest.TestCase):
    def test_show_center_client_calls_running_daemon(self):
        bus = Gio.TestDBus()
        bus.up()
        try:
            addr = bus.get_bus_address()
            root = Path(__file__).resolve().parent.parent
            with tempfile.TemporaryDirectory() as tmp:
                daemon = subprocess.Popen(
                    [
                        str(root / "bin" / "notifyd"),
                        "--bus-address",
                        addr,
                        "--db",
                        str(Path(tmp) / "t.db"),
                        "--config",
                        str(Path(tmp) / "none.toml"),
                        "--no-display",
                    ],
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    client = connect(addr)
                    dbus = Caller(client, "org.freedesktop.DBus")

                    def has():
                        r = dbus.call(
                            "/org/freedesktop/DBus",
                            "org.freedesktop.DBus",
                            "NameHasOwner",
                            GLib.Variant("(s)", (CTL_IFACE,)),
                            "(b)",
                        )
                        return r.unpack()[0]

                    pump_until(has, timeout=10)
                    for flag in (["--show-center"], ["--show-notification", "5"]):
                        r = subprocess.run(
                            [str(root / "bin" / "notifyd"), "--bus-address", addr, *flag],
                            capture_output=True,
                            text=True,
                            timeout=20,
                        )
                        self.assertEqual(r.returncode, 0, r.stderr)
                    client.close_sync(None)
                finally:
                    daemon.terminate()
                    daemon.wait(timeout=10)
                    daemon.stderr.close()
                # no daemon any more: the client reports it and exits 1
                r = subprocess.run(
                    [str(root / "bin" / "notifyd"), "--bus-address", addr, "--show-center"],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(r.returncode, 1)
                self.assertIn("is the daemon running", r.stderr)
        finally:
            bus.down()


if __name__ == "__main__":
    unittest.main()
