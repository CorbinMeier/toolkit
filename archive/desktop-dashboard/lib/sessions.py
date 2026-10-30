"""Login-session history for the Sessions panel (#71).

Answers one question for the wallpaper: *who has logged into this machine
since it booted, when, and for how long?* Ended sessions stay on the list, so
a login that opened, ran a script, and logged out is still visible until the
next reboot -- which is the whole point of the panel.

**Source is systemd-logind's journal, not utmp/wtmp.** Measured on this
machine (Pop!_OS 24.04 / COSMIC): `who -a` and `utmpdump /run/utmp` list only
`reboot`/`runlevel` entries, and `last` returns the boot record alone, despite
an active GUI session -- the COSMIC greeter path never writes a user utmp
record. (`w` shows the session anyway only because procps-ng 4.x falls back to
logind.) So the classic tools are a dead end here and the journal is the
primary source, not a fallback.

Matching is on systemd's documented catalog MESSAGE_IDs rather than the
human-readable "New session 3 of user corbin." string: those IDs hand back
`SESSION_ID`/`USER_ID`/`LEADER` as real journal fields, so there is no message
regex to break when the wording is translated or reworded upstream.

`journalctl -b` scopes every read to the current boot, which is what makes the
list reset on reboot. That is deliberately *not* a stored history: there is no
table to migrate and nothing to prune, unlike lib/metrics.py.

Cost is a per-poll subprocess pair, matching how `logsrc` and
`sysinfo.now_playing()` already work -- measured at ~12ms for the journal read
and ~5ms for the loginctl read, which is why this needs no cache file or
background thread the way `ghstats` does. Missing binaries or an unreadable
journal degrade to `available: False` rather than raising, same posture as
`ghstats` without `gh`.
"""

from __future__ import annotations

import json
import pwd
import shutil
import subprocess
import time

# systemd catalog IDs: sd-messages.h's SD_MESSAGE_SESSION_START/_STOP.
START_ID = "8d45620c1a4348dbb17410da57c60c66"
STOP_ID = "3354939424b4456d9802ca8333ed424a"

# Only these fields are worth transferring; the default `-o json` record is
# ~30 keys of cgroup/selinux/cmdline metadata per event.
_FIELDS = "SESSION_ID,USER_ID,LEADER,MESSAGE_ID"

# The retroactive "human sessions only" filter. A closed session has no logind
# object left to ask for `Class=user`, so the class test that works for live
# sessions cannot be applied to history -- but the uid can, and it separates
# the same set: on this machine the greeter runs as `cosmic-greeter` (uid 988,
# a system account) and opens a session at every single boot that closes the
# instant a human logs in. That row is invariant per boot and pure noise.
DEFAULT_MIN_UID = 1000
DEFAULT_TIMEOUT = 10
DEFAULT_MAX_ROWS = 12


class SessionsUnavailableError(RuntimeError):
    """`journalctl`/`loginctl` is missing, or the journal is unreadable."""


def _run(args: list[str], timeout: int) -> str:
    if not shutil.which(args[0]):
        raise SessionsUnavailableError(f"{args[0]} is not installed")
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SessionsUnavailableError(f"{args[0]} failed: {exc}") from exc
    if proc.returncode != 0:
        raise SessionsUnavailableError(
            (proc.stderr or f"{args[0]} exited non-zero").strip()[:300])
    return proc.stdout


