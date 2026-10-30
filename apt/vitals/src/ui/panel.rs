// SPDX-License-Identifier: MIT
//! CPU, Memory, Disk and Network sections (spec §4): their timelines, bars, summaries and meter lines.

use std::fmt::Write;

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::Style;

use super::graph::Dir;
use super::section::{Half, Line, bar_row, draw_ring, label_w, mirrored, window_gutter};
use super::{Cx, Label, cells, fmt, meter, put};
use crate::app::Data;
use crate::config::{Config, PanelId};
use crate::input::{Action, Target};
use crate::sample::disk::{DiskDev, Mount};
use crate::theme::Series;

/// Disk timeline and bar ceiling floor, bytes per second.
const DISK_FLOOR_BPS: f32 = 1024.0 * 1024.0;
/// The CPU value column: a percent, `100%`.
const CPU_VAL_W: u16 = fmt::PERCENT_W;
/// A thread's cell: label column 4 (`all`, or three digits, and a space), bar at least 8, space, value,
/// and a space between cells.
const CPU_CELL_W: u16 = 4 + 8 + 1 + CPU_VAL_W + 1;

fn points(cfg: &Config, data: &Data, id: PanelId, minutes: u32) -> usize {
    let _ = id;
    crate::history::points_for_window(
        std::time::Duration::from_secs(u64::from(minutes) * 60),
        std::time::Duration::from_millis(cfg.interval_ms),
    )
    .min(data.rings.cpu.capacity())
}

fn window_points(cfg: &Config, data: &Data, id: PanelId) -> usize {
    points(cfg, data, id, cfg.window_min(id).unwrap_or(2))
}

fn pct_text(t: &mut String, p: f32) {
    t.clear();
    fmt::percent(t, p);
}

// ---------------------------------------------------------------- CPU

/// Cells per row and rows of a grid of `n` cells, each `cell_w` wide with a space after it, at an inner
/// width (the CPU threads and the network interfaces flow the same way).
fn grid(n: usize, w: u16, cell_w: u16) -> (u16, u16) {
    let cols = ((w + 1) / cell_w).max(1);
    let rows = (n as u16).div_ceil(cols).max(1);
    ((n as u16).div_ceil(rows).max(1), rows)
}

/// The geometry of a grid cell: all cells of a grid are the same width (`stride - 1`), so the bars of a
/// section line up; leftover cells at the right stay blank. `i` is the cell index, `y0` the first row.
fn grid_cell(r: Rect, cols: u16, i: usize, y0: u16) -> Rect {
    let stride = (r.width + 1) / cols;
    let (row, col) = (i as u16 / cols, i as u16 % cols);
    Rect::new(r.x + col * stride, y0 + row, stride - 1, 1)
}

/// Threads per row and rows of the thread grid at an inner width.
fn cpu_grid(threads: usize, w: u16) -> (u16, u16) {
    grid(threads, w, CPU_CELL_W)
}

pub fn cpu_bar_rows(data: &Data, cfg: &Config, w: u16) -> (u16, u16) {
    let t = data.sys.cpu.percent().len();
    if t == 0 || !cfg.section.cpu.per_thread {
        return (1, 1);
    }
    (1 + cpu_grid(t, w).1, 1)
}

pub fn cpu_headline(cx: &mut Cx) -> Style {
    let c = &cx.data.sys.cpu;
    let t = &mut cx.sc.text2;
    t.clear();
    if !c.ready() {
        t.push('…');
        return cx.theme.muted_style();
    }
    fmt::percent(t, c.mean());
    Style::new().fg(cx.theme.ramp(Series::Cpu, c.mean()))
}

pub fn cpu_timeline(cx: &mut Cx, buf: &mut Buffer, area: Rect) {
    let (theme, cfg, data) = (cx.theme, cx.cfg, cx.data);
    let conf = &cfg.section.cpu;
    let g = window_gutter(cx, buf, area, Label::minutes(conf.window_min).as_str());
    draw_ring(
        buf,
        &mut cx.sc.cols,
        &data.rings.cpu,
        window_points(cfg, data, PanelId::Cpu),
        g,
        100.0,
        Dir::Up,
        conf.c.style,
        &|_, p, _| theme.ramp(Series::Cpu, p),
    );
}

