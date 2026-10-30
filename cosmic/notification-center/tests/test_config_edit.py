import os
import re
import tempfile
import tomllib
import unittest
from pathlib import Path

from notifyd import config_edit as ce

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = (ROOT / "tests" / "data" / "default_config.toml").read_text(encoding="utf-8")


def diff_lines(a: str, b: str) -> tuple[list[str], list[str]]:
    """(lines only in a, lines only in b), order kept."""
    sa, sb = a.splitlines(), b.splitlines()
    return [x for x in sa if x not in sb], [x for x in sb if x not in sa]


class FixtureTests(unittest.TestCase):
    def test_fixture_is_the_installed_default(self):
        install = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
        m = re.search(r"<<'TOML'\n(.*?)\nTOML\n", install, re.S)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1) + "\n", DEFAULT)


class SetValuesTests(unittest.TestCase):
    def test_replace_existing_key_keeps_everything_else(self):
        out = ce.set_values(DEFAULT, {"toasts": {"margin": 30, "anchor": "bottom-left"}})
        self.assertEqual(tomllib.loads(out)["toasts"], {"anchor": "bottom-left", "margin": 30})
        gone, new = diff_lines(DEFAULT, out)
        self.assertEqual(sorted(gone), ['anchor = "top-right"', "margin = 12"])
        self.assertEqual(sorted(new), ['anchor = "bottom-left"', "margin = 30"])
        self.assertEqual(len(out.splitlines()), len(DEFAULT.splitlines()))

    def test_trailing_comment_and_indent_kept(self):
        text = '[a]\n  key = 5   # why\nother = "x" # keep\n'
        out = ce.set_values(text, {"a": {"key": 7, "other": "y"}})
        self.assertEqual(out, '[a]\n  key = 7   # why\nother = "y" # keep\n')

    def test_hash_inside_string_is_not_a_comment(self):
        text = '[a]\nk = "a # b" # real\n'
        out = ce.set_values(text, {"a": {"k": "z"}})
        self.assertEqual(out, '[a]\nk = "z" # real\n')

    def test_commented_default_becomes_real_line_under_the_comment(self):
        out = ce.set_values(DEFAULT, {"toasts": {"timeout_normal": 12000}})
        lines = out.splitlines()
        i = lines.index("# timeout_normal = 10000")
        self.assertEqual(lines[i + 1], "timeout_normal = 12000")
        self.assertEqual(lines[i + 2], '# timeout_critical = "never"')
        self.assertEqual(tomllib.loads(out)["toasts"]["timeout_normal"], 12000)
        self.assertEqual(len(out.splitlines()), len(DEFAULT.splitlines()) + 1)

    def test_never_string(self):
        out = ce.set_values(DEFAULT, {"toasts": {"timeout_critical": "never"}})
        self.assertEqual(tomllib.loads(out)["toasts"]["timeout_critical"], "never")

    def test_missing_key_without_comment_added_before_trailing_blank(self):
        text = "[a]\nx = 1\n\n\n[b]\ny = 2\n"
        out = ce.set_values(text, {"a": {"z": True}})
        self.assertEqual(out, "[a]\nx = 1\nz = true\n\n\n[b]\ny = 2\n")

    def test_missing_table_appended(self):
        out = ce.set_values(DEFAULT, {"behavior": {"dnd": True, "name": "x"}})
        self.assertTrue(out.startswith(DEFAULT))
        self.assertTrue(out.endswith('\n[behavior]\ndnd = true\nname = "x"\n'))
        self.assertEqual(tomllib.loads(out)["behavior"], {"dnd": True, "name": "x"})

    def test_missing_table_in_text_without_final_newline(self):
        out = ce.set_values("[a]\nx = 1", {"b": {"y": 2}})
        self.assertEqual(out, "[a]\nx = 1\n\n[b]\ny = 2\n")

    def test_empty_text(self):
        self.assertEqual(ce.set_values("", {"a": {"x": 1}}), "[a]\nx = 1\n")

    def test_idempotent(self):
        upd = {"toasts": {"timeout_normal": 12000, "margin": 9}, "sound": {"master_volume": 40}}
        once = ce.set_values(DEFAULT, upd)
        self.assertEqual(ce.set_values(once, upd), once)

    def test_roundtrip_every_default_key(self):
        data = tomllib.loads(DEFAULT)
        upd = {
            "toasts": {"respect_app_timeout": False, "hover_max_hold_ms": 0},
            "shade": {"anchor": "top-left", "margin": 3, "width": 500},
            "history": {"retention_days": 30},
            "sound": {"default": '~/Music/söund "x".mp3', "master_volume": 5},
        }
        out = ce.set_values(DEFAULT, upd)
        got = tomllib.loads(out)
        for table, kv in upd.items():
            for k, v in kv.items():
                self.assertEqual(got[table][k], v)
        self.assertEqual(got["toasts"]["anchor"], data["toasts"]["anchor"])

    def test_string_escaping(self):
        nasty = 'a"b\\c\nd\te\x01f é ☃ # not a comment'
        out = ce.set_values("[a]\n", {"a": {"k": nasty}})
        self.assertEqual(tomllib.loads(out)["a"]["k"], nasty)

    def test_bool_is_not_an_int(self):
        out = ce.set_values("[a]\n", {"a": {"t": True, "f": False, "i": 1}})
        self.assertEqual(tomllib.loads(out)["a"], {"t": True, "f": False, "i": 1})

    def test_unsupported_value_types(self):
        for bad in (None, [1], {"a": 1}, float("nan")):
            with self.assertRaises((TypeError, ValueError)):
                ce.set_values("[a]\n", {"a": {"k": bad}})

    def test_bad_names_refused(self):
        with self.assertRaises(ValueError):
            ce.set_values("[a]\n", {"a": {"k = 1\n[evil]\nx": 1}})
        with self.assertRaises(ValueError):
            ce.set_values("[a]\n", {"a]\n[b": {"k": 1}})

    def test_crlf_preserved(self):
        text = "[a]\r\nx = 1\r\n"
        out = ce.set_values(text, {"a": {"x": 2, "y": 3}})
        self.assertEqual(out, "[a]\r\nx = 2\r\ny = 3\r\n")

    def test_other_tables_and_arrays_untouched(self):
        text = (
            "[meta]\n"
            "tags = [\n  'a', # one\n  'b',\n]\n"
            "inline = { x = 1, y = [2, 3] }\n"
            'long = """\n[fake]\nk = 1\n"""\n'
            "\n[[rule]]\nname = 'r1'\n\n[[rule]]\nname = 'r2'\n"
            "\n[toasts]\nmargin = 1\n"
        )
        out = ce.set_values(text, {"toasts": {"margin": 2, "anchor": "top-left"}})
        gone, new = diff_lines(text, out)
        self.assertEqual(gone, ["margin = 1"])
        self.assertEqual(sorted(new), ['anchor = "top-left"', "margin = 2"])
        self.assertEqual(tomllib.loads(out)["meta"], tomllib.loads(text)["meta"])
        self.assertEqual(tomllib.loads(out)["rule"], tomllib.loads(text)["rule"])

    def test_edit_beside_multiline_array_in_same_table(self):
        text = "[t]\nlist = [\n  1,\n  2,\n]\nn = 1\n"
        out = ce.set_values(text, {"t": {"n": 5, "m": 6}})
        self.assertEqual(out, "[t]\nlist = [\n  1,\n  2,\n]\nn = 5\nm = 6\n")

    def test_header_with_comment_and_spaces(self):
        text = "[ a ]  # note\nx = 1\n"
        out = ce.set_values(text, {"a": {"x": 2}})
        self.assertEqual(out, "[ a ]  # note\nx = 2\n")

    def test_refuses_array_of_tables_target(self):
        text = "[[rule]]\nname = 'a'\n"
        with self.assertRaises(ValueError):
            ce.set_values(text, {"rule": {"name": "b"}})
        with self.assertRaises(ValueError):
            ce.remove_keys(text, {"rule": ["name"]})

    def test_refuses_to_overwrite_array_or_inline_value(self):
        for val in ("[1, 2]", "{ a = 1 }", "[\n1,\n]", '"""x\ny"""'):
            with self.assertRaises(ValueError, msg=val):
                ce.set_values(f"[t]\nk = {val}\n", {"t": {"k": 1}})

    def test_duplicate_key_that_breaks_toml_is_caught(self):
        with self.assertRaises(ValueError):
            ce.set_values("a = 1\n[t]\nx = 1\n", {"a": {"b": 1}})  # [a] collides with key a


