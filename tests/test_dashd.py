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
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import psutil

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
        orig_weather, orig_devices, orig_logs = (
            dashd.cached_weather, dashd.cached_devices, dashd.logsrc.read_log_lines)
        dashd.cached_weather = lambda _c: {"stale": False, "unavailable": True}
        dashd.cached_devices = lambda _c: {"gateway": None, "devices": []}
        dashd.logsrc.read_log_lines = lambda _c: []
        try:
            st = dashd.build_state(cfg)
        finally:
            dashd.cached_weather = orig_weather
            dashd.cached_devices = orig_devices
            dashd.logsrc.read_log_lines = orig_logs
        for key in ("ts", "config", "weather", "sys", "music", "tasks", "devices",
                    "devices_gateway", "devices_scanning", "logs", "extra"):
            self.assertIn(key, st)
        self.assertEqual(st["devices"], [])
        self.assertIsNone(st["devices_gateway"])
        self.assertFalse(st["devices_scanning"])
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


class TestDevices(unittest.TestCase):
    """dashd.devices is lib/devices.py (#27), re-exported the same way
    dashd.sysinfo is -- patch the subprocess module it actually imported.

    target_is_local() is patched True in most of these: they're testing
    the grepable-output parser and the subprocess-failure fallback, not
    the local-subnet safety check (that's TestDevicesTargetIsLocal /
    TestDevicesRefusesNonLocalTarget below) -- without the patch, whether
    192.168.1.0/24 happens to overlap the machine actually running the
    test suite would make these flaky by environment."""

    _GREP_OUTPUT = (
        "Host: 192.168.1.5 (router.lan)\tStatus: Up\n"
        "Host: 192.168.1.9 ()\tStatus: Up\n"
        "Host: 192.168.1.20 ()\tStatus: Down\n"
    )

    def test_parses_up_hosts_from_grepable_output(self):
        fake = type("R", (), {"stdout": self._GREP_OUTPUT})()
        with patch.object(dashd.devices, "target_is_local", return_value=True), \
                patch.object(dashd.devices, "arp_table", return_value={}), \
                patch.object(dashd.devices, "_reverse_dns", return_value=""), \
                patch.object(dashd.devices, "_mdns_resolve", return_value=""), \
                patch.object(dashd.devices.subprocess, "run", return_value=fake):
            found = dashd.devices.scan_devices("192.168.1.0/24")
        self.assertEqual(found, [
            {"ip": "192.168.1.5", "mac": None,
             "hostname": "router.lan", "status": "up"},
            {"ip": "192.168.1.9", "mac": None, "hostname": "", "status": "up"},
        ])

    def test_missing_hostname_falls_back_to_reverse_dns(self):
        fake = type("R", (), {"stdout": self._GREP_OUTPUT})()
        with patch.object(dashd.devices, "target_is_local", return_value=True), \
                patch.object(dashd.devices, "arp_table",
                              return_value={"192.168.1.9": "aa:bb:cc:dd:ee:ff"}), \
                patch.object(dashd.devices, "_reverse_dns", return_value="nas.local"), \
                patch.object(dashd.devices.subprocess, "run", return_value=fake):
            found = dashd.devices.scan_devices("192.168.1.0/24")
        self.assertEqual(found[1], {
            "ip": "192.168.1.9", "mac": "aa:bb:cc:dd:ee:ff",
            "hostname": "nas.local", "status": "up"})

    def test_reverse_dns_miss_falls_back_to_mdns(self):
        # Real-world case (user report: "the devices are not reporting
        # their names") -- this network's router doesn't run PTR records
        # for DHCP clients at all, so _reverse_dns() alone found nothing
        # for anything but this machine and the gateway; mDNS/avahi picks
        # up phones/printers/etc that DNS never will.
        fake = type("R", (), {"stdout": self._GREP_OUTPUT})()
        with patch.object(dashd.devices, "target_is_local", return_value=True), \
                patch.object(dashd.devices, "arp_table", return_value={}), \
                patch.object(dashd.devices, "_reverse_dns", return_value=""), \
                patch.object(dashd.devices, "_mdns_resolve",
                              return_value="phone.local"), \
                patch.object(dashd.devices.subprocess, "run", return_value=fake):
            found = dashd.devices.scan_devices("192.168.1.0/24")
        self.assertEqual(found[1]["hostname"], "phone.local")

    def test_no_resolver_finds_anything_hostname_stays_empty(self):
        fake = type("R", (), {"stdout": self._GREP_OUTPUT})()
        with patch.object(dashd.devices, "target_is_local", return_value=True), \
                patch.object(dashd.devices, "arp_table", return_value={}), \
                patch.object(dashd.devices, "_reverse_dns", return_value=""), \
                patch.object(dashd.devices, "_mdns_resolve", return_value=""), \
                patch.object(dashd.devices.subprocess, "run", return_value=fake):
            found = dashd.devices.scan_devices("192.168.1.0/24")
        self.assertEqual(found[1]["hostname"], "")

    def test_missing_nmap_returns_empty_list_not_an_error(self):
        with patch.object(dashd.devices, "target_is_local", return_value=True), \
                patch.object(dashd.devices.subprocess, "run",
                              side_effect=FileNotFoundError("no nmap")):
            self.assertEqual(dashd.devices.scan_devices("192.168.1.0/24"), [])


