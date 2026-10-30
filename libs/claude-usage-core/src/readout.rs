// SPDX-License-Identifier: MIT
//! The usage readout shared by the COSMIC applet's hover text and `vitals`: the 5 Hour block
//! ("5 Hour : 8% / 62%" + "Resets : 7:19 PM"), then one group of Daily (today's part of the week budget),
//! Weekly and the per-model weeks sharing the week's Resets line, then the stale line.
//! [`lines`] is the one implementation (rows a TUI can draw); [`text`] joins them into the hover string.

use chrono::{Local, TimeZone};

use crate::pace::{self, Kind};
use crate::reading::{Limit, Reading};
use crate::words;

const NO_EXPECTED: &str = "\u{2014}";

/// One line of the readout.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Line {
    /// "5 Hour : 8% / 62%": `used` and `expected` are already formatted ("8%", "62%", or an em dash).
    Row {
        label: String,
        used: String,
        expected: String,
    },
    /// "Resets : 7:19 PM": the local reset time of the rows just above it.
    Reset(String),
    /// Blank separator between blocks.
    Blank,
    /// A free-standing message: "Reading usage…", "No usage yet", or the stale line.
    Note(String),
}

/// Width the labels (and "Resets") are padded to so the colons line up: the longest row label, at least 6.
pub fn label_width(lines: &[Line]) -> usize {
    lines
        .iter()
        .filter_map(|l| match l {
            Line::Row { label, .. } => Some(label.chars().count()),
            _ => None,
        })
        .chain([6])
        .max()
        .unwrap_or(6)
}

/// Local reset time: "7:19 PM" on the same calendar day as now, otherwise "Thu 6:00 AM".
fn reset_clock(resets_at: i64, now_ms: i64) -> Option<String> {
    let at = Local.timestamp_millis_opt(resets_at).single()?;
    let now = Local.timestamp_millis_opt(now_ms).single()?;
    let fmt = if at.date_naive() == now.date_naive() {
        "%-I:%M %p"
    } else {
        "%a %-I:%M %p"
    };
    Some(at.format(fmt).to_string())
}

/// "Fable" from "Week (Fable)".
fn model_name(label: &str) -> &str {
    label
        .strip_prefix("Week (")
        .and_then(|l| l.strip_suffix(')'))
        .unwrap_or(label)
}

type Block = (String, String, String, Option<String>);

/// (label, used, expected, reset) for a limit, or None while it has no usage.
fn block(label: &str, kind: Kind, limit: &Limit, now_ms: i64) -> Option<Block> {
    if limit.percent <= 0.0 {
        return None;
    }
    let expected = pace::elapsed_percent(kind, limit.resets_at, now_ms).map_or_else(
        || NO_EXPECTED.to_owned(),
        |e| format!("{}%", pace::round(e)),
    );
    let reset = limit.resets_at.and_then(|r| reset_clock(r, now_ms));
    Some((
        label.to_owned(),
        format!("{}%", pace::round(limit.percent)),
        expected,
        reset,
    ))
}

/// The Daily line for the week limit: today's share of the week allowance used / the part's marker (its own
/// boundary is not shown, so the reset slot is None). Shown whenever the Weekly block is, even at 0%.
fn daily_block(week: &Limit, now_ms: i64) -> Option<Block> {
    if week.percent <= 0.0 {
        return None;
    }
    let part = pace::day_part(week.resets_at, now_ms)?;
    Some((
        "Daily".to_owned(),
        format!(
            "{}%",
            pace::round(pace::daily_percent(week.percent, part.index))
        ),
        format!("{}%", pace::round(part.elapsed_percent)),
        None,
    ))
}

