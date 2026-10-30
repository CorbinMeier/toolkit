// SPDX-License-Identifier: MIT
//! What every section shares (spec §4): the frame with its title, the three stacked parts (timeline, bars,
//! summary), and the pieces they are drawn from: bar rows, mirrored timelines, the summary line.

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Color, Style};

use super::graph::{self, Dir};
use super::layout::{Needs, Plan, Slot};
use super::{Cx, bars, cells, claude_row, frame, panel, put, tokens};
use crate::app::Data;
use crate::config::{Config, GraphStyle, PanelId};
use crate::history::Ring;
use crate::input::Target;
use crate::theme::{Series, Theme};

/// What `id` would like in a box `inner_w` cells wide inside.
pub fn needs(data: &Data, cfg: &Config, id: PanelId, inner_w: u16) -> Needs {
    let c = cfg.common(id);
    let (full, head) = match id {
        PanelId::Cpu => panel::cpu_bar_rows(data, cfg, inner_w),
        PanelId::Mem => panel::mem_bar_rows(data, cfg),
        PanelId::Disk => panel::disk_bar_rows(data),
        PanelId::Net => panel::net_bar_rows(data, cfg, inner_w),
        PanelId::Tokens => tokens::bar_rows(data),
        PanelId::Claude => claude_row::bar_rows(data),
    };
    let (full, head) = if !c.bars {
        (0, 0)
    } else if c.headline_only {
        (head, head)
    } else {
        (full, head)
    };
    Needs {
        tl: if c.graph { c.graph_rows } else { 0 },
        bars_full: full,
        bars_head: head,
        summary: c.summary,
    }
}

/// Draws one section in its slot.
pub fn render(cx: &mut Cx, buf: &mut Buffer, id: PanelId, slot: Slot) {
    let Slot { rect, plan } = slot;
    if plan.meter || rect.height < 3 || rect.width < 8 {
        cx.hits.push(rect, Target::Title(id));
        meter_line(cx, buf, id, Rect::new(rect.x, rect.y, rect.width, 1));
        return;
    }
    let focused = cx.ui.focus == id;
    frame(buf, rect, cx.theme.border_style(focused));
    let inner = Rect::new(rect.x + 1, rect.y + 1, rect.width - 2, rect.height - 2);
    cx.hits.push(inner, Target::Body(id));
    cx.hits
        .push(Rect::new(rect.x, rect.y, rect.width, 1), Target::Title(id));

    // The title: name, the section's own chips, and the headline reading at the right end.
    let style = headline(cx, id);
    let right = cells(&cx.sc.text2);
    let mut limit = rect.x + rect.width - 1;
    if right > 0 && right + 6 < rect.width {
        let x = rect.x + rect.width - right - 2;
        let text = std::mem::take(&mut cx.sc.text2);
        put(buf, x, rect.y, &text, style, right);
        cx.sc.text2 = text;
        limit = x.saturating_sub(1);
    }
    put(buf, rect.x + 1, rect.y, " ", Style::new(), 1);
    let mut tw = super::Tw::new(rect.x + 2, rect.y, limit);
    tw.seg(buf, id.title(), cx.theme.title_style());
    if id == PanelId::Tokens {
        tokens::title(cx, buf, &mut tw);
    }

    // The parts, top to bottom.
    let (tl, bars_r, sum) = parts(inner, plan);
    if tl.height > 0 {
        cx.hits.push(tl, Target::Timeline(id));
        match id {
            PanelId::Cpu => panel::cpu_timeline(cx, buf, tl),
            PanelId::Mem => panel::mem_timeline(cx, buf, tl),
            PanelId::Disk => panel::disk_timeline(cx, buf, tl),
            PanelId::Net => panel::net_timeline(cx, buf, tl),
            PanelId::Tokens => tokens::timeline(cx, buf, tl),
            PanelId::Claude => claude_row::timeline(cx, buf, tl),
        }
    }
    if bars_r.height > 0 {
        cx.hits.push(bars_r, Target::Bars(id));
        match id {
            PanelId::Cpu => panel::cpu_bars(cx, buf, bars_r),
            PanelId::Mem => panel::mem_bars(cx, buf, bars_r),
            PanelId::Disk => panel::disk_bars(cx, buf, bars_r),
            PanelId::Net => panel::net_bars(cx, buf, bars_r),
            PanelId::Tokens => tokens::bars(cx, buf, bars_r),
            PanelId::Claude => claude_row::bars(cx, buf, bars_r),
        }
    }
    if sum.height > 0 {
        match id {
            PanelId::Cpu => panel::cpu_summary(cx, buf, sum),
            PanelId::Mem => panel::mem_summary(cx, buf, sum),
            PanelId::Disk => panel::disk_summary(cx, buf, sum),
            PanelId::Net => panel::net_summary(cx, buf, sum),
            PanelId::Tokens => tokens::summary(cx, buf, sum),
            PanelId::Claude => claude_row::summary(cx, buf, sum),
        }
    }
}

