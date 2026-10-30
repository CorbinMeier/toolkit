"""rules_edit: block-level, comment-preserving edits of rules.toml."""

import tempfile
import tomllib
import unittest
from pathlib import Path

from notifyd import rules_edit as re_
from notifyd.rules import RuleSet
from notifyd.rules_edit import RulesEditError

ROOT = Path(__file__).resolve().parent.parent
USER = (ROOT / "tests" / "data" / "rules_user.toml").read_text(encoding="utf-8")
USER_NL = USER if USER.endswith("\n") else USER + "\n"
EXAMPLE = (ROOT / "examples" / "rules.toml").read_text(encoding="utf-8")
# the user-style fixture: the shipped example plus a block with buttons, hint, run, comments
MIXED = EXAMPLE + "\n" + USER.replace('"example sound"', '"example sound 2"')


def rules(text):
    return tomllib.loads(text).get("rule", [])


def blocks_text(text):
    header, blocks = re_._split(text)
    return "".join(header), [b.text() for b in blocks]


class SplitTests(unittest.TestCase):
    def test_every_file_joins_back_byte_for_byte(self):
        for text in (USER, USER_NL, EXAMPLE, MIXED, "", "\n\n", "# only comments\n"):
            header, blocks = re_._split(text)
            self.assertEqual(re_._join(header, blocks), text)

    def test_header_comments_stay_apart_from_the_first_rule(self):
        header, blocks = blocks_text(USER)
        self.assertTrue(header.startswith("# --- my notification rules ---"))
        self.assertTrue(header.endswith("\n\n"))
        self.assertTrue(blocks[0].startswith("# Example gets its own sound.\n[[rule]]"))

    def test_leading_comment_run_belongs_to_the_block(self):
        _, blocks = blocks_text(USER)
        self.assertTrue(blocks[1].startswith("# CI failures: loud"))
        self.assertIn("(the second comment line stays attached)", blocks[1])

    def test_subtables_and_buttons_are_in_the_body(self):
        _, blocks = re_._split(USER)
        body = "".join(blocks[1].body)
        self.assertIn("[rule.hint]", body)
        self.assertEqual(body.count("[[rule.buttons]]"), 3)
        self.assertIn("# open the build page", body)

    def test_tail_keeps_orphan_comments_with_the_block(self):
        _, blocks = re_._split(USER)
        tail = "".join(blocks[1].tail)
        self.assertIn("section: quiet ones", tail)
        self.assertIn('# name = "later"', tail)

    def test_multiline_string_does_not_hide_or_fake_a_header(self):
        text = (
            '[[rule]]\nname = "a"\nrun = """\n[[rule]]\n# not a header\n"""\n\n'
            '[[rule]]\nname = "b"\n'
        )
        self.assertEqual(len(re_._split(text)[1]), 2)
        self.assertEqual([r["name"] for r in re_.list_rules(text)], ["a", "b"])

    def test_unsupported_layouts_are_refused(self):
        for text in ('x = 1\n[[rule]]\nname = "a"\n', "[other]\na = 1\n", "[rule]\nname='a'\n"):
            with self.assertRaises(RulesEditError):
                re_.list_rules(text)
        with self.assertRaises(RulesEditError):
            re_.list_rules('rule = [{ name = "a" }]\n')


class ListTests(unittest.TestCase):
    def test_list_shape(self):
        out = re_.list_rules(USER)
        self.assertEqual([r["index"] for r in out], [0, 1, 2, 3])
        self.assertEqual(out[0]["name"], "example sound")
        self.assertTrue(out[0]["enabled"])
        self.assertFalse(out[2]["enabled"])
        self.assertEqual(out[0]["data"], rules(USER)[0])
        self.assertTrue(out[1]["source"].startswith("# CI failures"))
        self.assertIn("[[rule.buttons]]", out[1]["source"])

    def test_summary_reads_like_the_spec(self):
        self.assertEqual(
            re_.summarize(
                {
                    "name": "x",
                    "app": "Example App",
                    "sound": "bell",
                    "buttons": [{"label": "a", "builtin": "details"}] * 2,
                }
            ),
            "app = Example App → sound, 2 buttons",
        )
        self.assertTrue(re_.summarize({"name": "x"}).startswith("every notification → "))
        self.assertIn(" or ", re_.list_rules(USER)[2]["summary"])

    def test_invalid_toml_raises(self):
        with self.assertRaises(RulesEditError):
            re_.list_rules("[[rule]\n")

    def test_example_lists(self):
        self.assertEqual(
            [r["name"] for r in re_.list_rules(EXAMPLE)],
            ["example sound", "URGENT stays and glows red", "mute noise"],
        )


