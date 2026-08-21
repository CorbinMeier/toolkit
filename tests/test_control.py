"""Unit tests for bin/dashd-control (#30).

Same hermetic philosophy as test_dashd.py: no network, no real HTTP
server -- the auth/whitelist logic is pulled into plain functions
(check_token, merge_patch) precisely so it's testable without one.
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


class TestMergePatch(unittest.TestCase):
    def test_editable_field_is_applied(self):
        cfg = {"units": {"temperature": "fahrenheit"}}
        rejected = ctrl.merge_patch(cfg, {"units": {"temperature": "celsius"}})
        self.assertEqual(cfg["units"]["temperature"], "celsius")
        self.assertEqual(rejected, [])

    def test_unknown_section_is_rejected_and_untouched(self):
        cfg = {"server": {"host": "127.0.0.1"}}
        rejected = ctrl.merge_patch(cfg, {"server": {"host": "0.0.0.0"}})
        self.assertEqual(cfg["server"]["host"], "127.0.0.1")
        self.assertEqual(rejected, ["server"])

    def test_unknown_key_within_editable_section_is_rejected(self):
        cfg = {"units": {}}
        rejected = ctrl.merge_patch(cfg, {"units": {"bogus": "x"}})
        self.assertNotIn("bogus", cfg["units"])
        self.assertEqual(rejected, ["units.bogus"])

    def test_devices_and_logs_sections_are_editable(self):
        cfg = {}
        rejected = ctrl.merge_patch(cfg, {
            "devices": {"scan_interval_seconds": 60, "scan_target": "10.0.0.0/24"},
            "logs": {"journalctl_unit": "sshd"},
        })
        self.assertEqual(rejected, [])
        self.assertEqual(cfg["devices"]["scan_interval_seconds"], 60)
        self.assertEqual(cfg["logs"]["journalctl_unit"], "sshd")

    def test_non_dict_patch_is_wholly_rejected(self):
        cfg = {}
        rejected = ctrl.merge_patch(cfg, "not a dict")
        self.assertEqual(rejected, ["<patch body>"])
        self.assertEqual(cfg, {})


class TestPairingUrl(unittest.TestCase):
    def test_url_contains_port_and_token(self):
        cfg = {"control": {"port": 4321}}
        url = ctrl.pairing_url(cfg, "tok123")
        self.assertIn(":4321", url)
        self.assertIn("t=tok123", url)


if __name__ == "__main__":
    unittest.main()
