// SPDX-License-Identifier: MIT
//! Spec §4, bars (v7): every bar row of a section shares one column layout, and the value columns are
//! fixed width, so a bar's start column and length never change with the data. Each test renders one
//! section twice with very different values and compares the bar tracks of both frames. Headless.

mod common;

use std::time::{Duration, Instant};

use common::ui::{Rig, panel_text, rig, set_server};
use vitals::config::PanelId;
use vitals::theme::Depth;

/// Cells a bar is drawn with (track, fills, partial blocks, the split divider and the pace marker).
fn is_bar(c: char) -> bool {
    matches!(
        c,
        '░' | '█' | '▏' | '▎' | '▍' | '▌' | '▋' | '▊' | '▉' | '▐' | '│'
    )
}

/// Per text row, the `(start column, length)` of every run of at least 3 bar cells inside the frame.
fn tracks(text: &str) -> Vec<Vec<(usize, usize)>> {
    text.lines()
        .map(|line| {
            let cells: Vec<char> = line.chars().collect();
            let mut runs = Vec::new();
            if cells.first() != Some(&'│') || cells.len() < 3 {
                return runs;
            }
            let (mut at, end) = (1, cells.len() - 1);
            while at < end {
                if is_bar(cells[at]) {
                    let start = at;
                    while at < end && is_bar(cells[at]) {
                        at += 1;
                    }
                    if at - start >= 3 {
                        runs.push((start, at - start));
                    }
                } else {
                    at += 1;
                }
            }
            runs
        })
        .collect()
}

/// The bar rows of a section must be identical in both frames, and (`single_column`) every row of one
/// frame must share one track.
fn assert_same_tracks(name: &str, a: &str, b: &str, single_column: bool) {
    let (ta, tb) = (tracks(a), tracks(b));
    assert_eq!(ta, tb, "{name}: bar tracks moved with the data\n{a}\n{b}");
    let rows: Vec<_> = ta.iter().filter(|r| !r.is_empty()).collect();
    assert!(!rows.is_empty(), "{name}: no bar rows\n{a}");
    if single_column {
        assert!(
            rows.iter().all(|r| **r == *rows[0]),
            "{name}: rows do not share one track\n{a}"
        );
    }
}

fn rewrite(r: &Rig, file: &str, f: impl Fn(&str) -> String) {
    let path = r.dir.join(file);
    let old = std::fs::read_to_string(&path).unwrap();
    std::fs::write(path, f(&old)).unwrap();
}

/// Words of `line`, one of them replaced by its value plus `by`.
fn bump(line: &str, field: usize, by: u64) -> String {
    let mut w: Vec<String> = line.split_whitespace().map(String::from).collect();
    let v: u64 = w[field].parse().unwrap();
    w[field] = (v + by).to_string();
    w.join(" ")
}

/// A system that has just seen no load at all (`busy = false`) or everything at its limit.
fn system(busy: bool) -> Rig {
    let mut r = rig(Depth::True, |c| c.section.cpu.c.graph = false);
    // CPU: user ticks on every core (100%) or idle ticks (0%).
    let field = if busy { 1 } else { 4 };
    rewrite(&r, "proc/stat", |s| {
        s.lines()
            .map(|l| {
                if l.starts_with("cpu") {
                    bump(l, field, 100_000)
                } else {
                    l.to_string()
                }
            })
            .collect::<Vec<_>>()
            .join("\n")
            + "\n"
    });
    // Memory: nothing used, or all of it.
    rewrite(&r, "proc/meminfo", |s| {
        let total: u64 = s
            .lines()
            .find(|l| l.starts_with("MemTotal"))
            .and_then(|l| l.split_whitespace().nth(1))
            .and_then(|v| v.parse().ok())
            .unwrap();
        s.lines()
            .map(|l| {
                if l.starts_with("MemAvailable") {
                    format!("MemAvailable: {} kB", if busy { 0 } else { total })
                } else {
                    l.to_string()
                }
            })
            .collect::<Vec<_>>()
            .join("\n")
            + "\n"
    });
    // Disk: the whole disk reads and writes about a gigabyte a second and is busy all the time.
    if busy {
        rewrite(&r, "proc/diskstats", |s| {
            s.lines()
                .map(|l| {
                    if l.split_whitespace().nth(2) == Some("nvme0n1") {
                        let l = bump(l, 5, 2_000_000);
                        let l = bump(&l, 9, 2_000_000);
                        bump(&l, 12, 5_000)
                    } else {
                        l.to_string()
                    }
                })
                .collect::<Vec<_>>()
                .join("\n")
                + "\n"
        });
    }
    r.app
        .data
        .sys
        .sample(Instant::now() + Duration::from_secs(2))
        .unwrap();
    for m in r.app.data.sys.disk.mounts_mut() {
        (m.total_bytes, m.used_bytes) = if busy {
            (1023 << 30, 1023 << 30)
        } else {
            (1 << 30, 0)
        };
        m.avail_bytes = m.total_bytes - m.used_bytes;
    }
    r
}

