// SPDX-License-Identifier: MIT
//! Where each section goes (spec §4): a header line, then the config's rows (or one column) of sections,
//! each as tall as its parts need. Too little room drops parts in the spec's order. Pure: the same inputs
//! always give the same rectangles, and nothing allocates (at most six sections, six rows).

use ratatui::layout::Rect;

use crate::config::{Config, LayoutMode, PanelId, RowSpec};

pub const MIN_W: u16 = 40;
pub const MIN_H: u16 = 12;
/// `layout = "auto"` uses the rows from this width on, a single column below it.
pub const ROWS_W: u16 = 100;

/// What a section would like, given its width: the rows of each part (0 when the part is off).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Needs {
    pub tl: u16,
    /// All the bar rows, and the headline bar rows that stay when the extras go.
    pub bars_full: u16,
    pub bars_head: u16,
    pub summary: bool,
}

/// What a section gets: rows per part. `meter` is the last resort, one line without a frame.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Plan {
    pub tl: u16,
    /// A blank row between the timeline and the bars (spec §4); the first thing given up.
    pub gap: bool,
    pub bars: u16,
    pub summary: bool,
    pub meter: bool,
}

impl Plan {
    /// Rows of the whole box: the border, the parts, never less than 3.
    pub fn height(&self) -> u16 {
        if self.meter {
            1
        } else {
            (2 + self.tl + u16::from(self.gap) + self.bars + u16::from(self.summary)).max(3)
        }
    }
}

/// A section's rectangle and what is drawn in it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Slot {
    pub rect: Rect,
    pub plan: Plan,
}

#[derive(Debug, Clone, Default)]
pub struct Layout {
    pub header: Rect,
    pub slots: [Option<Slot>; 6],
}

impl Layout {
    pub fn slot(&self, p: PanelId) -> Option<Slot> {
        self.slots[p.index()]
    }
}

/// The plan at a degrade level: 0 as wanted, 1 no blank row between timeline and bars, 2 timeline to
/// one row, 3 no timeline, 4 no summary, 5 headline bars only, 6 a one-line meter.
fn plan_at(n: Needs, level: u8) -> Plan {
    let bars = if level >= 5 { n.bars_head } else { n.bars_full };
    Plan {
        tl: match level {
            0 | 1 => n.tl,
            2 => n.tl.min(1),
            _ => 0,
        },
        gap: level == 0 && n.tl > 0 && bars > 0,
        bars,
        summary: n.summary && level < 4,
        meter: level >= 6,
    }
}

/// Splits `width` by relative weights; the last one takes the remainder.
fn split(width: u16, weights: &[(PanelId, u16)], i: usize) -> (u16, u16) {
    let total: u32 = weights.iter().map(|w| u32::from(w.1)).sum::<u32>().max(1);
    let edge = |k: usize| -> u16 {
        if k >= weights.len() {
            return width;
        }
        let before: u32 = weights[..k].iter().map(|w| u32::from(w.1)).sum();
        (u32::from(width) * before / total) as u16
    };
    let (a, b) = (edge(i), edge(i + 1));
    (a, b - a)
}

