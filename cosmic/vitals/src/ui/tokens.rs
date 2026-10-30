// SPDX-License-Identifier: MIT
//! The Tokens section (spec §4, §5): Claude Code token usage over time from the local transcripts.

use std::fmt::Write;

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};

use super::graph::{self, Dir};
use super::section::{Line, bar_row, label_w, window_gutter};
use super::{Cx, Tw, fmt, meter, put};
use crate::app::Data;
use crate::config::TokSeries;
use crate::history::collapse;
use crate::input::Target;
use crate::theme::{Series, Theme};
use crate::tokens::{Counts, Family, Phase, TokenWindow, sum_families};

const MIN_MS: i64 = 60_000;
/// Claude's session window.
const SESSION_MS: i64 = 5 * 3_600_000;

/// The four rate bars, then one per model family with usage today.
pub fn bar_rows(data: &Data) -> (u16, u16) {
    let today = data.tokens.today(data.now_ms);
    let fam = today.iter().filter(|c| !c.is_zero()).count();
    (4 + fam as u16, 4)
}

fn per_min(data: &Data) -> u64 {
    let rate = data.tokens.recent(data.now_ms, 5);
    (rate.new_input() + rate.output) / 5
}

pub fn headline(cx: &mut Cx) -> Style {
    let t = &mut cx.sc.text2;
    t.clear();
    fmt::count(t, per_min(cx.data));
    t.push_str("/min");
    Style::new().fg(cx.theme.ramp(Series::TokOut, 100.0))
}

fn indexing(data: &Data) -> Option<u8> {
    data.tokens
        .progress()
        .filter(|p| p.phase != Phase::Live)
        .map(|p| p.percent())
}

/// `[io]` (click: next series) and the backfill progress.
pub fn title(cx: &mut Cx, buf: &mut Buffer, tw: &mut Tw) {
    let sc = &mut *cx.sc;
    tw.seg(buf, "  ", cx.theme.plain());
    sc.text.clear();
    let _ = write!(sc.text, "[{}]", cx.cfg.section.tokens.series.name());
    let chip = tw.seg(
        buf,
        &sc.text,
        cx.theme.plain().add_modifier(Modifier::UNDERLINED),
    );
    cx.hits.push(chip, Target::Series);
    if let Some(p) = indexing(cx.data) {
        sc.text.clear();
        let _ = write!(sc.text, "  indexing… {p}%");
        tw.seg(buf, &sc.text, cx.theme.muted_style());
    }
}

fn window(cx: &Cx) -> TokenWindow {
    TokenWindow::from_label(&cx.cfg.section.tokens.window).unwrap_or(TokenWindow::H24)
}

pub fn timeline(cx: &mut Cx, buf: &mut Buffer, area: Rect) {
    let w = window(cx);
    let g = window_gutter(cx, buf, area, w.label());
    graph_body(cx, buf, g, w, cx.data.now_ms);
}

