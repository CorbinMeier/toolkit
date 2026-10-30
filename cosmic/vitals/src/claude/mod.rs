// SPDX-License-Identifier: MIT
//! The Claude usage row's data (spec §3): the applet's feed file first, the usage server as the second source.
//! `merge` is pure; [`ClaudeSource`] does the polling; [`ClaudeView`] is what the UI reads, with every
//! string the row needs prepared once per data change or minute so drawing allocates nothing.

pub mod server;

use std::path::PathBuf;
use std::time::{Duration, Instant, SystemTime};

use claude_usage_core::feed::{self, Feed};
use claude_usage_core::pace::{self, DayPart, Kind};
use claude_usage_core::reading::Reading;
use claude_usage_core::readout::{self, Line};
use claude_usage_core::words;

use server::{ServerStatus, ServerThread};

/// How often the feed file's mtime is checked (spec §3).
pub const FEED_CHECK: Duration = Duration::from_secs(5);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Source {
    Feed,
    Server,
}

impl Source {
    pub fn name(self) -> &'static str {
        match self {
            Self::Feed => "feed",
            Self::Server => "server",
        }
    }
}

/// The reading the row shows and where it came from.
#[derive(Debug, Clone, PartialEq)]
pub struct Merged {
    pub reading: Reading,
    pub source: Source,
    /// When the chosen source took the reading, ms.
    pub read_at_ms: i64,
    pub stale: bool,
}

/// Session and week come from whichever source is newest (a limit the newest lacks comes from the other);
/// the per-model weeks and reset texts always come from the usage server.
pub fn merge(feed: Option<&Feed>, server: Option<&ServerStatus>, now_secs: i64) -> Option<Merged> {
    let server_reading = server.and_then(|h| h.reading.as_ref().map(|r| (r, h)));
    let feed_ms = feed.map(|f| f.written_at * 1000);
    let server_ms = server_reading.map(|(_, h)| h.read_at.unwrap_or(0));
    let use_feed = match (feed_ms, server_ms) {
        (Some(f), Some(h)) => f > h,
        (Some(_), None) => true,
        _ => false,
    };
    if use_feed {
        let f = feed?;
        let mut reading = feed::to_reading(f);
        if f.five_hour.is_none() {
            reading.session = None;
        }
        if f.seven_day.is_none() {
            reading.week = None;
        }
        if let Some((hr, _)) = server_reading {
            let text = |l: &Option<claude_usage_core::reading::Limit>| {
                l.as_ref().and_then(|l| l.resets_text.clone())
            };
            if let Some(s) = &mut reading.session {
                s.resets_text = text(&hr.session);
            }
            if let Some(w) = &mut reading.week {
                w.resets_text = text(&hr.week);
            }
            reading.models = hr.models.clone();
        }
        return Some(Merged {
            reading,
            source: Source::Feed,
            read_at_ms: f.written_at * 1000,
            stale: !feed::is_fresh(f, now_secs),
        });
    }
    let (hr, h) = server_reading?;
    let mut reading = hr.clone();
    if let Some(f) = feed {
        let fr = feed::to_reading(f);
        if reading.session.is_none() && f.five_hour.is_some() {
            reading.session = fr.session;
        }
        if reading.week.is_none() && f.seven_day.is_some() {
            reading.week = fr.week;
        }
    }
    Some(Merged {
        reading,
        source: Source::Server,
        read_at_ms: h.read_at.unwrap_or(0),
        stale: h.stale,
    })
}

/// One bar row of the Claude section: a line of the applet readout (`5 Hour`, `Daily`, `Weekly`, a model).
#[derive(Debug, Clone, PartialEq)]
pub struct ClaudeRow {
    pub label: String,
    /// Used percent, for the bar.
    pub used: f32,
    /// Expected percent (the pace marker), when known.
    pub expected: Option<f32>,
    /// `4% / 12%`, as the applet shows it.
    pub text: String,
    /// `2:00 PM (4h 7m)` on the 5 Hour and Weekly rows, else empty.
    pub reset: String,
    /// A model week with no usage (rounds to 0%): the bars skip it, the readouts keep it (v8.5).
    pub hidden: bool,
}

/// Widest `used / expected` text of a bar row (`100% / 100%`; a percent is at most three digits), in cells.
/// The Claude bar rows reserve exactly this, never the current rows' width (spec §4, bars).
pub const USED_EXPECTED_W: u16 = 11;
/// Widest reset text of a bar row (`Thu 12:00 PM (23h 59m)`, `Thu 12:00 PM (59d 23h)`), in cells.
pub const RESET_W: u16 = 22;

