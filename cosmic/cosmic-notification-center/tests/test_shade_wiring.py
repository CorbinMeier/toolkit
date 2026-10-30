"""Shade wiring: build_daemon with a fake display/center/shade, a real Store, a private bus."""

import os
import signal
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from gi.repository import Gio, GLib

from notifyd.__main__ import (
    Config,
    NullShade,
    Runtime,
    build_daemon,
    load_config,
    parse_args,
    shade_callbacks,
)
from notifyd.dbus_server import CTL_IFACE, FDO_IFACE, FDO_PATH
from notifyd.model import CloseReason
from notifyd.sound import NullPlayer
from tests.test_integration import Caller, connect, pump_briefly, pump_until
from tests.test_wiring import FakeCenter, WiringCase

ROOT = Path(__file__).resolve().parent.parent


class FakeShade:
    """Records calls; `cb` holds the callbacks the real ShadeWindow would get."""

    def __init__(self, server, runtime):
        self.cb = shade_callbacks(server, runtime)
        self.calls = []

    def toggle(self):
        self.calls.append(("toggle",))

    def show(self):
        self.calls.append(("show",))

    def hide(self):
        self.calls.append(("hide",))

    def notify_added(self, n=None):
        self.calls.append(("added", n.id))

    def refresh(self):
        self.calls.append(("refresh",))

    def set_user_css(self, css):
        self.calls.append(("css", css))

    def set_dnd(self, on):
        self.calls.append(("dnd", on))

    def configure(self, **kw):
        self.calls.append(("configure", kw))

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


