// SPDX-License-Identifier: MIT
//! The help texts (spec §4): what every bar and summary readout measures, where it comes from, its unit
//! and what "full" means. Plain `pub` data, so the help popup and any command-line help can share it.

/// One readout.
pub struct Item {
    pub name: &'static str,
    pub what: &'static str,
    pub source: &'static str,
    pub unit: &'static str,
    pub scale: &'static str,
}

/// One section's help: what the timeline shows, then its bars and its summary items.
pub struct Topic {
    pub title: &'static str,
    pub timeline: &'static str,
    pub bars: &'static [Item],
    pub summary: &'static [Item],
}

/// The tabs: one per section, then the controls.
pub const TABS: usize = 7;

/// The label of tab `i`.
pub fn tab_title(i: usize) -> &'static str {
    TOPICS.get(i).map_or("Controls", |t| t.title)
}

const fn item(
    name: &'static str,
    what: &'static str,
    source: &'static str,
    unit: &'static str,
    scale: &'static str,
) -> Item {
    Item {
        name,
        what,
        source,
        unit,
        scale,
    }
}

const TEMP_SCALE: &str = "colored by heat: muted below 60 °C, yellow 60-80 °C, red above 80 °C";

pub static TOPICS: [Topic; 6] = [
    Topic {
        title: "CPU",
        timeline: "The mean busy percent of all threads over the time window (2 minutes by default; scroll over the timeline or press + and - to change it).",
        bars: &[
            item(
                "all",
                "How busy the CPU is on average: the mean of the thread percents.",
                "/proc/stat, the cpuN lines, fields 1-8; busy = total - (idle + iowait)",
                "percent",
                "100% means every thread was busy for the whole refresh interval",
            ),
            item(
                "0, 1, 2 ...",
                "One bar per hardware thread, as many columns as fit (a bar is at least 8 cells wide). Hidden with per_thread = false.",
                "/proc/stat, the cpuN line of that thread",
                "percent",
                "100% means that thread was busy the whole interval",
            ),
        ],
        summary: &[
            item(
                "frequency",
                "The average clock speed over all cores.",
                "/sys/devices/system/cpu/cpufreq/policy*/scaling_cur_freq",
                "GHz",
                "not a bar: the current value",
            ),
            item(
                "temperature",
                "The CPU die temperature (Tctl on AMD).",
                "hwmon k10temp, temp1_input",
                "degrees Celsius",
                TEMP_SCALE,
            ),
            item(
                "load",
                "Runnable plus waiting processes averaged over the last 1, 5 and 15 minutes.",
                "/proc/loadavg",
                "processes",
                "compare it with the thread count: a load equal to the threads means fully used",
            ),
            item(
                "threads",
                "How many hardware threads are counted.",
                "/proc/stat",
                "count",
                "not a bar",
            ),
        ],
    },
    Topic {
        title: "Memory",
        timeline: "Used memory as a percent of the total over the time window.",
        bars: &[item(
            "used",
            "Current memory utilization: what programs are really using.",
            "/proc/meminfo: MemTotal - MemAvailable",
            "bytes and percent of total",
            "full means all memory is in use",
        )],
        summary: &[item(
            "used / total",
            "Used memory and the installed total. (Cache, available and swap are in vitals --mem.)",
            "/proc/meminfo",
            "bytes",
            "not a bar",
        )],
    },
    Topic {
        title: "Disk",
        timeline: "Read rate on top, write rate below (at one row, their sum), against the highest rate in the window, at least 1 MiB/s.",
        bars: &[
            item(
                "/ , /home ...",
                "One bar per mounted disk: how full it is. Network filesystems are skipped.",
                "statvfs on each mount in /proc/self/mounts; used = (blocks - bfree) x frsize",
                "percent full, then used / total bytes",
                "full means no space left",
            ),
            item(
                "nvme0n1 ...",
                "One bar per whole disk: how busy it is.",
                "/proc/diskstats field 13 (io_ticks), change divided by elapsed milliseconds",
                "percent busy",
                "100% means the disk had I/O in flight for the whole interval",
            ),
            item(
                "read, write",
                "Bytes per second read from and written to all whole disks.",
                "/proc/diskstats fields 6 and 10, times 512",
                "bytes per second",
                "against the highest rate of the last 5 minutes, at least 1 MiB/s",
            ),
        ],
        summary: &[
            item(
                "read, write",
                "The two rates.",
                "/proc/diskstats",
                "bytes per second",
                "not a bar",
            ),
            item(
                "busiest",
                "The busiest whole disk and its busy percent.",
                "/proc/diskstats",
                "percent",
                "not a bar",
            ),
            item(
                "temperature",
                "The NVMe drive temperature.",
                "hwmon nvme, temp1_input",
                "degrees Celsius",
                TEMP_SCALE,
            ),
            item(
                "free",
                "Space still free on /.",
                "statvfs, f_bavail",
                "bytes",
                "not a bar",
            ),
        ],
    },
    Topic {
        title: "Network",
        timeline: "Download on top, upload below (at one row, their sum), against the highest rate of the last 5 minutes, at least 100 kB/s. It is the sum of the included interfaces.",
        bars: &[
            item(
                "all",
                "One split bar: download fills from the centre divider to the left, upload to the right, each against its own 5-minute peak. It is the sum of the included interfaces and is the row that stays when space is short.",
                "/proc/net/dev, the interfaces ticked [x]",
                "bytes per second",
                "each half against the highest summed rate of that direction in the last 5 minutes, at least 100 kB/s",
            ),
            item(
                "[x] <interface>",
                "A split bar for every interface in /proc/net/dev (turn them all off with per interface in settings). The checkbox says whether it counts toward all: click it, or use the Network line of the settings popup. Physical interfaces are on by default (those with /sys/class/net/<if>/device); loopback, VPNs, bridges and docker veths are off. Overrides are saved as include and exclude in the config.",
                "/proc/net/dev",
                "bytes per second",
                "each half against the highest rate of that interface and direction in the last 5 minutes, at least 100 kB/s",
            ),
        ],
        summary: &[
            item(
                "rates",
                "Current download and upload.",
                "/proc/net/dev",
                "bytes per second",
                "not a bar",
            ),
            item(
                "since start",
                "Bytes received plus sent by the included interfaces since vitals started.",
                "/proc/net/dev counters, minus their value at start",
                "bytes",
                "not a bar",
            ),
            item(
                "interfaces",
                "The names of the included interfaces, then how many listed ones are off.",
                "/sys/class/net",
                "names",
                "not a bar",
            ),
        ],
    },
    Topic {
        title: "Tokens",
        timeline: "New input (input plus cache write) upward, output downward, per bucket (cache reads are left out: they are often 100 times larger). v switches to total or cache. The window is 1h, 6h, 24h, 7d or 30d (scroll over the timeline). Dim marks are Claude session resets.",
        bars: &[
            item(
                "input, output, cache wr, cache rd",
                "Tokens per minute over the last 5 minutes: new input, output, cache writes, cache reads.",
                "usage fields in ~/.claude/projects/**/*.jsonl, each reply counted once by message id",
                "tokens per minute",
                "against the busiest minute of the window",
            ),
            item(
                "opus, sonnet ...",
                "One bar per model family used today: its share of today's tokens.",
                "the same transcripts, grouped by the model name",
                "percent of today's tokens, then the count",
                "100% means every token today went to that model",
            ),
        ],
        summary: &[
            item(
                "today",
                "Today's input, output and cache-read tokens (local midnight to now).",
                "the transcripts",
                "tokens",
                "not a bar",
            ),
            item(
                "per minute",
                "The new-input plus output rate of the last 5 minutes.",
                "the transcripts",
                "tokens per minute",
                "not a bar",
            ),
            item(
                "indexing",
                "Progress of the first read of old transcripts, shown only while it runs.",
                "the background indexer",
                "percent",
                "not a bar",
            ),
        ],
    },
    Topic {
        title: "Claude",
        timeline: "Session percent and week percent, as seen while vitals has been running (kept in memory only). Off by default.",
        bars: &[
            item(
                "5 Hour",
                "How much of the 5-hour session limit is used. The bar fills from the left; the bar line marks how much of the window has passed (the pace). The numbers are used / expected, then the reset time with the time left.",
                "the applet's feed file, else the usage server at 127.0.0.1:4317; never claude itself",
                "percent of the limit",
                "full means the limit is used up; orange turns red when used is above expected, or from 95%",
            ),
            item(
                "Daily",
                "How much of today's share of the week allowance is used, against how much of the day has passed.",
                "the week percent and the day of the week",
                "percent of today's slice",
                "100% means today's slice is used",
            ),
            item(
                "Weekly",
                "How much of the weekly limit is used, with the week's reset time.",
                "the feed or the usage server",
                "percent of the limit",
                "full means the limit is used up",
            ),
            item(
                "model names",
                "One bar per model's weekly limit, sharing the weekly reset (usage server data only). A limit with no usage yet has no row.",
                "the usage server",
                "percent of the limit",
                "full means the limit is used up",
            ),
        ],
        summary: &[
            item(
                "resets",
                "When the session and the week reset, in local time.",
                "the reading",
                "clock time",
                "not a bar",
            ),
            item(
                "via, age",
                "Which source gave the reading (feed or server) and how old it is.",
                "the reading",
                "minutes or hours",
                "not a bar",
            ),
            item(
                "Stale",
                "Shown when the reading is old: the time it was last read.",
                "the reading",
                "clock time",
                "not a bar",
            ),
        ],
    },
];

