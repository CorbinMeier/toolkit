"""SettingsService: theme, style, config.toml, sound preview, test notifications, layering."""

import logging
import tempfile
import tomllib
import unittest
from pathlib import Path

from notifyd import config as cfgmod
from notifyd import theme
from notifyd.__main__ import Config, NullCenter, Runtime, build_daemon, load_config
from notifyd.config_edit import serialize_value
from notifyd.model import State
from notifyd.rules import RuleSet
from notifyd.settings import STYLE_MAX_BYTES, SettingsError, SettingsService
from notifyd.sound import NullPlayer, Resolved
from tests.test_shade_wiring import FakeShade, ShadeCase
from tests.test_wiring import FakeCenter, WiringCase

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = (ROOT / "tests" / "data" / "default_config.toml").read_text(encoding="utf-8")


class FakePlayer:
    def __init__(self, error=None):
        self.error, self.played, self.last_error = error, [], None

    def resolve(self, spec, volume=None):
        if self.error and spec == "bad.wav":
            self.last_error = self.error
        return Resolved("file" if spec and "/" in spec else "theme", spec or "dflt", 80)

    def play(self, spec, volume=None):
        self.played.append((spec, volume))
        return self.resolve(spec, volume)

    def set_default(self, spec):
        pass

    def set_master_volume(self, v):
        pass


class FakeDisplay:
    def __init__(self):
        self.css, self.shown = [], []
        self.hover = []

    def set_user_css(self, css):
        self.css.append(css)

    def set_hover_grace(self, ms):
        self.hover.append(("grace", ms))

    def set_hover_max_hold(self, ms):
        self.hover.append(("hold", ms))

    def show(self, n):
        self.shown.append(n.id)

    def close(self, id):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cfg = self.dir / "config.toml"
        self.cfg.write_text(DEFAULT)
        self.display = FakeDisplay()
        self.player = FakePlayer(error="sound file missing or unreadable: /x/bad.wav")
        self.runtime = Runtime(self.cfg, self.display, load_config(self.cfg), player=self.player)
        self.fc_calls = []
        sounds = self.dir / "sounds" / "Pop" / "stereo"
        sounds.mkdir(parents=True)
        for n in ("bell.oga", "alarm.wav", "notes.txt", "bell.ogg"):
            (sounds / n).write_text("")
        self.svc = SettingsService(
            self.runtime,
            None,
            self.dir,
            sound_globs=(str(self.dir / "sounds" / "*" / "stereo"), str(self.dir / "nope" / "*")),
            fc_list=self._fc,
        )

    def _fc(self):
        self.fc_calls.append(1)
        return "Inter,Inter Bold\nDejaVu Sans\n\nFira Mono,Fira Mono Medium\n"

    def config_text(self):
        return self.cfg.read_text(encoding="utf-8")