#[test]
fn cpu_bars_keep_their_track_from_0_to_100_percent() {
    for (w, h) in [(100, 14), (200, 20), (60, 14), (40, 12)] {
        let mut quiet = system(false);
        let mut busy = system(true);
        let (a, b) = (
            panel_text(&mut quiet.app, PanelId::Cpu, w, h),
            panel_text(&mut busy.app, PanelId::Cpu, w, h),
        );
        assert!(a.contains(" 0%") && b.contains("100%"), "{a}\n{b}");
        assert_same_tracks(&format!("cpu {w}x{h}"), &a, &b, false);
        // Every thread cell of one frame has the same width: the same track on every grid row.
        let rows: Vec<_> = tracks(&b).into_iter().filter(|r| !r.is_empty()).collect();
        assert!(
            rows[1..].iter().all(|r| r[0] == rows[1][0]),
            "cpu {w}x{h}: grid rows differ\n{b}"
        );
    }
}

#[test]
fn memory_bar_keeps_its_track_from_empty_to_full() {
    for (w, h) in [(60, 8), (100, 8), (40, 12)] {
        let mut quiet = system(false);
        let mut busy = system(true);
        let (a, b) = (
            panel_text(&mut quiet.app, PanelId::Mem, w, h),
            panel_text(&mut busy.app, PanelId::Mem, w, h),
        );
        assert!(a.contains("0B 0%") && b.contains("100%"), "{a}\n{b}");
        assert_same_tracks(&format!("memory {w}x{h}"), &a, &b, true);
    }
}

#[test]
fn disk_bars_keep_one_track_from_idle_to_saturated() {
    for (w, h) in [(60, 14), (100, 14), (40, 12)] {
        let mut quiet = system(false);
        let mut busy = system(true);
        let (a, b) = (
            panel_text(&mut quiet.app, PanelId::Disk, w, h),
            panel_text(&mut busy.app, PanelId::Disk, w, h),
        );
        assert!(
            a.contains("0B/1.0G") && b.contains("1023G/1023G"),
            "{a}\n{b}"
        );
        assert_same_tracks(&format!("disk {w}x{h}"), &a, &b, true);
    }
}

/// A network sampler whose wlo1 moves `rx` bytes in the first second and half of that in the second
/// (so the current rate is `rx / 2`), and a tenth of it upstream.
fn net(dir: &common::TempDir, rx: u64) -> vitals::sample::net::NetSampler {
    let sys = dir.join("sys");
    let dev = |k: u64| {
        let (rx, tx) = match k {
            0 => (0, 0),
            1 => (rx, rx / 10),
            _ => (rx + rx / 2, rx / 10 + rx / 20),
        };
        let mut t = String::from("Inter-|\n face |\n");
        for (name, rx, tx) in [
            ("lo", 0, 0),
            ("wlo1", rx, tx),
            ("enx0", rx / 8, tx / 8),
            ("docker0", 0, 0),
        ] {
            t += &format!("{name}: {rx} 1 0 0 0 0 0 0 {tx} 1 0 0 0 0 0 0\n");
        }
        t
    };
    for n in ["lo", "docker0"] {
        common::write(&sys.join(format!("class/net/{n}/.keep")), "");
    }
    for n in ["wlo1", "enx0"] {
        common::write(&sys.join(format!("class/net/{n}/device/.keep")), "");
    }
    let root = vitals::sample::Root::new(dir.join("proc"), sys);
    common::write(&dir.join("proc/net/dev"), dev(0));
    let mut net = vitals::sample::net::NetSampler::new(&root).unwrap();
    let t0 = Instant::now();
    net.sample(t0).unwrap();
    common::write(&dir.join("proc/net/dev"), dev(1));
    net.sample(t0 + Duration::from_secs(1)).unwrap();
    common::write(&dir.join("proc/net/dev"), dev(2));
    net.sample(t0 + Duration::from_secs(2)).unwrap();
    net
}

