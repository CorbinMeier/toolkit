// SPDX-License-Identifier: MIT
//! The Claude section (spec §4): one bar per line of the applet readout (`5 Hour`, `Daily`, `Weekly`, the
//! model weeks) in the same bar style as every other section, with the pace marker at the expected
//! percent. Also its timeline, the summary and the details popup.

use std::fmt::Write;

use claude_usage_core::reading::HOT_PERCENT;
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};

use super::graph::{self, Dir};
use super::section::{Line, bar_row, label_w, window_gutter};
use super::{Cx, Label, cells, fmt, meter, overlay_box, put};
use crate::claude::ClaudeRow;
use crate::theme::Series;

/// The `used / expected` column (`100% / 100%`) and the reset column (`Thu 12:00 PM (23h 59m)`): the widest
/// the formatters produce (`crate::claude::USED_EXPECTED_W`, `RESET_W`, both tested there).
const USED_W: u16 = crate::claude::USED_EXPECTED_W;
const RESET_W: u16 = crate::claude::RESET_W;
/// The whole value column with the reset, and the smallest bar that is worth giving up the reset for.
const FULL_VAL_W: u16 = USED_W + 2 + RESET_W;
const RESET_BAR_MIN: u16 = 8;

/// Bar rows: one per readout line; the first (5 Hour) is the headline.
pub fn bar_rows(data: &crate::app::Data) -> (u16, u16) {
    let shown = data.claude.rows.iter().filter(|r| !r.hidden).count();
    ((shown as u16).max(1), 1)
}

/// Over pace (used above expected) or at the hot threshold.
fn is_hot(r: &ClaudeRow) -> bool {
    r.used >= HOT_PERCENT as f32 || r.expected.is_some_and(|e| r.used > e)
}

/// The 5 Hour percent at the right end of the title, in orange or danger red.
pub fn headline(cx: &mut Cx) -> Style {
    let t = &mut cx.sc.text2;
    t.clear();
    match cx.data.claude.rows.first() {
        Some(r) => {
            let _ = write!(t, "{:.0}%", r.used);
            if is_hot(r) {
                cx.theme.danger_style()
            } else {
                cx.theme.claude_style()
            }
        }
        None => {
            t.push_str("no reading");
            cx.theme.muted_style()
        }
    }
}

/// The one-line fallback when even the frame does not fit.
pub fn meter(cx: &mut Cx, buf: &mut Buffer, r: Rect) {
    let theme = cx.theme;
    let t = &mut cx.sc.text;
    t.clear();
    let (pct, hot) = match cx.data.claude.rows.first() {
        Some(row) => {
            t.push_str(&row.text);
            (row.used, is_hot(row))
        }
        None => {
            t.push_str("no reading");
            (0.0, false)
        }
    };
    let color = if hot { theme.danger } else { theme.claude };
    meter::line(buf, theme, r, "Claude", pct, &|_| color, t);
}