class TestDevicesArpAndGateway(unittest.TestCase):
    """arp_table() / default_gateway() -- parse `ip neigh show` / `ip route
    show default` output. Patches devices._run (the shared subprocess
    helper) rather than shelling out for real, so these don't depend on
    the test machine's actual network state."""

    _NEIGH_OUTPUT = (
        "10.1.1.1 dev wlo1 lladdr 2c:f0:5d:f9:bc:f6 REACHABLE\n"
        "10.1.1.15 dev wlo1 lladdr e0:51:63:01:f8:0a STALE\n"
        "10.1.1.4 dev wlo1  FAILED\n"  # no lladdr -- unresolved, must be skipped
    )
    _ROUTE_OUTPUT = (
        "default via 10.1.1.1 dev wlo1 proto dhcp src 10.1.1.38 metric 600\n")

    def test_arp_table_parses_lladdr_and_skips_unresolved(self):
        with patch.object(dashd.devices, "_run", return_value=self._NEIGH_OUTPUT):
            table = dashd.devices.arp_table()
        self.assertEqual(table, {
            "10.1.1.1": "2c:f0:5d:f9:bc:f6",
            "10.1.1.15": "e0:51:63:01:f8:0a",
        })

    def test_default_gateway_resolves_mac_from_arp_table(self):
        def fake_run(cmd, timeout=5):
            if cmd[:2] == ["ip", "route"]:
                return self._ROUTE_OUTPUT
            return self._NEIGH_OUTPUT
        with patch.object(dashd.devices, "_run", side_effect=fake_run):
            gw = dashd.devices.default_gateway()
        self.assertEqual(
            gw, {"ip": "10.1.1.1", "mac": "2c:f0:5d:f9:bc:f6", "iface": "wlo1"})

    def test_no_default_route_returns_none(self):
        with patch.object(dashd.devices, "_run", return_value=""):
            self.assertIsNone(dashd.devices.default_gateway())


