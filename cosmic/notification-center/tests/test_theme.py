import re
import tempfile
import unittest
from pathlib import Path

from notifyd import theme

WEB = Path(__file__).resolve().parent.parent / "web"


def _block_vars(file: str, selector: str) -> dict[str, str]:
    css = (WEB / file).read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    block = re.search(rf"^{re.escape(selector)}\s*\{{(.*?)\n\}}", css, re.S | re.M).group(1)
    return {
        m.group(1): " ".join(m.group(2).split())
        for m in re.finditer(r"(--nc-[a-z0-9-]+)\s*:\s*([^;]+);", block)
    }


def parse_default_css() -> dict[str, str]:
    """Every theme variable the shipped CSS defines: default.css, shade.css, center.css."""
    out = _block_vars("default.css", ":root")
    for file, selector in (("shade.css", ":root"), ("center.css", ".center")):
        extra = _block_vars(file, selector)
        assert not set(extra) & set(out), f"{file} redefines {set(extra) & set(out)}"
        out.update(extra)
    return out


class SyncTests(unittest.TestCase):
    def test_variables_match_default_css_exactly(self):
        css = parse_default_css()
        mine = {v.name: v.default for v in theme.VARIABLES}
        self.assertEqual(set(mine), set(css), "VARIABLES and default.css disagree on names")
        for name, default in mine.items():
            self.assertEqual(default, css[name], name)

    def test_metadata_is_complete(self):
        names = [v.name for v in theme.VARIABLES]
        self.assertEqual(len(names), len(set(names)))
        for v in theme.VARIABLES:
            self.assertIn(v.type, theme.TYPES, v.name)
            self.assertIn(v.group, theme.GROUPS, v.name)
            self.assertTrue(v.label)

    def test_every_default_is_valid_for_its_own_type(self):
        for v in theme.VARIABLES:
            self.assertEqual(theme.validate(v.name, v.default), v.default, v.name)


