"""Unit tests for bin/dashd-control (#30).

Same hermetic philosophy as test_dashd.py: no network, no real HTTP
server -- the auth/whitelist logic is pulled into plain functions
(check_token, filter_patch) precisely so it's testable without one.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin" / "dashd-control"

_loader = importlib.machinery.SourceFileLoader("dashd_control", str(BIN))
_spec = importlib.util.spec_from_loader("dashd_control", _loader)
ctrl = importlib.util.module_from_spec(_spec)
_loader.exec_module(ctrl)


class TestCheckToken(unittest.TestCase):
    def test_matching_token_is_authed(self):
        self.assertTrue(ctrl.check_token("secret", "secret"))

    def test_wrong_token_is_rejected(self):
        self.assertFalse(ctrl.check_token("secret", "nope"))

    def test_empty_supplied_is_rejected(self):
        self.assertFalse(ctrl.check_token("secret", ""))


class TestFilterPatch(unittest.TestCase):
    """#74: the patch is filtered, then persisted as overrides to
    data/config.local.json -- so what matters is which fields survive the
    whitelist, not what a config dict looks like afterwards."""

    def test_editable_field_is_accepted(self):
        accepted, rejected = ctrl.filter_patch(
            {"units": {"temperature": "celsius"}})
        self.assertEqual(accepted, {"units": {"temperature": "celsius"}})
        self.assertEqual(rejected, [])

    def test_unknown_section_is_rejected_and_not_accepted(self):
        accepted, rejected = ctrl.filter_patch({"server": {"host": "0.0.0.0"}})
        self.assertEqual(accepted, {})
        self.assertEqual(rejected, ["server"])

    def test_unknown_key_within_editable_section_is_rejected(self):
        accepted, rejected = ctrl.filter_patch({"units": {"bogus": "x"}})
        self.assertEqual(accepted, {})
        self.assertEqual(rejected, ["units.bogus"])

    def test_logs_section_is_editable(self):
        accepted, rejected = ctrl.filter_patch(
            {"logs": {"journalctl_unit": "sshd"}})
        self.assertEqual(rejected, [])
        self.assertEqual(accepted["logs"]["journalctl_unit"], "sshd")

    def test_non_dict_patch_is_wholly_rejected(self):
        accepted, rejected = ctrl.filter_patch("not a dict")
        self.assertEqual(rejected, ["<patch body>"])
        self.assertEqual(accepted, {})

    def test_a_partly_valid_patch_keeps_only_the_valid_half(self):
        accepted, rejected = ctrl.filter_patch(
            {"units": {"clock24": False, "bogus": 1}, "server": {"port": 1}})
        self.assertEqual(accepted, {"units": {"clock24": False}})
        self.assertEqual(sorted(rejected), ["server", "units.bogus"])


class TestPairingUrl(unittest.TestCase):
    def test_url_contains_port_and_token(self):
        cfg = {"control": {"port": 4321}}
        url = ctrl.pairing_url(cfg, "tok123")
        self.assertIn(":4321", url)
        self.assertIn("t=tok123", url)


if __name__ == "__main__":
    unittest.main()
