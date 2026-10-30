"""Integration: real Store + build_daemon on a private bus, the launcher subprocess, the shim."""

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from gi.repository import Gio, GLib

from notifyd.__main__ import Config, build_daemon, load_config
from notifyd.dbus_server import CTL_IFACE, CTL_PATH, FDO_IFACE, FDO_PATH
from notifyd.model import CloseReason
from notifyd.store import Store

ROOT = Path(__file__).resolve().parent.parent
SHIM_TEMPLATE = ROOT / "scripts" / "shim" / "cosmic-notifications"


def pump_until(cond, timeout=5.0):
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


def connect(addr):
    flags = (
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
    )
    return Gio.DBusConnection.new_for_address_sync(addr, flags, None, None)


class FakeDisplay:
    def __init__(self):
        self.shown, self.closed = [], []
        self.on_action = self.on_closed = None

    def show(self, n):
        self.shown.append(n.id)

    def close(self, id):
        self.closed.append(id)


class Caller:
    def __init__(self, conn, dest):
        self.conn, self.dest = conn, dest

    def call(self, path, iface, method, args=None, reply="()"):
        done = []
        self.conn.call(
            self.dest,
            path,
            iface,
            method,
            args,
            GLib.VariantType(reply),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
            lambda src, res: done.append(res),
        )
        pump_until(lambda: done)
        return self.conn.call_finish(done[0])

    def notify(self, summary="Hi", body="b", replaces=0):
        args = GLib.Variant("(susssasa{sv}i)", ("App", replaces, "", summary, body, [], {}, -1))
        return self.call(FDO_PATH, FDO_IFACE, "Notify", args, "(u)").unpack()[0]

    def history(self, query=""):
        args = GLib.Variant("(suu)", (query, 0, 50))
        return json.loads(self.call(CTL_PATH, CTL_IFACE, "GetHistory", args, "(s)").unpack()[0])


class DaemonWiringTests(unittest.TestCase):
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
        self.server = build_daemon(self.server_conn, self.store, self.display)
        self.c = Caller(self.client, self.server_conn.get_unique_name())
        self.signals = []
        self.sub = self.client.signal_subscribe(
            self.server_conn.get_unique_name(),
            FDO_IFACE,
            None,
            None,
            None,
            Gio.DBusSignalFlags.NONE,
            lambda c, s, p, i, n, params: self.signals.append((n, params.unpack())),
        )

    def tearDown(self):
        self.client.signal_unsubscribe(self.sub)
        self.server.unregister()
        self.store.close()

    def test_callbacks_are_wired(self):
        self.assertEqual(self.display.on_action, self.server.on_action)
        self.assertEqual(self.display.on_closed, self.server.on_closed)

    def test_notify_round_trip_and_history(self):
        nid = self.c.notify("Hello", "world")
        self.assertEqual(self.display.shown, [nid])
        stored = self.store.get(nid)
        self.assertEqual((stored.summary, stored.body), ("Hello", "world"))
        hist = self.c.history()
        self.assertEqual([h["id"] for h in hist], [nid])
        self.assertEqual(hist[0]["summary"], "Hello")
        self.assertEqual(hist[0]["state"], "shown")
        self.assertEqual([h["id"] for h in self.c.history("Hello")], [nid])

    def test_replaces_id(self):
        a = self.c.notify("one")
        b = self.c.notify("two", replaces=a)
        self.assertEqual(a, b)
        self.assertEqual(self.store.get(a).summary, "two")
        self.assertEqual(len(self.c.history()), 1)

    def test_close_notification_signals_and_stores(self):
        nid = self.c.notify("bye")
        self.c.call(FDO_PATH, FDO_IFACE, "CloseNotification", GLib.Variant("(u)", (nid,)))
        pump_briefly()
        self.assertEqual(self.display.closed, [nid])
        closed = [p for n, p in self.signals if n == "NotificationClosed"]
        self.assertEqual(closed, [(nid, int(CloseReason.CLOSED))])
        row = self.store.get(nid)
        self.assertIsNotNone(row.closed_at)
        self.assertEqual(row.close_reason, CloseReason.CLOSED)

    def test_second_close_is_guarded(self):
        nid = self.c.notify("twice")
        self.c.call(FDO_PATH, FDO_IFACE, "CloseNotification", GLib.Variant("(u)", (nid,)))
        first = self.store.get(nid).closed_at
        self.display.on_closed(nid, CloseReason.EXPIRED)  # a late report from the display
        self.c.call(FDO_PATH, FDO_IFACE, "CloseNotification", GLib.Variant("(u)", (nid,)))
        pump_briefly()
        closed = [p for n, p in self.signals if n == "NotificationClosed"]
        self.assertEqual(len(closed), 1)
        row = self.store.get(nid)
        self.assertEqual((row.closed_at, row.close_reason), (first, CloseReason.CLOSED))


