// SPDX-License-Identifier: MIT
//! The header line: section chips, then the clock, the interval, the theme and warnings, with the
//! settings and help buttons pinned at the top right.

use std::fmt::Write;

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};

use super::{Cx, cells, put};
use crate::config::{PanelId, Part, Tri};
use crate::input::{Action, Target};

/// The interval as the chip shows it: `250ms`, `1s`.
pub fn interval_text(out: &mut String, ms: u64) {
    if ms < 1000 {
        let _ = write!(out, "{ms}ms");
    } else {
        let _ = write!(out, "{}s", ms / 1000);
    }
}

/// Width of the four buttons (`[▤ bars] [⣿ graphs] [⚙] [?]`) and the gaps between them.
const BUTTONS: u16 = 27;

pub fn render(cx: &mut Cx, buf: &mut Buffer, r: Rect) {
    let (theme, cfg, data, ui) = (cx.theme, cx.cfg, cx.data, cx.ui);
    if r.width == 0 {
        return;
    }
    let end = r.x + r.width;
    let mut x = r.x;
    let brand = if r.width >= 56 { "vitals " } else { "" };
    x += put(buf, x, r.y, brand, theme.title_style(), end - x);

    // Chips. Full names when they fit next to the essentials on the right, else just the numbers.
    let full_w: u16 = PanelId::ALL.iter().map(|p| 4 + cells(p.name()) + 1).sum();
    let essentials = 2 + 5 + BUTTONS + 3; // interval, clock, buttons, gaps
    let full = x + full_w + essentials <= end;
    for p in PanelId::ALL {
        let hidden = !cfg.enabled(p);
        let mut style = if hidden {
            theme.muted_style()
        } else {
            theme.title_style()
        };
        if ui.focus == p && !hidden {
            style = style.add_modifier(Modifier::UNDERLINED);
        }
        cx.sc.text.clear();
        if full {
            let _ = write!(cx.sc.text, "[{} {}]", p.number(), p.name());
        } else {
            let _ = write!(cx.sc.text, "[{}]", p.number());
        }
        // A chip that does not fit whole is left out (its key still works).
        if x + cells(&cx.sc.text) > end.saturating_sub(BUTTONS + 1) {
            break;
        }
        let w = put(buf, x, r.y, &cx.sc.text, style, end.saturating_sub(x));
        cx.hits.push(Rect::new(x, r.y, w, 1), Target::Chip(p));
        x += w + u16::from(full);
    }

    // Right side, built from the most important item outwards while it fits.
    let sc = &mut *cx.sc;
    // The buttons come first and are never dropped: `[⚙]` then `[?]` in the corner.
    let corner = end.saturating_sub(3);
    put(buf, corner, r.y, "[?]", theme.title_style(), 3);
    cx.hits.push(Rect::new(corner, r.y, 3, 1), Target::Help);
    let gear = corner.saturating_sub(4);
    put(buf, gear, r.y, "[⚙]", theme.title_style(), 3);
    cx.hits.push(Rect::new(gear, r.y, 3, 1), Target::Settings);
    // The global toggles, left of the gear: dim when the part is off in every section.
    let mut left_of = gear;
    for (text, part) in [("[⣿ graphs]", Part::Graph), ("[▤ bars]", Part::Bars)] {
        let w = cells(text);
        left_of = left_of.saturating_sub(w + 1);
        let style = if cfg.tri(Some(part)) == Tri::None {
            theme.muted_style()
        } else {
            theme.title_style()
        };
        put(buf, left_of, r.y, text, style, w);
        cx.hits.push(
            Rect::new(left_of, r.y, w, 1),
            Target::Do(Action::ToggleAll(Some(part))),
        );
    }
    let mut right = left_of.saturating_sub(1);
    let room_left = x + 1;
    let mut place = |buf: &mut Buffer, text: &str, style: Style, target: Option<Target>| -> bool {
        let w = cells(text);
        if w == 0 || right < room_left + w + 1 {
            return false;
        }
        right -= w;
        put(buf, right, r.y, text, style, w);
        if let Some(t) = target {
            cx.hits.push(Rect::new(right, r.y, w, 1), t);
        }
        right = right.saturating_sub(1);
        true
    };
    place(buf, &data.clock, theme.plain(), None);
    sc.text.clear();
    interval_text(&mut sc.text, cfg.interval_ms);
    place(buf, &sc.text, theme.plain(), Some(Target::Interval));
    if ui.paused {
        place(buf, "paused", theme.danger_style(), None);
    }
    if !cfg.mouse {
        place(buf, "mouse off", theme.danger_style(), None);
    }
    place(buf, &cfg.theme, theme.muted_style(), Some(Target::Theme));
    if r.width >= 100 {
        let [l1, l5, l15] = data.sys.cpu.load();
        sc.text.clear();
        let _ = write!(sc.text, "load {l1:.2} {l5:.2} {l15:.2}");
        place(buf, &sc.text, theme.muted_style(), None);
    }
    if let Some(w) = &ui.warn {
        place(buf, w, theme.danger_style(), None);
    }
}
