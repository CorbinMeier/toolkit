// SPDX-License-Identifier: MIT
//! The graph widget: braille (2x4 dots per cell), block (eighths) and ascii, growing up from the bottom
//! or down from the top. Each cell is colored by the reading of its own column.

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};

use crate::config::GraphStyle;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Dir {
    /// Fills from the bottom edge.
    Up,
    /// Fills from the top edge (the mirrored half of Disk, Network and Tokens).
    Down,
}

/// `(data column, reading in percent of the ceiling, height 0..1 of the cell from the base)` to a color.
pub type ColorFn<'a> = &'a dyn Fn(usize, f32, f32) -> Color;

/// Data columns a graph `width` cells wide wants: two per cell in braille, one otherwise.
pub fn data_cols(style: GraphStyle, width: u16) -> usize {
    let w = usize::from(width);
    match style {
        GraphStyle::Braille => w * 2,
        _ => w,
    }
}

const LEFT: [u8; 4] = [0x01, 0x02, 0x04, 0x40];
const RIGHT: [u8; 4] = [0x08, 0x10, 0x20, 0x80];
const LOWER: [char; 8] = ['▁', '▂', '▃', '▄', '▅', '▆', '▇', '█'];

/// How many of `units` a value fills: at least one if it is above zero.
fn level(v: f32, max: f32, units: usize) -> usize {
    if v.is_nan() || v <= 0.0 || max <= 0.0 {
        return 0;
    }
    (((v / max).min(1.0) * units as f32).round() as usize).max(1)
}

fn pct(v: f32, max: f32) -> f32 {
    if v.is_nan() || max <= 0.0 {
        0.0
    } else {
        (v / max * 100.0).clamp(0.0, 100.0)
    }
}

/// Draws `cols` (oldest left) scaled so that `max` fills the area's height.
pub fn draw(
    buf: &mut Buffer,
    area: Rect,
    cols: &[f32],
    max: f32,
    dir: Dir,
    style: GraphStyle,
    color: ColorFn,
) {
    if area.width == 0 || area.height == 0 {
        return;
    }
    let h = usize::from(area.height);
    let get = |i: usize| cols.get(i).copied().unwrap_or(f32::NAN);
    for cx in 0..area.width {
        let x = area.x + cx;
        let cxu = usize::from(cx);
        match style {
            GraphStyle::Braille => {
                let (li, ri) = (cxu * 2, cxu * 2 + 1);
                let (lv, rv) = (get(li), get(ri));
                let (dl, dr) = (level(lv, max, h * 4), level(rv, max, h * 4));
                if dl + dr == 0 {
                    continue;
                }
                let (hi, hv) = if rv > lv || lv.is_nan() {
                    (ri, rv)
                } else {
                    (li, lv)
                };
                for cy in 0..h {
                    // Distance in dots from the base to the top dot row of this cell.
                    let base_row = match dir {
                        Dir::Up => (h - 1 - cy) * 4,
                        Dir::Down => cy * 4,
                    };
                    let mut bits = 0u8;
                    for k in 0..4 {
                        // Dot row k counts from the cell's top; its distance from the base.
                        let g = match dir {
                            Dir::Up => base_row + (3 - k),
                            Dir::Down => base_row + k,
                        };
                        if g < dl {
                            bits |= LEFT[k];
                        }
                        if g < dr {
                            bits |= RIGHT[k];
                        }
                    }
                    if bits == 0 {
                        continue;
                    }
                    let frac = (base_row as f32 + 2.0) / (h * 4) as f32;
                    let ch = char::from_u32(0x2800 + u32::from(bits)).unwrap_or(' ');
                    put(
                        buf,
                        x,
                        area.y + cy as u16,
                        ch,
                        color(hi, pct(hv, max), frac),
                        false,
                    );
                }
            }
            GraphStyle::Block | GraphStyle::Ascii => {
                let v = get(cxu);
                let e = level(v, max, h * 8);
                if e == 0 {
                    continue;
                }
                for cy in 0..h {
                    let r = match dir {
                        Dir::Up => h - 1 - cy,
                        Dir::Down => cy,
                    };
                    let fill = e.saturating_sub(r * 8).min(8);
                    if fill == 0 {
                        continue;
                    }
                    let frac = (r as f32 + 0.5) / h as f32;
                    let c = color(cxu, pct(v, max), frac);
                    let y = area.y + cy as u16;
                    if style == GraphStyle::Ascii {
                        let ch = match fill {
                            8 => '#',
                            6..=7 => '|',
                            3..=5 => ':',
                            _ => '.',
                        };
                        put(buf, x, y, ch, c, false);
                    } else if fill == 8 {
                        put(buf, x, y, '█', c, false);
                    } else if dir == Dir::Up {
                        put(buf, x, y, LOWER[fill - 1], c, false);
                    } else {
                        // Filled from the top: the complementary lower block, reversed.
                        put(buf, x, y, LOWER[7 - fill], c, true);
                    }
                }
            }
        }
    }
}