/// Sums of the buckets into the scratch columns, then the mirrored or single graph.
fn graph_body(cx: &mut Cx, buf: &mut Buffer, inner: Rect, window: TokenWindow, now: i64) {
    let (theme, cfg, data) = (cx.theme, cx.cfg, cx.data);
    let conf = &cfg.section.tokens;
    let sc = &mut *cx.sc;
    // Per bucket: new input, output, cache read, and the stack [cache read, new input, output].
    sc.up.clear();
    sc.down.clear();
    sc.cache.clear();
    sc.stack.clear();
    for b in data.tokens.series_iter(window, now) {
        let c = sum_families(&b);
        let (i, o, r) = (c.new_input() as f32, c.output as f32, c.cache_read as f32);
        sc.up.push(i);
        sc.down.push(o);
        sc.cache.push(r);
        sc.stack.push([r, i, o, 0.0]);
    }
    let style = conf.c.style;
    let gw = graph::data_cols(style, inner.width);
    let bucket_unit = if window.bucket_ms() == MIN_MS {
        "min"
    } else {
        "h"
    };
    match conf.series {
        TokSeries::Io if inner.height == 1 => {
            collapse(&sc.up, gw, &mut sc.cols);
            collapse(&sc.down, gw, &mut sc.cols2);
            for (a, b) in sc.cols.iter_mut().zip(&sc.cols2) {
                *a += *b;
            }
            let max = max_of(&sc.cols);
            graph::draw(buf, inner, &sc.cols, max, Dir::Up, style, &|_, p, _| {
                theme.ramp(Series::TokIn, p)
            });
        }
        TokSeries::Io => {
            let up_h = inner.height.div_ceil(2);
            let up_area = Rect::new(inner.x, inner.y, inner.width, up_h);
            let down_area = Rect::new(inner.x, inner.y + up_h, inner.width, inner.height - up_h);
            collapse(&sc.up, gw, &mut sc.cols);
            let up_max = max_of(&sc.cols);
            graph::draw(
                buf,
                up_area,
                &sc.cols,
                up_max,
                Dir::Up,
                style,
                &|_, p, _| theme.ramp(Series::TokIn, p),
            );
            label(
                buf,
                theme,
                &mut sc.text,
                up_area,
                "in",
                up_max,
                bucket_unit,
                Dir::Up,
            );
            if down_area.height > 0 {
                collapse(&sc.down, gw, &mut sc.cols2);
                let down_max = max_of(&sc.cols2);
                graph::draw(
                    buf,
                    down_area,
                    &sc.cols2,
                    down_max,
                    Dir::Down,
                    style,
                    &|_, p, _| theme.ramp(Series::TokOut, p),
                );
                label(
                    buf,
                    theme,
                    &mut sc.text,
                    down_area,
                    "out",
                    down_max,
                    bucket_unit,
                    Dir::Down,
                );
            }
        }
        TokSeries::Cache => {
            collapse(&sc.cache, gw, &mut sc.cols);
            let max = max_of(&sc.cols);
            graph::draw(buf, inner, &sc.cols, max, Dir::Up, style, &|_, p, _| {
                theme.ramp(Series::TokCache, p)
            });
            label(
                buf,
                theme,
                &mut sc.text,
                inner,
                "cache read",
                max,
                bucket_unit,
                Dir::Up,
            );
        }
        TokSeries::Total => {
            // Stacked: the cell's color follows the part of the stack it sits in.
            sc.up.clear();
            sc.up.extend(sc.stack.iter().map(|s| s[0] + s[1] + s[2]));
            collapse_stack(&sc.up, &sc.stack, gw, &mut sc.cols, &mut sc.stack2);
            let max = max_of(&sc.cols);
            let stack = &sc.stack2;
            graph::draw(
                buf,
                inner,
                &sc.cols,
                max,
                Dir::Up,
                style,
                &|col, _, frac| {
                    let s = stack.get(col).copied().unwrap_or_default();
                    let at = frac * max;
                    let series = if at < s[0] {
                        Series::TokCache
                    } else if at < s[0] + s[1] {
                        Series::TokIn
                    } else {
                        Series::TokOut
                    };
                    theme.ramp(series, 100.0)
                },
            );
            label(
                buf,
                theme,
                &mut sc.text,
                inner,
                "total",
                max,
                bucket_unit,
                Dir::Up,
            );
        }
    }
    // Session resets inside the window: dim vertical marks.
    sc.marks.clear();
    if let Some(m) = &data.claude.merged
        && let Some(at) = m.reading.session.as_ref().and_then(|s| s.resets_at)
    {
        sc.marks.push(at);
        sc.marks.push(at - SESSION_MS);
    }
    sc.marks.extend(data.session_resets.iter().copied());
    let bucket = window.bucket_ms();
    let end = (now.div_euclid(bucket) + 1) * bucket;
    let start = end - window.buckets() as i64 * bucket;
    for &t in &sc.marks {
        if t > start && t < end {
            let x = inner.x
                + ((t - start) as i128 * i128::from(inner.width) / i128::from(end - start)) as u16;
            graph::draw_mark(
                buf,
                inner,
                x.min(inner.x + inner.width - 1),
                theme.muted_style(),
            );
        }
    }
}

fn max_of(v: &[f32]) -> f32 {
    v.iter()
        .copied()
        .filter(|x| !x.is_nan())
        .fold(0.0, f32::max)
}

/// `in ≤12K/min` in the corner of a half.
fn label(
    buf: &mut Buffer,
    theme: &Theme,
    text: &mut String,
    area: Rect,
    name: &str,
    max: f32,
    unit: &str,
    dir: Dir,
) {
    if area.width < 24 || area.height < 2 || max <= 0.0 {
        return;
    }
    text.clear();
    let _ = write!(text, "{name} ≤");
    fmt::count(text, max as u64);
    let _ = write!(text, "/{unit}");
    let y = if dir == Dir::Up {
        area.y
    } else {
        area.y + area.height - 1
    };
    put(buf, area.x, y, text, theme.muted_style(), area.width);
}

/// Like [`collapse`], but also keeps the stack of the bucket that set each column's maximum.
fn collapse_stack(
    totals: &[f32],
    stack: &[[f32; 4]],
    width: usize,
    out: &mut Vec<f32>,
    out_stack: &mut Vec<[f32; 4]>,
) {
    out.clear();
    out.resize(width, f32::NAN);
    out_stack.clear();
    out_stack.resize(width, [0.0; 4]);
    let n = totals.len();
    if n == 0 {
        return;
    }
    for c in 0..width {
        let lo = (c * n / width).min(n - 1);
        let hi = ((c + 1) * n / width).max(lo + 1).min(n);
        let mut best = None;
        for i in lo..hi {
            if best.is_none_or(|b| totals[i] > totals[b]) {
                best = Some(i);
            }
        }
        if let Some(b) = best {
            out[c] = totals[b];
            out_stack[c] = stack[b];
        }
    }
}

