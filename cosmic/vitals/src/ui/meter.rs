// SPDX-License-Identifier: MIT
//! The one-line meter every panel can shrink to: `CPU ▕████░░░░▏ 23% 52°C`.

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::Color;

use super::{bars, cells, put};
use crate::theme::Theme;

/// Draws the label, a bar at `pct` and the `extras` text on the single line `r`.
pub fn line(
    buf: &mut Buffer,
    theme: &Theme,
    r: Rect,
    label: &str,
    pct: f32,
    color: &dyn Fn(f32) -> Color,
    extras: &str,
) {
    if r.width == 0 || r.height == 0 {
        return;
    }
    let mut x = r.x;
    let end = r.x + r.width;
    x += put(buf, x, r.y, label, theme.title_style(), end - x) + 1;
    let extras_w = cells(extras);
    let bar_w = i32::from(end.saturating_sub(x)) - i32::from(extras_w) - 3;
    if bar_w >= 3 {
        let bar_w = (bar_w as u16).min(40);
        put(buf, x, r.y, "▕", theme.muted_style(), 1);
        bars::hbar(buf, x + 1, r.y, bar_w, pct, color, theme.muted_style());
        put(buf, x + 1 + bar_w, r.y, "▏", theme.muted_style(), 1);
        x += bar_w + 3;
    }
    put(buf, x, r.y, extras, theme.plain(), end.saturating_sub(x));
}
