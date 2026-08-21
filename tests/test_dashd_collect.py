"""Unit tests for the standalone metrics collector (bin/dashd-collect).

Deliberately hermetic, same precedent as test_dashd.py: sysinfo.sample_stats
is monkeypatched rather than trusted to read real hardware, so these don't
depend on this machine having a battery/thermal sensor.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BIN = Path(__file__).resolve().parent.parent / "bin" / "dashd-collect"

# bin/dashd-collect has no .py suffix, so it needs an explicit source loader.
_loader = importlib.machinery.SourceFileLoader("dashd_collect", str(BIN))
_spec = importlib.util.spec_from_loader("dashd_collect", _loader)
collect = importlib.util.module_from_spec(_spec)
_loader.exec_module(collect)


class TestCollectOnce(unittest.TestCase):
    """collect_once() is main()'s loop body pulled out so it's testable
    without sleeping -- see #14."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_data = collect.DATA
        collect.DATA = Path(self._tmp.name)

    def tearDown(self):
        collect.DATA = self._orig_data
        self._tmp.cleanup()

    @staticmethod
    def _sample(**overrides):
        base = {"cpu_pct": 10.0, "cpu_temp_c": 50, "cpu_freq_mhz": 2600,
                "mem_pct": 33.0, "battery_pct": 77, "net_down": 1.0, "net_up": 2.0}
        return {**base, **overrides}

    def test_writes_a_sample_row(self):
        with patch.object(collect.sysinfo, "sample_stats",
                           return_value=self._sample()):
            collect.collect_once({"refresh": {"metrics_retain_hours": 24}})
        rows = collect.metrics.query_history(
            collect.DATA / "metrics.db", since_seconds=3600)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["cpu_pct"], 10.0)
        self.assertAlmostEqual(rows[0]["mem_pct"], 33.0)
        self.assertAlmostEqual(rows[0]["battery_pct"], 77)

    def test_no_battery_stores_null_battery_pct(self):
        with patch.object(collect.sysinfo, "sample_stats",
                           return_value=self._sample(battery_pct=None)):
            collect.collect_once({"refresh": {"metrics_retain_hours": 24}})
        rows = collect.metrics.query_history(
            collect.DATA / "metrics.db", since_seconds=3600)
        self.assertIsNone(rows[0]["battery_pct"])

    def test_every_call_writes_regardless_of_interval(self):
        """No piggyback-poll throttle here (unlike the old
        dashd-serve.maybe_sample_metrics) -- pacing is main()'s sleep loop's
        job, not collect_once()'s."""
        cfg = {"refresh": {"metrics_retain_hours": 24}}
        with patch.object(collect.sysinfo, "sample_stats",
                           return_value=self._sample()):
            collect.collect_once(cfg)
            collect.collect_once(cfg)
        rows = collect.metrics.query_history(
            collect.DATA / "metrics.db", since_seconds=3600)
        self.assertEqual(len(rows), 2, rows)

    def test_missing_retain_hours_defaults_to_24(self):
        with patch.object(collect.sysinfo, "sample_stats",
                           return_value=self._sample()):
            collect.collect_once({})  # no "refresh" key at all
        rows = collect.metrics.query_history(
            collect.DATA / "metrics.db", since_seconds=3600)
        self.assertEqual(len(rows), 1)


class TestSampleStatsShape(unittest.TestCase):
    """sysinfo.sample_stats() must match lib/metrics.COLUMNS exactly --
    insert_sample() silently drops any key that isn't a real column and
    stores NULL for any column sample_stats() forgot to include."""

    def test_keys_match_metrics_columns(self):
        sample = collect.sysinfo.sample_stats()
        self.assertEqual(set(sample.keys()), set(collect.metrics.COLUMNS))


if __name__ == "__main__":
    unittest.main()
