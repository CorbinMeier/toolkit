"""SettingsService rules_*: edits of rules.toml on temp files, validation, the dry-run test."""

import tomllib
import unittest
from unittest import mock

from notifyd.rules import RuleSet
from notifyd.settings import SettingsError
from tests.test_rules_edit import USER
from tests.test_settings import Base


class RulesBase(Base):
    def setUp(self):
        super().setUp()
        self.path = self.dir / "rules.toml"

    def seed(self, text=USER):
        self.path.write_text(text, encoding="utf-8")

    def disk(self):
        return self.path.read_text(encoding="utf-8")

    def names(self):
        return [r["name"] for r in tomllib.loads(self.disk())["rule"]]


class GetTests(RulesBase):
    def test_missing_file_is_an_empty_list(self):
        res = self.svc.rules_get()
        self.assertEqual(res["rules"], [])
        self.assertIsNone(res["error"])
        self.assertTrue(res["editable"])
        self.assertEqual(res["path"], str(self.path))

    def test_lists_rules_with_summaries(self):
        self.seed()
        res = self.svc.rules_get()
        self.assertEqual([r["name"] for r in res["rules"]][:2], ["example sound", "ci failed"])
        self.assertTrue(res["rules"][0]["summary"])
        self.assertIsNone(res["error"])

    def test_a_file_with_a_validation_error_still_lists_and_reports_it(self):
        self.seed('[[rule]]\nname = "a"\nvolume = 500\n')
        res = self.svc.rules_get()
        self.assertEqual(len(res["rules"]), 1)
        self.assertIn("volume", res["error"])
        self.assertIn("line 1", res["error"])
        self.assertTrue(res["editable"])

    def test_broken_toml_is_not_editable(self):
        self.seed("[[rule]\n")
        res = self.svc.rules_get()
        self.assertEqual(res["rules"], [])
        self.assertFalse(res["editable"])
        self.assertIn("not valid TOML", res["error"])
        self.assertEqual(self.svc.rules_get_source()["text"], "[[rule]\n")


class WriteTests(RulesBase):
    def test_update_writes_valid_toml_the_daemon_reloads(self):
        self.seed()
        res = self.svc.rules_update(0, {"name": "example sound", "app": "Example App", "volume": 20})
        self.assertEqual(res["rules"][0]["data"]["volume"], 20)
        self.assertIn("volume = 20", self.disk())
        self.assertIn("# Example gets its own sound.", self.disk())
        self.runtime.reload_rules()  # what the file monitor does
        self.assertIsNone(self.runtime.rules_error)
        self.assertEqual(self.runtime.rules.names, self.names())

    def test_add_delete_move_enable(self):
        self.seed()
        self.svc.rules_add({"name": "extra", "app": "X"}, 1)
        self.assertEqual(self.names()[1], "extra")
        self.svc.rules_move(1, 3)
        self.assertEqual(self.names()[3], "extra")
        res = self.svc.rules_set_enabled(3, False)
        self.assertFalse(res["rules"][3]["enabled"])
        self.svc.rules_delete(3)
        self.assertEqual(len(self.names()), 4)
        self.assertNotIn("extra", self.names())
        self.assertEqual(RuleSet.load(self.path).names, self.names())

    def test_add_creates_the_file(self):
        res = self.svc.rules_add({"name": "first"})
        self.assertEqual([r["name"] for r in res["rules"]], ["first"])
        self.assertEqual(self.disk(), '[[rule]]\nname = "first"\n')

    def test_invalid_edits_raise_with_field_paths_and_write_nothing(self):
        self.seed()
        before = self.disk()
        with self.assertRaises(SettingsError) as cm:
            self.svc.rules_update(0, {"name": "example sound", "app_regex": "(["})
        self.assertEqual(list(cm.exception.errors), ["app_regex"])
        with self.assertRaises(SettingsError) as cm:
            self.svc.rules_add({"name": "example sound"})
        self.assertEqual(list(cm.exception.errors), ["name"])
        with self.assertRaises(SettingsError):
            self.svc.rules_move(0, 99)
        with self.assertRaises(SettingsError):
            self.svc.rules_delete(-1)
        self.assertEqual(self.disk(), before)
        self.assertEqual([p.name for p in self.dir.glob(".rules*")], [])

    def test_unchanged_edit_does_not_rewrite_the_file(self):
        self.seed()
        mtime = self.path.stat().st_mtime_ns
        data = self.svc.rules_get()["rules"][0]["data"]
        self.svc.rules_update(0, data)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)

    def test_expect_guards_against_a_stale_list(self):
        self.seed()
        before = self.disk()
        with self.assertRaises(SettingsError) as cm:
            self.svc.rules_delete(0, expect="ci failed")
        self.assertIn("changed", cm.exception.errors[""])
        with self.assertRaises(SettingsError):
            self.svc.rules_update(9, {"name": "x"}, expect="x")
        self.assertEqual(self.disk(), before)
        self.svc.rules_set_enabled(0, False, expect="example sound")
        self.assertFalse(tomllib.loads(self.disk())["rule"][0]["enabled"])