/// The three rectangles of a plan inside the box: timeline and bars stacked from the top, the summary
/// on the last line.
fn parts(inner: Rect, plan: Plan) -> (Rect, Rect, Rect) {
    let mut y = inner.y;
    let end = inner.y + inner.height;
    let mut take = |rows: u16| {
        let h = rows.min(end - y);
        let r = Rect::new(inner.x, y, inner.width, h);
        y += h;
        r
    };
    let tl = take(plan.tl);
    take(u16::from(plan.gap));
    let bars = take(plan.bars);
    // Spare rows (a section that cannot use them) sit between the bars and the summary at the bottom.
    let sum = if plan.summary && y < end {
        Rect::new(inner.x, end - 1, inner.width, 1)
    } else {
        Rect::new(inner.x, end, inner.width, 0)
    };
    (tl, bars, sum)
}

/// The reading at the right end of the title, written to `sc.text2`; returns its style.
fn headline(cx: &mut Cx, id: PanelId) -> Style {
    match id {
        PanelId::Cpu => panel::cpu_headline(cx),
        PanelId::Mem => panel::mem_headline(cx),
        PanelId::Disk => panel::disk_headline(cx),
        PanelId::Net => panel::net_headline(cx),
        PanelId::Tokens => tokens::headline(cx),
        PanelId::Claude => claude_row::headline(cx),
    }
}

fn meter_line(cx: &mut Cx, buf: &mut Buffer, id: PanelId, r: Rect) {
    match id {
        PanelId::Cpu => panel::cpu_meter(cx, buf, r),
        PanelId::Mem => panel::mem_meter(cx, buf, r),
        PanelId::Disk => panel::disk_meter(cx, buf, r),
        PanelId::Net => panel::net_meter(cx, buf, r),
        PanelId::Tokens => tokens::meter(cx, buf, r),
        PanelId::Claude => claude_row::meter(cx, buf, r),
    }
}

// ------------------------------------------------------------------ pieces

/// One row of bars: muted label, bar, value. `(label_w, val_w)` is the section's one column layout (spec
/// §4, bars): the label column, the bar start and the value column are the same on every row of a section,
/// so the bar width depends on the row width only, never on the value; the value is right-aligned in its
/// column. Returns the bar's `(x, width)`, or `None` when there is no room for a bar (then only the value
/// is written).
pub fn bar_row(
    buf: &mut Buffer,
    theme: &Theme,
    r: Rect,
    label: &str,
    (label_w, val_w): (u16, u16),
    pct: f32,
    color: &dyn Fn(f32) -> Color,
    text: &str,
) -> Option<(u16, u16)> {
    let end = r.x + r.width;
    put(
        buf,
        r.x,
        r.y,
        label,
        theme.muted_style(),
        label_w.min(r.width),
    );
    let bar_x = r.x + label_w;
    let bar_w = bar_width(r.width, label_w, val_w);
    if bar_w >= BAR_MIN {
        bars::hbar(buf, bar_x, r.y, bar_w, pct, color, theme.muted_style());
        let vx = bar_x + bar_w + 1;
        let tw = cells(text).min(val_w);
        put(buf, vx + val_w - tw, r.y, text, theme.plain(), tw);
        Some((bar_x, bar_w))
    } else {
        put(
            buf,
            bar_x,
            r.y,
            text,
            theme.plain(),
            end.saturating_sub(bar_x),
        );
        None
    }
}

/// The smallest bar worth drawing.
pub const BAR_MIN: u16 = 3;

/// Bar width of a row `w` cells wide: what the label column, the value column and the space before it
/// leave (0 when nothing does). A function of the layout and the width only, never of the data.
pub fn bar_width(w: u16, label_w: u16, val_w: u16) -> u16 {
    w.saturating_sub(label_w + val_w + 1)
}