/// The CPU label column: the widest label (`all`, or the last thread number) and a space.
fn cpu_label_w(threads: usize) -> u16 {
    let digits = (threads.max(2) - 1).ilog10() as u16 + 1;
    digits.max(3) + 1
}

pub fn cpu_bars(cx: &mut Cx, buf: &mut Buffer, r: Rect) {
    let (theme, cfg, data) = (cx.theme, cx.cfg, cx.data);
    let sc = &mut *cx.sc;
    let threads = data.sys.cpu.percent();
    let color = |v: f32| theme.ramp(Series::Cpu, v);
    // One column layout for the `all` row and every thread cell.
    let cols_w = (cpu_label_w(threads.len()), CPU_VAL_W);
    sc.text.clear();
    fmt::percent(&mut sc.text, data.sys.cpu.mean());
    bar_row(
        buf,
        theme,
        Rect::new(r.x, r.y, r.width, 1),
        "all",
        cols_w,
        data.sys.cpu.mean(),
        &color,
        &sc.text,
    );
    if r.height < 2 || !cfg.section.cpu.per_thread || threads.is_empty() {
        return;
    }
    let (cols, _) = cpu_grid(threads.len(), r.width);
    for (i, &p) in threads.iter().enumerate() {
        let cell = grid_cell(r, cols, i, r.y + 1);
        if cell.y >= r.y + r.height {
            break;
        }
        sc.text.clear();
        fmt::percent(&mut sc.text, p);
        sc.text2.clear();
        let _ = write!(sc.text2, "{i}");
        bar_row(buf, theme, cell, &sc.text2, cols_w, p, &color, &sc.text);
    }
}

pub fn cpu_summary(cx: &mut Cx, buf: &mut Buffer, r: Rect) {
    let (theme, data) = (cx.theme, cx.data);
    let sc = &mut *cx.sc;
    let c = &data.sys.cpu;
    let mut line = Line::new(r);
    if let Some(f) = c.freq_ghz() {
        sc.text.clear();
        let _ = write!(sc.text, "{f:.1}GHz");
        line.item(buf, theme, &sc.text, theme.plain());
    }
    if let Some(t) = data.sys.hwmon.cpu_c() {
        sc.text.clear();
        let _ = write!(sc.text, "{t:.0}°C");
        line.item(buf, theme, &sc.text, Style::new().fg(theme.temp(t)));
    }
    let [l1, l5, l15] = c.load();
    sc.text.clear();
    let _ = write!(sc.text, "load {l1:.2} {l5:.2} {l15:.2}");
    line.item(buf, theme, &sc.text, theme.plain());
    sc.text.clear();
    let _ = write!(sc.text, "{} threads", c.percent().len());
    line.item(buf, theme, &sc.text, theme.muted_style());
}

pub fn cpu_meter(cx: &mut Cx, buf: &mut Buffer, r: Rect) {
    let (theme, data) = (cx.theme, cx.data);
    let c = &data.sys.cpu;
    let t = &mut cx.sc.text;
    t.clear();
    if c.ready() {
        fmt::percent(t, c.mean());
    } else {
        t.push('…');
    }
    if let Some(temp) = data.sys.hwmon.cpu_c() {
        let _ = write!(t, " {temp:.0}°C");
    }
    meter::line(
        buf,
        theme,
        r,
        "CPU",
        c.mean(),
        &|p| theme.ramp(Series::Cpu, p),
        t,
    );
}

// ---------------------------------------------------------------- Memory

/// The Memory value column: bytes and a percent, `1023G 100%`.
const MEM_VAL_W: u16 = fmt::SIZE_W + 1 + fmt::PERCENT_W;

pub fn mem_bar_rows(_: &Data, _: &Config) -> (u16, u16) {
    (1, 1)
}

pub fn mem_headline(cx: &mut Cx) -> Style {
    let p = cx.data.sys.mem.mem().used_percent();
    pct_text(&mut cx.sc.text2, p);
    Style::new().fg(cx.theme.ramp(Series::Mem, p))
}