class TestIpv6Neighbors(unittest.TestCase):
    """ipv6_neighbors() -- the IPv6 analog of arp_table(), added for user
    request "I want ipv6 to be the main way to identify devices". Verified
    live against the real network this shipped against: `ip -6 neigh show`
    was empty at rest, and had 9 entries moments after pinging ff02::1."""

    # `ip -6 neigh show dev <iface>` (filtered by interface, what
    # ipv6_neighbors() actually runs) omits the "dev <iface>" field each
    # line would otherwise carry -- confirmed against the real command
    # live. Using the unfiltered format here (which DOES carry "dev
    # wlo1") would silently mask a real regression: an earlier version of
    # this fixture did exactly that and let a real live bug (the filtered
    # form matched nothing at all) slip past this test.
    _NEIGH6_OUTPUT = (
        "fe80::2ef0:5dff:fef9:bcf6 lladdr 2c:f0:5d:f9:bc:f6 router STALE\n"
        "fe80::d6ab:cdff:fe67:718d lladdr d4:ab:cd:67:71:8d STALE\n"
        "fe80::9999  FAILED\n"  # no lladdr -- unresolved, must be skipped
    )

    def test_parses_lladdr_keyed_by_mac(self):
        with patch.object(dashd.devices, "_run",
                           return_value=self._NEIGH6_OUTPUT) as run:
            table = dashd.devices.ipv6_neighbors("wlo1")
        self.assertEqual(table, {
            "2c:f0:5d:f9:bc:f6": "fe80::2ef0:5dff:fef9:bcf6",
            "d4:ab:cd:67:71:8d": "fe80::d6ab:cdff:fe67:718d",
        })
        # Pings the all-nodes multicast group on the given interface before
        # reading the neighbor cache, to freshen it -- verify both calls
        # happened, not just the one whose output got parsed.
        cmds = [c.args[0] for c in run.call_args_list]
        self.assertTrue(any(c[:3] == ["ping", "-6", "-c"] for c in cmds))
        self.assertTrue(any(c[:4] == ["ip", "-6", "neigh", "show"] for c in cmds))

    def test_empty_neighbor_cache_is_empty_dict(self):
        with patch.object(dashd.devices, "_run", return_value=""):
            self.assertEqual(dashd.devices.ipv6_neighbors("wlo1"), {})


class TestMdnsResolve(unittest.TestCase):
    """_mdns_resolve() -- parses `avahi-resolve -a` output, the fallback
    name source added after user report ("the devices are not reporting
    their names"): this network's router has no DNS PTR records for DHCP
    clients at all, but several devices answer mDNS instead."""

    def test_parses_successful_resolution(self):
        fake = type("R", (), {
            "returncode": 0, "stdout": "10.1.1.18\tAndroid_2MECJMIY.local\n"})()
        with patch.object(dashd.devices.subprocess, "run", return_value=fake):
            self.assertEqual(
                dashd.devices._mdns_resolve("10.1.1.18"), "Android_2MECJMIY.local")

    def test_failed_resolution_is_empty_string(self):
        fake = type("R", (), {"returncode": 1, "stdout": ""})()
        with patch.object(dashd.devices.subprocess, "run", return_value=fake):
            self.assertEqual(dashd.devices._mdns_resolve("10.1.1.99"), "")

    def test_missing_avahi_resolve_binary_is_empty_not_an_error(self):
        with patch.object(dashd.devices.subprocess, "run",
                           side_effect=FileNotFoundError("no avahi-resolve")):
            self.assertEqual(dashd.devices._mdns_resolve("10.1.1.18"), "")

    def test_hang_is_bounded_by_timeout_not_left_running(self):
        with patch.object(dashd.devices.subprocess, "run",
                           side_effect=subprocess.TimeoutExpired("avahi-resolve", 1.5)):
            self.assertEqual(dashd.devices._mdns_resolve("10.1.1.18"), "")