/// Local reset time: "7:19 PM" on the same calendar day as now, otherwise "Thu 6:00 AM"; empty if unknown.
fn reset_clock(resets_at: Option<i64>, now_ms: i64) -> String {
    use chrono::TimeZone;
    let (Some(r), Some(now)) = (
        resets_at.and_then(|r| chrono::Local.timestamp_millis_opt(r).single()),
        chrono::Local.timestamp_millis_opt(now_ms).single(),
    ) else {
        return String::new();
    };
    let fmt = if r.date_naive() == now.date_naive() {
        "%-I:%M %p"
    } else {
        "%a %-I:%M %p"
    };
    r.format(fmt).to_string()
}

fn percent_of(t: &str) -> Option<f32> {
    t.trim_end_matches('%').parse().ok()
}

/// The readout lines as bar rows. A `Resets` line belongs to the block just above it: the 5 Hour row, or
/// the Daily/Weekly/model group (whose reset is the week's).
fn readout_rows(lines: &[Line], session_short: &str, week_short: &str) -> Vec<ClaudeRow> {
    let mut rows: Vec<ClaudeRow> = Vec::new();
    let mut block_start = 0;
    for l in lines {
        match l {
            Line::Row {
                label,
                used,
                expected,
            } => rows.push(ClaudeRow {
                label: label.clone(),
                used: percent_of(used).unwrap_or(0.0),
                hidden: !matches!(label.as_str(), "5 Hour" | "Daily" | "Weekly")
                    && percent_of(used).unwrap_or(0.0) < 0.5,
                expected: percent_of(expected),
                text: format!("{used} / {expected}"),
                reset: String::new(),
            }),
            Line::Reset(clock) => {
                for r in &mut rows[block_start..] {
                    let short = match r.label.as_str() {
                        "5 Hour" => session_short,
                        "Weekly" => week_short,
                        _ => continue,
                    };
                    r.reset = if short.is_empty() {
                        clock.clone()
                    } else {
                        format!("{clock} ({short})")
                    };
                }
                block_start = rows.len();
            }
            Line::Blank | Line::Note(_) => block_start = rows.len(),
        }
    }
    rows
}

/// The Claude row's data plus its prepared strings and pace figures.
#[derive(Default)]
pub struct ClaudeView {
    feed: Option<Feed>,
    server: Option<ServerStatus>,
    /// The last usage server fetch failed with this.
    pub server_error: Option<String>,
    pub merged: Option<Merged>,
    /// `35m`, `12h 33m`, `3d 4h`; empty when unknown.
    pub session_reset: String,
    pub week_reset: String,
    /// `Stale - last read 2:14 PM`, empty when fresh.
    pub stale_text: String,
    /// Local reset times: `7:19 PM` today, `Thu 6:00 AM` later; empty when unknown.
    pub session_clock: String,
    pub week_clock: String,
    /// The readout rows the section's bars draw, rebuilt with the strings above.
    pub rows: Vec<ClaudeRow>,
    /// Share of the window already gone, 0 to 100.
    pub session_pace: Option<f64>,
    pub week_pace: Option<f64>,
    pub day: Option<DayPart>,
    /// The share of today's slice of the week that is used.
    pub daily_percent: Option<f64>,
    dirty: bool,
    last_minute: i64,
}

impl ClaudeView {
    pub fn set_feed(&mut self, feed: Option<Feed>) {
        if self.feed != feed {
            self.feed = feed;
            self.dirty = true;
        }
    }

    pub fn set_server(&mut self, result: Result<ServerStatus, String>) {
        match result {
            Ok(h) => {
                self.server_error = h.error.clone();
                self.server = Some(h);
            }
            Err(e) => self.server_error = Some(e),
        }
        self.dirty = true;
    }

    pub fn feed(&self) -> Option<&Feed> {
        self.feed.as_ref()
    }