class IdentityTests(unittest.TestCase):
    def test_untouched_data_returns_identical_text(self):
        for text in (USER, EXAMPLE, MIXED):
            for i, r in enumerate(re_.list_rules(text)):
                self.assertEqual(re_.update_rule(text, i, r["data"]), text)

    def test_set_enabled_to_the_same_state_is_a_noop(self):
        self.assertEqual(re_.set_enabled(USER, 0, True), USER)
        self.assertEqual(re_.set_enabled(USER, 2, False), USER)


class UpdateTests(unittest.TestCase):
    def edit(self, text, i, **changes):
        data = dict(re_.list_rules(text)[i]["data"])
        for k, v in changes.items():
            if v is None:
                data.pop(k, None)
            else:
                data[k] = v
        return re_.update_rule(text, i, data)

    def assert_only_block_changed(self, before, after, i):
        h1, b1 = blocks_text(before)
        h2, b2 = blocks_text(after)
        self.assertEqual(h1, h2)
        self.assertEqual(len(b1), len(b2))
        for j, (x, y) in enumerate(zip(b1, b2, strict=True)):
            if j == i:
                self.assertNotEqual(x, y)
            else:
                self.assertEqual(x, y, f"block {j} changed")

    def test_changing_one_value_keeps_every_other_byte(self):
        out = self.edit(USER, 0, volume=40)
        self.assertEqual(out, USER.replace("volume = 70", "volume = 40"))

    def test_inline_comment_survives_a_changed_value(self):
        out = self.edit(USER, 0, sound="bell")
        self.assertIn('sound = "bell"     # my file', out)
        self.assertIn("# exact, case-insensitive", out)
        self.assert_only_block_changed(USER, out, 0)

    def test_other_blocks_are_identical_in_a_bigger_file(self):
        out = self.edit(MIXED, 4, cooldown=60)
        self.assert_only_block_changed(MIXED, out, 4)
        self.assertIn("cooldown = 60\n", out)
        self.assertEqual(rules(out)[4]["cooldown"], 60)

    def test_leading_comments_stay(self):
        out = self.edit(USER, 1, name="ci broke")
        self.assertIn("# CI failures: loud, with buttons and a logger.\n# (the second", out)
        self.assertEqual(rules(out)[1]["name"], "ci broke")
        self.assert_only_block_changed(USER, out, 1)

    def test_removing_a_key_removes_its_line_only(self):
        out = self.edit(USER, 0, volume=None)
        self.assertEqual(out, USER.replace("volume = 70\n", ""))

    def test_adding_a_key_goes_in_canonical_place(self):
        out = self.edit(USER, 0, timeout=3000)
        self.assertEqual(rules(out)[0]["timeout"], 3000)
        lines = out.splitlines()
        self.assertEqual(lines[lines.index("volume = 70") + 1], "timeout = 3000")

    def test_new_key_without_a_neighbor_after_header(self):
        out = self.edit(EXAMPLE, 2, block=True, urgency="low")
        self.assertEqual(rules(out)[2]["urgency"], "low")
        self.assert_only_block_changed(EXAMPLE, out, 2)

    def test_changed_button_is_rewritten_others_are_verbatim(self):
        data = re_.list_rules(USER)[1]["data"]
        data = {**data, "buttons": [dict(b) for b in data["buttons"]]}
        data["buttons"][1]["minutes"] = 30
        out = re_.update_rule(USER, 1, data)
        self.assertIn("minutes = 30", out)
        self.assertIn('# open the build page\n[[rule.buttons]]\nlabel = "Open"', out)
        self.assertIn('label = "Copy"\nbuiltin = "copy-body"\n', out)
        self.assertEqual(rules(out)[1]["buttons"][1]["minutes"], 30)
        self.assert_only_block_changed(USER, out, 1)

    def test_removing_and_adding_buttons(self):
        data = re_.list_rules(USER)[1]["data"]
        fewer = {**data, "buttons": data["buttons"][:1]}
        out = re_.update_rule(USER, 1, fewer)
        self.assertEqual(len(rules(out)[1]["buttons"]), 1)
        self.assertIn("# open the build page", out)
        more = {**data, "buttons": data["buttons"] + [{"label": "Mark", "builtin": "mark-read"}]}
        out = re_.update_rule(USER, 1, more)
        self.assertEqual(
            [b["label"] for b in rules(out)[1]["buttons"]], ["Open", "Snooze", "Copy", "Mark"]
        )
        self.assertEqual(out.count("[[rule.buttons]]"), 4)
        none = {k: v for k, v in data.items() if k != "buttons"}
        out = re_.update_rule(USER, 1, none)
        self.assertNotIn("buttons", rules(out)[1])
        self.assertNotIn("[[rule.buttons]]", out)

    def test_hint_table_edit_and_removal(self):
        out = self.edit(USER, 1, hint={"x-status": "^ok"})
        self.assertEqual(rules(out)[1]["hint"], {"x-status": "^ok"})
        self.assertIn("# a note between the keys and the tables", out)
        out = self.edit(USER, 1, hint=None)
        self.assertNotIn("hint", rules(out)[1])
        self.assertNotIn("[rule.hint]", out)

    def test_new_hint_and_buttons_become_subtables(self):
        out = self.edit(
            USER,
            0,
            hint={"x-a": "b", "odd key": "c"},
            buttons=[{"label": "Details", "builtin": "details"}],
        )
        self.assertIn("[rule.hint]\n", out)
        self.assertIn('"odd key" = "c"', out)
        self.assertIn('[[rule.buttons]]\nlabel = "Details"\nbuiltin = "details"\n', out)
        self.assertEqual(rules(out)[0]["hint"], {"x-a": "b", "odd key": "c"})
        self.assert_only_block_changed(USER, out, 0)

    def test_multiline_inline_array_is_replaced_as_one_value(self):
        out = self.edit(USER, 1, highlight=[{"pattern": "boom", "class": "b"}])
        self.assertEqual(rules(out)[1]["highlight"], [{"pattern": "boom", "class": "b"}])
        self.assertNotIn("# red", out)
        self.assertIn("run = [", out)
        self.assert_only_block_changed(USER, out, 1)

    def test_unchanged_multiline_array_keeps_its_comments(self):
        out = self.edit(USER, 1, cooldown=31)
        self.assertIn(
            "  { pattern = '(?i)\\bfail(ed|ure)?\\b', class = \"urgent\" },   # red\n", out
        )

    def test_dotted_keys_fall_back_to_a_clean_block(self):
        text = (
            '# lead\n[[rule]]\nname = "a"\nhint.x = "y"   # dotted\n\n'
            '# next\n[[rule]]\nname = "b"\n'
        )
        out = re_.update_rule(text, 0, {"name": "a", "hint": {"x": "z"}})
        self.assertEqual(rules(out)[0]["hint"], {"x": "z"})
        self.assertTrue(out.startswith("# lead\n[[rule]]\n"))
        self.assertIn('\n\n# next\n[[rule]]\nname = "b"\n', out)

    def test_defaults_and_empties_are_not_written(self):
        out = self.edit(
            USER,
            0,
            enabled=True,
            match_mode="all",
            block=False,
            app="",
            tags=[],
            note=None,
        )
        self.assertNotIn("enabled", out.split("[[rule]]")[1].split("[[rule]]")[0])
        self.assertNotIn("block", out.split("[[rule]]")[1].split("\n\n")[0])
        self.assertEqual(out.count("match_mode"), 1)  # only the other rule's
        self.assertNotIn("app = ", out.split("[[rule]]")[1].split("\n\n")[0])

    def test_existing_default_keys_are_kept(self):
        text = '[[rule]]\nname = "a"\nenabled = true\nblock = false\n'
        out = re_.update_rule(text, 0, {"name": "a", "enabled": True, "block": False, "stop": True})
        self.assertIn("enabled = true", out)
        self.assertIn("block = false", out)

    def test_last_block_without_newline(self):
        out = self.edit(USER, 3, icon="x")
        self.assertTrue(out.endswith('icon = "x"\n'))
        self.assertEqual(rules(out)[3]["icon"], "x")

    def test_crlf_files_keep_crlf(self):
        text = USER_NL.replace("\n", "\r\n")
        out = self.edit(text, 0, timeout=5, volume=None)
        self.assertNotIn("\n", out.replace("\r\n", ""))
        self.assertEqual(rules(out)[0]["timeout"], 5)


