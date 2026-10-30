// SPDX-License-Identifier: MIT
//! Horizontal bars with eighth-cell precision, colored by the reading each cell stands for.

use ratatui::buffer::Buffer;
use ratatui::style::{Color, Style};

/// Left-aligned partial blocks for 1/8 to 7/8 of a cell.
const PARTIAL: [char; 7] = ['▏', '▎', '▍', '▌', '▋', '▊', '▉'];

/// A bar `w` cells wide at `(x, y)` filled to `pct` percent. Cell `i` is colored by `color` at the
/// reading its right edge stands for (so a half-full bar shows the lower half of the gradient).
pub fn hbar(
    buf: &mut Buffer,
    x: u16,
    y: u16,
    w: u16,
    pct: f32,
    color: &dyn Fn(f32) -> Color,
    track: Style,
) {
    if w == 0 {
        return;
    }
    let pct = if pct.is_nan() {
        0.0
    } else {
        pct.clamp(0.0, 100.0)
    };
    let eighths = (pct / 100.0 * f32::from(w) * 8.0).round() as u32;
    for i in 0..w {
        let cell_eighths = eighths.saturating_sub(u32::from(i) * 8).min(8);
        let Some(cell) = buf.cell_mut((x + i, y)) else {
            continue;
        };
        match cell_eighths {
            0 => {
                cell.set_char('░').set_style(track);
            }
            8 => {
                let at = (f32::from(i) + 1.0) / f32::from(w) * 100.0;
                cell.set_char('█').set_style(Style::new().fg(color(at)));
            }
            n => {
                let at = (f32::from(i) + 1.0) / f32::from(w) * 100.0;
                cell.set_char(PARTIAL[n as usize - 1])
                    .set_style(Style::new().fg(color(at)));
            }
        }
    }
}

/// Eighths of a cell filled out of a half `cells` wide at `pct` percent.
fn eighths(pct: f32, cells: u16) -> u32 {
    let pct = if pct.is_nan() {
        0.0
    } else {
        pct.clamp(0.0, 100.0)
    };
    (pct / 100.0 * f32::from(cells) * 8.0).round() as u32
}

/// A split bar `w` cells wide (at least 3): a centre divider `│`, the first reading growing from it to
/// the left, the second to the right, each half filled to its own percent on the usual `░` track. With
/// an even `w` the right half is one cell longer; callers pass an odd width.
///
/// The right half uses the left-aligned partial blocks of [`hbar`]. Unicode has no right-aligned block
/// for most eighths (only `▐` and `▕`), so a partial cell on the left half is drawn the other way round:
/// the left-aligned block that covers the *unfilled* part, in the track's color, on the fill color as
/// background. It reads as a fill that ends at that eighth.
pub fn split_bar(
    buf: &mut Buffer,
    (x, y, w): (u16, u16, u16),
    (left_pct, right_pct): (f32, f32),
    (left_color, right_color): (&dyn Fn(f32) -> Color, &dyn Fn(f32) -> Color),
    divider: Style,
    track: Style,
) {
    if w < 3 {
        return;
    }
    let left = (w - 1) / 2;
    let right = w - 1 - left;
    let track_fg = track.fg.unwrap_or(Color::DarkGray);
    let (el, er) = (eighths(left_pct, left), eighths(right_pct, right));
    for d in 0..left {
        let Some(cell) = buf.cell_mut((x + left - 1 - d, y)) else {
            continue;
        };
        let n = el.saturating_sub(u32::from(d) * 8).min(8);
        let at = (f32::from(d) + 1.0) / f32::from(left) * 100.0;
        match n {
            0 => {
                cell.set_char('░').set_style(track);
            }
            8 => {
                cell.set_char('█')
                    .set_style(Style::new().fg(left_color(at)));
            }
            n => {
                cell.set_char(PARTIAL[(8 - n) as usize - 1])
                    .set_style(Style::new().fg(track_fg).bg(left_color(at)));
            }
        }
    }
    if let Some(cell) = buf.cell_mut((x + left, y)) {
        cell.set_char('│').set_style(divider);
    }
    for d in 0..right {
        let Some(cell) = buf.cell_mut((x + left + 1 + d, y)) else {
            continue;
        };
        let n = er.saturating_sub(u32::from(d) * 8).min(8);
        let at = (f32::from(d) + 1.0) / f32::from(right) * 100.0;
        match n {
            0 => {
                cell.set_char('░').set_style(track);
            }
            8 => {
                cell.set_char('█')
                    .set_style(Style::new().fg(right_color(at)));
            }
            n => {
                cell.set_char(PARTIAL[n as usize - 1])
                    .set_style(Style::new().fg(right_color(at)));
            }
        }
    }
}