pub fn mem_timeline(cx: &mut Cx, buf: &mut Buffer, area: Rect) {
    let (theme, cfg, data) = (cx.theme, cx.cfg, cx.data);
    let conf = &cfg.section.memory;
    let g = window_gutter(cx, buf, area, Label::minutes(conf.window_min).as_str());
    draw_ring(
        buf,
        &mut cx.sc.cols,
        &data.rings.mem,
        window_points(cfg, data, PanelId::Mem),
        g,
        100.0,
        Dir::Up,
        conf.c.style,
        &|_, p, _| theme.ramp(Series::Mem, p),
    );
}

/// One bar: current utilization, `used` as a percent of the total, with the bytes (`11.7G 51%`).
pub fn mem_bars(cx: &mut Cx, buf: &mut Buffer, r: Rect) {
    let (theme, data) = (cx.theme, cx.data);
    let sc = &mut *cx.sc;
    let m = data.sys.mem.mem();
    let p = m.used_percent();
    sc.text.clear();
    fmt::size(&mut sc.text, m.used() as f64);
    sc.text.push(' ');
    fmt::percent(&mut sc.text, p);
    bar_row(
        buf,
        theme,
        Rect::new(r.x, r.y, r.width, 1),
        "used",
        (label_w(4), MEM_VAL_W),
        p,
        &|v| theme.ramp(Series::Mem, v),
        &sc.text,
    );
}

pub fn mem_summary(cx: &mut Cx, buf: &mut Buffer, r: Rect) {
    let (theme, data) = (cx.theme, cx.data);
    let sc = &mut *cx.sc;
    let m = data.sys.mem.mem();
    let mut line = Line::new(r);
    sc.text.clear();
    fmt::size(&mut sc.text, m.used() as f64);
    sc.text.push_str(" / ");
    fmt::size(&mut sc.text, m.total as f64);
    line.item(buf, theme, &sc.text, theme.plain());
}

pub fn mem_meter(cx: &mut Cx, buf: &mut Buffer, r: Rect) {
    let (theme, data) = (cx.theme, cx.data);
    let m = data.sys.mem.mem();
    let t = &mut cx.sc.text;
    t.clear();
    fmt::size(t, m.used() as f64);
    t.push('/');
    fmt::size(t, m.total as f64);
    meter::line(
        buf,
        theme,
        r,
        "Mem",
        m.used_percent(),
        &|p| theme.ramp(Series::Mem, p),
        t,
    );
}

// ---------------------------------------------------------------- Disk

enum DiskRow<'a> {
    Mount(&'a Mount),
    Busy(&'a DiskDev),
    Read,
    Write,
}

/// The disk bars in order: `/` first, the other mounts, each whole disk's busy %, read, write.
fn disk_row(data: &Data, i: usize) -> Option<DiskRow<'_>> {
    let mounts = data.sys.disk.mounts();
    let devs = data.sys.disk.devices();
    let root = mounts.iter().position(|m| m.target == "/").unwrap_or(0);
    if i < mounts.len() {
        let idx = match i {
            0 => root,
            k if k > root => k,
            k => k - 1,
        };
        return mounts.get(idx).map(DiskRow::Mount);
    }
    let j = i - mounts.len();
    if j < devs.len() {
        return Some(DiskRow::Busy(&devs[j]));
    }
    match j - devs.len() {
        0 => Some(DiskRow::Read),
        1 => Some(DiskRow::Write),
        _ => None,
    }
}

/// The Disk value column, the widest of its kinds: a mount's `100% 1023G/1023G`, a disk's `busy 100%`
/// and a rate `1023K/s`.
const DISK_VAL_W: u16 = fmt::PERCENT_W + 1 + fmt::SIZE_W + 1 + fmt::SIZE_W;

pub fn disk_bar_rows(data: &Data) -> (u16, u16) {
    let n = data.sys.disk.mounts().len() + data.sys.disk.devices().len() + 2;
    (n as u16, 1)
}

pub fn disk_headline(cx: &mut Cx) -> Style {
    let busy = cx.data.sys.disk.rates().busy_pct;
    let t = &mut cx.sc.text2;
    t.clear();
    let _ = write!(t, "busy {busy:.0}%");
    Style::new().fg(cx.theme.ramp(Series::Cpu, busy))
}

