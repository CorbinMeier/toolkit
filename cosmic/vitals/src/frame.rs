// SPDX-License-Identifier: MIT
//! A rendered ratatui `Buffer` as text: plain (for tests, pipes and `--once`) or with ANSI colors.

use std::fmt::Write;

use ratatui::buffer::Buffer;
use ratatui::style::{Color, Modifier};

/// The symbols of each row with trailing blanks removed, rows joined by newlines.
pub fn to_plain(buf: &Buffer) -> String {
    let mut out = String::new();
    for y in 0..buf.area.height {
        let start = out.len();
        for x in 0..buf.area.width {
            out.push_str(buf[(x, y)].symbol());
        }
        let trimmed = out[start..].trim_end().len();
        out.truncate(start + trimmed);
        out.push('\n');
    }
    out
}

fn sgr_color(out: &mut String, c: Color, fg: bool) {
    let base = if fg { 30 } else { 40 };
    let _ = match c {
        Color::Reset => write!(out, ";{}", base + 9),
        Color::Black => write!(out, ";{base}"),
        Color::Red => write!(out, ";{}", base + 1),
        Color::Green => write!(out, ";{}", base + 2),
        Color::Yellow => write!(out, ";{}", base + 3),
        Color::Blue => write!(out, ";{}", base + 4),
        Color::Magenta => write!(out, ";{}", base + 5),
        Color::Cyan => write!(out, ";{}", base + 6),
        Color::Gray => write!(out, ";{}", base + 7),
        Color::DarkGray => write!(out, ";{}", base + 60),
        Color::LightRed => write!(out, ";{}", base + 61),
        Color::LightGreen => write!(out, ";{}", base + 62),
        Color::LightYellow => write!(out, ";{}", base + 63),
        Color::LightBlue => write!(out, ";{}", base + 64),
        Color::LightMagenta => write!(out, ";{}", base + 65),
        Color::LightCyan => write!(out, ";{}", base + 66),
        Color::White => write!(out, ";{}", base + 67),
        Color::Rgb(r, g, b) => write!(out, ";{};2;{r};{g};{b}", base + 8),
        Color::Indexed(i) => write!(out, ";{};5;{i}", base + 8),
    };
}

/// Rows with SGR escapes where the style changes, each row ending in a reset.
pub fn to_ansi(buf: &Buffer) -> String {
    let mut out = String::new();
    for y in 0..buf.area.height {
        let mut last = None;
        let mut pending_blanks = 0usize;
        for x in 0..buf.area.width {
            let c = &buf[(x, y)];
            let style = (c.fg, c.bg, c.modifier);
            let blank_default = c.symbol() == " "
                && c.bg == Color::Reset
                && !c.modifier.contains(Modifier::REVERSED);
            if blank_default {
                pending_blanks += 1;
                continue;
            }
            for _ in 0..pending_blanks {
                out.push(' ');
            }
            pending_blanks = 0;
            if last != Some(style) {
                out.push_str("\x1b[0");
                for (m, code) in [
                    (Modifier::BOLD, 1),
                    (Modifier::DIM, 2),
                    (Modifier::UNDERLINED, 4),
                    (Modifier::REVERSED, 7),
                ] {
                    if c.modifier.contains(m) {
                        let _ = write!(out, ";{code}");
                    }
                }
                if c.fg != Color::Reset {
                    sgr_color(&mut out, c.fg, true);
                }
                if c.bg != Color::Reset {
                    sgr_color(&mut out, c.bg, false);
                }
                out.push('m');
                last = Some(style);
            }
            out.push_str(c.symbol());
        }
        out.push_str("\x1b[0m\n");
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::layout::Rect;
    use ratatui::style::Style;

    #[test]
    fn plain_text_drops_trailing_blanks() {
        let mut b = Buffer::empty(Rect::new(0, 0, 6, 2));
        b.set_string(1, 0, "ab", Style::new());
        assert_eq!(to_plain(&b), " ab\n\n");
    }

    #[test]
    fn ansi_text_sets_styles_only_where_they_change() {
        let mut b = Buffer::empty(Rect::new(0, 0, 4, 1));
        b.set_string(
            0,
            0,
            "a",
            Style::new()
                .fg(Color::Rgb(1, 2, 3))
                .add_modifier(Modifier::BOLD),
        );
        b.set_string(
            1,
            0,
            "b",
            Style::new()
                .fg(Color::Rgb(1, 2, 3))
                .add_modifier(Modifier::BOLD),
        );
        b.set_string(2, 0, "c", Style::new().fg(Color::Indexed(173)));
        let s = to_ansi(&b);
        assert_eq!(s, "\x1b[0;1;38;2;1;2;3mab\x1b[0;38;5;173mc\x1b[0m\n");
    }
}
