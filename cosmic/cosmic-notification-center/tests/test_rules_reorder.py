"""Rules tab reordering, headless: the page's keyboard move (ArrowUp / ArrowDown / Home / End on a
row handle) and its drag-drop both end in one `rules_move {index, to, expect}` message. These tests
send exactly that through a real `CenterBridge` into the real `SettingsService` on a temp config
dir, and check rules.toml on disk.

Not covered here: the drag gesture and how rows look. The key -> target, drop arithmetic and
focus rules are in tests/test_rules_move_js.py.
"""

import json
import re
import tomllib
import unittest

from notifyd.center import CenterBridge
from notifyd.rules import RuleSet
from notifyd.store import Store
from tests.test_rules_edit import USER
from tests.test_settings import Base

STALE = "rules.toml changed since the list was loaded"


class ReorderCase(Base):
    def setUp(self):
        super().setUp()
        self.path = self.dir / "rules.toml"
        self.path.write_text(USER, encoding="utf-8")
        self.js: list[str] = []
        self.bridge = CenterBridge(
            Store(":memory:"),
            on_invoke_action=lambda i, k: True,
            on_dismiss=lambda i: None,
            on_mark_read=lambda ids: None,
            on_delete=lambda ids: None,
            on_clear=lambda: None,
            run_js=self.js.append,
            copy_text=lambda t: None,
            open_link=lambda h: None,
            settings=self.svc,
        )
        self.req = 0
        self.listed = self.ask(type="rules_get")["rules"]  # what the page holds

    def ask(self, **msg):
        self.req += 1
        self.bridge.handle({"req": self.req, **msg})
        m = re.fullmatch(r"window\.nc\.reply\((\d+), (.*)\)", self.js[-1], re.S)
        assert m and int(m[1]) == self.req, self.js[-1]
        return json.loads(m[2])

    def move(self, index, to, expect="auto"):
        """What the page sends: the name it listed at `index` as `expect`."""
        if expect == "auto":
            expect = self.listed[index]["name"]
        res = self.ask(type="rules_move", index=index, to=to, expect=expect)
        if res["ok"]:
            self.listed = res["rules"]  # the reply carries the new list
        return res

    def names(self):
        return [r["name"] for r in tomllib.loads(self.path.read_text(encoding="utf-8"))["rule"]]

    def disk(self):
        return self.path.read_text(encoding="utf-8")


ORDER = ["example sound", "ci failed", "mute noise ☾", "last, no trailing newline"]


class Moves(ReorderCase):
    def test_listing_starts_in_file_order(self):
        self.assertEqual([r["name"] for r in self.listed], ORDER)
        self.assertEqual(self.names(), ORDER)

    def test_move_up_one(self):  # ArrowUp on the second row
        res = self.move(1, 0)
        self.assertTrue(res["ok"])
        self.assertEqual([r["name"] for r in res["rules"]], [ORDER[1], ORDER[0], *ORDER[2:]])
        self.assertEqual(self.names(), [ORDER[1], ORDER[0], *ORDER[2:]])

    def test_move_down_one(self):  # ArrowDown on the first row
        self.assertTrue(self.move(0, 1)["ok"])
        self.assertEqual(self.names(), [ORDER[1], ORDER[0], *ORDER[2:]])

    def test_move_to_first_from_last(self):  # Home on the last row
        self.assertTrue(self.move(3, 0)["ok"])
        self.assertEqual(self.names(), [ORDER[3], *ORDER[:3]])

    def test_move_to_last_from_first(self):  # End on the first row
        self.assertTrue(self.move(0, 3)["ok"])
        self.assertEqual(self.names(), [*ORDER[1:], ORDER[0]])

    def test_move_to_the_same_place_changes_nothing(self):
        before = self.disk()
        self.assertTrue(self.move(2, 2)["ok"])
        self.assertEqual(self.disk(), before)

    def test_edges_are_refused_not_wrapped(self):
        before = self.disk()
        for index, to in ((0, 4), (3, 4), (0, -1), (4, 0), (0, 10000)):
            res = self.ask(type="rules_move", index=index, to=to, expect=None)
            self.assertFalse(res["ok"], (index, to))
        self.assertEqual(self.disk(), before)

    def test_key_presses_in_a_row_follow_the_reply_each_time(self):
        # the page re-lists after every move, so a second ArrowDown uses the new index and name
        self.assertTrue(self.move(0, 1)["ok"])
        self.assertTrue(self.move(1, 2)["ok"])  # the same rule, one step further
        self.assertEqual(self.names(), [ORDER[1], ORDER[2], ORDER[0], ORDER[3]])
        self.assertTrue(self.move(2, 3)["ok"])
        self.assertEqual(self.names(), [ORDER[1], ORDER[2], ORDER[3], ORDER[0]])
        self.assertTrue(self.move(3, 0)["ok"])
        self.assertEqual(self.names(), ORDER[:1] + [ORDER[1], ORDER[2], ORDER[3]])

    def test_the_rule_set_the_daemon_loads_matches(self):
        self.move(3, 0)
        self.assertEqual(RuleSet.from_toml(self.disk()).names, self.names())

    def test_a_round_trip_restores_the_file(self):
        original = self.disk()
        self.move(0, 3)
        self.move(3, 0)
        self.assertEqual(tomllib.loads(self.disk())["rule"], tomllib.loads(original)["rule"])
        self.assertEqual(self.disk().count("\n\n\n"), 0)


