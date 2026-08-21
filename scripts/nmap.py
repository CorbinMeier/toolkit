#!/usr/bin/env python3
"""Manual on-demand LAN device scan (`npm`/`pnpm run nmap`).

Runs the same nmap -sn ping sweep dashd-serve's background thread runs
periodically (lib/devices.py), against config.json's devices.scan_target,
and merges the result into data/devices.json's per-network registry (same
load_registry()/merge_scan() dashd-serve itself uses -- writing the old
flat {"devices": [...]} shape here would clobber whatever dashd-serve had
remembered) so the running dashboard picks it up on its next poll instead
of waiting out scan_interval_seconds.

Standalone -- this is a separate process from dashd-serve, so it doesn't
(and can't cheaply) drive the live Devices panel's scanning spinner; that
spinner tracks dashd-serve's own background scan thread (#27 follow-up),
which is what actually matters during normal operation.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import devices  # noqa: E402 -- needs ROOT/lib on sys.path first

CONFIG = ROOT / "config.json"
DATA = ROOT / "data"


def main() -> int:
    cfg = json.loads(CONFIG.read_text())
    target = cfg.get("devices", {}).get("scan_target")
    if not target:
        print("config.json has no devices.scan_target set", file=sys.stderr)
        return 1

    gateway = devices.default_gateway()
    if not gateway:
        print("no default network route -- not connected to a network", file=sys.stderr)
        return 1

    print(f"scanning {target} (network: {gateway['ip']}) ...")
    found = devices.scan_devices(target)

    DATA.mkdir(exist_ok=True)
    path = DATA / "devices.json"
    registry = devices.load_registry(path)
    devices.merge_scan(registry, gateway, found)
    path.write_text(json.dumps(registry))

    if not found:
        print("no devices found")
        return 0
    for d in found:
        label = d["hostname"] or "—"
        print(f"  {d['ip']:<15} {d['mac'] or '(no mac)':<19} {label}")
    print(f"{len(found)} device(s) up")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