class RemoveKeysTests(unittest.TestCase):
    def test_removes_only_the_real_line(self):
        text = DEFAULT.replace("# timeout_low = 5000", "timeout_low = 5000")
        out = ce.remove_keys(text, {"toasts": ["timeout_low"], "nothing": ["x"], "shade": ["nope"]})
        self.assertNotIn("timeout_low = 5000", out)
        self.assertEqual(len(out.splitlines()), len(text.splitlines()) - 1)

    def test_commented_default_stays(self):
        out = ce.remove_keys(DEFAULT, {"toasts": ["timeout_low"]})
        self.assertEqual(out, DEFAULT)

    def test_removes_multiline_value(self):
        text = "[t]\na = 1\nl = [\n 1,\n 2,\n]\nb = 2\n"
        self.assertEqual(ce.remove_keys(text, {"t": ["l"]}), "[t]\na = 1\nb = 2\n")

    def test_set_then_remove_restores_default(self):
        once = ce.set_values(DEFAULT, {"toasts": {"timeout_normal": 12000}})
        self.assertEqual(ce.remove_keys(once, {"toasts": ["timeout_normal"]}), DEFAULT)


class UpdateFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.toml"

    def test_writes_atomically_and_leaves_no_tmp(self):
        self.path.write_text(DEFAULT, encoding="utf-8")
        os.chmod(self.path, 0o600)
        ce.update_file(self.path, {"toasts": {"margin": 40}}, {"toasts": ["anchor"]})
        got = tomllib.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(got["toasts"], {"margin": 40})
        self.assertEqual(os.listdir(self.tmp.name), ["config.toml"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_missing_file_is_created(self):
        ce.update_file(self.path, {"sound": {"master_volume": 10}})
        self.assertEqual(tomllib.loads(self.path.read_text())["sound"]["master_volume"], 10)

    def test_refusal_writes_nothing(self):
        text = "[[rule]]\nname = 'a'\n"
        self.path.write_text(text)
        with self.assertRaises(ValueError):
            ce.update_file(self.path, {"rule": {"name": "b"}})
        self.assertEqual(self.path.read_text(), text)
        self.assertEqual(os.listdir(self.tmp.name), ["config.toml"])

    def test_noop_does_not_rewrite(self):
        self.path.write_text(DEFAULT)
        before = self.path.stat().st_mtime_ns
        ce.update_file(self.path, {"toasts": {"margin": 12}})
        self.assertEqual(self.path.stat().st_mtime_ns, before)


if __name__ == "__main__":
    unittest.main()