class GetTests(Base):
    def test_shape(self):
        g = self.svc.get()
        self.assertEqual(
            set(g), {"theme", "style_css", "config", "config_meta", "sound_themes", "fonts"}
        )
        self.assertEqual(g["theme"]["presets"], ["default", "crt-green", "night-ops-hud"])
        self.assertEqual(g["theme"]["values"], {})
        self.assertEqual(g["theme"]["active_preset"], "default")
        self.assertEqual(len(g["theme"]["variables"]), len(theme.VARIABLES))
        self.assertEqual(
            set(g["theme"]["variables"][0]),
            {"name", "type", "default", "label", "group", "lo", "hi"},
        )
        self.assertEqual(g["style_css"], "")
        self.assertEqual(
            set(g["config"]), {"toasts", "shade", "history", "sound", "windows", "images"}
        )
        self.assertEqual(g["config"]["toasts"]["anchor"], "top-right")
        self.assertEqual(g["config"]["toasts"]["timeout_critical"], "never")
        self.assertEqual(g["config"]["toasts"]["timeout_low"], 5000)
        self.assertIsNone(g["config"]["history"]["retention_days"])
        self.assertIsNone(g["config"]["sound"]["default"])
        self.assertEqual(g["config"]["sound"]["master_volume"], 100)

    def test_meta_covers_every_config_value_and_the_declared_types(self):
        g = self.svc.get()
        types = {"int", "bool", "enum", "timeout", "path_or_theme", "volume"}
        for section, keys in g["config"].items():
            self.assertEqual(set(keys), set(g["config_meta"][section]), section)
            for key, m in g["config_meta"][section].items():
                self.assertIn(m["type"], types, key)
                self.assertTrue(m["label"] and m["help"], key)
                self.assertIsInstance(m["restart"], bool)
        meta = g["config_meta"]
        self.assertEqual(meta["toasts"]["anchor"]["choices"], list(cfgmod.ANCHORS))
        self.assertEqual(
            (meta["shade"]["width"]["min"], meta["shade"]["width"]["max"]), (240, 1000)
        )
        self.assertEqual(meta["sound"]["master_volume"]["max"], 100)

    def test_config_is_read_from_the_file(self):
        self.cfg.write_text('[toasts]\nmargin = 30\n[sound]\ndefault = "bell"\n')
        c = self.svc.get()["config"]
        self.assertEqual((c["toasts"]["margin"], c["sound"]["default"]), (30, "bell"))

    def test_sound_themes_and_fonts(self):
        g = self.svc.get()
        self.assertEqual(g["sound_themes"], ["alarm", "bell"])
        self.assertEqual(
            g["fonts"], ["DejaVu Sans", "Fira Mono", "Fira Mono Medium", "Inter", "Inter Bold"]
        )
        self.svc.get()
        self.assertEqual(len(self.fc_calls), 1, "fonts are cached")

    def test_fonts_empty_when_fc_list_is_missing(self):
        def boom():
            raise FileNotFoundError("fc-list")

        svc = SettingsService(self.runtime, None, self.dir, sound_globs=(), fc_list=boom)
        self.assertEqual(svc.get()["fonts"], [])
        self.assertEqual(svc.get()["sound_themes"], [])


class ThemeTests(Base):
    def test_save_writes_theme_css_and_returns_state(self):
        out = self.svc.save_theme({"--nc-bg": "#101010", "--nc-radius": "4px"})
        self.assertEqual(out["values"], {"--nc-bg": "#101010", "--nc-radius": "4px"})
        self.assertIsNone(out["active_preset"])
        self.assertEqual(
            theme.read_theme(self.dir / "theme.css"), {"--nc-bg": "#101010", "--nc-radius": "4px"}
        )
        self.assertEqual(self.svc.get()["theme"], out)
        self.assertEqual([p.name for p in self.dir.iterdir() if p.suffix == ".tmp"], [])

    def test_shade_and_center_variables_save(self):
        self.svc.save_theme({"--nc-shade-surface": "#222", "--nc-center-radius": "2px"})
        text = (self.dir / "theme.css").read_text()
        self.assertIn(".center {\n  --nc-center-radius: 2px;", text)
        self.assertEqual(
            self.svc.get()["theme"]["values"],
            {"--nc-shade-surface": "#222", "--nc-center-radius": "2px"},
        )

    def test_errors_are_collected_per_variable_and_nothing_is_written(self):
        self.svc.save_theme({"--nc-bg": "#111111"})
        with self.assertRaises(SettingsError) as cm:
            self.svc.save_theme({"--nc-bg": "#222222", "--nc-radius": "red; x", "--nope": "1px"})
        self.assertEqual(set(cm.exception.errors), {"--nc-radius", "--nope"})
        self.assertEqual(theme.read_theme(self.dir / "theme.css"), {"--nc-bg": "#111111"})

    def test_non_dict_and_bad_types(self):
        with self.assertRaises(SettingsError):
            self.svc.save_theme(["x"])
        with self.assertRaises(SettingsError) as cm:
            self.svc.save_theme({"--nc-opacity": 0.5})
        self.assertIn("--nc-opacity", cm.exception.errors)

    def test_none_and_empty_mean_no_override(self):
        out = self.svc.save_theme({"--nc-bg": None, "--nc-fg": "", "--nc-gap": "3px"})
        self.assertEqual(out["values"], {"--nc-gap": "3px"})
        with self.assertRaises(SettingsError):
            self.svc.save_theme({"--unknown": ""})

    def test_active_preset_detection(self):
        self.assertEqual(self.svc.save_theme({})["active_preset"], "default")
        for name in ("crt-green", "night-ops-hud"):
            out = self.svc.save_theme(self.svc.preset_values(name))
            self.assertEqual(out["active_preset"], name)
        # a preset plus one tweak is no longer that preset
        values = self.svc.preset_values("crt-green")
        values["--nc-radius"] = "3px"
        self.assertIsNone(self.svc.save_theme(values)["active_preset"])
        # values equal to the defaults do not break a match
        values = self.svc.preset_values("crt-green")
        values["--nc-gap"] = theme.BY_NAME["--nc-gap"].default
        self.assertEqual(self.svc.save_theme(values)["active_preset"], "crt-green")

    def test_preset_values_does_not_save(self):
        v = self.svc.preset_values("crt-green")
        self.assertEqual(v, theme.PRESETS["crt-green"])
        self.assertFalse((self.dir / "theme.css").exists())
        v["--nc-bg"] = "red"
        self.assertNotEqual(theme.PRESETS["crt-green"]["--nc-bg"], "red")
        with self.assertRaises(SettingsError):
            self.svc.preset_values("nope")