class TestDevicesRegistry(unittest.TestCase):
    """load_registry()/merge_scan()/devices_for_network() -- the "remember
    devices" behavior (user request): a device missing from a scan is
    marked offline, not dropped, and everything is scoped per-network so
    hopping networks doesn't leak one network's devices into another's.
    Registry shape is a list of gateways each with a list of devices
    (user request: "structured data" -- "a list of gateways, and each
    gateway should have a list of connected devices"), not the dict-keyed-
    by-opaque-id shape this used before."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "devices.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_load_registry_missing_file_is_empty(self):
        self.assertEqual(dashd.devices.load_registry(self.path), {"gateways": []})

    def test_load_registry_tolerates_corrupt_json(self):
        self.path.write_text("{not json")
        self.assertEqual(dashd.devices.load_registry(self.path), {"gateways": []})

    def test_load_registry_tolerates_older_dict_shape(self):
        # The dict-keyed-by-network-id shape this registry used before the
        # "structured data" follow-up, and the even older pre-registry
        # flat list before that -- both must be treated as "nothing
        # remembered yet", not corrupt data merge_scan() might choke on.
        self.path.write_text(json.dumps({"networks": {"aa:bb": {"devices": {}}}}))
        self.assertEqual(dashd.devices.load_registry(self.path), {"gateways": []})
        self.path.write_text(json.dumps({"devices": [], "fetched_at": 1.0}))
        self.assertEqual(dashd.devices.load_registry(self.path), {"gateways": []})

    def test_network_key_prefers_gateway_mac(self):
        self.assertEqual(
            dashd.devices.network_key({"ip": "10.1.1.1", "mac": "aa:bb:cc:dd:ee:ff"}),
            "aa:bb:cc:dd:ee:ff")

    def test_network_key_falls_back_to_gateway_ip(self):
        self.assertEqual(
            dashd.devices.network_key({"ip": "10.1.1.1", "mac": None}), "gw:10.1.1.1")

    def test_network_key_none_without_a_gateway(self):
        self.assertIsNone(dashd.devices.network_key(None))

    def test_find_gateway_by_id(self):
        registry = {"gateways": [{"id": "aa:aa:aa:aa:aa:aa", "devices": []}]}
        self.assertIsNotNone(dashd.devices.find_gateway(registry, "aa:aa:aa:aa:aa:aa"))
        self.assertIsNone(dashd.devices.find_gateway(registry, "bb:bb:bb:bb:bb:bb"))

    def test_merge_scan_creates_one_gateway_entry_per_network(self):
        gw = {"ip": "10.1.1.1", "mac": "aa:aa:aa:aa:aa:aa"}
        registry = {"gateways": []}
        dashd.devices.merge_scan(registry, gw, [], now=1000.0)
        dashd.devices.merge_scan(registry, gw, [], now=2000.0)  # same network again
        self.assertEqual(len(registry["gateways"]), 1)
        self.assertEqual(registry["gateways"][0]["id"], "aa:aa:aa:aa:aa:aa")
        self.assertEqual(registry["gateways"][0]["last_scan"], 2000.0)

    def test_gateway_ipv6_stored_and_preserved_when_not_resupplied(self):
        gw = {"ip": "10.1.1.1", "mac": "aa:aa:aa:aa:aa:aa"}
        registry = {"gateways": []}
        dashd.devices.merge_scan(registry, gw, [], gateway_ipv6="fe80::1", now=1000.0)
        self.assertEqual(registry["gateways"][0]["ipv6"], "fe80::1")
        # A later scan that couldn't resolve the gateway's IPv6 this round
        # (gateway_ipv6=None) must not blank out what was already known.
        dashd.devices.merge_scan(registry, gw, [], gateway_ipv6=None, now=2000.0)
        self.assertEqual(registry["gateways"][0]["ipv6"], "fe80::1")

    def test_device_ipv6_layered_in_and_preserved(self):
        gw = {"ip": "10.1.1.1", "mac": "aa:aa:aa:aa:aa:aa"}
        registry = {"gateways": []}
        dashd.devices.merge_scan(registry, gw, [
            {"ip": "10.1.1.5", "mac": "11:11:11:11:11:11", "hostname": "",
             "ipv6": "fe80::5"},
        ], now=1000.0)
        # A later scan where this device's ipv6 wasn't resolved must keep
        # the previously-known address rather than blanking it.
        dashd.devices.merge_scan(registry, gw, [
            {"ip": "10.1.1.5", "mac": "11:11:11:11:11:11",
             "hostname": "", "ipv6": None},
        ], now=2000.0)
        found = dashd.devices.devices_for_network(registry, gw)
        self.assertEqual(found[0]["ipv6"], "fe80::5")

    def test_name_override_is_never_set_or_cleared_by_a_scan(self):
        gw = {"ip": "10.1.1.1", "mac": "aa:aa:aa:aa:aa:aa"}
        registry = {"gateways": [{
            "id": "aa:aa:aa:aa:aa:aa", "ip": "10.1.1.1", "ipv6": None, "last_scan": 0,
            "devices": [{
                "mac": "11:11:11:11:11:11", "ip": "10.1.1.5", "ipv6": None,
                "hostname": "", "name_override": "Living Room TV",
                "status": "offline", "first_seen": 0, "last_seen": 0,
            }],
        }]}
        dashd.devices.merge_scan(registry, gw, [
            {"ip": "10.1.1.5", "mac": "11:11:11:11:11:11", "hostname": "roku-abc123"},
        ], now=1000.0)

        found = dashd.devices.devices_for_network(registry, gw)
        self.assertEqual(found[0]["name_override"], "Living Room TV")
        self.assertEqual(found[0]["hostname"], "roku-abc123")  # still resolves normally

    def test_device_missing_from_a_later_scan_goes_offline_not_dropped(self):
        gw = {"ip": "10.1.1.1", "mac": "aa:aa:aa:aa:aa:aa"}
        registry = {"gateways": []}
        dashd.devices.merge_scan(registry, gw, [
            {"ip": "10.1.1.5", "mac": "11:11:11:11:11:11", "hostname": "phone"},
        ], now=1000.0)
        dashd.devices.merge_scan(registry, gw, [], now=2000.0)  # phone didn't answer

        found = dashd.devices.devices_for_network(registry, gw)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["status"], "offline")
        self.assertEqual(found[0]["first_seen"], 1000.0)
        self.assertEqual(found[0]["last_seen"], 1000.0)  # unchanged -- not seen again

    def test_device_seen_again_goes_back_to_up(self):
        gw = {"ip": "10.1.1.1", "mac": "aa:aa:aa:aa:aa:aa"}
        registry = {"gateways": []}
        found = [{"ip": "10.1.1.5", "mac": "11:11:11:11:11:11", "hostname": "phone"}]
        dashd.devices.merge_scan(registry, gw, found, now=1000.0)
        dashd.devices.merge_scan(registry, gw, [], now=2000.0)
        dashd.devices.merge_scan(registry, gw, found, now=3000.0)

        result = dashd.devices.devices_for_network(registry, gw)
        self.assertEqual(result[0]["status"], "up")
        self.assertEqual(result[0]["last_seen"], 3000.0)

    def test_devices_scoped_to_current_network_only(self):
        gw_home = {"ip": "10.1.1.1", "mac": "aa:aa:aa:aa:aa:aa"}
        gw_cafe = {"ip": "192.168.5.1", "mac": "bb:bb:bb:bb:bb:bb"}
        registry = {"gateways": []}
        dashd.devices.merge_scan(registry, gw_home, [
            {"ip": "10.1.1.5", "mac": "11:11:11:11:11:11", "hostname": "phone"},
        ], now=1000.0)

        # Hopping to a different network must not show home's devices,
        # remembered as offline or otherwise.
        self.assertEqual(dashd.devices.devices_for_network(registry, gw_cafe), [])
        self.assertEqual(len(dashd.devices.devices_for_network(registry, gw_home)), 1)

    def test_devices_for_network_sorts_up_before_offline_then_by_ip(self):
        gw = {"ip": "10.1.1.1", "mac": "aa:aa:aa:aa:aa:aa"}
        registry = {"gateways": []}
        dashd.devices.merge_scan(registry, gw, [
            {"ip": "10.1.1.20", "mac": "22:22:22:22:22:22", "hostname": ""},
            {"ip": "10.1.1.5", "mac": "11:11:11:11:11:11", "hostname": ""},
        ], now=1000.0)
        dashd.devices.merge_scan(registry, gw, [
            {"ip": "10.1.1.5", "mac": "11:11:11:11:11:11", "hostname": ""},
        ], now=2000.0)  # .20 now offline

        ordered = dashd.devices.devices_for_network(registry, gw)
        self.assertEqual([(d["ip"], d["status"]) for d in ordered],
                          [("10.1.1.5", "up"), ("10.1.1.20", "offline")])

    def test_devices_for_network_empty_without_a_gateway(self):
        self.assertEqual(dashd.devices.devices_for_network({"gateways": []}, None), [])


class TestDevicesSelf(unittest.TestCase):
    """self_device() / devices_for_network()'s self-pinning -- user
    request: "Devices should show THIS device on the first line ... it's
    technically on and connected". Fakes psutil.net_if_addrs() and
    socket.gethostname() (applied for every test via enterContext, since
    all of them need this same identity) so this doesn't depend on the
    test machine's real interfaces/hostname."""

    _GATEWAY = {"ip": "10.1.1.1", "mac": "aa:aa:aa:aa:aa:aa", "iface": "wlo1"}
    _MY_ADDRS = {"wlo1": [
        type("A", (), {"family": socket.AF_INET, "address": "10.1.1.38",
                        "netmask": "255.255.254.0"})(),
        type("A", (), {"family": socket.AF_INET6,
                        "address": "fe80::1234%wlo1", "netmask": None})(),
        # A global/temporary address too, to confirm self_device() ignores
        # it in favor of the link-local one (see its docstring: privacy-
        # extension addresses rotate, link-local doesn't).
        type("A", (), {"family": socket.AF_INET6,
                        "address": "fdb4:f832::abcd", "netmask": None})(),
        type("A", (), {"family": psutil.AF_LINK, "address": "F8:3D:C6:BF:50:70",
                        "netmask": None})(),
    ]}

    def setUp(self):
        self.enterContext(patch.object(
            dashd.devices.psutil, "net_if_addrs", return_value=self._MY_ADDRS))
        self.enterContext(patch.object(
            dashd.devices.socket, "gethostname", return_value="cim-hp-flip"))

    def test_self_device_reads_the_gateways_interface(self):
        me = dashd.devices.self_device(self._GATEWAY)
        self.assertEqual(me, {
            "mac": "f8:3d:c6:bf:50:70", "ip": "10.1.1.38",
            "ipv6": "fe80::1234", "hostname": "cim-hp-flip",
        })

    def test_self_device_none_without_gateway_or_interface(self):
        self.assertIsNone(dashd.devices.self_device(None))
        self.assertIsNone(dashd.devices.self_device({"ip": "10.1.1.1", "iface": None}))

    def test_self_device_leads_the_list_even_though_ip_sorts_later(self):
        registry = {"gateways": []}
        dashd.devices.merge_scan(registry, self._GATEWAY, [
            {"ip": "10.1.1.5", "mac": "11:11:11:11:11:11", "hostname": "phone"},
        ], now=1000.0)
        result = dashd.devices.devices_for_network(registry, self._GATEWAY)
        self.assertEqual([d["ip"] for d in result], ["10.1.1.38", "10.1.1.5"])
        self.assertEqual(result[0]["status"], "up")

    def test_self_device_matched_by_mac_gets_its_ipv6_filled_in(self):
        # Real-world case: a previous IPv4-only scan (or one where NDP
        # hadn't resolved this device's ipv6 yet) recorded this machine
        # with mac but no ipv6 -- self_device() should still recognize it
        # (by mac, not ip) and backfill the ipv6 it already knows.
        registry = {"gateways": []}
        dashd.devices.merge_scan(registry, self._GATEWAY, [
            {"ip": "10.1.1.38", "mac": "f8:3d:c6:bf:50:70",
             "hostname": "cim-hp-flip", "ipv6": None},
        ], now=1000.0)
        result = dashd.devices.devices_for_network(registry, self._GATEWAY)
        self.assertEqual(result[0]["ipv6"], "fe80::1234")

    def test_self_device_forced_up_even_if_scan_marked_it_offline(self):
        registry = {"gateways": []}
        dashd.devices.merge_scan(registry, self._GATEWAY, [
            {"ip": "10.1.1.38", "mac": None, "hostname": "cim-hp-flip"},
        ], now=1000.0)
        dashd.devices.merge_scan(registry, self._GATEWAY, [], now=2000.0)  # -> offline

        result = dashd.devices.devices_for_network(registry, self._GATEWAY)
        self.assertEqual(result[0]["ip"], "10.1.1.38")
        self.assertEqual(result[0]["status"], "up")

    def test_self_device_synthesized_when_no_scan_has_run_yet(self):
        result = dashd.devices.devices_for_network({"gateways": []}, self._GATEWAY)
        self.assertEqual(result, [{
            "mac": "f8:3d:c6:bf:50:70", "ip": "10.1.1.38", "ipv6": "fe80::1234",
            "hostname": "cim-hp-flip", "name_override": None, "status": "up",
            "first_seen": result[0]["first_seen"], "last_seen": result[0]["last_seen"],
        }])


