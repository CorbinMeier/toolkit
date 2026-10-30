"""Phase 2 wiring: rules + sound hook + DND bypass + body runs, and hot reload of the config dir.

Real Store(":memory:"), Gio.TestDBus, a recording fake display and a fake sound callable, so
nothing is audible and nothing touches the live session or the real config dir.
"""

import json
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path

from gi.repository import Gio, GLib

from notifyd.__main__ import WATCHED_FILES, Runtime, build_daemon, load_config
from notifyd.dbus_server import CTL_IFACE, CTL_PATH, FDO_IFACE, PROPS_IFACE
from notifyd.rules import RuleSet
from notifyd.sound import choose
from notifyd.store import Store
from tests.test_integration import Caller, connect, pump_briefly, pump_until

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_RULES = (ROOT / "examples" / "rules.toml").read_text()

DND_RULES = """
[[rule]]
name = "bypass"
app = "Pager"
dnd_bypass = true
sound = "bell"
"""


class FakeDisplay:
    def __init__(self):
        self.shown, self.closed, self.css = [], [], []
        self.on_action = self.on_closed = None
        self.grace = []

    def show(self, n):
        self.shown.append(n)

    def close(self, id):
        self.closed.append(id)

    def set_user_css(self, css):
        self.css.append(css)

    def set_hover_grace(self, ms):
        self.grace.append(ms)

    def set_hover_max_hold(self, ms):
        pass


class BusCase(unittest.TestCase):
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
        self.sounds = []
        self.c = Caller(self.client, self.server_conn.get_unique_name())
        self.signals = []
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

    def build(self, rules_text="", **kw):
        rules = RuleSet.from_toml(rules_text)
        self.server = build_daemon(
            self.server_conn,
            self.store,
            self.display,
            process=rules.apply,
            sound=self.sounds.append,
            **kw,
        )

    def notify(self, app="App", summary="Hi", body="b", replaces=0, expire=-1, urgency=None):
        hints = {} if urgency is None else {"urgency": GLib.Variant("y", urgency)}
        args = GLib.Variant(
            "(susssasa{sv}i)", (app, replaces, "", summary, body, [], hints, expire)
        )
        return self.c.call(
            "/org/freedesktop/Notifications", FDO_IFACE, "Notify", args, "(u)"
        ).unpack()[0]

    def history(self):
        args = GLib.Variant("(suu)", ("", 0, 50))
        r = self.c.call(CTL_PATH, CTL_IFACE, "GetHistory", args, "(s)")
        return json.loads(r.unpack()[0])

    def set_dnd(self, on):
        args = GLib.Variant("(ssv)", (CTL_IFACE, "DoNotDisturb", GLib.Variant("b", on)))
        self.c.call(CTL_PATH, PROPS_IFACE, "Set", args)

    def rules_signals(self):
        return [p for n, p in self.signals if n == "RulesReloaded"]