/// The rows: label, bar on the normal track, the pace marker `│` at the expected percent, then
/// `used / expected` and the reset (`2:00 PM (4h 7m)`) on 5 Hour and Weekly.
pub fn bars(cx: &mut Cx, buf: &mut Buffer, area: Rect) {
    let (theme, data) = (cx.theme, cx.data);
    let rows = &data.claude.rows;
    if rows.is_empty() {
        let msg = if data.claude.merged.is_none() {
            "no reading — feed and usage server unavailable"
        } else {
            "no usage yet"
        };
        put(buf, area.x, area.y, msg, theme.muted_style(), area.width);
        return;
    }
    // The label column spans every row, hidden model weeks included, so it does not move when one appears.
    // One column layout for every row, from the formatters' widest output (never from the current rows):
    // the label column, `used / expected`, then the reset column. When the width cannot hold the reset
    // column with a bar of at least RESET_BAR_MIN, the reset column goes for every row (by width only).
    let lw = label_w(
        rows.iter()
            .map(|r| cells(&r.label))
            .max()
            .unwrap_or(4)
            .max(6),
    );
    let with_reset = crate::ui::section::bar_width(area.width, lw, FULL_VAL_W) >= RESET_BAR_MIN;
    let val_w = if with_reset { FULL_VAL_W } else { USED_W };
    let sc = &mut *cx.sc;
    for (i, row) in rows
        .iter()
        .filter(|r| !r.hidden)
        .enumerate()
        .take(usize::from(area.height))
    {
        sc.text.clear();
        let _ = write!(sc.text, "{:>w$}", row.text, w = usize::from(USED_W));
        if with_reset {
            let _ = write!(sc.text, "  {:<w$}", row.reset, w = usize::from(RESET_W));
        }
        let color = if is_hot(row) {
            theme.danger
        } else {
            theme.claude
        };
        let rect = Rect::new(area.x, area.y + i as u16, area.width, 1);
        let bar = bar_row(
            buf,
            theme,
            rect,
            &row.label,
            (lw, val_w),
            row.used,
            &|_| color,
            &sc.text,
        );
        if let (Some((x, w)), Some(e)) = (bar, row.expected) {
            let at = x + ((e.clamp(0.0, 100.0) / 100.0 * f32::from(w)) as u16).min(w - 1);
            if let Some(cell) = buf.cell_mut((at, rect.y)) {
                cell.set_char('│')
                    .set_style(theme.plain().add_modifier(Modifier::BOLD));
            }
        }
    }
}

pub fn summary(cx: &mut Cx, buf: &mut Buffer, r: Rect) {
    let (theme, data) = (cx.theme, cx.data);
    let view = &data.claude;
    let sc = &mut *cx.sc;
    let mut line = Line::new(r);
    let Some(m) = &view.merged else {
        // The bars already say so; the summary repeats it only when they are off.
        if !cx.cfg.section.claude.c.bars {
            line.item(
                buf,
                theme,
                "no reading — feed and usage server unavailable",
                theme.muted_style(),
            );
        }
        return;
    };
    if !view.session_clock.is_empty() {
        sc.text.clear();
        let _ = write!(sc.text, "session resets {}", view.session_clock);
        line.item(buf, theme, &sc.text, theme.plain());
    }
    if !view.week_clock.is_empty() {
        sc.text.clear();
        let _ = write!(sc.text, "week resets {}", view.week_clock);
        line.item(buf, theme, &sc.text, theme.plain());
    }
    sc.text.clear();
    let _ = write!(sc.text, "via {}, ", m.source.name());
    age(&mut sc.text, data.now_ms - m.read_at_ms);
    line.item(buf, theme, &sc.text, theme.muted_style());
    if m.stale {
        line.item(buf, theme, &view.stale_text, theme.danger_style());
    }
}

/// Session and week percent over the time vitals has watched (memory only).
pub fn timeline(cx: &mut Cx, buf: &mut Buffer, area: Rect) {
    let g = window_gutter(cx, buf, area, Label::minutes(30).as_str());
    let inner = g;
    let (theme, cfg, data) = (cx.theme, cx.cfg, cx.data);
    let style = cfg.section.claude.c.style;
    let sc = &mut *cx.sc;
    let points = crate::history::points_for_window(
        std::time::Duration::from_secs(30 * 60),
        std::time::Duration::from_millis(cfg.interval_ms),
    );
    let top_h = inner.height.div_ceil(2);
    let _ = Dir::Up;
    let halves = [
        (
            Rect::new(inner.x, inner.y, inner.width, top_h),
            &data.rings.claude_session,
            "session",
            1.0,
        ),
        (
            Rect::new(inner.x, inner.y + top_h, inner.width, inner.height - top_h),
            &data.rings.claude_week,
            "week",
            0.7,
        ),
    ];
    for (area, ring, label, _) in halves {
        if area.height == 0 {
            continue;
        }
        ring.columns(points, graph::data_cols(style, area.width), &mut sc.cols);
        graph::draw(buf, area, &sc.cols, 100.0, Dir::Up, style, &|_, p, _| {
            theme.ramp(Series::Claude, p)
        });
        sc.text.clear();
        let _ = write!(sc.text, "{label} ");
        if let Some(v) = ring.latest().filter(|v| !v.is_nan()) {
            fmt::percent(&mut sc.text, v);
        }
        put(
            buf,
            area.x,
            area.y,
            &sc.text,
            theme.muted_style(),
            area.width,
        );
    }
}