/// The readout as lines. `read_at_ms` and `stale` add the trailing stale line when both say so.
pub fn lines(
    reading: Option<&Reading>,
    read_at_ms: Option<i64>,
    stale: bool,
    now_ms: i64,
) -> Vec<Line> {
    let Some(r) = reading else {
        return vec![Line::Note("Reading usage\u{2026}".to_owned())];
    };
    let session = r
        .session
        .as_ref()
        .and_then(|s| block("5 Hour", Kind::Session, s, now_ms));
    // Daily, Weekly and the per-model weeks share one Resets line: the week's.
    let mut group = Vec::new();
    if let Some(w) = &r.week {
        group.extend(daily_block(w, now_ms));
        group.extend(block("Weekly", Kind::Week, w, now_ms));
    }
    for m in &r.models {
        group.extend(block(model_name(&m.label), Kind::Week, m, now_ms));
    }
    let group_reset = r
        .week
        .iter()
        .chain(r.models.iter())
        .find_map(|l| l.resets_at)
        .and_then(|t| reset_clock(t, now_ms));
    let row = |(label, used, expected, _): &Block| Line::Row {
        label: label.clone(),
        used: used.clone(),
        expected: expected.clone(),
    };
    let mut out = Vec::new();
    if let Some(b) = &session {
        out.push(row(b));
        out.extend(b.3.clone().map(Line::Reset));
    }
    if !group.is_empty() {
        if !out.is_empty() {
            out.push(Line::Blank);
        }
        out.extend(group.iter().map(row));
        out.extend(group_reset.map(Line::Reset));
    }
    if out.is_empty() {
        out.push(Line::Note("No usage yet".to_owned()));
    }
    if stale && let Some(at) = read_at_ms {
        out.push(Line::Blank);
        out.push(Line::Note(words::stale_words(at)));
    }
    out
}

