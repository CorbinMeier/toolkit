"""Icon resolution chain (notifyd.icons), the per-app icon pref, the avatar assets."""

import tempfile
import unittest
from pathlib import Path

from notifyd import icons
from notifyd.app_settings import AppPrefs, AppSettings
from notifyd.icons import DesktopIndex
from notifyd.model import Notification
from notifyd.toasts import resolve_icon

WEB = Path(__file__).resolve().parent.parent / "web"


class Fx:
    """Temp XDG data dir with fake .desktop files, a fake icon theme dir and a clock."""

    def __init__(self, tmp):
        self.root = Path(tmp)
        self.apps = self.root / "share" / "applications"
        self.apps.mkdir(parents=True)
        self.theme = self.root / "theme"
        self.theme.mkdir()
        self.now = 0.0
        self.index = DesktopIndex(lambda: [self.apps], lambda: self.now, 60.0)

    def icon(self, name):
        p = self.theme / f"{name}.svg"
        p.write_bytes(b"<svg/>")
        return str(p)

    def lookup(self, name):
        p = self.theme / f"{name}.svg"
        return str(p) if p.is_file() else None

    def desktop(self, file, name="", icon="", wm="", sub=""):
        p = self.apps / sub / f"{file}.desktop"
        p.parent.mkdir(parents=True, exist_ok=True)
        lines = ["[Desktop Entry]", "Type=Application", f"Name={name}", f"Icon={icon}"]
        if wm:
            lines.append(f"StartupWMClass={wm}")
        lines += ["Name[de]=Falsch", "[Desktop Action x]", "Name=Other", "Icon=other"]
        p.write_text("\n".join(lines) + "\n")

    def resolve(self, app="", app_icon="", hints=None):
        return icons.resolve(app, app_icon, hints or {}, self.lookup, self.index)


