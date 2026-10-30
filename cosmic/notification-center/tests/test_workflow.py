"""Phase 3b daemon wiring: rule scripts, button presses, snooze, PressButton/GetRuns, CLI."""

import json
import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path

from gi.repository import GLib

from notifyd.__main__ import Config, NullCenter, Runtime, build_daemon
from notifyd.dbus_server import CTL_IFACE, CTL_PATH, FDO_IFACE, FDO_PATH
from notifyd.rules import RuleSet
from notifyd.runner import Runner
from notifyd.sound import NullPlayer
from notifyd.store import Store
from tests.test_integration import Caller, connect, pump_briefly, pump_until
from tests.test_shade_wiring import FakeShade
from tests.test_wiring import FakeCenter, WiringCase

ROOT = Path(__file__).resolve().parent.parent


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.on_finished = None
        self.shut = False

    def run(self, n, spec, *, trigger, label=None):
        self.calls.append((n.id, dict(spec), trigger, label))
        return len(self.calls)

    def shutdown(self):
        self.shut = True


BUTTONS = """
[[rule]]
name = "btn"
body = "link"
run = "true"
[[rule.buttons]]
label = "Details"
builtin = "details"
[[rule.buttons]]
label = "Copy body"
builtin = "copy-body"
[[rule.buttons]]
label = "Copy summary"
builtin = "copy-summary"
[[rule.buttons]]
label = "Open"
builtin = "open-link"
[[rule.buttons]]
label = "Read"
builtin = "mark-read"
close = false
[[rule.buttons]]
label = "Dismiss"
builtin = "dismiss"
"""
# ids: b0 details, b1 copy-body, b2 copy-summary, b3 open-link, b4 mark-read (keeps open),
# b5 dismiss. (MAX_BUTTONS is 6, so the rest live in BUTTONS2.)

BUTTONS2 = """
[[rule]]
name = "btn2"
body = "link"
[[rule.buttons]]
label = "Snooze"
builtin = "snooze"
minutes = 10
[[rule.buttons]]
label = "Run"
run = ["true"]
[[rule.buttons]]
label = "Stay"
builtin = "copy-body"
close = false
[[rule.buttons]]
label = "Leave"
builtin = "copy-body"
close = true
"""
# ids: b0 snooze, b1 run (closes), b2 copy-body keeps open, b3 copy-body closes.


class FlowCase(WiringCase):
    def setUp(self):
        super().setUp()
        self.now = [1000.0]
        self.copied, self.opened = [], []
        self.runner = FakeRunner()
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        super().tearDown()
        self.tmp.cleanup()

    def build(self, rules_text="", runner="fake", shade=True, dnd=False):
        rules = RuleSet.from_toml(rules_text)
        if runner == "fake":
            runner = self.runner
        self.server = build_daemon(
            self.server_conn,
            self.store,
            self.display,
            FakeCenter,
            (lambda s, r: FakeShade(s, r)) if shade else None,
            Runtime(Path(self.tmp.name) / "c.toml", self.display, Config(), player=NullPlayer()),
            process=rules.apply,
            clock=lambda: self.now[0],
            runner=runner,
            clipboard=self.copied.append,
            opener=self.opened.append,
        )
        self.center = self.server.center
        if dnd:
            self.server.set_dnd(True)
        return self.server

    def notify_full(self, summary="Hi", body="b", replaces=0):
        args = GLib.Variant("(susssasa{sv}i)", ("App", replaces, "", summary, body, [], {}, -1))
        return self.c.call(FDO_PATH, FDO_IFACE, "Notify", args, "(u)").unpack()[0]