/// Segments `(percent of the bar, color)` placed one after the other, then the track.
pub fn stacked(buf: &mut Buffer, x: u16, y: u16, w: u16, segs: &[(f32, Color)], track: Style) {
    let mut at = 0u16;
    for &(pct, color) in segs {
        let n = ((pct.clamp(0.0, 100.0) / 100.0 * f32::from(w)).round() as u16).min(w - at);
        for i in 0..n {
            if let Some(c) = buf.cell_mut((x + at + i, y)) {
                c.set_char('█').set_style(Style::new().fg(color));
            }
        }
        at += n;
    }
    for i in at..w {
        if let Some(c) = buf.cell_mut((x + i, y)) {
            c.set_char('░').set_style(track);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::layout::Rect;

    fn row(buf: &Buffer, w: u16) -> String {
        (0..w).map(|x| buf[(x, 0)].symbol().to_owned()).collect()
    }

    #[test]
    fn bars_fill_in_eighths() {
        let mut buf = Buffer::empty(Rect::new(0, 0, 8, 1));
        hbar(&mut buf, 0, 0, 8, 50.0, &|_| Color::Reset, Style::new());
        assert_eq!(row(&buf, 8), "████░░░░");
        hbar(&mut buf, 0, 0, 8, 56.25, &|_| Color::Reset, Style::new());
        assert_eq!(row(&buf, 8), "████▌░░░");
        hbar(&mut buf, 0, 0, 8, f32::NAN, &|_| Color::Reset, Style::new());
        assert_eq!(row(&buf, 8), "░░░░░░░░");
        hbar(&mut buf, 0, 0, 8, 400.0, &|_| Color::Reset, Style::new());
        assert_eq!(row(&buf, 8), "████████");
    }

    #[test]
    fn stacked_segments_never_overflow_the_bar() {
        let mut buf = Buffer::empty(Rect::new(0, 0, 10, 1));
        stacked(
            &mut buf,
            0,
            0,
            10,
            &[(60.0, Color::Red), (70.0, Color::Blue)],
            Style::new(),
        );
        assert_eq!(row(&buf, 10), "██████████");
        stacked(&mut buf, 0, 0, 10, &[(30.0, Color::Red)], Style::new());
        assert_eq!(row(&buf, 10), "███░░░░░░░");
    }

    #[test]
    fn split_bars_grow_from_the_centre_in_both_directions() {
        let mut buf = Buffer::empty(Rect::new(0, 0, 11, 1));
        let c = |_: f32| Color::Reset;
        let mut draw = |l: f32, r: f32| {
            split_bar(
                &mut buf,
                (0, 0, 11),
                (l, r),
                (&c, &c),
                Style::new(),
                Style::new(),
            );
            row(&buf, 11)
        };
        assert_eq!(draw(0.0, 0.0), "░░░░░│░░░░░");
        assert_eq!(draw(100.0, 100.0), "█████│█████");
        assert_eq!(draw(40.0, 80.0), "░░░██│████░");
        assert_eq!(draw(100.0, 0.0), "█████│░░░░░");
        assert_eq!(draw(f32::NAN, 400.0), "░░░░░│█████");
        // A half cell: the right side grows as ▌, the left side as the block covering the rest.
        assert_eq!(draw(10.0, 10.0), "░░░░▌│▌░░░░");
    }

    #[test]
    fn a_left_partial_cell_is_the_track_color_on_the_fill_color() {
        let mut buf = Buffer::empty(Rect::new(0, 0, 11, 1));
        let fill = |_: f32| Color::Blue;
        let track = Style::new().fg(Color::Gray);
        split_bar(
            &mut buf,
            (0, 0, 11),
            (50.0, 0.0),
            (&fill, &fill),
            Style::new(),
            track,
        );
        // 50% of 5 cells = 2.5 cells: two full, then a half cell (left half track, right half fill).
        assert_eq!(row(&buf, 11), "░░▌██│░░░░░");
        let half = &buf[(2, 0)];
        assert_eq!((half.fg, half.bg), (Color::Gray, Color::Blue));
    }
}
