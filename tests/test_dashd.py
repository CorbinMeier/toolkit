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

    def test_icons_are_known(self):
        # /api/state's icon field is a fixed vocabulary regardless of
        # whether the frontend currently renders anything with it (#40
        # dropped the weather icon graphics, but the field itself stays --
        # a future consumer, e.g. the mobile control surface, still could).
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


class TestMusicArt(unittest.TestCase):
    """GET /api/music/art's logic, factored out as music_art_response() so
    it's testable without a live HTTP handler -- see now_playing() note
    above for why dashd.now_playing (re-exported) is the patch target."""

    def test_nothing_playing_returns_none(self):
        with patch.object(dashd, "now_playing", return_value=None):
            self.assertIsNone(dashd.music_art_response())

    def test_no_art_url_returns_none(self):
        with patch.object(dashd, "now_playing", return_value={"art_url": None}):
            self.assertIsNone(dashd.music_art_response())

    def test_remote_art_url_is_a_redirect(self):
        music = {"art_url": "https://example/cover.jpg"}
        with patch.object(dashd, "now_playing", return_value=music):
            art = dashd.music_art_response()
        self.assertEqual(art, {"redirect": "https://example/cover.jpg"})

    def test_local_file_art_url_is_read_and_typed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cover.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake png bytes")
            music = {"art_url": path.as_uri()}
            with patch.object(dashd, "now_playing", return_value=music):
                art = dashd.music_art_response()
        self.assertEqual(art["content_type"], "image/png")
        self.assertEqual(art["data"], b"\x89PNG\r\n\x1a\n" + b"fake png bytes")

    def test_local_file_with_no_extension_is_sniffed(self):
        # Chromium's MPRIS bridge hands out extension-less temp paths.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".org.chromium.Chromium.z0Ffbp"
            path.write_bytes(b"\xff\xd8\xff" + b"fake jpeg bytes")
            music = {"art_url": path.as_uri()}
            with patch.object(dashd, "now_playing", return_value=music):
                art = dashd.music_art_response()
        self.assertEqual(art["content_type"], "image/jpeg")

    def test_missing_local_file_returns_none(self):
        music = {"art_url": "file:///nonexistent/path/cover.jpg"}
        with patch.object(dashd, "now_playing", return_value=music):
            self.assertIsNone(dashd.music_art_response())

    def test_unsupported_scheme_returns_none(self):
        music = {"art_url": "data:image/png;base64,abc123"}
        with patch.object(dashd, "now_playing", return_value=music):
            self.assertIsNone(dashd.music_art_response())


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

    def test_alert_attached_when_fresh_cache_matches_thresholds(self):
        import time
        payload = {"temp": 95, "desc": "Clear", "precip_prob": 0,
                   "fetched_at": time.time()}
        (dashd.DATA / "weather.json").write_text(json.dumps(payload))
        cfg = self._cfg(600)
        cfg["weather_alerts"] = {"temp_high_f": 90, "rain_pct": 40,
                                  "condition_patterns": []}
        out = dashd.cached_weather(cfg)
        self.assertTrue(out["alert"]["active"])
        self.assertTrue(out["alert"]["fields"]["temp"])

    def test_alert_not_attached_when_unavailable(self):
        def boom(_cfg):
            raise TimeoutError("network down")

        orig, dashd.fetch_weather = dashd.fetch_weather, boom
        try:
            out = dashd.cached_weather(self._cfg(600))
        finally:
            dashd.fetch_weather = orig
        self.assertNotIn("alert", out)


