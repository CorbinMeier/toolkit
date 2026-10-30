---
paths:
  - "bin/dashd-serve"
  - "bin/dashd-collect"
  - "bin/dashd-control"
---

# Service rules (`dashd-serve`, `dashd-collect`, `dashd-control`)

```
bin/dashd-serve     HTTP server; /api/state merges weather+sys+extra (and
                    exposes config.metrics_retain_hours so the frontend's
                    history fetch window tracks it), /api/history reads
                    lib/metrics.py's stored samples -- read-only against
                    data/metrics.db, it never writes to it (#14). sys.disks
                    comes from lsblk (sysinfo.disk_tree()), not just
                    psutil's mounted-partition view; battery gets a real
                    charging flag from /sys/class/power_supply (see gotchas).
                    load_tasks() still merges data/tasks.json in as `tasks`
                    and extra.json's `schedule` array still rides along in
                    `extra`, but NOTHING RENDERS EITHER -- both panels were
                    deleted from web/ (#63). Deliberately kept: the reads
                    are harmless (missing file is not an error), source-
                    agnostic, and smoke.py asserts on `tasks`, so dropping
                    them is a code change with test fallout, not cleanup.
                    GET /api/geocode?q= proxies geocoding-api.open-meteo.com
                    (#77), query validated whitelist-style against
                    GEOCODE_QUERY_RE before it ever reaches urllib -- same
                    posture as MUSIC_COMMANDS, checked inside geocode()
                    itself, not just at the HTTP edge. load_locations()
                    reads data/locations.json the same source-agnostic way
                    as tasks.json (merged into /api/state as `locations`);
                    POST /api/locations (#104) validates and atomically
                    appends a picked geocode result to it
                    (clean_location_entry/save_location, capped at 50);
                    /api/geocode only looks matches up. POST
                    /api/config's EDITABLE whitelist also covers `location`
                    now (#77, same shape as `display.theme`), and accepting
                    a location patch calls invalidate_weather_cache() to
                    drop data/weather.json immediately rather than serving
                    the old city's cache for the rest of its TTL --
                    invalidate_weather_cache() also drops
                    data/air_quality.json and data/nws_alerts.json (#79/#80),
                    the same stale-city problem applying to both. Air
                    quality (fetch_air_quality/cached_air_quality) and NWS
                    alerts (fetch_nws_alerts/cached_nws_alerts) reuse a new
                    generic _fetch_or_cache(path, ttl, fetch_fn, lock)
                    helper -- weather's own cache function is now a thin
                    wrapper over it, own cache file/lock/TTL each so a slow
                    NWS fetch never serializes behind weather on the
                    request thread
bin/dashd-collect   standalone timer loop (config.metrics_sample_seconds);
                    the sole writer of data/metrics.db, independent of
                    whether any view or dashd-serve poll is happening (#14)
bin/dashd-control   mobile control server (#30) -- see `CLAUDE.md`'s process
                    list and the docstring at the top of the file for
                    the token/loopback trust model
```

## Data

Open-Meteo, no API key, no auth. Location pinned to Chico, CA
(39.72849, -121.83748); IP geolocation was tried and returned nulls, so it is
not used. Weather caches to `data/weather.json` and refetches past its TTL; a
failed fetch serves the stale cache with `stale: true` rather than raising,
because old weather beats an error on a wallpaper. The forecast `zip()`s use
`strict=True` on purpose — misaligned arrays would pair one day's temperature
with another day's icon, and serving the cache is better than showing that.

`data/extra.json` is merged into `/api/state` verbatim as `extra`, so cron or
any other script can add panels without touching the server.

## Gotchas

- **A `bin/dashd-serve`/`bin/dashd-collect` code change does not take effect
  until that unit is *restarted*, not just reloaded.** `scripts/update-
  desktop.sh` (`pnpm run deploy`) only `systemctl --user reload desktop-
  dashboard-host` on a green chain -- by design, `reload` is the one always-
  safe verb (SIGHUP re-render of an already-running surface), while
  restarting `-serve`/`-collect` is deliberately left out of the automated
  deploy path. This bit #46/#47: `weather_alert()` was merged and deployed,
  but the long-running `desktop-dashboard-serve` process kept executing the
  pre-merge code in memory (a Python `systemctl reload` doesn't re-exec the
  interpreter), so `/api/state`'s `weather.alert` was silently absent until
  a manual `systemctl --user restart desktop-dashboard-serve`. Any change to
  `bin/dashd-serve` or `bin/dashd-collect` (not `web/`) needs an explicit
  restart of that unit to actually ship -- `reload`ing `-host` alone is not
  enough and will not error, it just keeps serving stale logic.
