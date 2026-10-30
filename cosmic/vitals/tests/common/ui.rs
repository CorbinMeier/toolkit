// SPDX-License-Identifier: MIT
//! A deterministic `App` for the UI tests: fixture /proc and /sys, a fixed clock, UTC, synthetic history
//! and tokens. Never touches the real `~/.claude`, the real config or the live /proc.
#![allow(dead_code)]

use std::sync::{Mutex, Once};
use std::time::{Duration, Instant};

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use vitals::app::App;
use vitals::claude::server::parse_status;
use vitals::config::{Config, PanelId};
use vitals::frame::to_plain;
use vitals::sample::{Root, System};
use vitals::theme::{Depth, ThemeBook, ThemeInputs};
use vitals::tokens::{ByFamily, CacheState, Delta, Family, Msg, Phase, Progress};

use super::{TempDir, copy_tree, fixture, write};

/// 2026-10-28 12:00:00 UTC.
pub const NOW: i64 = 1_793_188_800_000;

static TZ: Once = Once::new();
static SERIAL: Mutex<()> = Mutex::new(());

/// Pins the time zone to UTC in this process. `scripts/check.sh` also exports `TZ=UTC`, which is what
/// makes it hold for every test: libc caches the zone on first use, so this in-process call is only a
/// fallback for a bare `cargo test`, and any test that prints a local time should call it first.
pub fn pin_tz() {
    TZ.call_once(|| {
        // SAFETY: runs once, before the first time lookup in a test that calls it; `Once` blocks the
        // other callers until it returns.
        unsafe { std::env::set_var("TZ", "UTC") };
    });
}

pub fn server_fixture(name: &str) -> String {
    std::fs::read_to_string(fixture(&format!("server/{name}.json"))).unwrap()
}

pub struct Rig {
    pub app: App,
    pub dir: TempDir,
}

/// Builds the app: two samples a second apart on the fixture trees, 120 synthetic history points per
/// ring, a day of token buckets, the `fresh` server reading.
pub fn rig(depth: Depth, edit: impl FnOnce(&mut Config)) -> Rig {
    pin_tz();
    let _g = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
    let dir = TempDir::new("ui");
    copy_tree(&fixture("sys"), &dir.join("sys"));
    copy_tree(&fixture("proc/a"), &dir.join("proc"));
    // Two mounts that exist on any machine; the figures are pinned below.
    write(
        &dir.join("proc/self/mounts"),
        "/dev/nvme0n1p3 / ext4 rw 0 0\n/dev/nvme0n1p5 /tmp ext4 rw 0 0\n",
    );
    let mut sys = System::new(&Root::new(dir.join("proc"), dir.join("sys"))).unwrap();
    let t0 = Instant::now();
    sys.sample(t0).unwrap();
    copy_tree(&fixture("proc/b"), &dir.join("proc"));
    write(
        &dir.join("proc/self/mounts"),
        "/dev/nvme0n1p3 / ext4 rw 0 0\n/dev/nvme0n1p5 /tmp ext4 rw 0 0\n",
    );
    sys.sample(t0 + Duration::from_secs(1)).unwrap();
    for (i, m) in sys.disk.mounts_mut().iter_mut().enumerate() {
        m.total_bytes = 500 << 30;
        m.used_bytes = if i == 0 { 360 << 30 } else { 40 << 30 };
        m.avail_bytes = m.total_bytes - m.used_bytes;
    }
    let mut cfg = Config::default();
    edit(&mut cfg);
    let themes = ThemeBook::new(depth, ThemeInputs::default(), vec![]);
    let mut app = App::new(cfg, themes, sys, NOW);
    app.tick(t0 + Duration::from_secs(1), NOW);

    let r = &mut app.data.rings;
    for i in 0..120 {
        let f = i as f32;
        r.cpu.push(45.0 + 40.0 * (f / 9.0).sin());
        r.mem.push(30.0 + f * 0.2);
        r.disk_read.push(if i % 17 < 5 {
            4.0e6 * (f / 17.0 % 1.0 + 0.2)
        } else {
            2.0e5
        });
        r.disk_write.push(if i % 23 < 8 { 9.0e6 } else { 1.0e5 });
        r.net_down.push(2.0e4 + 6.0e4 * (f / 11.0).sin().abs());
        r.net_up.push(5.0e3 + 1.5e4 * (f / 7.0).cos().abs());
        r.claude_session.push((f / 120.0 * 11.0).min(11.0));
        r.claude_week.push(38.0 + f / 40.0);
    }

    let mut minutes = Vec::new();
    for k in 0..1440i64 {
        let minute = NOW / 60_000 - k;
        if k % 7 < 4 {
            let mut by = ByFamily::default();
            let scale = 1 + ((k * 37) % 11) as u64;
            by[Family::Opus.index()].input = 300 * scale;
            by[Family::Opus.index()].cache_write = 1_500 * scale;
            by[Family::Opus.index()].output = 400 * scale;
            by[Family::Opus.index()].cache_read = 40_000 * scale;
            by[Family::Sonnet.index()].input = 100 * scale;
            by[Family::Sonnet.index()].output = 150 * scale;
            by[Family::Sonnet.index()].cache_read = 9_000 * scale;
            minutes.push((minute, by));
        }
    }
    let mut hours = Vec::new();
    for k in 0..720i64 {
        let mut by = ByFamily::default();
        by[Family::Opus.index()].input = 8_000 + (k as u64 % 13) * 900;
        by[Family::Opus.index()].output = 3_000 + (k as u64 % 7) * 400;
        by[Family::Opus.index()].cache_read = 900_000;
        hours.push((NOW / 3_600_000 - k, by));
    }
    app.data.tokens.apply(Msg::Delta(Delta {
        minutes,
        hours,
        minute_floor: 0,
        hour_floor: 0,
    }));
    app.data.tokens.apply(Msg::Progress(Progress {
        phase: Phase::Live,
        cache: CacheState::Loaded,
        files_done: 3,
        files_total: 3,
        bytes_done: 1,
        bytes_total: 1,
    }));
    set_server(&mut app, "fresh");
    Rig { app, dir }
}

/// Feeds a usage server fixture to the Claude view and prepares its strings for `NOW`.
pub fn set_server(app: &mut App, name: &str) {
    app.data
        .claude
        .set_server(Ok(parse_status(&server_fixture(name)).unwrap()));
    app.data.claude.refresh(NOW);
}

/// One section alone in a `w` x `h` rectangle, as text (trailing blank rows trimmed).
pub fn panel_text(app: &mut App, id: PanelId, w: u16, h: u16) -> String {
    let area = Rect::new(0, 0, w, h);
    let mut buf = Buffer::empty(area);
    vitals::ui::render_one(app, id, area, &mut buf);
    let text = to_plain(&buf);
    format!("{}\n", text.trim_end())
}

/// The whole screen as text.
pub fn screen_text(app: &mut App, w: u16, h: u16) -> String {
    let area = Rect::new(0, 0, w, h);
    let mut buf = Buffer::empty(area);
    vitals::ui::render(app, area, &mut buf);
    to_plain(&buf)
}
