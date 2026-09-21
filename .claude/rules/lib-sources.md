---
paths:
  - "lib/sysinfo.py"
  - "lib/logsrc.py"
  - "lib/sessions.py"
  - "lib/ghstats.py"
---

# Data-source rules (sysinfo, logsrc, sessions, ghstats)

```
lib/sysinfo.py      shared psutil sampling (system_stats() for dashd-serve's
                    full live snapshot, sample_stats() for dashd-collect's
                    smaller persisted row) so the two processes can't
                    measure a metric two different ways. now_playing() (#26)
                    shells out to `playerctl` for MPRIS now-playing state --
                    source-agnostic (local player or browser tab, whatever
                    registers over MPRIS), returns None if nothing is
                    playing/paused or playerctl isn't installed
lib/logsrc.py       filtered/highlighted log tailing (#28): journal or
                    arbitrary file, matched against config.logs.patterns
                    (regex -> status/label); returns only matched lines.
                    The journal source ORs INDEXED FIELD matches
                    (config.logs.journalctl_matches, e.g. `_COMM=sudo`)
                    instead of naming one unit, because local auth failures
                    come from cosmic-greeter/greetd/sudo spread across
                    several units incl. per-session session-N.scope names,
                    so no single `-u` sees a failed graphical login (#72).
                    Never `--grep` -- see gotchas for why, with timings.
                    config.logs.journalctl_unit still WINS when set: it's
                    the Control Backend's phone-editable narrowing, and a
                    control that silently did nothing would be a defect
lib/sessions.py     who logged in since the last boot (#71), read from
                    systemd-logind's journal -- NOT utmp/wtmp, which carries
                    no user record on this machine (see gotchas). Pairs the
                    SESSION_START/_STOP catalog messages by SESSION_ID and
                    enriches still-open rows from `loginctl`. `journalctl -b`
                    is the whole reset-at-boot mechanism: no stored history,
                    nothing to prune, unlike metrics.db. shape() holds the
                    product decisions (uid filter, sort, ended_unknown) and
                    is what the tests point at. Degrades to available:False,
                    same posture as ghstats/now_playing
lib/ghstats.py      GitHub per-repo issue stats for Projects (#62). ONE
                    aliased `gh api graphql` document covers the
                    authenticated user + every org they belong to, with
                    totalCount-only sub-selections -- measured 1 rate-limit
                    point of 5000/hr, and no issue node is ever transferred
                    (stats only, by design). shape() holds every product
                    decision (sort, staleness filter, actionable = open -
                    ai_blocked) and is what the tests point at; the fetch
                    itself is a thin subprocess wrapper. Degrades to
                    available:False if `gh` is missing/unauthed, same
                    posture as sysinfo.now_playing() without playerctl
```

## Gotchas

- **utmp/wtmp records no user login on this machine, so `last`/`who` are a
  dead end.** Measured 2026-09-01: `who -a` and `utmpdump /run/utmp` list
  only `reboot`/`runlevel`, and `last -s "$(uptime -s)"` returns the boot
  record alone -- while a GUI session was active. The COSMIC greeter path
  never writes a user utmp record. `w` *does* show the session, which makes
  this easy to misdiagnose: procps-ng 4.x falls back to logind, so `w`
  disagreeing with `who`/`last` is expected here, not a broken utmp. The
  usable source is systemd-logind's journal, matched on the catalog
  MESSAGE_IDs `8d45620c1a4348dbb17410da57c60c66` (start) /
  `3354939424b4456d9802ca8333ed424a` (stop), which carry `SESSION_ID`,
  `USER_ID` and `LEADER` as real fields -- no message-string regex needed.
  Readable unprivileged, and it works under `desktop-dashboard-serve`'s
  sandbox (verified with `systemd-run --user` reproducing the unit's real
  flags, per the `ReadWritePaths` lesson below).
- **`journalctl --grep` is a full text scan and must never run on the
  request thread.** There is no index for `-g`, so when matches are sparse
  `-n 200` keeps walking backwards hunting a 200th hit that does not exist
  and effectively scans the whole journal. Measured on this box: **60s+**
  unbounded, still **4.3s** bounded to `--since -24h`, versus **126ms** for
  the equivalent OR'd indexed field matches
  (`_COMM=sudo + _COMM=sshd + ...`), which return the same auth lines. At a
  5-second poll the first two are not options -- hence
  `config.logs.journalctl_matches` (#72). Related ordering trap: plain `-n`
  returns oldest-first but `-n` **with** `--grep` returns newest-first, so
  `lib/logsrc.py` passes `-r` unconditionally and reverses once in Python
  rather than depending on which form is in play.

- `psutil.sensors_battery()` returns `power_plugged`, not `plugged_in`, and
  it's AC-present, not charge state — a full battery still left plugged in
  reports `power_plugged: True` with no current flowing. The battery
  utilization ring's charging state needs the real thing, so
  `battery_charging()` reads
  `/sys/class/power_supply/BAT*/status` (`"Charging"` vs `"Full"` /
  `"Discharging"` / `"Not charging"`) and only falls back to
  `power_plugged` when no `BAT*` node exists at all.