def journal_events(timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """Session start/stop events for the current boot, oldest first.

    `+` is journalctl's OR between match groups, so both message IDs come back
    from one invocation already interleaved in chronological order -- which is
    what lets pair() walk them in a single pass.
    """
    out = _run(["journalctl", "-b", "-o", "json", "--no-pager",
                f"--output-fields={_FIELDS}",
                f"MESSAGE_ID={START_ID}", "+", f"MESSAGE_ID={STOP_ID}"],
               timeout)
    events = []
    for line in out.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = rec.get("SESSION_ID")
        if not sid:
            continue
        try:
            # journal timestamps are microseconds since the epoch, as strings.
            ts = int(rec.get("__REALTIME_TIMESTAMP") or 0) / 1_000_000
        except (TypeError, ValueError):
            continue
        events.append({
            "session_id": str(sid),
            "user": rec.get("USER_ID") or "?",
            "leader": rec.get("LEADER"),
            "start": rec.get("MESSAGE_ID") == START_ID,
            "ts": ts,
        })
    return events


def _parse_show_blocks(text: str) -> dict[str, dict]:
    """`loginctl show-session a b c` emits one blank-line-separated block per
    session. Keyed off each block's own `Id=` rather than the argument order,
    since the property order in a block does not follow the `-p` order."""
    blocks: dict[str, dict] = {}
    current: dict[str, str] = {}
    for line in text.splitlines() + [""]:
        if not line.strip():
            if current.get("Id"):
                blocks[current["Id"]] = current
            current = {}
            continue
        key, _, value = line.partition("=")
        current[key.strip()] = value.strip()
    return blocks


def active_sessions(timeout: int = DEFAULT_TIMEOUT) -> dict[str, dict]:
    """Live detail for the sessions logind still knows about, keyed by id.

    Two calls: `list-sessions` for the roster (tty/state/idle) and one
    `show-session` covering every id at once for the fields it omits (session
    type, and whether the login came from off-box). None of this is
    recoverable for a session that has already ended -- logind drops the
    object -- so history rows carry only what the journal recorded.
    """
    roster = _run(["loginctl", "list-sessions", "-o", "json"], timeout)
    try:
        listed = json.loads(roster or "[]")
    except json.JSONDecodeError as exc:
        raise SessionsUnavailableError(f"unparseable loginctl output: {exc}") from exc

    out: dict[str, dict] = {}
    for row in listed if isinstance(listed, list) else []:
        sid = str(row.get("session") or "")
        if sid:
            out[sid] = {
                "tty": row.get("tty") or "",
                "seat": row.get("seat") or "",
                "state": row.get("state") or "",
                "idle": bool(row.get("idle")),
                "uid": row.get("uid"),
            }
    if not out:
        return out

    # Guarded: `show-session` with no ids dumps the manager's own properties
    # rather than nothing, which would parse into a bogus row.
    detail = _parse_show_blocks(_run(
        ["loginctl", "show-session", *out,
         "-p", "Id", "-p", "Type", "-p", "Class", "-p", "Service",
         "-p", "Remote", "-p", "RemoteHost"], timeout))
    for sid, props in detail.items():
        if sid in out:
            out[sid].update({
                "type": props.get("Type") or "",
                "class": props.get("Class") or "",
                "service": props.get("Service") or "",
                "remote": props.get("Remote") == "yes",
                "remote_host": props.get("RemoteHost") or "",
            })
    return out


def _uid_of(user: str) -> int | None:
    try:
        return pwd.getpwnam(user).pw_uid
    except (KeyError, TypeError):
        return None


def pair(events: list[dict]) -> list[dict]:
    """Fold the start/stop stream into one record per session.

    Sessions are tracked in an open-by-id map rather than by looking up the
    last record for an id, so a reused session number opens a second, distinct
    row instead of overwriting the first. A stop with no matching start (the
    journal was vacuumed mid-boot) still produces a record, with a null login
    time -- dropping it would silently under-report a login that happened.
    """
    records: list[dict] = []
    open_by_id: dict[str, dict] = {}
    for ev in events:
        sid = ev["session_id"]
        if ev["start"]:
            rec = {
                "session_id": sid,
                "user": ev["user"],
                "leader": ev["leader"],
                "login_at": ev["ts"],
                "logout_at": None,
            }
            records.append(rec)
            open_by_id[sid] = rec
        else:
            rec = open_by_id.pop(sid, None)
            if rec is None:
                rec = {
                    "session_id": sid,
                    "user": ev["user"],
                    "leader": ev["leader"],
                    "login_at": None,
                }
                records.append(rec)
            rec["logout_at"] = ev["ts"]
    return records


def shape(events: list[dict], active: dict[str, dict],
          min_uid: int = DEFAULT_MIN_UID, max_rows: int = DEFAULT_MAX_ROWS,
          now: float | None = None) -> dict:
    """Panel rows from raw events plus the live roster.

    "Still logged in" is decided by logind's roster, never by the absence of a
    stop record. If logind restarted, or a stop event was lost, the session is
    over and the journal simply cannot say when -- reporting that as active
    would render a duration that grows forever for a session that is gone.
    Such a row is marked `ended_unknown` and carries a null duration instead of
    a fabricated one.
    """
    now = time.time() if now is None else now
    rows = []
    for rec in pair(events):
        uid = _uid_of(rec["user"])
        # An unresolvable user is kept deliberately: a session belonging to an
        # account that no longer exists is exactly the thing this panel is
        # worth having for, so it must not be filtered out as "not human".
        if uid is not None and uid < min_uid:
            continue
        sid = rec["session_id"]
        live = active.get(sid) if rec["logout_at"] is None else None
        is_active = live is not None
        ended_unknown = rec["logout_at"] is None and not is_active
        end = rec["logout_at"] if rec["logout_at"] is not None else now
        duration = None
        if rec["login_at"] is not None and not ended_unknown:
            duration = max(0, round(end - rec["login_at"]))
        rows.append({
            "session_id": sid,
            "user": rec["user"],
            "uid": uid,
            "leader": rec["leader"],
            "login_at": rec["login_at"],
            "logout_at": rec["logout_at"],
            "duration_s": duration,
            "active": is_active,
            "ended_unknown": ended_unknown,
            "tty": (live or {}).get("tty", ""),
            "seat": (live or {}).get("seat", ""),
            "type": (live or {}).get("type", ""),
            "service": (live or {}).get("service", ""),
            "idle": (live or {}).get("idle", False),
            "remote": (live or {}).get("remote", False),
            "remote_host": (live or {}).get("remote_host", ""),
        })

    # Active first, then most recent login. "Who is on this machine right now"
    # is the panel's first question; a long-running session started at boot
    # would otherwise sink below a short one that opened and closed since.
    rows.sort(key=lambda r: r["login_at"] or 0, reverse=True)
    rows.sort(key=lambda r: not r["active"])
    truncated = max(0, len(rows) - max_rows) if max_rows > 0 else 0
    if truncated:
        rows = rows[:max_rows]
    return {
        "rows": rows,
        "totals": {
            "active": sum(1 for r in rows if r["active"]),
            "sessions": len(rows) + truncated,
            "users": len({r["user"] for r in rows}),
        },
        "truncated": truncated,
        "min_uid": min_uid,
        "available": True,
    }


def collect(cfg: dict | None = None, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Full read for /api/state. Never raises: a machine without systemd, or
    a journal this user cannot read, yields `available: False` and a panel
    that says so, rather than a 500 that blanks the whole dashboard."""
    scfg = (cfg or {}).get("sessions") or {}
    if not scfg.get("enabled", True):
        return {"available": False, "disabled": True, "rows": [], "totals": {}}
    try:
        events = journal_events(timeout)
        active = active_sessions(timeout)
    except SessionsUnavailableError as exc:
        return {"available": False, "error": str(exc), "rows": [], "totals": {}}
    return shape(events, active,
                 min_uid=int(scfg.get("min_uid", DEFAULT_MIN_UID)),
                 max_rows=int(scfg.get("max_rows", DEFAULT_MAX_ROWS)))
