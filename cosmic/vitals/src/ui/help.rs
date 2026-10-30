// SPDX-License-Identifier: MIT
//! The help popup (spec §4): one tab per section explaining every readout, and a controls tab. Scrollable
//! with the wheel, `↑`/`↓` and `PgUp`/`PgDn`; tabs by click or `←`/`→`. The text is in [`super::help_text`].

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::Modifier;

use super::help_text::{CONTROLS, TABS, TOPICS, tab_title};
use super::{Cx, cells, overlay_box, put};
use crate::input::{Action, Target};

/// How a line of the help text is drawn.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    /// A readout's name.
    Head,
    /// Its explanation, indented.
    Body,
    /// Source, unit and scale, indented and muted.
    Muted,
    /// A section heading (`Bars`, `Summary`).
    Part,
    /// A key and its meaning, side by side.
    Key,
}

const KEY_W: usize = 26;

/// Calls `emit(text)` for each line of `text` wrapped to `width` cells at word boundaries.
fn wrap(text: &str, width: usize, mut emit: impl FnMut(&str)) {
    let (mut start, mut end, mut cols, mut pos) = (0, 0, 0, 0);
    let mut started = false;
    for word in text.split(' ') {
        let w = word.chars().count();
        let (ws, we) = (pos, pos + word.len());
        pos = we + 1;
        if !started {
            (start, end, cols, started) = (ws, we, w, true);
        } else if cols + 1 + w <= width {
            end = we;
            cols += 1 + w;
        } else {
            emit(&text[start..end]);
            (start, end, cols) = (ws, we, w);
        }
    }
    if started {
        emit(&text[start..end]);
    }
}

/// Walks the lines of a tab at `width`: `f(kind, key, text)`.
fn walk(tab: usize, width: usize, f: &mut dyn FnMut(Kind, &str, &str)) {
    let body = width.saturating_sub(2).max(10);
    if let Some(t) = TOPICS.get(tab) {
        f(Kind::Part, "", "Timeline");
        wrap(t.timeline, body, |l| f(Kind::Body, "", l));
        for (title, items) in [("Bars", t.bars), ("Summary line", t.summary)] {
            f(Kind::Body, "", "");
            f(Kind::Part, "", title);
            for it in items {
                f(Kind::Head, "", it.name);
                wrap(it.what, body, |l| f(Kind::Body, "", l));
                for (key, text) in [
                    ("source", it.source),
                    ("unit", it.unit),
                    ("scale", it.scale),
                ] {
                    let mut first = true;
                    wrap(text, body.saturating_sub(8), |l| {
                        f(Kind::Muted, if first { key } else { "" }, l);
                        first = false;
                    });
                }
            }
        }
    } else {
        for (keys, what) in CONTROLS {
            let mut first = true;
            wrap(what, width.saturating_sub(KEY_W).max(10), |l| {
                f(Kind::Key, if first { keys } else { "" }, l);
                first = false;
            });
        }
    }
}

pub fn render(cx: &mut Cx, buf: &mut Buffer, area: Rect) {
    let theme = cx.theme;
    let w = area.width.min(92);
    let h = area.height.saturating_sub(2).clamp(8, 40);
    let inner = overlay_box(cx, buf, area, w, h, "vitals help");
    cx.hits.push(
        Rect::new(
            area.x + (area.width - w.min(area.width)) / 2,
            area.y + (area.height - h.min(area.height)) / 2,
            w.min(area.width),
            h.min(area.height),
        ),
        Target::Popup,
    );

    // Tabs, wrapping onto a second line when they do not fit.
    let (mut x, mut y) = (0u16, 0u16);
    for i in 0..TABS {
        let title = tab_title(i);
        let tw = cells(title) + 2;
        if x + tw > inner.width && x > 0 {
            x = 0;
            y += 1;
        }
        let r = Rect::new(
            inner.x + x,
            inner.y + y,
            tw.min(inner.width - x.min(inner.width)),
            1,
        );
        let style = if i == cx.ui.help_tab {
            theme.title_style().add_modifier(Modifier::REVERSED)
        } else {
            theme.muted_style()
        };
        put(buf, r.x, r.y, " ", style, 1);
        put(buf, r.x + 1, r.y, title, style, r.width.saturating_sub(2));
        put(buf, r.x + 1 + cells(title), r.y, " ", style, 1);
        cx.hits.push(r, Target::Do(Action::HelpTab(i)));
        x += tw + 1;
    }
    let top = y + 2;
    let text = Rect::new(
        inner.x,
        inner.y + top,
        inner.width,
        inner.height.saturating_sub(top + 1),
    );
    cx.hits.push(text, Target::HelpText);
    let width = usize::from(text.width);

    let mut total = 0usize;
    walk(cx.ui.help_tab, width, &mut |_, _, _| total += 1);
    let max = total.saturating_sub(usize::from(text.height)) as u16;
    cx.ui.help_max.set(max);
    let scroll = usize::from(cx.ui.help_scroll.min(max));
    let mut idx = 0usize;
    walk(cx.ui.help_tab, width, &mut |kind, key, line| {
        let at = idx;
        idx += 1;
        if at < scroll || at >= scroll + usize::from(text.height) {
            return;
        }
        let y = text.y + (at - scroll) as u16;
        match kind {
            Kind::Head => {
                put(buf, text.x, y, line, theme.title_style(), text.width);
            }
            Kind::Part => {
                put(
                    buf,
                    text.x,
                    y,
                    line,
                    theme.claude_style().add_modifier(Modifier::BOLD),
                    text.width,
                );
            }
            Kind::Body => {
                put(
                    buf,
                    text.x + 2.min(text.width),
                    y,
                    line,
                    theme.plain(),
                    text.width.saturating_sub(2),
                );
            }
            Kind::Muted => {
                put(buf, text.x + 2, y, key, theme.muted_style(), 7);
                put(
                    buf,
                    text.x + 10,
                    y,
                    line,
                    theme.muted_style(),
                    text.width.saturating_sub(10),
                );
            }
            Kind::Key => {
                put(buf, text.x, y, key, theme.title_style(), KEY_W as u16 - 1);
                put(
                    buf,
                    text.x + KEY_W as u16,
                    y,
                    line,
                    theme.plain(),
                    text.width.saturating_sub(KEY_W as u16),
                );
            }
        }
    });
    // The body text of a topic's first paragraph has no indent: undo the shift for the intro lines.
    let hint_y = inner.y + inner.height.saturating_sub(1);
    put(
        buf,
        inner.x,
        hint_y,
        "←→ tab · ↑↓ PgUp PgDn scroll · Esc closes",
        theme.muted_style(),
        inner.width,
    );
    if max > 0 {
        use std::fmt::Write;
        let sc = &mut *cx.sc;
        sc.text.clear();
        let _ = write!(sc.text, "{}/{}", scroll + 1, usize::from(max) + 1);
        let w = cells(&sc.text);
        put(
            buf,
            inner.x + inner.width - w,
            hint_y,
            &sc.text,
            theme.muted_style(),
            w,
        );
    }
}
