"""Shared psutil-based system sampling.

Used by both `bin/dashd-serve` (the live /api/state snapshot, full detail)
and `bin/dashd-collect` (the smaller periodic row persisted to SQLite) --
kept in one place so the two processes' net-throughput delta, real
charging-vs-plugged-in state, and cpu-temp lookup can't drift out of sync
(see #14).

Each importing process gets its own module instance, so `_net_prev`'s delta
baseline is private to that process -- exactly what's wanted, since it means
"bytes transferred since *this* process's last call", not a shared counter
two independently-timed callers would fight over.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path

import psutil

_net_prev: tuple[float, int, int] | None = None


def _disk_tree_from_psutil() -> list[dict]:
    """Fallback disk_tree() for when lsblk is missing -- mounted view only."""
    rows = []
    for part in psutil.disk_partitions(all=False):
        if part.fstype in ("squashfs", "overlay", ""):
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        rows.append({"name": part.device.rsplit("/", 1)[-1], "fstype": part.fstype,
                     "size": u.total, "pct": u.percent, "mount": part.mountpoint})
    return rows


def disk_tree() -> list[dict]:
    """Every partition lsblk knows about, with its current utilization.

    psutil.disk_partitions() only sees mounted filesystems -- an unmounted
    EFI/recovery/swap partition is invisible to it. lsblk walks the whole
    block-device tree instead. Whole-disk rows (type "disk") and non-"part"
    children (loop devices, LUKS/crypt mappings) are skipped -- they don't
    carry a meaningful utilization figure of their own; a partition with
    neither a mountpoint nor FSUSE% (e.g. a raw swap slot whose real usage
    lives on its crypt child) is skipped too rather than shown as a bare,
    dataless row. Falls back to the psutil-only view if lsblk is missing or
    fails, so a minimal/container environment still gets *something*.
    """
    try:
        out = subprocess.run(
            ["lsblk", "-J", "-b", "-o", "NAME,TYPE,SIZE,FSTYPE,FSUSE%,MOUNTPOINT"],
            capture_output=True, text=True, timeout=5, check=True)
        tree = json.loads(out.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return _disk_tree_from_psutil()

    rows: list[dict] = []

    def walk(devices: list[dict]) -> None:
        for d in devices:
            use, mount = d.get("fsuse%"), d.get("mountpoint")
            if d.get("type") == "part" and (use or mount):
                rows.append({
                    "name": d["name"], "fstype": d.get("fstype"),
                    "size": d.get("size") or 0,
                    "pct": float(use.rstrip("%")) if use else None,
                    "mount": mount,
                })
            walk(d.get("children") or [])

    walk(tree.get("blockdevices", []))
    return rows


def battery_charging(plugged: bool,
                      power_supply_dir: Path = Path("/sys/class/power_supply")) -> bool:
    """True only while the battery is *actually* charging, not merely
    plugged in -- psutil's power_plugged is AC-present, not charge state
    (see CLAUDE.md). /sys/class/power_supply/BAT*/status
    ("Charging"/"Discharging"/"Full"/"Not charging") is the real signal;
    fall back to `plugged` if no BAT node is exposed at all.
    """
    for status_path in sorted(power_supply_dir.glob("BAT*/status")):
        try:
            return status_path.read_text().strip() == "Charging"
        except OSError:
            continue
    return plugged


def _net_rates() -> tuple[float, float]:
    """(down, up) bytes/sec since this process's last call; 0 on the first."""
    global _net_prev
    now = time.time()
    io = psutil.net_io_counters()
    up = down = 0.0
    if _net_prev:
        dt = max(now - _net_prev[0], 1e-6)
        down = max(io.bytes_recv - _net_prev[1], 0) / dt
        up = max(io.bytes_sent - _net_prev[2], 0) / dt
    _net_prev = (now, io.bytes_recv, io.bytes_sent)
    return down, up


def _battery() -> dict | None:
    if (b := psutil.sensors_battery()):
        return {"pct": round(b.percent), "plugged": b.power_plugged,
                "charging": battery_charging(b.power_plugged),
                "secs_left": None if b.secsleft in
                (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN)
                else b.secsleft}
    return None


def _cpu_temp() -> float | None:
    try:
        for name in ("k10temp", "coretemp", "acpitz", "zenpower"):
            if readings := psutil.sensors_temperatures().get(name):
                return round(readings[0].current)
    except (AttributeError, OSError):
        pass
    return None


def _core_sample() -> dict:
    """cpu%/temp/freq/battery/net -- the fields common to both a full
    system_stats() snapshot and the smaller history sample_stats(), each
    computed exactly once per call so the two functions never take two
    independently-timed psutil readings of the same delta-based counter.
    """
    down, up = _net_rates()
    return {
        "cpu_pct": psutil.cpu_percent(interval=None),
        "cpu_temp_c": _cpu_temp(),
        "cpu_freq_mhz": round(f.current) if (f := psutil.cpu_freq()) else None,
        "battery": _battery(),
        "net_down": down,
        "net_up": up,
    }


def sample_stats() -> dict:
    """The subset dashd-collect persists to SQLite -- matches
    lib/metrics.COLUMNS. Cheap: no lsblk, no per-core/process detail, since
    this runs on its own timer, unattached to any view poll.
    """
    core = _core_sample()
    return {
        "cpu_pct": core["cpu_pct"],
        "cpu_temp_c": core["cpu_temp_c"],
        "cpu_freq_mhz": core["cpu_freq_mhz"],
        "mem_pct": psutil.virtual_memory().percent,
        "battery_pct": core["battery"]["pct"] if core["battery"] else None,
        "net_down": round(core["net_down"]),
        "net_up": round(core["net_up"]),
    }


def system_stats() -> dict:
    """Full live snapshot for /api/state -- everything sample_stats() covers
    plus disks, per-core detail, load, swap, uptime, process count, host.
    """
    now = time.time()
    core = _core_sample()
    vm, sw = psutil.virtual_memory(), psutil.swap_memory()
    return {
        "cpu": core["cpu_pct"],
        "cpu_cores": psutil.cpu_percent(interval=None, percpu=True),
        "cpu_freq": core["cpu_freq_mhz"],
        "load": list(os.getloadavg()),
        "mem": {"pct": vm.percent, "used": vm.used, "total": vm.total},
        "swap": {"pct": sw.percent, "used": sw.used, "total": sw.total},
        "disks": sorted(disk_tree(), key=lambda d: d["name"]),
        "net": {"up": round(core["net_up"]), "down": round(core["net_down"])},
        "battery": core["battery"],
        "temp_c": core["cpu_temp_c"],
        "uptime": round(now - psutil.boot_time()),
        "procs": len(psutil.pids()),
        "host": socket.gethostname(),
    }
