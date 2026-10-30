"""teardown() on the three window hosts and the shutdown order of run(), all without a display."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from notifyd import __main__ as nmain
from notifyd.center import CenterWindow
from notifyd.shade import ShadeWindow
from notifyd.toasts import ToastHost, teardown_webview


class Rec:
    """Records calls made on it, in order, into a shared list."""

    def __init__(self, log, name, fail=()):
        self.log, self.name, self.fail = log, name, set(fail)

    def __getattr__(self, attr):
        def call(*a):
            self.log.append((self.name, attr, a))
            if attr in self.fail:
                raise RuntimeError(f"{self.name}.{attr} boom")
            return 7

        return call


def calls(log, name):
    return [attr for n, attr, _ in log if n == name]


def attach(host, log, with_catcher=False):
    host._ucm, host._ucm_handler = Rec(log, "ucm"), 42
    host._view, host._window = Rec(log, "view"), Rec(log, "win")
    if with_catcher:
        host._catcher = Rec(log, "catcher")


class TeardownWebviewTests(unittest.TestCase):
    def test_order_and_args(self):
        log = []
        teardown_webview(Rec(log, "ucm"), 5, Rec(log, "view"), (Rec(log, "win"), None))
        self.assertEqual(
            [(n, a) for n, a, _ in log],
            [
                ("ucm", "disconnect"),
                ("ucm", "unregister_script_message_handler"),
                ("view", "try_close"),
                ("win", "destroy"),
            ],
        )
        self.assertEqual(log[0][2], (5,))
        self.assertEqual(log[1][2], ("nc",))

    def test_all_none_and_failures_do_not_stop_later_steps(self):
        teardown_webview(None, None, None, (None,))
        log = []
        with self.assertLogs("notifyd", "ERROR"):
            teardown_webview(
                Rec(log, "ucm", fail=["disconnect"]), 1, Rec(log, "view"), (Rec(log, "win"),)
            )
        self.assertEqual(calls(log, "win"), ["destroy"])

    def test_view_without_try_close(self):
        class Old:
            pass

        log = []
        teardown_webview(None, None, Old(), (Rec(log, "win"),))
        self.assertEqual(calls(log, "win"), ["destroy"])


class HostCloseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)

    def toasts(self):
        return ToastHost(self.path, lambda i, k: None, lambda i, r: None)

    def shade(self):
        noop = lambda *a: None  # noqa: E731
        return ShadeWindow(
            None,
            self.path,
            on_dismiss=noop,
            on_mark_read=noop,
            on_open_details=noop,
            on_open_center=noop,
            on_invoke_action=lambda *a: False,
            get_dnd=lambda: False,
            on_set_dnd=noop,
            get_app_prefs=lambda a: {},
            on_set_app_prefs=noop,
            state_path=self.path / "shade.json",
        )

    def center(self):
        noop = lambda *a: None  # noqa: E731
        return CenterWindow(
            None,
            self.path,
            on_invoke_action=lambda *a: False,
            on_dismiss=noop,
            on_mark_read=noop,
            on_delete=noop,
            on_clear=noop,
            state_path=self.path / "center.json",
        )

    def test_never_shown_and_twice(self):
        for make in (self.toasts, self.shade, self.center):
            with self.subTest(make.__name__):
                host = make()
                host.teardown()
                host.teardown()
                self.assertTrue(host._closed)

    def test_toasts_teardown_once(self):
        log = []
        host = self.toasts()
        attach(host, log)
        host._active = {1, 2}
        host.teardown()
        host.teardown()
        self.assertEqual(calls(log, "ucm"), ["disconnect", "unregister_script_message_handler"])
        self.assertEqual(calls(log, "view"), ["try_close"])
        self.assertEqual(calls(log, "win"), ["destroy"])
        self.assertIsNone(host._window)
        self.assertIsNone(host._view)
        self.assertIsNone(host._ucm)
        self.assertEqual(host._active, set())

    def test_toasts_close_with_id_still_removes_one_toast(self):
        host = self.toasts()
        host._active = {1, 2}
        host.close(1)
        self.assertEqual(host._active, {2})
        self.assertFalse(host._closed)

    def test_toasts_close_requires_an_id(self):
        host = self.toasts()
        with self.assertRaises(TypeError):
            host.close()
        self.assertFalse(host._closed)

    def test_toasts_close_id_never_tears_down_host(self):
        log = []
        host = self.toasts()
        attach(host, log)
        host._active = {1, 2}
        host.close(1)
        host.close(2)
        host.close(99)
        self.assertFalse(host._closed)
        self.assertIsNotNone(host._window)
        self.assertIsNotNone(host._view)
        self.assertEqual(calls(log, "view"), [])
        self.assertEqual(calls(log, "win"), [])

    def test_toasts_never_rebuild_after_close(self):
        host = self.toasts()
        host.teardown()
        host._ensure_view()  # would need a display; closed, so it returns
        self.assertIsNone(host._window)

    def test_shade_teardown_tears_down_both_surfaces(self):
        log = []
        host = self.shade()
        attach(host, log, with_catcher=True)
        host.teardown()
        host.teardown()
        self.assertEqual(calls(log, "ucm"), ["disconnect", "unregister_script_message_handler"])
        self.assertEqual(calls(log, "view"), ["try_close"])
        self.assertEqual(calls(log, "win").count("destroy"), 1)
        self.assertEqual(calls(log, "catcher").count("destroy"), 1)
        self.assertIsNone(host._catcher)
        host.show()  # closed: a no-op, never builds
        self.assertIsNone(host._window)

    def test_center_teardown(self):
        log = []
        host = self.center()
        attach(host, log)
        host.teardown()
        host.teardown()
        self.assertEqual(calls(log, "ucm"), ["disconnect", "unregister_script_message_handler"])
        self.assertEqual(calls(log, "view"), ["try_close"])
        self.assertEqual(calls(log, "win").count("destroy"), 1)
        host.show()  # closed: a no-op
        self.assertIsNone(host._window)

    def test_null_stand_ins_expose_teardown(self):
        for obj in (nmain.NullDisplay(), nmain.NullShade(), nmain.NullCenter()):
            obj.teardown()
            obj.teardown()
        nmain.NullDisplay().close(3)


class ShutdownOrderTests(unittest.TestCase):
    def run_shutdown(self, fail=(), pump=True):
        log = []
        nmain.shutdown_daemon(
            monitor=Rec(log, "monitor"),
            runner=Rec(log, "runner", fail=fail),
            windows=[
                ("center", Rec(log, "center", fail=fail)),
                ("shade", Rec(log, "shade", fail=fail)),
                ("toasts", Rec(log, "toasts", fail=fail)),
            ],
            pump=(lambda: log.append(("pump", "run", ()))) if pump else None,
            release_bus=lambda: log.append(("bus", "release", ())),
            store=Rec(log, "store", fail=fail),
        )
        return [n for n, _, _ in log]

    def test_order(self):
        self.assertEqual(
            self.run_shutdown(),
            ["monitor", "runner", "center", "shade", "toasts", "pump", "bus", "store"],
        )

    def test_no_pump_without_display(self):
        self.assertNotIn("pump", self.run_shutdown(pump=False))

    def test_failing_teardowns_still_close_the_store(self):
        with self.assertLogs("notifyd", "ERROR") as cm:
            order = self.run_shutdown(fail=["shutdown", "teardown", "close"])
        self.assertEqual(order[-1], "store")
        self.assertIn("pump", order)
        self.assertEqual(len(cm.records), 5)  # runner, three teardowns, the store itself

    def test_failing_store_close_is_logged(self):
        with self.assertLogs("notifyd", "ERROR"):
            self.run_shutdown(fail=["close"])

    def test_pump_is_bounded(self):
        gtk = mock.MagicMock()
        gtk.events_pending.return_value = True
        with mock.patch.dict("sys.modules", {"gi.repository": mock.MagicMock(Gtk=gtk)}):
            n = nmain.pump_gtk(max_s=10, max_iter=5)
        self.assertEqual(n, 5)


if __name__ == "__main__":
    unittest.main()
