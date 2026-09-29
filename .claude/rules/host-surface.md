---
paths:
  - "bin/dashd-host"
  - "systemd/**"
---

# Host surface rules (`bin/dashd-host`, systemd units)

```
bin/dashd-host      layer-shell surfaces; --list prints monitor keys. Each sync
                    POSTs the connected set to dashd-serve /api/outputs (#93)
                    -> data/outputs_detected.json -> state.outputs_detected.
                    Duplicate models get ' #2' suffixes so each has its own key.
                    Surface is always RGBA/clear; the page decides opacity (#94)
systemd/            the four user units (dashd-control's not yet enabled)
```

## Environment (verified 2026-08-20, Pop!_OS 24.04 / COSMIC)

- Wayland, `XDG_CURRENT_DESKTOP=COSMIC`, compositor `cosmic-comp`. No X11
  wallpaper tool applies (feh/hsetroot/xwinwrap/conky are all dead ends), and
  `cosmic-bg` itself only accepts image files.
- Stack is GTK3 + `GtkLayerShell-0.1` + `WebKit2-4.1`. There is **no**
  `gtk4-layer-shell` in the Ubuntu 24.04 repos, so GTK4 is not an option.
  All three typelibs plus `psutil` and Chrome already ship on this machine —
  **no sudo was needed for any of this**, and none should be.
- Outputs are reported by GDK in **logical** px: the laptop eDP-1 is 1920x1200
  physical but presents as `1440x900 @0,490` with `scale=1`, and its
  `get_model()` is `"Unknown"` — hence the `monitor-N` fallback in
  `monitor_name()`. The Dell is `2560x1080 @1440,0`.

## The layer choice is the whole trick

`cosmic-bg` owns the **BACKGROUND** layer. A layer-shell surface placed there
maps fine, paints fine, and receives frame callbacks at ~30fps — but
`cosmic-bg` draws over it, so it is **invisible**. This cost a full diagnostic
cycle: the "frozen clock" seen on the desktop was `cosmic-bg` still showing a
static PNG, not our stalled surface.

Use **BOTTOM**: above `cosmic-bg`, below every normal window. A test in
`tests/test_dashd.py` asserts the config never regresses to `BACKGROUND`.

Frame scheduling was measured with a `requestAnimationFrame` vs `setInterval`
counter probe — rAF only advances if the compositor is actually scheduling
frames — at a steady 30fps on both layers. **Rendering was never the problem;**
if something looks frozen again, suspect z-order or a stale process, not the
render loop.

## Gotchas

- **PyGObject version pinning**: `gi.require_version("Gdk", "3.0")` must come
  before the `from gi.repository import ...` line and before anything pulls in
  Gtk 4, or the import dies with `Requiring namespace 'Gdk' version '3.0', but
  '4.0' is already loaded`.
- **A single-file `ReadWritePaths` entry under `ProtectHome=read-only` does
  not permit a tmp-then-rename write.** `#69` diagnosed `dashd-serve`'s
  `POST /api/config` 500ing with `OSError(30, 'Read-only file system')`
  under the sandboxed unit, and was first "fixed" (164f745) by adding
  `config.json` to `desktop-dashboard-serve.service`'s `ReadWritePaths` --
  matching `dashd-control`'s unit, which already had it. That fix didn't
  actually work: a `ReadWritePaths` entry naming one file grants
  content-level write to that exact inode, not directory-level write to
  its parent, so creating the sibling `config.json.tmp` still fails (and
  staging the tmp under `data/`, which does have a directory-level entry,
  then renaming across also fails with `EXDEV` -- separate
  `ReadWritePaths` entries are separate bind mounts). A follow-up commit
  fixed `dashd-serve`'s `save_config()` to write `CONFIG` in place
  (no tmp+rename), still under `#69`. `dashd-control`'s `save_config()`
  carried the identical latent bug -- never exercised, since its unit
  isn't enabled -- and got the same in-place-write fix under `#70`.
  Trade-off both accept: no crash-safety if the write dies mid-flight.
  General lesson: a systemd `ReadWritePaths` grant on a file path is not
  equivalent to write access to its containing directory -- verify with
  `systemd-run` reproducing the real sandbox flags, not just a
  plain-process test.
  **Resolved at the root by #74**, which stopped writing `config.json`
  altogether: selections now go to `data/config.local.json`, and `data/`
  has always had a directory-level grant, so tmp-then-rename works there
  and the crash-safety #69/#70 gave up is back. `config.json` was dropped
  from both units' `ReadWritePaths` in the same change -- the sandbox now
  enforces "never written at runtime" instead of leaving it to convention.
  The mechanism above is still the thing to know when granting any single
  file; it just no longer has a live consumer here.
- `pkill -f dashd-serve` **kills the shell running it**, because the pattern
  matches that shell's own command line. Resolve PIDs with `pgrep` and skip
  `$$`, or use `systemctl --user stop`.