    /// Recomputes the reading and its strings when data changed or the minute turned. True if it did.
    pub fn refresh(&mut self, now_ms: i64) -> bool {
        let minute = now_ms.div_euclid(60_000);
        if !self.dirty && minute == self.last_minute {
            return false;
        }
        self.dirty = false;
        self.last_minute = minute;
        self.merged = merge(self.feed.as_ref(), self.server.as_ref(), now_ms / 1000);
        self.session_reset.clear();
        self.week_reset.clear();
        self.stale_text.clear();
        self.rows.clear();
        self.session_clock.clear();
        self.week_clock.clear();
        self.session_pace = None;
        self.week_pace = None;
        self.day = None;
        self.daily_percent = None;
        if let Some(m) = &self.merged {
            let r = &m.reading;
            if let Some(s) = &r.session {
                self.session_reset = words::reset_short(s.resets_at, now_ms);
                self.session_clock = reset_clock(s.resets_at, now_ms);
                self.session_pace = pace::elapsed_percent(Kind::Session, s.resets_at, now_ms);
            }
            if let Some(w) = &r.week {
                self.week_reset = words::reset_short(w.resets_at, now_ms);
                self.week_clock = reset_clock(w.resets_at, now_ms);
                self.week_pace = pace::elapsed_percent(Kind::Week, w.resets_at, now_ms);
                self.day = pace::day_part(w.resets_at, now_ms);
                self.daily_percent = self.day.map(|d| pace::daily_percent(w.percent, d.index));
            }
            if m.stale {
                self.stale_text = words::stale_words(m.read_at_ms);
            }
            self.rows = readout_rows(
                &readout::lines(Some(r), Some(m.read_at_ms), m.stale, now_ms),
                &self.session_reset,
                &self.week_reset,
            );
        }
        true
    }
}

/// Polls the two sources for a [`ClaudeView`]: the feed file's mtime every 5 s, and the usage server thread's
/// channel. Never spawns `claude`.
pub struct ClaudeSource {
    feed_path: Option<PathBuf>,
    next_check: Option<Instant>,
    mtime: Option<SystemTime>,
    server: Option<ServerThread>,
}

impl ClaudeSource {
    /// `feed_dir` is the applet's feed folder, `usage_url` the usage server (`None` skips the usage server, e.g. in tests).
    pub fn new(feed_dir: Option<PathBuf>, usage_url: Option<String>) -> Self {
        ClaudeSource {
            feed_path: feed_dir.map(|d| d.join(feed::FILE_NAME)),
            next_check: None,
            mtime: None,
            server: usage_url.and_then(|u| ServerThread::spawn(u).ok()),
        }
    }

    /// Applies whatever is new. True if the view changed.
    pub fn poll(&mut self, now: Instant, view: &mut ClaudeView) -> bool {
        let mut changed = false;
        if self.next_check.is_none_or(|t| now >= t) {
            self.next_check = Some(now + FEED_CHECK);
            if let Some(path) = &self.feed_path {
                let mtime = std::fs::metadata(path).and_then(|m| m.modified()).ok();
                if mtime != self.mtime {
                    self.mtime = mtime;
                    let feed = path
                        .parent()
                        .filter(|_| mtime.is_some())
                        .and_then(feed::read_from);
                    view.set_feed(feed);
                    changed = true;
                }
            }
        }
        if let Some(h) = &self.server {
            while let Ok(res) = h.rx.try_recv() {
                view.set_server(res);
                changed = true;
            }
        }
        changed
    }