/// Age in words: `just now`, `3m ago`, `2h 5m ago`.
fn age(out: &mut String, ms: i64) {
    let mins = (ms.max(0) / 60_000) as u64;
    match mins {
        0 => out.push_str("just now"),
        1..=59 => {
            let _ = write!(out, "{mins}m ago");
        }
        _ => {
            let _ = write!(out, "{}h {}m ago", mins / 60, mins % 60);
        }
    }
}

/// The details popup (spec §6): the applet's hover text drawn from `claude_usage_core::readout::lines`,
/// then the source and its age. Values are Claude orange, a used percent above its expected percent is
/// danger red (over pace), labels are muted.
pub fn popup(cx: &mut Cx, buf: &mut Buffer, area: Rect) {
    use claude_usage_core::readout::{self, Line};
    let (theme, data) = (cx.theme, cx.data);
    let view = &data.claude;
    let reading = view.merged.as_ref().map(|m| &m.reading);
    let lines = readout::lines(
        reading,
        view.merged.as_ref().map(|m| m.read_at_ms),
        view.merged.as_ref().is_some_and(|m| m.stale),
        data.now_ms,
    );
    let extra = 3 + u16::from(view.server_error.is_some());
    let inner = overlay_box(
        cx,
        buf,
        area,
        50,
        lines.len() as u16 + 2 + extra,
        "Claude usage",
    );
    // Any click closes this one: the whole screen is "outside".
    let sc = &mut *cx.sc;
    let lw = readout::label_width(&lines);
    let mut y = inner.y;
    let bottom = inner.y + inner.height;
    for l in &lines {
        if y >= bottom {
            break;
        }
        match l {
            Line::Blank => {}
            Line::Note(t) => {
                put(buf, inner.x, y, t, theme.muted_style(), inner.width);
            }
            Line::Reset(t) => {
                sc.text.clear();
                let _ = write!(sc.text, "{:<lw$} : ", "Resets");
                let x = put(buf, inner.x, y, &sc.text, theme.muted_style(), inner.width);
                put(
                    buf,
                    inner.x + x,
                    y,
                    t,
                    theme.claude_style(),
                    inner.width.saturating_sub(x),
                );
            }
            Line::Row {
                label,
                used,
                expected,
            } => {
                sc.text.clear();
                let _ = write!(sc.text, "{label:<lw$} : ");
                let mut x = put(buf, inner.x, y, &sc.text, theme.muted_style(), inner.width);
                let num = |t: &str| t.trim_end_matches('%').parse::<f64>().ok();
                let over = matches!((num(used), num(expected)), (Some(u), Some(e)) if u > e);
                let used_style = if over {
                    theme.danger_style()
                } else {
                    theme.claude_style()
                };
                x += put(
                    buf,
                    inner.x + x,
                    y,
                    used,
                    used_style,
                    inner.width.saturating_sub(x),
                );
                x += put(
                    buf,
                    inner.x + x,
                    y,
                    " / ",
                    theme.muted_style(),
                    inner.width.saturating_sub(x),
                );
                put(
                    buf,
                    inner.x + x,
                    y,
                    expected,
                    theme.claude_style(),
                    inner.width.saturating_sub(x),
                );
            }
        }
        y += 1;
    }
    y += 1;
    if let Some(m) = &view.merged {
        sc.text.clear();
        let _ = write!(sc.text, "source: {}, read ", m.source.name());
        age(&mut sc.text, data.now_ms - m.read_at_ms);
        put(buf, inner.x, y, &sc.text, theme.plain(), inner.width);
        y += 1;
    }
    if let Some(e) = &view.server_error {
        sc.text.clear();
        let _ = write!(sc.text, "server: {e}");
        put(buf, inner.x, y, &sc.text, theme.muted_style(), inner.width);
    }
    put(
        buf,
        inner.x,
        inner.y + inner.height.saturating_sub(1),
        "click or Esc to close",
        theme.muted_style(),
        inner.width,
    );
}
