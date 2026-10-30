# cosmic-applet-claude-usage

COSMIC panel applet showing Claude plan usage as two vertical bars.

- Two vertical bars side by side. Left: the 5-hour session limit. Right: the weekly limit (all models).
- Each bar fills from the bottom upward with the percent used, like a level gauge. The fill uses the theme accent
  and turns the theme's destructive color at 95% or more.
- A horizontal marker line (white on a dark theme, dark on a light one, following live theme switches) shows how much of the window is already gone, measured from the
  bottom. Fill above the line = you are ahead of pace; line above the fill = behind.
- Hover the applet for the details, one block per limit in use (percent above 0):

  ```
  5 Hour : 8% / 62%
  Resets : 7:19 PM

  Weekly : 81% / 70%
  Resets : Thu 6:00 AM
  ```

  The first number is the percent used, the second the share of the window already gone (where the marker
  sits). Per-model weeks get their own block when they have usage. A failed poll adds "Stale - last read 2:14 PM".

Data comes from Claude Code's status-line feed: add this to `~/.claude/settings.json` (`scripts/install.sh`
prints it with your path), and Claude Code hands the applet your limits after every reply:

```
"statusLine": {"type": "command", "command": "~/.local/bin/cosmic-applet-claude-usage --statusline"}
```

(use the absolute path). The feed lands in `$XDG_RUNTIME_DIR/claude-usage/rate_limits.json`. When the feed is
older than 15 minutes or missing, the applet falls back to `claude -p /usage` (about 16 s and 0.5 GB per run), at
most every 15 minutes. A failed or rate-limited run keeps the last reading and marks it stale. Per-model weeks
appear in the hover only for readings that came from `/usage`.

## Install

```
scripts/install.sh      # builds, installs to ~/.local/bin and ~/.local/share/applications
```

Then add "Claude Usage" in Settings > Desktop > Panel > Applets. `scripts/uninstall.sh` removes it
(remove it from the panel first). Needs `claude` at `~/.local/bin/claude` or on PATH, and a Rust
toolchain to build. Verify a change with `scripts/check.sh`.

## Outside this repo
- `scripts/install.sh` writes `~/.local/bin/cosmic-applet-claude-usage` (release build, not tracked) and `~/.local/share/applications/net.corbinmeier.CosmicAppletClaudeUsage.desktop`. `scripts/uninstall.sh` removes both.
- **Hand edit it does not make:** the `statusLine` entry in `~/.claude/settings.json` (see above). Remove it yourself on uninstall, or Claude Code keeps calling a binary that is gone.
- Runtime state: `$XDG_RUNTIME_DIR/claude-usage/rate_limits.json` (tmpfs, gone at logout).
- **Depends on `../../libs/claude-usage-core`** (`path` dependency): it only builds with the repo layout intact. `vitals` reads the same feed.
- The `/usage` fallback shells out to `claude -p /usage` (about 16 s and 0.5 GB per run, at most every 15 minutes).
- Part of the `cosmic/` folder (`../README.md`).
