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
import sqlite3
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

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


class TestNightAdjusted(unittest.TestCase):
    def test_clear_and_partly_get_a_night_variant(self):
        self.assertEqual(dashd.night_adjusted("clear", False), "clear-night")
        self.assertEqual(dashd.night_adjusted("partly", False), "partly-night")

    def test_daytime_is_unchanged(self):
        self.assertEqual(dashd.night_adjusted("clear", True), "clear")

    def test_other_icons_have_no_night_variant(self):
        for icon in ("cloudy", "fog", "drizzle", "rain", "sleet", "snow", "storm"):
            self.assertEqual(dashd.night_adjusted(icon, False), icon)


class TestSystemStats(unittest.TestCase):
    def test_shape(self):
        s = dashd.system_stats()
        for key in ("cpu", "mem", "swap", "disks", "net", "uptime", "procs"):
            self.assertIn(key, s)
        self.assertGreaterEqual(s["mem"]["pct"], 0)
        self.assertIsInstance(s["disks"], list)
        for d in s["disks"]:
            for key in ("name", "fstype", "size", "pct", "mount"):
                self.assertIn(key, d)

    def test_net_rates_are_non_negative(self):
        dashd.system_stats()
        s = dashd.system_stats()
        self.assertGreaterEqual(s["net"]["up"], 0)
        self.assertGreaterEqual(s["net"]["down"], 0)


class TestDiskTree(unittest.TestCase):
    """Real lsblk on this machine -- same "hits the real system, no network"
    precedent as TestSystemStats. The fallback path is exercised in
    isolation below by forcing subprocess.run to fail."""

    def test_returns_partitions_with_utilization_shape(self):
        rows = dashd.disk_tree()
        self.assertIsInstance(rows, list)
        for row in rows:
            for key in ("name", "fstype", "size", "pct", "mount"):
                self.assertIn(key, row)
            # every row was filtered to have *something* to show
            self.assertTrue(row["pct"] is not None or row["mount"])

    def test_falls_back_to_psutil_view_when_lsblk_unavailable(self):
        # disk_tree() lives in lib/sysinfo.py now (#14); dashd
        # just re-exports the function, so the real subprocess module to
        # patch is the one sysinfo imported, not dashd's own namespace.
        with patch.object(dashd.sysinfo.subprocess, "run",
                           side_effect=FileNotFoundError("no lsblk")):
            rows = dashd.disk_tree()
        self.assertEqual(rows, dashd._disk_tree_from_psutil())


class TestNowPlaying(unittest.TestCase):
    """now_playing() lives in lib/sysinfo.py (ISSUES.md #14 pattern); dashd
    re-exports it, so the real subprocess module to patch is the one
    sysinfo imported, not dashd's own namespace (same as disk_tree above).
    """

    def _run(self, returncode=0, stdout=""):
        return type("CP", (), {"returncode": returncode, "stdout": stdout})()

    def test_no_playerctl_binary_returns_none(self):
        with patch.object(dashd.sysinfo.subprocess, "run",
                           side_effect=FileNotFoundError("no playerctl")):
            self.assertIsNone(dashd.now_playing())

    def test_no_players_returns_none(self):
        with patch.object(dashd.sysinfo.subprocess, "run",
                           return_value=self._run(returncode=1, stdout="")):
            self.assertIsNone(dashd.now_playing())

    def test_stopped_player_is_ignored(self):
        sep = "\x1f"
        line = sep.join(["some.player", "Stopped", "", "", "", "", "", ""])
        with patch.object(dashd.sysinfo.subprocess, "run",
                           return_value=self._run(stdout=line)):
            self.assertIsNone(dashd.now_playing())

    def test_playing_track_is_parsed(self):
        sep = "\x1f"
        line = sep.join(["some.player", "Playing", "Artist", "Title", "Album",
                          "180000000", "45000000", "https://example/art.jpg"])
        with patch.object(dashd.sysinfo.subprocess, "run",
                           return_value=self._run(stdout=line)):
            music = dashd.now_playing()
        self.assertEqual(music["title"], "Title")
        self.assertEqual(music["artist"], "Artist")
        self.assertTrue(music["playing"])
        self.assertAlmostEqual(music["length_secs"], 180.0)
        self.assertAlmostEqual(music["position_secs"], 45.0)

    def test_playing_preferred_over_paused(self):
        sep = "\x1f"
        paused = sep.join(["p1", "Paused", "A1", "T1", "", "", "", ""])
        playing = sep.join(["p2", "Playing", "A2", "T2", "", "", "", ""])
        with patch.object(dashd.sysinfo.subprocess, "run",
                           return_value=self._run(stdout=f"{paused}\n{playing}")):
            music = dashd.now_playing()
        self.assertEqual(music["title"], "T2")
        self.assertTrue(music["playing"])