class RulesAndSoundTests(BusCase):
    def test_example_rule_sound_chosen(self):
        self.build(EXAMPLE_RULES)
        nid = self.notify("Example App", "Example", "Something happened")
        self.assertEqual([n.id for n in self.display.shown], [nid])
        self.assertEqual(len(self.sounds), 1)
        self.assertEqual(
            choose(self.sounds[0]), (str(Path("~/Music/sounds/example.mp3").expanduser()), None)
        )
        self.assertEqual(self.store.get(nid).matched_rules, ["example sound"])

    def test_urgent_highlight_and_no_timeout(self):
        self.build(EXAMPLE_RULES)
        nid = self.notify("Other", "URGENT: disk", "")
        shown = self.display.shown[0]
        self.assertEqual(shown.timeout_ms, None)
        self.assertEqual(shown.highlights, [{"pattern": r"(?i)\bURGENT\b", "class": "urgent"}])
        stored = self.store.get(nid)
        self.assertIsNone(stored.timeout_ms)
        self.assertEqual(stored.highlights, shown.highlights)

    def test_blocked_and_store_only_recorded_silent(self):
        self.build(
            '[[rule]]\nname="b"\nbody="nope"\nblock=true\n'
            '[[rule]]\nname="s"\nbody="quiet"\nstore_only=true\nsound="bell"\n'
        )
        a = self.notify(body="nope")
        b = self.notify(body="quiet")
        self.assertEqual(self.display.shown, [])
        self.assertEqual(self.sounds, [])
        self.assertEqual(str(self.store.get(a).state), "blocked")
        self.assertEqual(str(self.store.get(b).state), "store_only")
        self.assertEqual({h["id"] for h in self.history()}, {a, b})

    def test_dnd_suppresses_and_bypass_shows_and_sounds(self):
        self.build(DND_RULES)
        self.set_dnd(True)
        quiet = self.notify("Other", "x")
        self.assertEqual(str(self.store.get(quiet).state), "dnd")
        self.assertEqual((self.display.shown, self.sounds), ([], []))
        loud = self.notify("Pager", "wake up")
        self.assertEqual([n.id for n in self.display.shown], [loud])
        self.assertEqual(str(self.store.get(loud).state), "shown")
        self.assertEqual(len(self.sounds), 1)
        self.assertEqual(choose(self.sounds[0])[0], "bell")

    def test_replace_does_not_resound(self):
        self.build(DND_RULES)
        a = self.notify("Pager", "one")
        b = self.notify("Pager", "two", replaces=a)
        self.assertEqual(a, b)
        self.assertEqual(len(self.display.shown), 2)
        self.assertEqual(len(self.sounds), 1)

    def test_replace_of_closed_id_sounds_again(self):
        self.build(DND_RULES)
        a = self.notify("Pager", "one")
        self.display.on_closed(a, 2)
        self.notify("Pager", "two", replaces=a)
        self.assertEqual(len(self.sounds), 2)

    def test_failing_sound_hook_does_not_fail_notify(self):
        def boom(n):
            raise RuntimeError("no audio")

        self.server = build_daemon(self.server_conn, self.store, self.display, sound=boom)
        with self.assertLogs("notifyd.dbus", "ERROR"):
            nid = self.notify()
        self.assertEqual([n.id for n in self.display.shown], [nid])

    def test_sanitized_body_runs(self):
        self.build()
        self.notify(body='<b>bold</b> <script>x</script><a href="javascript:alert(1)">l</a>')
        runs = self.history()[0]["body"]
        self.assertEqual(runs[0], {"text": "bold", "b": True})
        flat = json.dumps(runs)
        self.assertNotIn("<b>", flat)
        self.assertNotIn("javascript:", flat)
        pump_briefly(50)
        added = [p for n, p in self.signals if n == "NotificationAdded"]
        self.assertEqual(json.loads(added[0][1])["body"], runs)

    def test_emit_rules_reloaded(self):
        self.build()
        self.server.emit_rules_reloaded(False, "bad")
        pump_until(lambda: self.rules_signals())
        self.assertEqual(self.rules_signals(), [(False, "bad")])