/// The highest rate of the last 5 minutes, at least the floor.
fn disk_peak(cfg: &Config, data: &Data) -> (f32, f32) {
    let n = points(cfg, data, PanelId::Disk, 5);
    let f = |r: &crate::history::Ring| r.max_over(n).unwrap_or(0.0).max(DISK_FLOOR_BPS);
    (f(&data.rings.disk_read), f(&data.rings.disk_write))
}

pub fn disk_timeline(cx: &mut Cx, buf: &mut Buffer, area: Rect) {
    let (cfg, data) = (cx.cfg, cx.data);
    let conf = &cfg.section.disk;
    let g = window_gutter(cx, buf, area, Label::minutes(conf.window_min).as_str());
    let n = window_points(cfg, data, PanelId::Disk);
    let max = data
        .rings
        .disk_read
        .max_over(n)
        .unwrap_or(0.0)
        .max(data.rings.disk_write.max_over(n).unwrap_or(0.0))
        .max(DISK_FLOOR_BPS);
    mirrored(
        cx,
        buf,
        g,
        conf.c.style,
        [
            Half {
                label: "read",
                ring: &data.rings.disk_read,
                max,
                series: Series::DiskRead,
            },
            Half {
                label: "write",
                ring: &data.rings.disk_write,
                max,
                series: Series::DiskWrite,
            },
        ],
        n,
    );
}

pub fn disk_bars(cx: &mut Cx, buf: &mut Buffer, r: Rect) {
    let (theme, cfg, data) = (cx.theme, cx.cfg, cx.data);
    let sc = &mut *cx.sc;
    let rates = data.sys.disk.rates();
    let (peak_r, peak_w) = disk_peak(cfg, data);
    // One column layout for every row: the widest label of all rows (not just the visible ones), and a value
    // column as wide as the widest value kind.
    let longest = (0..)
        .map_while(|i| match disk_row(data, i)? {
            DiskRow::Mount(m) => Some(cells(&m.target)),
            DiskRow::Busy(d) => Some(cells(&d.name)),
            _ => Some(5),
        })
        .max()
        .unwrap_or(4);
    let lw = (label_w(longest), DISK_VAL_W);
    for i in 0..usize::from(r.height) {
        let Some(row) = disk_row(data, i) else { break };
        let rect = Rect::new(r.x, r.y + i as u16, r.width, 1);
        sc.text.clear();
        match row {
            DiskRow::Mount(m) => {
                let p = m.used_percent();
                fmt::percent(&mut sc.text, p);
                sc.text.push(' ');
                fmt::size(&mut sc.text, m.used_bytes as f64);
                sc.text.push('/');
                fmt::size(&mut sc.text, m.total_bytes as f64);
                bar_row(
                    buf,
                    theme,
                    rect,
                    &m.target,
                    lw,
                    p,
                    &|v| theme.ramp(Series::Cpu, v),
                    &sc.text,
                );
            }
            DiskRow::Busy(d) => {
                sc.text.push_str("busy ");
                fmt::percent(&mut sc.text, d.busy_pct);
                bar_row(
                    buf,
                    theme,
                    rect,
                    &d.name,
                    lw,
                    d.busy_pct,
                    &|v| theme.ramp(Series::Cpu, v),
                    &sc.text,
                );
            }
            DiskRow::Read => {
                fmt::rate(&mut sc.text, rates.read_bps);
                let p = rates.read_bps as f32 / peak_r * 100.0;
                bar_row(
                    buf,
                    theme,
                    rect,
                    "read",
                    lw,
                    p,
                    &|v| theme.ramp(Series::DiskRead, v),
                    &sc.text,
                );
            }
            DiskRow::Write => {
                fmt::rate(&mut sc.text, rates.write_bps);
                let p = rates.write_bps as f32 / peak_w * 100.0;
                bar_row(
                    buf,
                    theme,
                    rect,
                    "write",
                    lw,
                    p,
                    &|v| theme.ramp(Series::DiskWrite, v),
                    &sc.text,
                );
            }
        }
    }
}