class StringTests(unittest.TestCase):
    def roundtrip(self, **kw):
        text = '[[rule]]\nname = "a"\n'
        out = re_.update_rule(text, 0, {"name": "a", **kw})
        got = rules(out)[0]
        for k, v in kw.items():
            self.assertEqual(got[k], v)
        return out

    def test_strings_with_escapes_and_unicode(self):
        weird = 'tab\t "quote" back\\slash é ✓ \U0001f600 line\nbreak \x01 \x7f'
        for v in (weird, "plain", 'a "b"', "back\\slash", "it's \\ here", "café ✓"):
            self.roundtrip(sound=v)

    def test_regex_backslashes_use_literal_quotes(self):
        out = self.roundtrip(summary=r"(?i)\bURGENT\b")
        self.assertIn(r"summary = '(?i)\bURGENT\b'", out)

    def test_ints_bools_lists(self):
        out = self.roundtrip(
            volume=0,
            timeout=1500,
            dnd_bypass=True,
            stop=True,
            tags=["a", "b_c"],
            urgency=["low", "critical"],
            run=["sh", "-c", "echo 1"],
            cooldown=5,
        )
        self.assertIn("dnd_bypass = true", out)
        self.roundtrip(timeout="never", run="echo hi")

    def test_inline_highlight_tables(self):
        out = self.roundtrip(
            highlight=[{"pattern": r"\d+", "class": "n"}, {"pattern": "x", "class": "y"}]
        )
        self.assertIn("highlight = [{ pattern = '\\d+', class = \"n\" }, ", out)
        self.roundtrip(highlight={"pattern": "x", "class": "y"})

    def test_lone_surrogate_is_refused(self):
        with self.assertRaises(RulesEditError):
            re_.update_rule('[[rule]]\nname = "a"\n', 0, {"name": "a", "sound": "\ud800"})

    def test_unserializable_value_is_refused(self):
        with self.assertRaises(RulesEditError):
            re_.update_rule('[[rule]]\nname = "a"\n', 0, {"name": "a", "sound": object()})