class RuleRunTests(FlowCase):
    RULES = """
[[rule]]
name = "arr"
run = "a"
[[rule]]
name = "shown"
run = "s"
run_when = "shown"
"""

    def runs(self):
        return [(c[1]["rule"], c[2]) for c in self.runner.calls]

    def test_shown_notification_runs_both(self):
        self.build(self.RULES)
        self.notify_full()
        self.assertEqual(self.runs(), [("arr", "rule"), ("shown", "rule")])
        self.assertEqual(self.runner.calls[0][1]["cmd"], "a")

    def test_blocked_and_store_only_run_arrive_only(self):
        for flag in ("block", "store_only"):
            self.runner.calls.clear()
            self.build(self.RULES + f'[[rule]]\nname = "x"\n{flag} = true\n')
            self.notify_full()
            self.assertEqual(self.runs(), [("arr", "rule")], flag)
            self.server.unregister()

    def test_dnd_runs_arrive_only(self):
        self.build(self.RULES, dnd=True)
        self.notify_full()
        self.assertEqual(self.runs(), [("arr", "rule")])
        self.assertEqual(self.display.shown, [])

    def test_open_replace_does_not_rerun(self):
        self.build(self.RULES)
        nid = self.notify_full()
        self.runner.calls.clear()
        self.notify_full("again", replaces=nid)
        self.assertEqual(self.runner.calls, [])

    def test_replace_of_closed_runs_again(self):
        self.build(self.RULES)
        nid = self.notify_full()
        self.server.dismiss(nid)
        self.runner.calls.clear()
        self.notify_full("again", replaces=nid)
        self.assertEqual(len(self.runner.calls), 2)

    def test_no_runner_is_fine(self):
        self.build(self.RULES, runner=None)
        self.notify_full()
        self.assertEqual(len(self.display.shown), 1)

    def test_runner_error_does_not_fail_notify(self):
        self.runner.run = lambda *a, **k: 1 / 0
        self.build(self.RULES)
        self.assertGreater(self.notify_full(), 0)

    def test_runner_finish_hook_refreshes_center_and_shade(self):
        self.build(self.RULES)
        self.center.refresh = lambda: self.center.calls.append(("refresh",))
        self.assertIsNotNone(self.runner.on_finished)
        self.runner.on_finished(1, 1)
        self.assertIn(("refresh",), self.center.calls)
        self.assertTrue(self.server.shade.of("refresh"))


class WiringAttrTests(FlowCase):
    def test_hosts_get_press_button_and_center_get_runs(self):
        self.build()
        self.assertEqual(self.display.on_button, self.server.press_button)
        self.assertEqual(self.server.shade.on_button, self.server.press_button)
        self.assertEqual(self.center.on_button, self.server.press_button)
        self.assertEqual(self.center.get_runs, self.store.runs_for)


class ButtonTests(FlowCase):
    def setUp(self):
        super().setUp()
        self.build(BUTTONS)
        self.nid = self.notify_full("Sum", '<b>x</b> <a href="https://e.com/p">l</a> link')
        self.display.closed.clear()
        self.signals.clear()

    def press(self, b):
        self.server.press_button(self.nid, b)
        pump_briefly(30)

    def is_open(self):
        return self.store.get(self.nid).closed_at is None

    def test_buttons_persisted_with_ids(self):
        ids = [b["id"] for b in self.store.get(self.nid).buttons]
        self.assertEqual(ids, [f"b{i}" for i in range(6)])

    def test_details(self):
        self.press("b0")
        self.assertIn(("show_notification", self.nid), self.center.calls)
        self.assertTrue(self.is_open())

    def test_copy_body_is_plain_text(self):
        self.press("b1")
        self.assertEqual(self.copied, ["x l link"])
        self.assertTrue(self.is_open())

    def test_copy_summary(self):
        self.press("b2")
        self.assertEqual(self.copied, ["Sum"])

    def test_open_link_first_href(self):
        self.press("b3")
        self.assertEqual(self.opened, ["https://e.com/p"])

    def test_open_link_without_link_does_nothing(self):
        nid = self.notify_full("n", "link only")
        self.server.press_button(nid, "b3")
        self.assertEqual(self.opened, [])

    def test_mark_read_keeps_open_when_close_false(self):
        self.press("b4")
        self.assertIsNotNone(self.store.get(self.nid).read_at)
        self.assertTrue(self.is_open())

    def test_dismiss_closes_once(self):
        self.press("b5")
        self.assertFalse(self.is_open())
        self.assertEqual(self.display.closed, [self.nid])
        self.assertEqual(self.names("NotificationClosed"), [(self.nid, 2)])

    def test_unknown_id_and_button_are_ignored(self):
        self.server.press_button(9999, "b0")
        self.server.press_button(self.nid, "b99")
        self.server.press_button(self.nid, "")
        self.assertEqual(self.copied + self.opened, [])
        self.assertTrue(self.is_open())
        self.assertEqual(self.names("NotificationClosed"), [])

    def test_failing_builtin_is_logged_not_raised(self):
        self.server._clipboard = lambda t: 1 / 0
        with self.assertLogs("notifyd.dbus", "ERROR"):
            self.press("b1")


