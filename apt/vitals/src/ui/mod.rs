// SPDX-License-Identifier: MIT
//! The widgets. [`render`] lays the screen out, rebuilds the mouse hit map and draws every panel into a
//! ratatui `Buffer`. Nothing here reads the clock, the environment or the disk, and in steady state
//! nothing allocates: text goes through the reused `Scratch` strings.

// Drawing functions take a buffer, a rectangle, a style and the pieces of one row: many small arguments
// read better here than a struct per call.
#![allow(clippy::too_many_arguments, clippy::type_complexity)]

pub mod bars;
pub mod claude_row;
pub mod fmt;
pub mod graph;
pub mod header;
pub mod help;
pub mod help_text;
pub mod layout;
pub mod meter;
pub mod panel;
pub mod section;
pub mod settings;
pub mod tokens;

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};

use crate::app::{App, Data, Overlay, Scratch, UiState};
use crate::config::{Config, PanelId};
use crate::input::{HitMap, Target};
use crate::theme::Theme;

/// Everything a widget reads, plus the two things it may change: the hit map and the scratch buffers.
pub struct Cx<'a> {
    pub theme: &'a Theme,
    pub cfg: &'a Config,
    pub data: &'a Data,
    pub ui: &'a UiState,
    pub hits: &'a mut HitMap,
    pub sc: &'a mut Scratch,
}

/// Writes `s` at `(x, y)`, at most `max_w` cells wide; returns the cells used.
pub fn put(buf: &mut Buffer, x: u16, y: u16, s: &str, style: Style, max_w: u16) -> u16 {
    if max_w == 0 {
        return 0;
    }
    let (end, _) = buf.set_stringn(x, y, s, usize::from(max_w), style);
    end - x
}

/// Fills a rectangle with one symbol.
pub fn fill(buf: &mut Buffer, r: Rect, ch: char, style: Style) {
    for y in r.y..r.y + r.height {
        for x in r.x..r.x + r.width {
            if let Some(c) = buf.cell_mut((x, y)) {
                c.set_char(ch).set_style(style);
            }
        }
    }
}

/// Cells a string takes (every symbol vitals draws is one cell wide).
pub fn cells(s: &str) -> u16 {
    s.chars().count().min(usize::from(u16::MAX)) as u16
}

/// A square-cornered box.
pub fn frame(buf: &mut Buffer, r: Rect, style: Style) {
    if r.width < 2 || r.height < 2 {
        return;
    }
    let (x1, y1) = (r.x + r.width - 1, r.y + r.height - 1);
    for x in r.x + 1..x1 {
        for y in [r.y, y1] {
            if let Some(c) = buf.cell_mut((x, y)) {
                c.set_char('─').set_style(style);
            }
        }
    }
    for y in r.y + 1..y1 {
        for x in [r.x, x1] {
            if let Some(c) = buf.cell_mut((x, y)) {
                c.set_char('│').set_style(style);
            }
        }
    }
    for (x, y, ch) in [
        (r.x, r.y, '┌'),
        (x1, r.y, '┐'),
        (r.x, y1, '└'),
        (x1, y1, '┘'),
    ] {
        if let Some(c) = buf.cell_mut((x, y)) {
            c.set_char(ch).set_style(style);
        }
    }
}

/// Writes the pieces of a panel title left to right along the top border.
pub struct Tw {
    x: u16,
    y: u16,
    limit: u16,
}

impl Tw {
    pub fn new(x: u16, y: u16, limit: u16) -> Tw {
        Tw { x, y, limit }
    }

    /// Writes a piece; returns the rectangle it took (for hit targets).
    pub fn seg(&mut self, buf: &mut Buffer, s: &str, style: Style) -> Rect {
        let room = self.limit.saturating_sub(self.x);
        let used = put(buf, self.x, self.y, s, style, room);
        let r = Rect::new(self.x, self.y, used, 1);
        self.x += used;
        // A space after the title so it never touches the border; the next piece overwrites it.
        put(
            buf,
            self.x,
            self.y,
            " ",
            Style::new(),
            self.limit.saturating_sub(self.x).min(1),
        );
        r
    }
}

/// A short label such as `2m`, built on the stack.
pub struct Label {
    b: [u8; 8],
    n: usize,
}

impl Label {
    pub fn minutes(m: u32) -> Label {
        let mut l = Label { b: [0; 8], n: 0 };
        let mut digits = [0u8; 4];
        let mut k = 0;
        let mut v = m.min(9999);
        loop {
            digits[k] = b'0' + (v % 10) as u8;
            k += 1;
            v /= 10;
            if v == 0 {
                break;
            }
        }
        for i in (0..k).rev() {
            l.b[l.n] = digits[i];
            l.n += 1;
        }
        l.b[l.n] = b'm';
        l.n += 1;
        l
    }

    pub fn as_str(&self) -> &str {
        std::str::from_utf8(&self.b[..self.n]).unwrap_or("")
    }
}