/// Per-minute rate of each count over the last 5 minutes and the busiest minute of the window.
fn rates(data: &Data, window: TokenWindow) -> ([u64; 4], [u64; 4]) {
    let get = |c: &Counts| [c.input, c.output, c.cache_write, c.cache_read];
    let now = get(&data.tokens.recent(data.now_ms, 5)).map(|v| v / 5);
    let per_bucket = (window.bucket_ms() / MIN_MS).max(1) as u64;
    let mut peak = [1u64; 4];
    for b in data.tokens.series_iter(window, data.now_ms) {
        let v = get(&sum_families(&b));
        for (p, v) in peak.iter_mut().zip(v) {
            *p = (*p).max(v / per_bucket);
        }
    }
    (now, peak)
}

/// The Tokens value column, the widest of its kinds: a family's `100%  1000K` and a rate `1000K/min`.
const TOK_VAL_W: u16 = fmt::PERCENT_W + 2 + fmt::COUNT_W;

pub fn bars(cx: &mut Cx, buf: &mut Buffer, r: Rect) {
    let (theme, data) = (cx.theme, cx.data);
    let (now, peak) = rates(data, window(cx));
    let sc = &mut *cx.sc;
    let rows = [
        ("input", Series::TokIn),
        ("output", Series::TokOut),
        ("cache wr", Series::TokCache),
        ("cache rd", Series::TokCache),
    ];
    let lw = (label_w(8), TOK_VAL_W);
    for (i, (label, series)) in rows.into_iter().enumerate().take(usize::from(r.height)) {
        sc.text.clear();
        fmt::count(&mut sc.text, now[i]);
        sc.text.push_str("/min");
        let pct = now[i] as f32 / peak[i] as f32 * 100.0;
        bar_row(
            buf,
            theme,
            Rect::new(r.x, r.y + i as u16, r.width, 1),
            label,
            lw,
            pct,
            &|_| theme.ramp(series, 100.0),
            &sc.text,
        );
    }
    // One bar per model family: its share of today's tokens.
    let today = data.tokens.today(data.now_ms);
    let total = sum_families(&today).all().max(1);
    let live = Family::ALL.iter().filter(|f| !today[f.index()].is_zero());
    for (k, f) in live.enumerate() {
        let y = r.y + 4 + k as u16;
        if y >= r.y + r.height {
            break;
        }
        let c = &today[f.index()];
        let share = c.all() as f32 / total as f32 * 100.0;
        sc.text.clear();
        fmt::percent(&mut sc.text, share);
        sc.text.push_str("  ");
        fmt::count(&mut sc.text, c.all());
        bar_row(
            buf,
            theme,
            Rect::new(r.x, y, r.width, 1),
            f.name(),
            lw,
            share,
            &|_| theme.ramp(Series::TokOut, 100.0),
            &sc.text,
        );
    }
}

pub fn summary(cx: &mut Cx, buf: &mut Buffer, r: Rect) {
    let (theme, data) = (cx.theme, cx.data);
    let sc = &mut *cx.sc;
    let today = sum_families(&data.tokens.today(data.now_ms));
    let mut line = Line::new(r);
    sc.text.clear();
    sc.text.push_str("today in ");
    fmt::count(&mut sc.text, today.new_input());
    sc.text.push_str(" out ");
    fmt::count(&mut sc.text, today.output);
    sc.text.push_str(" cache ");
    fmt::count(&mut sc.text, today.cache_read);
    line.item(buf, theme, &sc.text, theme.plain());
    sc.text.clear();
    fmt::count(&mut sc.text, per_min(data));
    sc.text.push_str("/min");
    line.item(
        buf,
        theme,
        &sc.text,
        Style::new().fg(theme.ramp(Series::TokOut, 100.0)),
    );
    if let Some(p) = indexing(data) {
        sc.text.clear();
        let _ = write!(sc.text, "indexing… {p}%");
        line.item(buf, theme, &sc.text, theme.muted_style());
    }
}

pub fn meter(cx: &mut Cx, buf: &mut Buffer, r: Rect) {
    let (theme, data) = (cx.theme, cx.data);
    let now = data.now_ms;
    let today = sum_families(&data.tokens.today(now));
    let rate = per_min(data);
    let sc = &mut *cx.sc;
    sc.text.clear();
    fmt::count(&mut sc.text, rate);
    sc.text.push_str("/min  today ");
    fmt::count(&mut sc.text, today.new_input());
    sc.text.push_str(" in · ");
    fmt::count(&mut sc.text, today.output);
    sc.text.push_str(" out");
    if let Some(p) = indexing(data) {
        let _ = write!(sc.text, "  indexing… {p}%");
    }
    // The bar: the last 5 minutes' rate against the busiest minute of the last hour.
    let busiest = data
        .tokens
        .series_iter(TokenWindow::H1, now)
        .map(|b| {
            let c = sum_families(&b);
            c.new_input() + c.output
        })
        .max()
        .unwrap_or(0)
        .max(1);
    let pct = rate as f32 / busiest as f32 * 100.0;
    meter::line(
        buf,
        theme,
        r,
        "Tokens",
        pct,
        &|p| theme.ramp(Series::TokOut, p),
        &sc.text,
    );
}
