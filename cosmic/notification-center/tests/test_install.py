"""Runs the real scripts/install.sh and scripts/uninstall.sh against a temp HOME.

Never touches the real HOME, never passes --activate, and writes the change log to a temp file.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from notifyd.rules import RuleSet

ROOT = Path(__file__).resolve().parent.parent
MARKER = "# notifyd-shim: cosmic-notification-center"


class ScriptCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.home.mkdir()
        self.log = Path(tmp.name) / "changes.md"
        self.shim = self.home / ".local/bin/cosmic-notifications"
        self.conf = self.home / ".config/net.corbinmeier.notifications"
        self.data = self.home / ".local/share/net.corbinmeier.notifications"

    def run_script(self, name, *args):
        env = {
            **os.environ,
            "HOME": str(self.home),
            "NOTIFYD_CHANGES_LOG": str(self.log),
            "NOTIFYD_INSTALL_SKIP_CHECK": "1",
            "NOTIFYD_INSTALL_SKIP_SESSION": "1",
        }
        return subprocess.run(
            [str(ROOT / "scripts" / name), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def install(self):
        r = self.run_script("install.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def foreign_shim(self):
        self.shim.parent.mkdir(parents=True)
        self.shim.write_text("#!/bin/sh\necho not ours\n")
        return self.shim.read_text()


class InstallTest(ScriptCase):
    def test_fresh_install(self):
        self.install()
        text = self.shim.read_text()
        self.assertTrue(any(ln.startswith(MARKER) for ln in text.splitlines()))
        self.assertNotIn("@REPO@", text)
        self.assertIn(str(ROOT), text)
        self.assertTrue(os.access(self.shim, os.X_OK))
        self.assertTrue((self.conf / "config.toml").is_file())
        self.assertTrue((self.conf / "style.css").is_file())
        rules = self.conf / "rules.toml"
        RuleSet.load(rules)
        lines = rules.read_text().splitlines()
        self.assertIn('# sound = "~/Music/sounds/example.mp3"', lines)
        self.assertNotIn('sound = "~/Music/sounds/example.mp3"', lines)
        self.assertTrue(self.data.is_dir())
        self.assertIn("create shim", self.log.read_text())

    def test_reinstall_keeps_edits(self):
        self.install()
        edits = {}
        for name in ("rules.toml", "style.css", "config.toml"):
            p = self.conf / name
            edits[name] = p.read_text() + "\n# my edit\n"
            p.write_text(edits[name])
        self.install()
        for name, content in edits.items():
            self.assertEqual((self.conf / name).read_text(), content, name)
        self.assertIn(MARKER, self.shim.read_text())

    def test_refuses_foreign_shim(self):
        original = self.foreign_shim()
        r = self.run_script("install.sh")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not our shim", r.stderr)
        self.assertEqual(self.shim.read_text(), original)

    def test_activate_refused_under_test_overrides(self):
        r = self.run_script("install.sh", "--activate")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.shim.exists())


class UninstallTest(ScriptCase):
    def test_removes_shim_keeps_config(self):
        self.install()
        r = self.run_script("uninstall.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.shim.exists())
        self.assertTrue((self.conf / "config.toml").is_file())
        self.assertTrue(self.data.is_dir())
        self.assertIn("remove shim", self.log.read_text())

    def test_purge_removes_config(self):
        self.install()
        r = self.run_script("uninstall.sh", "--purge")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.shim.exists())
        self.assertFalse(self.conf.exists())
        self.assertFalse(self.data.exists())

    def test_refuses_foreign_file(self):
        original = self.foreign_shim()
        r = self.run_script("uninstall.sh")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.shim.read_text(), original)


if __name__ == "__main__":
    unittest.main()