class ValidationTests(unittest.TestCase):
    def ok(self, name, *values):
        for v in values:
            self.assertEqual(theme.validate(name, v), v.strip(), (name, v))

    def bad(self, name, *values):
        for v in values:
            with self.assertRaises(ValueError, msg=f"{name}: {v!r}"):
                theme.validate(name, v)

    def test_colors(self):
        self.ok(
            "--nc-bg",
            "#fff", "#FFFF", "#1f2125", "#1f212580", "rgb(1, 2, 3)", "rgba(1,2,3,0.5)",
            "hsl(120 50% 40%)", "hsla(120, 50%, 40%, .5)", "rgb(0 0 0 / 50%)", "rebeccapurple",
            "Transparent", "inherit",
        )  # fmt: skip
        self.bad(
            "--nc-bg",
            "#ff", "#12345", "#gggggg", "rgb(", "rgb(1,2,3", "red blue", "notacolor", "",
            "var(--x)", "rgb(1,2,3) url(x)", 5,
        )  # fmt: skip

    def test_lengths(self):
        self.ok("--nc-radius", "0", "12px", "0.5rem", "1.25em", "50%", ".5px")
        self.bad("--nc-radius", "12", "12pt", "-3px", "1e3px", "px", "12 px", "calc(1px)", "auto")

    def test_number_range(self):
        self.ok("--nc-opacity", "0", "1", "0.5", ".9")
        self.bad("--nc-opacity", "1.5", "-0.1", "abc", "50%")

    def test_durations(self):
        self.ok("--nc-anim-duration", "220ms", "0.3s", "0ms", "1s")
        self.bad("--nc-anim-duration", "220", "fast", "-1s", "1m")

    def test_fonts(self):
        self.ok(
            "--nc-font-family",
            '"Inter", "Noto Sans", system-ui, sans-serif',
            "Fira Mono, monospace",
            "'VT323', monospace",
        )
        self.bad(
            "--nc-font-family",
            "Arial; color: red",
            "Arial} body{x:y",
            "url(http://x/f.woff)",
            "a<b",
            '"unclosed, serif',
            "Arial, ",
            "x\\41",
            "",
            "Arial\nSerif",
        )

    def test_shadows(self):
        self.ok(
            "--nc-shadow",
            "none",
            "0 6px 20px rgba(0, 0, 0, 0.45)",
            "0 0 14px #3f8, inset 0 0 8px rgba(61, 255, 122, 0.12)",
            "2px 2px red",
            "inset 0 0 4px 1px black",
            "0 -2px 6px #000",
        )
        self.bad(
            "--nc-shadow",
            "1px",
            "0 0 -4px red",
            "0 0 0 0 0 red",
            "0 0 4px red blue",
            "0 0 4px nocolor",
            "0 0 4px red, ",
            "red 0 0 4px 1px 2px",
            "0 0 4px rgba(0,0,0",
        )

    def test_animation_and_easing(self):
        self.ok(
            "--nc-enter-animation",
            "none",
            "nc-enter var(--nc-anim-duration) ease-out both",
            "nc-enter 300ms cubic-bezier(0.2, 0.8, 0.2, 1) forwards",
            "nc-exit 100ms steps(4, end)",
        )
        self.bad(
            "--nc-enter-animation",
            "evil 1s linear",
            "nc-enter 1s linear, nc-exit 1s",
            "nc-enter nc-exit",
            "nc-enter 1s 2s",
            "nc-enter var(--other)",
        )
        self.assertTrue(theme._is_easing("ease-in-out"))
        self.assertFalse(theme._is_easing("ease-sideways"))

    def test_injection_attempts_rejected_for_every_type(self):
        payloads = [
            "red; background: url(http://evil/x)",
            "red}\n:root{--x:1",
            "red /* c */",
            "red */ x",
            "url(http://evil)",
            "expression(alert(1))",
            "@import 'x.css'",
            "red\nblue",
            "red\r\nblue",
            "red !important",
            "\\72 ed",
            "red<script>",
            "red\x00",
        ]
        for var in theme.VARIABLES:
            for p in payloads:
                with self.assertRaises(ValueError, msg=f"{var.name}: {p!r}"):
                    theme.validate(var.name, p)
            # also prefixed by a legitimate value
            with self.assertRaises(ValueError):
                theme.validate(var.name, var.default + "; color: red")
        with self.assertRaises(ValueError):
            theme.render_theme_css({"--nc-bg": "red; } body { display:none"})

    def test_unknown_variable_rejected(self):
        for name in ("--nc-nope", "--other", "bg", "--nc-bg; x"):
            with self.assertRaises(ValueError):
                theme.render_theme_css({name: "red"})


class RenderTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(theme.render_theme_css({}), "")

    def test_only_overrides_in_variable_order(self):
        css = theme.render_theme_css({"--nc-radius": "4px", "--nc-bg": "#000"})
        self.assertEqual(css, ":root {\n  --nc-bg: #000;\n  --nc-radius: 4px;\n}\n")

    def test_rendering_is_valid_css_shape(self):
        css = theme.render_theme_css({v.name: v.default for v in theme.VARIABLES})
        self.assertEqual(css.count("{"), 2)  # :root and .center
        self.assertEqual(css.count("}"), 2)
        self.assertEqual(css.count(";"), len(theme.VARIABLES))

    def test_center_variables_go_on_the_center_rule(self):
        # center.css declares them on `.center`, which would shadow a :root override.
        css = theme.render_theme_css({"--nc-center-radius": "2px", "--nc-bg": "#000"})
        self.assertEqual(
            css,
            ":root {\n  --nc-bg: #000;\n}\n.center {\n  --nc-center-radius: 2px;\n}\n",
        )
        self.assertEqual(
            theme.read_theme_text(css), {"--nc-bg": "#000", "--nc-center-radius": "2px"}
        )

    def test_references_to_other_variables(self):
        self.assertEqual(theme.validate("--nc-danger", "var(--nc-accent)"), "var(--nc-accent)")
        self.assertEqual(
            theme.validate("--nc-center-radius", "min(var(--nc-radius), 4px)"),
            "min(var(--nc-radius), 4px)",
        )
        for name, bad in (
            ("--nc-danger", "var(--nc-danger)"),  # itself
            ("--nc-danger", "var(--nc-nope)"),
            ("--nc-danger", "var(--x)"),
            ("--nc-fg", "var(--nc-fg"),
            ("--nc-radius", "min(var(--nc-gap), url(x))"),
            ("--nc-font-size", "var(--nc-bg); x"),
        ):
            with self.assertRaises(ValueError, msg=bad):
                theme.validate(name, bad)


class FileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "theme.css"

    def test_roundtrip(self):
        values = theme.apply_preset("crt-green")
        theme.write_theme(self.path, values)
        self.assertEqual(theme.read_theme(self.path), values)
        text = self.path.read_text()
        self.assertTrue(text.startswith("/* theme.css:"))
        self.assertIn("overwritten", text)
        self.assertEqual([p.name for p in Path(self.tmp.name).iterdir()], ["theme.css"])

    def test_empty_roundtrip(self):
        theme.write_theme(self.path, {})
        self.assertEqual(theme.read_theme(self.path), {})
        self.assertNotIn(":root", self.path.read_text())

    def test_invalid_write_leaves_file_alone(self):
        theme.write_theme(self.path, {"--nc-bg": "#111111"})
        with self.assertRaises(ValueError):
            theme.write_theme(self.path, {"--nc-bg": "red; x"})
        self.assertEqual(theme.read_theme(self.path), {"--nc-bg": "#111111"})

    def test_read_missing_and_hand_edited(self):
        self.assertEqual(theme.read_theme(self.path), {})
        self.path.write_text(
            "/* --nc-fg: red; */\n:root {\n --nc-bg: #123456;\n --nc-radius: bogus;\n"
            " --other: 1px;\n --nc-gap: 4px;\n}\n"
        )
        self.assertEqual(theme.read_theme(self.path), {"--nc-bg": "#123456", "--nc-gap": "4px"})

    def test_theme_path_is_under_the_app_dir(self):
        self.assertTrue(
            str(theme.theme_path()).endswith("/net.corbinmeier.notifications/theme.css")
        )


class PresetTests(unittest.TestCase):
    def test_names(self):
        self.assertEqual(set(theme.PRESETS), {"default", "crt-green", "night-ops-hud"})
        self.assertEqual(theme.PRESETS["default"], {})

    def test_each_preset_renders_valid_css_with_known_variables_only(self):
        known = {v.name for v in theme.VARIABLES}
        for name, values in theme.PRESETS.items():
            self.assertLessEqual(set(values), known, name)
            self.assertEqual(theme.apply_preset(name), values)
            css = theme.render_theme_css(values)
            if values:
                self.assertEqual(set(re.findall(r"--nc-[a-z0-9-]+(?=:)", css)), set(values))
                self.assertEqual(css.count(";"), len(values))

    def test_apply_returns_a_copy(self):
        a = theme.apply_preset("crt-green")
        a["--nc-bg"] = "red"
        self.assertNotEqual(theme.PRESETS["crt-green"]["--nc-bg"], "red")

    def test_unknown_preset(self):
        with self.assertRaises(KeyError):
            theme.apply_preset("nope")

    def test_shipped_files_match_the_presets(self):
        for name in theme.PRESETS:
            self.assertEqual(
                (WEB / "presets" / f"{name}.css").read_text(), theme.preset_css(name), name
            )
        files = {p.stem for p in (WEB / "presets").glob("*.css")}
        self.assertEqual(files, set(theme.PRESETS))


class DocsTests(unittest.TestCase):
    def test_theming_doc_has_the_current_table(self):
        doc = (Path(__file__).resolve().parent.parent / "docs" / "theming.md").read_text()
        self.assertIn(theme.variables_table(), doc)


if __name__ == "__main__":
    unittest.main()