pub fn disk_summary(cx: &mut Cx, buf: &mut Buffer, r: Rect) {
    let (theme, data) = (cx.theme, cx.data);
    let sc = &mut *cx.sc;
    let d = data.sys.disk.rates();
    let mut line = Line::new(r);
    sc.text.clear();
    sc.text.push_str("read ");
    fmt::rate(&mut sc.text, d.read_bps);
    sc.text.push_str("  write ");
    fmt::rate(&mut sc.text, d.write_bps);
    line.item(buf, theme, &sc.text, theme.plain());
    let busiest = data
        .sys
        .disk
        .devices()
        .iter()
        .max_by(|a, b| a.busy_pct.total_cmp(&b.busy_pct));
    if let Some(b) = busiest {
        sc.text.clear();
        let _ = write!(sc.text, "busiest {} {:.0}%", b.name, b.busy_pct);
        line.item(buf, theme, &sc.text, theme.plain());
    }
    if let Some(t) = data.sys.hwmon.nvme_c() {
        sc.text.clear();
        let _ = write!(sc.text, "{t:.0}°C");
        line.item(buf, theme, &sc.text, Style::new().fg(theme.temp(t)));
    }
    let mounts = data.sys.disk.mounts();
    if let Some(m) = mounts.iter().find(|m| m.target == "/").or(mounts.first()) {
        sc.text.clear();
        let _ = write!(sc.text, "{} free ", m.target);
        fmt::size(&mut sc.text, m.avail_bytes as f64);
        line.item(buf, theme, &sc.text, theme.plain());
    }
}

pub fn disk_meter(cx: &mut Cx, buf: &mut Buffer, r: Rect) {
    let (theme, data) = (cx.theme, cx.data);
    let mounts = data.sys.disk.mounts();
    let root = mounts.iter().find(|m| m.target == "/").or(mounts.first());
    let t = &mut cx.sc.text;
    t.clear();
    if let Some(m) = root {
        let _ = write!(t, "{} {:.0}%  ", m.target, m.used_percent());
    }
    let _ = write!(t, "busy {:.0}%", data.sys.disk.rates().busy_pct);
    let full = root.map_or(0.0, |m| m.used_percent());
    meter::line(
        buf,
        theme,
        r,
        "Disk",
        full,
        &|p| theme.ramp(Series::Cpu, p),
        t,
    );
}

// ---------------------------------------------------------------- Network

/// Value column of both the `all` row (`1023K/s`) and every interface cell (`1023K`, right-aligned in the
/// same width), so all split bars of the section have the same label, value and bar columns.
const NET_VAL_W: u16 = fmt::RATE_W;
/// The smallest split bar worth drawing in an interface cell.
const NET_BAR_MIN: u16 = 9;
/// Longest interface name shown whole; longer ones end in `…`.
const NET_NAME_MAX: u16 = 10;
/// The interface grid never takes more rows than this; the rest are one `+N more` cell (every interface
/// stays reachable in the settings popup). Dozens of docker veths must not push every other section out.
const NET_GRID_MAX_ROWS: u16 = 6;

pub fn net_headline(cx: &mut Cx) -> Style {
    let rate = cx.data.sys.net.total();
    let t = &mut cx.sc.text2;
    t.clear();
    t.push('↓');
    fmt::rate(t, rate.down_bps);
    t.push_str(" ↑");
    fmt::rate(t, rate.up_bps);
    Style::new().fg(cx.theme.ramp(Series::NetDown, 100.0))
}

pub fn net_timeline(cx: &mut Cx, buf: &mut Buffer, area: Rect) {
    let (cfg, data) = (cx.cfg, cx.data);
    let conf = &cfg.section.network;
    let peak = data.sys.net.peak();
    let g = window_gutter(cx, buf, area, Label::minutes(conf.window_min).as_str());
    mirrored(
        cx,
        buf,
        g,
        conf.c.style,
        [
            Half {
                label: "down",
                ring: &data.rings.net_down,
                max: peak.down_bps as f32,
                series: Series::NetDown,
            },
            Half {
                label: "up",
                ring: &data.rings.net_up,
                max: peak.up_bps as f32,
                series: Series::NetUp,
            },
        ],
        window_points(cfg, data, PanelId::Net),
    );
}

/// The name column of the interface grid: the longest name, 3 to [`NET_NAME_MAX`] cells.
fn net_name_w(data: &Data) -> u16 {
    data.sys
        .net
        .interfaces()
        .map(|i| cells(&i.name))
        .max()
        .unwrap_or(3)
        .clamp(3, NET_NAME_MAX)
}