/// The controls tab: keys and what they do (the mouse twin is in the second part of each line).
pub static CONTROLS: &[(&str, &str)] = &[
    ("[⚙] or ,", "settings popup"),
    ("[?] or ?", "this help"),
    ("1-6", "show or hide a section (header chips)"),
    (
        "Shift+1-6",
        "zoom a section to the whole screen; Esc or a click restores",
    ),
    ("click a title", "zoom that section"),
    (
        "g  b  s",
        "timeline, bars, summary of the focused section on or off",
    ),
    (
        "{  }",
        "timeline rows fewer or more (1-8) for the focused section",
    ),
    (
        "S  / right-click title",
        "next graph style: braille, block, ascii",
    ),
    ("+  -  / scroll timeline", "time window longer or shorter"),
    (
        "v  / click Tokens timeline",
        "token series: io, total, cache",
    ),
    (
        "Settings, Network line",
        "per interface on or off, and which interfaces count toward all (click a [x] in the bars too)",
    ),
    ("c  / click Claude bars", "Claude usage details"),
    (
        "Tab, Shift+Tab / click",
        "focus the next, previous or clicked section",
    ),
    (
        "[  ]  / scroll the chip",
        "refresh interval faster or slower",
    ),
    ("t  / click the theme", "next theme"),
    ("p", "pause sampling"),
    (
        "M",
        "mouse capture off or on (Shift+drag selects text while it is on)",
    ),
    ("Esc", "close a popup, else leave the zoom"),
    ("q, Ctrl+C", "quit"),
];
