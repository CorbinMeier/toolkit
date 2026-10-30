"""The Rules tab's reorder logic (web/rules.js) run headless under node, no browser, no display.

Skipped when node is not installed. Covers the pure key / drop / focus functions in
`window.ncRulesMove` and drives the real keydown and drop handlers of the list through a stand-in
DOM, asserting the `rules_move {index, to, expect}` bridge message each one sends.

Not provable here: the real drag gesture (dragstart / dragover geometry from a pointer),
the drop-indicator and dragging styles, and how the rows look. The fake DOM supplies row
rectangles by hand.
"""

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DRIVER = ROOT / "tests" / "data" / "rules_move_driver.js"
SCRIPT = ROOT / "web" / "rules.js"


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class RulesMoveJs(unittest.TestCase):
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
        self.assertGreater(out["checks"], 80)
