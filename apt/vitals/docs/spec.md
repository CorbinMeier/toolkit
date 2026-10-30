# vitals: a lightweight, btop-style TUI for CPU, memory, disk, network and Claude usage

Spec, 2026-10-28. Lives beside `cosmic-applet-claude-usage`.
Corbin's decisions of 2026-10-28 are in §13. The web dashboard (`vitals --serve`) is §14; it sits after
§13 so the §13 references elsewhere stay valid (Decisions is therefore not the last section).

## 1. Outcome

`vitals` is one terminal command that opens a full-screen, colored dashboard:

- six **sections**: **CPU**, **Memory**, **Disk**, **Network**, **Tokens** (Claude Code token usage over
  time, from the local transcripts, §3.2) and **Claude** usage (a bottom bar: the session fills
  from the left, the week from the right, with pace markers, day ticks and reset times)
- each section stacks a compact timeline, bars (the focus) and a summary line, each part switched on or off,
  placed by rows in the config (§4); a settings popup and a help popup sit behind buttons at the top right
- everything works with the **mouse** (click, right-click, scroll), and every mouse action also has a key
- **truecolor** gradients, falling back to 256 and 16 colors and honoring `NO_COLOR`

It is done when it meets the §8 performance budget on this laptop (Ryzen with 16 threads, `k10temp`, `amdgpu`,
NVMe), passes `scripts/check.sh` and is installed to `~/.local/bin/vitals`.

**Out of scope for v1:** process list or kill (btop already does that), GPU, battery, remote hosts,
saving system history between runs (the token index is the one thing kept on disk), Windows and macOS,
token usage from claude.ai or the desktop app (only Claude Code writes local transcripts).

## 2. Stack

- **Rust**, edition 2024, with **ratatui** (it diffs the frame buffer, so only changed cells are written) on
  **crossterm** (mouse capture, truecolor, alternate screen, resize events).
- **No `sysinfo` crate.** It reads far more than these panels need and allocates on every refresh. vitals
  reads `/proc` and `/sys` itself. Most of the "lightweight" goal is won here.
- **No async runtime.** One thread runs `crossterm::event::poll(time_to_next_tick)`, then samples or handles
  input, then draws. The usage-server client is a hand-written 15-line HTTP/1.1 GET over `TcpStream` to 127.0.0.1,
  so no `ureq` or `reqwest`.
- Crates: `ratatui`, `crossterm`, `serde` and `serde_json` (feed and usage-server JSON), `toml` (config), `chrono`
  (reset clocks, the same as the applet). Dev only: `insta` (snapshot tests).
- Release profile: `lto = "fat"`, `codegen-units = 1`, `panic = "abort"`, `strip = true`, `opt-level = 3`.

Why Rust: the Claude parts are already written and tested in Rust in the applet. They move into the shared
path crate `claude-usage-core` (§10), used by both the applet and vitals.

Web dashboard (§14): the one crate added for it is `rustls` (TLS 1.3, the `ring` provider, no `tokio`,
`rustls::StreamOwned` over a `std::net::TcpStream`), plus `ring` itself for the certificate fingerprint. The
server is std threads and `std::net`, like the rest. Nothing in the TUI path uses them.

Extra crates: `memchr` (line and substring scanning for the token index). The token indexer runs on one
background thread that sends results to the UI thread over `std::sync::mpsc`.

## 3. Data sources

The definitions are the conventional ones, so vitals and other system monitors show the
same numbers.

| Panel | Source | Definition |
|---|---|---|
| CPU per thread | `/proc/stat` `cpuN` lines, fields 1-8 | busy = total − (idle + iowait); % = Δbusy / Δtotal. Total = mean of the threads. |
| CPU extra | `/proc/loadavg`; hwmon `k10temp` `temp1_input` (Tctl) | load averages 1/5/15 shown in the header; temperature shown in the title, colored by heat |
| CPU frequency | `/sys/devices/system/cpu/cpufreq/policy*/scaling_cur_freq` | average GHz in the title (can be turned off) |
| Memory | `/proc/meminfo` | used = MemTotal − MemAvailable; cached = Cached + SReclaimable; buffers; swap used = SwapTotal − SwapFree |
| Disk I/O | `/proc/diskstats`, whole disks only (`sd*`, `vd*`, `nvme*n*`, `mmcblk*`, whole-disk name filter) | read and write in bytes/s (fields 6 and 10 × 512); busy % = Δio_ticks (field 13) / Δms |
| Disk space | `statvfs` on each real mount from `/proc/self/mounts` | skips tmpfs, overlay, squashfs, proc and other pseudo filesystems, and network filesystems (nfs, cifs, sshfs: `statvfs` blocks when the server is gone); mounts on the same device count once |
| Disk temperature | hwmon `nvme` `temp1_input` | shown in the disk title |
| Network | `/proc/net/dev`, **every** interface (physical, VPN and tunnels, bridges, `docker0`, `veth*`, `lo`) | down and up in bytes/s per interface, and `all`: the sum over the **included** interfaces. **(v5, Corbin 2026-10-29)** Default inclusion is the physical-interface rule (`/sys/class/net/<if>/device` exists); every other interface is off. `[section.network]` `include = [..]` and `exclude = [..]` are explicit overrides on top of that rule (`exclude` wins), so a new physical adapter counts by itself and a new veth does not (§9). Bytes since vitals started: per interface, from its counters at the first read (an interface that appears later counts from 0) |
| Network scale | in memory | each half of a split bar, and the timeline, is scaled against the highest rate of the last 5 minutes of that interface (or of the `all` sum) and direction, at least 100 kB/s. Changing which interfaces are included restarts the `all` peak (the old one was of another sum) |

**Reading cheaply:** every `/proc` and `/sys` file is opened once and kept open. Each tick does
`seek(0)` and `read` into a reused `Vec<u8>` and parses the bytes in place, without `String` or `split`
allocations. hwmon paths are found once at startup by matching `name`. The mount list is read again only
when `/proc/self/mounts` reports a change (poll `POLLPRI`) or every 30 s.

### Claude usage

Two sources, in this order. vitals **never runs `claude /usage`**: a single run takes about 16 s and 0.5 GB,
and the applet already owns that fallback with its 15-minute, no-overlap rule. A second tool doing the same
would double those runs.