class ButtonTests2(FlowCase):
    def setUp(self):
        super().setUp()
        self.build(BUTTONS2)
        self.nid = self.notify_full("Sum", "link")
        self.display.closed.clear()
        self.signals.clear()

    def press(self, b):
        self.server.press_button(self.nid, b)
        pump_briefly(30)

    def is_open(self):
        return self.store.get(self.nid).closed_at is None

    def test_run_button_goes_to_runner(self):
        self.press("b1")
        self.assertEqual(
            self.runner.calls[-1],
            (self.nid, {"cmd": ["true"], "rule": "btn2", "timeout": 30}, "button", "Run"),
        )
        self.assertFalse(self.is_open())  # close defaults to true

    def test_close_false_stays_open_close_true_dismisses(self):
        self.press("b2")
        self.assertTrue(self.is_open())
        self.press("b3")
        self.assertFalse(self.is_open())
        self.assertEqual(self.names("NotificationClosed"), [(self.nid, 2)])


class SnoozeTests(FlowCase):
    def setUp(self):
        super().setUp()
        self.build(BUTTONS2)
        self.nid = self.notify_full("Sum", "link")
        self.display.closed.clear()
        self.display.shown.clear()
        self.signals.clear()

    def snooze(self):
        self.server.press_button(self.nid, "b0")
        pump_briefly(30)

    def test_snooze_closes_quietly_and_hides_from_shade(self):
        self.assertEqual([n.id for n in self.store.shade_items()], [self.nid])
        refreshes = len(self.server.shade.of("refresh"))
        self.snooze()
        self.assertEqual(self.display.closed, [self.nid])
        self.assertEqual(self.names("NotificationClosed"), [])
        self.assertIsNone(self.store.get(self.nid).closed_at)
        self.assertEqual(self.store.shade_items(), [])
        self.assertEqual(self.store.snoozed_ids(), [self.nid])
        self.assertGreater(len(self.server.shade.of("refresh")), refreshes)

    def test_not_due_yet_then_reshown_when_due(self):
        self.snooze()
        self.now[0] += 599
        self.assertEqual(self.server.check_snoozes(), 0)
        self.assertEqual(self.display.shown, [])
        self.now[0] += 2
        self.assertEqual(self.server.check_snoozes(), 1)
        self.assertEqual(self.display.shown, [self.nid])
        self.assertEqual(self.store.snoozed_ids(), [])
        self.assertEqual([n.id for n in self.store.shade_items()], [self.nid])
        self.assertIn(("added", self.nid, "shown"), self.center.calls)
        self.assertEqual(self.server.check_snoozes(), 0)

    def test_reshow_clears_a_close(self):
        self.snooze()
        self.store.mark_closed(self.nid, __import__("notifyd.model").model.CloseReason.EXPIRED, 1.0)
        self.now[0] += 700
        self.server.check_snoozes()
        n = self.store.get(self.nid)
        self.assertIsNone(n.closed_at)
        self.assertIsNone(n.close_reason)

    def test_sound_plays_on_reshow(self):
        played = []
        self.server._sound = lambda n: played.append(n.id)
        self.snooze()
        self.now[0] += 700
        self.server.check_snoozes()
        self.assertEqual(played, [self.nid])

    def test_survives_store_reopen(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "h.db"
            st = Store(path)
            from tests.test_runner import mk

            st.record(mk(id=5))
            st.snooze(5, 50.0)
            st.close()
            st = Store(path)
            self.assertEqual(st.due_snoozes(10.0), [])
            self.assertEqual(st.due_snoozes(60.0), [5])
            self.assertEqual(st.shade_items(), [])
            st.close()

    def test_deleted_while_snoozed_is_dropped(self):
        self.snooze()
        self.server.delete([self.nid])
        self.now[0] += 700
        self.assertEqual(self.server.check_snoozes(), 0)
        self.assertEqual(self.display.shown, [])

    def test_app_close_cancels_the_snooze(self):
        self.snooze()
        args = GLib.Variant("(u)", (self.nid,))
        self.c.call(FDO_PATH, FDO_IFACE, "CloseNotification", args)
        self.now[0] += 700
        self.server.check_snoozes()
        self.assertEqual(self.display.shown, [])


class BusTests(FlowCase):
    def test_press_button_and_get_runs(self):
        self.build(BUTTONS)
        nid = self.notify_full("S", "link")
        self.c.call(CTL_PATH, CTL_IFACE, "PressButton", GLib.Variant("(us)", (nid, "b2")))
        self.assertEqual(self.copied, ["S"])
        # unknown ids/buttons are not bus errors
        self.c.call(CTL_PATH, CTL_IFACE, "PressButton", GLib.Variant("(us)", (404, "b2")))
        self.c.call(CTL_PATH, CTL_IFACE, "PressButton", GLib.Variant("(us)", (nid, "zz")))
        rid = self.store.add_run(nid, rule="r", trigger="rule", cmd=["x"], started_at=1.0)
        r = self.c.call(CTL_PATH, CTL_IFACE, "GetRuns", GLib.Variant("(u)", (nid,)), "(s)")
        rows = json.loads(r.unpack()[0])
        self.assertEqual([x["id"] for x in rows], [rid])
        self.assertEqual(rows[0]["cmd"], ["x"])
        r = self.c.call(CTL_PATH, CTL_IFACE, "GetRuns", GLib.Variant("(u)", (404,)), "(s)")
        self.assertEqual(json.loads(r.unpack()[0]), [])


class RealRunnerTests(FlowCase):
    """A real Runner with harmless commands under a temp HOME."""

    def setUp(self):
        super().setUp()
        self.home = Path(self.tmp.name)
        self.real = Runner(self.store, home=self.home)

    def tearDown(self):
        self.real.shutdown()
        super().tearDown()

    def settle(self):
        pump_until(lambda: self.real.running() == 0, 10)
        pump_briefly(30)

    def test_rule_run_and_button_run(self):
        rules = """
[[rule]]
name = "logger"
run = ["sh", "-c", "echo \\"$NC_RULE|$NC_SUMMARY|${NC_BUTTON-none}\\""]
[[rule.buttons]]
label = "Do it"
run = ["sh", "-c", "echo \\"$NC_RULE|$NC_SUMMARY|$NC_BUTTON\\""]
close = false
"""
        self.build(rules, runner=self.real)
        finished = []
        self.real.on_finished = lambda rid, nid: finished.append((rid, nid))
        self.server.shade.refresh = lambda: finished.append("shade")
        nid = self.notify_full("Hello")
        self.settle()
        self.server.press_button(nid, "b0")
        self.settle()
        runs = self.store.runs_for(nid)
        self.assertEqual([r["trigger"] for r in runs], ["rule", "button"])
        self.assertEqual(runs[0]["output_tail"], "logger|Hello|none\n")
        self.assertEqual(runs[1]["output_tail"], "logger|Hello|Do it\n")
        self.assertEqual(runs[1]["button_label"], "Do it")
        self.assertEqual(runs[1]["rule"], "logger")
        self.assertEqual(runs[1]["exit_code"], 0)
        self.assertEqual(len(finished), 2)

    def test_default_finish_hook_is_wired(self):
        self.build("", runner=self.real)
        self.assertEqual(self.real.on_finished, self.server.run_finished)


class CliTests(unittest.TestCase):
    def start(self, tmp, addr, extra=()):
        return subprocess.Popen(
            [
                str(ROOT / "bin" / "notifyd"),
                "--bus-address",
                addr,
                "--db",
                str(Path(tmp) / "t.db"),
                "--config",
                str(Path(tmp) / "config.toml"),
                "--no-display",
                *extra,
            ],
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "HOME": tmp},
        )

    def wait_name(self, client, name):
        dbus = Caller(client, "org.freedesktop.DBus")

        def has():
            r = dbus.call(
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "NameHasOwner",
                GLib.Variant("(s)", (name,)),
                "(b)",
            )
            return r.unpack()[0]

        pump_until(has, timeout=10)

    def test_press_button_cli_no_run(self):
        from gi.repository import Gio

        bus = Gio.TestDBus()
        bus.up()
        try:
            addr = bus.get_bus_address()
            with tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "config.toml").write_text("")
                (Path(tmp) / "rules.toml").write_text(
                    '[[rule]]\nname = "r"\nrun = "touch ran-rule"\n'
                    '[[rule.buttons]]\nlabel = "Read"\nbuiltin = "mark-read"\nclose = false\n'
                    '[[rule.buttons]]\nlabel = "Run"\nrun = "touch ran-button"\nclose = false\n'
                )
                daemon = self.start(tmp, addr, ["--no-run"])
                try:
                    client = connect(addr)
                    self.wait_name(client, CTL_IFACE)
                    c = Caller(client, CTL_IFACE)
                    nid = c.notify("hi")
                    self.assertIsNone(c.history()[0]["read_at"])

                    def press(arg):
                        return subprocess.run(
                            [str(ROOT / "bin" / "notifyd"), "--bus-address", addr,
                             "--press-button", str(arg), "b0"],
                            capture_output=True, text=True, timeout=20,
                        )  # fmt: skip

                    self.assertEqual(press(nid).returncode, 0)
                    pump_until(lambda: c.history()[0]["read_at"] is not None)
                    r = subprocess.run(
                        [str(ROOT / "bin" / "notifyd"), "--bus-address", addr,
                         "--press-button", str(nid), "b1"],
                        capture_output=True, text=True, timeout=20,
                    )  # fmt: skip
                    self.assertEqual(r.returncode, 0, r.stderr)
                    self.assertEqual(press(4242).returncode, 0)  # unknown id: ignored
                    r = subprocess.run(
                        [str(ROOT / "bin" / "notifyd"), "--bus-address", addr,
                         "--press-button", "nope", "b0"],
                        capture_output=True, text=True, timeout=20,
                    )  # fmt: skip
                    self.assertEqual(r.returncode, 1)
                    pump_briefly(300)
                    self.assertFalse((Path(tmp) / "ran-rule").exists())
                    self.assertFalse((Path(tmp) / "ran-button").exists())
                    client.close_sync(None)
                finally:
                    daemon.terminate()
                    daemon.wait(timeout=10)
                    daemon.stderr.close()
        finally:
            bus.down()

    def test_runner_shutdown_on_exit(self):
        from gi.repository import Gio

        bus = Gio.TestDBus()
        bus.up()
        try:
            addr = bus.get_bus_address()
            with tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "config.toml").write_text("")
                (Path(tmp) / "rules.toml").write_text(
                    '[[rule]]\nname = "slow"\nrun = ["sleep", "60"]\n'
                    '[[rule]]\nname = "fast"\nrun = "touch ran-rule"\n'
                )
                daemon = self.start(tmp, addr)
                try:
                    client = connect(addr)
                    self.wait_name(client, CTL_IFACE)
                    Caller(client, CTL_IFACE).notify("hi")
                    pump_until(lambda: (Path(tmp) / "ran-rule").exists(), 10)
                    client.close_sync(None)
                finally:
                    daemon.send_signal(signal.SIGTERM)
                    code = daemon.wait(timeout=15)
                    daemon.stderr.close()
                self.assertEqual(code, 0)
                st = Store(Path(tmp) / "t.db")
                rows = {r["rule"]: r for r in st.runs_for(1)}
                st.close()
                self.assertIsNotNone(rows["slow"]["finished_at"])
                self.assertEqual(rows["slow"]["signal"], 15)
                self.assertEqual(rows["fast"]["exit_code"], 0)
        finally:
            bus.down()


class StoreShadeFilterTests(unittest.TestCase):
    def test_snoozed_hidden_until_cleared(self):
        from tests.test_runner import mk

        st = Store(":memory:")
        st.record(mk(id=1))
        st.record(mk(id=2))
        st.snooze(1, 10.0)
        self.assertEqual([n.id for n in st.shade_items()], [2])
        st.clear_snooze(1)
        self.assertEqual({n.id for n in st.shade_items()}, {1, 2})
        st.close()


class NullHostTests(unittest.TestCase):
    def test_null_center_and_display_accept_on_button(self):
        d = NullCenter()
        d.on_button = lambda i, b: None
        from notifyd.__main__ import NullDisplay

        self.assertTrue(callable(NullDisplay().on_button))


if __name__ == "__main__":
    unittest.main()