class StyleTests(Base):
    def test_save_and_get(self):
        self.svc.save_style(".toast { color: red }\n")
        self.assertEqual(self.svc.get()["style_css"], ".toast { color: red }\n")
        self.assertEqual([p.name for p in self.dir.iterdir() if p.suffix == ".tmp"], [])

    def test_not_validated_beyond_size_and_utf8(self):
        self.svc.save_style("this is { not ( css")
        self.assertEqual(self.svc.get()["style_css"], "this is { not ( css")
        self.svc.save_style("")
        self.assertEqual(self.svc.get()["style_css"], "")

    def test_cap(self):
        self.svc.save_style("a" * STYLE_MAX_BYTES)
        for bad in ("a" * (STYLE_MAX_BYTES + 1), "é" * (STYLE_MAX_BYTES // 2 + 1)):
            with self.assertRaises(SettingsError) as cm:
                self.svc.save_style(bad)
            self.assertIn("style_css", cm.exception.errors)
        self.assertEqual(len((self.dir / "style.css").read_bytes()), STYLE_MAX_BYTES)

    def test_bad_input(self):
        for bad in (None, 5, "\ud800"):
            with self.assertRaises(SettingsError):
                self.svc.save_style(bad)


# (section, key, candidates), all different from the key's default.
CANDIDATES = {
    ("toasts", "anchor"): ["bottom-left", "middle", "", 5, True, None],
    ("toasts", "margin"): [0, 40, -1, 1.5, "12", True],
    ("toasts", "timeout_low"): [1, 2500, "never", 0, -5, "soon", 1.5, True],
    ("toasts", "timeout_normal"): [3000, "never", 0, "Never", False],
    ("toasts", "timeout_critical"): [9000, 0, "never "],
    ("toasts", "respect_app_timeout"): [False, "false", 0],
    ("toasts", "hover_leave_grace_ms"): [0, 250, -1, "5", 2.5],
    ("toasts", "hover_max_hold_ms"): [0, 5000, -1, True],
    ("shade", "anchor"): ["bottom-center", "nowhere", 1],
    ("shade", "margin"): [0, 20, -4, "x"],
    ("shade", "width"): [240, 1000, 500, 239, 1001, "500", True],
    ("shade", "clear_toasts_on_open"): [False, "false", 0, 1],
    ("history", "retention_days"): [30, 1, 0, -1, "30", True],
    ("sound", "default"): ["bell", "~/a.mp3", "none", "", "   ", 3],
    ("sound", "master_volume"): [0, 50, 99, 101, -1, "50", 50.5, True],
    ("windows", "prewarm"): [False, True, "false", 0, 1],
    ("images", "preview"): [False, True, "false", 0, 1],
}


class ValidatorSharingTests(Base):
    def test_candidates_cover_every_field(self):
        self.assertEqual(set(CANDIDATES), set(cfgmod.BY_KEY))

    def test_loader_and_save_config_agree_on_every_value(self):
        defaults = cfgmod.Config()
        for (section, key), values in CANDIDATES.items():
            f = cfgmod.BY_KEY[(section, key)]
            for value in values:
                if value is None:
                    continue  # TOML has no null; None is save_config's "remove"
                if f.external(getattr(defaults, f.attr)) == value:
                    continue
                with self.subTest(section=section, key=key, value=value):
                    try:
                        saved = True
                        self.cfg.write_text(DEFAULT)
                        self.svc.save_config(section, {key: value})
                    except SettingsError:
                        saved = False
                    # the loader, reading a hand-written file with the same value
                    self.cfg.write_text(f"[{section}]\n{key} = {serialize_value(value)}\n")
                    with self.assertLogs("notifyd", "ERROR") if not saved else _nullctx():
                        loaded = load_config(self.cfg)
                    took = getattr(loaded, f.attr) != getattr(defaults, f.attr)
                    self.assertEqual(saved, took, "save_config and load_config disagree")
                    if saved:
                        self.assertEqual(
                            f.external(getattr(loaded, f.attr)),
                            value.strip() if isinstance(value, str) else value,
                        )

    def test_load_config_logs_the_shared_rule_text(self):
        self.cfg.write_text("[shade]\nwidth = 5\n")
        with self.assertLogs("notifyd", "ERROR") as logs:
            self.assertEqual(load_config(self.cfg).shade_width, 420)
        self.assertIn("shade.width 5 invalid (integer 240-1000)", logs.output[0])

    def test_errors_are_keyed_by_key(self):
        with self.assertRaises(SettingsError) as cm:
            self.svc.save_config("shade", {"width": 5, "margin": 8, "bogus": 1})
        self.assertEqual(set(cm.exception.errors), {"width", "bogus"})
        self.assertEqual(self.config_text(), DEFAULT, "nothing is written on error")

    def test_unknown_section_and_non_nullable_none(self):
        with self.assertRaises(SettingsError):
            self.svc.save_config("rules", {"x": 1})
        with self.assertRaises(SettingsError) as cm:
            self.svc.save_config("toasts", {"margin": None})
        self.assertIn("margin", cm.exception.errors)


class _nullctx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class SaveConfigFileTests(Base):
    def test_comments_survive(self):
        self.svc.save_config(
            "toasts", {"margin": 20, "timeout_normal": "never", "respect_app_timeout": False}
        )
        self.svc.save_config("sound", {"default": " bell ", "master_volume": 60})
        self.svc.save_config("history", {"retention_days": 90})
        self.svc.save_config("shade", {"width": 500})
        text = self.config_text()
        for comment in (line for line in DEFAULT.splitlines() if line.startswith("#")):
            if "timeout_normal =" in comment or "respect_app_timeout =" in comment:
                continue  # becomes a real line, comment text above stays
            if comment.lstrip("# ").split(" =")[0] in ("retention_days", "default", "width"):
                continue
            self.assertIn(comment, text)
        self.assertIn("# Default toast lifetime per urgency", text)
        self.assertIn("# Master volume, 0-100", text)
        data = tomllib.loads(text)
        self.assertEqual(data["toasts"]["margin"], 20)
        self.assertEqual(data["toasts"]["timeout_normal"], "never")
        self.assertIs(data["toasts"]["respect_app_timeout"], False)
        self.assertEqual(data["sound"], {"default": "bell", "master_volume": 60})
        self.assertEqual(data["history"], {"retention_days": 90})
        self.assertEqual(data["shade"], {"width": 500})
        loaded = load_config(self.cfg)
        self.assertEqual((loaded.margin, loaded.timeout_normal), (20, None))

    def test_remove_with_none(self):
        self.svc.save_config("history", {"retention_days": 90})
        self.svc.save_config("sound", {"default": "bell"})
        self.svc.save_config("history", {"retention_days": None})
        self.svc.save_config("sound", {"default": None})
        data = tomllib.loads(self.config_text())
        self.assertNotIn("retention_days", data.get("history", {}))
        self.assertNotIn("default", data["sound"])
        self.assertIsNone(self.svc.get()["config"]["history"]["retention_days"])

    def test_missing_file_is_created(self):
        self.cfg.unlink()
        self.svc.save_config("toasts", {"margin": 7})
        self.assertEqual(tomllib.loads(self.config_text()), {"toasts": {"margin": 7}})

    def test_unparseable_file_is_an_error_not_a_crash(self):
        self.cfg.write_text("[toasts\nmargin = ")
        with self.assertRaises(SettingsError) as cm:
            self.svc.save_config("toasts", {"margin": 7})
        self.assertIn("config", cm.exception.errors)


class RestartRequiredTests(Base):
    """`restart_required` must match what Runtime.reload_config really does."""

    NEW = {
        ("toasts", "anchor"): "bottom-left",
        ("toasts", "margin"): 40,
        ("toasts", "timeout_low"): 2500,
        ("toasts", "timeout_normal"): "never",
        ("toasts", "timeout_critical"): 9000,
        ("toasts", "respect_app_timeout"): False,
        ("toasts", "hover_leave_grace_ms"): 250,
        ("toasts", "hover_max_hold_ms"): 5000,
        ("shade", "anchor"): "bottom-center",
        ("shade", "margin"): 20,
        ("shade", "width"): 500,
        ("shade", "clear_toasts_on_open"): False,
        ("history", "retention_days"): 30,
        ("sound", "default"): "bell",
        ("sound", "master_volume"): 40,
        ("windows", "prewarm"): False,
        ("images", "preview"): False,
    }

    def test_truth_table_against_reload_config(self):
        self.assertEqual(set(self.NEW), set(cfgmod.BY_KEY))
        shade = FakeShade.__new__(FakeShade)
        shade.calls = []
        shade.configure = lambda **kw: shade.calls.append(kw)
        self.runtime.shade = shade
        for (section, key), value in self.NEW.items():
            with self.subTest(key=f"{section}.{key}"):
                f = cfgmod.BY_KEY[(section, key)]
                self.cfg.write_text(DEFAULT)
                self.runtime.config = load_config(self.cfg)
                self.runtime.timeouts = self.runtime.config.timeout_policy()
                before = getattr(self.runtime.config, f.attr)
                out = self.svc.save_config(section, {key: value})
                self.runtime.reload_config()  # what the file watcher does after the save
                applied = getattr(self.runtime.config, f.attr) != before
                self.assertEqual(out["restart_required"], [] if applied else [key])
                self.assertEqual(f.restart, not applied, "Field.restart is not truthful")

    def test_only_a_real_change_is_reported(self):
        self.assertEqual(
            self.svc.save_config("toasts", {"anchor": "top-right"}), {"restart_required": []}
        )
        out = self.svc.save_config("toasts", {"anchor": "top-left", "margin": 12, "timeout_low": 1})
        self.assertEqual(out, {"restart_required": ["anchor"]})
        self.assertEqual(
            self.svc.save_config("history", {"retention_days": None}), {"restart_required": []}
        )

    def test_a_change_pending_since_an_earlier_save_is_still_reported(self):
        self.svc.save_config("toasts", {"margin": 30})
        self.runtime.reload_config()  # the daemon keeps running with 12
        self.assertEqual(self.runtime.config.margin, 12)
        self.assertEqual(
            self.svc.save_config("toasts", {"margin": 30}), {"restart_required": ["margin"]}
        )


class PreviewSoundTests(Base):
    def test_plays_and_reports_the_resolution(self):
        r = self.svc.preview_sound("bell", 70)
        self.assertEqual(r, {"ok": True, "resolved": "bell", "error": None})
        self.assertEqual(self.player.played, [("bell", 70)])
        r = self.svc.preview_sound("/a/b.wav", None)
        self.assertEqual(r["resolved"], "/a/b.wav")
        self.assertEqual(self.player.played[-1], ("/a/b.wav", None))

    def test_default_when_no_spec(self):
        r = self.svc.preview_sound(None, None)
        self.assertTrue(r["ok"])
        self.assertEqual(self.player.played, [(None, None)])

    def test_error_is_reported_and_nothing_plays(self):
        r = self.svc.preview_sound("bad.wav", 50)
        self.assertFalse(r["ok"])
        self.assertIn("missing", r["error"])
        self.assertEqual(self.player.played, [])

    def test_stale_error_does_not_leak(self):
        self.player.last_error = "old problem"
        self.assertTrue(self.svc.preview_sound("bell", 50)["ok"])

    def test_bad_arguments(self):
        for spec, vol in ((5, None), ("bell", "loud"), ("bell", True)):
            self.assertFalse(self.svc.preview_sound(spec, vol)["ok"])
        self.assertEqual(self.player.played, [])

    def test_none_player_is_fine(self):
        self.runtime.player = NullPlayer()
        r = self.svc.preview_sound("bell", 50)
        self.assertEqual(r, {"ok": True, "resolved": "none", "error": None})


class LayeringTests(ShadeCase):
    def test_theme_then_style_reaches_toasts_shade_and_center(self):
        self.build()
        (self.dir / "theme.css").write_text(":root{--nc-bg:red}\n")
        (self.dir / "style.css").write_text(".x{}")
        self.runtime.apply_style()
        want = ":root{--nc-bg:red}\n\n.x{}"
        self.assertEqual(self.display.css, [want])
        self.assertEqual(self.center.css, [want])
        self.assertEqual(self.shade.of("css"), [("css", want)])
        self.assertLess(want.index("--nc-bg"), want.index(".x{}"), "style.css must come last")

    def test_missing_files_are_empty(self):
        self.build()
        self.runtime.apply_style()
        self.assertEqual(self.display.css, ["\n"])

    def test_theme_css_is_watched(self):
        from notifyd.__main__ import WATCHED_FILES

        self.assertIn("theme.css", WATCHED_FILES)


class WiringTests(ShadeCase):
    def test_build_daemon_assigns_the_service(self):
        self.build()
        self.assertIsInstance(self.center.settings, SettingsService)
        self.assertIs(self.center.settings.server, self.server)
        self.assertIs(self.center.settings.runtime, self.runtime)
        self.assertEqual(self.center.settings.dir, self.runtime.dir)

    def test_null_center_accepts_it(self):
        c = NullCenter()
        self.assertIsNone(c.settings)
        c.settings = object()


class SendTestTests(WiringCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.sounds = []

    def build_with(self, rules_text=""):
        self.runtime = Runtime(
            self.dir / "config.toml", self.display, Config(), player=NullPlayer()
        )
        self.runtime.rules = RuleSet.from_toml(rules_text)
        self.server = build_daemon(
            self.server_conn,
            self.store,
            self.display,
            FakeCenter,
            None,
            self.runtime,
            process=self.runtime.process,
            sound=self.sounds.append,
        )
        return self.server.center.settings

    def test_normal_goes_through_rules_store_display_and_sound(self):
        svc = self.build_with(
            '[[rule]]\nname="t"\napp="Notification Center"\ntags=["seen-by-rule"]\nsound="bell"\n'
        )
        nid = svc.send_test("normal")
        n = self.store.get(nid)
        self.assertEqual(n.app_name, "Notification Center")
        self.assertIn("Test notification", n.summary)
        self.assertEqual(n.state, State.SHOWN)
        self.assertEqual(n.tags, ["seen-by-rule"], "rules ran")
        self.assertEqual(self.display.shown, [nid])
        self.assertEqual([s.id for s in self.sounds], [nid])
        self.assertEqual(self.sounds[0].sound, "bell")
        self.assertEqual(int(n.urgency), 1)

    def test_ids_are_distinct_and_recorded(self):
        svc = self.build_with()
        a, b = svc.send_test("normal"), svc.send_test("normal")
        self.assertNotEqual(a, b)
        self.assertIsNotNone(self.store.get(a))
        self.assertIsNotNone(self.store.get(b))

    def test_critical(self):
        svc = self.build_with()
        n = self.store.get(svc.send_test("critical"))
        self.assertEqual(int(n.urgency), 2)
        self.assertIn("critical", n.summary.lower())

    def test_buttons_has_actions_link_and_markup(self):
        svc = self.build_with()
        n = self.store.get(svc.send_test("buttons"))
        self.assertEqual(n.actions, [("default", "Open"), ("reply", "Reply")])
        self.assertIn("<b>", n.body)
        self.assertIn('<a href="https://example.org">', n.body)

    def test_block_rule_still_records_but_does_not_show(self):
        svc = self.build_with('[[rule]]\nname="b"\napp="Notification Center"\nblock=true\n')
        nid = svc.send_test("normal")
        self.assertEqual(self.store.get(nid).state, State.BLOCKED)
        self.assertEqual(self.display.shown, [])

    def test_dnd_applies(self):
        svc = self.build_with()
        self.server.set_dnd(True)
        self.assertEqual(self.store.get(svc.send_test("normal")).state, State.DND)

    def test_bad_kind(self):
        svc = self.build_with()
        with self.assertRaises(SettingsError):
            svc.send_test("loud")
        self.assertEqual(self.display.shown, [])

    def test_added_signal_is_emitted_on_the_bus(self):
        svc = self.build_with()
        from tests.test_integration import pump_until

        nid = svc.send_test("normal")
        pump_until(lambda: self.names("NotificationAdded"))
        self.assertEqual(self.names("NotificationAdded")[0][0], nid)


if __name__ == "__main__":
    logging.disable(logging.NOTSET)
    unittest.main()