class StructureTests(unittest.TestCase):
    def names(self, text):
        return [r["name"] for r in rules(text)]

    def test_delete_takes_comments_and_keeps_the_rest(self):
        out = re_.delete_rule(USER, 1)
        self.assertEqual(
            self.names(out), ["example sound", "mute noise ☾", "last, no trailing newline"]
        )
        self.assertNotIn("CI failures", out)
        self.assertNotIn("[[rule.buttons]]", out)
        _, b_before = blocks_text(USER)
        _, b_after = blocks_text(out)
        self.assertEqual(b_after[0], b_before[0])
        self.assertEqual(b_after[1], b_before[2])
        self.assertEqual(b_after[2], b_before[3])

    def test_delete_the_last_and_the_first(self):
        out = re_.delete_rule(USER_NL, 3)
        self.assertEqual(len(rules(out)), 3)
        self.assertTrue(out.endswith("stop = true\n"))
        out = re_.delete_rule(USER, 0)
        self.assertTrue(out.startswith("# --- my notification rules ---"))
        self.assertEqual(self.names(out)[0], "ci failed")

    def test_delete_everything_leaves_the_header(self):
        out = USER
        for _ in range(4):
            out = re_.delete_rule(out, 0)
        self.assertEqual(
            out,
            (
                "# --- my notification rules ---\n"
                "# Hand written; the Rules panel must leave all of this alone.\n\n"
            ),
        )
        self.assertEqual(RuleSet.from_toml(out).names, [])

    def test_move_carries_comments(self):
        out = re_.move_rule(USER, 1, 0)
        self.assertEqual(self.names(out)[:2], ["ci failed", "example sound"])
        header, blocks = blocks_text(out)
        self.assertTrue(blocks[0].startswith("# CI failures: loud"))
        self.assertIn("# open the build page", blocks[0])
        self.assertIn("section: quiet ones", blocks[0])  # the tail travels too
        self.assertTrue(blocks[1].startswith("# Example gets its own sound.\n[[rule]]"))
        self.assertEqual(sorted(self.names(out)), sorted(self.names(USER)))

    def test_move_to_the_end_and_back_restores_the_rules(self):
        out = re_.move_rule(USER, 0, 3)
        self.assertEqual(self.names(out)[-1], "example sound")
        self.assertEqual(rules(out)[:3], rules(USER)[1:])
        self.assertEqual(re_.move_rule(out, 3, 0).count("\n\n\n"), 0)
        back = re_.move_rule(out, 3, 0)
        self.assertEqual(rules(back), rules(USER))
        # each block still starts with its own comment
        for b, first in zip(
            blocks_text(back)[1],
            ("# Example gets", "# CI failures", "# Mute chat", "[[rule]]"),
            strict=True,
        ):
            self.assertTrue(b.startswith(first), b[:30])

    def test_move_last_to_middle_keeps_a_valid_file(self):
        out = re_.move_rule(USER, 3, 1)
        self.assertEqual(self.names(out)[1], "last, no trailing newline")
        self.assertEqual(len(rules(out)), 4)
        RuleSet.from_toml(out)

    def test_move_same_index_is_a_noop(self):
        self.assertEqual(re_.move_rule(USER, 2, 2), USER)

    def test_add_appends_with_a_blank_line(self):
        out = re_.add_rule(USER, {"name": "new", "app": "X", "sound": "bell"})
        self.assertEqual(self.names(out)[-1], "new")
        self.assertIn(
            'icon = "~/icons/a.png"\n\n[[rule]]\nname = "new"\napp = "X"\nsound = "bell"\n', out
        )
        self.assertTrue(out.startswith(USER.rstrip("\n")))

    def test_add_at_a_position_leaves_other_blocks_alone(self):
        out = re_.add_rule(USER, {"name": "new"}, 1)
        self.assertEqual(
            self.names(out),
            ["example sound", "new", "ci failed", "mute noise ☾", "last, no trailing newline"],
        )
        _, before = blocks_text(USER)
        _, after = blocks_text(out)
        self.assertEqual(after[0], before[0])
        self.assertEqual(after[2:], before[1:])

    def test_add_into_an_empty_or_comment_only_file(self):
        out = re_.add_rule("", {"name": "a"})
        self.assertEqual(out, '[[rule]]\nname = "a"\n')
        out = re_.add_rule("# my rules\n", {"name": "a"})
        self.assertEqual(out, '# my rules\n\n[[rule]]\nname = "a"\n')
        self.assertEqual(len(re_.list_rules(out)), 1)

    def test_add_buttons_and_hint(self):
        out = re_.add_rule(
            "",
            {
                "name": "a",
                "hint": {"k": "v"},
                "buttons": [{"label": "Snooze", "builtin": "snooze", "minutes": 5}],
            },
        )
        self.assertEqual(
            out,
            "[[rule]]\nname = \"a\"\n[rule.hint]\nk = 'v'\n".replace("'v'", '"v"')
            + '[[rule.buttons]]\nlabel = "Snooze"\nbuiltin = "snooze"\nminutes = 5\n',
        )

    def test_set_enabled(self):
        off = re_.set_enabled(USER, 0, False)
        self.assertFalse(rules(off)[0]["enabled"])
        self.assertIn('name = "example sound"\nenabled = false\n', off)
        self.assertEqual(
            re_.set_enabled(off, 0, True), off.replace("enabled = false\n", "enabled = true\n", 1)
        )
        on = re_.set_enabled(USER, 2, True)
        self.assertTrue(rules(on)[2]["enabled"])
        self.assertEqual(re_.list_rules(on)[2]["enabled"], True)

    def test_operations_do_not_pile_up_blank_lines(self):
        out = USER_NL
        for i in (0, 1, 2, 3, 0, 2):
            out = re_.move_rule(out, i, 3 - i if i < 4 else 0)
        self.assertNotIn("\n\n\n\n", out)
        self.assertEqual(sorted(self.names(out)), sorted(self.names(USER)))