fn put(buf: &mut Buffer, x: u16, y: u16, ch: char, fg: Color, reversed: bool) {
    if let Some(cell) = buf.cell_mut((x, y)) {
        let mut st = Style::new().fg(fg);
        if reversed {
            st = st.add_modifier(Modifier::REVERSED);
        }
        cell.set_char(ch).set_style(st);
    }
}

/// Dim vertical marks in the blank cells of column `x` (session resets in the Tokens graph).
pub fn draw_mark(buf: &mut Buffer, area: Rect, x: u16, style: Style) {
    if x < area.x || x >= area.x + area.width {
        return;
    }
    for y in area.y..area.y + area.height {
        if let Some(cell) = buf.cell_mut((x, y))
            && (cell.symbol() == " " || cell.symbol() == "\u{2800}")
        {
            cell.set_char('┊').set_style(style);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn render(style: GraphStyle, dir: Dir, cols: &[f32], w: u16, h: u16) -> Vec<String> {
        let area = Rect::new(0, 0, w, h);
        let mut buf = Buffer::empty(area);
        draw(&mut buf, area, cols, 100.0, dir, style, &|_, _, _| {
            Color::Reset
        });
        (0..h)
            .map(|y| (0..w).map(|x| buf[(x, y)].symbol().to_owned()).collect())
            .collect()
    }

    #[test]
    fn braille_fills_dots_from_the_bottom() {
        // Left column full (4 dots), right column half (2 dots), one cell high.
        let rows = render(GraphStyle::Braille, Dir::Up, &[100.0, 50.0], 1, 1);
        assert_eq!(rows, vec!["\u{28E7}".to_string()]);
        // Mirrored: filled from the top.
        let rows = render(GraphStyle::Braille, Dir::Down, &[100.0, 50.0], 1, 1);
        assert_eq!(rows, vec!["\u{285F}".to_string()]);
    }

    #[test]
    fn a_tiny_nonzero_value_still_shows_one_dot() {
        let rows = render(GraphStyle::Braille, Dir::Up, &[0.001, f32::NAN], 1, 2);
        assert_eq!(rows[0], " ");
        assert_eq!(rows[1], "\u{2840}");
    }

    #[test]
    fn block_uses_eighths_and_ascii_uses_four_levels() {
        let rows = render(GraphStyle::Block, Dir::Up, &[100.0, 75.0, 50.0, 12.5], 4, 1);
        assert_eq!(rows[0], "█▆▄▁");
        let rows = render(GraphStyle::Ascii, Dir::Up, &[100.0, 75.0, 50.0, 12.5], 4, 1);
        assert_eq!(rows[0], "#|:.");
    }

    #[test]
    fn block_down_reverses_the_complement() {
        let area = Rect::new(0, 0, 1, 1);
        let mut buf = Buffer::empty(area);
        draw(
            &mut buf,
            area,
            &[25.0],
            100.0,
            Dir::Down,
            GraphStyle::Block,
            &|_, _, _| Color::Reset,
        );
        assert_eq!(buf[(0, 0)].symbol(), "▆");
        assert!(buf[(0, 0)].modifier.contains(Modifier::REVERSED));
    }
}