class HotReloadTests(BusCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.rt = None
        self.monitor = None

    def tearDown(self):
        if self.monitor is not None:
            self.monitor.cancel()
        super().tearDown()

    def start(self, rules=None, config=None, style=None):
        for name, text in (("rules.toml", rules), ("config.toml", config), ("style.css", style)):
            if text is not None:
                (self.dir / name).write_text(text)
        cfg_path = self.dir / "config.toml"
        self.rt = Runtime(cfg_path, self.display, load_config(cfg_path))
        self.rt.load_initial_rules()
        self.rt.apply_style()
        self.server = build_daemon(
            self.server_conn,
            self.store,
            self.display,
            process=self.rt.process,
            sound=self.sounds.append,
            timeouts=self.rt.timeouts,
        )
        self.rt.on_rules_reloaded = self.server.emit_rules_reloaded
        self.monitor = self.rt.watch()

    def write_atomic(self, name, text):
        """Save the way editors do: write a temp file, rename over the target."""
        tmp = self.dir / f".{name}.swp"
        tmp.write_text(text)
        tmp.rename(self.dir / name)

    def test_valid_rules_edit_swaps_and_signals(self):
        self.start(rules='[[rule]]\nname="a"\napp="X"\nsound="one"\n')
        self.notify("X")
        self.assertEqual(choose(self.sounds[-1])[0], "one")
        self.write_atomic("rules.toml", '[[rule]]\nname="a"\napp="X"\nsound="two"\n')
        pump_until(lambda: self.rules_signals())
        self.assertEqual(self.rules_signals(), [(True, "")])
        self.notify("X")
        self.assertEqual(choose(self.sounds[-1])[0], "two")

    def test_broken_rules_edit_keeps_old_rules(self):
        self.start(rules='[[rule]]\nname="a"\napp="X"\nsound="one"\n')
        with self.assertLogs("notifyd", "ERROR"):
            self.write_atomic("rules.toml", '[[rule]]\nname="a"\nsumary="oops"\n')
            pump_until(lambda: self.rules_signals())
        ok, msg = self.rules_signals()[0]
        self.assertFalse(ok)
        self.assertIn("sumary", msg)
        self.notify("X")
        self.assertEqual(choose(self.sounds[-1])[0], "one")

    def test_startup_with_broken_rules_is_empty(self):
        with self.assertLogs("notifyd", "ERROR"):
            self.start(rules="not = = toml")
        self.assertEqual(len(self.rt.rules), 0)
        self.assertIsNotNone(self.rt.rules_error)

    def test_style_edit_sets_user_css(self):
        self.start(style="a{}")
        self.assertEqual(self.display.css, ["\na{}"])
        self.write_atomic("style.css", "b{color:red}")
        pump_until(lambda: len(self.display.css) == 2)
        self.assertEqual(self.display.css[-1], "\nb{color:red}")

    def test_missing_style_is_empty_css(self):
        self.start()
        self.assertEqual(self.display.css, ["\n"])

    def test_theme_comes_before_style_and_is_hot_applied(self):
        (self.dir / "theme.css").write_text(":root{--nc-bg:red}")
        self.start(style="a{}")
        self.assertEqual(self.display.css, [":root{--nc-bg:red}\na{}"])
        self.write_atomic("theme.css", ":root{--nc-bg:blue}")
        pump_until(lambda: len(self.display.css) == 2)
        self.assertEqual(self.display.css[-1], ":root{--nc-bg:blue}\na{}")
        self.assertIn("theme.css", WATCHED_FILES)

    def test_sound_config_edit_applies_live(self):
        self.start(config="[sound]\nmaster_volume = 100\n")
        self.assertEqual(self.rt.player._master, 100)
        self.write_atomic("config.toml", '[sound]\ndefault = "bell"\nmaster_volume = 40\n')
        pump_until(lambda: self.rt.player._master == 40)
        self.assertEqual(self.rt.player._default, "bell")
        self.write_atomic("config.toml", '[sound]\n# default = "bell"\nmaster_volume = 40\n')
        pump_until(lambda: self.rt.player._default is None)

    def test_toasts_change_logs_restart_required(self):
        self.start(config="[toasts]\nmargin = 4\n")
        with self.assertLogs("notifyd", "WARNING") as logs:
            self.write_atomic("config.toml", "[toasts]\nmargin = 20\n")
            pump_until(lambda: self.rt.config.margin != 4 or logs.records, timeout=3)
        self.assertTrue(any("restart required" in r.getMessage() for r in logs.records))

    def test_toast_timeouts_hot_reload_for_later_notifications(self):
        self.start(config="[toasts]\ntimeout_normal = 4000\n")
        self.notify()
        self.assertEqual(self.display.shown[-1].timeout_ms, 4000)
        with self.assertNoLogs("notifyd", "WARNING"):
            self.write_atomic(
                "config.toml",
                "[toasts]\ntimeout_normal = 6000\ntimeout_critical = 9000\n"
                "respect_app_timeout = false\nhover_leave_grace_ms = 250\n",
            )
            pump_until(lambda: self.display.grace[-1:] == [250])
        self.assertEqual(self.display.shown[-1].timeout_ms, 4000)  # already shown: unchanged
        self.notify()
        self.assertEqual(self.display.shown[-1].timeout_ms, 6000)
        self.notify(urgency=2)
        self.assertEqual(self.display.shown[-1].timeout_ms, 9000)
        self.notify(expire=1234)  # respect_app_timeout is now false
        self.assertEqual(self.display.shown[-1].timeout_ms, 6000)
        self.assertEqual(self.rt.config.timeout_normal, 6000)

    def test_removing_toast_keys_restores_defaults(self):
        self.start(config="[toasts]\ntimeout_low = 100\n")
        self.write_atomic("config.toml", "[toasts]\n")
        pump_until(lambda: self.rt.timeouts.low == 5000)
        self.notify(urgency=0)
        self.assertEqual(self.display.shown[-1].timeout_ms, 5000)

    def test_rule_timeout_overrides_config_and_app(self):
        self.start(
            rules='[[rule]]\nname="r"\napp="X"\ntimeout = 777\n'
            '[[rule]]\nname="n"\napp="Y"\ntimeout = "never"\n',
            config="[toasts]\ntimeout_normal = 4000\n",
        )
        self.notify("X", expire=1234)
        self.assertEqual(self.display.shown[-1].timeout_ms, 777)
        self.notify("Y", expire=1234)
        self.assertIsNone(self.display.shown[-1].timeout_ms)
        self.notify("Z", expire=1234)
        self.assertEqual(self.display.shown[-1].timeout_ms, 1234)

    def test_burst_of_writes_reloads_once(self):
        self.start(rules="")
        for i in range(3):
            self.write_atomic("rules.toml", f'[[rule]]\nname="r{i}"\n')
        pump_until(lambda: self.rules_signals())
        pump_briefly(600)
        self.assertEqual(len(self.rules_signals()), 1)
        self.assertEqual(self.rt.rules.names, ["r2"])


class ToastConfigTests(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            p.write_text(text)
            return load_config(p)

    def test_defaults(self):
        cfg = self.load("")
        self.assertEqual(
            (cfg.timeout_low, cfg.timeout_normal, cfg.timeout_critical), (5000, 10000, None)
        )
        self.assertTrue(cfg.respect_app_timeout)
        self.assertEqual(cfg.hover_leave_grace_ms, 1000)
        self.assertEqual(cfg.hover_max_hold_ms, 120000)

    def test_hover_max_hold_valid_and_invalid(self):
        self.assertEqual(self.load("[toasts]\nhover_max_hold_ms = 0\n").hover_max_hold_ms, 0)
        self.assertEqual(self.load("[toasts]\nhover_max_hold_ms = 500\n").hover_max_hold_ms, 500)
        for bad in ("-1", '"x"', "true", "1.5"):
            with self.assertLogs("notifyd", "ERROR"):
                cfg = self.load(f"[toasts]\nhover_max_hold_ms = {bad}\n")
            self.assertEqual(cfg.hover_max_hold_ms, 120000, bad)

    def test_valid_values(self):
        cfg = self.load(
            '[toasts]\ntimeout_low = 1\ntimeout_normal = "never"\ntimeout_critical = 30000\n'
            "respect_app_timeout = false\nhover_leave_grace_ms = 0\n"
        )
        got = (cfg.timeout_low, cfg.timeout_normal, cfg.timeout_critical)
        self.assertEqual(got, (1, None, 30000))
        self.assertFalse(cfg.respect_app_timeout)
        self.assertEqual(cfg.hover_leave_grace_ms, 0)

    def test_each_key_validated_individually(self):
        with self.assertLogs("notifyd", "ERROR") as logs:
            cfg = self.load(
                '[toasts]\ntimeout_low = 0\ntimeout_normal = "soon"\ntimeout_critical = 2500\n'
                'respect_app_timeout = "yes"\nhover_leave_grace_ms = -5\n'
            )
        self.assertEqual(
            (cfg.timeout_low, cfg.timeout_normal, cfg.timeout_critical), (5000, 10000, 2500)
        )
        self.assertTrue(cfg.respect_app_timeout)
        self.assertEqual(cfg.hover_leave_grace_ms, 1000)
        self.assertEqual(len(logs.records), 4)

    def test_bool_and_float_rejected(self):
        with self.assertLogs("notifyd", "ERROR"):
            cfg = self.load("[toasts]\ntimeout_low = true\ntimeout_normal = 1.5\n")
        self.assertEqual((cfg.timeout_low, cfg.timeout_normal), (5000, 10000))


class SubprocessSmokeTest(unittest.TestCase):
    def test_daemon_with_rules_file(self):
        bus = Gio.TestDBus()
        bus.up()
        try:
            addr = bus.get_bus_address()
            with tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                (tmp / "rules.toml").write_text('[[rule]]\nname="mute"\nbody="shh"\nblock=true\n')
                (tmp / "style.css").write_text("/* hi */")
                proc = subprocess.Popen(
                    [
                        str(ROOT / "bin" / "notifyd"),
                        "--bus-address",
                        addr,
                        "--db",
                        str(tmp / "t.db"),
                        "--config",
                        str(tmp / "config.toml"),
                        "--no-display",
                    ],
                    stderr=subprocess.PIPE,
                    text=True,
                )
                client = connect(addr)
                try:
                    dbus = Caller(client, "org.freedesktop.DBus")

                    def owned():
                        args = GLib.Variant("(s)", (CTL_IFACE,))
                        return dbus.call(
                            "/org/freedesktop/DBus",
                            "org.freedesktop.DBus",
                            "NameHasOwner",
                            args,
                            "(b)",
                        ).unpack()[0]

                    pump_until(owned, timeout=10)
                    c = Caller(client, FDO_IFACE)
                    c.notify("quiet", "shh")
                    c.notify("loud", "hello")
                    hist = Caller(client, CTL_IFACE).history()
                    states = {h["summary"]: h["state"] for h in hist}
                    self.assertEqual(states, {"quiet": "blocked", "loud": "shown"})
                    proc.send_signal(signal.SIGTERM)
                    self.assertEqual(proc.wait(timeout=10), 0)
                    err = proc.stderr.read()
                finally:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait()
                    proc.stderr.close()
                    client.close_sync(None)
            self.assertIn("loaded 1 rules", err)
        finally:
            bus.down()


if __name__ == "__main__":
    unittest.main()
