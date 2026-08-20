"""Unit tests for the dashboard data server.

Run: python3 -m unittest discover -s tests -v

Deliberately hermetic -- nothing here hits the network. The weather cache
path is exercised by writing a fake cache file, so the stale-fallback
behaviour is provable without depending on Open-Meteo being reachable.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin" / "dashd-serve"

# bin/dashd-serve has no .py suffix, so it needs an explicit source loader.
_loader = importlib.machinery.SourceFileLoader("dashd_serve", str(BIN))
_spec = importlib.util.spec_from_loader("dashd_serve", _loader)
dashd = importlib.util.module_from_spec(_spec)
_loader.exec_module(dashd)


class TestMoonPhase(unittest.TestCase):
    def test_known_new_moon_is_new(self):
        m = dashd.moon_phase(datetime(2000, 1, 6, 18, 14, tzinfo=UTC))
        self.assertEqual(m["name"], "New Moon")
        self.assertLess(m["illumination"], 0.01)

    def test_half_synodic_later_is_full(self):
        # 14.77 days after a new moon is a full moon.
        when = datetime(2000, 1, 21, 8, 0, tzinfo=UTC)
        m = dashd.moon_phase(when)
        self.assertEqual(m["name"], "Full Moon")
        self.assertGreater(m["illumination"], 0.98)

    def test_phase_and_illumination_stay_in_range(self):
        for day in range(0, 400, 7):
            when = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=day)
            m = dashd.moon_phase(when)
            self.assertTrue(0.0 <= m["phase"] <= 1.0, m)
            self.assertTrue(0.0 <= m["illumination"] <= 1.0, m)
            self.assertTrue(0.0 <= m["age_days"] < 29.6, m)

    def test_illumination_is_symmetric_about_full(self):
        base = datetime(2000, 1, 6, 18, 14, tzinfo=UTC)
        waxing = dashd.moon_phase(base + timedelta(days=7.38))
        waning = dashd.moon_phase(base + timedelta(days=22.15))
        self.assertAlmostEqual(
            waxing["illumination"], waning["illumination"], places=2)


class TestWeatherCodes(unittest.TestCase):
    def test_every_code_maps_to_desc_and_icon(self):
        for code, (desc, icon) in dashd.WMO.items():
            self.assertIsInstance(code, int)
            self.assertTrue(desc and icon, code)

    def test_icons_are_known_to_the_renderer(self):
        # Must stay in sync with the switch in web/app.js weatherIcon().
        known = {"clear", "partly", "cloudy", "fog", "drizzle", "rain",
                 "sleet", "snow", "storm"}
        for code, (_desc, icon) in dashd.WMO.items():
            self.assertIn(icon, known, f"code {code}")


class TestSystemStats(unittest.TestCase):
    def test_shape(self):
        s = dashd.system_stats()
        for key in ("cpu", "mem", "swap", "disks", "net", "uptime", "procs"):
            self.assertIn(key, s)
        self.assertGreaterEqual(s["mem"]["pct"], 0)
        self.assertIsInstance(s["disks"], list)

    def test_net_rates_are_non_negative(self):
        dashd.system_stats()
        s = dashd.system_stats()
        self.assertGreaterEqual(s["net"]["up"], 0)
        self.assertGreaterEqual(s["net"]["down"], 0)


class TestWeatherCache(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = dashd.DATA
        dashd.DATA = Path(self._tmp.name)

    def tearDown(self):
        dashd.DATA = self._orig
        self._tmp.cleanup()

    def _cfg(self, ttl):
        return {"refresh": {"weather_seconds": ttl},
                "location": {"name": "T", "latitude": 0, "longitude": 0,
                             "timezone": "UTC"},
                "units": {"temperature": "fahrenheit", "wind": "mph",
                          "precipitation": "inch"}}

    def test_fresh_cache_is_served_without_refetch(self):
        import time
        payload = {"temp": 70, "fetched_at": time.time()}
        (dashd.DATA / "weather.json").write_text(json.dumps(payload))

        def boom(_cfg):
            raise AssertionError("must not refetch inside TTL")

        orig, dashd.fetch_weather = dashd.fetch_weather, boom
        try:
            out = dashd.cached_weather(self._cfg(600))
        finally:
            dashd.fetch_weather = orig
        self.assertEqual(out["temp"], 70)
        self.assertFalse(out["stale"])

    def test_expired_cache_survives_a_failed_refetch(self):
        """The whole point of the cache: an outage must not blank the panel."""
        payload = {"temp": 55, "fetched_at": 0}  # ancient
        (dashd.DATA / "weather.json").write_text(json.dumps(payload))

        def boom(_cfg):
            raise TimeoutError("network down")

        orig, dashd.fetch_weather = dashd.fetch_weather, boom
        try:
            out = dashd.cached_weather(self._cfg(600))
        finally:
            dashd.fetch_weather = orig
        self.assertEqual(out["temp"], 55)
        self.assertTrue(out["stale"])
        self.assertIn("error", out)

    def test_no_cache_and_failed_fetch_reports_unavailable(self):
        def boom(_cfg):
            raise TimeoutError("network down")

        orig, dashd.fetch_weather = dashd.fetch_weather, boom
        try:
            out = dashd.cached_weather(self._cfg(600))
        finally:
            dashd.fetch_weather = orig
        self.assertTrue(out["unavailable"])
        self.assertTrue(out["stale"])


class TestBuildState(unittest.TestCase):
    def test_contains_the_keys_the_page_reads(self):
        cfg = dashd.load_config()
        orig = dashd.cached_weather
        dashd.cached_weather = lambda _c: {"stale": False, "unavailable": True}
        try:
            st = dashd.build_state(cfg)
        finally:
            dashd.cached_weather = orig
        for key in ("ts", "config", "weather", "sys", "extra"):
            self.assertIn(key, st)
        for key in ("units", "display", "location", "poll"):
            self.assertIn(key, st["config"])


class TestConfig(unittest.TestCase):
    def test_config_is_valid_and_complete(self):
        cfg = dashd.load_config()
        self.assertIn("latitude", cfg["location"])
        self.assertIn("longitude", cfg["location"])
        self.assertIn(cfg["units"]["temperature"], ("fahrenheit", "celsius"))
        self.assertIsInstance(cfg["units"]["clock24"], bool)
        self.assertIn(cfg["display"]["layer"], ("BACKGROUND", "BOTTOM", "TOP"))
        self.assertGreaterEqual(cfg["display"]["safe_area_top"], 0)
        self.assertGreaterEqual(cfg["display"]["safe_area_bottom"], 0)

    def test_layer_is_not_background(self):
        """BACKGROUND renders but is invisible under cosmic-bg. See CLAUDE.md."""
        cfg = dashd.load_config()
        self.assertNotEqual(cfg["display"]["layer"], "BACKGROUND")


if __name__ == "__main__":
    unittest.main()