/// One interface cell with its trailing space: `[x]`, name, down value, bar, up value.
fn net_cell_w(name_w: u16) -> u16 {
    3 + name_w + 1 + NET_VAL_W + 1 + NET_BAR_MIN + 1 + NET_VAL_W + 1
}

/// The `all` row, then (when `per_interface` is on) the interface grid; the `all` row alone is the
/// headline that stays when space is short.
pub fn net_bar_rows(data: &Data, cfg: &Config, w: u16) -> (u16, u16) {
    let n = data.sys.net.interfaces().count();
    if n == 0 || !cfg.section.network.per_interface {
        return (1, 1);
    }
    (
        1 + grid(n, w, net_cell_w(net_name_w(data)))
            .1
            .min(NET_GRID_MAX_ROWS),
        1,
    )
}

/// One split row, its values in `sc.text` (down) and `sc.text2` (up): the label column of `label_w`
/// cells is already drawn; then the down value right-aligned in `val_w`, the split bar, the up value.
/// Too narrow for a bar, it keeps just the two values.
fn split_row(
    cx: &mut Cx,
    buf: &mut Buffer,
    r: Rect,
    (label_w, val_w): (u16, u16),
    pcts: (f32, f32),
) {
    let theme = cx.theme;
    let (down, up) = (&cx.sc.text, &cx.sc.text2);
    let bar_w = i32::from(r.width) - i32::from(label_w) - 2 * i32::from(val_w) - 2;
    let dx = r.x + label_w;
    if bar_w < 5 {
        let n = put(
            buf,
            dx,
            r.y,
            down,
            theme.plain(),
            r.width.saturating_sub(label_w),
        );
        let at = dx + n + 1;
        put(
            buf,
            at,
            r.y,
            up,
            theme.plain(),
            (r.x + r.width).saturating_sub(at),
        );
        return;
    }
    // An odd width keeps the divider in the middle; a spare cell stays blank.
    let mut bar_w = bar_w as u16;
    if bar_w.is_multiple_of(2) {
        bar_w -= 1;
    }
    put(
        buf,
        dx + val_w - cells(down).min(val_w),
        r.y,
        down,
        theme.plain(),
        val_w,
    );
    let bx = dx + val_w + 1;
    let down_c = |p: f32| theme.ramp(Series::NetDown, p);
    let up_c = |p: f32| theme.ramp(Series::NetUp, p);
    super::bars::split_bar(
        buf,
        (bx, r.y, bar_w),
        pcts,
        (&down_c, &up_c),
        theme.plain(),
        theme.muted_style(),
    );
    put(buf, bx + bar_w + 1, r.y, up, theme.plain(), val_w);
}

fn pct_of(bps: f64, peak: f64) -> f32 {
    (bps / peak.max(1.0) * 100.0) as f32
}

