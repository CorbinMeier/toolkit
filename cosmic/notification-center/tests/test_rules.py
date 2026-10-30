import logging
import os
import tempfile
import unittest
from pathlib import Path

from notifyd.model import Notification, State, Urgency, to_web
from notifyd.rules import RulesError, RuleSet, js_incompatibilities

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "rules.toml"


def make(**kw) -> Notification:
    hints = kw.pop("hints", {})
    n = Notification.from_notify(
        1,
        kw.pop("app", "App"),
        0,
        kw.pop("icon", ""),
        kw.pop("summary", "Hello"),
        kw.pop("body", "World"),
        [],
        hints,
        kw.pop("expire", -1),
        1000.0,
    )
    for k, v in kw.items():
        setattr(n, k, v)
    return n


def rs(toml: str) -> RuleSet:
    return RuleSet.from_toml(toml)


class MatchTests(unittest.TestCase):
    def matched(self, rule: str, n: Notification) -> bool:
        return bool(rs(f'[[rule]]\nname = "r"\n{rule}\n').apply(n).matched_rules)

    def test_no_match_fields_matches_all(self):
        self.assertTrue(self.matched("", make()))

    def test_app_exact_case_insensitive(self):
        self.assertTrue(self.matched('app = "example app"', make(app="Example App")))
        self.assertFalse(self.matched('app = "Management"', make(app="Example App")))

    def test_app_regex(self):
        self.assertTrue(self.matched('app_regex = "^Exa"', make(app="Example App")))
        self.assertFalse(self.matched('app_regex = "^App"', make(app="Example App")))

    def test_summary_and_body_regex_searched(self):
        self.assertTrue(self.matched('summary = "ell"', make(summary="Hello")))
        self.assertFalse(self.matched('summary = "^ell"', make(summary="Hello")))
        self.assertTrue(self.matched('body = "(?i)WOR"', make(body="World")))

    def test_urgency_single_and_list(self):
        crit = make(urgency=Urgency.CRITICAL)
        self.assertTrue(self.matched('urgency = "critical"', crit))
        self.assertFalse(self.matched('urgency = "low"', crit))
        self.assertTrue(self.matched('urgency = ["low", "Critical"]', crit))

    def test_category_exact_and_list(self):
        n = make(category="email.arrived")
        self.assertTrue(self.matched('category = "email.arrived"', n))
        self.assertFalse(self.matched('category = "email"', n))
        self.assertTrue(self.matched('category = ["im", "email.arrived"]', n))

    def test_hint_table(self):
        n = make(hints={"x-kind": "build-ok", "count": 5})
        self.assertTrue(self.matched('[rule.hint]\n"x-kind" = "^build"', n))
        self.assertTrue(self.matched("[rule.hint]\ncount = '^5$'", n))
        self.assertFalse(self.matched('[rule.hint]\n"x-kind" = "fail"', n))
        self.assertFalse(self.matched('[rule.hint]\nmissing = ".*"', n))

    def test_hint_all_keys_required(self):
        n = make(hints={"a": "1"})
        self.assertFalse(self.matched('[rule.hint]\na = "1"\nb = "2"', n))

    def test_hint_present(self):
        n = make(hints={"a": 1, "b": 2})
        self.assertTrue(self.matched('hint_present = ["a", "b"]', n))
        self.assertFalse(self.matched('hint_present = ["a", "c"]', n))

    def test_match_mode_all_default(self):
        n = make(summary="Hello", body="Nope")
        self.assertFalse(self.matched('summary = "Hello"\nbody = "World"', n))
        self.assertTrue(self.matched('summary = "Hello"\nbody = "Nope"', n))

    def test_match_mode_any(self):
        rule = 'match_mode = "any"\nsummary = "Hello"\nbody = "World"'
        self.assertTrue(self.matched(rule, make(summary="Hello", body="x")))
        self.assertTrue(self.matched(rule, make(summary="x", body="World")))
        self.assertFalse(self.matched(rule, make(summary="x", body="y")))

    def test_match_mode_any_without_fields_matches(self):
        self.assertTrue(self.matched('match_mode = "any"', make()))

    def test_regex_input_limited_to_10000_chars(self):
        n = make(body="a" * 10_000 + "NEEDLE")
        self.assertFalse(self.matched('body = "NEEDLE"', n))
        n = make(body="a" * 9_990 + "NEEDLE")
        self.assertTrue(self.matched('body = "NEEDLE"', n))