1. **Feed file** `$XDG_RUNTIME_DIR/claude-usage/rate_limits.json`. The applet's `--statusline` hook writes
   it after every Claude reply (already set in `~/.claude/settings.json`). It holds `five_hour` and
   `seven_day` with `used_percentage` and `resets_at`, plus `written_at`. It counts as fresh if written in
   the last 15 minutes (the applet's `FRESH_SECS`). vitals checks the file's `mtime` every 5 s and reads it
   only when that changes.
2. **Usage server** `GET http://127.0.0.1:4317/api/usage`, at most once every 60 s, with a 500 ms connect timeout.
   This is the only source of the per-model weeks, `resets_text`, and the server's own `stale` flag. The
   session and week percents come from whichever source is newest; per-model weeks always come from the usage server.
3. **Neither available:** the row shows `no reading — feed and usage server unavailable` in muted text. A stale
   reading shows `Stale - last read 2:14 PM` (the same wording as the applet).

The usage only changes after a Claude reply, so drawing it on every tick adds nothing. The row is redrawn
when its data or the clock-based pace marker changes, which is once a minute.

### 3.2 Token usage (panel 6)

**Source:** Claude Code's own transcripts, `~/.claude/projects/**/*.jsonl` (or `$CLAUDE_CONFIG_DIR/projects`),
including the `subagents/` folders. This is local and read-only, needs no login and makes no network calls.
On 2026-10-28 there were 5,079 files (5.6 GB), 2.0 GB of them changed in the last 7 days.

**What is read:** lines with `"type":"assistant"` that carry `message.usage`. A cheap `memchr` search for
`"usage"` runs first; only lines that match are parsed with serde, into a struct with just these fields:

| Field | Use |
|---|---|
| `timestamp` (ISO 8601 UTC) | which time bucket |
| `message.id` (fallback `requestId`) | dedupe key |
| `message.model` | model family: `opus`, `sonnet`, `haiku`, `fable`, `other` |
| `usage.input_tokens` | **input** (new, uncached) |
| `usage.cache_creation_input_tokens` | **cache write** |
| `usage.cache_read_input_tokens` | **cache read** |
| `usage.output_tokens` | **output** (thinking tokens are already inside it) |

Message content is never kept: only these numbers reach the index.

**Dedupe:** one reply is written once per content block, all lines with the same `message.id` (a live
28-reply transcript had 11 repeated ids). Resumed and forked sessions also copy earlier lines into new
files. So each id counts once across all files. The index keeps a set of id hashes, each packed with a day
stamp (48 bits of the 64-bit hash plus a 16-bit day, one `u64`, open addressing, about 12 bytes per id)
so old ones can be pruned. A false duplicate needs two ids to share 48 bits: about 1 in 10^9 per reply.

**Index** `$XDG_CACHE_HOME/vitals/tokens-v1.bin` (`~/.cache/vitals/`), written atomically every 60 s and on
quit:
- per file: path, device and inode, and the byte offset read so far
- minute buckets for the last 48 h, hour buckets for the last 90 days; each bucket holds the four counts
  per model family
- the dedupe id set for 90 days
- a version byte; a missing, bad or other-version file is deleted and rebuilt

**Backfill** on first run, or after a rebuild: a background thread at the lowest CPU and I/O priority
(`setpriority` and `ioprio_set` through `libc`) reads files changed in the last 30 days, newest first.
The panel shows `indexing… 38%` and fills in as results arrive. The UI never waits on it, and the §8
budget does not count the backfill.

**Live updates** (on the same thread): every 2 s it stats the hot files (changed in the last 10 minutes)
and reads only the bytes added since the saved offset. An unfinished last line is held until its newline
arrives. Every 30 s it rescans the directory tree for new files. If a file's inode changes or it gets
shorter than the saved offset, it is read again from 0 (dedupe stops double counting).

**Day boundaries** are local midnight. "Today" and "this week" in the title use local days; the Claude
week (from the reset time) only marks the graph.

## 4. Layout (v2, Corbin 2026-10-29)

**Bars and readouts lead; graphs are compact and secondary.** The screen is made of **sections** (CPU,
Memory, Disk, Network, Tokens, Claude). Each section stacks three parts, top to bottom, and each part can be
turned on or off. **(v5, Corbin 2026-10-29)** When both the timeline and the bars are shown, **one blank
row** sits between them, in every section (the zoomed one too); it is not drawn when either part is off:

```
┌ CPU ────────────────────────────────────────────────────── 41% ┐
│⣀⣠⣤⣶⣿⣷⣶⣤⣀⣠⣤⣶⣿⣷⣶⣤⣀⣠⣤⣶⣿⣷⣶⣤⣀⣠⣤⣶⣿⣷⣶⣤⣀⣠⣤⣶⣿⣷⣶⣤⣀⣠⣤⣶⣿⣷⣶⣤⣀⣠⣤⣶⣿⣷  2m│  1. timeline (compact)
│                                                                 │     (blank row, v5)
│all ████████████████████▌░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░    41%│  2. bars (the focus)
│ 0 ██████████▌░░░░░  62%   1 ███▌░░░░░░░░░░  18%   2 █████▌░  35%│
│ 3 ██▌░░░░░░░░░░░░░  12%   4 ███████▌░░░░░░  44%   5 █▌░░░░░   8%│
│ …                                                               │
│3.1GHz · 68°C · load 6.65 7.57 12.06 · 16 threads                │  3. summary
└─────────────────────────────────────────────────────────────────┘
```

1. **Timeline:** a braille graph (style `braille`, `block` or `ascii`, §5) of the section's headline
   reading. **Compact by default: 2 rows** (8 braille dots high). Height is adjustable from 1 to 8 rows
   per section (`graph_rows`), in the settings popup or with `{` / `}`. Mirrored series (disk read/write,
   network down/up, tokens in/out) split the height, upward over downward; at 1 row they show the sum.
   The time window sits at the right end of the timeline (`2m`).
2. **Bars:** one bar per reading, `label  ████▌░░░  value`, filling the section width, colored by value
   (§7). This is the main part of every section:

   | Section | Bars |
   |---|---|
   | CPU | `all` (mean), then one per thread in as many columns as fit (bar at least 8 cells); `per_thread = false` keeps `all` only |
   | Memory | **(v4, Corbin 2026-10-29)** one bar only: current utilization, `used` (MemTotal − MemAvailable) as % of total, value `11.7G 51%`. Cache, available and swap are not shown in the TUI (they stay in `--mem` and the JSON). |
   | Disk | one per mount: fullness (`/  72%  340G/476G`); one per whole disk: busy %; read and write rate against the 5-minute peak |
   | Network | **(v5, Corbin 2026-10-29)** one **split bar** per network; see Network bars below. The timeline shows the sum of the included interfaces. Headline when space is short: the `all` row. |
   | Tokens | input, output, cache write, cache read as tokens/min over the last 5 minutes against the window's peak minute; then one bar per model family with today's share of tokens |
   | Claude | **(v3, 2026-10-29)** the same bar style as every other section, one row per line of the applet readout (`claude_usage_core::readout::lines`): `5 Hour`, `Daily`, `Weekly`, then each per-model week; **(v8.5)** a model week whose used percent rounds to 0 is not drawn (the readouts keep it), and the label column never narrows below `Weekly`. Each row: label, a left-to-right bar of used % on the normal `░` track with the pace marker `│` (theme foreground) at the expected %, then `used / expected` (e.g. `4% / 12%`, the applet's numbers) and, on the `5 Hour` and `Weekly` rows, the reset as `2:00 PM (4h 7m)`. No split bar, no day ticks, no dark-filled track. Fill Claude orange; red when used is above expected or at 95%+. |
   Details: the first bar of a section is its headline (the rows that stay when space runs short: CPU `all`,
   Memory `used`, Disk `/` (or the first mount, listed first), Network the `all` row, Tokens the four rate
   bars, Claude the `5 Hour` row). What each bar's "full" means is in the help popup. The keys `1`-`6`
   follow the section order above (5 Tokens, 6 Claude).

   **Bar columns (v7, Corbin 2026-10-29):** bars in a section never change width with the data.

   - **One column layout per section:** every bar row shares the same label column, bar start column, bar
     width and value column, so all bar tracks line up and are equal length. The label column is the widest
     label of the section's rows (all rows, not just the visible ones). In a grid (CPU threads, Network
     interfaces) all cells are the same width and share one layout, and the headline row (`all`) uses the
     same label and value columns; its bar spans the full width, so it is as long as the grid's width
     allows (in a one-column grid it is identical to the cells').
   - **Value columns are fixed width per value kind**, right-aligned, never sized from the current number:
     the widest output of the formatter (`ui/fmt.rs`: percent `100%` 4, size `1023K` 5, rate `1023K/s` 7,
     count `1000K` 5; Claude `100% / 100%` 11 and reset `Thu 12:00 PM (23h 59m)` 22 in `claude/mod.rs`), a
     section's column being its widest kind (Memory `1023G 100%`, Disk `100% 1023G/1023G`, Tokens
     `100%  1000K`, Network rate). Each constant sits next to its formatter, and a unit test sweeps the
     formatter (zero, unit boundaries, huge, NaN) against it. The Network split bar prints the down value
     right-aligned left of the bar and the up value left-aligned right of it, each in its own fixed column,
     so the `│` centre stays put.
   - **Short width:** the bar is what the layout leaves (`width - label - value - 1`); when that is under 3
     the bar goes and only the value stays; Claude drops its reset column first when the bar would be under
     8. A function of the terminal width only, never of the data.

   **Network bars (v5):**

   ```
   │all  1.2M/s ░░░░░▐███████████│███▌░░░░░░░░░░ 310K/s          │
   │[x]wlo1 1.1M ░░▐████│█▌░░ 290K  [x]enx0 80K ░░░░▐│░░░░ 20K   │
   │[ ]docker0 0 ░░░░░│░░░░░ 0     [ ]tailscale0 3K ░░░░│░░░░ 1K  │
   ```

   - **Split bar:** a centre divider `│`; download fills from the divider to the **left** (down color,
     blue), upload to the **right** (up color, magenta); the down value is printed left of the bar and the
     up value right of it. Each half is scaled against that network's own 5-minute peak of that direction
     (Network scale, §3). The bar width is odd so the divider is in the middle. Sub-cell precision on both
     halves: the right half uses the left-aligned eighth blocks `▏▎▍▌▋▊▉`; Unicode has no right-aligned block
     for most eighths (only `▐` and `▕`), so a partial cell on the left half is drawn the other way round:
     the left-aligned block that covers the *unfilled* part, in the track's color, on the fill color as the
     background. Under a color-less terminal the left partial cell reads as a slightly longer fill.
   - **`all` row first** (label `all`, values with `/s`): the sum over the included interfaces. Then **one
     row per interface**, flowing in as many columns as fit exactly like the CPU thread bars (same grid
     code; a cell is `[x]` + name + down value + bar of at least 9 cells + up value (both in the `1023K/s`
     value width, 7), the name column up to 10 cells, longer names end in `…`). Each starts with a **checkbox**, `[x]` counts toward `all`, `[ ]`
     does not (its name is dim). A click on the checkbox toggles it (key twin: the Network block of the
     settings popup, §6). Toggling clears the Network timeline history and restarts the `all` peak.
   - **Which interfaces:** every interface in `/proc/net/dev` (§3), in that order.
   - **Row cap:** the interface grid never takes more than 6 rows, so dozens of docker veths do not push
     every other section out; when there are more, the last cell reads `+N more: ⚙ settings` and the rest
     stay reachable in the settings popup.
   - **`per_interface`** (default true), like CPU's `per_thread`: false shows only the `all` row. It is a
     checkbox on the Network line of the settings popup.
   - **Short height:** the `all` row is the headline; the interface rows go first.
3. **Summary:** one line of plain readouts, an item is dropped whole when it does not fit:

   | Section | Summary |
   |---|---|
   | CPU | frequency · temperature · load 1/5/15 · thread count |
   | Memory | used / total (e.g. `11.7G / 22.7G`) |
   | Disk | read and write rate · busiest disk busy % · NVMe temperature · free space on `/` |
   | Network | **(v5)** names of the included interfaces · `N off` (listed but not included; omitted at 0) · bytes received plus sent since vitals started by the included interfaces (`4.1G since start`) |
   | Tokens | today in · out · cache · rate per minute · `indexing… N%` while backfilling |
   | Claude | session and week reset clock times · source (feed or server) and age · `Stale - last read 2:14 PM` |

**Sizing (v3, dynamic, Corbin 2026-10-29):** the screen is always filled. Each section needs 2 border rows +
bar rows + 1 summary row + its timeline's **minimum** (`graph_rows`, default 2) + the blank row between timeline and bars (v5). Rows of the layout start at
the height of their tallest section; **sections in one row share that height** (boxes line up; the extra
rows go to the shorter section's timeline). Leftover screen rows are then shared out to the rows' timelines,
evenly, per row (weight `grow`, default 1; `grow = 0` keeps a section compact at its minimum; a section with
its timeline off takes no extra). Width works the same way through the row `widths`. The result: on a tall
terminal the graphs get taller, on a short one they shrink back to their minimum first. When the terminal is
too short, parts are dropped in this
order, last row first: **(v5) the blank row between timeline and bars goes first (before any bar or timeline row)**, then the timeline shrinks to 1 row, then the timeline goes, then the summary, then the extra
bars (CPU threads, model families) down to each section's headline bar. Bars are the last thing to go:
only when even the headline bars do not fit does a row (last first) collapse to one-line meters
(`CPU ▕████░░▏ 41% 52°C`, no frame). Each step is taken for all rows from the last up before the next step.
The smallest supported size is 40 × 12; below it vitals shows `terminal too small (40×12 min)`.

**Data-driven placement:** the layout is a list of rows in the config, each row a list of sections with
relative widths (§9). Default:

```toml
[[row]]  sections = ["cpu", "memory"]      widths = [1, 1]
[[row]]  sections = ["disk", "network"]    widths = [1, 1]
[[row]]  sections = ["tokens"]
[[row]]  sections = ["claude"]
```

`layout = "auto"` (default) uses the rows at 100 columns or more and a single column of the same order
below that; `rows` always uses them; `column` always stacks. A section missing from every row is not
shown; a section named twice or unknown is a config warning (header) and is skipped. The Claude section
is no longer pinned: it sits wherever the rows put it (bottom by default). Turning on a section that no row
names (header chip or settings) gives it a row of its own at the end. `rows` was `grid` in v1 files and
`--layout grid` still parses.

**Header row:** one plain line: `vitals`, the section chips (click to show or hide; numbers only when the
width is short), then, from the right: **`[⚙]` settings** and **`[?]` help** buttons at the top-right
corner, with two global toggles left of them, **`[▤ bars]`** and **`[⣿ graphs]`** (v3): click to turn the bars,
or the timelines, of every section off or on at once (`B` / `G`). A toggle shows dim when that part is off in
every section, bright when on in any; clicking it when mixed turns the part on everywhere. These three
buttons are never dropped. Then the clock, the refresh interval (click or scroll), `paused`, `mouse off`, the theme name (click to
cycle), a config or theme warning, and the load averages (100 columns and up). Items that do not fit are
left out; the two buttons are never dropped.

**Settings popup** (`[⚙]` or `,`): anchored to the top-right, live (changes apply at once and are saved
to the config immediately, atomic write):

```
┌ Settings ─────────────────────────────────────────────┐
│ [x] CPU       [x] braille  [x] bars  [x] summary  rows − 2 +  ▲ ▼ │
│ [x] Memory    [x] braille  [x] bars  [x] summary  rows − 2 +  ▲ ▼ │
│ [x] Disk      [x] braille  [x] bars  [x] summary  rows − 2 +  ▲ ▼ │
│ [x] Network   [x] braille  [x] bars  [x] summary  rows − 2 +  ▲ ▼ │
│ [x] Tokens    [x] braille  [x] bars  [x] summary  rows − 2 +  ▲ ▼ │
│ [x] Claude    [x] braille  [x] bars  [x] summary  rows − 2 +  ▲ ▼ │
│ [x] All       [x] braille  [x] bars  [x] summary                 │
│ interval − 1s +   theme default ▸   layout auto ▸   style braille ▸ │
│ Network [x] per interface  counted in all:                        │
│ [ ] lo        [x] enx0       [x] wlo1       [ ] docker0   …       │
└───────────────────────────────────────────────────────┘
```

- The first checkbox enables the section; under it (on the same line) `braille` toggles the timeline and
  `bars` toggles the bars (Corbin's two required toggles); `summary` toggles the summary line.
- The `All` row (v3) sets that part for every section at once (same as the header toggles); its boxes
  show `[x]`, `[ ]` or `[-]` (mixed).
- **Network block (v5):** `per interface` is the `per_interface` toggle (§4); under `counted in all:` a
  checkbox per listed interface (flowing in as many columns as fit) counts it toward `all` or not, the
  keyboard twin of the checkbox in its Network row. Arrows move through the controls (left and right step
  through the checkboxes, up and down between rows), Space or Enter presses. Each press is saved at once
  (`include` / `exclude`, §9).
- `rows − n +` sets the timeline's **minimum** height (it grows past it when there is room, §4 Sizing); `▲ ▼` move the section one place earlier or later in the
  layout's reading order (rows keep their shapes; sections flow through them).
- The `braille` toggle is labelled with the section's graph style (`block`, `ascii`). `style ▸` sets the
  style of every section. `▲ ▼` skip disabled sections. The popup is 68 columns wide (44 below 70 columns);
  when the screen is shorter than the popup it scrolls to keep the keyboard cursor in view.
- Mouse: click any control. Keys: arrows move between controls, `Space`/`Enter` toggle or press, `Esc`
  or a click outside closes. Below 70 columns the popup drops `summary` and `rows` to a second line per
  section.

**Help popup** (`[?]` or `?`): explains every readout, one tab per section (click a tab, or `←`/`→`),
scrollable (wheel, `↑`/`↓`, `PgUp`/`PgDn`). For each bar and summary item: what it measures, its source
(`/proc/stat`, `MemAvailable`, `io_ticks`, transcripts, feed or server …), its unit and its scale (what
"full" means, e.g. "against the highest rate of the last 5 minutes, at least 100 kB/s"). A final tab lists
the mouse and key controls (§6). The help text lives in one data module (`ui/help_text.rs`) so the
popup and `vitals --help` share it.

**Zoom (v3):** clicking a section's title (or `Shift`+`1`–`6`) expands it to the whole screen, and **the
timeline takes every row the bars and summary do not need** (shown even if the timeline toggle is off). For
mirrored series the two halves split it evenly. Graph columns follow the full width, so a zoomed timeline
shows more history at the same window. `Esc` or another click on the title restores.

## 5. Graph styles and time windows

The old per-panel modes (`graph`, `bars`, `meter`, `hidden`) are replaced by the three part toggles in §4.

**Graph styles:** `braille` (2 × 4 dots per cell, the sharpest, the default), `block` (`▁▂▃▄▅▆▇█`
eighths, reads better in some fonts), `ascii` (`.:|#`, for the TTY console or fonts without braille).
Set per section in the config or for all sections in the settings popup (`style ▸`).

**Tokens timeline:** new input (input + cache write) upward, output downward, per bucket. Cache reads are
left out of the graph (often 100× the rest, they would flatten it) and shown in the summary. `v` switches
the series: `io` (default), `total`, `cache`. Dim vertical marks at the Claude session resets in the
window. The **token window** is separate from the system windows: 1 h (minute buckets), 6 h, 24 h (the
default), 7 d or 30 d (hour buckets).

**Claude timeline:** session % and week % over the time vitals has been running (memory only).

**Time window** per system timeline: 1, 2 (default), 5, 10 or 30 minutes,
changed by scrolling over the timeline or `+`/`-`. History is a fixed-size ring buffer per series (`f32`),
sized to 30 minutes at the fastest interval and allocated once. Points are collapsed to the timeline
width by max (not mean) per column, so short spikes still show.

## 6. Mouse and keys

Mouse capture is the two DEC modes 1000 (press, release, wheel) and 1006 (SGR coordinates), written by
vitals itself: crossterm's `EnableMouseCapture` also turns on button-drag and any-motion reporting (1002,
1003, 1015), which would wake the program on every mouse move. With only 1000 and 1006, moving the mouse
sends no input and costs no CPU.

| Action | Mouse | Key |
|---|---|---|
| Settings popup | click `[⚙]` (top right) | `,` |
| Help popup | click `[?]` (top right, next to settings) | `?` |
| Toggle timeline / bars / summary of the focused section | in settings | `g` / `b` / `s` |
| Toggle bars / timelines of every section | `[▤ bars]` / `[⣿ graphs]` in the header, or the `All` row in settings | `B` / `G` |
| Timeline minimum height (1–8 rows) | in settings | `{` / `}` |
| Next graph style | right-click the section title | `S` |
| Time window up or down | scroll over the timeline | `+` / `-` |
| Zoom or unzoom a section | left-click the section title | `Shift`+`1`-`6`, `Esc` |
| Show or hide a section | click its header chip | `1`-`6` |
| Next token series (`io`, `total`, `cache`) | click the Tokens timeline | `v` |
| Focus a section | click its body | `Tab` / `Shift+Tab` |
| Refresh interval (250 ms, 500 ms, **1 s**, 2 s, 5 s) | click or scroll the interval chip | `[` / `]` |
| Next theme | click the theme chip | `t` |
| Count a network interface toward `all` (checkbox) | click the `[x]`/`[ ]` in its Network row | the `counted in all` checkboxes under Network in settings (arrows, Space) |
| Network: one bar per interface or `all` only | — | the `per interface` checkbox under Network in settings (arrows, Space) |
| Claude details | click the Claude bars: a popup with the applet's hover text (below) and the source and its age | `c` |
| Pause sampling | — | `p` |
| Turn mouse capture off or on | — | `M` |
| Quit | — | `q`, `Ctrl+C` |

**Claude details popup:** the same text as the COSMIC applet's hover, from `claude_usage_core::readout::lines`
(never reformatted here): `5 Hour : 8% / 62%` (used / expected, the pace marker) and `Resets : 7:19 PM`, a
blank line, then Daily, Weekly and the per-model weeks with one shared `Resets` line, colons aligned, then
the stale line; under it `source: feed|server, read 3m ago`. Values are Claude orange, a used percent above
its expected percent is danger red (over pace), labels and notes are muted. Any click or `Esc` closes it.

**Caveat:** while mouse capture is on, the terminal's own text selection needs Shift+drag (in COSMIC
Terminal, kitty, foot and alacritty). `M` turns capture off so selection works normally, and the header
shows `mouse off` while it is off.

## 7. Color

- **Detection:** `COLORTERM=truecolor|24bit` → 24-bit; otherwise `TERM` containing `256color` → 256-color
  palette (nearest match, computed once per theme); otherwise 16 colors. `NO_COLOR` set → monochrome: graphs
  keep their shapes, and danger states use bold and reverse video instead of red.
- **The background is never painted.** It stays the terminal's default, so transparent or themed terminals
  look right.
- **Gradients are by value, not by position.** Each graph column or bar cell is colored by its own
  reading, from a gradient table of 101 entries computed once per theme.
- **Built-in themes:**
  - `default` (btop-like, with a hue per panel; **the default theme**): CPU green → yellow → red; Memory
    teal → violet; Disk read cyan and write lime; Network down blue and up magenta. Orange belongs to Claude
    only: the Claude row and the Tokens panel (output `#D97757`, input `#E8B48F`, cache `#8A5A44`).
  - `ramp`: a load-colour ramp on the system panels (black at 0, accent at 85, white at 100), with the accent
    read from the COSMIC theme (`~/.config/cosmic/com.system76.CosmicTheme.{Dark,Light}/v1/accent`, whichever
    is active), falling back to `#ef4444`. This makes vitals match the desktop accent. The Tokens
    panel and the Claude row keep their orange in every theme (they are Claude's, not the machine's).
  - `mono`: shades of one hue, chosen by `accent = "#rrggbb"`.
- **The Claude row is always Claude orange `#D97757`**, in every theme: the bar fill, the percents and
  the Claude popup. The track is the normal muted `░` track of every other bar (v3; the dark-filled orange
  track is gone) and the pace marker is the theme's foreground. The fill turns danger red `#ef4444` when used
  is above expected or at 95% or more (the popup colors the used percent the same way). Fallbacks: color 173 (`#d7875f`) on 256 colors, ANSI yellow on 16 colors, and plain bold
  with `NO_COLOR`. A custom theme can override it with `claude = "#rrggbb"`.
- **Temperatures** use their own fixed scale: below 60 °C muted, 60-80 °C yellow, above 80 °C red.
- **Custom themes:** `~/.config/vitals/themes/<name>.toml`, with gradient stops per series and colors for
  title, border, muted, accent and danger. Keys: `fg`, `title`, `border`, `muted`, `accent`, `danger`,
  `claude` as `#rrggbb`, and a `[series]` table of stop lists named `cpu`, `memory`, `cache`, `disk_read`,
  `disk_write`, `net_down`, `net_up`, `tokens_in`, `tokens_out`, `tokens_cache`, `claude`. Anything left out
  comes from `default`. A broken file falls back to `default` with a header warning. Custom themes join the
  `t` cycle after the three built-ins, sorted by name.

## 8. Performance budget

Measured on this laptop at the default 1 s interval, the default rows, 200 × 50 terminal. The settings and
help popups also draw without allocating (`tests/alloc.rs`); the Claude details popup allocates (it builds
the readout strings) and is not part of the steady state:

| Metric | Budget |
|---|---|
| CPU | ≤ 0.5% of one core on average (btop is typically 1-3%) |
| RSS | ≤ 8 MB |
| Time to first frame | ≤ 30 ms (the first CPU reading needs two samples: show the frame at once, with `…` for the first interval) |
| Stripped binary | ≤ 3.25 MB (2.5 MB until v6; raised by Corbin 2026-10-29 for `--serve`) |
| Allocations per tick | 0 in steady state, sampling plus a full draw, also through ratatui's `Terminal::draw` into a `TestBackend` with changing data (checked with a counting allocator in `tests/alloc.rs`). The Claude row's strings are rebuilt once a minute or when its data changes, outside that count. |
| Bytes written per tick | only changed cells (ratatui diff); one `write` syscall per frame |

First measurement of the finished UI, 2026-10-28, `scripts/bench.sh` (release build, 200 × 50, 1 s, warm
token index): CPU 0.34% of one core, first frame 7.5 ms, binary 1.48 MB, steady RSS **10.9 to 14.4 MB**
with the token indexer (4.8 MB with `--no-tokens`): about 10.9 MB until the first 30 s directory rescan,
about 14 MB after it, 12.8 MB once a save with `malloc_trim(0)` was tried (not kept). That is over the
8 MB line. Of the 4.8 MB without the indexer, 3.2 MB is file-backed (libc 1.5, the binary 1.4, the
loader 0.2) and 1.6 MB is the UI's heap (terminal buffers 0.8, graph rings 0.2, token buckets 0.6). The
rest, 6 to 9.6 MB, is the indexer thread's allocator arena (the loaded index with its dedupe set, the
table of about 5,000 transcript files, the save buffers). The jump coincides with the 30 s rescan; it
was not traced further. Corbin decides whether to raise the budget or slim the indexer.