class ConfigTests(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            p.write_text(text)
            return load_config(p)

    def test_missing_file_is_defaults(self):
        self.assertEqual(load_config(Path("/nonexistent/config.toml")), Config())

    def test_valid(self):
        c = self.load(
            '[toasts]\nanchor = "bottom-left"\nmargin = 4\n[history]\nretention_days = 30\n'
        )
        self.assertEqual(c, Config("bottom-left", 4, 30))

    def test_invalid_falls_back(self):
        with self.assertLogs("notifyd", "ERROR") as logs:
            c = self.load(
                '[toasts]\nanchor = "middle"\nmargin = -3\n[history]\nretention_days = 0\n'
            )
            self.assertEqual(c, Config())
            self.assertEqual(self.load("this is = = not toml"), Config())
        self.assertEqual(len(logs.records), 4)


class LauncherTests(unittest.TestCase):
    def test_names_owned_notify_and_sigterm(self):
        bus = Gio.TestDBus()
        bus.up()
        try:
            addr = bus.get_bus_address()
            with tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "t.db"
                proc = subprocess.Popen(
                    [
                        str(ROOT / "bin" / "notifyd"),
                        "--bus-address",
                        addr,
                        "--db",
                        str(db),
                        "--config",
                        str(Path(tmp) / "none.toml"),
                        "--no-display",
                    ],
                    stderr=subprocess.PIPE,
                    text=True,
                )
                client = connect(addr)
                try:
                    dbus = Caller(client, "org.freedesktop.DBus")

                    def has(name):
                        r = dbus.call(
                            "/org/freedesktop/DBus",
                            "org.freedesktop.DBus",
                            "NameHasOwner",
                            GLib.Variant("(s)", (name,)),
                            "(b)",
                        )
                        return r.unpack()[0]

                    pump_until(lambda: has(FDO_IFACE) and has(CTL_IFACE), timeout=10)
                    c = Caller(client, FDO_IFACE)
                    nid = c.notify("From subprocess", "body")
                    self.assertEqual(nid, 1)
                    hist = Caller(client, CTL_IFACE).history()
                    self.assertEqual(hist[0]["summary"], "From subprocess")
                    proc.send_signal(signal.SIGTERM)
                    self.assertEqual(proc.wait(timeout=10), 0)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait()
                    proc.stderr.close()
                    client.close_sync(None)
                row = sqlite3.connect(db).execute("SELECT summary FROM notifications").fetchone()
                self.assertEqual(row[0], "From subprocess")
        finally:
            bus.down()

    def test_exit_2_when_name_is_held(self):
        bus = Gio.TestDBus()
        bus.up()
        try:
            addr = bus.get_bus_address()
            holder = connect(addr)
            # Hold the name without allowing replacement.
            done = []
            oid = Gio.bus_own_name_on_connection(
                holder, FDO_IFACE, Gio.BusNameOwnerFlags.NONE, lambda *a: done.append(1), None
            )
            pump_until(lambda: done)
            with tempfile.TemporaryDirectory() as tmp:
                proc = subprocess.run(
                    [
                        str(ROOT / "bin" / "notifyd"),
                        "--bus-address",
                        addr,
                        "--db",
                        str(Path(tmp) / "t.db"),
                        "--config",
                        str(Path(tmp) / "none.toml"),
                        "--no-display",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn("notifyd:", proc.stderr)
            Gio.bus_unown_name(oid)
            holder.close_sync(None)
        finally:
            bus.down()


class ShimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.shim = self.home / "cosmic-notifications"
        tpl = SHIM_TEMPLATE.read_text()
        self.shim.write_text(tpl.replace("@REPO@", str(ROOT)))
        self.shim.chmod(0o755)
        self.count = self.home / "count"
        self.fake = self.home / "fake-notifyd"
        self.fake.write_text(f'#!/bin/bash\necho x >> "{self.count}"\nexit 3\n')
        self.fake.chmod(0o755)
        self.stock = self.home / "stock"
        self.stock.write_text(f'#!/bin/bash\necho "stock $*" > "{self.home}/stock.out"\n')
        self.stock.chmod(0o755)
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "NOTIFYD_BIN": str(self.fake),
            "NOTIFYD_STOCK": str(self.stock),
            "NOTIFYD_BACKOFF_MIN": "0.2",
            "NOTIFYD_BACKOFF_MAX": "0.8",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_restarts_with_backoff_and_forwards_term(self):
        proc = subprocess.Popen([str(self.shim)], env=self.env, stderr=subprocess.PIPE, text=True)
        time.sleep(2.5)
        self.assertIsNone(proc.poll(), "shim must not exit on its own")
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=10), -signal.SIGTERM)
        err = proc.stderr.read()
        proc.stderr.close()
        runs = len(self.count.read_text().split())
        # delays 0.2, 0.4, 0.8, 0.8 ... -> starts at ~0, 0.2, 0.6, 1.4, 2.2: bounded both ways
        self.assertTrue(3 <= runs <= 6, f"runs={runs}")
        self.assertIn("restarting in 0.2s", err)
        self.assertIn("restarting in 0.4s", err)
        self.assertIn("restarting in 0.8s", err)

    def test_term_is_forwarded_to_a_running_child(self):
        pidfile = self.home / "child.pid"
        self.fake.write_text(
            f'#!/bin/bash\necho $$ > "{pidfile}"\n'
            f"trap 'echo got-term > \"{self.home}/term\"; exit 0' TERM\n"
            "while :; do sleep 0.1; done\n"
        )
        proc = subprocess.Popen([str(self.shim)], env=self.env, stderr=subprocess.DEVNULL)
        for _ in range(50):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.1)
        time.sleep(0.3)
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=10), -signal.SIGTERM)
        self.assertTrue((self.home / "term").exists(), "child did not receive TERM")
        child_pid = int(pidfile.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)  # reaped/gone

    def test_int_is_forwarded_and_shim_dies_by_sigint(self):
        pidfile = self.home / "child.pid"
        self.fake.write_text(
            f'#!/bin/bash\necho $$ > "{pidfile}"\n'
            f"trap 'echo got-term > \"{self.home}/term\"; exit 0' TERM\n"
            "while :; do sleep 0.1; done\n"
        )
        proc = subprocess.Popen([str(self.shim)], env=self.env, stderr=subprocess.DEVNULL)
        for _ in range(50):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.1)
        time.sleep(0.3)
        proc.send_signal(signal.SIGINT)
        self.assertEqual(proc.wait(timeout=10), -signal.SIGINT)
        self.assertTrue((self.home / "term").exists(), "child did not receive TERM")

    @unittest.skipUnless(shutil.which("killall"), "killall not installed")
    def test_killall_by_full_name_finds_the_shim(self):
        # Never name this cosmic-notifications: killall would hit the live daemon.
        name = "nc-shim-killtest-xyz"
        shim = self.home / name
        shim.write_text(SHIM_TEMPLATE.read_text().replace("@REPO@", str(ROOT)))
        shim.chmod(0o755)
        self.fake.write_text("#!/bin/bash\nwhile :; do sleep 0.1; done\n")
        proc = subprocess.Popen([str(shim)], env=self.env, stderr=subprocess.DEVNULL)
        try:
            comm = Path(f"/proc/{proc.pid}/comm")
            for _ in range(50):
                if comm.read_text().strip() == name[:15]:
                    break
                time.sleep(0.1)
            self.assertEqual(comm.read_text().strip(), name[:15])
            time.sleep(0.3)
            kill = subprocess.run(["killall", name], capture_output=True, text=True, timeout=10)
            self.assertEqual(kill.returncode, 0, kill.stderr)
            self.assertEqual(proc.wait(timeout=5), -signal.SIGTERM)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_disable_file_execs_stock(self):
        conf = self.home / ".config" / "net.corbinmeier.notifications"
        conf.mkdir(parents=True)
        (conf / "disable").touch()
        r = subprocess.run(
            [str(self.shim), "a", "b"], env=self.env, capture_output=True, timeout=10
        )
        self.assertEqual(r.returncode, 0)
        self.assertEqual((self.home / "stock.out").read_text().strip(), "stock a b")
        self.assertFalse(self.count.exists())