pub fn net_bars(cx: &mut Cx, buf: &mut Buffer, r: Rect) {
    let (theme, cfg, data) = (cx.theme, cx.cfg, cx.data);
    let net = &data.sys.net;
    // The `all` row: the sum of the included interfaces against its own 5-minute peaks.
    let (rate, peak) = (net.total(), net.peak());
    cx.sc.text.clear();
    fmt::rate(&mut cx.sc.text, rate.down_bps);
    cx.sc.text2.clear();
    fmt::rate(&mut cx.sc.text2, rate.up_bps);
    // One label column for the `all` row and every interface cell (`[x]`, the name column, a space).
    let n = net.interfaces().count();
    let name_w = net_name_w(data);
    let label_w = if cfg.section.network.per_interface && n > 0 {
        3 + name_w + 1
    } else {
        4
    };
    put(
        buf,
        r.x,
        r.y,
        "all",
        theme.muted_style(),
        label_w.min(r.width),
    );
    split_row(
        cx,
        buf,
        Rect::new(r.x, r.y, r.width, 1),
        (label_w, NET_VAL_W),
        (
            pct_of(rate.down_bps, peak.down_bps),
            pct_of(rate.up_bps, peak.up_bps),
        ),
    );
    if r.height < 2 || !cfg.section.network.per_interface || n == 0 {
        return;
    }
    let (cols, rows) = grid(n, r.width, net_cell_w(name_w));
    let rows = rows.min(NET_GRID_MAX_ROWS).min(r.height - 1);
    // Too many for the grid: its last cell says how many are not shown.
    let capacity = usize::from(cols * rows);
    let shown = if n > capacity { capacity - 1 } else { n };
    let cell_at = |i: usize| grid_cell(r, cols, i, r.y + 1);
    if shown < n {
        let cell = cell_at(shown);
        cx.sc.text.clear();
        let _ = write!(cx.sc.text, "+{} more: ⚙ settings", n - shown);
        put(
            buf,
            cell.x,
            cell.y,
            &cx.sc.text,
            theme.muted_style(),
            cell.width,
        );
    }
    for (i, f) in net.interfaces().enumerate().take(shown) {
        let cell = cell_at(i);
        let label_style = if f.included() {
            theme.plain()
        } else {
            theme.muted_style()
        };
        // `[x]` / `[ ]`: a click flips whether the interface counts toward `all`.
        let tick = if f.included() { "[x]" } else { "[ ]" };
        put(buf, cell.x, cell.y, tick, label_style, cell.width);
        if cx.ui.overlay == crate::app::Overlay::None {
            cx.hits.push(
                Rect::new(cell.x, cell.y, 3.min(cell.width), 1),
                Target::Do(Action::ToggleIface(i)),
            );
        }
        let name_x = cell.x + 3;
        let room = cell.width.saturating_sub(3).min(name_w);
        if cells(&f.name) > room && room > 0 {
            let cut = f
                .name
                .char_indices()
                .nth(usize::from(room - 1))
                .map_or(f.name.len(), |(b, _)| b);
            let n = put(buf, name_x, cell.y, &f.name[..cut], label_style, room);
            put(buf, name_x + n, cell.y, "…", label_style, 1);
        } else {
            put(buf, name_x, cell.y, &f.name, label_style, room);
        }
        let (rate, peak) = (f.rate(), f.peak());
        cx.sc.text.clear();
        fmt::size(&mut cx.sc.text, rate.down_bps);
        cx.sc.text2.clear();
        fmt::size(&mut cx.sc.text2, rate.up_bps);
        split_row(
            cx,
            buf,
            cell,
            (label_w, NET_VAL_W),
            (
                pct_of(rate.down_bps, peak.down_bps),
                pct_of(rate.up_bps, peak.up_bps),
            ),
        );
    }
}

pub fn net_summary(cx: &mut Cx, buf: &mut Buffer, r: Rect) {
    let (theme, data) = (cx.theme, cx.data);
    let sc = &mut *cx.sc;
    let net = &data.sys.net;
    let mut line = Line::new(r);
    sc.text.clear();
    for (i, n) in net.included().enumerate() {
        if i > 0 {
            sc.text.push(' ');
        }
        sc.text.push_str(&n.name);
    }
    line.item(buf, theme, &sc.text, theme.plain());
    let off = net.interfaces().filter(|i| !i.included()).count();
    if off > 0 {
        sc.text.clear();
        let _ = write!(sc.text, "{off} off");
        line.item(buf, theme, &sc.text, theme.muted_style());
    }
    let (rx, tx) = data.net_since_start();
    sc.text.clear();
    fmt::size(&mut sc.text, (rx + tx) as f64);
    sc.text.push_str(" since start");
    line.item(buf, theme, &sc.text, theme.plain());
}

pub fn net_meter(cx: &mut Cx, buf: &mut Buffer, r: Rect) {
    let (theme, data) = (cx.theme, cx.data);
    let rate = data.sys.net.total();
    let peak = data.sys.net.peak();
    let t = &mut cx.sc.text;
    t.clear();
    t.push('↓');
    fmt::rate(t, rate.down_bps);
    t.push_str(" ↑");
    fmt::rate(t, rate.up_bps);
    let pct = (rate.down_bps / peak.down_bps.max(1.0) * 100.0) as f32;
    meter::line(
        buf,
        theme,
        r,
        "Net",
        pct,
        &|p| theme.ramp(Series::NetDown, p),
        t,
    );
}