class SourceTests(RulesBase):
    def test_get_and_save_source(self):
        self.assertEqual(self.svc.rules_get_source()["text"], "")
        res = self.svc.rules_save_source(USER)
        self.assertEqual(self.disk(), USER)
        self.assertEqual(len(res["rules"]), 4)

    def test_bad_source_is_refused_with_line_info(self):
        self.seed()
        before = self.disk()
        with self.assertRaises(SettingsError) as cm:
            self.svc.rules_save_source('[[rule]]\nname = "a"\nsumary = "x"\n')
        self.assertEqual(list(cm.exception.errors), ["source"])
        self.assertIn("line 1", cm.exception.errors["source"])
        with self.assertRaises(SettingsError) as cm:
            self.svc.rules_save_source("[[rule]\n")
        self.assertIn("line 1", cm.exception.errors["source"])
        for bad in (None, 5, "x" * (256 * 1024 + 1), "\ud800"):
            with self.assertRaises(SettingsError):
                self.svc.rules_save_source(bad)
        self.assertEqual(self.disk(), before)


SAMPLE = {
    "app": "Example App",
    "summary": "URGENT build",
    "body": "x",
    "urgency": "normal",
    "category": "",
    "hints": {"x-status": "fail"},
}


class TestTests(RulesBase):
    def test_match_and_result(self):
        self.seed()
        res = self.svc.rules_test(
            {"name": "t", "app": "Example App", "sound": "bell", "class": ["hot"]}, SAMPLE
        )
        self.assertTrue(res["matched"])
        alone, full = res["result"]["rule"], res["result"]["full"]
        self.assertEqual(alone["sound"], "bell")
        self.assertEqual(alone["classes"], ["hot"])
        self.assertEqual(alone["matched_rules"], ["t"])
        self.assertEqual(alone["state"], "shown")
        self.assertEqual(alone["urgency"], "normal")
        self.assertIn("example sound", full["matched_rules"])  # the saved file ran as well
        self.assertEqual(full["matched_rules"][-1], "t")
        self.assertTrue(full["rule_matched"])
        self.assertEqual(full["sound"], "bell")  # the draft is later, so it wins

    def test_no_match(self):
        res = self.svc.rules_test({"name": "t", "app": "Nobody"}, SAMPLE)
        self.assertFalse(res["matched"])
        self.assertEqual(res["result"]["rule"]["matched_rules"], [])

    def test_the_draft_replaces_the_saved_rule_at_index(self):
        self.seed()
        res = self.svc.rules_test(
            {"name": "example sound", "app": "Example App", "sound": "alarm"}, SAMPLE, 0
        )
        self.assertEqual(res["result"]["full"]["sound"], "alarm")
        self.assertEqual(res["result"]["full"]["matched_rules"].count("example sound"), 1)

    def test_actions_show_in_the_result(self):
        res = self.svc.rules_test(
            {
                "name": "t",
                "block": True,
                "set_urgency": "critical",
                "timeout": "never",
                "volume": 30,
                "tags": ["a"],
                "highlight": {"pattern": "URGENT", "class": "u"},
                "run": "echo hi",
                "run_when": "shown",
                "buttons": [{"label": "S", "builtin": "snooze", "minutes": 5}],
                "dnd_bypass": True,
            },
            SAMPLE,
        )
        r = res["result"]["rule"]
        self.assertEqual(r["state"], "blocked")
        self.assertEqual(r["urgency"], "critical")
        self.assertIsNone(r["timeout_ms"])
        self.assertEqual(r["volume"], 30)
        self.assertEqual(r["tags"], ["a"])
        self.assertEqual(r["highlights"], [{"pattern": "URGENT", "class": "u"}])
        self.assertEqual(r["runs"], [{"rule": "t", "when": "shown", "cooldown": 0}])
        self.assertNotIn("echo", str(r))  # the command is never part of the preview
        self.assertEqual(r["buttons"][0]["id"], "b0")
        self.assertTrue(r["dnd_bypass"])

    def test_invalid_draft_and_sample_raise_field_errors(self):
        with self.assertRaises(SettingsError) as cm:
            self.svc.rules_test({"name": "t", "summary": "(["}, SAMPLE)
        self.assertEqual(list(cm.exception.errors), ["summary"])
        for bad in (
            {**SAMPLE, "urgency": "loud"},
            {**SAMPLE, "app": "x" * 5000},
            {**SAMPLE, "hints": {"k": [1]}},
            {**SAMPLE, "hints": {str(i): 1 for i in range(60)}},
            "text",
        ):
            with self.assertRaises(SettingsError):
                self.svc.rules_test({"name": "t"}, bad)

    def test_a_broken_rest_of_the_file_only_blanks_the_full_result(self):
        self.seed('[[rule]]\nname = "a"\nvolume = 500\n')
        res = self.svc.rules_test({"name": "t"}, SAMPLE)
        self.assertTrue(res["matched"])
        self.assertIsNone(res["result"]["full"])
        self.assertIn("rule #1", res["result"]["full_error"])

    def test_it_has_no_side_effects(self):
        self.seed()
        raw = self.path.read_bytes()
        mtime = self.path.stat().st_mtime_ns
        rules_before, names_before = self.runtime.rules, sorted(p.name for p in self.dir.iterdir())
        banned = mock.Mock(side_effect=AssertionError("must not run"))
        with (
            mock.patch("subprocess.Popen", banned),
            mock.patch("subprocess.run", banned),
            mock.patch("os.system", banned),
        ):
            self.svc.rules_test(
                {
                    "name": "t",
                    "run": "touch /tmp/should-never-exist",
                    "sound": "bell",
                    "buttons": [{"label": "x", "run": "false"}],
                },
                SAMPLE,
                0,
            )
        banned.assert_not_called()
        self.assertEqual(self.player.played, [])
        self.assertEqual(self.display.shown, [])
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)
        self.assertIs(self.runtime.rules, rules_before)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), names_before)


if __name__ == "__main__":
    unittest.main()
