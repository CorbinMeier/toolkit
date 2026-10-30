// SPDX-License-Identifier: MIT
//! The settings popup (spec §4), anchored top right: per section an enable checkbox, the timeline
//! (`braille`), `bars` and `summary` toggles, timeline rows `− n +` and `▲ ▼` to move it; under them the
//! interval, theme, layout and style; then the Network block (`per interface` and one checkbox per
//! listed interface for whether it counts toward `all`). Every control is a hit target that carries its [`Action`], so the
//! mouse and the keyboard cursor (arrows, Space/Enter) press the same thing.

use std::fmt::Write;

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};

use super::{Cx, cells, corner_box, put};
use crate::config::{PanelId, Part, Tri};
use crate::input::{Action, Target};

/// Section rows, the `All` row, the last row (interval, theme, layout, style) and the Network row.
pub const ROWS: usize = 9;
/// The Network row: control 0 is `per interface`, control `k` the `k`-th listed interface.
const NET_ROW: usize = ALL_ROW + 2;
/// The `All` row.
const ALL_ROW: usize = PanelId::ALL.len();
/// Its controls: every section on, every timeline, every bars, every summary.
const ALL_CTLS: usize = 4;
/// Controls per section row: enable, timeline, bars, summary, rows −, rows +, up, down.
const SECTION_CTLS: usize = 8;
/// Controls in the last row: interval −, interval +, theme, layout, style.
const LAST_CTLS: usize = 5;
/// Below this screen width the summary and rows controls move to a second line.
const WIDE: u16 = 70;

/// Controls in `row`; the Network row has one more than there are `ifaces` listed.
pub fn cols(row: usize, ifaces: usize) -> usize {
    match row {
        r if r < ALL_ROW => SECTION_CTLS,
        ALL_ROW => ALL_CTLS,
        NET_ROW => 1 + ifaces,
        _ => LAST_CTLS,
    }
}

/// What control `col` of `row` does.
pub fn control(row: usize, col: usize) -> Option<Action> {
    if let Some(&p) = PanelId::ALL.get(row) {
        return Some(match col {
            0 => Action::TogglePanel(p),
            1 => Action::TogglePart(p, Part::Graph),
            2 => Action::TogglePart(p, Part::Bars),
            3 => Action::TogglePart(p, Part::Summary),
            4 => Action::GraphRows(p, -1),
            5 => Action::GraphRows(p, 1),
            6 => Action::MoveSection(p, -1),
            7 => Action::MoveSection(p, 1),
            _ => return None,
        });
    }
    if row == ALL_ROW {
        return Some(match col {
            0 => Action::ToggleAll(None),
            1 => Action::ToggleAll(Some(Part::Graph)),
            2 => Action::ToggleAll(Some(Part::Bars)),
            3 => Action::ToggleAll(Some(Part::Summary)),
            _ => return None,
        });
    }
    if row == NET_ROW {
        return Some(match col {
            0 => Action::TogglePerIface,
            k => Action::ToggleIface(k - 1),
        });
    }
    Some(match col {
        0 => Action::Interval(-1),
        1 => Action::Interval(1),
        2 => Action::NextTheme,
        3 => Action::NextLayout,
        4 => Action::AllStyles,
        _ => return None,
    })
}

/// Where the next control goes, and the rows that are on screen.
struct Pen {
    x: u16,
    /// Line in the popup's own coordinates (before scrolling).
    line: u16,
    left: u16,
    scroll: u16,
    rows: u16,
    cursor: Option<(usize, usize)>,
}

impl Pen {
    fn newline(&mut self) {
        self.line += 1;
        self.x = 0;
    }
}

/// Draws one control (text from `text`), records its hit target, returns nothing; advances the pen.
fn ctl(
    cx: &mut Cx,
    buf: &mut Buffer,
    inner: Rect,
    pen: &mut Pen,
    at: (usize, usize),
    text: &str,
    style: Style,
    gap: u16,
) {
    let w = cells(text);
    let visible = pen.line >= pen.scroll && pen.line < pen.scroll + pen.rows;
    if visible
        && pen.x < inner.width
        && let Some(action) = control(at.0, at.1)
    {
        let y = inner.y + pen.line - pen.scroll;
        let style = if pen.cursor == Some(at) {
            style.add_modifier(Modifier::REVERSED)
        } else {
            style
        };
        let shown = w.min(inner.width - pen.x);
        put(buf, inner.x + pen.x, y, text, style, shown);
        cx.hits
            .push(Rect::new(inner.x + pen.x, y, shown, 1), Target::Do(action));
    }
    pen.x += w + gap;
}