Re-measured with the v2 UI (2026-10-29, `scripts/bench.sh`, 200 × 50, 1 s, warm index): CPU 0.33% of one core,
first frame 7.2 ms, binary 1.72 MB, steady RSS 13.5 MB. RSS is still over the 8 MB line for the same reason
(the indexer thread); the UI did not change it.

**Web server (`--serve`, §14) is outside this table.** It runs headless: no TUI, no frame, no
`tests/alloc.rs` count. Each tick it allocates (the sample JSON, the history point, `Arc<str>` copies), and
each connection allocates. Measured 2026-10-29, release build, 1 s interval, no clients, `--no-tokens`:
0.15% of one core, 4.7 MB RSS. The stripped binary grew from 1.72 MB to **3.02 MB** with `rustls`, `ring`
and the embedded page (about 0.15 MB of that is the page, uPlot and the icons), over the old 2.5 MB
line (`opt-level = "s"` for dependencies gave 2.71 MB). Corbin raised the line to 3.25 MB (2026-10-29) rather
than make `--serve` a Cargo feature or drop TLS: one binary, one install.

Rules that keep it inside the budget: draw only on a tick, input or resize; no animation timers; no
motion events; the Claude row is redrawn only on data or once a minute; and file contents are never kept
beyond the parsed numbers.