class TestWeatherAlert(unittest.TestCase):
    def _cfg(self, **overrides):
        alerts = {"temp_high_f": 90, "rain_pct": 40,
                  "condition_patterns": [
                      {"regex": "rain|shower|storm", "label": "Severe weather"}]}
        alerts.update(overrides)
        return {"weather_alerts": alerts}

    def test_hot_temp_triggers_only_the_temp_field(self):
        out = dashd.weather_alert({"temp": 92, "high": 80, "desc": "Clear",
                                    "precip_prob": 0}, self._cfg())
        self.assertTrue(out["active"])
        self.assertTrue(out["fields"]["temp"])
        self.assertFalse(out["fields"]["high"])

    def test_hot_forecast_high_triggers_only_the_high_field(self):
        # #48: the user's report was specifically "H100 is not highlighting"
        # -- the daily forecast high is a distinct field from current temp
        # and must be checked (and flagged) independently.
        out = dashd.weather_alert({"temp": 70, "high": 100, "desc": "Clear",
                                    "precip_prob": 0}, self._cfg())
        self.assertTrue(out["active"])
        self.assertFalse(out["fields"]["temp"])
        self.assertTrue(out["fields"]["high"])

    def test_condition_regex_triggers(self):
        out = dashd.weather_alert(
            {"temp": 70, "high": 75, "desc": "Light showers", "precip_prob": 0},
            self._cfg())
        self.assertTrue(out["active"])
        self.assertTrue(out["fields"]["condition"])
        self.assertEqual(out["condition_label"], "Severe weather")

    def test_rain_probability_triggers_only_the_rain_field(self):
        out = dashd.weather_alert(
            {"temp": 70, "high": 75, "desc": "Clear", "precip_prob": 45},
            self._cfg())
        self.assertTrue(out["active"])
        self.assertTrue(out["fields"]["rain"])
        self.assertFalse(out["fields"]["temp"])

    def test_no_alert_when_nothing_matches(self):
        out = dashd.weather_alert(
            {"temp": 70, "high": 75, "desc": "Clear", "precip_prob": 5},
            self._cfg())
        self.assertFalse(out["active"])
        self.assertFalse(any(out["fields"].values()))
        self.assertIsNone(out["condition_label"])


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
        orig_weather, orig_logs = dashd.cached_weather, dashd.logsrc.read_log_lines
        orig_projects = dashd.cached_projects
        dashd.cached_weather = lambda _c: {"stale": False, "unavailable": True}
        dashd.logsrc.read_log_lines = lambda _c: []
        # Stubbed, not just tolerated: the real cached_projects() would
        # spawn a background `gh` fetch on a cache miss, making this
        # hermetic unit test shell out to the network.
        dashd.cached_projects = lambda _c: {"available": True, "rows": [],
                                            "totals": {}}
        try:
            st = dashd.build_state(cfg)
        finally:
            dashd.cached_weather = orig_weather
            dashd.logsrc.read_log_lines = orig_logs
            dashd.cached_projects = orig_projects
        for key in ("ts", "config", "weather", "sys", "music", "projects",
                    "tasks", "logs", "extra"):
            self.assertIn(key, st)
        self.assertEqual(st["logs"], [])
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


