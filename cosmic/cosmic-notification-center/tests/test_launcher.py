"""Runs scripts/install-launcher.sh and uninstall-launcher.sh against a temp HOME.

Never touches the real HOME; the change log goes to a temp file.
"""

import configparser
import os
import re
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_ID = "net.corbinmeier.NotificationCenter"


class LauncherBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.home.mkdir()
        self.log = Path(tmp.name) / "changes.md"
        share = self.home / ".local/share"
        self.desktop = share / f"applications/{APP_ID}.desktop"
        self.icon = share / f"icons/hicolor/scalable/apps/{APP_ID}.svg"
        self.symbolic = share / f"icons/hicolor/symbolic/apps/{APP_ID}-symbolic.svg"

    def run_script(self, name, *args):
        env = {**os.environ, "HOME": str(self.home), "NOTIFYD_CHANGES_LOG": str(self.log)}
        env.pop("XDG_DATA_HOME", None)
        return subprocess.run(
            [str(ROOT / "scripts" / name), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def log_rows(self):
        return self.log.read_text().count("\n") if self.log.exists() else 0


class LauncherCase(LauncherBase):
    def test_install_lands_files(self):
        r = self.run_script("install-launcher.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        for f in (self.desktop, self.icon, self.symbolic):
            self.assertTrue(f.is_file(), f)
        cp = configparser.ConfigParser(interpolation=None, delimiters=("=",))
        cp.optionxform = str
        cp.read(self.desktop)
        entry = cp["Desktop Entry"]
        self.assertEqual(entry["Exec"], f"{ROOT}/bin/notifyd --show-center")
        self.assertEqual(entry["Icon"], APP_ID)
        self.assertEqual(entry["StartupWMClass"], APP_ID)
        self.assertEqual(entry["Name"], "Notification Center")
        self.assertNotIn("@REPO@", self.desktop.read_text())
        self.assertEqual(self.log_rows(), 3)

    def test_svgs_parse(self):
        for f in (ROOT / "resources/icons").glob("*.svg"):
            root = ET.parse(f).getroot()
            self.assertTrue(root.tag.endswith("svg"), f)
            text = f.read_text()
            self.assertNotIn("<image", text)
            self.assertNotIn('href="http', text)

    def test_reinstall_is_idempotent(self):
        self.run_script("install-launcher.sh")
        before = self.desktop.read_text()
        rows = self.log_rows()
        r = self.run_script("install-launcher.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.desktop.read_text(), before)
        self.assertEqual(self.log_rows(), rows)

    def test_foreign_file_refused(self):
        self.desktop.parent.mkdir(parents=True)
        self.desktop.write_text("[Desktop Entry]\nName=Mine\n")
        r = self.run_script("install-launcher.sh")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refusing", r.stderr)
        self.assertEqual(self.desktop.read_text(), "[Desktop Entry]\nName=Mine\n")
        self.assertFalse(self.icon.exists())  # nothing written before the refusal
        self.assertFalse(self.log.exists())

    def test_foreign_icon_refused(self):
        self.icon.parent.mkdir(parents=True)
        self.icon.write_text("<svg/>")
        r = self.run_script("install-launcher.sh")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.desktop.exists())

    def test_uninstall_removes_only_ours(self):
        self.run_script("install-launcher.sh")
        other = self.icon.parent / "other.svg"
        other.write_text("<svg/>")
        r = self.run_script("uninstall-launcher.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        for f in (self.desktop, self.icon, self.symbolic):
            self.assertFalse(f.exists(), f)
        self.assertTrue(other.exists())

    def test_uninstall_leaves_foreign(self):
        self.desktop.parent.mkdir(parents=True)
        self.desktop.write_text("[Desktop Entry]\nName=Mine\n")
        r = self.run_script("uninstall-launcher.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.desktop.exists())
        self.assertIn("leaving", r.stderr)


class AppletIconCase(LauncherBase):
    """install-applet.sh --icons-only / uninstall-applet.sh against a temp HOME (no cargo)."""

    def setUp(self):
        super().setUp()
        base = self.home / ".local/share/icons/hicolor/symbolic/apps"
        self.new = base / f"{APP_ID}-new-symbolic.svg"
        self.dnd = base / f"{APP_ID}-dnd-symbolic.svg"
        self.applet_desktop = (
            self.home
            / ".local/share/applications/net.corbinmeier.CosmicAppletNotifications.desktop"
        )

    def icons(self):
        return (self.icon, self.symbolic, self.new, self.dnd)

    def install(self):
        r = self.run_script("install-applet.sh", "--icons-only")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_installs_all_four_and_names_exist_in_repo(self):
        self.install()
        for f in self.icons():
            self.assertTrue(f.is_file(), f)
            self.assertIn("notifyd-launcher: cosmic-notification-center", f.read_text())
        self.assertEqual(self.log_rows(), 4)

    def test_symbolic_icons_use_one_plain_fill(self):
        for name in ("-symbolic", "-new-symbolic", "-dnd-symbolic"):
            text = (ROOT / f"resources/icons/{APP_ID}{name}.svg").read_text()
            self.assertIn('viewBox="0 0 16 16"', text)
            self.assertIn('fill="#bebebe"', text)
            # only #bebebe paints; #fff / #000 appear solely as mask luminance
            colours = set(re.findall(r'(?:fill|stroke)="(#[0-9a-fA-F]+)"', text))
            self.assertLessEqual(colours, {"#bebebe", "#fff", "#000"}, name)

    def test_reinstall_skips_unchanged(self):
        self.install()
        rows = self.log_rows()
        self.install()
        self.assertEqual(self.log_rows(), rows)

    def test_changed_icon_is_rewritten_with_one_row(self):
        self.install()
        self.new.write_text(self.new.read_text() + "<!-- stale -->\n")
        rows = self.log_rows()
        self.install()
        self.assertNotIn("stale", self.new.read_text())
        self.assertEqual(self.log_rows(), rows + 1)

    def test_foreign_icon_refused_before_any_write(self):
        self.dnd.parent.mkdir(parents=True)
        self.dnd.write_text("<svg/>")
        r = self.run_script("install-applet.sh", "--icons-only")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refusing", r.stderr)
        self.assertEqual(self.dnd.read_text(), "<svg/>")
        self.assertFalse(self.icon.exists() or self.symbolic.exists() or self.new.exists())
        self.assertFalse(self.log.exists())

    def test_uninstall_removes_all_without_launcher(self):
        self.install()
        r = self.run_script("uninstall-applet.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        for f in self.icons():
            self.assertFalse(f.exists(), f)

    def test_uninstall_keeps_shared_icons_while_launcher_installed(self):
        self.run_script("install-launcher.sh")
        self.install()
        r = self.run_script("uninstall-applet.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.icon.exists() and self.symbolic.exists() and self.desktop.exists())
        self.assertFalse(self.new.exists() or self.dnd.exists())
        # launcher gone afterwards: nothing left behind
        self.run_script("uninstall-launcher.sh")
        for f in self.icons():
            self.assertFalse(f.exists(), f)

    def test_launcher_uninstall_keeps_shared_icons_while_applet_installed(self):
        self.install()
        self.applet_desktop.parent.mkdir(parents=True, exist_ok=True)
        self.applet_desktop.write_text("[Desktop Entry]\n")
        self.run_script("install-launcher.sh")
        self.run_script("uninstall-launcher.sh")
        self.assertTrue(self.icon.exists() and self.symbolic.exists())
        self.assertFalse(self.desktop.exists())

    def test_uninstall_leaves_foreign_icon(self):
        self.dnd.parent.mkdir(parents=True)
        self.dnd.write_text("<svg/>")
        r = self.run_script("uninstall-applet.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.dnd.exists())
        self.assertIn("leaving", r.stderr)

    def test_applet_desktop_names_the_symbolic_icon(self):
        text = (
            ROOT / "applet/resources/net.corbinmeier.CosmicAppletNotifications.desktop"
        ).read_text()
        self.assertIn(f"Icon={APP_ID}-symbolic\n", text)


if __name__ == "__main__":
    unittest.main()