class ValidationTests(unittest.TestCase):
    def fails(self, fn, *args):
        with self.assertRaises(RulesEditError) as cm:
            fn(*args)
        return cm.exception.errors

    def test_bad_regex_is_keyed_by_field(self):
        errs = self.fails(re_.update_rule, USER, 0, {"name": "example sound", "summary": "(["})
        self.assertEqual(list(errs), ["summary"])
        self.assertIn("not a valid regex", errs["summary"])

    def test_field_paths(self):
        base = {"name": "a"}
        cases = [
            ({**base, "app_regex": "("}, "app_regex"),
            ({**base, "volume": 101}, "volume"),
            ({**base, "timeout": 0}, "timeout"),
            ({**base, "set_urgency": "loud"}, "set_urgency"),
            ({**base, "class": ["2fast"]}, "class"),
            ({**base, "tags": ["a b"]}, "tags"),
            ({**base, "sumary": "x"}, "sumary"),
            ({**base, "hint": {"k": "("}}, "hint.k"),
            ({**base, "highlight": [{"pattern": "(", "class": "x"}]}, "highlight.pattern"),
            ({**base, "buttons": [{"label": "", "builtin": "details"}]}, "buttons[0].label"),
            ({**base, "buttons": [{"label": "x", "builtin": "snooze"}]}, "buttons[0]"),
            ({**base, "buttons": [{"label": "x", "builtin": "nope"}]}, "buttons[0].builtin"),
            ({**base, "buttons": [{"label": "x", "run": "x", "bogus": 1}]}, "buttons[0].bogus"),
            ({**base, "run_when": "shown"}, "run_when"),
            ({**base, "run": "echo", "run_timeout": 0}, "run_timeout"),
            ({**base, "run": ""}, None),
            ({"name": ""}, "name"),
            ({"name": "a", "match_mode": "some"}, "match_mode"),
        ]
        for data, path in cases:
            if path is None:  # an empty run is dropped, so this is valid
                re_.update_rule('[[rule]]\nname = "a"\n', 0, data)
                continue
            errs = self.fails(re_.update_rule, '[[rule]]\nname = "a"\n', 0, data)
            self.assertIn(path, errs, (data, errs))

    def test_duplicate_name(self):
        errs = self.fails(re_.add_rule, USER, {"name": "example sound"})
        self.assertEqual(list(errs), ["name"])
        errs = self.fails(re_.update_rule, USER, 1, {"name": "example sound"})
        self.assertEqual(list(errs), ["name"])

    def test_error_in_another_rule_is_general(self):
        text = '[[rule]]\nname = "a"\nvolume = 500\n\n[[rule]]\nname = "b"\n'
        errs = self.fails(re_.update_rule, text, 1, {"name": "b2"})
        self.assertEqual(list(errs), [""])
        self.assertIn("rule #1", errs[""])
        self.assertEqual(len(rules(re_.delete_rule(text, 0))), 1)  # deleting the culprit works

    def test_bad_index_and_position(self):
        for fn, args in (
            (re_.update_rule, (USER, 9, {"name": "x"})),
            (re_.update_rule, (USER, -1, {"name": "x"})),
            (re_.update_rule, (USER, True, {"name": "x"})),
            (re_.delete_rule, (USER, 4)),
            (re_.move_rule, (USER, 0, 4)),
            (re_.move_rule, (USER, 9, 0)),
            (re_.add_rule, (USER, {"name": "x"}, 5)),
            (re_.add_rule, (USER, {"name": "x"}, "1")),
            (re_.set_enabled, (USER, 0, "yes")),
            (re_.update_rule, (USER, 0, [])),
        ):
            self.fails(fn, *args)

    def test_failed_operations_return_nothing_and_write_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "rules.toml"
            p.write_text(USER, encoding="utf-8")
            with self.assertRaises(RulesEditError):
                re_.write_file(p, USER + "\n[[rule]]\nname = 'x'\nsumary = 'y'\n")
            with self.assertRaises(RulesEditError):
                re_.write_file(p, "[[rule]\n")
            self.assertEqual(p.read_text(encoding="utf-8"), USER)
            re_.write_file(p, USER_NL + "\n")
            self.assertEqual(p.read_text(encoding="utf-8"), USER_NL + "\n")
            self.assertEqual([x.name for x in Path(d).iterdir()], ["rules.toml"])

    def test_write_file_creates_a_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "rules.toml"
            re_.write_file(p, '[[rule]]\nname = "a"\n')
            self.assertEqual(RuleSet.load(p).names, ["a"])