## 9. Config and CLI

`$XDG_CONFIG_HOME/vitals/config.toml` (`~/.config/vitals/config.toml`). Changes made in the UI (settings
popup, keys) are saved at once with an atomic rename. A missing or bad file falls back to the defaults,
with a one-line warning in the header and no crash. Configs from v1 (`[panel.*]` with `mode`) are read
once and converted: `graph` → all three parts on, `bars` → timeline off, `meter` → timeline and summary off,
bars on with `headline_only = true`, `hidden` → section disabled (a Claude `meter`, v1's default, becomes the
v2 Claude default). The converted file is written back as v2 on the next change.

```toml
interval_ms = 1000
theme = "default"
mouse = true
layout = "auto"            # auto | rows | column

[[row]]  sections = ["cpu", "memory"]      widths = [1, 1]
[[row]]  sections = ["disk", "network"]    widths = [1, 1]
[[row]]  sections = ["tokens"]
[[row]]  sections = ["claude"]

# every section also takes grow = 1 and headline_only = false. `grow` is a weight for the spare screen
# rows (§4 Sizing); 0 keeps the section compact. v2 wrote `grow = false` everywhere: a boolean is ignored
# and reads as the default weight 1 (v3).
[section.cpu]      enabled = true  graph = true  bars = true  summary = true  graph_rows = 2  style = "braille"  window_min = 2  grow = 1  per_thread = true
[section.memory]   enabled = true  graph = true  bars = true  summary = true  graph_rows = 2  style = "braille"  window_min = 2
[section.disk]     enabled = true  graph = true  bars = true  summary = true  graph_rows = 2  style = "braille"  window_min = 2
[section.network]  enabled = true  graph = true  bars = true  summary = true  graph_rows = 2  style = "braille"  window_min = 2  per_interface = true  include = []  exclude = []
[section.tokens]   enabled = true  graph = true  bars = true  summary = true  graph_rows = 2  style = "braille"  window = "24h"  series = "io"
[section.claude]   enabled = true  graph = false bars = true  summary = true  graph_rows = 2  style = "braille"  usage_url = "http://127.0.0.1:4317"
```

`accent = "#rrggbb"` (the `mono` theme) at the top level. Numbers are snapped to the values the UI steps
through (interval 250/500/1000/2000/5000 ms, windows 1/2/5/10/30 min, graph rows 1–8). Flags last for that
run only and are not written back.

**Network inclusion (v5):** `include` and `exclude` hold only explicit overrides of the default rule
(physical interfaces on, everything else off): toggling an interface to what the rule already says removes
it from both lists. `exclude` wins when a name is in both. `interface = "…"` (v1 to v4) is no longer a
setting; an old config that still has it parses fine and the key is dropped on the next save.

`[web]` (v6, §14; no key by default since v9), read by `vitals --serve` only; written back only when a key is first generated under `auth = "key"`:

```toml
[web]
listen = "0.0.0.0:4318"
auth = "none"         # default: every endpoint open. "key": the access key below is required (opt-in)
allow_public = false  # default: only private networks may connect (§14.3); true accepts any address
# key = "…"           # auth = "key" only: generated on the first --serve (128 random bits, url-safe base64); ignored under "none"
history_min = 10   # minutes of graph history sent on connect (1-60)
# tls_dir = "…"    # default $VITALS_DATA_DIR/tls, else ~/.local/share/vitals/tls
```

### CLI

`vitals` with no section flag opens the TUI. TUI flags: `--interval <ms>`, `--theme <name>`,
`--no-mouse`, `--layout <auto|rows|column>`, `--version`, `--help`. `--serve [ADDR:PORT]` runs the web
dashboard instead (§14); it takes `--interval`, `--theme` and `--no-tokens` and cannot be combined with
`--once` or a section flag (exit 2).

