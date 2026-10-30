"""Pending apt-get upgrade counts for the System panel (#83).

Answers one question: is there anything pending that the user should
actually go run? Two numbers -- the total count of packages with a newer
candidate version, and how many of those come from a `-security` origin,
which is the one a wallpaper reader would actually want called out.

**Never runs a real upgrade and never needs privileges.** `apt-get -s
upgrade` ("simulate") computes the same dependency resolution a real
`apt-get upgrade` would, entirely against the already-downloaded local apt
cache, and prints what it *would* do without doing it -- no network, no
root, no state change. Measured ~4s on this machine, which is exactly why
this must never run on the request thread (see dashd-serve's
cached_apt_updates(), same posture as ghstats.fetch()).

Each pending package appears as one line of the form
`Inst <name> [<old>] (<new-version> <origin list> [<arch>]) [...]`. The
origin list names every archive component a candidate is available from,
e.g. `Ubuntu:24.04/noble-security, Ubuntu:24.04/noble-updates` -- `-security`
appearing anywhere in that parenthesized text is what earns the security
count. No `-s` output line is ever parsed for a package *name*, only
counted, so there's nothing here that could feed back into a real apt
invocation.

`apt-get` is a hard dependency but a soft failure: no binary, or a
non-zero exit (e.g. a broken/held package apt itself can't resolve past)
degrades to `available: False`, matching how `sysinfo.now_playing()`
treats a missing `playerctl` and `ghstats` treats a missing/unauthed `gh`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time

DEFAULT_TIMEOUT = 30
_ORIGIN_RE = re.compile(r"\(([^)]*)\)")


class AptUnavailableError(RuntimeError):
    """`apt-get` is missing or the simulated upgrade failed."""


def _run_simulate(timeout: int) -> str:
    if not shutil.which("apt-get"):
        raise AptUnavailableError("apt-get is not installed")
    env = dict(os.environ, LC_ALL="C")
    try:
        proc = subprocess.run(
            ["apt-get", "-s", "upgrade"],
            capture_output=True, text=True, timeout=timeout, check=False,
            env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AptUnavailableError(f"apt-get failed: {exc}") from exc
    if proc.returncode != 0:
        raise AptUnavailableError(
            (proc.stderr or "apt-get exited non-zero").strip()[:300])
    return proc.stdout


def shape(sim_output: str) -> dict:
    """Count total pending upgrades and how many are security, from
    `apt-get -s upgrade`'s simulated output."""
    total = 0
    security = 0
    for line in sim_output.splitlines():
        if not line.startswith("Inst "):
            continue
        total += 1
        match = _ORIGIN_RE.search(line)
        if match and "-security" in match.group(1):
            security += 1
    return {"total": total, "security": security, "available": True}


def fetch(timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Full refresh: run the simulation, shape the result.

    Raises AptUnavailableError on any failure so the caller can decide to
    serve a stale cache instead -- an old count beats an error on a
    wallpaper, same posture as the weather fetch and ghstats.fetch().
    """
    out = _run_simulate(timeout)
    shaped = shape(out)
    shaped["fetched_at"] = time.time()
    return shaped
