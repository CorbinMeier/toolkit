"""web/reply.js run headless under node with a stand-in DOM (no browser, no display).

Skipped when node is not installed. Covers what the reply block shows (answered, refused,
disabled, confirm), the keyboard handling (Enter / Shift+Enter), the counter, and the messages
it posts. Nothing here proves how it looks.
"""

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DRIVER = ROOT / "tests" / "data" / "reply_driver.js"
SCRIPT = ROOT / "web" / "reply.js"


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ReplyJs(unittest.TestCase):
    def test_driver_checks_all_pass(self):
        env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
        env["TZ"] = "UTC"
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
        self.assertGreater(out["checks"], 90)


class ReplyJsSource(unittest.TestCase):
    def test_no_html_string_setters_and_pages_load_it(self):
        text = SCRIPT.read_text(encoding="utf-8")
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(banned, text)
        web = ROOT / "web"
        for page in ("toasts.html", "shade.html", "center.html"):
            html = (web / page).read_text(encoding="utf-8")
            self.assertIn('<script src="reply.js"></script>', html, page)
            self.assertLess(html.index("reply.js"), html.index(page.split(".")[0] + ".js"), page)
            self.assertIn("script-src 'self'", html)
            self.assertNotIn("unsafe-eval", html)