    pub fn shutdown(self) {
        if let Some(h) = self.server {
            h.shutdown();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use claude_usage_core::feed::Window;
    use claude_usage_core::reading::Limit;

    #[test]
    fn row_texts_never_exceed_the_reserved_column_widths() {
        let now = 1_793_188_800_000i64;
        let row = |used: &str, expected: &str| {
            let lines = [Line::Row {
                label: "5 Hour".into(),
                used: used.into(),
                expected: expected.into(),
            }];
            readout_rows(&lines, "", "").remove(0).text
        };
        for (u, e) in [
            ("0%", "0%"),
            ("100%", "100%"),
            ("999%", "999%"),
            ("—", "—"),
            ("4%", "100%"),
        ] {
            assert!(
                row(u, e).chars().count() <= usize::from(USED_EXPECTED_W),
                "{u} {e}"
            );
        }
        assert_eq!(
            row("100%", "100%").chars().count(),
            usize::from(USED_EXPECTED_W)
        );
        // Every offset from now to 60 days ahead, so every weekday and minute of day shows up.
        let mut m = 0i64;
        while m <= 60 * 1440 {
            let at = Some(now + m * 60_000);
            let (clock, short) = (reset_clock(at, now), words::reset_short(at, now));
            let text = format!("{clock} ({short})");
            assert!(text.chars().count() <= usize::from(RESET_W), "{text}");
            m += 7;
        }
    }

    fn lim(label: &str, percent: f64, resets_at: Option<i64>, text: Option<&str>) -> Limit {
        Limit {
            label: label.into(),
            percent,
            resets_text: text.map(Into::into),
            resets_at,
        }
    }

    fn server(read_at: i64, stale: bool, session: f64, week: Option<f64>) -> ServerStatus {
        ServerStatus {
            reading: Some(Reading {
                session: Some(lim(
                    "Session",
                    session,
                    Some(9_000_000),
                    Some("Oct 28, 6pm"),
                )),
                week: week.map(|w| {
                    lim(
                        "Week (all models)",
                        w,
                        Some(99_000_000),
                        Some("Oct 29, 6am"),
                    )
                }),
                models: vec![lim("Week (Fable)", 0.0, None, None)],
            }),
            read_at: Some(read_at),
            stale,
            error: None,
        }
    }

    fn feed_at(written_at: i64, five: f64, seven: Option<f64>) -> Feed {
        Feed {
            written_at,
            five_hour: Some(Window {
                used_percentage: five,
                resets_at: 9_100,
            }),
            seven_day: seven.map(|s| Window {
                used_percentage: s,
                resets_at: 99_100,
            }),
        }
    }

    #[test]
    fn the_newest_source_wins_and_models_come_from_the_server() {
        let h = server(1_000_000, false, 11.0, Some(98.0));
        let f = feed_at(2_000, 20.0, Some(50.0));
        let m = merge(Some(&f), Some(&h), 2_100).unwrap();
        assert_eq!(m.source, Source::Feed);
        assert_eq!(m.reading.session.as_ref().unwrap().percent, 20.0);
        assert_eq!(
            m.reading.session.as_ref().unwrap().resets_text.as_deref(),
            Some("Oct 28, 6pm")
        );
        assert_eq!(m.reading.models.len(), 1);
        assert!(!m.stale);
        let f_old = feed_at(500, 20.0, Some(50.0));
        let m = merge(Some(&f_old), Some(&h), 600).unwrap();
        assert_eq!(m.source, Source::Server);
        assert_eq!(m.reading.week.as_ref().unwrap().percent, 98.0);
    }

    #[test]
    fn a_missing_week_falls_back_to_the_other_source() {
        let h = server(1_000_000, false, 11.0, None);
        let f = feed_at(500, 20.0, Some(50.0));
        let m = merge(Some(&f), Some(&h), 600).unwrap();
        assert_eq!(m.source, Source::Server);
        assert_eq!(m.reading.week.as_ref().unwrap().percent, 50.0);
        let m = merge(None, Some(&h), 600).unwrap();
        assert!(m.reading.week.is_none());
    }

    #[test]
    fn a_feed_with_one_window_shows_no_phantom_session() {
        let f = Feed {
            written_at: 10,
            five_hour: None,
            seven_day: Some(Window {
                used_percentage: 8.0,
                resets_at: 99,
            }),
        };
        let m = merge(Some(&f), None, 10).unwrap();
        assert!(m.reading.session.is_none());
        assert_eq!(m.reading.week.unwrap().percent, 8.0);
    }

    #[test]
    fn staleness_follows_the_source() {
        let f = feed_at(1_000, 1.0, None);
        assert!(!merge(Some(&f), None, 1_000 + 900).unwrap().stale);
        assert!(merge(Some(&f), None, 1_000 + 901).unwrap().stale);
        let h = server(5, true, 1.0, None);
        assert!(merge(None, Some(&h), 6).unwrap().stale);
        assert!(merge(None, None, 6).is_none());
    }

    #[test]
    fn the_view_prepares_its_strings_once_per_minute() {
        let mut v = ClaudeView::default();
        let now = 8_000_000;
        v.set_server(Ok(server(now - 60_000, true, 11.0, Some(98.0))));
        assert!(v.refresh(now));
        assert_eq!(v.session_reset, "17m");
        assert!(v.stale_text.starts_with("Stale - last read"));
        assert!(v.session_pace.is_some());
        assert!(!v.refresh(now + 1_000));
        assert!(v.refresh(now + 61_000));
        v.set_server(Err("down".into()));
        assert_eq!(v.server_error.as_deref(), Some("down"));
    }
}