class ActionTests(unittest.TestCase):
    def one(self, actions: str, **kw) -> Notification:
        return rs(f'[[rule]]\nname = "r"\n{actions}\n').apply(make(**kw))

    def test_block(self):
        self.assertEqual(self.one("block = true").state, State.BLOCKED)

    def test_store_only(self):
        self.assertEqual(self.one("store_only = true").state, State.STORE_ONLY)

    def test_block_beats_store_only_in_one_rule(self):
        self.assertEqual(self.one("block = true\nstore_only = true").state, State.BLOCKED)

    def test_false_flags_are_noops(self):
        n = self.one("block = false\nstore_only = false\ndnd_bypass = false")
        self.assertEqual(n.state, State.SHOWN)
        self.assertFalse(n.dnd_bypass)

    def test_sound_path_theme_none(self):
        self.assertEqual(self.one('sound = "/a/b.mp3"').sound, "/a/b.mp3")
        self.assertEqual(self.one('sound = "bell"').sound, "bell")
        self.assertEqual(self.one('sound = "none"').sound, "none")

    def test_sound_tilde_expanded(self):
        self.assertEqual(self.one('sound = "~/x.wav"').sound, os.path.expanduser("~/x.wav"))

    def test_volume(self):
        self.assertEqual(self.one("volume = 40").sound_volume, 40)
        self.assertEqual(self.one("volume = 0").sound_volume, 0)

    def test_timeout_ms_and_never(self):
        self.assertEqual(self.one("timeout = 1234").timeout_ms, 1234)
        n = self.one('timeout = "never"', expire=2000)
        self.assertIsNone(n.timeout_ms)

    def test_set_urgency_updates_urgency(self):
        n = self.one('set_urgency = "critical"')
        self.assertEqual(n.urgency, Urgency.CRITICAL)

    def test_icon(self):
        self.assertEqual(self.one('icon = "mail-unread"', icon="old").app_icon, "mail-unread")

    def test_class_str_and_list(self):
        self.assertEqual(self.one('class = "a"').classes, ["a"])
        self.assertEqual(self.one('class = ["a", "b-c"]').classes, ["a", "b-c"])

    def test_highlight_table_and_list(self):
        one = self.one("highlight = { pattern = 'x', class = 'hl' }")
        self.assertEqual(one.highlights, [{"pattern": "x", "class": "hl"}])
        many = self.one(
            "highlight = [{ pattern = 'x', class = 'a' }, { pattern = 'y', class = 'b' }]"
        )
        self.assertEqual([h["class"] for h in many.highlights], ["a", "b"])

    def test_tags(self):
        self.assertEqual(self.one('tags = ["ci", "build_ok"]').tags, ["ci", "build_ok"])

    def test_dnd_bypass(self):
        self.assertTrue(self.one("dnd_bypass = true").dnd_bypass)

    def test_defaults_untouched_without_actions(self):
        n = self.one("")
        self.assertEqual(n.state, State.SHOWN)
        self.assertIsNone(n.sound)
        self.assertIsNone(n.sound_volume)
        self.assertFalse(n.dnd_bypass)