class Chain(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.fx = Fx(self._t.name)

    def test_nothing_is_none(self):
        self.assertIsNone(self.fx.resolve("Example App"))
        self.assertEqual(resolve_icon("", {}, self.fx.lookup), "")

    def test_order(self):
        fx = self.fx
        img = fx.root / "hint.png"
        img.write_bytes(b"x")
        fx.icon("themed")
        fx.icon("entry-icon")
        fx.icon("by-name")
        fx.desktop("entry", name="Entry", icon="entry-icon")
        fx.desktop("example", name="Example App", icon="by-name")
        hints = {"image-path": str(img), "desktop-entry": "entry"}
        self.assertTrue(fx.resolve("Example App", "themed", hints).endswith("hint.png"))
        self.assertTrue(
            fx.resolve("Example App", "themed", {"desktop-entry": "entry"}).endswith(
                "themed.svg"
            )
        )
        self.assertTrue(
            fx.resolve("Example App", "", {"desktop-entry": "entry.desktop"}).endswith(
                "entry-icon.svg"
            )
        )
        self.assertTrue(fx.resolve("Example App").endswith("by-name.svg"))
        # a desktop-entry hint that finds nothing falls through to the app-name match
        self.assertTrue(
            fx.resolve("Example App", "", {"desktop-entry": "zz"}).endswith("by-name.svg")
        )
        # an app_icon that does not exist falls through too
        self.assertTrue(fx.resolve("Example App", "missing-name").endswith("by-name.svg"))

    def test_app_icon_forms(self):
        fx = self.fx
        p = fx.icon("a b")
        self.assertEqual(fx.resolve(app_icon=p), "file://" + p.replace(" ", "%20"))
        self.assertEqual(
            fx.resolve(app_icon="file://" + p.replace(" ", "%20")), fx.resolve(app_icon=p)
        )
        self.assertEqual(fx.resolve(app_icon="a b"), fx.resolve(app_icon=p))
        self.assertIsNone(fx.resolve(app_icon="http://x/y.png"))

    def test_app_name_matches(self):
        fx = self.fx
        fx.icon("n")
        fx.icon("w")
        fx.icon("f")
        fx.desktop("org.x.Named", name="My App", icon="n")
        fx.desktop("wmthing", name="Zed", icon="w", wm="Slacky")
        fx.desktop("Files", name="Other", icon="f", sub="sub")
        self.assertTrue(fx.resolve("my app").endswith("n.svg"))
        self.assertTrue(fx.resolve("SLACKY").endswith("w.svg"))
        self.assertTrue(fx.resolve("sub-files").endswith("f.svg"))
        self.assertTrue(fx.resolve("org.x.named").endswith("n.svg"))
        self.assertIsNone(fx.resolve("My"))
        # only [Desktop Entry] keys count: the action group's Icon=other is ignored
        self.assertEqual(fx.index.by_id("wmthing").icon, "w")

    def test_icon_path_in_desktop_file(self):
        fx = self.fx
        p = fx.icon("abs")
        fx.desktop("abs", name="Abs", icon=p)
        self.assertTrue(fx.resolve("Abs").endswith("abs.svg"))

    def test_cache_refreshes_at_most_every_60s(self):
        fx = self.fx
        fx.icon("late")
        self.assertIsNone(fx.resolve("Late"))
        fx.desktop("late", name="Late", icon="late")
        fx.now = 59.0
        self.assertIsNone(fx.resolve("Late"))  # still the old scan
        fx.now = 60.0
        self.assertTrue(fx.resolve("Late").endswith("late.svg"))

    def test_first_dir_wins(self):
        fx = self.fx
        other = fx.root / "share2"
        other.mkdir()
        fx.icon("one")
        fx.icon("two")
        fx.desktop("dup", name="Dup", icon="one")
        (other / "dup.desktop").write_text("[Desktop Entry]\nName=Dup\nIcon=two\n")
        idx = DesktopIndex(lambda: [fx.apps, other], lambda: 0.0)
        self.assertEqual(idx.by_id("dup").icon, "one")

    def test_xdg_dirs_from_env(self):
        d = icons.xdg_data_dirs({"XDG_DATA_HOME": "/h", "XDG_DATA_DIRS": "/a:/b", "HOME": "/x"})
        self.assertEqual(
            [str(p) for p in d], ["/h/applications", "/a/applications", "/b/applications"]
        )

    def test_tilde_and_error(self):
        fx = self.fx
        self.assertIn("not found", icons.icon_error("/no/such.png", fx.lookup))
        self.assertIn("no theme icon", icons.icon_error("nope", fx.lookup))
        fx.icon("ok")
        self.assertIsNone(icons.icon_error("ok", fx.lookup))
        self.assertIsNone(icons.icon_error(fx.icon("ok"), fx.lookup))


class PerAppIcon(unittest.TestCase):
    def test_pref_round_trip_and_default_file(self):
        p = AppPrefs.from_dict({"icon": "  /x/y.png "})
        self.assertEqual(p.icon, "/x/y.png")
        self.assertIsNone(AppPrefs.from_dict({"icon": "  "}).icon)
        with self.assertRaises(ValueError):
            AppPrefs.from_dict({"icon": 3})
        s = AppSettings()
        s.set("Example", p)
        again = AppSettings.loads(s.dumps())
        self.assertEqual(again.get("example").icon, "/x/y.png")
        # files written before the key existed still load
        old = AppSettings.loads('[[app]]\nname = "A"\nmuted = true\n')
        self.assertIsNone(old.get("A").icon)

    def test_outranks_chain(self):
        with tempfile.TemporaryDirectory() as d:
            fx = Fx(d)
            mine = fx.icon("mine")
            fx.icon("hint")
            fx.desktop("example", name="Example App", icon="hint")
            s = AppSettings()
            s.set("Example App", AppPrefs(icon="mine"))
            n = Notification.from_notify(
                1, "Example App", 0, "", "s", "", [], {"image-path": fx.icon("hint")}, -1, 1.0
            )
            out = s.to_ruleset().apply(n)
            self.assertEqual(out.app_icon, "mine")
            self.assertEqual(
                icons.resolve("Example App", out.app_icon, out.hints, fx.lookup, fx.index),
                "file://" + mine,
            )


class Assets(unittest.TestCase):
    def test_pages_load_avatar_script_and_css_documents_hook(self):
        for page in ("toasts", "shade", "center"):
            html = (WEB / f"{page}.html").read_text()
            self.assertLess(html.index("avatar.js"), html.index(f"{page}.js"))
        for css in ("default.css", "shade.css", "center.css"):
            head = (WEB / css).read_text()[:3500]
            self.assertIn("nc-avatar", head, css)
        js = (WEB / "avatar.js").read_text()
        self.assertIn('setProperty("--nc-avatar-hue"', js)
        self.assertNotIn("cssText", js)
        self.assertNotIn(".style =", js)


if __name__ == "__main__":
    unittest.main()
