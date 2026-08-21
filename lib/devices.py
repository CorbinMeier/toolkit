"""LAN device-presence scanning for the Networked Devices panel (#27).

nmap -sn is a ping sweep (ARP on a local subnet, ICMP/TCP otherwise) -- no
raw-socket privileges needed for host discovery, unlike a SYN port scan
(-sS), so this runs fine as the same unprivileged user as the rest of the
dashboard. Parsed from `-oG -` (grepable output), which is stable and easy
to parse without an XML dependency.

Also owns the "remember devices" registry (user request): nmap's own -sn
output has no stable per-device identifier -- an IP can jump between DHCP
renewals -- and no offline tracking -- a device that doesn't answer this
round just silently disappears from the list. MAC address (from the
kernel's ARP/neighbor cache, itself populated by nmap's own ping traffic)
is the actual stable identifier; the registry below keys on it, marks a
previously-seen device "offline" instead of dropping it when a scan
doesn't find it, and scopes the whole thing to the CURRENT default
gateway's identity so hopping onto a different network doesn't show a
pile of some other network's offline devices.

IPv6 is layered on top (user request: "I want ipv6 to be the main way to
identify devices") via NDP (ip_v6_neighbors()), the IPv6 analog of ARP.
There's no ARP-sweep equivalent for a whole /64 (2**64 addresses), so
IPv6 discovery instead pings the all-nodes multicast group, which
populates the kernel's neighbor cache with whatever's on the segment --
verified live, an empty cache had 9 entries a moment after that ping. MAC
is the join key between "who's up" (IPv4 sweep) and "what's their IPv6
address" (NDP), same MAC the rest of this module already treats as the
one true stable identifier.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import psutil

_HOST_RE = re.compile(
    r"^Host:\s+(?P<ip>\S+)\s+(?:\((?P<hostname>[^)]*)\))?\s+Status:\s+(?P<status>\S+)")
# "dev <iface>" is optional: `ip neigh show` (no dev filter, arp_table()'s
# case) prints it on every line, but `ip -6 neigh show dev <iface>`
# (ipv6_neighbors()'s case -- filtering BY interface) omits it, since it's
# now implied. Confirmed live: without this the filtered form matched
# nothing at all, silently returning {} from every real scan.
_NEIGH_RE = re.compile(r"^(?P<ip>\S+)(?:\s+dev\s+\S+)?.*?\blladdr\s+(?P<mac>\S+)")
_DEFAULT_ROUTE_RE = re.compile(r"^default\s+via\s+(?P<gw>\S+)\s+dev\s+(?P<iface>\S+)")
_IPV6_ALL_NODES = "ff02::1"


def _run(cmd: list[str], timeout: float = 5) -> str:
    """Best-effort subprocess run -- empty string on any failure, never
    raises. Used for the small, local, always-available `ip` commands
    below, not the nmap sweep itself (that keeps its own error handling
    in scan_devices(), which cares about a longer/configurable timeout)."""
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def local_networks() -> list[ipaddress.IPv4Network]:
    """This machine's own IPv4 subnets, one per non-loopback interface --
    the ground truth `target_is_local()` checks a scan target against."""
    nets = []
    for addrs in psutil.net_if_addrs().values():
        for a in addrs:
            if a.family != socket.AF_INET or not a.netmask or a.address == "127.0.0.1":
                continue
            try:
                nets.append(
                    ipaddress.ip_network(f"{a.address}/{a.netmask}", strict=False))
            except ValueError:
                continue
    return nets


def target_is_local(target: str) -> bool | None:
    """Whether `target` overlaps one of this machine's own subnets.

    Returns None (unknown, not rejected) for anything not parseable as a
    plain CIDR/host -- nmap also accepts ranges ("a.b.c.1-50") and
    hostnames, and this is a safety check against scanning a network the
    machine isn't even on (#config drift: a scan_target left over from a
    different location/network), not a target-syntax validator. When in
    doubt, this lets the scan through rather than blocking a legitimate
    config it doesn't understand.
    """
    try:
        network = ipaddress.ip_network(target, strict=False)
    except ValueError:
        return None
    return any(network.overlaps(local) for local in local_networks())


def arp_table() -> dict[str, str]:
    """IP -> lowercased MAC, from the kernel's neighbor/ARP cache
    (`ip neigh show`). Read-only, no root needed -- entries are populated
    as a side effect of ordinary traffic, including nmap's own -sn sweep,
    which is why this is read right after that sweep runs."""
    table = {}
    for line in _run(["ip", "neigh", "show"]).splitlines():
        m = _NEIGH_RE.match(line)
        if m:
            table[m.group("ip")] = m.group("mac").lower()
    return table


def ipv6_neighbors(iface: str, timeout: float = 2) -> dict[str, str]:
    """MAC -> IPv6 address on `iface`, the IPv6 analog of arp_table().
    IPv6 has no ARP-sweep equivalent for an entire /64 (2**64 addresses,
    vs IPv4's 254-address /24), so instead of scanning this pings the
    all-nodes multicast group (ff02::1) -- every IPv6 host on the segment
    answers a multicast ping, populating the kernel's neighbor cache
    immediately (verified live: an empty `ip -6 neigh show` had 9 entries
    moments after this ping). A device that already has a cached entry
    from other traffic is picked up too, whether or not it answered this
    particular ping.

    Deliberately NOT trying to prefer a "better" global/ULA address over
    link-local when both are cached: privacy-extension global addresses
    (RFC 4941) rotate on their own schedule, which would make a device's
    displayed identifier drift over time even though nothing about the
    device changed -- link-local is what the multicast ping actually
    elicits and is stable for as long as the device stays on this link,
    so that's what this returns.
    """
    # Best-effort -- errors (bad iface, no ping6 binary, etc.) just mean
    # the neighbor cache doesn't get freshened, not a hard failure.
    _run(["ping", "-6", "-c", "1", "-W", "1", f"{_IPV6_ALL_NODES}%{iface}"],
         timeout=timeout)
    table = {}
    for line in _run(["ip", "-6", "neigh", "show", "dev", iface]).splitlines():
        m = _NEIGH_RE.match(line)
        if m:
            table[m.group("mac").lower()] = m.group("ip")
    return table


def default_gateway() -> dict | None:
    """{"ip", "mac", "iface"} for the current default route, or None when
    there isn't one (no network connection at all). `mac` may be None if
    the ARP cache hasn't resolved the gateway yet -- callers that need a
    stable network identity should fall back to `ip` in that case (see
    network_key()). Cheap (two local `ip` reads, no network I/O of its
    own) -- safe to call on every /api/state request, unlike the nmap
    sweep, which stays on the background-thread path."""
    out = _run(["ip", "route", "show", "default"])
    m = _DEFAULT_ROUTE_RE.search(out)
    if not m:
        return None
    gw_ip = m.group("gw")
    return {"ip": gw_ip, "mac": arp_table().get(gw_ip), "iface": m.group("iface")}


def _reverse_dns(ip: str, timeout: float = 1.5) -> str:
    """Best-effort PTR lookup, used as a fallback when nmap's own
    resolution (which it does by default during -sn) didn't get a name --
    user request: "nmap is just for discovery, I'm sure there's other
    things in place for fetching names". A short timeout matters here: an
    unresolvable LAN IP can otherwise hang on the OS resolver's default
    timeout per host, multiplying across an entire subnet sweep.
    setdefaulttimeout is process-global, not just this call's -- acceptable
    for a single-purpose local dashboard process, but worth knowing if
    socket code is ever added elsewhere that shouldn't inherit it."""
    prev = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        return socket.gethostbyaddr(ip)[0]
    except (OSError, socket.herror, socket.gaierror):
        return ""
    finally:
        socket.setdefaulttimeout(prev)


def _mdns_resolve(ip: str, timeout: float = 1.5) -> str:
    """Best-effort mDNS/Bonjour lookup via `avahi-resolve -a`, tried after
    reverse-DNS -- most home routers don't run a DNS server with PTR
    records for DHCP clients at all (verified: _reverse_dns() alone
    resolved nothing for this network's actual devices), but phones,
    printers, and other LAN gear commonly answer mDNS instead. `timeout=`
    on subprocess.run actually kills the process on expiry (verified);
    avahi-resolve itself has no built-in timeout flag and can otherwise
    hang past the caller's patience on an address nobody answers for.
    Missing `avahi-resolve` binary is just "no name", not an error."""
    try:
        out = subprocess.run(
            ["avahi-resolve", "-a", ip],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0 or not out.stdout.strip():
        return ""
    # Output is "<ip>\t<name>.local" on success.
    parts = out.stdout.strip().split("\t", 1)
    return parts[1] if len(parts) == 2 else ""


def scan_devices(target: str, timeout: int = 60) -> list[dict]:
    """One-shot ping sweep of `target` (CIDR or range). Returns
    [{"ip", "mac", "hostname", "status"}, ...] for hosts nmap reports as
    up. "mac" is None when the ARP cache hasn't resolved it (e.g. the host
    is behind a router hop rather than on the local L2 segment). "hostname"
    is resolved in order: nmap's own default PTR lookup, then
    _reverse_dns() again independently, then _mdns_resolve() (mDNS/Bonjour)
    -- most home routers don't run a DNS server with PTR records for DHCP
    clients at all (confirmed on the actual network this shipped against:
    reverse-DNS alone resolved nothing), but mDNS commonly picks up phones,
    printers, and other LAN gear that DNS never will. Still "" for
    anything none of those know about.

    Refuses (returns [], nmap never runs) when `target` is a CIDR/host we
    can positively confirm is outside every local interface's subnet --
    e.g. a stale config.json scan_target left over from a different
    network. This was a real incident: the shipped default (192.168.1.0/24)
    didn't match this machine's actual network, so it was ping-sweeping a
    subnet the user isn't even part of. Logged loudly (stderr) rather than
    silently, since "no devices found" alone gives no hint the config is
    wrong -- see scripts/nmap.py, which surfaces the same warning
    interactively.

    `timeout` defaults to 60, not nmap's own fast-path speed -- a real
    scan against this shipped network was observed taking 29.71s (against
    the old 30s default), so an occasionally-slower run would silently
    truncate to zero hosts, indistinguishable from "nothing's up". This
    runs on a background thread (see bin/dashd-serve's _run_device_scan),
    so a longer budget has no cost besides slightly staler data on an
    unusually slow round.

    Otherwise never raises for the caller's benefit -- a missing `nmap`
    binary or a scan timeout means "no devices this round", not a dead
    panel.
    """
    if target_is_local(target) is False:
        print(f"devices.scan_devices: refusing to scan {target!r} -- it "
              "doesn't overlap any of this machine's own network interfaces "
              "(check config.json's devices.scan_target)", file=sys.stderr)
        return []
    try:
        out = subprocess.run(
            ["nmap", "-sn", "-oG", "-", target],
            capture_output=True, text=True, timeout=timeout, check=True)
    except (OSError, subprocess.SubprocessError):
        return []

    arp = arp_table()
    found = []
    for line in out.stdout.splitlines():
        m = _HOST_RE.match(line)
        if not m or m.group("status") != "Up":
            continue
        ip = m.group("ip")
        hostname = m.group("hostname") or ""
        if hostname == ip:
            hostname = ""
        if not hostname:
            hostname = _reverse_dns(ip)
        if not hostname:
            hostname = _mdns_resolve(ip)
        found.append({
            "ip": ip,
            "mac": arp.get(ip),
            "hostname": hostname,
            "status": "up",
        })
    return found


def full_scan(target: str, gateway: dict,
              timeout: int = 60) -> tuple[list[dict], str | None]:
    """scan_devices() plus each device's IPv6 address layered in by MAC
    (user request: "I want ipv6 to be the main way to identify devices").
    One ipv6_neighbors() call, not one per device -- it pings the whole
    segment, so a single pass already has everything it's going to get.

    Returns (devices, gateway_ipv6) -- devices gain an "ipv6" key (None if
    not resolved); gateway_ipv6 is the gateway's own address, split out
    because it isn't part of the device list and default_gateway() itself
    deliberately stays IPv6-free (it's called on every /api/state request;
    ipv6_neighbors() pings the segment and isn't cheap enough for that).
    """
    found = scan_devices(target, timeout=timeout)
    v6 = ipv6_neighbors(gateway["iface"]) if gateway.get("iface") else {}
    for d in found:
        d["ipv6"] = v6.get(d["mac"]) if d.get("mac") else None
    return found, v6.get(gateway.get("mac"))


# --------------------------------------------------------------- registry
#
# Persisted to data/devices.json by callers (bin/dashd-serve's background
# scan thread, scripts/nmap.py's manual run) -- both funnel through
# load_registry()/merge_scan() so the on-disk shape only has one owner and
# a stale pre-registry file can't corrupt a merge. A list of gateways, each
# with a list of connected devices (user request: "structured data",
# explicitly a list-of-lists rather than the dict-keyed-by-opaque-id shape
# this used before) -- easy to hand-read/hand-edit, e.g. to set a device's
# name_override, which merge_scan() always carries forward verbatim since
# it's the one field a scan never derives or overwrites. Shape:
#
#   {"gateways": [
#      {"id": "<gateway mac, or 'gw:<ip>' if the mac isn't known>",
#       "ip": "...", "ipv6": "..."|null, "last_scan": <epoch seconds>,
#       "devices": [
#         {"mac": "..."|null, "ip": "...", "ipv6": "..."|null,
#          "hostname": "...", "name_override": "..."|null,
#          "status": "up"|"offline",
#          "first_seen": <epoch seconds>, "last_seen": <epoch seconds>},
#         ...
#       ]},
#      ...
#   ]}

def load_registry(path: Path) -> dict:
    """Reads the on-disk registry, tolerating a missing/corrupt/older-shape
    (the pre-#27-follow-up dict-of-networks, or the earlier flat list)
    file -- any of those just means "nothing remembered yet", not an
    error. There's no migration: the registry is a self-healing cache, not
    a record of things that can't be re-discovered by scanning again."""
    if not path.exists():
        return {"gateways": []}
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        return {"gateways": []}
    if not isinstance(data, dict) or not isinstance(data.get("gateways"), list):
        return {"gateways": []}
    return data


def network_key(gateway: dict | None) -> str | None:
    """The registry key identifying "this network" -- the gateway's MAC
    when known (stable across DHCP/IP changes on either end), else a
    `gw:<ip>` fallback (still scopes correctly, just less robust if the
    gateway's own IP ever changes). None when there's no gateway at all
    (no network), which callers treat as "nothing to remember right now"
    rather than inventing a key for it."""
    if not gateway:
        return None
    return gateway["mac"] or f"gw:{gateway['ip']}"


def find_gateway(registry: dict, key: str) -> dict | None:
    """The registry's entry for network `key`, or None if this network has
    never been scanned. Public (not _-prefixed): bin/dashd-serve reads the
    gateway's stored ipv6/last_scan directly, same reasoning devices_for_
    network() and merge_scan() are both public."""
    for gw in registry["gateways"]:
        if gw.get("id") == key:
            return gw
    return None


def merge_scan(registry: dict, gateway: dict | None, found: list[dict],
               gateway_ipv6: str | None = None, now: float | None = None) -> None:
    """Merges a fresh scan's results into `registry` in place, scoped to
    `gateway`'s network. Devices from a previous scan on THIS network that
    didn't show up this round are marked "offline", not dropped -- that's
    the actual "remember devices" behavior. Devices under a DIFFERENT
    network's entry are untouched (that's what makes network-hopping not
    bleed one network's devices into another's).

    `name_override` is carried forward from the existing record verbatim,
    never set or cleared here -- it's the one field this function treats
    as purely user-owned (user request: "a name override which I can
    set"), so a scan can never quietly overwrite a name someone typed in.
    """
    key = network_key(gateway)
    if not key:
        return
    now = time.time() if now is None else now
    gw_rec = find_gateway(registry, key)
    if gw_rec is None:
        gw_rec = {"id": key, "ip": None, "ipv6": None, "last_scan": None, "devices": []}
        registry["gateways"].append(gw_rec)
    gw_rec["ip"] = gateway["ip"]
    if gateway_ipv6:
        gw_rec["ipv6"] = gateway_ipv6
    gw_rec["last_scan"] = now

    by_id, order = {}, []
    for d in gw_rec["devices"]:
        dev_id = d.get("mac") or d["ip"]
        by_id[dev_id] = d
        order.append(dev_id)

    seen = set()
    for d in found:
        dev_id = d.get("mac") or d["ip"]
        seen.add(dev_id)
        prev = by_id.get(dev_id, {})
        by_id[dev_id] = {
            "mac": d.get("mac"),
            "ip": d["ip"],
            "ipv6": d.get("ipv6") or prev.get("ipv6"),
            "hostname": d.get("hostname") or prev.get("hostname", ""),
            "name_override": prev.get("name_override"),
            "status": "up",
            "first_seen": prev.get("first_seen", now),
            "last_seen": now,
        }
        if dev_id not in order:
            order.append(dev_id)

    for dev_id in order:
        if dev_id not in seen:
            by_id[dev_id]["status"] = "offline"

    gw_rec["devices"] = [by_id[dev_id] for dev_id in order]


def self_device(gateway: dict | None) -> dict | None:
    """This machine's own {"mac", "ip", "ipv6", "hostname"} on `gateway`'s
    interface -- used to pin "this device" first in devices_for_network()
    (user request: it's "technically on and connected" by definition,
    since this code is what's running the scan). `ipv6` is the
    link-local address specifically (matches what ipv6_neighbors() gives
    every other device, and doesn't drift the way a privacy-extension
    global address would -- see ipv6_neighbors()'s docstring). `mac` comes
    from psutil.AF_LINK, the same interface's own hardware address, so
    devices_for_network() can match by MAC like everywhere else instead of
    falling back to comparing IPs. None without a gateway/interface."""
    if not gateway or not gateway.get("iface"):
        return None
    mac = ip = ipv6 = None
    for addr in psutil.net_if_addrs().get(gateway["iface"], []):
        if addr.family == socket.AF_INET:
            ip = addr.address
        elif addr.family == socket.AF_INET6 and addr.address.startswith("fe80"):
            ipv6 = addr.address.split("%")[0]
        elif addr.family == psutil.AF_LINK:
            mac = addr.address.lower()
    return {"mac": mac, "ip": ip, "ipv6": ipv6, "hostname": socket.gethostname()}


def devices_for_network(registry: dict, gateway: dict | None) -> list[dict]:
    """The remembered device list for `gateway`'s network only -- up
    devices and remembered-offline devices alike, sorted up-first then by
    IP, EXCEPT this machine's own entry (self_device()) always leads --
    it's the one device on the list guaranteed to be up (user request).
    Synthesized on the fly if a scan hasn't found it yet (e.g. right after
    boot, before the first sweep completes) rather than waiting for nmap
    to confirm what's already true. Empty (not an error) when there's no
    gateway or nothing's been scanned on this network yet.
    """
    key = network_key(gateway)
    if not key:
        return []
    gw_rec = find_gateway(registry, key)
    devs = list(gw_rec["devices"]) if gw_rec else []

    me = self_device(gateway)
    self_row = None
    if me:
        for i, rec in enumerate(devs):
            same = ((me["mac"] and rec.get("mac") == me["mac"])
                    or rec.get("ip") == me["ip"])
            if same:
                self_row = {
                    **rec,
                    "hostname": me["hostname"] or rec.get("hostname", ""),
                    "ipv6": rec.get("ipv6") or me.get("ipv6"),
                    "status": "up",
                }
                del devs[i]
                break
        if self_row is None:
            now = time.time()
            self_row = {"mac": me["mac"], "ip": me["ip"], "ipv6": me["ipv6"],
                        "hostname": me["hostname"], "name_override": None,
                        "status": "up", "first_seen": now, "last_seen": now}

    rest = sorted(
        devs, key=lambda d: (d["status"] != "up", ipaddress.ip_address(d["ip"])))
    return ([self_row] if self_row else []) + rest
