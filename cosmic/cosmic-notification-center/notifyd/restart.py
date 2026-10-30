"""`bin/notifyd --restart`: SIGTERM only the daemon's Python process; the shim restarts it.

Why not `killall cosmic-notifications`: that kills the shim, cosmic-session restarts the daemon
AND the panel, and its per-process exponential restart backoff grows with every restart (about
49 s after a dozen restarts). The shim's own backoff is 1 s and resets after 60 s of uptime.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

from gi.repository import Gio, GLib

from notifyd.dbus_server import CTL_IFACE, FDO_IFACE

SHIM_MARKER = "# notifyd-shim: cosmic-notification-center"
SHIM_NAME = "cosmic-notifications"
WAIT_SECONDS = 15.0
NAMES = (CTL_IFACE, FDO_IFACE)
PYTHON = "/usr/bin/python3"


class RestartError(Exception):
    pass


def _bus_call(connection, method: str, params: GLib.Variant, reply: str):
    return connection.call_sync(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        method,
        params,
        GLib.VariantType(reply),
        Gio.DBusCallFlags.NONE,
        5000,
        None,
    ).unpack()[0]


def owner_pid(connection, name: str) -> int | None:
    """PID of the connection owning `name`, None when nobody does."""
    try:
        owner = _bus_call(connection, "GetNameOwner", GLib.Variant("(s)", (name,)), "(s)")
        return _bus_call(
            connection, "GetConnectionUnixProcessID", GLib.Variant("(s)", (owner,)), "(u)"
        )
    except GLib.Error:
        return None


def _cmdline(pid: int, proc: Path) -> list[str]:
    try:
        raw = (proc / str(pid) / "cmdline").read_bytes()
    except OSError as e:
        raise RestartError(f"cannot read /proc/{pid}/cmdline: {e.strerror}") from e
    return [a for a in raw.decode("utf-8", "replace").split("\0") if a]


def _ppid(pid: int, proc: Path) -> int:
    try:
        stat = (proc / str(pid) / "stat").read_text()
        # comm may contain spaces and parentheses: the fields after the LAST ')' are fixed.
        return int(stat.rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError) as e:
        raise RestartError(f"cannot read the parent of pid {pid}") from e


def check_under_shim(pid: int, proc: Path = Path("/proc")) -> int:
    """Return the shim pid when `pid` is our daemon running under our shim, else raise."""
    cmd = _cmdline(pid, proc)
    if cmd[:3] != [PYTHON, "-m", "notifyd"]:
        raise RestartError(f"pid {pid} is not `{PYTHON} -m notifyd` (cmdline: {' '.join(cmd)})")
    parent = _ppid(pid, proc)
    if parent <= 1:
        raise RestartError(f"pid {pid} has no shim parent (parent pid {parent})")
    pcmd = _cmdline(parent, proc)
    scripts = [a for a in pcmd[1:] if os.path.basename(a) == SHIM_NAME]
    if not scripts:
        raise RestartError(
            f"parent pid {parent} of the daemon is not the {SHIM_NAME} shim "
            f"(cmdline: {' '.join(pcmd)}); not signalling. Start it under the shim, "
            f"or stop it by hand."
        )
    try:
        text = Path(scripts[0]).read_text(errors="replace")
    except OSError as e:
        raise RestartError(f"cannot read the shim {scripts[0]}: {e.strerror}") from e
    if SHIM_MARKER not in text:
        raise RestartError(f"{scripts[0]} does not carry our shim marker; not signalling")
    return parent


def restart(connection, proc: Path = Path("/proc"), wait: float = WAIT_SECONDS) -> tuple[int, int]:
    """Signal the daemon, wait for a new owner of both names. Returns (old pid, new pid)."""
    old = owner_pid(connection, CTL_IFACE)
    if old is None:
        raise RestartError(f"nobody owns {CTL_IFACE}: is the daemon running?")
    if old == os.getpid():
        raise RestartError("refusing to signal myself")
    check_under_shim(old, proc)
    try:
        os.kill(old, signal.SIGTERM)
    except OSError as e:
        raise RestartError(f"cannot signal pid {old}: {e.strerror}") from e
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        time.sleep(0.2)
        pids = {owner_pid(connection, n) for n in NAMES}
        if len(pids) == 1 and None not in pids and old not in pids:
            return old, pids.pop()
    raise RestartError(
        f"sent SIGTERM to pid {old}, but no new daemon owned both bus names within "
        f"{wait:.0f} s (the shim restarts it with a backoff; check the journal)"
    )


def run(bus_address: str | None, connect) -> int:
    try:
        old, new = restart(connect(bus_address))
    except GLib.Error as e:
        print(f"notifyd: --restart: bus error: {e.message}", file=sys.stderr)
        return 1
    except RestartError as e:
        print(f"notifyd: --restart: {e}", file=sys.stderr)
        return 1
    print(f"notifyd: restarted: old pid {old}, new pid {new}")
    return 0