class StaleList(ReorderCase):
    def test_a_stale_expect_is_refused_and_nothing_changes(self):
        self.move(0, 3)  # now another editor moved things (this page's list is stale)
        before = self.disk()
        res = self.ask(type="rules_move", index=0, to=1, expect=ORDER[0])  # old name at index 0
        self.assertFalse(res["ok"])
        self.assertIn(STALE, res["errors"][""])
        self.assertEqual(self.disk(), before)

    def test_expect_naming_a_deleted_rule_is_refused(self):
        self.svc.rules_delete(1)
        before = self.disk()
        res = self.ask(type="rules_move", index=3, to=0, expect=ORDER[3])  # index now out of range
        self.assertFalse(res["ok"])
        self.assertIn(STALE, res["errors"][""])
        self.assertEqual(self.disk(), before)

    def test_without_expect_the_move_is_applied_blindly(self):
        # `expect` is optional on the wire; the page always sends it
        self.assertTrue(self.ask(type="rules_move", index=0, to=1)["ok"])

    def test_bad_expect_types_are_refused(self):
        before = self.disk()
        for bad in (5, ["a"], "x" * 513, True):
            res = self.ask(type="rules_move", index=0, to=1, expect=bad)
            self.assertFalse(res["ok"], bad)
        self.assertEqual(self.disk(), before)


class Comments(ReorderCase):
    def test_comments_travel_with_their_rule(self):
        self.assertTrue(self.move(1, 0)["ok"])  # "ci failed" above "example sound"
        text = self.disk()
        ci = text.index("# CI failures: loud")
        example = text.index("# Example gets its own sound.")
        self.assertLess(ci, example)
        self.assertLess(ci, text.index('name = "ci failed"'))
        self.assertLess(text.index('name = "ci failed"'), example)
        self.assertLess(example, text.index('name = "example sound"'))
        # inline comments stay on their lines
        self.assertIn('app = "Example App"        # exact, case-insensitive', text)
        self.assertIn("# open the build page", text)
        self.assertIn("# --- my notification rules ---", text.split("# CI failures")[0])

    def test_a_trailing_section_comment_moves_with_the_block_before_it(self):
        self.assertTrue(self.move(1, 3)["ok"])  # "ci failed" to the end, with its tail
        text = self.disk()
        self.assertGreater(
            text.index("# ----- section: quiet ones -----"), text.index('name = "last')
        )
        self.assertGreater(
            text.index("# (commented-out rule kept for later)"), text.index('name = "last')
        )
        self.assertEqual(self.names()[-1], "ci failed")

    def test_the_header_stays_on_top(self):
        for index, to in ((3, 0), (0, 3), (2, 0)):
            self.assertTrue(self.move(index, to)["ok"])
            self.assertTrue(self.disk().startswith("# --- my notification rules ---\n"))

    def test_every_comment_survives_every_single_move(self):
        comments = sorted(line for line in USER.splitlines() if line.lstrip().startswith("#"))
        for index in range(4):
            for to in range(4):
                self.path.write_text(USER, encoding="utf-8")
                self.listed = self.ask(type="rules_get")["rules"]
                self.assertTrue(self.move(index, to)["ok"], (index, to))
                got = sorted(
                    line for line in self.disk().splitlines() if line.lstrip().startswith("#")
                )
                self.assertEqual(got, comments, (index, to))
                self.assertEqual(sorted(self.names()), sorted(ORDER), (index, to))


if __name__ == "__main__":
    unittest.main()