/// Reserves the right end of a timeline for its window label (`2m`) and draws the label. Returns the
/// rectangle left for the graph.
pub fn window_gutter(cx: &Cx, buf: &mut Buffer, area: Rect, label: &str) -> Rect {
    let w = cells(label);
    if area.width < w + 12 {
        return area;
    }
    put(
        buf,
        area.x + area.width - w,
        area.y,
        label,
        cx.theme.muted_style(),
        w,
    );
    Rect::new(area.x, area.y, area.width - w - 1, area.height)
}

/// A ring collapsed to the area's columns and drawn.
pub fn draw_ring(
    buf: &mut Buffer,
    cols: &mut Vec<f32>,
    ring: &Ring,
    points: usize,
    area: Rect,
    max: f32,
    dir: Dir,
    style: GraphStyle,
    color: graph::ColorFn,
) {
    ring.columns(points, graph::data_cols(style, area.width), cols);
    graph::draw(buf, area, cols, max, dir, style, color);
}

/// One half of a mirrored timeline.
pub struct Half<'a> {
    pub label: &'static str,
    pub ring: &'a Ring,
    pub max: f32,
    pub series: Series,
}

/// Two series in one timeline: the first grows up in the top half, the second down in the bottom half.
/// At one row they show their sum.
pub fn mirrored(
    cx: &mut Cx,
    buf: &mut Buffer,
    area: Rect,
    style: GraphStyle,
    halves: [Half; 2],
    points: usize,
) {
    let theme = cx.theme;
    let sc = &mut *cx.sc;
    let [up, down] = halves;
    if area.height == 1 {
        let n = graph::data_cols(style, area.width);
        up.ring.columns(points, n, &mut sc.cols);
        down.ring.columns(points, n, &mut sc.cols2);
        // The sum, scaled to the larger of the two ceilings so a lone peak still fits.
        let scale = up.max.max(down.max);
        for (a, b) in sc.cols.iter_mut().zip(&sc.cols2) {
            *a = match (a.is_nan(), b.is_nan()) {
                (true, true) => f32::NAN,
                (true, false) => *b,
                (false, true) => *a,
                _ => *a + *b,
            };
        }
        let max = sc
            .cols
            .iter()
            .copied()
            .filter(|v| !v.is_nan())
            .fold(scale, f32::max);
        let series = up.series;
        graph::draw(buf, area, &sc.cols, max, Dir::Up, style, &|_, p, _| {
            theme.ramp(series, p)
        });
        return;
    }
    let up_h = area.height.div_ceil(2);
    let areas = [
        Rect::new(area.x, area.y, area.width, up_h),
        Rect::new(area.x, area.y + up_h, area.width, area.height - up_h),
    ];
    for (half, a, dir) in [(&up, areas[0], Dir::Up), (&down, areas[1], Dir::Down)] {
        if a.height == 0 {
            continue;
        }
        let series = half.series;
        draw_ring(
            buf,
            &mut sc.cols,
            half.ring,
            points,
            a,
            half.max,
            dir,
            style,
            &|_, p, _| theme.ramp(series, p),
        );
        if a.width >= 24 && a.height >= 2 {
            use std::fmt::Write;
            sc.text.clear();
            let _ = write!(sc.text, "{} ≤", half.label);
            super::fmt::rate(&mut sc.text, f64::from(half.max));
            let y = if dir == Dir::Up {
                a.y
            } else {
                a.y + a.height - 1
            };
            put(buf, a.x, y, &sc.text, theme.muted_style(), a.width);
        }
    }
}

/// The summary line: items separated by ` · `, each written whole or not at all.
pub struct Line {
    x: u16,
    y: u16,
    end: u16,
    first: bool,
}

impl Line {
    pub fn new(r: Rect) -> Line {
        Line {
            x: r.x,
            y: r.y,
            end: r.x + r.width,
            first: true,
        }
    }

    /// Writes an item if it fits; false (and nothing written) if not.
    pub fn item(&mut self, buf: &mut Buffer, theme: &Theme, text: &str, style: Style) -> bool {
        let w = cells(text);
        let sep = if self.first { 0 } else { 3 };
        if w == 0 || self.x + sep + w > self.end {
            return false;
        }
        if !self.first {
            put(buf, self.x, self.y, " · ", theme.muted_style(), 3);
            self.x += 3;
        }
        put(buf, self.x, self.y, text, style, w);
        self.x += w;
        self.first = false;
        true
    }
}

/// A section's label column for its bars: the longest label plus a space, within 6 and 14.
pub fn label_w(longest: u16) -> u16 {
    (longest + 1).clamp(6, 14)
}
