---
paths:
  - "lib/metrics.py"
  - "lib/userconf.py"
  - "config.json"
---

# Storage and config rules (metrics.db, userconf, config.json)

```
lib/metrics.py      SQLite historical-metrics store (cpu/mem/battery_pct);
                    self-migrates ALTER TABLE ADD COLUMN for an older DB
lib/userconf.py     two-file config (#74): config.json holds tracked
                    DEFINITIONS and is never written at runtime; this
                    machine's SELECTIONS (theme, units, log source, and
                    since #77 location) live in untracked
                    data/config.local.json. load() deep-merges,
                    local winning; every write path stores ONLY the
                    overrides, never a full snapshot -- a snapshot would
                    freeze the defaults so a later config.json edit would
                    silently stop taking effect. Both units' ReadWritePaths
                    now grant data/ ONLY, so systemd enforces the
                    never-written property rather than convention alone
config.json         location, units, port, display layer, per-output overrides,
                    metrics sample/retain interval, control host/port,
                    log source/patterns (#30), projects refresh/staleness
                    window (#62)
data/               weather.json cache, extra.json, tasks.json (read but
                    unrendered, #63), metrics.db, projects.json cache
                    (#62), control_token.txt, config.local.json (this
                    machine's selections, #74), locations.json (selectable
                    locations for the context menu's Location submenu,
                    #77 -- nothing in this repo writes it yet, hand-edit
                    or a future helper populates it from /api/geocode).
                    All gitignored -- but note .gitignore lists these
                    files INDIVIDUALLY, it does not ignore the directory,
                    so a new file here needs its own entry or it shows up
                    as untracked
```

## Gotchas

- **`CREATE TABLE IF NOT EXISTS` does not add a column to an existing
  table.** `data/metrics.db` is gitignored and had already been running in
  production (cpu/mem/net only) by the time `battery_pct` was added to
  `metrics.COLUMNS` (#12) — the old on-disk table would otherwise 500 on
  first `INSERT`/`SELECT` referencing it. `metrics._connect()` now diffs
  `PRAGMA table_info(samples)` against `COLUMNS` and `ALTER TABLE ADD
  COLUMN`s whatever's missing, every connection. Any *future* `COLUMNS`
  addition needs nothing more than adding the name to the tuple — the
  migration is generic — but removing/renaming a column would need real
  thought (this only ever adds).
- **`lsblk -J` keys are lowercased column names, including the `%`**:
  `FSUSE%` becomes JSON key `"fsuse%"`, not `"FSUSE%"` or `"fsuse_pct"`.
  Whole-disk rows (`type: "disk"`) and swap's `type: "crypt"` child carry no
  usable `FSUSE%`; `disk_tree()` only emits `type: "part"` rows that have a
  mountpoint or an `FSUSE%`, which is what keeps a bare, dataless swap slot
  (`nvme0n1p4` on this machine — the actual swap usage lives on its `crypt`
  child, not the partition itself) from showing up as a noise row.