**Section readouts without the TUI:** one or more section flags print that section's readings and exit.

| Flag | Section |
|---|---|
| `--cpu` | CPU |
| `--mem`, `--memory` | Memory |
| `--disk` | Disk |
| `--net`, `--network` | Network |
| `--tokens` | Tokens |
| `--claude` | Claude usage |
| `--all` | all six |

- Default output is plain text: per section a title line (`CPU`, `Memory`, …), the section's bars (§4) as
  aligned `label  value` lines, then its summary line (§4), with a blank line between sections. Human
  units: `12.6G`, `1.6M/s`, `340.0K` tokens. `--dump` is an alias for `--all`; with `--no-tokens`, `--all`
  leaves Tokens out (an explicit `--tokens` always counts). Text and JSON come from the same readings.
- `--json` prints one JSON object instead (below). `vitals --cpu --json` → `{"schema":2,"sampled_at":…,"cpu":{…}}`.
- `--watch` keeps printing, one object per line (NDJSON with `--json`, a blank-line-separated block
  without), every `--interval` ms until interrupted. The hidden `--count <N>` stops it after N readouts
  (tests).
- `--json`, `--watch`, `--no-wait` and `--count` without a section flag are bad flags (exit 2), as is a
  section flag with `--once`. `--interval` is clamped to 250–5000 ms and defaults to the config's.
- Rates (CPU %, disk and network) need two samples, so a one-shot readout takes one interval (default 1 s;
  `--interval 250` for a faster answer).
- `--tokens` reads the cached index; with no index it runs the backfill and waits, printing progress to
  stderr. `--no-wait` skips waiting for the backfill but still lets the saved index load (up to 10 s), so
  the `index` object shows `phase`/`percent` of what is still running. `--claude` reads the feed, then the
  the usage server (`usage_url` from the config); never `claude`; with neither it fails (`"error": "no reading …"`).
- Exit codes: 0 ok; 2 bad flags; 1 when a requested section could not be read (the others still print;
  the failed one carries `"error": "…"` in place of its readings, and the text shows an `error  …` row).
- `--once` (one rendered TUI frame to stdout), `--size <WxH>`, `--no-tokens`, and the hidden bench flags
  `--exit-after <seconds>` and `--stats <file>` stay as before.

**JSON schema 2** (times are epoch milliseconds; units are in the key names; absent readings are `null`):

```jsonc
{
  "schema": 2,
  "sampled_at": 1793253540000,
  "cpu": { "percent": 41.2, "threads": [62.0, 18.1, …], "load": [6.65, 7.57, 12.06],
           "freq_ghz": 3.1, "temp_c": 68.0 },
  "memory": { "total_bytes": 0, "used_bytes": 0, "available_bytes": 0, "cached_bytes": 0, "buffers_bytes": 0,
              "used_percent": 0.0, "swap_total_bytes": 0, "swap_used_bytes": 0, "swap_percent": 0.0 },
  "disk": { "read_bps": 0, "write_bps": 0, "busy_percent": 0.0, "temp_c": 43.0,
            "devices": [{ "name": "nvme0n1", "read_bps": 0, "write_bps": 0, "busy_percent": 0.0 }],
            "mounts": [{ "target": "/", "source": "/dev/nvme0n1p3", "fstype": "ext4", "disk": "nvme0n1",
                         "total_bytes": 0, "used_bytes": 0, "avail_bytes": 0, "used_percent": 72.0 }] },
  "network": { "down_bps": 0, "up_bps": 0, "peak_down_bps": 0, "peak_up_bps": 0,
               "interfaces": [{ "name": "wlo1", "down_bps": 0, "up_bps": 0, "rx_bytes": 0, "tx_bytes": 0,
                                "included": true }] },
  "tokens": { "today": { "input": 0, "cache_write": 0, "cache_read": 0, "output": 0 },
              "last_hour": { … }, "per_min_5m": { … },
              "by_model_today": { "opus": { … }, "sonnet": { … }, "haiku": { … }, "fable": { … }, "other": { … } },
              "index": { "phase": "live", "percent": 100.0 } },
  "claude": { "source": "feed", "read_at": 0, "stale": false,
              "session": { "percent": 3.0, "resets_at": 0, "elapsed_percent": 12.0 },
              "week": { "percent": 99.0, "resets_at": 0, "elapsed_percent": 96.0,
                        "day_part": { "index": 6, "percent": 93.0 } },
              "models": [{ "label": "Week (Fable)", "percent": 0.0, "resets_at": 0 }] }
}
```