class SemanticsTests(unittest.TestCase):
    def test_later_scalar_overrides_earlier(self):
        r = rs(
            '[[rule]]\nname = "a"\nsound = "one"\ntimeout = 100\nvolume = 10\n'
            '[[rule]]\nname = "b"\nsound = "two"\ntimeout = "never"\nvolume = 90\n'
        )
        n = r.apply(make())
        self.assertEqual((n.sound, n.timeout_ms, n.sound_volume), ("two", None, 90))

    def test_lists_append_in_order_deduplicated(self):
        r = rs(
            '[[rule]]\nname = "a"\nclass = ["x", "y"]\ntags = ["t1"]\n'
            "highlight = { pattern = 'p', class = 'h' }\n"
            '[[rule]]\nname = "b"\nclass = ["y", "z"]\ntags = ["t1", "t2"]\n'
            "highlight = [{ pattern = 'p', class = 'h' }, { pattern = 'q', class = 'h' }]\n"
        )
        n = r.apply(make(classes=["pre"]))
        self.assertEqual(n.classes, ["pre", "x", "y", "z"])
        self.assertEqual(n.tags, ["t1", "t2"])
        self.assertEqual(len(n.highlights), 2)

    def test_later_state_wins(self):
        r = rs('[[rule]]\nname = "a"\nblock = true\n[[rule]]\nname = "b"\nstore_only = true\n')
        self.assertEqual(r.apply(make()).state, State.STORE_ONLY)

    def test_stop_ends_evaluation(self):
        r = rs(
            '[[rule]]\nname = "a"\nsound = "one"\nstop = true\n'
            '[[rule]]\nname = "b"\nsound = "two"\n'
        )
        n = r.apply(make())
        self.assertEqual(n.sound, "one")
        self.assertEqual(n.matched_rules, ["a"])

    def test_stop_only_when_matched(self):
        r = rs(
            '[[rule]]\nname = "a"\nsummary = "nomatch"\nstop = true\n'
            '[[rule]]\nname = "b"\nsound = "two"\n'
        )
        self.assertEqual(r.apply(make()).sound, "two")

    def test_enabled_false_skipped(self):
        r = rs('[[rule]]\nname = "a"\nenabled = false\nblock = true\n')
        n = r.apply(make())
        self.assertEqual(n.state, State.SHOWN)
        self.assertEqual(n.matched_rules, [])
        self.assertEqual(r.names, ["a"])

    def test_matched_rules_in_order(self):
        r = rs(
            '[[rule]]\nname = "a"\n[[rule]]\nname = "skip"\nsummary = "zzz"\n[[rule]]\nname = "c"\n'
        )
        self.assertEqual(r.apply(make()).matched_rules, ["a", "c"])

    def test_later_rule_sees_earlier_urgency_override(self):
        r = rs(
            '[[rule]]\nname = "a"\nset_urgency = "critical"\n'
            '[[rule]]\nname = "b"\nurgency = "critical"\nclass = "was-crit"\n'
        )
        self.assertEqual(r.apply(make()).classes, ["was-crit"])

    def test_input_not_mutated(self):
        r = rs(
            '[[rule]]\nname = "a"\nblock = true\nsound = "s"\nclass = "c"\ntags = ["t"]\n'
            "highlight = { pattern = 'p', class = 'h' }\ntimeout = 5\n"
            'set_urgency = "critical"\nicon = "i"\ndnd_bypass = true\nvolume = 3\n'
        )
        n = make(hints={"k": [1]})
        before = repr(n)
        out = r.apply(n)
        self.assertEqual(repr(n), before)
        self.assertIsNot(out, n)
        self.assertIsNot(out.hints, n.hints)
        self.assertEqual(out.matched_rules, ["a"])

    def test_empty_ruleset(self):
        n = make()
        out = RuleSet.empty().apply(n)
        self.assertEqual(repr(out), repr(n))
        self.assertEqual(RuleSet.empty().names, [])
        self.assertEqual(rs("").names, [])


