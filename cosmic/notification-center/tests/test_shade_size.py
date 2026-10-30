"""The shade asks the page for a fresh size on every open.

A prewarmed page reports its size while unmapped; `ShadeWindow._apply_size` drops that report
(a hidden layout is untrusted) and the page never repeats an unchanged size, so the surface stayed
at its 100 px first-map height. Python now calls `window.nc.measure()` after every map, and the
page's reporter re-sends on it. The JS half runs under node with a stand-in DOM (skipped when node
is missing); the Python half needs no display."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from notifyd.shade import DEFAULT_HEIGHT, ShadeWindow
from notifyd.store import Store

ROOT = Path(__file__).resolve().parent.parent
DRIVER = ROOT / "tests" / "data" / "shade_size_driver.js"
SCRIPT = ROOT / "web" / "shade.js"


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ShadeSizeJs(unittest.TestCase):
    def test_driver_checks_all_pass(self):
        env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
        proc = subprocess.run(
            ["node", str(DRIVER), str(SCRIPT)],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(out["failures"], [])
        self.assertGreater(out["checks"], 8)


class ShadeSizeWindow(unittest.TestCase):
    def make(self, tmp):
        noop = lambda *a: None  # noqa: E731
        w = ShadeWindow(
            Store(":memory:"),
            ROOT / "web",
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
        w._ensure_view = lambda: None
        w.surfaces.show = lambda: True
        w.scripts = []
        w._eval = w.scripts.append
        return w

    def test_every_show_measures_after_the_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = self.make(tmp)
            w.show()
            w.show()
            self.assertEqual(w.scripts.count("window.nc.measure()"), 2)
            self.assertGreater(
                w.scripts.index("window.nc.measure()"), w.scripts.index("window.ncPaint.arm()")
            )

    def test_no_measure_when_the_surface_did_not_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = self.make(tmp)
            w.surfaces.show = lambda: False
            w.show()
            self.assertNotIn("window.nc.measure()", w.scripts)

    def test_first_map_uses_a_sensible_height_until_the_page_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = self.make(tmp)
            sized = []
            w._resize = lambda a, b: sized.append((a, b))
            w._window = type("Win", (), {"show_all": lambda s: None, "present": lambda s: None})()
            w._map_shade()
            self.assertEqual(sized[-1][1], DEFAULT_HEIGHT)
            w._size = (400, 555)
            w._map_shade()
            self.assertEqual(sized[-1][1], 555)


if __name__ == "__main__":
    unittest.main()
