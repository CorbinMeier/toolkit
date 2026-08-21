"""LAN device-presence scanning for the Networked Devices panel (#27).

nmap -sn is a ping sweep (ARP on a local subnet, ICMP/TCP otherwise) -- no
raw-socket privileges needed for host discovery, unlike a SYN port scan
(-sS), so this runs fine as the same unprivileged user as the rest of the
dashboard. Parsed from `-oG -` (grepable output), which is stable and easy
to parse without an XML dependency.
"""
from __future__ import annotations

import re
import subprocess

_HOST_RE = re.compile(
    r"^Host:\s+(?P<ip>\S+)\s+(?:\((?P<hostname>[^)]*)\))?\s+Status:\s+(?P<status>\S+)")


def scan_devices(target: str, timeout: int = 30) -> list[dict]:
    """One-shot ping sweep of `target` (CIDR or range). Returns
    [{"ip", "hostname", "status"}, ...] for hosts nmap reports as up.

    Never raises for the caller's benefit -- a missing `nmap` binary or a
    scan timeout means "no devices this round", not a dead panel.
    """
    try:
        out = subprocess.run(
            ["nmap", "-sn", "-oG", "-", target],
            capture_output=True, text=True, timeout=timeout, check=True)
    except (OSError, subprocess.SubprocessError):
        return []

    devices = []
    for line in out.stdout.splitlines():
        m = _HOST_RE.match(line)
        if not m or m.group("status") != "Up":
            continue
        hostname = m.group("hostname") or ""
        devices.append({
            "ip": m.group("ip"),
            "hostname": hostname if hostname != m.group("ip") else "",
            "status": "up",
        })
    return devices
