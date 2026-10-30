"""Per-app settings: prefs validation, the TOML file, the rule mapping, and the Runtime wiring."""

import os
import tempfile
import tomllib
import unittest
from pathlib import Path

from notifyd.app_settings import AppPrefs, AppSettings, AppSettingsError, toml_string
from notifyd.model import Notification, State, Urgency
from notifyd.rules import RuleSet
from tests import test_phase2
from tests.test_integration import pump_briefly, pump_until

NASTY_NAMES = [
    'Quote"App',
    "Back\\slash",
    "Reg.*ex(a|b)[x]+$",
    "line\nbreak\ttab",
    "ctl\x01\x7fchar",
    "Ünïcode ☃ 日本語",
    "[[app]]\nname = 'x'",
    "# not a comment",
    "~tilde",
]
NASTY_PATHS = ['/tmp/a "b".ogg', "C:\\snd\\x.wav", "/tmp/sp ace/é.oga", "/tmp/\ttab\\n.ogg"]


def notif(app="X", **kw):
    base = dict(
        id=1,
        app_name=app,
        replaces_id=0,
        app_icon="",
        summary="s",
        body="b",
        actions=[],
        hints={},
        expire_timeout=-1,
        urgency=Urgency.NORMAL,
        category="",
        desktop_entry="",
        received_at=0.0,
        timeout_ms=4000,
    )
    base.update(kw)
    return Notification(**base)


class PrefsTests(unittest.TestCase):
    def test_default_and_dict_round_trip(self):
        p = AppPrefs()
        self.assertTrue(p.is_default())
        self.assertEqual(AppPrefs.from_dict(p.to_dict()), p)
        full = AppPrefs(True, True, "bell", 40, True, True, True)
        self.assertFalse(full.is_default())
        self.assertEqual(AppPrefs.from_dict(full.to_dict()), full)

    def test_validation(self):
        bad = [
            {"muted": 1},
            {"silent": "yes"},
            {"volume": 101},
            {"volume": -1},
            {"volume": True},
            {"volume": 1.5},
            {"volume": "5"},
            {"sound": 3},
            {"nope": True},
        ]
        for d in bad:
            with self.assertRaises(ValueError, msg=str(d)):
                AppPrefs.from_dict(d)
        with self.assertRaises(ValueError):
            AppPrefs.from_dict("x")

    def test_blank_sound_is_unset(self):
        self.assertIsNone(AppPrefs.from_dict({"sound": "  "}).sound)
        self.assertTrue(AppPrefs.from_dict({"sound": "", "volume": None}).is_default())

    def test_set_default_removes_entry_case_insensitively(self):
        s = AppSettings()
        s.set("Slack", AppPrefs(muted=True))
        self.assertEqual(s.apps(), ["Slack"])
        self.assertTrue(s.get("sLaCk").muted)
        s.set("SLACK", AppPrefs(sticky=True))  # same app; keeps the first spelling
        self.assertEqual(s.apps(), ["Slack"])
        self.assertFalse(s.get("slack").muted)
        s.set("slack", AppPrefs())
        self.assertEqual(s.apps(), [])
        self.assertTrue(s.get("slack").is_default())
        with self.assertRaises(ValueError):
            s.set("  ", AppPrefs(muted=True))


class FileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "app-settings.toml"

    def test_missing_is_empty(self):
        self.assertEqual(AppSettings.load(self.path).apps(), [])

    def test_header_and_only_non_default_keys(self):
        s = AppSettings()
        s.set("A", AppPrefs(muted=True, volume=0))
        s.save(self.path)
        text = self.path.read_text()
        self.assertTrue(text.startswith("# Managed by the notification center UI"))
        self.assertIn("comments are not preserved", text)
        self.assertIn("muted = true", text)
        self.assertIn("volume = 0", text)
        for absent in ("silent", "sticky", "blocked", "dnd_bypass", "sound"):
            self.assertNotIn(absent, text)

    def test_round_trip_nasty_names_and_paths(self):
        s = AppSettings()
        for i, name in enumerate(NASTY_NAMES):
            s.set(name, AppPrefs(sound=NASTY_PATHS[i % len(NASTY_PATHS)], volume=i, sticky=True))
        s.save(self.path)
        back = AppSettings.load(self.path)
        self.assertEqual(back.apps(), NASTY_NAMES)
        for name in NASTY_NAMES:
            self.assertEqual(back.get(name), s.get(name))
        self.assertEqual(back.dumps(), s.dumps())

    def test_toml_string_escapes(self):
        for raw in NASTY_NAMES + NASTY_PATHS + ["\x00\x1f\x7f", "\b\f\r"]:
            self.assertEqual(tomllib.loads(f"k = {toml_string(raw)}")["k"], raw)

    def test_atomic_save_leaves_no_tmp_files(self):
        s = AppSettings()
        s.set("A", AppPrefs(muted=True))
        for _ in range(3):
            s.save(self.path)
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["app-settings.toml"])

    def test_failed_save_cleans_up_and_keeps_old_file(self):
        s = AppSettings()
        s.set("A", AppPrefs(muted=True))
        s.save(self.path)
        before = self.path.read_text()
        s.set("B", AppPrefs(muted=True))
        s._apps["b"] = ("B", None)  # dumps() will blow up mid-save
        with self.assertRaises(AttributeError):
            s.save(self.path)
        self.assertEqual(self.path.read_text(), before)
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["app-settings.toml"])

    def test_save_creates_missing_directory(self):
        target = Path(self.tmp.name) / "sub" / "app-settings.toml"
        AppSettings().save(target)
        self.assertTrue(target.exists())

    def test_malformed_reports_line(self):
        self.path.write_text(
            '[[app]]\nname = "ok"\nmuted = true\n\n[[app]]\nname = "bad"\nvolume = 500\n'
        )
        with self.assertRaises(AppSettingsError) as cm:
            AppSettings.load(self.path)
        self.assertIn("line 5", str(cm.exception))
        self.assertIn("volume", str(cm.exception))
        self.path.write_text("[[app]\nname=")
        with self.assertRaises(AppSettingsError) as cm:
            AppSettings.load(self.path)
        self.assertIn("line 1", str(cm.exception))

    def test_other_load_errors(self):
        for text in (
            "[[app]]\nmuted = true\n",
            '[[app]]\nname = "a"\nbogus = 1\n',
            '[[app]]\nname = "a"\nmuted = true\n[[app]]\nname = "A"\nsticky = true\n',
            "stray = 1\n",
            '[app]\nname = "a"\n',
        ):
            with self.assertRaises(AppSettingsError, msg=text):
                AppSettings.loads(text)


class RulesetTests(unittest.TestCase):
    def one(self, prefs, app="X", n=None):
        s = AppSettings()
        s.set(app, prefs)
        rs = s.to_ruleset()
        return rs, rs.apply(n or notif(app))

    def test_each_pref_maps(self):
        _, out = self.one(AppPrefs(muted=True))
        self.assertEqual(out.state, State.STORE_ONLY)
        _, out = self.one(AppPrefs(silent=True, sound="bell"))
        self.assertEqual(out.sound, "none")
        _, out = self.one(AppPrefs(sound="bell", volume=30))
        self.assertEqual((out.sound, out.sound_volume), ("bell", 30))
        _, out = self.one(AppPrefs(volume=10))
        self.assertEqual((out.sound, out.sound_volume), (None, 10))
        _, out = self.one(AppPrefs(sticky=True))
        self.assertIsNone(out.timeout_ms)
        _, out = self.one(AppPrefs(blocked=True, muted=True))
        self.assertEqual(out.state, State.BLOCKED)
        _, out = self.one(AppPrefs(dnd_bypass=True))
        self.assertTrue(out.dnd_bypass)

    def test_rule_name_and_exact_case_insensitive_match(self):
        rs, out = self.one(AppPrefs(muted=True), app="Slack")
        self.assertEqual(rs.names, ["app: Slack"])
        self.assertEqual(out.matched_rules, ["app: Slack"])
        self.assertEqual(rs.apply(notif("SLACK")).state, State.STORE_ONLY)
        self.assertEqual(rs.apply(notif("Slack Beta")).state, State.SHOWN)
        self.assertEqual(rs.apply(notif("")).matched_rules, [])

    def test_default_prefs_make_no_rules(self):
        self.assertEqual(len(AppSettings().to_ruleset()), 0)

    def test_nasty_names_and_paths_are_literal(self):
        for i, name in enumerate(NASTY_NAMES):
            path = NASTY_PATHS[i % len(NASTY_PATHS)]
            rs, out = self.one(AppPrefs(sound=path), app=name)
            self.assertEqual(out.sound, path)
            self.assertEqual(out.matched_rules, [f"app: {name}"])
            # regex metacharacters in a name must not make it match other apps
            self.assertEqual(rs.apply(notif("Reg")).matched_rules, [])
            self.assertEqual(rs.apply(notif("anything")).matched_rules, [])

    def test_tilde_sound_expands(self):
        _, out = self.one(AppPrefs(sound="~/x.ogg"))
        self.assertEqual(out.sound, os.path.expanduser("~/x.ogg"))

    def test_user_rule_overrides_app_pref(self):
        app_rules = AppSettings()
        app_rules.set("X", AppPrefs(sticky=True, sound="bell", muted=True))
        user = RuleSet.from_toml('[[rule]]\nname = "u"\napp = "X"\ntimeout = 5000\nsound = "pop"\n')
        out = user.apply(app_rules.to_ruleset().apply(notif("X")))
        self.assertEqual((out.timeout_ms, out.sound), (5000, "pop"))
        self.assertEqual(out.state, State.STORE_ONLY)
        self.assertEqual(out.matched_rules, ["app: X", "u"])


