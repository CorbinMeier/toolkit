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

# Importable only after dashd-serve has run: it is what puts lib/ on sys.path.
import logsrc  # noqa: E402
import sessions  # noqa: E402
import userconf  # noqa: E402


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


class TestMusicCommand(unittest.TestCase):
    """Transport controls (#65). The whitelist is the security boundary --
    request data must never reach playerctl's argv as anything but one of
    three fixed literals."""

    def _run(self, returncode=0):
        return type("CP", (), {"returncode": returncode, "stdout": ""})()

    def test_known_commands_are_forwarded_verbatim(self):
        for command in ("play-pause", "next", "previous"):
            with patch.object(dashd.sysinfo.subprocess, "run",
                               return_value=self._run()) as run:
                self.assertTrue(dashd.sysinfo.music_command(command))
            self.assertEqual(run.call_args[0][0], ["playerctl", "-a", command])

    def test_unknown_command_raises_before_spawning_anything(self):
        for bad in ("stop; rm -rf /", "volume", "", "PLAY-PAUSE", None):
            with patch.object(dashd.sysinfo.subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    dashd.sysinfo.music_command(bad)
                run.assert_not_called()

    def test_missing_playerctl_is_a_no_op_not_an_error(self):
        with patch.object(dashd.sysinfo.subprocess, "run",
                           side_effect=FileNotFoundError("no playerctl")):
            self.assertFalse(dashd.sysinfo.music_command("next"))

    def test_player_refusal_reports_false(self):
        with patch.object(dashd.sysinfo.subprocess, "run",
                           return_value=self._run(returncode=1)):
            self.assertFalse(dashd.sysinfo.music_command("next"))

    def test_route_whitelist_matches_the_implementation(self):
        """The handler validates against sysinfo.MUSIC_COMMANDS; if that
        tuple and the accepted verbs ever drift, the route silently starts
        rejecting (or worse, forwarding) something unexpected."""
        self.assertEqual(set(dashd.sysinfo.MUSIC_COMMANDS),
                          {"play-pause", "next", "previous"})


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


class TestAqiCategory(unittest.TestCase):
    def test_bands_are_inclusive_of_their_ceiling(self):
        self.assertEqual(dashd.aqi_category(50), "Good")
        self.assertEqual(dashd.aqi_category(51), "Moderate")
        self.assertEqual(dashd.aqi_category(101), "Unhealthy for Sensitive Groups")
        self.assertEqual(dashd.aqi_category(500), "Hazardous")


class TestAirQualityCache(unittest.TestCase):
    """Same cache-with-TTL-and-stale-fallback shape as TestWeatherCache,
    since #79 explicitly follows that pattern -- exercised through the
    shared _fetch_or_cache() helper."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = dashd.DATA
        dashd.DATA = Path(self._tmp.name)

    def tearDown(self):
        dashd.DATA = self._orig
        self._tmp.cleanup()

    def _cfg(self, ttl=900, aqi_high=None):
        cfg = {"refresh": {"air_quality_seconds": ttl},
               "location": {"name": "T", "latitude": 0, "longitude": 0,
                            "timezone": "UTC"}}
        if aqi_high is not None:
            cfg["weather_alerts"] = {"aqi_high": aqi_high}
        return cfg

    def test_fresh_cache_is_served_without_refetch(self):
        import time
        payload = {"aqi": 42, "pm2_5": 8.1, "category": "Good",
                   "fetched_at": time.time()}
        (dashd.DATA / "air_quality.json").write_text(json.dumps(payload))

        def boom(_cfg):
            raise AssertionError("must not refetch inside TTL")

        orig, dashd.fetch_air_quality = dashd.fetch_air_quality, boom
        try:
            out = dashd.cached_air_quality(self._cfg())
        finally:
            dashd.fetch_air_quality = orig
        self.assertEqual(out["aqi"], 42)
        self.assertFalse(out["stale"])

    def test_expired_cache_survives_a_failed_refetch(self):
        payload = {"aqi": 30, "fetched_at": 0}
        (dashd.DATA / "air_quality.json").write_text(json.dumps(payload))

        def boom(_cfg):
            raise TimeoutError("network down")

        orig, dashd.fetch_air_quality = dashd.fetch_air_quality, boom
        try:
            out = dashd.cached_air_quality(self._cfg())
        finally:
            dashd.fetch_air_quality = orig
        self.assertEqual(out["aqi"], 30)
        self.assertTrue(out["stale"])

    def test_no_cache_and_failed_fetch_reports_unavailable(self):
        def boom(_cfg):
            raise TimeoutError("network down")

        orig, dashd.fetch_air_quality = dashd.fetch_air_quality, boom
        try:
            out = dashd.cached_air_quality(self._cfg())
        finally:
            dashd.fetch_air_quality = orig
        self.assertTrue(out["unavailable"])

    def test_alert_flag_set_past_configured_threshold(self):
        import time
        payload = {"aqi": 150, "fetched_at": time.time()}
        (dashd.DATA / "air_quality.json").write_text(json.dumps(payload))
        out = dashd.cached_air_quality(self._cfg(aqi_high=101))
        self.assertTrue(out["alert"])

    def test_alert_flag_clear_under_configured_threshold(self):
        import time
        payload = {"aqi": 40, "fetched_at": time.time()}
        (dashd.DATA / "air_quality.json").write_text(json.dumps(payload))
        out = dashd.cached_air_quality(self._cfg(aqi_high=101))
        self.assertFalse(out["alert"])


class TestShapeNwsAlerts(unittest.TestCase):
    def test_extracts_the_fields_the_panel_needs(self):
        raw = {"features": [{"properties": {
            "event": "Red Flag Warning", "severity": "Severe",
            "headline": "Red Flag Warning issued", "onset": "2026-09-21T00:00:00Z",
            "ends": "2026-09-22T00:00:00Z", "senderName": "NWS Sacramento",
            "status": "Actual"}}]}
        out = dashd.shape_nws_alerts(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["event"], "Red Flag Warning")
        self.assertEqual(out[0]["severity"], "Severe")

    def test_non_actual_status_is_dropped(self):
        raw = {"features": [{"properties": {
            "event": "Test Alert", "severity": "Minor", "status": "Test"}}]}
        self.assertEqual(dashd.shape_nws_alerts(raw), [])

    def test_no_features_is_an_empty_list(self):
        self.assertEqual(dashd.shape_nws_alerts({"features": []}), [])
        self.assertEqual(dashd.shape_nws_alerts({}), [])

    def test_sorted_most_severe_first(self):
        raw = {"features": [
            {"properties": {"event": "Minor thing", "severity": "Minor"}},
            {"properties": {"event": "Extreme thing", "severity": "Extreme"}},
            {"properties": {"event": "Moderate thing", "severity": "Moderate"}},
        ]}
        out = dashd.shape_nws_alerts(raw)
        self.assertEqual([r["severity"] for r in out],
                         ["Extreme", "Moderate", "Minor"])


class TestNwsAlertsCache(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = dashd.DATA
        dashd.DATA = Path(self._tmp.name)

    def tearDown(self):
        dashd.DATA = self._orig
        self._tmp.cleanup()

    def _cfg(self, ttl=300):
        return {"refresh": {"nws_alerts_seconds": ttl},
                "location": {"name": "T", "latitude": 0, "longitude": 0,
                             "timezone": "UTC"}}

    def test_fresh_cache_is_served_without_refetch(self):
        import time
        payload = {"alerts": [{"event": "Heat Advisory"}], "fetched_at": time.time()}
        (dashd.DATA / "nws_alerts.json").write_text(json.dumps(payload))

        def boom(_cfg):
            raise AssertionError("must not refetch inside TTL")

        orig, dashd.fetch_nws_alerts = dashd.fetch_nws_alerts, boom
        try:
            out = dashd.cached_nws_alerts(self._cfg())
        finally:
            dashd.fetch_nws_alerts = orig
        self.assertEqual(out["alerts"][0]["event"], "Heat Advisory")

    def test_no_cache_and_failed_fetch_reports_unavailable(self):
        def boom(_cfg):
            raise TimeoutError("network down")

        orig, dashd.fetch_nws_alerts = dashd.fetch_nws_alerts, boom
        try:
            out = dashd.cached_nws_alerts(self._cfg())
        finally:
            dashd.fetch_nws_alerts = orig
        self.assertTrue(out["unavailable"])


class TestInvalidateWeatherCache(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = dashd.DATA
        dashd.DATA = Path(self._tmp.name)

    def tearDown(self):
        dashd.DATA = self._orig
        self._tmp.cleanup()

    def test_drops_all_three_location_derived_caches(self):
        for name in ("weather.json", "air_quality.json", "nws_alerts.json"):
            (dashd.DATA / name).write_text("{}")
        dashd.invalidate_weather_cache()
        for name in ("weather.json", "air_quality.json", "nws_alerts.json"):
            self.assertFalse((dashd.DATA / name).exists())


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
        orig_apt = dashd.cached_apt_updates
        orig_aq, orig_nws = dashd.cached_air_quality, dashd.cached_nws_alerts
        dashd.cached_weather = lambda _c: {"stale": False, "unavailable": True}
        dashd.logsrc.read_log_lines = lambda _c: []
        # Stubbed, not just tolerated: the real cached_projects()/
        # cached_apt_updates() would spawn a background `gh`/`apt-get` fetch
        # on a cache miss, making this hermetic unit test shell out to the
        # network or take the ~4s a real apt simulation costs.
        # cached_air_quality/cached_nws_alerts would do the same on an
        # empty cache dir (no cache -> straight to the live fetch), so
        # they're stubbed too.
        dashd.cached_projects = lambda _c: {"available": True, "rows": [],
                                            "totals": {}}
        dashd.cached_apt_updates = lambda _c: {"available": True, "total": 0,
                                                "security": 0}
        dashd.cached_air_quality = lambda _c: {"stale": False, "unavailable": True}
        dashd.cached_nws_alerts = lambda _c: {"stale": False, "unavailable": True}
        try:
            st = dashd.build_state(cfg)
        finally:
            dashd.cached_weather = orig_weather
            dashd.logsrc.read_log_lines = orig_logs
            dashd.cached_projects = orig_projects
            dashd.cached_apt_updates = orig_apt
            dashd.cached_air_quality = orig_aq
            dashd.cached_nws_alerts = orig_nws
        for key in ("ts", "config", "weather", "air_quality", "nws_alerts",
                    "sys", "music", "projects", "updates",
                    "tasks", "logs", "extra", "locations"):
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

    def test_interactive_is_a_bool(self):
        """display.interactive (#36) gates dashd-host's input region. A
        string here would be truthy regardless of its value, so "false"
        would silently make the surface interactive."""
        self.assertIsInstance(dashd.load_config()["display"]["interactive"], bool)

    def test_units_may_write_data_but_never_config_json(self):
        """#74 inverted the #69 grant. config.json now holds tracked
        definitions only and is never written at runtime -- selections go to
        data/config.local.json -- so the sandbox should no longer permit
        writing it. Keeping the old entry would leave the "never written"
        property enforced by convention alone; dropping it makes systemd
        enforce it. data/ stays writable, and at DIRECTORY level, which is
        what allows the tmp-then-rename the single-file grant could not
        (#69/#70)."""
        root = BIN.parent.parent
        for name in ("desktop-dashboard-serve.service",
                     "desktop-dashboard-control.service"):
            unit = root / "systemd" / name
            rw_line = next(
                line for line in unit.read_text().splitlines()
                if line.startswith("ReadWritePaths=")
            )
            self.assertIn("desktop/data", rw_line, name)
            self.assertNotIn("config.json", rw_line, name)


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


class TestAptCheckShape(unittest.TestCase):
    """aptcheck.shape() parses `apt-get -s upgrade`'s simulated output --
    only `Inst ` lines count, and `-security` in the parenthesized origin
    list is what earns the security count (#83)."""

    def test_counts_total_and_security(self):
        sim = (
            "Inst libc6-i386 [2.39-0ubuntu8.8] "
            "(2.39-0ubuntu8.9 Ubuntu:24.04/noble-security, "
            "Ubuntu:24.04/noble-updates [amd64]) []\n"
            "Inst vim [2:9.1.0-1] (2:9.1.1-1 Ubuntu:24.04/noble-updates "
            "[amd64])\n"
            "Conf libc6-i386 (2.39-0ubuntu8.9 Ubuntu:24.04/noble-security "
            "[amd64])\n"
        )
        out = dashd.aptcheck.shape(sim)
        self.assertEqual(out["total"], 2)
        self.assertEqual(out["security"], 1)
        self.assertTrue(out["available"])

    def test_no_pending_upgrades(self):
        out = dashd.aptcheck.shape("Reading package lists...\n"
                                    "Building dependency tree...\n")
        self.assertEqual(out, {"total": 0, "security": 0, "available": True})


class TestCachedAptUpdates(unittest.TestCase):
    """cached_apt_updates() must never block the request thread on
    apt-get's ~4s simulation (#83) -- same fetch-once-and-cache shape as
    cached_projects()."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_data = dashd.DATA
        dashd.DATA = Path(self._tmp.name)

    def tearDown(self):
        dashd.DATA = self._orig_data
        dashd._apt_refreshing = False
        self._tmp.cleanup()

    def _write(self, payload):
        (Path(self._tmp.name) / "apt_updates.json").write_text(json.dumps(payload))

    def test_disabled_short_circuits_before_any_fetch(self):
        with patch.object(dashd.threading, "Thread") as thread:
            out = dashd.cached_apt_updates({"updates": {"enabled": False}})
        self.assertFalse(out["available"])
        thread.assert_not_called()

    def test_cache_miss_returns_pending_and_schedules_a_thread(self):
        with patch.object(dashd.threading, "Thread") as thread:
            out = dashd.cached_apt_updates({"updates": {}})
        self.assertTrue(out["pending"])
        self.assertEqual(out["total"], 0)
        thread.assert_called_once()
        self.assertTrue(thread.call_args.kwargs["daemon"])

    def test_fresh_cache_is_served_without_refetching(self):
        self._write({"available": True, "total": 5, "security": 1,
                     "fetched_at": time.time()})
        with patch.object(dashd.threading, "Thread") as thread:
            out = dashd.cached_apt_updates({"updates": {"refresh_seconds": 3600}})
        self.assertFalse(out["stale"])
        self.assertEqual(out["total"], 5)
        thread.assert_not_called()

    def test_stale_cache_is_served_and_marked_while_refreshing(self):
        self._write({"available": True, "total": 5, "security": 1,
                     "fetched_at": time.time() - 99999})
        with patch.object(dashd.threading, "Thread") as thread:
            out = dashd.cached_apt_updates({"updates": {"refresh_seconds": 60}})
        self.assertTrue(out["stale"])
        self.assertEqual(out["total"], 5)
        thread.assert_called_once()

    def test_apt_failure_is_cached_so_it_does_not_respawn_every_poll(self):
        def boom(timeout=None):
            raise dashd.aptcheck.AptUnavailableError("apt-get is not installed")

        with patch.object(dashd.aptcheck, "fetch", boom):
            dashd._apt_refresh()
        written = json.loads(
            (Path(self._tmp.name) / "apt_updates.json").read_text())
        self.assertFalse(written["available"])
        self.assertIn("not installed", written["error"])
        self.assertIn("fetched_at", written)

    def test_unexpected_exception_still_clears_the_refresh_flag(self):
        def boom(timeout=None):
            raise ValueError("something unexpected")

        dashd._apt_refreshing = True
        with patch.object(dashd.aptcheck, "fetch", boom):
            dashd._apt_refresh()
        self.assertFalse(dashd._apt_refreshing)


class TestConfigPatch(unittest.TestCase):
    """POST /api/config (#66 -- the wallpaper's own context menu, theme
    switch). Whitelist, not sanitisation: same posture as dashd-control's
    EDITABLE and #65's MUSIC_COMMANDS -- a request body can never touch a
    field outside EDITABLE, no matter what it contains."""

    def test_editable_field_is_accepted(self):
        accepted, rejected = dashd.filter_patch(
            {"display": {"theme": "retro_orange"}})
        self.assertEqual(rejected, [])
        self.assertEqual(accepted, {"display": {"theme": "retro_orange"}})

    def test_unknown_top_level_section_is_rejected_and_not_accepted(self):
        accepted, rejected = dashd.filter_patch({"server": {"port": 9999}})
        self.assertEqual(rejected, ["server"])
        self.assertEqual(accepted, {})

    def test_unknown_field_within_an_editable_section_is_rejected(self):
        accepted, rejected = dashd.filter_patch({"display": {"transparent": True}})
        self.assertEqual(rejected, ["display.transparent"])
        self.assertEqual(accepted, {})

    def test_non_dict_patch_body_is_rejected_wholesale(self):
        accepted, rejected = dashd.filter_patch(["not", "a", "dict"])
        self.assertEqual(rejected, ["<patch body>"])
        self.assertEqual(accepted, {})

    def test_location_is_editable_since_77(self):
        """#77: location moved from a config.json-only definition to a
        selection, same as display.theme -- the whole point of the
        Location submenu (#66/#77)."""
        accepted, rejected = dashd.filter_patch(
            {"location": {"name": "Reno, NV", "latitude": 39.5296,
                          "longitude": -119.8138, "timezone": "America/Los_Angeles"}})
        self.assertEqual(rejected, [])
        self.assertEqual(accepted["location"]["name"], "Reno, NV")

    def test_location_field_outside_the_whitelist_is_rejected(self):
        accepted, rejected = dashd.filter_patch(
            {"location": {"name": "X", "elevation": 500}})
        self.assertEqual(rejected, ["location.elevation"])
        self.assertEqual(accepted, {"location": {"name": "X"}})


class TestGeocodeQuery(unittest.TestCase):
    """GET /api/geocode?q= input validation (#77) -- whitelist, not
    sanitisation, same posture as MUSIC_COMMANDS: a request must never
    reach the network as arbitrary text."""

    def test_ordinary_place_names_match(self):
        for q in ("Chico", "Chico, CA", "San Francisco", "München",
                  "St. Louis", "Winston-Salem"):
            self.assertTrue(dashd.GEOCODE_QUERY_RE.match(q), q)

    def test_empty_query_is_rejected(self):
        self.assertIsNone(dashd.GEOCODE_QUERY_RE.match(""))

    def test_overlong_query_is_rejected(self):
        self.assertIsNone(dashd.GEOCODE_QUERY_RE.match("a" * 81))

    def test_shell_and_url_metacharacters_are_rejected(self):
        for q in ("Chico; rm -rf /", "Chico`whoami`", "Chico$(id)",
                  "http://evil/", "Chico&x=1", "Chico\nInjected",
                  "../../etc/passwd"):
            self.assertIsNone(dashd.GEOCODE_QUERY_RE.match(q), q)

    def test_geocode_hits_the_expected_url_and_trims_fields(self):
        raw = {"results": [
            {"name": "Chico", "admin1": "California", "country": "United States",
             "latitude": 39.72849, "longitude": -121.83748,
             "timezone": "America/Los_Angeles", "population": 101475,
             "id": 5327684},
        ]}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(raw).encode()

        with patch.object(dashd.urllib.request, "urlopen",
                           return_value=FakeResp()) as urlopen:
            results = dashd.geocode("Chico", count=3)
        url = urlopen.call_args[0][0]
        self.assertTrue(url.startswith(
            "https://geocoding-api.open-meteo.com/v1/search?"))
        self.assertIn("name=Chico", url)
        self.assertIn("count=3", url)
        self.assertEqual(results, [
            {"name": "Chico", "admin1": "California",
             "country": "United States", "latitude": 39.72849,
             "longitude": -121.83748, "timezone": "America/Los_Angeles"},
        ])

    def test_geocode_rejects_an_invalid_query_before_touching_the_network(self):
        """geocode() validates itself -- same "checked at the point that
        would reach a network/subprocess call, not just at the HTTP edge"
        posture as sysinfo.music_command() -- so a caller that isn't the
        HTTP route (e.g. a future data/locations.json-populating script)
        can't skip the whitelist by not going through do_GET."""
        with patch.object(dashd.urllib.request, "urlopen") as urlopen:
            with self.assertRaises(ValueError):
                dashd.geocode("Chico; rm -rf /")
            urlopen.assert_not_called()

    def test_geocode_count_is_capped(self):
        with patch.object(dashd.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = (
                json.dumps({"results": []}).encode())
            dashd.geocode("Chico", count=999)
        url = urlopen.call_args[0][0]
        self.assertIn(f"count={dashd.GEOCODE_MAX_COUNT}", url)


class TestLoadLocations(unittest.TestCase):
    """data/locations.json, read the same source-agnostic way as
    tasks.json/extra.json (#77): missing file just means the configured
    location is the only choice."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = dashd.DATA
        dashd.DATA = Path(self._tmp.name)

    def tearDown(self):
        dashd.DATA = self._orig
        self._tmp.cleanup()

    def test_missing_file_returns_empty_list(self):
        self.assertEqual(dashd.load_locations(), [])

    def test_valid_entries_are_trimmed_to_the_expected_shape(self):
        entries = [
            {"name": "Reno, NV", "latitude": 39.5, "longitude": -119.8,
             "timezone": "America/Los_Angeles", "extra_junk": True},
        ]
        (dashd.DATA / "locations.json").write_text(json.dumps(entries))
        self.assertEqual(dashd.load_locations(), [
            {"name": "Reno, NV", "latitude": 39.5, "longitude": -119.8,
             "timezone": "America/Los_Angeles"},
        ])

    def test_malformed_entries_are_dropped_not_fatal(self):
        entries = [{"name": "Incomplete"}, "not a dict", 5]
        (dashd.DATA / "locations.json").write_text(json.dumps(entries))
        self.assertEqual(dashd.load_locations(), [])

    def test_non_list_json_returns_empty_list(self):
        (dashd.DATA / "locations.json").write_text(json.dumps({"oops": 1}))
        self.assertEqual(dashd.load_locations(), [])

    def test_corrupt_json_returns_empty_list_not_an_error(self):
        (dashd.DATA / "locations.json").write_text("{not json")
        self.assertEqual(dashd.load_locations(), [])


class TestWeatherCacheInvalidation(unittest.TestCase):
    """#77 acceptance criterion: a location change must invalidate
    data/weather.json immediately, not wait out the TTL, or the header
    names the new city over the old city's cached observations."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = dashd.DATA
        dashd.DATA = Path(self._tmp.name)

    def tearDown(self):
        dashd.DATA = self._orig
        self._tmp.cleanup()

    def test_deletes_an_existing_cache_file(self):
        path = dashd.DATA / "weather.json"
        path.write_text(json.dumps({"temp": 70, "fetched_at": time.time()}))
        dashd.invalidate_weather_cache()
        self.assertFalse(path.exists())

    def test_missing_cache_file_is_a_no_op(self):
        dashd.invalidate_weather_cache()  # must not raise


class TestUserConfig(unittest.TestCase):
    """lib/userconf -- tracked definitions vs. untracked selections (#74)."""

    def root(self, tmp, base, local=None):
        root = Path(tmp)
        (root / "config.json").write_text(json.dumps(base))
        if local is not None:
            (root / "data").mkdir(exist_ok=True)
            (root / "data" / "config.local.json").write_text(json.dumps(local))
        return root

    def test_local_selections_win_over_tracked_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.root(tmp, {"display": {"theme": "night_ops"}},
                             {"display": {"theme": "retro_orange"}})
            self.assertEqual(userconf.load(root)["display"]["theme"],
                             "retro_orange")

    def test_merge_is_deep_so_siblings_survive(self):
        # A shallow merge would drop `layer` when only `theme` is overridden,
        # silently reverting an unrelated setting.
        with tempfile.TemporaryDirectory() as tmp:
            root = self.root(tmp,
                             {"display": {"theme": "night_ops",
                                          "layer": "BOTTOM"}},
                             {"display": {"theme": "retro_terminal"}})
            display = userconf.load(root)["display"]
            self.assertEqual(display["theme"], "retro_terminal")
            self.assertEqual(display["layer"], "BOTTOM")

    def test_lists_replace_wholesale_rather_than_merging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.root(tmp, {"logs": {"patterns": [1, 2, 3]}},
                             {"logs": {"patterns": [9]}})
            self.assertEqual(userconf.load(root)["logs"]["patterns"], [9])

    def test_stored_file_holds_only_overrides_not_a_snapshot(self):
        # The property that keeps config.json's defaults live: a full copy
        # would freeze them, so a later edit there would stop taking effect.
        with tempfile.TemporaryDirectory() as tmp:
            root = self.root(tmp, {"display": {"theme": "night_ops"},
                                   "server": {"port": 4320}})
            userconf.apply_patch(root, {"display": {"theme": "retro_orange"}})
            stored = json.loads(userconf.local_path(root).read_text())
            self.assertEqual(stored, {"display": {"theme": "retro_orange"}})
            self.assertNotIn("server", stored)

    def test_a_later_default_change_still_reaches_the_running_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.root(tmp, {"display": {"theme": "night_ops"},
                                   "server": {"port": 4320}})
            userconf.apply_patch(root, {"display": {"theme": "retro_orange"}})
            # Someone edits the tracked defaults afterwards.
            (root / "config.json").write_text(json.dumps(
                {"display": {"theme": "night_ops"}, "server": {"port": 9999}}))
            cfg = userconf.load(root)
            self.assertEqual(cfg["server"]["port"], 9999)      # default moved
            self.assertEqual(cfg["display"]["theme"], "retro_orange")  # kept

    def test_config_json_is_never_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.root(tmp, {"display": {"theme": "night_ops"}})
            before = (root / "config.json").read_bytes()
            userconf.apply_patch(root, {"display": {"theme": "retro_terminal"}})
            self.assertEqual((root / "config.json").read_bytes(), before)

    def test_written_file_ends_with_a_newline(self):
        # #73: json.dumps omits it, and its absence is what left a tracked
        # file permanently modified. Asserted on bytes -- a parsed round trip
        # cannot see a missing final byte.
        with tempfile.TemporaryDirectory() as tmp:
            root = self.root(tmp, {"display": {}})
            userconf.apply_patch(root, {"display": {"theme": "retro_orange"}})
            self.assertTrue(
                userconf.local_path(root).read_bytes().endswith(b"\n"))

    def test_no_temp_file_is_left_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.root(tmp, {"display": {}})
            userconf.apply_patch(root, {"display": {"theme": "retro_orange"}})
            leftovers = list((root / "data").glob("*.tmp"))
            self.assertEqual(leftovers, [])

    def test_missing_local_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.root(tmp, {"display": {"theme": "night_ops"}})
            self.assertEqual(userconf.load(root)["display"]["theme"],
                             "night_ops")

    def test_corrupt_local_file_degrades_to_the_defaults(self):
        # A broken selections file must not take the whole dashboard down:
        # the tracked defaults alone are a complete, working configuration.
        with tempfile.TemporaryDirectory() as tmp:
            root = self.root(tmp, {"display": {"theme": "night_ops"}})
            (root / "data").mkdir(exist_ok=True)
            userconf.local_path(root).write_text("{ not json")
            self.assertEqual(userconf.load(root)["display"]["theme"],
                             "night_ops")

    def test_successive_patches_accumulate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.root(tmp, {"display": {}, "units": {}})
            userconf.apply_patch(root, {"display": {"theme": "retro_orange"}})
            userconf.apply_patch(root, {"units": {"clock24": False}})
            stored = json.loads(userconf.local_path(root).read_text())
            self.assertEqual(stored["display"]["theme"], "retro_orange")
            self.assertIs(stored["units"]["clock24"], False)


class TestSessionPairing(unittest.TestCase):
    """lib/sessions.pair() -- folding the start/stop journal stream into one
    record per session."""

    @staticmethod
    def ev(sid, user, ts, start):
        return {"session_id": sid, "user": user, "leader": "1",
                "ts": ts, "start": start}

    def test_start_and_stop_become_one_record(self):
        recs = sessions.pair([self.ev("3", "corbin", 100.0, True),
                              self.ev("3", "corbin", 400.0, False)])
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["login_at"], 100.0)
        self.assertEqual(recs[0]["logout_at"], 400.0)

    def test_reused_session_id_yields_two_records(self):
        # logind numbers are per-boot and can repeat after a logind restart.
        # Overwriting by id would silently erase the first login.
        recs = sessions.pair([self.ev("3", "corbin", 100.0, True),
                              self.ev("3", "corbin", 200.0, False),
                              self.ev("3", "guest", 300.0, True),
                              self.ev("3", "guest", 400.0, False)])
        self.assertEqual(len(recs), 2)
        self.assertEqual([r["user"] for r in recs], ["corbin", "guest"])
        self.assertEqual(recs[0]["logout_at"], 200.0)
        self.assertEqual(recs[1]["login_at"], 300.0)

    def test_stop_without_start_is_still_reported(self):
        # Dropping it would under-report a login that demonstrably happened.
        recs = sessions.pair([self.ev("9", "corbin", 500.0, False)])
        self.assertEqual(len(recs), 1)
        self.assertIsNone(recs[0]["login_at"])
        self.assertEqual(recs[0]["logout_at"], 500.0)

    def test_open_session_has_no_logout(self):
        recs = sessions.pair([self.ev("3", "corbin", 100.0, True)])
        self.assertIsNone(recs[0]["logout_at"])


class TestSessionShape(unittest.TestCase):
    NOW = 1_000.0

    def shape(self, events, active, **kw):
        # Patched so the filter is tested against known uids rather than
        # whatever accounts happen to exist on the machine running the suite.
        uids = {"corbin": 1000, "cosmic-greeter": 988, "root": 0}
        with patch.object(sessions, "_uid_of", lambda u: uids.get(u)):
            return sessions.shape(events, active, now=self.NOW, **kw)

    @staticmethod
    def ev(sid, user, ts, start):
        return {"session_id": sid, "user": user, "leader": "1",
                "ts": ts, "start": start}

    def test_system_uid_sessions_are_filtered_out(self):
        out = self.shape([self.ev("1", "cosmic-greeter", 10.0, True),
                          self.ev("1", "cosmic-greeter", 20.0, False),
                          self.ev("3", "corbin", 30.0, True)],
                         {"3": {"tty": "tty1"}})
        self.assertEqual([r["user"] for r in out["rows"]], ["corbin"])

    def test_unknown_user_is_kept_not_filtered(self):
        # A session for an account that no longer exists is exactly what this
        # panel is worth having for, so it must survive the "human" filter.
        out = self.shape([self.ev("4", "deleted-acct", 30.0, True),
                          self.ev("4", "deleted-acct", 40.0, False)], {})
        self.assertEqual([r["user"] for r in out["rows"]], ["deleted-acct"])
        self.assertIsNone(out["rows"][0]["uid"])

    def test_active_session_duration_runs_to_now(self):
        out = self.shape([self.ev("3", "corbin", 400.0, True)],
                         {"3": {"tty": "tty1", "type": "wayland"}})
        row = out["rows"][0]
        self.assertTrue(row["active"])
        self.assertEqual(row["duration_s"], 600)
        self.assertEqual(row["tty"], "tty1")

    def test_open_record_absent_from_roster_is_ended_with_unknown_duration(self):
        # The session is over and the journal cannot say when. Reporting it as
        # active would render a duration that grows forever for a dead session.
        out = self.shape([self.ev("7", "corbin", 400.0, True)], {})
        row = out["rows"][0]
        self.assertFalse(row["active"])
        self.assertTrue(row["ended_unknown"])
        self.assertIsNone(row["duration_s"])

    def test_closed_session_duration_is_the_recorded_span(self):
        out = self.shape([self.ev("3", "corbin", 100.0, True),
                          self.ev("3", "corbin", 250.0, False)], {})
        self.assertEqual(out["rows"][0]["duration_s"], 150)
        self.assertFalse(out["rows"][0]["ended_unknown"])

    def test_active_sorts_above_a_more_recent_ended_session(self):
        out = self.shape([self.ev("3", "corbin", 100.0, True),
                          self.ev("5", "corbin", 800.0, True),
                          self.ev("5", "corbin", 900.0, False)],
                         {"3": {"tty": "tty1"}})
        self.assertEqual([r["session_id"] for r in out["rows"]], ["3", "5"])
        self.assertEqual(out["totals"]["active"], 1)

    def test_ended_sessions_sort_newest_first(self):
        out = self.shape([self.ev("1", "corbin", 100.0, True),
                          self.ev("1", "corbin", 150.0, False),
                          self.ev("2", "corbin", 300.0, True),
                          self.ev("2", "corbin", 350.0, False)], {})
        self.assertEqual([r["session_id"] for r in out["rows"]], ["2", "1"])

    def test_max_rows_caps_and_reports_the_remainder(self):
        events = []
        for i in range(5):
            events.append(self.ev(str(i), "corbin", 100.0 + i, True))
            events.append(self.ev(str(i), "corbin", 200.0 + i, False))
        out = self.shape(events, {}, max_rows=2)
        self.assertEqual(len(out["rows"]), 2)
        self.assertEqual(out["truncated"], 3)
        # The total still counts what was dropped -- a silent cap would read
        # as "only two logins this boot".
        self.assertEqual(out["totals"]["sessions"], 5)

    def test_zero_max_rows_disables_the_cap(self):
        events = []
        for i in range(4):
            events.append(self.ev(str(i), "corbin", 100.0 + i, True))
            events.append(self.ev(str(i), "corbin", 200.0 + i, False))
        out = self.shape(events, {}, max_rows=0)
        self.assertEqual(len(out["rows"]), 4)
        self.assertEqual(out["truncated"], 0)


class TestSessionCollect(unittest.TestCase):
    def test_disabled_in_config_short_circuits(self):
        out = sessions.collect({"sessions": {"enabled": False}})
        self.assertFalse(out["available"])
        self.assertTrue(out["disabled"])
        self.assertEqual(out["rows"], [])

    def test_unavailable_journal_degrades_instead_of_raising(self):
        with patch.object(sessions, "journal_events",
                          side_effect=sessions.SessionsUnavailableError("nope")):
            out = sessions.collect({})
        self.assertFalse(out["available"])
        self.assertEqual(out["error"], "nope")
        self.assertEqual(out["rows"], [])

    def test_show_session_blocks_are_keyed_by_their_own_id(self):
        # loginctl does not emit properties in the -p order requested, so the
        # parse must key off each block's Id rather than argument position.
        parsed = sessions._parse_show_blocks(
            "Id=3\nRemote=no\nType=wayland\n\nId=7\nType=tty\nRemote=yes\n")
        self.assertEqual(set(parsed), {"3", "7"})
        self.assertEqual(parsed["3"]["Type"], "wayland")
        self.assertEqual(parsed["7"]["Remote"], "yes")

    def test_malformed_journal_lines_are_skipped(self):
        payload = "\n".join([
            "not json",
            json.dumps({"SESSION_ID": "3", "USER_ID": "corbin",
                        "MESSAGE_ID": sessions.START_ID,
                        "__REALTIME_TIMESTAMP": "1500000000000000"}),
            json.dumps({"USER_ID": "nobody"}),  # no SESSION_ID
        ])
        with patch.object(sessions, "_run", return_value=payload):
            events = sessions.journal_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["ts"], 1_500_000_000.0)
        self.assertTrue(events[0]["start"])


class TestLogJournalSource(unittest.TestCase):
    """lib/logsrc journal source *selection* (#72) -- distinct from
    TestLogSource above, which covers read_log_lines' filtering."""

    MATCHES = ["_COMM=sudo", "_COMM=sshd"]

    def test_explicit_unit_wins_over_the_broad_matches(self):
        # The Control Backend's phone-editable unit field must still narrow
        # the feed, otherwise it is a control that silently does nothing.
        argv = logsrc._journalctl_argv(
            {"journalctl_unit": "ssh", "journalctl_matches": self.MATCHES}, 50)
        self.assertIn("-u", argv)
        self.assertIn("ssh", argv)
        self.assertNotIn("_COMM=sudo", argv)

    def test_matches_are_ord_together_when_no_unit_is_set(self):
        argv = logsrc._journalctl_argv(
            {"journalctl_unit": None, "journalctl_matches": self.MATCHES}, 50)
        self.assertEqual(argv[-3:], ["_COMM=sudo", "+", "_COMM=sshd"])
        self.assertNotIn("-u", argv)

    def test_ordering_is_forced_rather_than_inherited(self):
        # -n alone is oldest-first, but -n with --grep is newest-first;
        # depending on which would invert the panel on a source change.
        argv = logsrc._journalctl_argv({"journalctl_matches": ["_COMM=sudo"]}, 50)
        self.assertIn("-r", argv)

    def test_never_uses_grep(self):
        # A system-wide --grep is a full text scan: measured 4.3s bounded to
        # 24h versus 126ms for indexed matches, and this runs per poll.
        argv = logsrc._journalctl_argv({"journalctl_matches": ["_COMM=sudo"]}, 50)
        self.assertNotIn("-g", argv)
        self.assertNotIn("--grep", argv)

    def test_match_that_could_become_a_flag_is_rejected(self):
        # "-f" would start a follow that never returns, hanging the request
        # thread. Validated as FIELD=value rather than trusted.
        kept = logsrc.journal_matches(
            {"journalctl_matches": ["-f", "--output=cat", "_COMM=sudo",
                                    "lowercase=x", 7, None]})
        self.assertEqual(kept, ["_COMM=sudo"])

    def test_no_source_configured_returns_none(self):
        self.assertIsNone(logsrc._journalctl_argv({"journalctl_unit": None}, 50))
        self.assertIsNone(logsrc._journalctl_argv({}, 50))

    def test_lines_are_reversed_back_to_most_recent_last(self):
        proc = type("P", (), {"stdout": "newest\nmiddle\noldest\n"})()
        with patch.object(logsrc.subprocess, "run", return_value=proc):
            lines = logsrc._journalctl_lines(["journalctl"])
        self.assertEqual(lines, ["oldest", "middle", "newest"])

    def test_shipped_config_matches_cover_the_local_login_path(self):
        # #72's actual defect: the shipped source could not see a failed
        # graphical login because cosmic-greeter/greetd log outside `ssh`.
        cfg = json.loads(
            (Path(__file__).resolve().parent.parent / "config.json").read_text())
        kept = logsrc.journal_matches(cfg["logs"])
        self.assertIn("_COMM=cosmic-greeter", kept)
        self.assertIn("_COMM=greetd", kept)
        self.assertIsNone(cfg["logs"]["journalctl_unit"],
                          "a set unit would override the broad matches")

    def test_failed_login_pattern_matches_a_real_pam_line(self):
        # Verbatim from this machine's journal, 2026-09-01T15:31:04.
        line = ("2026-09-01T15:31:04-07:00 cosmic-greeter[3190]: "
                "pam_unix(cosmic-greeter:auth): authentication failure; "
                "logname=corbin uid=1000 euid=1000 tty= ruser= rhost=  "
                "user=corbin")
        cfg = json.loads(
            (Path(__file__).resolve().parent.parent / "config.json").read_text())
        with patch.object(logsrc, "_journalctl_lines", return_value=[line]):
            matched = logsrc.read_log_lines(cfg)
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["status"], "critical")
        self.assertEqual(matched[0]["label"], "Failed login")


if __name__ == "__main__":
    unittest.main()