class ShadeCase(WiringCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        super().tearDown()
        self.tmp.cleanup()

    def build(self, rules_text=""):
        self.runtime = Runtime(
            self.dir / "config.toml", self.display, Config(), player=NullPlayer()
        )
        self.server = build_daemon(
            self.server_conn,
            self.store,
            self.display,
            FakeCenter,
            lambda server, runtime: FakeShade(server, runtime),
            self.runtime,
            process=self.runtime.process,
        )
        self.center, self.shade = self.server.center, self.server.shade
        return self.server

    def closed(self):
        return self.names("NotificationClosed")


class ShadeBusTests(ShadeCase):
    def test_toggle_and_show_reach_the_shade_not_the_center(self):
        self.build()
        self.ctl("ToggleShade")
        self.ctl("ShowShade")
        self.assertEqual(self.shade.of("toggle") + self.shade.of("show"), [("toggle",), ("show",)])
        self.assertEqual(self.center.calls, [])

    def test_null_shade_without_a_factory(self):
        self.server = build_daemon(self.server_conn, self.store, self.display)
        self.assertIsInstance(self.server.shade, NullShade)
        self.ctl("ToggleShade")
        self.ctl("ShowShade")
        self.notify()

    def test_runtime_gets_center_and_shade(self):
        self.build()
        self.assertIs(self.runtime.shade, self.shade)
        self.assertIs(self.runtime.center, self.center)

    def test_open_details_and_center_go_to_the_center(self):
        self.build()
        self.shade.cb["on_open_details"](7)
        self.shade.cb["on_open_center"]()
        self.assertEqual(self.center.calls, [("show_notification", 7), ("show",)])

    def test_invoke_action_and_mark_read_go_to_the_server(self):
        self.build()
        nid = self.notify("act", actions=["go", "Go"])
        self.assertTrue(self.shade.cb["on_invoke_action"](nid, "go"))
        pump_briefly()
        self.assertEqual(self.names("ActionInvoked"), [(nid, "go")])
        other = self.notify("read me")
        self.shade.cb["on_mark_read"]([other])
        self.assertIsNotNone(self.store.get(other).read_at)


class DismissSplitTests(ShadeCase):
    def test_open_is_signalled_expired_is_quiet_both_leave_the_shade(self):
        self.build()
        open_id = self.notify("open")
        expired_id = self.notify("expired")
        self.server.on_closed(expired_id, CloseReason.EXPIRED)
        pump_briefly()
        self.signals.clear()
        self.assertEqual({n.id for n in self.store.shade_items()}, {open_id, expired_id})

        self.shade.cb["on_dismiss"]([open_id, expired_id])
        pump_briefly()

        self.assertEqual(self.closed(), [(open_id, int(CloseReason.DISMISSED))])
        self.assertEqual(self.display.closed, [open_id])
        self.assertEqual(self.store.shade_items(), [])
        expired = self.store.get(expired_id)
        self.assertEqual(expired.close_reason, CloseReason.DISMISSED)
        self.assertIsNotNone(expired.closed_at)

    def test_unknown_and_duplicate_ids_are_ignored(self):
        self.build()
        nid = self.notify()
        self.signals.clear()
        self.shade.cb["on_dismiss"]([9999, nid, nid])
        pump_briefly()
        self.assertEqual(self.closed(), [(nid, int(CloseReason.DISMISSED))])

    def test_only_expired_closes_nothing_on_the_bus(self):
        self.build()
        nid = self.notify()
        self.server.on_closed(nid, CloseReason.EXPIRED)
        pump_briefly()
        self.signals.clear()
        self.shade.cb["on_dismiss"]([nid])
        pump_briefly()
        self.assertEqual(self.closed(), [])
        self.assertEqual(self.display.closed, [])
        self.assertEqual(self.store.shade_items(), [])
        self.assertIn(("refresh",), self.shade.calls)


class DndSyncTests(ShadeCase):
    def dnd_signals(self):
        return [
            p[1]["DoNotDisturb"]
            for n, p in self.signals
            if n == "PropertiesChanged" and "DoNotDisturb" in p[1]
        ]

    def test_bus_change_reaches_the_shade(self):
        self.build()
        self.assertFalse(self.shade.cb["get_dnd"]())
        self.set_dnd(True)
        self.assertTrue(self.shade.cb["get_dnd"]())
        self.assertEqual(self.shade.of("dnd"), [("dnd", True)])
        self.set_dnd(True)  # no change: no second push
        self.assertEqual(self.shade.of("dnd"), [("dnd", True)])

    def test_shade_change_fires_properties_changed_and_applies_to_notifications(self):
        self.build()
        self.shade.cb["on_set_dnd"](True)
        pump_until(lambda: self.dnd_signals() == [True])
        nid = self.notify("quiet")
        self.assertEqual(str(self.store.get(nid).state), "dnd")
        self.assertEqual(self.display.shown, [])
        self.shade.cb["on_set_dnd"](False)
        pump_until(lambda: self.dnd_signals() == [True, False])
        # the observer pushed the new value back to the shade once per change, no loop
        self.assertEqual(self.shade.of("dnd"), [("dnd", True), ("dnd", False)])
        self.shade.cb["on_set_dnd"](False)
        pump_briefly()
        self.assertEqual(len(self.shade.of("dnd")), 2)
        self.assertEqual(self.dnd_signals(), [True, False])

    def test_dnd_change_does_not_refresh_the_list(self):
        self.build()
        self.set_dnd(True)
        self.assertEqual(self.shade.of("refresh"), [])


class AppPrefsTests(ShadeCase):
    def test_prefs_set_from_the_shade_apply_to_the_next_notification(self):
        self.build()
        first = self.notify("before")
        self.assertEqual(str(self.store.get(first).state), "shown")
        self.shade.cb["on_set_app_prefs"]("App", {"muted": True})
        self.assertTrue(self.shade.cb["get_app_prefs"]("App")["muted"])
        self.assertTrue((self.dir / "app-settings.toml").is_file())
        second = self.notify("after")
        self.assertEqual(str(self.store.get(second).state), "store_only")
        self.assertEqual(self.display.shown, [first])

    def test_invalid_prefs_raise_value_error(self):
        self.build()
        with self.assertRaises(ValueError):
            self.shade.cb["on_set_app_prefs"]("App", {"muted": "yes"})


class ObserverTests(ShadeCase):
    def test_added_fires_for_every_state(self):
        self.build('[[rule]]\nname = "b"\nsummary = "block"\nblock = true\n')
        a = self.notify("plain")
        self.assertEqual(self.shade.of("added"), [("added", a)])

    def test_changes_call_refresh(self):
        self.build()
        a, b, c = (self.notify(f"n{i}") for i in range(3))
        self.assertEqual(self.shade.of("refresh"), [])  # a Notify is `added`, not `changed`
        self.ctl("MarkRead", GLib.Variant("(au)", ([a],)))
        self.assertEqual(len(self.shade.of("refresh")), 1)
        self.ctl("Dismiss", GLib.Variant("(u)", (b,)))
        self.assertEqual(len(self.shade.of("refresh")), 2)  # exactly one per dismiss
        self.c.call(FDO_PATH, FDO_IFACE, "CloseNotification", GLib.Variant("(u)", (c,)), "()")
        self.assertEqual(len(self.shade.of("refresh")), 3)
        self.ctl("Delete", GLib.Variant("(au)", ([a],)))
        self.assertEqual(len(self.shade.of("refresh")), 4)
        self.ctl("ClearHistory")
        self.assertEqual(len(self.shade.of("refresh")), 5)

    def test_toast_expiry_and_center_callbacks_refresh(self):
        self.build()
        nid = self.notify()
        self.server.on_closed(nid, CloseReason.EXPIRED)  # what the toast host calls
        self.assertEqual(len(self.shade.of("refresh")), 1)
        self.server.on_closed(nid, CloseReason.EXPIRED)  # already closed: ignored
        self.assertEqual(len(self.shade.of("refresh")), 1)
        other = self.notify("two")
        self.server.mark_read([other])  # the center's callbacks are the server's methods
        self.assertEqual(len(self.shade.of("refresh")), 2)

    def test_observer_errors_do_not_break_the_operation(self):
        self.build()

        def boom():
            raise RuntimeError("x")

        self.server.add_changed_observer(boom)
        nid = self.notify()
        with self.assertLogs("notifyd.dbus", "ERROR"):
            self.server.mark_read([nid])
        self.assertIsNotNone(self.store.get(nid).read_at)

    def test_style_reaches_the_shade(self):
        self.build()
        (self.dir / "style.css").write_text(".x{}")
        self.runtime.apply_style()
        self.assertEqual(self.shade.of("css"), [("css", "\n.x{}")])
        self.assertEqual(self.display.css, ["\n.x{}"])
        self.assertEqual(self.center.css, ["\n.x{}"])


class ShadeConfigTests(ShadeCase):
    def write(self, text):
        (self.dir / "config.toml").write_text(text)

    def test_defaults(self):
        cfg = load_config(self.dir / "missing.toml")
        self.assertEqual(
            (cfg.shade_anchor, cfg.shade_margin, cfg.shade_width), ("top-right", 8, 420)
        )

    def test_each_key_is_validated_individually(self):
        self.write('[shade]\nanchor = "middle"\nmargin = 30\nwidth = 5\n')
        with self.assertLogs("notifyd", "ERROR") as logs:
            cfg = load_config(self.dir / "config.toml")
        self.assertEqual(
            (cfg.shade_anchor, cfg.shade_margin, cfg.shade_width), ("top-right", 30, 420)
        )
        self.assertEqual(len(logs.output), 2)
        self.write('[shade]\nanchor = "bottom-left"\nmargin = -1\nwidth = 500\n')
        with self.assertLogs("notifyd", "ERROR"):
            cfg = load_config(self.dir / "config.toml")
        self.assertEqual(
            (cfg.shade_anchor, cfg.shade_margin, cfg.shade_width), ("bottom-left", 8, 500)
        )
        self.write('[shade]\nmargin = true\nwidth = "wide"\n')
        with self.assertLogs("notifyd", "ERROR"):
            cfg = load_config(self.dir / "config.toml")
        self.assertEqual((cfg.shade_margin, cfg.shade_width), (8, 420))

    def test_shade_must_be_a_table(self):
        self.write('shade = "x"\n')
        with self.assertLogs("notifyd", "ERROR"):
            cfg = load_config(self.dir / "config.toml")
        self.assertEqual(cfg.shade_width, 420)

    def test_reload_configures_the_shade_live(self):
        self.build()
        self.write('[shade]\nanchor = "bottom-center"\nmargin = 20\nwidth = 600\n')
        self.runtime.reload_config()
        self.assertEqual(
            self.shade.of("configure"),
            [("configure", {"anchor": "bottom-center", "margin": 20, "width": 600})],
        )
        self.runtime.reload_config()  # unchanged: nothing more
        self.assertEqual(len(self.shade.of("configure")), 1)
        self.write('[shade]\nanchor = "bottom-center"\nmargin = 20\nwidth = 99999\n')
        with self.assertLogs("notifyd", "ERROR"):
            self.runtime.reload_config()  # bad width falls back to the default, others hold
        self.assertEqual(
            self.shade.of("configure")[-1],
            ("configure", {"anchor": "bottom-center", "margin": 20, "width": 420}),
        )

    def test_toast_anchor_still_needs_a_restart(self):
        self.build()
        self.write('[toasts]\nanchor = "bottom-left"\n')
        with self.assertLogs("notifyd", "WARNING"):
            self.runtime.reload_config()
        self.assertEqual(self.shade.of("configure"), [])


class CliArgsTests(unittest.TestCase):
    def test_flags_parse(self):
        self.assertTrue(parse_args(["--toggle-shade"]).toggle_shade)
        self.assertTrue(parse_args(["--no-sound"]).no_sound)
        self.assertFalse(parse_args([]).no_sound)


def make_shims(directory: Path, marker: Path) -> None:
    for name in ("pw-play", "paplay", "canberra-gtk-play"):
        f = directory / name
        f.write_text(f'#!/bin/sh\necho "{name} $*" >> "{marker}"\n')
        f.chmod(f.stat().st_mode | stat.S_IEXEC)


class DaemonProcessTests(unittest.TestCase):
    def setUp(self):
        self.bus = Gio.TestDBus()
        self.bus.up()
        self.addr = self.bus.get_bus_address()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.marker = self.dir / "played"
        shims = self.dir / "shims"
        shims.mkdir()
        make_shims(shims, self.marker)
        self.env = dict(os.environ, PATH=f"{shims}:{os.environ['PATH']}")
        self.client = connect(self.addr)

    def tearDown(self):
        self.client.close_sync(None)
        self.tmp.cleanup()
        self.bus.down()

    def start(self, *flags):
        proc = subprocess.Popen(
            [
                str(ROOT / "bin" / "notifyd"),
                "--bus-address",
                self.addr,
                "--db",
                str(self.dir / "t.db"),
                "--config",
                str(self.dir / "none.toml"),
                *flags,
            ],
            stderr=subprocess.PIPE,
            text=True,
            env=self.env,
        )
        self._err = []
        self._reader = threading.Thread(
            target=lambda: self._err.extend(iter(proc.stderr.readline, "")), daemon=True
        )
        self._reader.start()
        dbus = Caller(self.client, "org.freedesktop.DBus")

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
        # Owning the bus name only means the request landed; the daemon logs "acquired" from
        # its main loop, after its SIGTERM handler is installed. Wait for both before returning.
        want = (f"acquired {FDO_IFACE}", f"acquired {CTL_IFACE}")
        pump_until(lambda: all(any(w in ln for ln in self._err) for w in want), timeout=10)
        return proc

    def stop(self, proc):
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        self._reader.join(timeout=10)
        proc.stderr.close()
        return "".join(self._err)

    def test_toggle_shade_client_calls_running_daemon(self):
        proc = self.start("--no-display")
        try:
            r = subprocess.run(
                [str(ROOT / "bin" / "notifyd"), "--bus-address", self.addr, "--toggle-shade"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
        finally:
            self.stop(proc)
        r = subprocess.run(
            [str(ROOT / "bin" / "notifyd"), "--bus-address", self.addr, "--toggle-shade"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(r.returncode, 1)
        self.assertIn("ToggleShade failed", r.stderr)

    def test_shim_control_actually_records_a_play(self):
        subprocess.run(["pw-play", "x"], env=self.env, check=True)
        self.assertTrue(self.marker.exists())

    def test_no_display_never_spawns_a_sound_player(self):
        proc = self.start("--no-display")
        try:
            caller = Caller(self.client, CTL_IFACE)
            for hints in ({}, {"sound-name": GLib.Variant("s", "bell")}):
                args = GLib.Variant("(susssasa{sv}i)", ("App", 0, "", "ding", "b", [], hints, -1))
                caller.call(FDO_PATH, FDO_IFACE, "Notify", args, "(u)")
            pump_briefly(700)
        finally:
            err = self.stop(proc)
        self.assertFalse(self.marker.exists(), "a sound player was spawned")
        self.assertIn("sound=off", err)
        self.assertNotIn("Traceback", err)

    def test_no_sound_flag_is_accepted_and_logged(self):
        proc = self.start("--no-display", "--no-sound")
        err = self.stop(proc)
        self.assertIn("sound=off", err)
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()


class ClearToastsOnOpenTests(ShadeCase):
    """Opening the shade closes every toast as EXPIRED; they stay in the shade."""

    def build_real_host(self, cfg_text=None):
        from notifyd.toasts import ExpiryTimers, ToastHost

        class QuietHost(ToastHost):
            """The real bookkeeping, no GTK: show registers the toast and starts its timer."""

            def show(self, n):
                self._active.add(n.id)
                self._hover.forget(n.id)
                self._timers_obj().start(n.id, 60000, self._expire)

        handles = iter(range(1, 1000))
        self.host = QuietHost(ROOT / "web", lambda i, k: None, lambda i, r: None)
        self.host._eval = lambda script: None
        self.host._timers = ExpiryTimers(lambda ms, fn: next(handles), lambda h: None)
        self.display = self.host
        if cfg_text is not None:
            (self.dir / "config.toml").write_text(cfg_text)
        self.runtime = Runtime(
            self.dir / "config.toml",
            self.display,
            load_config(self.dir / "config.toml"),
            player=NullPlayer(),
        )
        self.server = build_daemon(
            self.server_conn,
            self.store,
            self.host,
            FakeCenter,
            lambda server, runtime: FakeShade(server, runtime),
            self.runtime,
            process=self.runtime.process,
        )
        self.shade = self.server.shade
        return self.server

    def three_toasts(self):
        sticky, timed, hovered = self.notify("sticky"), self.notify("timed"), self.notify("hov")
        self.host._timers_obj().start(sticky, None, self.host._expire)  # sticky: no timer
        self.host._hover.set_inside(True, "gtk")
        self.assertEqual(self.host._active, {sticky, timed, hovered})
        return sticky, timed, hovered

    def unseen(self):
        from notifyd.dbus_server import CTL_IFACE, CTL_PATH, PROPS_IFACE

        args = GLib.Variant("(ss)", (CTL_IFACE, "UnseenCount"))
        return self.c.call(CTL_PATH, PROPS_IFACE, "Get", args, "(v)").unpack()[0]

    def shade_ids(self):
        return {n.id for n in self.store.shade_items()}

    def test_open_closes_all_expired_and_keeps_them_in_the_shade(self):
        self.build_real_host()
        ids = self.three_toasts()
        read_before = {i: self.store.get(i).read_at for i in ids}
        self.assertEqual(self.unseen(), 3)
        self.ctl("ShowShade")
        pump_briefly()
        self.assertEqual(self.host._active, set())
        self.assertFalse(self.host._hover.held)
        self.assertEqual(self.host._timers_obj().pending(), set())
        self.assertEqual(sorted(self.names("NotificationClosed")), sorted((i, 1) for i in ids))
        for i in ids:
            n = self.store.get(i)
            self.assertEqual(n.close_reason, CloseReason.EXPIRED)
            self.assertEqual(n.read_at, read_before[i])
        self.assertTrue(set(ids) <= self.shade_ids())
        self.assertEqual(self.unseen(), 0)
        self.ctl("ShowShade")  # again: nothing left, no second signal
        pump_briefly()
        self.assertEqual(len(self.names("NotificationClosed")), 3)

    def test_toggle_to_hide_does_nothing(self):
        self.build_real_host()
        self.shade.visible = True  # the shade is already open

        self.shade.is_visible = lambda: self.shade.visible

        def toggle():
            self.shade.visible = not self.shade.visible

        self.shade.toggle = toggle
        from notifyd.__main__ import _mark_seen_on_open

        _mark_seen_on_open(
            self.server, self.shade, ("toggle",), "is_visible", self.server.clear_toasts_to_shade
        )
        self.three_toasts()
        self.shade.toggle()  # visible -> hidden
        pump_briefly()
        self.assertEqual(len(self.host._active), 3)
        self.assertEqual(self.names("NotificationClosed"), [])
        self.shade.toggle()  # hidden -> visible: now it clears
        pump_briefly()
        self.assertEqual(self.host._active, set())

    def test_key_false_closes_nothing(self):
        self.build_real_host("[shade]\nclear_toasts_on_open = false\n")
        self.three_toasts()
        self.ctl("ShowShade")
        pump_briefly()
        self.assertEqual(len(self.host._active), 3)
        self.assertEqual(self.names("NotificationClosed"), [])

    def test_hot_reload_of_the_key(self):
        self.build_real_host()
        cfg = self.dir / "config.toml"
        cfg.write_text("[shade]\nclear_toasts_on_open = false\n")
        self.runtime.reload_config()
        self.assertFalse(self.runtime.config.shade_clear_toasts_on_open)
        self.three_toasts()
        self.ctl("ShowShade")
        pump_briefly()
        self.assertEqual(len(self.host._active), 3)
        cfg.write_text("[shade]\nclear_toasts_on_open = true\n")
        self.runtime.reload_config()
        self.ctl("ShowShade")
        pump_briefly()
        self.assertEqual(self.host._active, set())
        self.assertEqual(len(self.names("NotificationClosed")), 3)