/// Plain text between controls.
fn label(cx: &Cx, buf: &mut Buffer, inner: Rect, pen: &mut Pen, text: &str, gap: u16) {
    let w = cells(text);
    if pen.line >= pen.scroll && pen.line < pen.scroll + pen.rows && pen.x < inner.width {
        put(
            buf,
            inner.x + pen.x,
            inner.y + pen.line - pen.scroll,
            text,
            cx.theme.muted_style(),
            w.min(inner.width - pen.x),
        );
    }
    pen.x += w + gap;
}

fn box_text(text: &mut String, on: bool, rest: &str, pad: usize) {
    text.clear();
    text.push_str(if on { "[x] " } else { "[ ] " });
    text.push_str(rest);
    for _ in rest.chars().count()..pad {
        text.push(' ');
    }
}

/// Lines the popup needs, and the line of each section row's first line, for the layout at `wide`.
fn total_lines(wide: bool, net_lines: u16) -> u16 {
    let sections = (PanelId::ALL.len() as u16 + 1) * if wide { 1 } else { 2 };
    let last = if wide { 1 } else { 2 };
    sections + 1 + last + net_lines + 1
}

/// The Network block: width of one interface entry (`[x] name` plus a gap), entries per line, and the
/// lines the block takes (the `per interface` line plus the entries).
fn net_block(cx: &Cx, inner_w: u16) -> (u16, u16, u16) {
    let net = &cx.data.sys.net;
    let longest = net.interfaces().map(|i| cells(&i.name)).max().unwrap_or(0);
    let entry_w = 4 + longest + 2;
    let per_line = (inner_w / entry_w).max(1);
    let n = net.interfaces().count() as u16;
    (entry_w, per_line, 1 + n.div_ceil(per_line))
}