#[test]
fn network_split_bars_keep_their_track_from_0_to_999_mb_per_second() {
    for (w, h) in [(100, 12), (200, 14), (60, 12), (40, 12)] {
        let (da, db) = (common::TempDir::new("net0"), common::TempDir::new("net999"));
        let mut quiet = rig(Depth::True, |_| {});
        quiet.app.data.sys.net = net(&da, 0);
        let mut busy = rig(Depth::True, |_| {});
        busy.app.data.sys.net = net(&db, 2 * 999 * 1024 * 1024);
        let (a, b) = (
            panel_text(&mut quiet.app, PanelId::Net, w, h),
            panel_text(&mut busy.app, PanelId::Net, w, h),
        );
        assert!(a.contains("0B/s") && b.contains("999M "), "{a}\n{b}");
        assert_same_tracks(&format!("network {w}x{h}"), &a, &b, false);
        // The centre divider stays put: the interface rows agree on every column of the grid.
        let rows: Vec<_> = tracks(&b).into_iter().filter(|r| !r.is_empty()).collect();
        assert!(
            rows[1..].iter().all(|r| r[0] == rows[1][0]),
            "network {w}x{h}: grid rows differ\n{b}"
        );
    }
}

#[test]
fn network_all_row_aligns_with_a_single_column_grid() {
    let da = common::TempDir::new("net1col");
    let mut r = rig(Depth::True, |_| {});
    r.app.data.sys.net = net(&da, 2 * 999 * 1024 * 1024);
    let text = panel_text(&mut r.app, PanelId::Net, 60, 12);
    let rows: Vec<_> = tracks(&text)
        .into_iter()
        .filter(|r| !r.is_empty())
        .collect();
    if rows.iter().all(|r| r.len() == 1) {
        assert!(rows.iter().all(|r| *r == rows[0]), "{text}");
    }
}

/// Tokens in every minute and hour bucket: `n` of each kind in each family.
fn tokens_of(r: &mut Rig, n: u64) {
    use vitals::tokens::{ByFamily, Delta, Family, Msg};
    let now = common::ui::NOW;
    let by = || {
        let mut by = ByFamily::default();
        for f in [Family::Opus, Family::Sonnet] {
            by[f.index()].input = n;
            by[f.index()].output = n;
            by[f.index()].cache_write = n;
            by[f.index()].cache_read = n;
        }
        by
    };
    let minutes = (0..1440).map(|k| (now / 60_000 - k, by())).collect();
    let hours = (0..720).map(|k| (now / 3_600_000 - k, by())).collect();
    r.app.data.tokens.apply(Msg::Delta(Delta {
        minutes,
        hours,
        minute_floor: 0,
        hour_floor: 0,
    }));
}

#[test]
fn token_bars_keep_one_track_from_1_to_a_trillion() {
    for (w, h) in [(60, 14), (120, 14), (40, 12)] {
        let mut quiet = rig(Depth::True, |_| {});
        tokens_of(&mut quiet, 1);
        let mut busy = rig(Depth::True, |_| {});
        tokens_of(&mut busy, 999_000_000_000);
        let (a, b) = (
            panel_text(&mut quiet.app, PanelId::Tokens, w, h),
            panel_text(&mut busy.app, PanelId::Tokens, w, h),
        );
        assert_same_tracks(&format!("tokens {w}x{h}"), &a, &b, true);
    }
}

#[test]
fn claude_bars_keep_one_track_for_short_and_long_resets() {
    // `fresh`: the weekly reset is days away; `hot`: it is hours away; the numbers differ too.
    for (w, h) in [(60, 12), (100, 12), (200, 12), (40, 12)] {
        let mut fresh = rig(Depth::True, |_| {});
        set_server(&mut fresh.app, "fresh");
        let mut hot = rig(Depth::True, |_| {});
        set_server(&mut hot.app, "hot");
        let (a, b) = (
            panel_text(&mut fresh.app, PanelId::Claude, w, h),
            panel_text(&mut hot.app, PanelId::Claude, w, h),
        );
        assert_same_tracks(&format!("claude {w}x{h}"), &a, &b, true);
    }
}