class TestBatteryCharging(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _bat(self, status):
        d = self.base / "BAT0"
        d.mkdir()
        (d / "status").write_text(status + "\n")

    def test_charging_status_is_true(self):
        self._bat("Charging")
        self.assertTrue(dashd.battery_charging(True, self.base))

    def test_full_status_is_not_charging_even_when_plugged(self):
        self._bat("Full")
        self.assertFalse(dashd.battery_charging(True, self.base))

    def test_no_battery_node_falls_back_to_plugged(self):
        self.assertTrue(dashd.battery_charging(True, self.base))
        self.assertFalse(dashd.battery_charging(False, self.base))


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
    def setUp(self):
        # build_state() reads weather.json/extra.json from DATA -- keep
        # that out of the real data/ dir, same as TestWeatherCache. (It no
        # longer writes metrics itself; bin/dashd-collect owns that, see
        # #14.)
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_data = dashd.DATA
        dashd.DATA = Path(self._tmp.name)

    def tearDown(self):
        dashd.DATA = self._orig_data
        self._tmp.cleanup()

    def test_contains_the_keys_the_page_reads(self):
        cfg = dashd.load_config()
        orig = dashd.cached_weather
        dashd.cached_weather = lambda _c: {"stale": False, "unavailable": True}
        try:
            st = dashd.build_state(cfg)
        finally:
            dashd.cached_weather = orig
        for key in ("ts", "config", "weather", "sys", "music", "tasks", "extra"):
            self.assertIn(key, st)
        for key in ("units", "display", "location", "poll", "metrics_retain_hours"):
            self.assertIn(key, st["config"])


class TestLoadTasks(unittest.TestCase):
    """load_tasks() reads data/tasks.json verbatim -- source-agnostic (#25)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_data = dashd.DATA
        dashd.DATA = Path(self._tmp.name)

    def tearDown(self):
        dashd.DATA = self._orig_data
        self._tmp.cleanup()

    def test_missing_file_returns_empty_items(self):
        self.assertEqual(dashd.load_tasks(), {"items": []})

    def test_valid_file_round_trips(self):
        payload = {"items": [{"text": "Buy milk", "done": False}]}
        (dashd.DATA / "tasks.json").write_text(json.dumps(payload))
        self.assertEqual(dashd.load_tasks(), payload)

    def test_malformed_json_reports_error_without_crashing(self):
        (dashd.DATA / "tasks.json").write_text("{not json")
        out = dashd.load_tasks()
        self.assertEqual(out["items"], [])
        self.assertIn("_error", out)

    def test_wrong_shape_reports_error_without_crashing(self):
        (dashd.DATA / "tasks.json").write_text(json.dumps({"items": "nope"}))
        out = dashd.load_tasks()
        self.assertEqual(out["items"], [])
        self.assertIn("_error", out)


class TestMetricsStore(unittest.TestCase):
    """lib/metrics.py, reached as dashd.metrics since dashd-serve imports it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "metrics.db"

    def tearDown(self):
        self._tmp.cleanup()

    def test_query_on_missing_db_returns_empty(self):
        self.assertEqual(dashd.metrics.query_history(self.db, since_seconds=3600), [])

    def test_insert_then_query_round_trips(self):
        dashd.metrics.insert_sample(
            self.db, {"cpu_pct": 12.5, "mem_pct": 40.0}, retain_seconds=3600)
        rows = dashd.metrics.query_history(self.db, since_seconds=3600)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["cpu_pct"], 12.5)
        self.assertAlmostEqual(rows[0]["mem_pct"], 40.0)
        self.assertIsNone(rows[0]["cpu_temp_c"])  # omitted key -> NULL

    def test_query_window_excludes_rows_outside_it(self):
        dashd.metrics.insert_sample(self.db, {"cpu_pct": 1}, retain_seconds=3600)
        # a negative window can't include anything just inserted at "now"
        self.assertEqual(dashd.metrics.query_history(self.db, since_seconds=-1), [])

    def test_retention_prunes_rows_older_than_retain_seconds(self):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS samples (ts REAL NOT NULL, "
            + ", ".join(f"{c} REAL" for c in dashd.metrics.COLUMNS) + ")")
        conn.execute("INSERT INTO samples (ts, cpu_pct) VALUES (?, ?)",
                     (time.time() - 999_999, 5.0))
        conn.commit()
        conn.close()

        dashd.metrics.insert_sample(self.db, {"cpu_pct": 99}, retain_seconds=10)
        rows = dashd.metrics.query_history(self.db, since_seconds=999_999_999)
        self.assertEqual(len(rows), 1, rows)
        self.assertAlmostEqual(rows[0]["cpu_pct"], 99)

    def test_old_schema_db_self_migrates_a_new_column(self):
        """A DB written before battery_pct existed in COLUMNS must not break
        on first touch after the upgrade -- CREATE TABLE IF NOT EXISTS is a
        no-op against it, so _connect() has to ALTER TABLE the gap in."""
        conn = sqlite3.connect(self.db)
        conn.execute(
            "CREATE TABLE samples (ts REAL NOT NULL, cpu_pct REAL, "
            "cpu_temp_c REAL, cpu_freq_mhz REAL, mem_pct REAL, "
            "net_down REAL, net_up REAL)")  # no battery_pct
        conn.execute("INSERT INTO samples (ts, cpu_pct) VALUES (?, ?)",
                     (time.time(), 7.0))
        conn.commit()
        conn.close()

        dashd.metrics.insert_sample(self.db, {"battery_pct": 55}, retain_seconds=3600)
        rows = dashd.metrics.query_history(self.db, since_seconds=3600)
        self.assertEqual(len(rows), 2, rows)
        self.assertIsNone(rows[0]["battery_pct"])   # pre-migration row
        self.assertAlmostEqual(rows[1]["battery_pct"], 55)


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
        self.assertIn(cfg["display"]["theme"], ("night_ops", "retro_terminal"))

    def test_layer_is_not_background(self):
        """BACKGROUND renders but is invisible under cosmic-bg. See CLAUDE.md."""
        cfg = dashd.load_config()
        self.assertNotEqual(cfg["display"]["layer"], "BACKGROUND")


if __name__ == "__main__":
    unittest.main()