if __name__ == "__main__":
    unittest.main()


def _has_name(client, name):
    r = Caller(client, "org.freedesktop.DBus").call(
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "NameHasOwner",
        GLib.Variant("(s)", (name,)),
        "(b)",
    )
    return r.unpack()[0]


def _owner_pid(client, name):
    c = Caller(client, "org.freedesktop.DBus")
    owner = c.call(
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "GetNameOwner",
        GLib.Variant("(s)", (name,)),
        "(s)",
    ).unpack()[0]
    return c.call(
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "GetConnectionUnixProcessID",
        GLib.Variant("(s)", (owner,)),
        "(u)",
    ).unpack()[0]


class RestartTests(unittest.TestCase):
    def daemon_args(self, addr, tmp):
        return [
            "--no-display",
            "--no-run",
            "--no-sound",
            "--bus-address",
            addr,
            "--db",
            str(Path(tmp) / "t.db"),
            "--state-dir",
            str(Path(tmp) / "state"),
            "--config",
            str(Path(tmp) / "config.toml"),
        ]

    def test_restart_signals_only_the_child_and_a_new_pid_takes_both_names(self):
        bus = Gio.TestDBus()
        bus.up()
        try:
            addr = bus.get_bus_address()
            with tempfile.TemporaryDirectory() as tmp:
                shim = Path(tmp) / "cosmic-notifications"
                shim.write_text(SHIM_TEMPLATE.read_text().replace("@REPO@", str(ROOT)))
                shim.chmod(0o755)
                env = {**os.environ, "HOME": tmp, "NOTIFYD_BACKOFF_MIN": "0.2"}
                proc = subprocess.Popen(
                    [str(shim), *self.daemon_args(addr, tmp)], env=env, stderr=subprocess.DEVNULL
                )
                client = connect(addr)
                try:
                    pump_until(
                        lambda: _has_name(client, FDO_IFACE) and _has_name(client, CTL_IFACE),
                        timeout=10,
                    )
                    old = _owner_pid(client, CTL_IFACE)
                    res = subprocess.run(
                        [str(ROOT / "bin" / "notifyd"), "--restart", "--bus-address", addr],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    self.assertEqual(res.returncode, 0, res.stderr)
                    new = _owner_pid(client, CTL_IFACE)
                    self.assertNotEqual(new, old)
                    self.assertEqual(_owner_pid(client, FDO_IFACE), new)
                    self.assertIn(str(old), res.stdout)
                    self.assertIn(str(new), res.stdout)
                    self.assertIsNone(proc.poll(), "the shim must survive the restart")
                finally:
                    proc.send_signal(signal.SIGTERM)
                    proc.wait(timeout=10)
                    client.close_sync(None)
        finally:
            bus.down()

    def test_restart_refuses_a_daemon_that_is_not_under_a_shim(self):
        bus = Gio.TestDBus()
        bus.up()
        try:
            addr = bus.get_bus_address()
            with tempfile.TemporaryDirectory() as tmp:
                daemon = subprocess.Popen(
                    [str(ROOT / "bin" / "notifyd"), *self.daemon_args(addr, tmp)],
                    stderr=subprocess.DEVNULL,
                )
                client = connect(addr)
                try:
                    pump_until(
                        lambda: _has_name(client, FDO_IFACE) and _has_name(client, CTL_IFACE),
                        timeout=10,
                    )
                    res = subprocess.run(
                        [str(ROOT / "bin" / "notifyd"), "--restart", "--bus-address", addr],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    self.assertEqual(res.returncode, 1)
                    self.assertIn("not the cosmic-notifications shim", res.stderr)
                    self.assertIsNone(daemon.poll(), "a refused restart must not signal")
                finally:
                    daemon.send_signal(signal.SIGTERM)
                    daemon.wait(timeout=10)
                    client.close_sync(None)
        finally:
            bus.down()