Only the requested sections appear, in the order above. Percents are rounded to 1 decimal, load to 2,
GHz to 2, bytes, rates and token counts to whole numbers. `per_min_5m` is the last 5 minutes divided by 5.
`index.phase` is `loading`, `backfill` or `live`. `claude.week.day_part.percent` is the share of that day's
allowance used (the TUI's daily bar). **Schema 2 (v5):** `network.down_bps`, `up_bps` and the two peaks are
the sum over the **included** interfaces (the config's `include` / `exclude` applied, §3), and
`network.interfaces[]` lists **every** interface with `"included": bool` (schema 1 listed physical ones only
and summed them). The text readout prints an `all` line, then one line per interface with a `[x]`/`[ ]`
marker, and its summary counts the included ones. `network.interfaces[].rx_bytes`/`tx_bytes` are the kernel counters
since boot, and the text summary shows their sum as `since boot` (the TUI's "since vitals started" has no
meaning for a one-shot). A failed section is `{"error": "…"}` instead of its object. The schema is checked by
`tests/readout.rs`, which parses every section's output against it key for key; any key change bumps `schema`.

## 10. Module map

| File | Job |
|---|---|
| `main.rs` | flags, terminal setup and restore (also on panic, Ctrl+C and SIGTERM/SIGHUP), event loop, `--once` |
| `readout/{mod,data,text,json}.rs` | CLI readouts (§9): `Request` (flags), `Env` (where readings come from, with the network include/exclude lists), `dispatch`/`run`; `data.rs` holds the schema-2 structs and the `Collector` that samples them; `text.rs` and `json.rs` print one `Document` |
| `app.rs` | `App`: config, sampled data, graph rings, UI state, the one place an `Action` changes anything, `tick` |
| `frame.rs` | a rendered buffer as plain text or ANSI (for `--once` and the tests) |
| `sample/{cpu,mem,disk,net,hwmon}.rs` | kept-open readers, byte parsers, Δ computation; each takes a `Root { proc, sys }` so tests point it at fixtures |
| `claude/server.rs` | usage-server `/api/usage` client (own thread, 500 ms timeouts, at most once a minute); feed reading, pace math and reset/stale words come from the `claude-usage-core` path crate (`../../libs/claude-usage-core`), shared with the applet |
| `claude/mod.rs` | `merge` (newest source wins, models from the usage server), `ClaudeView` (prepared strings, pace figures and the readout `rows`, refreshed on data or each minute), `ClaudeSource` (feed mtime every 5 s, usage-server channel) |
| `tokens/{scan,index,store}.rs` | transcript line filter and parser, dedupe and buckets, the on-disk index; runs on the indexer thread |
| `tokens/{mod,indexer,view}.rs` | shared types and messages; the indexer thread (`Indexer::spawn`, `Engine`); the UI-side `TokenView` that applies messages and answers window queries |
| `history.rs` | fixed-size ring buffers, column downsampling by max |
| `ui/{mod,layout,section,panel,graph,bars,meter,claude_row,tokens,header,settings,help,help_text,fmt}.rs` | widgets; `graph.rs` draws braille, block and ascii; `mod.rs` holds the render root, the popups' boxes and the title writer; `layout.rs` is the pure planner (rows, degrade steps, sharing out spare rows by `grow` weight, equal heights in a row); `section.rs` is what sections share (frame, parts, bar rows, mirrored timelines, summary line); `panel.rs` is CPU, Memory, Disk and Network; `tokens.rs` and `claude_row.rs` their sections (`claude_row.rs` draws the readout rows kept in `ClaudeView.rows` and the Claude details popup); `header.rs` also holds the `[▤ bars] [⣿ graphs] [⚙] [?]` buttons; `settings.rs` (with its `All` row) and `help.rs` the popups; `help_text.rs` the help data (plain `pub` structs) |
| `theme.rs` | color detection, built-in themes, gradient tables, COSMIC accent reader |
| `input.rs` | maps mouse and key events to `Action` through a hit map rebuilt on each layout pass (rectangle → target) |
| `config.rs` | TOML load and save; sections, rows (`resolve`, `move_section`), v1 conversion, row warnings; the `[web]` field |
| `web/{mod,conf,tls,http,feed}.rs` | the dashboard server (§14): `mod.rs` holds `Server`/`Params`, the accept loop, routes, `meta_json` (layout and theme for the page) and `serve_main` (`--serve`); `conf.rs` the `[web]` block and key generation; `tls.rs` the supplied certificate files (`TlsStore`, reloaded on mtime change, CA fingerprint); `http.rs` request parsing and responses; `feed.rs` the sampler thread (a `readout::data::Collector`) and the shared state (latest sample, history ring, token buckets, client count) |
| `web/` (repo folder, not `src/`) | the page, embedded with `include_str!`/`include_bytes!`: `index.html`, `app.js`, `app.css`, `setup.html`, `sw.js`, `manifest.webmanifest`, `icon.svg`, `icon-192.png`, `icon-512.png`, `icon-maskable-512.png`, and `vendor/` (uPlot, see `vendor/README`) |

## 11. Testing

- **Fully headless.** ratatui buffers render in memory, so no window or terminal opens on the live
  desktop; `check.sh` unsets `WAYLAND_DISPLAY` and `DISPLAY`. `insta` snapshots (`tests/snapshots/`) cover
  each section with its parts on and off and graph rows 1, 2 and 8 (and each style), at 40 × 12, 80 × 24 and
  200 × 50, the Claude section against each server fixture, the settings, help and Claude popups, reordering,
  a converted v1 config, the dynamic fill (200 × 60, 160 × 50, grow weights), the zoomed timeline (mirrored,
  timeline toggle off), the header toggles and the settings `All` row, and whole screens. `check.sh` exports `TZ=UTC` and the tests use a fixed clock, so they are the
  same on every machine. Accept new or changed snapshots with `INSTA_UPDATE=always cargo test --test ui` (no
  `cargo-insta` needed), then read the diff.
- **Fixtures:** `tests/fixtures/proc/` and `tests/fixtures/sys/`, pairs of `/proc/stat`, `meminfo`,
  `diskstats` and `net/dev` captured from this laptop; the feed JSON; the usage-server `/api/usage` JSON (fresh,
  stale, missing week, hot at 98%, the example below). The real `claude` never runs in tests.
- **Token tests:** fixture transcripts (made up, no real content) with repeated `message.id`s, a copied
  (resumed) file, a half-written last line, a truncated file and a file whose inode changed; the index
  round-trips, and a bad index file is rebuilt. Tests point `CLAUDE_CONFIG_DIR` and `XDG_CACHE_HOME` at temp dirs.
- **Parser tests:** counters that go backwards read 0; a thread missing from the older sample reads 0
  (whole-disk filter); mount filters; network inclusion: the default rule (physical on, the rest off), `include` and
  `exclude` overrides, re-summing when they change, bytes since start.
- **Network UI tests:** split bars at fill levels 0, partial and full on both halves (unit tests in
  `ui/bars.rs`, snapshots with known rates), the `all` row, the interface grid and its row cap, the
  aggregate-only mode, a click on a row checkbox and the settings checkboxes (with the `per_interface`
  toggle) change the config and mark it for saving, an old `interface = "all"` config parses.
- **Input tests:** synthetic mouse events at cells found in the rendered screen produce the expected
  change, including after zoom, hiding a panel and resize (`tests/input.rs`). `tests/pty.rs` runs the real
  binary in a pty (`script`, no display variables, temp config/cache/runtime dirs, usage server pointed at a closed
  port): Ctrl+C and `q` restore the terminal, a panic (debug builds, `VITALS_TEST_PANIC`) restores it
  before its message, `--once` prints one frame.
- **Budget checks:** `scripts/bench.sh` runs vitals in a pty for 60 s (`script -qfc` with no display
  variables) at 200 × 50 after one uncounted `--once` run that fills the token index, and reports CPU time
  (after the first 5 s), steady RSS and first-frame time from `/proc/self/stat` and `status` (via the hidden
  `--stats` flag), plus the binary size; it fails above the §8 numbers. It is separate from `check.sh`
  because it takes over a minute. The counting-allocator test (`tests/alloc.rs`) enforces zero allocations
  per tick and per draw.
- **Web tests** (`tests/web.rs`, §14): headless. A server on `127.0.0.1:0` over fixture `/proc` and `/sys`, a
  temp TLS folder whose CA and leaf `openssl` makes inside the test (never a real certificate folder), a plain
  `TcpStream` client and a rustls client that trusts the test CA. They check: plain HTTP serves the setup page
  (with the CA's SHA-256 fingerprint) and `/certificate.crt` and redirects everything else; HTTPS needs the
  key (401 otherwise), `?key=` sets the `HttpOnly; Secure; SameSite=Strict` cookie and redirects, the cookie
  then opens the page and assets, and the manifest, icons and service worker stay open; `/vitals` is the
  readout object; `/vitals/stream` sends `history` (with layout, theme, points) and then `sample`; no
  certificate folder means plain HTTP with `"tls": false`; a replaced leaf is served to new connections
  without a restart and a half-written one keeps the last good leaf; the stream cap answers 503 and a closed
  client frees its slot. The page's JavaScript is not run by any test (no browser may open); `node --check`
  covers its syntax only.
- `scripts/check.sh`: fmt, clippy `-D warnings`, tests, release build, binary size check, `CHECK PASS`.
  `scripts/install.sh` and `uninstall.sh` install to `~/.local/bin/vitals`, user-local only;
  `install.sh --service` also installs and enables the `vitals-web.service` user unit, `uninstall.sh` removes it.

Usage-server reading on 2026-10-28, used as a fixture:
`{"reading":{"session":{"percent":11,"resets_at":1793253540000,...},"week":{"percent":98,...},"models":[{"label":"Week (Fable)","percent":0,...}]},"stale":false,...}`

## 12. Build order

1. Samplers, history and `--once` with plain-text output; proves the numbers against the conventional definitions
   `/api/system/stats`.
2. Layout, braille graph, meter mode, truecolor `default` theme.
3. Claude row (feed, then usage server), pace markers, day ticks.
4. Mouse hit map, every action in §6, config save.
5. Token indexer and the Tokens panel.
6. Bars mode, block and ascii styles, `ramp` and `mono` themes, 256 and 16 color fallbacks.
7. Benchmark pass against §8, install.

## 13. Decisions (Corbin)

1. **Name:** `vitals`.
2. **Shared code:** `pace.rs`, the feed reader and writer, and the reset and stale words move from the
   applet into the path crate `../../libs/claude-usage-core`. The applet
   and vitals both depend on it by path.
3. **Disk:** measures both fullness (space used per mount) and busyness (I/O busy %). Both are shown; there
   is no setting to choose between them.
4. **GPU:** no GPU panel.
5. **Theme:** `default`, btop style, is the default.
6. **Claude orange:** the Claude row and the Tokens panel use `#D97757` (§7).
7. **Token usage over time:** in scope as panel 6 (§3.2, §5).
8. **2026-10-29, layout v2:** bars and readouts are the focus, graphs are compact and secondary. Each
   section stacks timeline (compact, adjustable height) → bars → summary, placed by data-driven rows
   in the config (§4). Panel modes are replaced by per-part toggles.
9. **2026-10-29:** a settings button and a help button at the top right; settings toggles each section
   and, under it, braille and bars; help explains every readout (§4).
10. **2026-10-29:** section readouts on the command line: `vitals --cpu --json` and friends (§9).
11. **2026-10-29, v3:** the Claude section uses the same bar style as the rest, rows from the applet
    readout (`used / expected`); zoom gives the timeline the whole screen; the layout fills the screen
    (timelines share spare rows, sections in a row share height); global bars and graphs toggles (§4).
12. **2026-10-29, v4:** Memory section shows only current utilization (one bar) and its timeline; Network
    shows a down/up bar pair for every physical interface (§4).
13. **2026-10-29, v5:** Network shows one split bar per network (↓ left, ↑ right) with `all` = sum of
    included interfaces, every interface listed with an inclusion checkbox, `per_interface` aggregate-only
    toggle, `i` dropped; every section has a blank row between timeline and bars.

14. **2026-10-29, v6:** a web dashboard, `vitals --serve`, for pinning on a tablet and installing as an app
    (PWA): one port with TLS sniffed from the first byte, a certificate folder you supply (vitals mints
    none), a key in a cookie (replaced in v9, entry 20), uPlot charts, Server-Sent Events; the full design is §14.

15. **2026-10-29, v7:** bars in a section share one column layout; value columns are fixed width per value
    kind, so bars never change width with the data (§4).

16. **2026-10-29, v8:** the web page is redesigned for compactness: `compact` density by default (`comfortable`
    keeps the v7 sizes), stacked layout by default (one section per row, no vertical splits) with `rows` and `custom`
    layouts and per-section controls, square everything, stepped charts, vertical bars where they fit (CPU threads,
    network pairs, token rates), idle interfaces hidden, settings key `vitals.settings.v2` migrating v1 (§14.5).

17. **2026-10-29, v8.3:** the web Claude card shows day notches on the weekly bars (§14.5); the TUI keeps none.
18. **2026-10-29, v8.4:** the auto chart floor of 40 px applies to the plot; the x axis row comes on top and the labels are never dropped on narrow screens (§14.5).
19. **2026-10-29, v8.5 (Corbin: "no fable usage shouldn't show fable at all"):** per-model rows with no usage are not
    drawn, in the TUI and the web page: a model week at 0% in the Claude section (`5 Hour`, `Daily`, `Weekly` stay)
    and a model family with zero tokens in the Tokens section (rate rows stay). Readouts (`--claude`, `--tokens`,
    `/vitals`) are unchanged. The label and value columns do not change when a row appears (v7 rule).

20. **2026-10-29, v9 (Corbin: "I don't want this to be password protected, just SSL secure. It's read-only
    access anyway."):** no key by default. `[web] auth = "none"` opens every endpoint; `auth = "key"` keeps the v6
    key and cookie as an opt-in. A private-network guard (loopback, RFC 1918, link-local, CGNAT 100.64/10, IPv6
    loopback/ULA/link-local) is always on unless `[web] allow_public = true`, matching private-network CA name
    constraints (LAN only). An existing `key = …` in the config is ignored and left untouched (§14.3).

**Dependency:** the Claude feed exists only while `cosmic-applet-claude-usage --statusline` is the Claude Code
statusLine command. If the applet is removed, vitals falls back to the usage server, and the Claude row goes blank
when the usage server is down too.

## 14. Web dashboard (v6, page redesigned in v8; Corbin 2026-10-29)

Corbin wants the dashboard pinned on his tablet, likely installed as an app. `vitals --serve [ADDR:PORT]` is
a headless mode: no TUI, it samples on its own at the config interval and serves a page, one JSON readout
and an event stream. Read-only: no endpoint changes anything, and the config is not re-read while it runs.

### 14.1 Listener, one port, TLS sniffed

- Default `0.0.0.0:4318` (the usage server is 4317), from `[web] listen` or the argument. Std threads: one accept
  thread, one thread per connection (at most 96 at once; more are dropped), one sampler thread. No async runtime.
- The first byte of each connection is peeked (5 s to arrive). `0x16` is a TLS handshake and the connection
  becomes HTTPS; anything else is plain HTTP. A connection serves up to 100 requests (HTTP/1.1 keep-alive, 5 s idle timeout, closed on `Connection: close`); the event stream always owns its connection until it ends.
- **With a certificate loaded, plain HTTP serves only** `/setup` (the setup page) and `/certificate.crt`;
  every other path redirects (302) to `/setup`. **Without one, plain HTTP serves everything** (behind the
  key under `auth = "key"`) and the page shows the banner "No certificate: provide a certificate folder, or the dashboard runs without
  HTTPS (no install-as-app, no keep-awake)"; a TLS handshake is dropped. The `history` event carries
  `"tls": true|false` for that banner.
- Read timeout 5 s for the request head, write timeout 10 s (a client that stopped reading is dropped).

### 14.2 Certificate (supplied by you, never new)

- The certificate folder is `$VITALS_DATA_DIR/tls` (default `~/.local/share/vitals/tls`; `[web] tls_dir` overrides): `ca.crt`
  (its local CA, name-constrained to 10/8, 172.16/12, 192.168/16 and `.local`), `server.crt` and `server.key`
  (the leaf for the laptop's LAN IPs and `<host>.local`; a certificate is not bound to a port, so it serves
  4318). vitals reads `server.crt` and `server.key` for TLS, serves `ca.crt` at `/certificate.crt`
  (`application/x-x509-ca-cert`, download name `vitals-certificate.crt`), and never reads `ca.key`.
- TLS is `rustls` with the `ring` provider (TLS 1.3 only, ALPN `http/1.1`).
- **Reload:** the two files' mtime and length are looked at when a connection arrives, at most once a minute.
  A changed pair is loaded again; one that fails to load (half-written) keeps the last good
  configuration and is retried at the next check. A folder that appears later is picked up the same way.
- The setup page shows the CA's SHA-256 fingerprint (`AA:BB:…`) so it can be compared with the one you supplied.

### 14.3 Auth and the private-network guard (v9, Corbin 2026-10-29)

- **Default `[web] auth = "none"`: no key, no cookie.** Every endpoint is open to any connection the guard
  admits. The dashboard is read-only and the transport is HTTPS (§14.2); that is the whole protection. Under
  `auth = "none"`, `--serve` never mints a key and never writes the config; a `key = …` already there is left
  untouched and ignored. A `?key=…` in a URL is accepted and ignored: the request answers 302 to the same
  path and query without `key`, so bookmarks lose it. An unknown `auth` value is treated as `"key"`
  (fail closed).
- **Private-network guard (always on).** The peer address of each connection is checked before anything is
  read. Admitted: 127.0.0.0/8, 10/8, 172.16/12, 192.168/16, 169.254/16, 100.64/10 (CGNAT, Tailscale), IPv6 `::1`,
  `fc00::/7` and `fe80::/10`; an IPv4-mapped IPv6 peer is judged by its IPv4 address. Any other peer is
  dropped (the connection is closed without a response) and logged once per address per hour on stderr.
  `[web] allow_public = true` turns the guard off. It matches private-network CA name constraints (LAN only).
- **Opt-in `auth = "key"`** (the v6 behavior): a key is required for everything except `/setup`,
  `/certificate.crt`, `/manifest.webmanifest`, the icons and `/sw.js`. `[web] key` is 128 random bits
  (`/dev/urandom`), url-safe base64, generated on the first `--serve` in this mode and written back to
  `config.toml` (atomic). The TUI never edits `[web]`: when it saves the config it takes `[web]` from the file on
  disk, so a key minted by a concurrent `--serve` survives (`config::save_keeping_web`). `GET /?key=…` with the
  right key sets the cookie `vitals_key` (`HttpOnly; SameSite=Strict; Path=/; Max-Age` one year, plus `Secure`
  on HTTPS) and redirects to `/`. A request with a valid cookie or a valid `?key=` is accepted; any other gets
  401 and a short page with a field to paste the key (this page exists only in this mode; the dashboard
  page itself has no key UI). Comparison is constant-time. No rate limit (128 bits).
- At start it prints the dashboard URL for each LAN IPv4 (non-loopback, interface up, not a container bridge)
  and for `<host>.local`, `https` when a certificate is loaded, and the setup URL. The URLs carry `?key=` only
  under `auth = "key"`.
- Every response carries `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` and a
  `Content-Security-Policy` (own origin only; no inline script).

### 14.4 Endpoints

| Path | Auth | Content |
|---|---|---|
| `/` | open (key if `auth = "key"`) | the page (`web/index.html`); `/app.js`, `/app.css`, `/uplot.js`, `/uplot.css` follow it |
| `/vitals` | as `/` | one JSON readout of all six sections: the exact text `vitals --all --json` prints (schema 2), serialized once per tick |
| `/vitals/stream` | as `/` | Server-Sent Events (below) |
| `/setup`, `/certificate.crt` | open | §14.1, §14.2 |
| `/manifest.webmanifest`, `/icon.svg`, `/icon-192.png`, `/icon-512.png`, `/icon-maskable-512.png`, `/sw.js` | open | the PWA files (§14.5) |

`HEAD` is answered like `GET`; any other method is 405.

**Stream.** `retry: 3000`, then:

- `event: history` once: `{"interval_ms", "history_min", "tls", "now", "layout", "theme", "tokens", "points"}`.
  `points` are the last `history_min` minutes of compact ticks `{"t": ms, "cpu", "th": [per thread], "mem",
  "dr", "dw", "busy", "nd", "nu", "if": {"wlo1": [down_bps, up_bps]}}` (a ring buffer filled by the sampler,
  capacity `history_min` × 60 000 / interval). `layout` is the server's resolved rows
  (`[[{"id":"cpu","w":1},…],…]`), each section's toggles (`enabled`, `graph`, `bars`, `summary`,
  `window_min`) and the token window; `theme` is the active theme as `#rrggbb` (`fg`, `title`, `border`,
  `muted`, `accent`, `danger`, `claude`, and `series.<name>` as three colors for 0, 50 and 100 percent).
  `tokens` is `{"min": […], "hour": […]}`, sparse `[bucket_start_ms, new_input, output, cache_read]` for the
  non-empty minutes of the last 24 h and hours of the last 30 days.
- `event: sample` per tick: the same JSON as `/vitals`.
- `event: tokens` every 30 s: the `tokens` object again (the indexer's new buckets).
- a `: keep-alive` comment after 15 s of silence.
- At most 16 stream clients; the 17th gets 503. The sampler serializes each tick **once**; client threads
  wait on a condvar and copy one `Arc<str>`. A closed client is noticed at its next write and frees its slot.

### 14.5 The page (v8: compact, Corbin 2026-10-29)

One HTML file plus `app.js` and `app.css`, embedded in the binary, no CDN, working offline on the LAN. Charts
are **uPlot 1.6.32** (MIT), vendored under `web/vendor/` with its license and a README. The page is built for
**compactness**: all six sections fit a 1280x800 landscape tablet without scrolling with the default settings.

**Look (all densities).** Square: `border-radius: 0` on everything (cards, bars, buttons, the sheet, chips), no
shadows, no gradients, no transitions. Charts are **stepped** (`uPlot.paths.stepped({align: 1})`), 1 px lines, a flat
fill at low opacity, no splines or smoothing. Bars are hard-edged blocks that change size at once. Values use
tabular figures. The default theme is the TUI `default` theme's colors; Claude rows use `#D97757`, red when over pace
or at 95 %.

**Density** (a per-device setting): `compact` (default) is a ~12 px monospace UI, 6 px card padding, 4 px gaps, 1 px
borders, the card title on one line with its headline value and (right aligned, muted) its summary, charts
**auto** high (see below; 56 px until the first fit), meters 8 px thick, touch targets in the settings sheet 32 px. `comfortable` is the v7 sizing: system font 15 px,
12 px padding and gaps, charts 120 px, meters 14 px, 44 px targets, the summary on its own line under the title.
**Chart height `auto` (v8.1, the compact default for every section).** After layout the page measures the spare
viewport height (`innerHeight` minus everything that is not an auto chart: header, banner, titles, bars, gaps, and
charts with a manual height) and shares it among the visible auto charts by weight (CPU 1, Memory 0.8, Disk 1,
Network 1, Tokens 1), each PLOT clamped to [40 px, 160 px] (v8.2: the floor was 24, too small to read; v8.4: the 16 px x axis row is added on top, so the chart box is 56 to 176 px), in whole pixels summing to the spare, so the stacked page fits
the viewport exactly when it can and scrolls only when even a 40 px plot per chart does not fit. The fit is one measurement
(reads only) and one write, run at most once per 50 ms burst and a frame later, on resize, orientation change,
visibility change, any setting or layout change, zoom exit and after each render (content height can change); it
writes nothing when the sizes are unchanged, so it cannot loop. A per-section manual height (the − n + control, steps
of 8, first step from 56) overrides auto for that section; the settings control shows `auto` as its value and an
**auto** button returns to it. Comfortable density keeps fixed heights (default 120 px, no auto).
Compact heights otherwise: CPU thread columns 30 px, network pair cells 30 px, Tokens rate columns 22 px, Claude rows
14 px, Disk mounts and busy/read/write on one line from 900 px wide (wrapping below, and also above when a cell would get a track under 60 px; v8.2), Tokens rate columns and model
strip side by side from 600 px; card padding 2/6/4 px.

**Sections** are the same six as the TUI, each with a card. Each part keeps the server's toggles (`graph`, `bars`,
`summary`) unless the device overrides them.

- **CPU:** the overall chart, then **one vertical column per thread**, side by side, equal width and filling the
  card (height = percent, color ramp, the thread index under it, the exact percent in its tooltip; shown under the
  column when zoomed); `all NN%` in the title.
- **Memory:** one thin meter `used  value` and the chart; swap in the summary.
- **Disk:** mirrored read/write chart; thin meters for each mount's fullness (several to a line when the card is
  wide; the mount label column is as wide as the longest mount name plus 1ch, at most 16ch, never truncating shorter names, and the
  tracks take the rest, at least 60 px; if the line cannot hold every mount and the I/O meters at that, the groups wrap onto
  their own lines instead of squeezing), then one line of meters: each device's busy %, read and write against the 5-minute peak.
- **Network:** mirrored down/up chart; the title carries the totals `↓1.2M ↑310K` (the server's `all`). Per
  interface a **mirrored vertical pair** in a fixed-width column (76 px): upload above a midline, download below,
  each half scaled to that interface's own rolling 5-minute peak of that direction, computed in the page from the
  samples (floor 100 kB/s), the name under it (full name and rates in the tooltip; zoomed, the rates are printed). An
  interface whose `included` is false is dimmed and its tooltip says "(not counted)"; `included` absent (schema 1)
  counts everything. **Idle interfaces are hidden:** an interface is shown only if the page saw non-zero traffic on it in
  the last 5 minutes of samples (history included), so it does not flicker; the setting "show idle interfaces"
  (default off) and the zoom view show all. Safety net: more than 12 shown interfaces keep 12 cells plus a `+N more`
  cell that expands (`netAll` in the settings; `show fewer` collapses; zoom shows all).
- **Tokens:** mirrored in/out chart over the tokens window; rate columns (input, output, cache write, cache read per
  minute, each against the highest minute of the 24 h index, fixed 64 px wide with the value under the label) beside
  **one stacked 100 % strip** of today's per-model share (opus, sonnet, haiku, other names; Claude oranges) with a tiny
  legend. **(v8.5)** A model with no tokens in the window is neither a segment nor a legend entry.
- **Claude:** thin rows (no chart) with the pace marker (used against expected) and the values on the same line:
  `23% / 73%  2:00 PM (1h 21m)`. **(v8.5)** A per-model week row whose used percent rounds to 0 is not shown
  (it returns as soon as it is above 0); `5 Hour`, `Daily`, `Weekly` always show. Display only: `/vitals`,
  `claude.models[]` and the other readouts keep every model.
  **Day notches (v8.3; the TUI has none, §4):** the `Weekly` row and each per-model week row (not `5 Hour`, not `Daily`)
  show 6 notches dividing the 7-day bar into days: 1 px, square, a page-background cut through the track and fill
  with 2 px muted stubs above and below the bar, at k/7 of the bar width (k = 1..6; the bar spans reset − 7 d to the
  reset, so day boundaries sit there for any reset time, and rows without `resets_at` look the same). Today's segment
  (from `resets_at`, else from `elapsed_percent`; a model row falls back to the week's reset) is slightly lighter.
  The pace marker stays distinct: 2 px, foreground colour, 3 px taller than the bar at both ends.

**Fixed value columns (v6.1, v7 rule kept):** within a card, label and value columns have fixed widths per value kind
(`--lbw`, `--vw` in `ch`, set per card on `.bars`; tabular figures, values right aligned) and vertical columns have
equal or fixed widths, so nothing moves as numbers change. A card with long values (Claude) puts the value above the bar
below 520 px.

**Chart axes (v8.1):** the x axis is one line of labels: `HH:MM` (`HH:MM:SS` up to a 2-minute window) up to two
days, `M/D` beyond. Tick increments match the window (uPlot `incrs`: seconds only up to 2 minutes, whole minutes
above, days beyond two days) with at least `width / 6` px per label in compact (`/ 10` comfortable), and a label equal
to the previous one is blanked, so none repeats. The charts are rebuilt when the graph or tokens window changes.
**Compact has no y axis (v8.2):** the options carry exactly two axes, a shown x axis and a y axis with `show: false` (uPlot adds a default visible y axis, with black tick labels, when none is given); nothing else draws text on the canvas. The x axis row (16 px) is reserved outside the plot area. (v8.4) The auto layout never drops the x labels, at any width: the plot floor is 40 px and the axis row comes on top; narrow screens scroll. Only a manual box shorter than 56 px hides them. The scale's maximum (`100%`, `1.2M/s`) is a tiny muted label in the chart's top-left corner
(`.cmax`). Comfortable keeps a y axis with 12 px of top padding so its top label is not clipped. Every
chart is resized (uPlot `setSize`) on zoom, unzoom, window resize, orientation change and a height change.

**Layout** (per device, setting `layout`):

- `stacked` (**default**): one section per row, full width, top to bottom in the device's section order (default the
  server's order), like the TUI's column layout. No vertical splits.
- `rows`: the server's TUI rows with its width weights.
- `custom`: a rows editor. Each row holds one or more sections with an integer width weight (1-9, columns
  `minmax(0, Nfr)`); the sheet can add and remove a row (removing moves its sections into the neighbor row), move a
  section left or right inside its row, into the previous or the next row (at the top or bottom a new row is made),
  and change its weight. The first switch to `custom` starts from what was on screen. Missing sections are appended as
  rows of their own; empty rows are skipped by the page. Below 700 px wide every row is one column.

**Per section** (sheet, per device): visible, timeline / bars / summary toggles (like the TUI), chart height (px,
steps of 8, minimum 24; default `auto` in compact, 120 in comfortable), order (stacked). **Global:** density, layout, graph window (1, 2, 5, 10,
30 minutes; the page keeps up to 30 minutes of samples after connecting), tokens window (1 h to 30 d), theme (server,
dark, black, light), show idle interfaces, **Reset layout** (layout, rows, order, section settings) and **Reset
everything**. The sheet also lists the network inclusion (read-only), the certificate link (on HTTPS) and an install
button when the browser offers it.

**Storage:** `localStorage` key `vitals.settings.v2`, every access in `try/catch` (the page works with it blocked):
`{density, layout, rows, order, sec: {id: {show, graph, bars, summary, h}}, win, twin, theme, idle, netAll}`. A v1
value (`vitals.settings.v1`, v6/v7) is migrated on the first load: `show` becomes `sec.<id>.show`, and `order`, `win`,
`twin`, `theme`, `netAll` carry over; `cols` has no equivalent and is dropped. Unset values mean the defaults
(compact, stacked, the server's toggles).

**Zoom:** tapping a section title shows that section over the whole screen (the chart takes the spare height, the
vertical columns grow, every item is shown, idle interfaces included); `Esc` or **Close** returns. A fullscreen
button (where the browser offers it); a status pill that says `live` or `reconnecting…`.

- **Reconnecting:** `EventSource` reconnects by itself (and re-requests `history`); a watchdog reopens it when
  nothing arrives for max(3 ticks, 8 s), and it reopens at once when the page becomes visible again, or the network
  returns, after a sleep. A 401 on a probe reloads the page into the 401 page.
- **Smoke test:** `web/test/smoke.mjs` (node, no packages) runs `app.js` against a fake DOM, a stubbed uPlot and
  `web/test/fixture.json` (one real readout) and fails on any exception, `console.error` or card that did not update.
  It also asserts: compact and stacked are the defaults; the CSS has no non-zero `border-radius`, shadow, gradient or
  transition; every chart series is stepped (`align: 1`), 1 px with a flat fill; one CPU column per thread; idle
  interfaces hidden by default and shown by the setting; the custom rows editor builds the expected rows and weights
  and survives a reload from storage v2; the v1 migration; fixed value-column widths; v8.1: the auto height shares the spare height within the clamps and a manual height
  wins, the x axis never repeats a label (5-minute and 24-hour windows), compact charts have no y axis and show the
  max label. `scripts/check.sh` runs it when node exists.
- **PWA:** `/manifest.webmanifest` (name `vitals`, `display: standalone`, `orientation: any`, dark theme color,
  icons: `icon.svg`, 192 and 512 PNGs, a maskable 512 PNG), a service worker (`/sw.js`) that caches the shell only
  (network first, cache when unreachable; it never touches `/vitals*`), and a Screen Wake Lock toggle shown only in a
  secure context, re-acquired when the page becomes visible. The PNGs are committed files made once from `icon.svg`
  with ImageMagick (no build-time step).
- **Setup page** (`/setup`, plain HTTP): why a certificate is needed, a **Download certificate** button, the
  fingerprint, the Android steps (Settings, Security, Encryption & credentials, Install a certificate, CA
  certificate) and the iPad steps (download, Settings, Profile Downloaded, Install; then Settings, General, About,
  Certificate Trust Settings, full trust), then **Open dashboard (HTTPS)**, which points at the host the client used.

### 14.6 Service

`scripts/install.sh --service` also writes `~/.config/systemd/user/vitals-web.service` (`ExecStart=%h/.local/bin/vitals --serve`,
`Restart=on-failure`) and enables it with `systemctl --user enable --now`; `scripts/uninstall.sh` disables and
removes it. The service and a TUI share the token index file at `~/.cache/vitals/tokens-v1.bin`: each runs its
own indexer and rewrites it by atomic rename, so the last writer wins and both converge on the same data.