pub fn compute(
    area: Rect,
    cfg: &Config,
    zoom: Option<PanelId>,
    needs: &dyn Fn(PanelId, u16) -> Needs,
) -> Layout {
    let mut out = Layout {
        header: Rect::new(area.x, area.y, area.width, 1.min(area.height)),
        ..Layout::default()
    };
    let body = Rect::new(
        area.x,
        area.y + 1,
        area.width,
        area.height.saturating_sub(1),
    );

    // The rows to lay out.
    let mut rows = [RowSpec {
        items: [(PanelId::Cpu, 1); 6],
        n: 0,
    }; 6];
    let mut n = 0;
    if let Some(z) = zoom.filter(|&z| cfg.is_visible(z)) {
        rows[0].items[0] = (z, 1);
        rows[0].n = 1;
        n = 1;
    } else {
        let resolved = cfg.resolve(true);
        let use_rows = match cfg.layout {
            LayoutMode::Rows => true,
            LayoutMode::Column => false,
            LayoutMode::Auto => area.width >= ROWS_W,
        };
        if use_rows {
            for r in resolved.rows() {
                rows[n] = *r;
                n += 1;
            }
        } else {
            for p in resolved.flat() {
                rows[n].items[0] = (p, 1);
                rows[n].n = 1;
                n += 1;
            }
        }
    }
    if n == 0 {
        return out;
    }

    // Each section's wishes at its width.
    let mut want = [[Needs::default(); 6]; 6];
    for (r, row) in rows[..n].iter().enumerate() {
        for (i, &(p, _)) in row.items().iter().enumerate() {
            let (_, w) = split(body.width, row.items(), i);
            let mut nd = needs(p, w.saturating_sub(2));
            if zoom == Some(p) {
                // The zoomed timeline takes whatever the bars and summary leave.
                nd.tl = 1;
            }
            want[r][i] = nd;
        }
    }
    let row_height = |r: usize, level: u8| -> u16 {
        (0..rows[r].n)
            .map(|i| plan_at(want[r][i], level).height())
            .max()
            .unwrap_or(0)
    };

    // Too tall: take parts away, the lowest level first and the last row first.
    let mut level = [0u8; 6];
    let total = |level: &[u8; 6]| -> u16 { (0..n).map(|r| row_height(r, level[r])).sum() };
    while total(&level) > body.height {
        let Some(r) = (0..n)
            .filter(|&r| level[r] < 6)
            .min_by_key(|&r| (level[r], n - r))
        else {
            break;
        };
        level[r] += 1;
    }

    // The screen is always filled: the rows' spare height is shared out by weight to timelines.
    let weight = |r: usize, i: usize| -> u16 {
        let (p, _) = rows[r].items[i];
        if level[r] <= 1 && want[r][i].tl > 0 {
            if zoom.is_some() {
                1
            } else {
                cfg.common(p).grow
            }
        } else {
            0
        }
    };
    let row_weight = |r: usize| -> u32 {
        (0..rows[r].n)
            .map(|i| u32::from(weight(r, i)))
            .max()
            .unwrap_or(0)
    };
    let spare = u32::from(body.height.saturating_sub(total(&level)));
    let total_w: u32 = (0..n).map(row_weight).sum();
    let mut share = [0u16; 6];
    if total_w > 0 {
        let mut given = 0;
        for (r, sh) in share.iter_mut().enumerate().take(n) {
            *sh = (spare * row_weight(r) / total_w) as u16;
            given += u32::from(*sh);
        }
        // The remainder goes one row at a time to the rows that take extra.
        let mut left = spare - given;
        for k in 0..n * 6 {
            let r = k % n;
            if left == 0 {
                break;
            }
            if row_weight(r) > 0 {
                share[r] += 1;
                left -= 1;
            }
        }
    }

    let mut y = body.y;
    for (r, row) in rows[..n].iter().enumerate() {
        let h_row = row_height(r, level[r]) + share[r];
        for (i, &(p, _)) in row.items().iter().enumerate() {
            let mut plan = plan_at(want[r][i], level[r]);
            // Boxes in a row line up: what a section lacks goes to its timeline (the spare rows only
            // when its weight is above 0), else it stays blank above the summary.
            let natural = plan.height();
            let mut extra = h_row.saturating_sub(natural);
            if plan.tl > 0 && !plan.meter {
                let keeps = if weight(r, i) > 0 { 0 } else { share[r] };
                let to_tl = extra - keeps.min(extra);
                plan.tl += to_tl;
                extra -= to_tl;
            }
            let (x, w) = split(body.width, row.items(), i);
            let h = if plan.meter { 1 } else { h_row };
            let _ = extra;
            let rect = Rect::new(body.x + x, y, w, h.min(body.height));
            out.slots[p.index()] = Some(Slot { rect, plan });
        }
        y += h_row;
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 2 timeline rows, 3 bars (1 headline), a summary.
    fn nd(_: PanelId, _: u16) -> Needs {
        Needs {
            tl: 2,
            bars_full: 3,
            bars_head: 1,
            summary: true,
        }
    }

    fn rect(l: &Layout, p: PanelId) -> Rect {
        l.slot(p).unwrap().rect
    }

    fn plan(l: &Layout, p: PanelId) -> Plan {
        l.slot(p).unwrap().plan
    }

    #[test]
    fn wide_terminals_fill_the_screen_by_growing_timelines() {
        // 4 rows of 9 (2 timeline + gap + 3 bars + summary + border) = 36; 49 rows are available.
        let l = compute(Rect::new(0, 0, 200, 50), &Config::default(), None, &nd);
        let bottom = rect(&l, PanelId::Claude);
        assert_eq!(
            bottom.y + bottom.height,
            50,
            "the last box ends at the screen's end"
        );
        let grown: u16 = [
            PanelId::Cpu,
            PanelId::Disk,
            PanelId::Tokens,
            PanelId::Claude,
        ]
        .iter()
        .map(|&p| plan(&l, p).tl)
        .sum();
        assert_eq!(
            grown,
            8 + 13,
            "13 spare rows shared out evenly over the 4 rows"
        );
        assert_eq!(rect(&l, PanelId::Cpu).height, rect(&l, PanelId::Mem).height);
        assert_eq!(rect(&l, PanelId::Mem).x, 100);
        // Spare rows go to the shorter box's timeline when its row mate is taller.
        let tall = |p: PanelId, w: u16| {
            let mut n = nd(p, w);
            if p == PanelId::Cpu {
                n.bars_full = 8;
            }
            n
        };
        let l = compute(Rect::new(0, 0, 200, 50), &Config::default(), None, &tall);
        assert_eq!(rect(&l, PanelId::Cpu).height, rect(&l, PanelId::Mem).height);
        assert!(plan(&l, PanelId::Mem).tl > plan(&l, PanelId::Cpu).tl);
    }

    #[test]
    fn narrow_terminals_get_one_column_in_reading_order() {
        let l = compute(Rect::new(0, 0, 99, 60), &Config::default(), None, &nd);
        let ys: Vec<u16> = PanelId::ALL.iter().map(|&p| rect(&l, p).y).collect();
        assert!(PanelId::ALL.iter().all(|&p| rect(&l, p).x == 0));
        assert!(ys.windows(2).all(|w| w[0] < w[1]), "{ys:?}");
        let mut c = Config {
            layout: LayoutMode::Rows,
            ..Config::default()
        };
        let r = compute(Rect::new(0, 0, 99, 60), &c, None, &nd);
        assert_eq!(rect(&r, PanelId::Mem).x, 49);
        c.layout = LayoutMode::Column;
        let col = compute(Rect::new(0, 0, 200, 60), &c, None, &nd);
        assert_eq!(rect(&col, PanelId::Mem).x, 0);
    }

    #[test]
    fn widths_are_relative_and_disabled_sections_leave_their_row() {
        let mut c = Config::default();
        c.row[0].widths = vec![3, 1];
        let l = compute(Rect::new(0, 0, 120, 40), &c, None, &nd);
        assert_eq!(rect(&l, PanelId::Cpu).width, 90);
        assert_eq!(rect(&l, PanelId::Mem).width, 30);
        c.section.memory.c.enabled = false;
        let l = compute(Rect::new(0, 0, 120, 40), &c, None, &nd);
        assert_eq!(rect(&l, PanelId::Cpu).width, 120);
        assert!(l.slot(PanelId::Mem).is_none());
    }

    #[test]
    fn a_short_terminal_drops_parts_last_row_first() {
        // 80x40 in one column: 6 sections of 8 rows do not fit. Timelines shrink to one row everywhere,
        // then go from the last section up, until it fits.
        let l = compute(Rect::new(0, 0, 80, 40), &Config::default(), None, &nd);
        let used: u16 = PanelId::ALL.iter().map(|&p| rect(&l, p).height).sum();
        assert!(used <= 39, "{used}");
        assert_eq!(plan(&l, PanelId::Cpu).tl, 1);
        assert_eq!(plan(&l, PanelId::Disk).tl, 1);
        assert_eq!(plan(&l, PanelId::Net).tl, 0);
        assert_eq!(plan(&l, PanelId::Claude).tl, 0);
        assert!(
            plan(&l, PanelId::Claude).summary,
            "summaries go after timelines"
        );
        // At 40x12 even the frames go: one-line meters for the tail, bars are the last thing to go.
        let l = compute(Rect::new(0, 0, 40, 12), &Config::default(), None, &nd);
        let total: u16 = PanelId::ALL.iter().map(|&p| rect(&l, p).height).sum();
        assert!(total <= 11, "{total}");
        assert!(plan(&l, PanelId::Claude).meter);
        assert!(PanelId::ALL.iter().all(|&p| l.slot(p).is_some()));
        let ends = rect(&l, PanelId::Claude);
        assert!(ends.y + ends.height <= 12);
    }

    #[test]
    fn grow_is_a_weight() {
        let mut c = Config::default();
        c.section.disk.c.grow = 0;
        c.section.tokens.c.grow = 3;
        let l = compute(Rect::new(0, 0, 200, 50), &c, None, &nd);
        assert_eq!(
            plan(&l, PanelId::Disk).tl,
            2,
            "weight 0 stays at its minimum"
        );
        assert!(plan(&l, PanelId::Tokens).tl > plan(&l, PanelId::Cpu).tl);
        let bottom = rect(&l, PanelId::Claude);
        assert_eq!(bottom.y + bottom.height, 50);
        // The zero-weight section's box still lines up with its row mate.
        assert_eq!(
            rect(&l, PanelId::Disk).height,
            rect(&l, PanelId::Net).height
        );
        // With every timeline off nothing grows and the bottom stays empty.
        let off = |p, w| Needs { tl: 0, ..nd(p, w) };
        let l = compute(Rect::new(0, 0, 200, 50), &Config::default(), None, &off);
        let bottom = rect(&l, PanelId::Claude);
        assert!(bottom.y + bottom.height < 50);
    }

    #[test]
    fn zoom_gives_the_timeline_every_row_the_bars_and_summary_leave() {
        let l = compute(
            Rect::new(0, 0, 120, 40),
            &Config::default(),
            Some(PanelId::Disk),
            &nd,
        );
        assert!(l.slot(PanelId::Cpu).is_none());
        assert_eq!(rect(&l, PanelId::Disk), Rect::new(0, 1, 120, 39));
        assert_eq!(plan(&l, PanelId::Disk).tl, 39 - 2 - 1 - 3 - 1);
    }

    #[test]
    fn a_section_with_every_part_off_keeps_a_three_row_frame() {
        let nothing = |_, _| Needs::default();
        let l = compute(Rect::new(0, 0, 100, 30), &Config::default(), None, &nothing);
        assert_eq!(rect(&l, PanelId::Cpu).height, 3);
    }
}