/// The hover text: [`lines`] joined with newlines, colons aligned.
pub fn text(
    reading: Option<&Reading>,
    read_at_ms: Option<i64>,
    stale: bool,
    now_ms: i64,
) -> String {
    let lines = lines(reading, read_at_ms, stale, now_ms);
    let width = label_width(&lines);
    lines
        .iter()
        .map(|l| match l {
            Line::Row {
                label,
                used,
                expected,
            } => format!("{label:<width$} : {used} / {expected}"),
            Line::Reset(t) => format!("{:<width$} : {t}", "Resets"),
            Line::Blank => String::new(),
            Line::Note(n) => n.clone(),
        })
        .collect::<Vec<_>>()
        .join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Stand-in for the applet's poller status: a stale reading is one with an error.
    #[derive(Default)]
    struct Status {
        reading: Option<Reading>,
        read_at: Option<i64>,
        error: Option<String>,
    }
    fn tooltip_text(s: &Status, now_ms: i64) -> String {
        text(s.reading.as_ref(), s.read_at, s.error.is_some(), now_ms)
    }

    const HOUR: i64 = 3_600_000;

    /// Wed Oct 28 2026 12:00 local.
    fn now() -> i64 {
        Local
            .with_ymd_and_hms(2026, 10, 28, 12, 0, 0)
            .unwrap()
            .timestamp_millis()
    }
    fn local(d: u32, h: u32, m: u32) -> i64 {
        Local
            .with_ymd_and_hms(2026, 10, d, h, m, 0)
            .unwrap()
            .timestamp_millis()
    }
    fn limit(label: &str, percent: f64, resets_at: Option<i64>) -> Limit {
        Limit {
            label: label.into(),
            percent,
            resets_text: None,
            resets_at,
        }
    }
    fn status(reading: Reading) -> Status {
        Status {
            reading: Some(reading),
            read_at: Some(local(28, 11, 55)),
            error: None,
        }
    }
    fn full() -> Reading {
        Reading {
            // 2 h left of 5 h: 60% gone.
            session: Some(limit("Session", 8.4, Some(now() + 2 * HOUR))),
            // Thu 6:00 AM: 18 h left of 168 h: 89% gone.
            week: Some(limit("Week (all models)", 81.0, Some(local(29, 6, 0)))),
            models: vec![limit("Week (Fable)", 3.0, Some(local(29, 6, 0)))],
        }
    }

    #[test]
    fn session_block_then_one_group_with_a_shared_week_reset_and_padded_labels() {
        let want = "5 Hour : 8% / 60%\nResets : 2:00 PM\n\nDaily  : 0% / 25%\nWeekly : 81% / 89%\nFable  : 3% / 89%\nResets : Thu 6:00 AM";
        assert_eq!(tooltip_text(&status(full()), now()), want);
    }

    #[test]
    fn zero_percent_blocks_are_hidden() {
        let mut r = full();
        r.session.as_mut().unwrap().percent = 0.0;
        r.models[0].percent = 0.0;
        assert_eq!(
            tooltip_text(&status(r), now()),
            "Daily  : 0% / 25%\nWeekly : 81% / 89%\nResets : Thu 6:00 AM"
        );
    }

    #[test]
    fn daily_sits_above_weekly_shows_at_zero_and_shares_the_week_reset() {
        let text = tooltip_text(&status(full()), now());
        assert!(text.find("Daily").unwrap() < text.find("Weekly").unwrap());
        // 3 days + 12 h in (Sun 25th 18:00 = week start Thu 22nd 06:00 + 3.5 d), 50% used: on pace.
        let at = local(25, 18, 0);
        let r = Reading {
            session: None,
            week: Some(limit("Week (all models)", 50.0, Some(local(29, 6, 0)))),
            models: vec![],
        };
        assert_eq!(
            tooltip_text(&status(r), at),
            "Daily  : 50% / 50%\nWeekly : 50% / 50%\nResets : Thu 6:00 AM"
        );
        // Zero daily number, week has usage: the Daily block stays.
        assert!(text.contains("Daily  : 0% / 25%"));
        // Over budget shows the raw number: 1 day + 0 h in, 40% used.
        let r = Reading {
            session: None,
            week: Some(limit("Week (all models)", 40.0, Some(local(29, 6, 0)))),
            models: vec![],
        };
        let t = tooltip_text(&status(r), local(23, 6, 0));
        assert!(t.starts_with("Daily  : 180% / 0%\nWeekly"), "{t}");
        // A week with no reset time: value lines only, no Resets line.
        let r = Reading {
            session: None,
            week: Some(limit("Week (all models)", 50.0, None)),
            models: vec![],
        };
        let t = tooltip_text(&status(r), local(25, 18, 0));
        assert!(
            t.starts_with("Daily  : ") && t.contains("\nWeekly : 50% / ") && !t.contains("Resets"),
            "{t}"
        );
        // No week reset: the first per-model week that has one supplies it.
        let r = Reading {
            session: None,
            week: Some(limit("Week (all models)", 50.0, None)),
            models: vec![limit("Week (Fable)", 3.0, Some(local(29, 6, 0)))],
        };
        assert!(tooltip_text(&status(r), now()).ends_with("\nResets : Thu 6:00 AM"));
    }

    #[test]
    fn a_lone_short_label_still_lines_up_with_resets() {
        let r = Reading {
            session: None,
            week: None,
            models: vec![limit("Week (Fable)", 3.0, Some(local(29, 6, 0)))],
        };
        assert_eq!(
            tooltip_text(&status(r), now()),
            "Fable  : 3% / 89%\nResets : Thu 6:00 AM"
        );
    }

    #[test]
    fn all_hidden_says_no_usage_yet() {
        let mut r = full();
        r.session.as_mut().unwrap().percent = 0.0;
        r.week.as_mut().unwrap().percent = 0.0;
        r.models.clear();
        assert_eq!(tooltip_text(&status(r), now()), "No usage yet");
    }

    #[test]
    fn unknown_expected_is_a_dash_and_unknown_reset_drops_its_line() {
        let r = Reading {
            session: Some(limit("Session", 12.0, None)),
            week: None,
            models: vec![],
        };
        assert_eq!(tooltip_text(&status(r), now()), "5 Hour : 12% / \u{2014}");
    }

    #[test]
    fn same_day_reset_shows_only_the_time() {
        assert_eq!(
            reset_clock(local(28, 19, 19), now()).as_deref(),
            Some("7:19 PM")
        );
        assert_eq!(
            reset_clock(local(29, 6, 0), now()).as_deref(),
            Some("Thu 6:00 AM")
        );
    }

    #[test]
    fn stale_adds_a_trailer_after_a_blank_line_without_error_words() {
        let mut s = status(full());
        s.error = Some("claude /usage failed: boom".into());
        let text = tooltip_text(&s, now());
        assert!(
            text.ends_with("Resets : Thu 6:00 AM\n\nStale - last read 11:55 AM"),
            "{text}"
        );
        assert!(!text.contains("boom"));
    }

    #[test]
    fn lines_expose_rows_resets_and_blanks() {
        let l = lines(Some(&full()), None, false, now());
        assert_eq!(l.len(), 7);
        assert_eq!(
            l[0],
            Line::Row {
                label: "5 Hour".into(),
                used: "8%".into(),
                expected: "60%".into()
            }
        );
        assert_eq!(l[1], Line::Reset("2:00 PM".into()));
        assert_eq!(l[2], Line::Blank);
        assert_eq!(l[6], Line::Reset("Thu 6:00 AM".into()));
        assert_eq!(label_width(&l), 6);
    }

    #[test]
    fn before_the_first_reading() {
        assert_eq!(tooltip_text(&Status::default(), 0), "Reading usage\u{2026}");
        let s = Status {
            error: Some("timed out".into()),
            ..Status::default()
        };
        assert_eq!(tooltip_text(&s, 0), "Reading usage\u{2026}");
    }
}
