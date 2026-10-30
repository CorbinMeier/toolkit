"""Open-latency trace and prewarm: the pure parts, no display, no WebKit."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from notifyd.center import CenterBridge, CenterWindow
from notifyd.config import Config, load_config
from notifyd.opentrace import PAGE_PHASES, OpenTrace
from notifyd.shade import DEFAULT_ROOM, ShadeWindow
from notifyd.store import Store
from tests.test_shade import Harness as ShadeHarness

WEB = Path(__file__).resolve().parent.parent / "web"
noop = lambda *a: None  # noqa: E731


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def advance(self, ms):
        self.t += ms / 1000


class Trace(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.lines: list[str] = []
        self.timers: list[tuple[int, object]] = []

    def trace(self, required, settle_ms=0):
        return OpenTrace(
            "center",
            required,
            settle_ms=settle_ms,
            clock=self.clock,
            emit=self.lines.append,
            schedule=lambda ms, fn: self.timers.append((ms, fn)),
        )

    def test_one_line_when_every_required_phase_arrived(self):
        t = self.trace(("painted",))
        t.begin(built=False)
        self.clock.advance(3)
        t.mark("present")
        self.assertEqual(self.lines, [])
        self.clock.advance(209)
        t.mark("painted")
        self.assertEqual(self.lines, ["center open: built=no present=3ms painted=212ms"])
        t.mark("painted")  # a late repeat after the flush is dropped
        self.assertEqual(len(self.lines), 1)

    def test_marks_count_once_unless_last(self):
        t = self.trace(("painted",))
        t.begin(built=True)
        t.mark("present")
        self.clock.advance(5)
        t.mark("present")
        t.mark("tail")
        self.clock.advance(5)
        t.mark("painted")
        self.assertEqual(self.lines, ["center open: built=yes present=0ms tail=5ms painted=10ms"])

    def test_shade_waits_for_rows_and_settles_for_a_resize(self):
        t = self.trace(("rows", "painted"), settle_ms=400)
        t.begin(built=False)
        t.mark("resized", last=True, after="rows")  # before the rows: not a resize after them
        self.clock.advance(2)
        t.mark("present")
        self.clock.advance(18)
        t.mark("rows")
        self.clock.advance(100)
        t.mark("painted")
        self.assertEqual(self.lines, [])
        self.assertEqual([ms for ms, _ in self.timers], [400])
        self.clock.advance(50)
        t.mark("resized", last=True, after="rows")
        self.clock.advance(50)
        t.mark("resized", last=True, after="rows")
        self.timers[0][1]()
        self.assertEqual(
            self.lines,
            ["center open: built=no present=2ms rows=20ms painted=120ms resized=220ms"],
        )

    def test_a_new_open_flushes_the_unfinished_one_with_dashes(self):
        t = self.trace(("rows", "painted"))
        t.begin(built=True)
        t.mark("present")
        t.mark("rows")
        t.begin(built=False)
        self.assertEqual(self.lines, ["center open: built=yes present=0ms rows=0ms painted=-"])

    def test_a_stale_settle_timer_does_not_cut_the_next_open_short(self):
        t = self.trace(("painted",), settle_ms=400)
        t.begin(built=False)
        t.mark("painted")
        t.begin(built=False)  # flushes the first
        t.mark("present")
        self.timers[0][1]()  # the first open's timer fires during the second
        self.assertEqual(len(self.lines), 1)
        t.mark("painted")
        self.assertEqual(len(self.lines), 1)  # the second now waits on its own timer
        self.timers[1][1]()
        self.assertEqual(len(self.lines), 2)

    def test_marks_outside_an_open_are_ignored(self):
        t = self.trace(("painted",))
        t.mark("painted")
        t.mark("present")
        self.assertEqual(self.lines, [])

    def test_the_line_is_one_line_of_numbers(self):
        t = self.trace(("painted",))
        t.begin(built=True)
        t.mark("present")
        t.mark("painted")
        self.assertRegex(self.lines[0], r"^center open: built=(yes|no)( [a-z]+=(\d+ms|-))+$")


class PageMessages(unittest.TestCase):
    def test_shade_bridge_takes_known_phases_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = ShadeHarness(tmp)
            got = []
            h.bridge.on_perf = got.append
            for msg in (
                {"type": "perf", "phase": "painted"},
                {"type": "perf", "phase": "rows"},
                {"type": "perf", "phase": "present"},
                {"type": "perf", "phase": ["painted"]},
                {"type": "perf", "phase": 5},
                {"type": "perf"},
                {"type": "perf", "phase": "painted", "extra": "x" * 10000},
            ):
                h.bridge.handle(msg)
            self.assertEqual(got, ["painted", "rows", "painted"])
            self.assertEqual(h.js, [])  # never answered, never touches the store or the page

    def test_shade_bridge_without_a_listener_drops_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = ShadeHarness(tmp)
            h.bridge.handle({"type": "perf", "phase": "painted"})
            self.assertEqual(h.calls, [])

    def test_center_bridge_takes_known_phases_only(self):
        got = []
        b = CenterBridge(
            Store(":memory:"),
            on_invoke_action=lambda i, k: True,
            on_dismiss=noop,
            on_mark_read=noop,
            on_delete=noop,
            on_clear=noop,
            run_js=noop,
            copy_text=noop,
            open_link=noop,
            on_perf=got.append,
        )
        for msg in ({"type": "perf", "phase": "painted"}, {"type": "perf", "phase": "bogus"}):
            b.handle(msg)
        self.assertEqual(got, ["painted"])

    def test_pages_load_paint_js_before_their_own_script(self):
        js = (WEB / "paint.js").read_text()
        self.assertIn('type: "perf"', js)
        self.assertIn("requestAnimationFrame", js)
        self.assertIn("visibilitychange", js)
        for banned in ("innerHTML", "eval(", "document.write"):
            self.assertNotIn(banned, js)
        for page in ("shade", "center"):
            html = (WEB / f"{page}.html").read_text()
            self.assertIn('<script src="paint.js"></script>', html)
            self.assertLess(html.index("paint.js"), html.index(f"{page}.js"))
        self.assertIn('phase: "rows"', (WEB / "shade.js").read_text())
        self.assertEqual(set(PAGE_PHASES), {"painted", "rows"})


def shade_window(tmp):
    return ShadeWindow(
        Store(":memory:"),
        WEB,
        on_dismiss=noop,
        on_mark_read=noop,
        on_open_details=noop,
        on_open_center=noop,
        on_invoke_action=lambda i, k: True,
        get_dnd=lambda: False,
        on_set_dnd=noop,
        get_app_prefs=lambda app: {},
        on_set_app_prefs=noop,
        state_path=Path(tmp) / "shade.json",
    )


def center_window(tmp):
    return CenterWindow(
        Store(":memory:"),
        WEB,
        on_invoke_action=lambda i, k: True,
        on_dismiss=noop,
        on_mark_read=noop,
        on_delete=noop,
        on_clear=noop,
        state_path=Path(tmp) / "center.json",
    )


class NoDisplay(unittest.TestCase):
    """Headless: building a view fails (no display); prewarm must swallow that, never raise."""

    def test_prewarm_logs_and_survives_without_a_display(self):
        with tempfile.TemporaryDirectory() as tmp:
            for w in (shade_window(tmp), center_window(tmp)):
                with self.assertLogs("notifyd", "ERROR"):
                    w.prewarm()
                self.assertIsNone(w._window)

    def test_prewarm_after_teardown_builds_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = shade_window(tmp)
            w.teardown()
            with mock.patch.object(w, "_ensure_view") as build:
                w.prewarm()
            build.assert_not_called()

    def test_prewarm_maps_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = shade_window(tmp)
            with (
                mock.patch.object(w, "_ensure_view") as build,
                mock.patch.object(w, "_map_shade") as shown,
                mock.patch.object(w, "_map_catcher") as catcher,
            ):
                w.prewarm()
            build.assert_called_once()
            shown.assert_not_called()
            catcher.assert_not_called()
            self.assertFalse(w.is_visible())

    def test_show_starts_a_trace_and_an_open_that_fails_logs_a_line_later(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = shade_window(tmp)
            lines = []
            w.trace._emit = lines.append
            with self.assertLogs("notifyd", "ERROR"):
                w.show()
            w.trace.flush()
            self.assertEqual(lines, ["shade open: built=yes present=- rows=- painted=-"])

    def test_size_reports_from_a_view_that_was_never_mapped_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = shade_window(tmp)
            w._window, w._view = mock.Mock(), mock.Mock()
            size = {"width": 30, "height": 4, "room": DEFAULT_ROOM, "rect": [1, 2, 3, 4]}
            w._apply_size(size)
            self.assertEqual(w._size, (0, 0))
            w._mapped = True
            w._apply_size(size)
            self.assertEqual(w._size, (30, 4))

    def test_resize_after_the_rows_lands_in_the_open_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = shade_window(tmp)
            w._window, w._view, w._mapped = mock.Mock(), mock.Mock(), True
            lines = []
            w.trace._emit = lines.append
            w.trace.begin(built=False)
            for phase in ("present", "rows", "painted"):
                w.trace.mark(phase)
            w._apply_size({"width": 30, "height": 4, "room": DEFAULT_ROOM, "rect": [1, 2, 3, 4]})
            w.trace.flush()
            self.assertRegex(lines[0], r"rows=\d+ms painted=\d+ms resized=\d+ms$")


class PrewarmConfig(unittest.TestCase):
    def test_on_by_default_and_a_restart_setting(self):
        from notifyd.config import BY_KEY

        self.assertTrue(Config().prewarm)
        self.assertTrue(BY_KEY[("windows", "prewarm")].restart)

    def test_config_toml_can_turn_it_off_and_a_bad_value_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.toml"
            p.write_text("[windows]\nprewarm = false\n")
            self.assertFalse(load_config(p).prewarm)
            p.write_text('[windows]\nprewarm = "yes"\n')
            with self.assertLogs("notifyd", "ERROR"):
                self.assertTrue(load_config(p).prewarm)


if __name__ == "__main__":
    unittest.main()
