"""web/image.js run headless under node with a stand-in DOM (no browser, no display).

Skipped when node is not installed. Covers what the preview block builds, that its buttons stop
the click (so a toast is never dismissed or activated by them), the messages they post, the
Copied / Failed feedback and the silent removal of a preview whose file is gone. Nothing here
proves how it looks."""

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DRIVER = ROOT / "tests" / "data" / "image_driver.js"
SCRIPT = ROOT / "web" / "image.js"


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ImageJs(unittest.TestCase):
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
        self.assertGreater(out["checks"], 30)


class ImageJsSource(unittest.TestCase):
    def test_no_html_string_setters(self):
        text = SCRIPT.read_text(encoding="utf-8")
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(banned, text)

    def test_pages_load_it_before_their_own_script_and_wire_the_result(self):
        web = ROOT / "web"
        for page in ("toasts", "shade", "center"):
            html = (web / f"{page}.html").read_text(encoding="utf-8")
            self.assertLess(html.index("image.js"), html.index(f"{page}.js"), page)
            self.assertIn("imageResult: window.ncImage.deliver", (web / f"{page}.js").read_text())

    def test_the_toast_never_posts_a_click_through_from_a_button(self):
        # The toast's own click handler dismisses or acts; the buttons must stop the event.
        self.assertIn("ev.stopPropagation()", SCRIPT.read_text(encoding="utf-8"))