class LoadTests(unittest.TestCase):
    def test_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(RuleSet.load(Path(d) / "nope.toml").names, [])

    def test_load_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "rules.toml"
            p.write_text('[[rule]]\nname = "x"\n')
            self.assertEqual(RuleSet.load(p).names, ["x"])

    def test_unreadable_path_raises_rules_error(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RulesError):
                RuleSet.load(Path(d))  # a directory

    def test_bad_utf8(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "rules.toml"
            p.write_bytes(b"\xff\xfe")
            with self.assertRaisesRegex(RulesError, "UTF-8"):
                RuleSet.load(p)


class ValidationTests(unittest.TestCase):
    def bad(self, toml: str, pattern: str):
        with self.assertRaisesRegex(RulesError, pattern):
            RuleSet.from_toml(toml)

    def test_toml_syntax_keeps_position(self):
        self.bad('[[rule]]\nname = "a"\nsound = \n', r"line 3, column")

    def test_missing_name(self):
        self.bad("[[rule]]\nblock = true\n", r"rule #1.*missing required `name`")

    def test_empty_name(self):
        self.bad('[[rule]]\nname = "  "\n', r"non-empty")

    def test_non_string_name(self):
        self.bad("[[rule]]\nname = 3\n", r"non-empty string")

    def test_duplicate_name(self):
        self.bad('[[rule]]\nname = "a"\n\n[[rule]]\nname = "a"\n', r"rule #2 \('a'\), line 4.*dup")

    def test_unknown_key_names_rule_and_line(self):
        self.bad(
            '[[rule]]\nname = "ok"\n\n[[rule]]\nname = "typo"\nsumary = "x"\n',
            r"rule #2 \('typo'\), line 4: unknown key.*sumary",
        )

    def test_unknown_top_level_key(self):
        self.bad("rules = 1\n", r"unknown top-level")

    def test_rule_must_be_array_of_tables(self):
        self.bad('[rule]\nname = "a"\n', r"array of tables")

    def test_unknown_urgency_override_key_urgency_is_match(self):
        self.bad('[[rule]]\nname = "a"\nurgency = "loud"\n', r"one of low, normal, critical")

    def test_type_errors(self):
        cases = {
            'block = "yes"': r"`block` must be true or false",
            "enabled = 1": r"`enabled`",
            "app = 3": r"`app` must be a string",
            "summary = 3": r"`summary` must be a string",
            "category = 3": r"`category`",
            "category = [1]": r"`category`",
            "urgency = 1": r"`urgency`",
            'volume = "loud"': r"`volume` must be an integer",
            "volume = true": r"`volume`",
            "volume = 101": r"0 to 100",
            "volume = -1": r"0 to 100",
            'timeout = "soon"': r"`timeout`",
            "timeout = 0": r"`timeout`",
            "timeout = true": r"`timeout`",
            "timeout = 1.5": r"`timeout`",
            "icon = 1": r"`icon`",
            'sound = ""': r"`sound`",
            "sound = 1": r"`sound`",
            "tags = 'a'": r"`tags` must be a list",
            "class = 5": r"`class`",
            'hint = "x"': r"`hint` must be a table",
            "hint = { a = 1 }": r"`hint.a` must be a string",
            'hint_present = "a"': r"`hint_present` must be a list",
            'match_mode = "some"': r"`match_mode`",
            'set_urgency = ["low", "normal"]': r"`set_urgency`",
            "dnd_bypass = 'y'": r"`dnd_bypass`",
            "stop = 1": r"`stop`",
            "highlight = 'x'": r"`highlight` must be a table",
        }
        for body, pattern in cases.items():
            with self.subTest(body=body):
                self.bad(f'[[rule]]\nname = "a"\n{body}\n', pattern)

    def test_bad_regexes(self):
        for key in ("summary", "body", "app_regex"):
            with self.subTest(key=key):
                self.bad(f'[[rule]]\nname = "a"\n{key} = "("\n', rf"`{key}` is not a valid regex")
        self.bad('[[rule]]\nname = "a"\n[rule.hint]\nk = "("\n', r"`hint.k` is not a valid regex")
        self.bad(
            '[[rule]]\nname = "a"\nhighlight = { pattern = "(", class = "x" }\n',
            r"`highlight.pattern` is not a valid regex",
        )

    def test_highlight_shape(self):
        self.bad('[[rule]]\nname = "a"\nhighlight = { pattern = "x" }\n', r"both `pattern`")
        self.bad(
            '[[rule]]\nname = "a"\nhighlight = { pattern = "x", class = "y", z = 1 }\n',
            r"unknown key.*z",
        )

    def test_css_identifiers(self):
        for body, key in [
            ('class = "9lives"', "class"),
            ('class = "has space"', "class"),
            ('class = ["ok", "-bad"]', "class"),
            ('tags = ["a b"]', "tags"),
            ('tags = ["x.y"]', "tags"),
            ("highlight = { pattern = 'x', class = 'a b' }", "highlight.class"),
        ]:
            with self.subTest(body=body):
                self.bad(f'[[rule]]\nname = "a"\n{body}\n', rf"`{key}` value .*CSS-safe")

    def test_disabled_rules_still_validated(self):
        self.bad('[[rule]]\nname = "a"\nenabled = false\nsumary = "x"\n', r"unknown key")

    def test_error_carries_index_and_line(self):
        try:
            RuleSet.from_toml('[[rule]]\nname = "a"\n\n[[rule]]\nname = "b"\nblock = 1\n')
        except RulesError as e:
            self.assertEqual((e.rule_index, e.line), (1, 4))
        else:
            self.fail("no error")


class JsWarningTests(unittest.TestCase):
    def test_detection(self):
        self.assertEqual(js_incompatibilities(r"(?i)\burgent\b"), [])
        self.assertEqual(js_incompatibilities(r"(?is)x"), [])
        self.assertTrue(js_incompatibilities(r"(?P<n>x)"))
        self.assertTrue(js_incompatibilities(r"\Ax"))
        self.assertTrue(js_incompatibilities(r"x\Z"))
        self.assertTrue(js_incompatibilities(r"a(?i)b"))
        self.assertTrue(js_incompatibilities(r"(?x)a"))
        self.assertTrue(js_incompatibilities(r"(?i:a)"))

    def test_highlight_warns_but_loads(self):
        with self.assertLogs("notifyd.rules", level=logging.WARNING) as cm:
            rs('[[rule]]\nname = "a"\nhighlight = { pattern = "(?P<n>x)", class = "h" }\n')
        self.assertIn("Python-only", cm.output[0])
        self.assertIn("rule #1", cm.output[0])

    def test_match_regex_does_not_warn(self):
        with self.assertNoLogs("notifyd.rules", level=logging.WARNING):
            rs('[[rule]]\nname = "a"\nsummary = "(?P<n>x)"\n')


class ExamplesFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = RuleSet.load(EXAMPLES)

    def test_loads_three_rules(self):
        self.assertEqual(
            self.rules.names, ["example sound", "URGENT stays and glows red", "mute noise"]
        )

    def test_example_sound(self):
        n = self.rules.apply(make(app="Example App"))
        self.assertEqual(n.sound, os.path.expanduser("~/Music/sounds/example.mp3"))
        self.assertEqual(n.matched_rules, ["example sound"])
        self.assertEqual(self.rules.apply(make(app="Other")).sound, None)

    def test_urgent_in_summary_or_body(self):
        for kw in ({"summary": "URGENT: disk"}, {"body": "this is urgent now"}):
            with self.subTest(kw=kw):
                n = self.rules.apply(make(**kw))
                self.assertEqual(n.highlights, [{"pattern": r"(?i)\bURGENT\b", "class": "urgent"}])
                self.assertIsNone(n.timeout_ms)
                self.assertIn("URGENT stays and glows red", n.matched_rules)

    def test_non_urgent_untouched(self):
        n = self.rules.apply(make(summary="Hi", body="there", expire=2000))
        self.assertEqual(n.highlights, [])
        self.assertEqual(n.timeout_ms, 2000)
        self.assertEqual(n.matched_rules, [])

    def test_block(self):
        n = self.rules.apply(make(body="xx regex-to-block xx"))
        self.assertEqual(n.state, State.BLOCKED)

    def test_commented_workflow_examples_load_when_enabled(self):
        text = EXAMPLES.read_text()
        start = text.index("# --- scripts and buttons")
        end = text.index("# --- end scripts and buttons ---")
        lines = []
        for line in text[start:end].splitlines()[1:]:
            if line.startswith("# [") or line.startswith("# name") or "= " in line[:12] + " ":
                lines.append(line[2:])
            elif line.startswith("# ") and "=" in line:
                lines.append(line[2:])
        rules = RuleSet.from_toml(text[:start] + "\n".join(lines))
        self.assertEqual(rules.names[3:], ["log example", "example open button", "snooze button"])
        n = rules.apply(make(app="Example App", hints={"x-nc-url": "https://e.x"}))
        self.assertEqual([r["rule"] for r in n.runs], ["log example"])
        self.assertEqual([b["id"] for b in n.buttons], ["b0", "b1"])
        self.assertEqual(n.buttons[1]["minutes"], 15)


class RunTests(unittest.TestCase):
    def one(self, extra: str, **kw) -> Notification:
        return rs(f'[[rule]]\nname = "r"\n{extra}\n').apply(make(**kw))

    def bad(self, extra: str, *fragments: str):
        with self.assertRaises(RulesError) as cm:
            rs(f'[[rule]]\nname = "r"\n{extra}\n')
        for f in fragments:
            self.assertIn(f, str(cm.exception))

    def test_run_string_defaults(self):
        n = self.one('run = "echo hi"')
        self.assertEqual(
            n.runs,
            [{"rule": "r", "cmd": "echo hi", "when": "arrive", "cooldown": 0, "timeout": 30}],
        )

    def test_run_list_and_options(self):
        n = self.one('run = ["a", "b"]\nrun_when = "shown"\ncooldown = 5\nrun_timeout = 3600')
        self.assertEqual(
            n.runs[0],
            {"rule": "r", "cmd": ["a", "b"], "when": "shown", "cooldown": 5, "timeout": 3600},
        )

    def test_invalid_run(self):
        for v in ('""', '"  "', "[]", '["a", ""]', '["a", 1]', "5", "true"):
            with self.subTest(v=v):
                self.bad(f"run = {v}", "`run`")

    def test_invalid_options(self):
        self.bad('run = "x"\nrun_when = "never"', "run_when")
        self.bad('run = "x"\nrun_when = 1', "run_when")
        self.bad('run = "x"\ncooldown = -1', "cooldown")
        self.bad('run = "x"\ncooldown = true', "cooldown")
        self.bad('run = "x"\nrun_timeout = 0', "run_timeout")
        self.bad('run = "x"\nrun_timeout = 3601', "run_timeout")

    def test_options_need_run(self):
        for k, v in (("run_when", '"shown"'), ("cooldown", "1"), ("run_timeout", "5")):
            with self.subTest(k=k):
                self.bad(f"{k} = {v}", k, "only valid together with `run`", "rule #1 ('r')")

    def test_runs_append_across_rules(self):
        r = rs(
            '[[rule]]\nname = "a"\nrun = "one"\n[[rule]]\nname = "b"\nrun = ["two"]\n'
            '[[rule]]\nname = "c"\napp = "Nope"\nrun = "three"\n'
        )
        n = r.apply(make())
        self.assertEqual([(x["rule"], x["cmd"]) for x in n.runs], [("a", "one"), ("b", ["two"])])

    def test_input_not_mutated(self):
        n = make()
        text = '[[rule]]\nname = "a"\nrun = "x"\n' + btn("L", builtin='"dismiss"')
        rs(text).apply(n)
        self.assertEqual((n.runs, n.buttons), ([], []))

    def test_blocked_still_gets_runs(self):
        n = rs('[[rule]]\nname = "a"\nblock = true\nrun = "x"\nrun_when = "arrive"\n').apply(make())
        self.assertEqual(n.state, State.BLOCKED)
        self.assertEqual(len(n.runs), 1)
        self.assertEqual(n.runs[0]["when"], "arrive")


def btn(label="L", **kw) -> str:
    lines = ["[[rule.buttons]]", f'label = "{label}"']
    lines += [f"{k} = {v}" for k, v in kw.items()]
    return "\n".join(lines) + "\n"


class ButtonTests(unittest.TestCase):
    def rule(self, name: str, *buttons: str, head: str = "") -> str:
        return f'[[rule]]\nname = "{name}"\n{head}\n' + "".join(buttons)

    def load(self, *buttons: str) -> list[dict]:
        return rs(self.rule("r", *buttons)).apply(make()).buttons

    def bad(self, *buttons: str, frag: str = ""):
        with self.assertRaises(RulesError) as cm:
            rs(self.rule("r", *buttons))
        self.assertIn("rule #1 ('r')", str(cm.exception))
        self.assertIn(frag, str(cm.exception))

    def test_run_button_defaults(self):
        (b,) = self.load(btn("Go", run='["a", "b"]'))
        self.assertEqual(
            b,
            {
                "id": "b0",
                "label": "Go",
                "run": ["a", "b"],
                "builtin": None,
                "minutes": None,
                "close": True,
            },
        )

    def test_run_string_button(self):
        (b,) = self.load(btn("Go", run='"echo $NC_ID"'))
        self.assertEqual(b["run"], "echo $NC_ID")

    def test_close_defaults_per_builtin(self):
        expect = {
            "details": False,
            "copy-body": False,
            "copy-summary": False,
            "open-link": False,
            "mark-read": True,
            "dismiss": True,
        }
        for name, close in expect.items():
            with self.subTest(name=name):
                (b,) = self.load(btn("x", builtin=f'"{name}"'))
                self.assertEqual((b["builtin"], b["close"], b["run"]), (name, close, None))
        (b,) = self.load(btn("x", builtin='"snooze"', minutes=5))
        self.assertTrue(b["close"])

    def test_close_override(self):
        (b,) = self.load(btn("x", builtin='"details"', close="true"))
        self.assertTrue(b["close"])
        (b,) = self.load(btn("x", run='"y"', close="false"))
        self.assertFalse(b["close"])
        self.bad(btn("x", run='"y"', close='"yes"'), frag="buttons[0].close")

    def test_snooze_minutes(self):
        for m in (1, 10080):
            (b,) = self.load(btn("z", builtin='"snooze"', minutes=m))
            self.assertEqual(b["minutes"], m)
        for m in (0, 10081, "true", '"5"'):
            with self.subTest(m=m):
                self.bad(btn("z", builtin='"snooze"', minutes=m), frag="buttons[0].minutes")
        self.bad(btn("z", builtin='"snooze"'), frag="needs `minutes`")

    def test_minutes_forbidden_otherwise(self):
        self.bad(btn("z", builtin='"dismiss"', minutes=5), frag="only valid")
        self.bad(btn("z", run='"x"', minutes=5), frag="only valid")

    def test_exactly_one_of_run_builtin(self):
        self.bad(btn("z"), frag="exactly one of")
        self.bad(btn("z", run='"x"', builtin='"dismiss"'), frag="exactly one of")

    def test_bad_builtin(self):
        self.bad(btn("z", builtin='"explode"'), frag="builtin")

    def test_bad_run(self):
        self.bad(btn("z", run="[]"), frag="buttons[0].run")
        self.bad(btn("z", run='""'), frag="buttons[0].run")

    def test_label_rules(self):
        self.bad(btn("", builtin='"dismiss"'), frag="buttons[0].label")
        self.bad(btn("x" * 41, builtin='"dismiss"'), frag="40")
        self.assertEqual(len(self.load(btn("x" * 40, builtin='"dismiss"'))), 1)
        self.bad('[[rule.buttons]]\nbuiltin = "dismiss"\n', frag="missing `label`")

    def test_unknown_key_names_index(self):
        self.bad(
            btn("a", builtin='"dismiss"'),
            btn("b", builtin='"dismiss"', colour='"red"'),
            frag="buttons[1]",
        )

    def test_buttons_shape(self):
        for v in ("5", '"x"', "[]", "[1]"):
            with self.subTest(v=v):
                with self.assertRaises(RulesError):
                    rs(f'[[rule]]\nname = "r"\nbuttons = {v}\n')

    def test_inline_array_form(self):
        r = rs('[[rule]]\nname = "r"\nbuttons = [{label = "D", builtin = "details"}]\n')
        self.assertEqual(r.apply(make()).buttons[0]["builtin"], "details")

    def test_order_ids_and_dedupe(self):
        text = (
            self.rule("a", btn("One", builtin='"dismiss"'), btn("Two", run='["x"]'))
            + self.rule(
                "b",
                btn("Two", run='["x"]'),
                btn("Three", builtin='"details"'),
                btn("One", builtin='"dismiss"'),
            )
            + self.rule("c", btn("Two", run='"x"'))  # string vs list: distinct
        )
        bs = rs(text).apply(make()).buttons
        self.assertEqual(
            [(b["id"], b["label"]) for b in bs],
            [("b0", "One"), ("b1", "Two"), ("b2", "Three"), ("b3", "Two")],
        )

    def test_dedupe_distinguishes_minutes(self):
        bs = self.load(
            btn("S", builtin='"snooze"', minutes=5), btn("S", builtin='"snooze"', minutes=10)
        )
        self.assertEqual(len(bs), 2)

    def test_cap_of_six_with_warning(self):
        text = self.rule("a", *[btn(f"a{i}", builtin='"dismiss"') for i in range(4)]) + self.rule(
            "b", *[btn(f"b{i}", builtin='"dismiss"') for i in range(4)]
        )
        with self.assertLogs("notifyd.rules", level=logging.WARNING) as cm:
            bs = rs(text).apply(make()).buttons
        self.assertEqual([b["label"] for b in bs], ["a0", "a1", "a2", "a3", "b0", "b1"])
        self.assertEqual(bs[-1]["id"], "b5")
        self.assertIn("b", cm.output[0].split("rule(s):")[1])
        self.assertNotIn("a", cm.output[0].split("rule(s):")[1].replace("rule", ""))

    def test_six_exactly_no_warning(self):
        text = self.rule("a", *[btn(f"a{i}", builtin='"dismiss"') for i in range(6)])
        with self.assertNoLogs("notifyd.rules", level=logging.WARNING):
            self.assertEqual(len(rs(text).apply(make()).buttons), 6)

    def test_stop_and_disabled_rules_add_nothing(self):
        text = (
            self.rule("a", btn("A", builtin='"dismiss"'), head="stop = true")
            + self.rule("b", btn("B", builtin='"dismiss"'))
            + self.rule("c", btn("C", builtin='"dismiss"'), head="enabled = false")
        )
        self.assertEqual([b["label"] for b in rs(text).apply(make()).buttons], ["A"])

    def test_blocked_still_gets_buttons(self):
        n = rs(self.rule("a", btn("A", builtin='"dismiss"'), head="block = true")).apply(make())
        self.assertEqual(n.state, State.BLOCKED)
        self.assertEqual(len(n.buttons), 1)

    def test_to_web_exposes_no_commands(self):
        text = self.rule(
            "a",
            btn("Do", run='["sh", "-c", "SECRET_CMD"]'),
            btn("Str", run='"SECRET_STR"'),
            btn("Snz", builtin='"snooze"', minutes=15),
            btn("Det", builtin='"details"'),
            head='run = "SECRET_RUN"',
        )
        n = rs(text).apply(make())
        w = to_web(n, [])
        self.assertEqual(
            w["buttons"],
            [
                {"id": "b0", "label": "Do", "kind": "run", "close": True},
                {"id": "b1", "label": "Str", "kind": "run", "close": True},
                {
                    "id": "b2",
                    "label": "Snz",
                    "kind": "builtin",
                    "builtin": "snooze",
                    "minutes": 15,
                    "close": True,
                },
                {
                    "id": "b3",
                    "label": "Det",
                    "kind": "builtin",
                    "builtin": "details",
                    "close": False,
                },
            ],
        )
        self.assertNotIn("SECRET", repr(w))

    def test_app_settings_ruleset_still_works(self):
        from notifyd.app_settings import AppSettings

        self.assertIsInstance(AppSettings().to_ruleset(), RuleSet)


if __name__ == "__main__":
    unittest.main()