fn too_small(buf: &mut Buffer, area: Rect, theme: &Theme) {
    let msg = "terminal too small (40×12 min)";
    put(buf, area.x, area.y, msg, theme.danger_style(), area.width);
}

/// The layout for the current config, data and zoom.
pub fn plan(app: &App, area: Rect) -> layout::Layout {
    let (cfg, data) = (&app.cfg, &app.data);
    layout::compute(area, cfg, app.ui.zoom, &|id, w| {
        section::needs(data, cfg, id, w)
    })
}

/// Draws the whole screen into `buf` (the size of `area`) and rebuilds the hit map.
pub fn render(app: &mut App, area: Rect, buf: &mut Buffer) {
    app.hits.clear();
    if area.width < layout::MIN_W || area.height < layout::MIN_H {
        too_small(buf, area, &app.theme);
        return;
    }
    let lay = plan(app, area);
    let mut cx = Cx {
        theme: &app.theme,
        cfg: &app.cfg,
        data: &app.data,
        ui: &app.ui,
        hits: &mut app.hits,
        sc: &mut app.scratch,
    };
    header::render(&mut cx, buf, lay.header);
    draw_sections(&mut cx, buf, &lay);
    popups(&mut cx, buf, area);
}

fn draw_sections(cx: &mut Cx, buf: &mut Buffer, lay: &layout::Layout) {
    for id in PanelId::ALL {
        if let Some(slot) = lay.slot(id) {
            section::render(cx, buf, id, slot);
        }
    }
}

/// An open popup: everything under the header becomes "outside" (a click closes it), then the popup.
fn popups(cx: &mut Cx, buf: &mut Buffer, area: Rect) {
    if cx.ui.overlay == Overlay::None {
        return;
    }
    cx.hits.push(
        Rect::new(area.x, area.y + 1, area.width, area.height - 1),
        Target::Outside,
    );
    match cx.ui.overlay {
        Overlay::None => {}
        Overlay::Help => help::render(cx, buf, area),
        Overlay::Settings => settings::render(cx, buf, area),
        Overlay::Claude => claude_row::popup(cx, buf, area),
    }
}

/// One section alone in `area`, laid out like a single-section screen (for tests).
pub fn render_one(app: &mut App, id: PanelId, area: Rect, buf: &mut Buffer) {
    app.hits.clear();
    let (cfg, data) = (&app.cfg, &app.data);
    let mut solo = cfg.clone();
    solo.row = vec![crate::config::Row {
        sections: vec![id.key().to_owned()],
        widths: vec![],
    }];
    for p in PanelId::ALL {
        solo.common_mut(p).enabled = p == id;
    }
    // The layout reserves a header row; lay out one row taller and shift everything up over it.
    let mut lay = layout::compute(
        Rect::new(area.x, area.y, area.width, area.height + 1),
        &solo,
        None,
        &|i, w| section::needs(data, cfg, i, w),
    );
    for slot in lay.slots.iter_mut().flatten() {
        slot.rect.y -= 1;
    }
    let mut cx = Cx {
        theme: &app.theme,
        cfg: &app.cfg,
        data: &app.data,
        ui: &app.ui,
        hits: &mut app.hits,
        sc: &mut app.scratch,
    };
    draw_sections(&mut cx, buf, &lay);
}

/// Clears a rectangle (an overlay's footprint) to blanks with the default style.
pub fn clear(buf: &mut Buffer, r: Rect) {
    fill(buf, r, ' ', Style::new().remove_modifier(Modifier::all()));
}

/// A centered box of the given size inside `area`, cleared and framed; returns the inside.
pub fn overlay_box(cx: &mut Cx, buf: &mut Buffer, area: Rect, w: u16, h: u16, title: &str) -> Rect {
    let w = w.min(area.width);
    let h = h.min(area.height);
    let r = Rect::new(
        area.x + (area.width - w) / 2,
        area.y + (area.height - h) / 2,
        w,
        h,
    );
    box_at(cx, buf, r, title)
}

/// A box in the top-right corner under the header (the settings popup).
pub fn corner_box(cx: &mut Cx, buf: &mut Buffer, area: Rect, w: u16, h: u16, title: &str) -> Rect {
    let w = w.min(area.width);
    let h = h.min(area.height.saturating_sub(1));
    let r = Rect::new(area.x + area.width - w, area.y + 1, w, h);
    box_at(cx, buf, r, title)
}

/// Clears and frames `r`, titles it; returns the inside, inset by one cell on the sides.
fn box_at(cx: &mut Cx, buf: &mut Buffer, r: Rect, title: &str) -> Rect {
    clear(buf, r);
    frame(buf, r, cx.theme.border_style(true));
    put(
        buf,
        r.x + 2,
        r.y,
        title,
        cx.theme.title_style(),
        r.width.saturating_sub(4),
    );
    Rect::new(
        r.x + 2,
        r.y + 1,
        r.width.saturating_sub(4),
        r.height.saturating_sub(2),
    )
}