pub fn render(cx: &mut Cx, buf: &mut Buffer, area: Rect) {
    let wide = area.width >= WIDE;
    let width = if wide { 68 } else { 44 };
    let (entry_w, per_line, net_lines) = net_block(cx, width.min(area.width).saturating_sub(2));
    let total = total_lines(wide, net_lines);
    let outer = corner_box(cx, buf, area, width, total + 2, "Settings");
    cx.hits.push(
        Rect::new(
            area.x + area.width - width.min(area.width),
            area.y + 1,
            width.min(area.width),
            (total + 2).min(area.height - 1),
        ),
        Target::Popup,
    );
    let mut pen = Pen {
        x: 0,
        line: 0,
        left: 0,
        scroll: 0,
        rows: outer.height,
        cursor: cx.ui.settings_cursor,
    };
    // Scroll so the cursor's line is on screen when the popup is taller than the terminal.
    if let Some((r, _)) = pen.cursor {
        let per = if wide { 1 } else { 2 };
        let last = (ALL_ROW as u16 + 1) * per + 1;
        let line = match r {
            r if r <= ALL_ROW => r as u16 * per,
            NET_ROW => {
                let at = last + if wide { 1 } else { 2 };
                match pen.cursor {
                    Some((_, k)) if k > 0 => at + 1 + (k as u16 - 1) / per_line,
                    _ => at,
                }
            }
            _ => last,
        };
        pen.scroll = (line + per).saturating_sub(pen.rows);
    }
    let _ = pen.left;
    let (theme, cfg) = (cx.theme, cx.cfg);
    let g = if wide { 2 } else { 1 };
    for (row, &p) in PanelId::ALL.iter().enumerate() {
        let c = cfg.common(p);
        let on = theme.title_style();
        let off = theme.muted_style();
        let dim = |flag: bool| if c.enabled && flag { on } else { off };
        let mut t = std::mem::take(&mut cx.sc.text);
        box_text(&mut t, c.enabled, p.title(), 8);
        ctl(
            cx,
            buf,
            outer,
            &mut pen,
            (row, 0),
            &t,
            if c.enabled { on } else { off },
            g - 1,
        );
        box_text(&mut t, c.graph, c.style.name(), 7);
        ctl(cx, buf, outer, &mut pen, (row, 1), &t, dim(c.graph), g);
        box_text(&mut t, c.bars, "bars", 0);
        ctl(cx, buf, outer, &mut pen, (row, 2), &t, dim(c.bars), g);
        if !wide {
            // The arrows stay on the first line; summary and rows go underneath.
            ctl(cx, buf, outer, &mut pen, (row, 6), "▲", theme.plain(), 1);
            ctl(cx, buf, outer, &mut pen, (row, 7), "▼", theme.plain(), 0);
            pen.newline();
            pen.x = 4;
        }
        box_text(&mut t, c.summary, "summary", 0);
        ctl(cx, buf, outer, &mut pen, (row, 3), &t, dim(c.summary), g);
        label(cx, buf, outer, &mut pen, "rows", 1);
        ctl(cx, buf, outer, &mut pen, (row, 4), "−", theme.plain(), 1);
        t.clear();
        let _ = write!(t, "{}", c.graph_rows);
        label(cx, buf, outer, &mut pen, &t, 1);
        ctl(cx, buf, outer, &mut pen, (row, 5), "+", theme.plain(), g);
        if wide {
            ctl(cx, buf, outer, &mut pen, (row, 6), "▲", theme.plain(), 1);
            ctl(cx, buf, outer, &mut pen, (row, 7), "▼", theme.plain(), 0);
        }
        cx.sc.text = t;
        pen.newline();
    }
    // The All row: each box is [x] on in every section, [ ] off in every one, [-] mixed.
    let mut t = std::mem::take(&mut cx.sc.text);
    let tri = |part: Option<Part>| match cfg.tri(part) {
        Tri::All => "[x] ",
        Tri::None => "[ ] ",
        Tri::Mixed => "[-] ",
    };
    for (col, (part, name, pad)) in [
        (None, "All", 8),
        (Some(Part::Graph), cfg.style(PanelId::Cpu).name(), 7),
        (Some(Part::Bars), "bars", 0),
        (Some(Part::Summary), "summary", 0),
    ]
    .into_iter()
    .enumerate()
    {
        if col == 3 && !wide {
            pen.newline();
            pen.x = 4;
        }
        t.clear();
        t.push_str(tri(part));
        t.push_str(name);
        for _ in name.len()..pad {
            t.push(' ');
        }
        let style = if cfg.tri(part) == Tri::None {
            theme.muted_style()
        } else {
            theme.title_style()
        };
        ctl(
            cx,
            buf,
            outer,
            &mut pen,
            (ALL_ROW, col),
            &t,
            style,
            if col == 0 { g - 1 } else { g },
        );
    }
    cx.sc.text = t;
    pen.newline();
    pen.newline();
    // interval, theme, layout, style
    let row = ALL_ROW + 1;
    let mut t = std::mem::take(&mut cx.sc.text);
    label(cx, buf, outer, &mut pen, "interval", 1);
    ctl(cx, buf, outer, &mut pen, (row, 0), "−", theme.plain(), 1);
    t.clear();
    super::header::interval_text(&mut t, cfg.interval_ms);
    label(cx, buf, outer, &mut pen, &t, 1);
    ctl(cx, buf, outer, &mut pen, (row, 1), "+", theme.plain(), 2);
    let style_name = cfg.style(PanelId::Cpu).name();
    let mut chip = |cx: &mut Cx, pen: &mut Pen, name: &str, value: &str, col: usize| {
        label(cx, buf, outer, pen, name, 1);
        let mut s = std::mem::take(&mut cx.sc.text2);
        s.clear();
        s.push_str(value);
        s.push_str(" ▸");
        ctl(cx, buf, outer, pen, (row, col), &s, theme.title_style(), 2);
        cx.sc.text2 = s;
    };
    if !wide {
        chip(cx, &mut pen, "style", style_name, 4);
        pen.newline();
        chip(cx, &mut pen, "theme", &cfg.theme, 2);
        chip(cx, &mut pen, "layout", cfg.layout.name(), 3);
    } else {
        chip(cx, &mut pen, "theme", &cfg.theme, 2);
        chip(cx, &mut pen, "layout", cfg.layout.name(), 3);
        chip(cx, &mut pen, "style", style_name, 4);
    }
    cx.sc.text = t;
    pen.newline();
    // Network: `per interface`, then a checkbox per listed interface (counts toward `all`).
    label(cx, buf, outer, &mut pen, "Network", 1);
    let mut t = std::mem::take(&mut cx.sc.text);
    box_text(
        &mut t,
        cfg.section.network.per_interface,
        "per interface",
        0,
    );
    ctl(
        cx,
        buf,
        outer,
        &mut pen,
        (NET_ROW, 0),
        &t,
        theme.title_style(),
        2,
    );
    label(cx, buf, outer, &mut pen, "counted in all:", 0);
    pen.newline();
    for (k, f) in cx.data.sys.net.interfaces().enumerate() {
        if k > 0 && (k as u16).is_multiple_of(per_line) {
            pen.newline();
        }
        box_text(&mut t, f.included(), &f.name, 0);
        let style = if f.included() {
            theme.title_style()
        } else {
            theme.muted_style()
        };
        let gap = entry_w.saturating_sub(cells(&t));
        ctl(cx, buf, outer, &mut pen, (NET_ROW, k + 1), &t, style, gap);
    }
    cx.sc.text = t;
    pen.newline();
    label(
        cx,
        buf,
        outer,
        &mut pen,
        "arrows move · Space presses · Esc closes",
        0,
    );
}