class TestLogSource(unittest.TestCase):
    """dashd.logsrc is lib/logsrc.py (#28)."""

    def test_only_matching_lines_are_returned_with_status_and_label(self):
        cfg = {"logs": {"source_type": "journalctl", "journalctl_unit": "ssh",
                         "max_lines": 50, "patterns": [
                             {"regex": "Failed password", "status": "critical",
                              "label": "Failed login"}]}}
        fake = type("R", (), {"stdout":
            "2026-08-20T10:00:00-07:00 Failed password for root\n"
            "2026-08-20T10:00:01-07:00 ordinary line, no match\n"})()
        with patch.object(dashd.logsrc.subprocess, "run", return_value=fake):
            rows = dashd.logsrc.read_log_lines(cfg)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "critical")
        self.assertEqual(rows[0]["label"], "Failed login")
        self.assertEqual(rows[0]["ts"], "2026-08-20T10:00:00-07:00")

    def test_file_source_tails_and_filters(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as fh:
            fh.write("plain line\n")
            fh.write("2026-08-20T09:00:00 Failed password for admin\n")
            path = fh.name
        try:
            cfg = {"logs": {"source_type": "file", "file_path": path,
                             "max_lines": 10, "patterns": [
                                 {"regex": "Failed password", "status": "critical",
                                  "label": "Failed login"}]}}
            rows = dashd.logsrc.read_log_lines(cfg)
        finally:
            Path(path).unlink()
        self.assertEqual(len(rows), 1)
        self.assertIn("admin", rows[0]["text"])

    def test_no_patterns_configured_returns_empty(self):
        cfg = {"logs": {"source_type": "journalctl", "journalctl_unit": "ssh"}}
        self.assertEqual(dashd.logsrc.read_log_lines(cfg), [])

    def test_missing_file_source_config_returns_empty(self):
        cfg = {"logs": {"source_type": "file", "file_path": None,
                         "patterns": [{"regex": "x", "status": "info", "label": ""}]}}
        self.assertEqual(dashd.logsrc.read_log_lines(cfg), [])

    def test_missing_journalctl_binary_returns_empty_not_an_error(self):
        cfg = {"logs": {"source_type": "journalctl", "journalctl_unit": "ssh",
                         "patterns": [{"regex": "x", "status": "info", "label": ""}]}}
        with patch.object(dashd.logsrc.subprocess, "run",
                           side_effect=FileNotFoundError("no journalctl")):
            self.assertEqual(dashd.logsrc.read_log_lines(cfg), [])


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
        self.assertIn(
            cfg["display"]["theme"], ("night_ops", "retro_terminal", "retro_orange")
        )

    def test_layer_is_not_background(self):
        """BACKGROUND renders but is invisible under cosmic-bg. See CLAUDE.md."""
        cfg = dashd.load_config()
        self.assertNotEqual(cfg["display"]["layer"], "BACKGROUND")


def _repo(name, open_n, blocked, closed, pushed):
    return {"nameWithOwner": name, "pushedAt": pushed, "isArchived": False,
            "open": {"totalCount": open_n}, "closed": {"totalCount": closed},
            "blocked": {"totalCount": blocked}}


def _payload(**owners):
    """{"o0": [repo, ...]} -> the GraphQL envelope shape()."""
    data = {}
    for alias, repos in owners.items():
        data[alias] = {"repositories": {"pageInfo": {"hasNextPage": False},
                                        "nodes": repos}}
    return {"data": data}


NOW = datetime(2026, 8, 27, tzinfo=UTC).timestamp()


class TestProjectStatsShape(unittest.TestCase):
    """ghstats.shape() carries every #62 product decision, so it gets the
    tests -- the GraphQL call itself is a thin subprocess wrapper."""

    OWNERS = [{"login": "me", "kind": "user"}]

    def _shape(self, repos, **kw):
        return dashd.ghstats.shape(_payload(o0=repos), self.OWNERS,
                                   now=NOW, **kw)

    def test_actionable_is_open_minus_blocked(self):
        out = self._shape([_repo("me/a", 21, 10, 200, "2026-08-26T00:00:00Z")])
        row = out["rows"][0]
        self.assertEqual(row["blocked"], 10)
        self.assertEqual(row["actionable"], 11)
        self.assertEqual(row["open"], 21)
        self.assertEqual(row["closed"], 200)

    def test_blocked_outranks_actionable(self):
        """The #62 sort call: 16-blocked/4-actionable beats 5-blocked/32."""
        out = self._shape([
            _repo("me/many-actionable", 37, 5, 0, "2026-08-26T00:00:00Z"),
            _repo("me/many-blocked", 20, 16, 0, "2026-08-25T00:00:00Z"),
        ])
        self.assertEqual([r["short"] for r in out["rows"]],
                         ["many-blocked", "many-actionable"])

    def test_recency_only_breaks_ties(self):
        """Equal blocked+actionable -> newest first. Recency must not
        outrank issue counts (that was the rejected first design).

        Every fixture sits inside the staleness window on purpose -- dating
        one outside it would filter the row before the sort ever saw it and
        silently turn this into a test of the cutoff instead.
        """
        out = self._shape([
            _repo("me/old", 3, 1, 0, "2026-06-01T00:00:00Z"),
            _repo("me/new", 3, 1, 0, "2026-08-20T00:00:00Z"),
            # Same blocked count, more actionable -- outranks both despite
            # being the least recently pushed of the three.
            _repo("me/busy", 9, 1, 0, "2026-05-30T00:00:00Z"),
        ])
        self.assertEqual([r["short"] for r in out["rows"]],
                         ["busy", "new", "old"])

    def test_repos_with_no_open_issues_are_dropped(self):
        out = self._shape([
            _repo("me/quiet", 0, 0, 99, "2026-08-26T00:00:00Z"),
            _repo("me/loud", 2, 0, 0, "2026-08-26T00:00:00Z"),
        ])
        self.assertEqual([r["short"] for r in out["rows"]], ["loud"])

    def test_stale_repos_are_filtered(self):
        repos = [_repo("me/dormant", 7, 0, 0, "2024-12-26T00:00:00Z"),
                 _repo("me/live", 1, 0, 0, "2026-08-26T00:00:00Z")]
        self.assertEqual(
            [r["short"] for r in self._shape(repos)["rows"]], ["live"])
        # 0 disables the cutoff -- both come back.
        self.assertEqual(len(self._shape(repos, stale_after_days=0)["rows"]), 2)

    def test_totals_sum_only_the_visible_rows(self):
        out = self._shape([
            _repo("me/a", 5, 2, 10, "2026-08-26T00:00:00Z"),
            _repo("me/b", 3, 1, 4, "2026-08-25T00:00:00Z"),
            # Filtered by staleness -- its 100 closed must not inflate totals.
            _repo("me/old", 9, 9, 100, "2023-01-01T00:00:00Z"),
        ])
        self.assertEqual(out["totals"], {"blocked": 3, "actionable": 5,
                                         "closed": 14, "repos": 2})

    def test_unreachable_owner_is_reported_not_silently_skipped(self):
        owners = [{"login": "me", "kind": "user"},
                  {"login": "secret-org", "kind": "org"}]
        out = dashd.ghstats.shape({"data": {"o0": None, "o1": None}}, owners,
                                  now=NOW)
        self.assertEqual(out["missing"], ["me", "secret-org"])
        self.assertEqual(out["rows"], [])

    def test_truncation_is_reported(self):
        raw = {"data": {"o0": {"repositories": {
            "pageInfo": {"hasNextPage": True},
            "nodes": [_repo("me/a", 1, 0, 0, "2026-08-26T00:00:00Z")]}}}}
        out = dashd.ghstats.shape(raw, self.OWNERS, now=NOW)
        self.assertEqual(out["truncated"], ["me"])

    def test_unparseable_push_date_survives(self):
        """A malformed pushedAt must not drop the row or raise -- the counts
        are still the useful part."""
        out = self._shape([_repo("me/a", 4, 1, 0, "not-a-date")])
        self.assertEqual(len(out["rows"]), 1)
        self.assertIsNone(out["rows"][0]["age_days"])


class TestProjectStatsQuery(unittest.TestCase):
    def test_user_and_org_use_different_root_fields(self):
        q = dashd.ghstats.build_query([{"login": "me", "kind": "user"},
                                       {"login": "Some-Org", "kind": "org"}])
        self.assertIn('o0: user(login: "me")', q)
        self.assertIn('o1: organization(login: "Some-Org")', q)

    def test_owner_affiliations_only_on_the_user_connection(self):
        """organization(...).repositories rejects ownerAffiliations; the
        user connection needs it to exclude collaborator repos."""
        user_q = dashd.ghstats.build_query([{"login": "me", "kind": "user"}])
        org_q = dashd.ghstats.build_query([{"login": "o", "kind": "org"}])
        self.assertIn("ownerAffiliations", user_q)
        self.assertNotIn("ownerAffiliations", org_q)

    def test_query_never_selects_issue_nodes(self):
        """Stats only -- selecting issue nodes would blow up the rate-limit
        cost and pull data the panel has no use for."""
        q = dashd.ghstats.build_query([{"login": "me", "kind": "user"}])
        self.assertIn("totalCount", q)
        self.assertNotIn("title", q)
        self.assertNotIn("body", q)


class TestCachedProjects(unittest.TestCase):
    """cached_projects() must never block the request thread on `gh`."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_data = dashd.DATA
        dashd.DATA = Path(self._tmp.name)

    def tearDown(self):
        dashd.DATA = self._orig_data
        dashd._projects_refreshing = False
        self._tmp.cleanup()

    def _write(self, payload):
        (Path(self._tmp.name) / "projects.json").write_text(json.dumps(payload))

    def test_disabled_short_circuits_before_any_fetch(self):
        with patch.object(dashd.threading, "Thread") as thread:
            out = dashd.cached_projects({"projects": {"enabled": False}})
        self.assertFalse(out["available"])
        thread.assert_not_called()

    def test_cache_miss_returns_pending_and_schedules_a_thread(self):
        with patch.object(dashd.threading, "Thread") as thread:
            out = dashd.cached_projects({"projects": {}})
        self.assertTrue(out["pending"])
        self.assertEqual(out["rows"], [])
        thread.assert_called_once()
        self.assertTrue(thread.call_args.kwargs["daemon"])

    def test_fresh_cache_is_served_without_refetching(self):
        self._write({"available": True, "rows": [{"repo": "me/a"}],
                     "totals": {}, "fetched_at": time.time()})
        with patch.object(dashd.threading, "Thread") as thread:
            out = dashd.cached_projects({"projects": {"refresh_seconds": 3600}})
        self.assertFalse(out["stale"])
        self.assertEqual(len(out["rows"]), 1)
        thread.assert_not_called()

    def test_stale_cache_is_served_and_marked_while_refreshing(self):
        self._write({"available": True, "rows": [{"repo": "me/a"}],
                     "totals": {}, "fetched_at": time.time() - 99999})
        with patch.object(dashd.threading, "Thread") as thread:
            out = dashd.cached_projects({"projects": {"refresh_seconds": 60}})
        # Old counts beat an empty panel: the row survives, flagged stale.
        self.assertTrue(out["stale"])
        self.assertEqual(len(out["rows"]), 1)
        thread.assert_called_once()

    def test_gh_failure_is_cached_so_it_does_not_respawn_every_poll(self):
        def boom(_cfg=None, timeout=None):
            raise dashd.ghstats.GhUnavailableError("gh is not installed")

        with patch.object(dashd.ghstats, "fetch", boom):
            dashd._projects_refresh({"projects": {}})
        written = json.loads(
            (Path(self._tmp.name) / "projects.json").read_text())
        self.assertFalse(written["available"])
        self.assertIn("not installed", written["error"])
        self.assertIn("fetched_at", written)

    def test_unexpected_exception_still_clears_the_refresh_flag(self):
        """A background thread that dies without clearing the flag would
        wedge the panel on a stale cache forever."""
        def boom(_cfg=None, timeout=None):
            raise ValueError("something unexpected")

        dashd._projects_refreshing = True
        with patch.object(dashd.ghstats, "fetch", boom):
            dashd._projects_refresh({"projects": {}})
        self.assertFalse(dashd._projects_refreshing)


if __name__ == "__main__":
    unittest.main()