class TestDevicesTargetIsLocal(unittest.TestCase):
    """target_is_local() / local_networks() -- the safety check added after
    a real incident: the shipped scan_target (192.168.1.0/24) didn't match
    the machine's actual network, so it ping-swept a subnet the user isn't
    even on. Fakes psutil.net_if_addrs() so this is deterministic
    regardless of whatever network the test suite actually runs on."""

    _FAKE_ADDRS = {
        "lo": [type("A", (), {"family": socket.AF_INET,
                               "address": "127.0.0.1", "netmask": "255.0.0.0"})()],
        "wlan0": [type("A", (), {
            "family": socket.AF_INET, "address": "10.1.1.38",
            "netmask": "255.255.254.0"})()],
    }

    def test_local_networks_excludes_loopback(self):
        with patch.object(dashd.devices.psutil, "net_if_addrs",
                           return_value=self._FAKE_ADDRS):
            nets = dashd.devices.local_networks()
        self.assertEqual([str(n) for n in nets], ["10.1.0.0/23"])

    def test_target_within_local_subnet_is_local(self):
        with patch.object(dashd.devices.psutil, "net_if_addrs",
                           return_value=self._FAKE_ADDRS):
            self.assertTrue(dashd.devices.target_is_local("10.1.1.0/24"))

    def test_target_outside_every_local_subnet_is_not_local(self):
        with patch.object(dashd.devices.psutil, "net_if_addrs",
                           return_value=self._FAKE_ADDRS):
            self.assertFalse(dashd.devices.target_is_local("192.168.1.0/24"))

    def test_unparseable_target_is_unknown_not_rejected(self):
        with patch.object(dashd.devices.psutil, "net_if_addrs",
                           return_value=self._FAKE_ADDRS):
            self.assertIsNone(dashd.devices.target_is_local("10.1.1.1-50"))