class RuntimeTests(test_phase2.HotReloadTests):
    def start(self, rules=None, config=None, style=None, apps=None):
        if apps is not None:
            (self.dir / "app-settings.toml").write_text(apps)
        super().start(rules=rules, config=config, style=style)
        # start() built the server with the original process hook; load app settings now
        self.rt.load_initial_app_settings()

    def test_app_pref_applies_through_process_hook(self):
        self.start(apps='[[app]]\nname = "X"\nsticky = true\n')
        self.notify("X", expire=1234)
        self.assertIsNone(self.display.shown[-1].timeout_ms)

    def test_user_rule_wins_over_app_pref_end_to_end(self):
        self.start(
            rules='[[rule]]\nname = "u"\napp = "X"\ntimeout = 5000\n',
            apps='[[app]]\nname = "X"\nsticky = true\n',
        )
        self.notify("X", expire=1234)
        self.assertEqual(self.display.shown[-1].timeout_ms, 5000)

    def test_hand_edit_hot_reloads(self):
        self.start(apps='[[app]]\nname = "X"\nsticky = true\n')
        self.write_atomic("app-settings.toml", '[[app]]\nname = "X"\nblocked = true\n')
        pump_until(lambda: self.rules_signals())
        self.assertEqual(self.rules_signals(), [(True, "")])
        self.assertTrue(self.rt.get_app_prefs("x")["blocked"])
        self.assertEqual(self.rt.app_prefs_apps(), ["X"])

    def test_set_applies_immediately_and_not_twice(self):
        self.start()
        self.rt.set_app_prefs("Chat", {"sticky": True, "volume": 20})
        self.assertEqual(self.rt.app_prefs_apps(), ["Chat"])
        self.assertEqual(self.rt.get_app_prefs("chat")["volume"], 20)
        self.notify("Chat", expire=1234)  # no monitor pump yet: already in effect
        self.assertIsNone(self.display.shown[-1].timeout_ms)
        applied = self.rt.app_rules
        pump_briefly(700)  # the monitor fires for our own write
        self.assertEqual(self.rules_signals(), [])
        self.assertIs(self.rt.app_rules, applied)
        self.rt.set_app_prefs("Chat", {})  # back to default removes the entry
        self.assertEqual(self.rt.app_prefs_apps(), [])
        self.assertNotIn("Chat", (self.dir / "app-settings.toml").read_text())
        pump_briefly(700)
        self.assertEqual(self.rules_signals(), [])

    def test_set_validates_without_changing_anything(self):
        self.start()
        with self.assertRaises(ValueError):
            self.rt.set_app_prefs("Chat", {"volume": 999})
        with self.assertRaises(ValueError):
            self.rt.set_app_prefs("", {"muted": True})
        self.assertFalse((self.dir / "app-settings.toml").exists())
        self.assertEqual(self.rt.app_prefs_apps(), [])

    def test_hand_edit_after_set_still_reloads(self):
        self.start()
        self.rt.set_app_prefs("Chat", {"muted": True})
        pump_briefly(700)
        self.write_atomic("app-settings.toml", '[[app]]\nname = "Chat"\nsticky = true\n')
        pump_until(lambda: self.rules_signals())
        self.assertEqual(self.rt.get_app_prefs("Chat")["muted"], False)
        self.assertTrue(self.rt.get_app_prefs("Chat")["sticky"])

    def test_malformed_edit_keeps_previous_and_signals(self):
        self.start(apps='[[app]]\nname = "X"\nsticky = true\n')
        with self.assertLogs("notifyd", "ERROR"):
            self.write_atomic("app-settings.toml", '[[app]]\nname = "X"\nvolume = 999\n')
            pump_until(lambda: self.rules_signals())
        ok, msg = self.rules_signals()[0]
        self.assertFalse(ok)
        self.assertTrue(msg.startswith("app-settings.toml: "))
        self.assertIn("line 1", msg)
        self.assertTrue(self.rt.get_app_prefs("X")["sticky"])
        self.notify("X", expire=1234)
        self.assertIsNone(self.display.shown[-1].timeout_ms)

    def test_malformed_at_startup_is_empty(self):
        with self.assertLogs("notifyd", "ERROR"):
            self.start(apps="not = = toml")
        self.assertEqual(self.rt.app_prefs_apps(), [])
        self.assertIsNotNone(self.rt.app_settings_error)


# the inherited HotReloadTests would run again here; keep only our own tests
for _name in [n for n in dir(test_phase2.HotReloadTests) if n.startswith("test_")]:
    setattr(RuntimeTests, _name, None)
del _name

if __name__ == "__main__":
    unittest.main()