class EveryKeyTests(unittest.TestCase):
    """Every action and match key survives a write and loads in the real RuleSet."""

    FULL = {
        "name": "everything",
        "enabled": False,
        "match_mode": "any",
        "app": "App",
        "app_regex": "^A",
        "summary": r"\bfoo\b",
        "body": "bar$",
        "urgency": ["low", "critical"],
        "category": ["email", "im"],
        "hint": {"x-status": "^fail", "x-n": "1"},
        "hint_present": ["x-url"],
        "block": True,
        "store_only": True,
        "sound": "~/s.oga",
        "volume": 55,
        "timeout": "never",
        "set_urgency": "critical",
        "icon": "mail-unread",
        "class": ["a", "b"],
        "highlight": [{"pattern": "(?i)x", "class": "hl"}, {"pattern": r"\d", "class": "n"}],
        "tags": ["t1", "t-2"],
        "dnd_bypass": True,
        "stop": True,
        "run": ["sh", "-c", "echo $NC_APP"],
        "run_when": "shown",
        "cooldown": 9,
        "run_timeout": 12,
        "buttons": [
            {"label": "Open", "run": "xdg-open x"},
            {"label": "Argv", "run": ["a", "b"], "close": True},
            {"label": "Snz", "builtin": "snooze", "minutes": 7},
            {"label": "Det", "builtin": "details", "close": True},
            {"label": "CB", "builtin": "copy-body"},
            {"label": "CS", "builtin": "copy-summary"},
        ],
    }

    def test_every_key_roundtrips_through_add_and_update(self):
        out = re_.add_rule("", self.FULL)
        got = rules(out)[0]
        self.assertEqual(got, self.FULL)
        RuleSet.from_toml(out)
        # clear it down to a name, then fill it again on top of a patched block
        bare = re_.update_rule(out, 0, {"name": "everything"})
        self.assertEqual(rules(bare)[0], {"name": "everything"})
        self.assertEqual(rules(re_.update_rule(bare, 0, self.FULL))[0], self.FULL)

    def test_each_key_alone(self):
        for k, v in self.FULL.items():
            if k in ("name",):
                continue
            data = {"name": "one", k: v}
            if k in ("run_when", "cooldown", "run_timeout"):
                data["run"] = "echo"
            out = re_.add_rule("", data)
            self.assertEqual(rules(out)[0][k], v, k)

    def test_urgency_and_other_scalars_as_strings(self):
        out = re_.add_rule(
            "", {"name": "x", "urgency": "critical", "class": "one", "category": "im"}
        )
        self.assertEqual(rules(out)[0]["class"], "one")
        self.assertEqual(rules(out)[0]["urgency"], "critical")

    def test_example_files_survive_every_operation(self):
        out = EXAMPLE
        out = re_.set_enabled(out, 1, False)
        out = re_.move_rule(out, 2, 0)
        self.assertIn("# --- scripts and buttons (commented out", out)  # the tail moved along
        out = re_.add_rule(out, {"name": "n"}, 1)
        out = re_.delete_rule(out, 0)
        self.assertNotIn("# --- scripts and buttons", out)
        self.assertEqual(
            RuleSet.from_toml(out).names, ["n", "example sound", "URGENT stays and glows red"]
        )


if __name__ == "__main__":
    unittest.main()