class TestDevicesRefusesNonLocalTarget(unittest.TestCase):
    """scan_devices() must never even invoke nmap against a target it can
    positively confirm isn't local -- the actual fix for the incident, not
    just the detection logic."""

    def test_nmap_never_runs_for_a_non_local_target(self):
        with patch.object(dashd.devices, "target_is_local", return_value=False), \
                patch.object(dashd.devices.subprocess, "run") as run:
            found = dashd.devices.scan_devices("192.168.1.0/24")
        run.assert_not_called()
        self.assertEqual(found, [])


class TestDevicesScanning(unittest.TestCase):
    """devices_scanning() backs the Devices panel's spinner (#27 follow-up)
    -- true only while cached_devices()'s background nmap thread is
    actually in flight, false again once it's written the cache file.
    default_gateway() is patched throughout: cached_devices() now calls it
    on every request (#27 "remember devices" follow-up), and the real
    function shells out to `ip route`, which would make these tests depend
    on whatever network the suite happens to run on. _run_device_scan()
    calls devices.full_scan() now (not scan_devices() directly), so that's
    what gets mocked to control the background thread's result."""

    _GATEWAY = {"ip": "10.1.1.1", "mac": "aa:aa:aa:aa:aa:aa", "iface": "wlo1"}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_data = dashd.DATA
        dashd.DATA = Path(self._tmp.name)
        self._orig_full_scan = dashd.devices.full_scan
        self._orig_gateway = dashd.devices.default_gateway
        self._orig_self = dashd.devices.self_device
        dashd.devices.default_gateway = lambda: self._GATEWAY
        # These tests aren't about self-pinning (TestDevicesSelf owns
        # that) -- without this, self_device() would read the REAL test
        # machine's psutil.net_if_addrs() for the fake "wlo1" interface
        # above and (on a machine that actually has one, like the dev box
        # this suite runs on) silently add an extra row to every result.
        dashd.devices.self_device = lambda _gw: None
        dashd._devices_scanning = False

    def tearDown(self):
        dashd.DATA = self._orig_data
        dashd.devices.full_scan = self._orig_full_scan
        dashd.devices.default_gateway = self._orig_gateway
        dashd.devices.self_device = self._orig_self
        dashd._devices_scanning = False
        self._tmp.cleanup()

    def test_true_while_scan_in_flight_false_after(self):
        started = threading.Event()
        release = threading.Event()

        def fake_full_scan(_target, _gateway, timeout=30):
            started.set()
            release.wait(timeout=2)
            found = [{"ip": "10.1.1.5", "mac": "11:11:11:11:11:11",
                      "hostname": "", "ipv6": "fe80::5", "status": "up"}]
            return found, "fe80::1"

        dashd.devices.full_scan = fake_full_scan
        cfg = {"devices": {"scan_target": "10.1.1.0/24",
                            "scan_interval_seconds": 300}}

        self.assertFalse(dashd.devices_scanning())
        dashd.cached_devices(cfg)  # no cache on disk yet -> kicks off a scan
        self.assertTrue(started.wait(timeout=2), "scan never started")
        self.assertTrue(dashd.devices_scanning())

        release.set()
        for _ in range(50):
            if not dashd.devices_scanning():
                break
            time.sleep(0.05)
        self.assertFalse(dashd.devices_scanning())

        # And the scan result actually landed in the registry, scoped to
        # this network, picked up on the very next call -- including the
        # gateway's own ipv6, enriched from the registry (#27 "ipv6"
        # follow-up: default_gateway() itself deliberately stays IPv6-free).
        result = dashd.cached_devices(cfg)
        self.assertEqual(result["gateway"], {**self._GATEWAY, "ipv6": "fe80::1"})
        self.assertEqual(len(result["devices"]), 1)
        self.assertEqual(result["devices"][0]["ip"], "10.1.1.5")
        self.assertEqual(result["devices"][0]["ipv6"], "fe80::5")
        self.assertEqual(result["devices"][0]["status"], "up")

    def test_does_not_retrigger_while_a_scan_is_already_in_flight(self):
        # User request: "not re-trigger until the last sweep completes".
        # age stays >= ttl for the ENTIRE duration of the first scan (the
        # registry's last_scan doesn't update until that scan finishes and
        # writes it) -- calling cached_devices() again mid-scan must still
        # only ever have one full_scan() call in flight.
        started = threading.Event()
        release = threading.Event()
        call_count = 0

        def fake_full_scan(_target, _gateway, timeout=30):
            nonlocal call_count
            call_count += 1
            started.set()
            release.wait(timeout=2)
            return [], None

        dashd.devices.full_scan = fake_full_scan
        cfg = {"devices": {"scan_target": "10.1.1.0/24",
                            "scan_interval_seconds": 300}}

        dashd.cached_devices(cfg)  # kicks off scan #1
        self.assertTrue(started.wait(timeout=2), "scan never started")

        # Several more polls while #1 is still blocked on `release` --
        # age is still >= ttl every time (nothing has written a fresh
        # last_scan yet), so this is exactly the case that must NOT spawn
        # a second thread.
        for _ in range(5):
            dashd.cached_devices(cfg)
        self.assertEqual(call_count, 1)

        release.set()
        for _ in range(50):
            if not dashd.devices_scanning():
                break
            time.sleep(0.05)
        self.assertEqual(call_count, 1)

    def test_no_gateway_means_no_scan_and_empty_devices(self):
        dashd.devices.default_gateway = lambda: None
        cfg = {"devices": {"scan_target": "10.1.1.0/24"}}

        with patch.object(dashd.devices, "full_scan") as run:
            result = dashd.cached_devices(cfg)
        self.assertIsNone(result["gateway"])
        self.assertEqual(result["devices"], [])
        run.assert_not_called()

    def test_a_different_networks_devices_dont_leak_in(self):
        # Pre-seed the registry as if the machine had previously been on a
        # different network -- cached_devices() must not surface that.
        other_gw = {"ip": "192.168.5.1", "mac": "bb:bb:bb:bb:bb:bb"}
        registry = {"gateways": []}
        dashd.devices.merge_scan(registry, other_gw, [
            {"ip": "192.168.5.9", "mac": "22:22:22:22:22:22", "hostname": "laptop"},
        ], now=1.0)
        path = dashd.DATA / "devices.json"
        path.write_text(json.dumps(registry))

        cfg = {"devices": {"scan_target": "10.1.1.0/24",
                            "scan_interval_seconds": 99999}}
        result = dashd.cached_devices(cfg)
        self.assertEqual(result["devices"], [])


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
        self.assertIn(cfg["display"]["theme"], ("night_ops", "retro_terminal"))

    def test_layer_is_not_background(self):
        """BACKGROUND renders but is invisible under cosmic-bg. See CLAUDE.md."""
        cfg = dashd.load_config()
        self.assertNotEqual(cfg["display"]["layer"], "BACKGROUND")


if __name__ == "__main__":
    unittest.main()
