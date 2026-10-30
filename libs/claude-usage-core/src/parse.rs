// SPDX-License-Identifier: MIT
//! Parser for the `result` text of `claude -p /usage` (port of an earlier TypeScript implementation (`parse.ts`)).

use std::sync::LazyLock;

use chrono::{Datelike, Local, NaiveDate, TimeZone, Utc};
use chrono_tz::Tz;
use regex::Regex;

use crate::reading::{Limit, Reading};

const MONTHS: [&str; 12] = [
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
];
const DAY_MS: i64 = 86_400_000;

static LIMIT_LINE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"(?i)^Current (session|week \(([^)]+)\)):\s*(\d+(?:\.\d+)?)%\s*used(?:\s*·\s*resets\s+(.+?))?\s*$",
    )
    .expect("LIMIT_LINE")
});
static RESET_TEXT: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)^([A-Za-z]{3})[a-z]* (\d{1,2}),? (\d{1,2})(?::(\d{2}))?\s*(am|pm)(?:\s*\(([^)]+)\))?$")
        .expect("RESET_TEXT")
});
static RATE_LIMITED: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)rate.?limited").expect("RATE_LIMITED"));

/// "Oct 22, 6am (America/Los_Angeles)" as ms UTC. The text has no year: the nearest such date that
/// is not more than a day past is taken. None when the text is not that shape or the zone is unknown.
pub fn parse_reset_time(text: &str, now_ms: i64) -> Option<i64> {
    let m = RESET_TEXT.captures(text.trim())?;
    let month = MONTHS.iter().position(|x| *x == m[1].to_lowercase())? as u32 + 1;
    let day: u32 = m[2].parse().ok()?;
    let pm = m[5].eq_ignore_ascii_case("pm");
    let hour = m[3].parse::<u32>().ok()? % 12 + if pm { 12 } else { 0 };
    let minute: u32 = m.get(4).map_or(Some(0), |x| x.as_str().parse().ok())?;
    let zone: Option<Tz> = match m.get(6) {
        Some(z) => Some(z.as_str().parse().ok()?),
        None => None,
    };
    let year = Utc.timestamp_millis_opt(now_ms).single()?.year();
    let at = |y: i32| -> Option<i64> {
        let wall = NaiveDate::from_ymd_opt(y, month, day)?.and_hms_opt(hour, minute, 0)?;
        let dt = match zone {
            Some(tz) => tz.from_local_datetime(&wall).earliest()?.timestamp_millis(),
            None => Local
                .from_local_datetime(&wall)
                .earliest()?
                .timestamp_millis(),
        };
        Some(dt)
    };
    let first = at(year)?;
    if first < now_ms - DAY_MS {
        at(year + 1)
    } else {
        Some(first)
    }
}

/// The limit lines of `/usage` output. None when no session or week line is found.
pub fn parse_usage(text: &str, now_ms: i64) -> Option<Reading> {
    let mut reading = Reading::default();
    for line in text.split('\n') {
        let Some(m) = LIMIT_LINE.captures(line.trim()) else {
            continue;
        };
        let resets_text = m.get(4).map(|x| x.as_str().trim().to_owned());
        let is_session = m[1].eq_ignore_ascii_case("session");
        let label = if is_session {
            "Session".to_owned()
        } else {
            format!("Week ({})", &m[2])
        };
        let limit = Limit {
            percent: m[3].parse().ok()?,
            resets_at: resets_text
                .as_deref()
                .and_then(|t| parse_reset_time(t, now_ms)),
            resets_text,
            label,
        };
        if is_session {
            reading.session = Some(limit);
        } else if m[2].eq_ignore_ascii_case("all models") {
            reading.week = Some(Limit {
                label: "Week (all models)".to_owned(),
                ..limit
            });
        } else {
            reading.models.push(limit);
        }
    }
    (reading.session.is_some() || reading.week.is_some()).then_some(reading)
}

/// A line of `/usage` output saying the figures are withheld ("rate limited until ..."), trimmed.
pub fn rate_limited_line(text: &str) -> Option<String> {
    text.split('\n')
        .find(|l| RATE_LIMITED.is_match(l))
        .map(|l| l.trim().to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    const FIXTURE: &str = include_str!("../tests/fixtures/usage-output.txt");
    /// Oct 20 2026, 13:00 in Los Angeles.
    fn now() -> i64 {
        Utc.with_ymd_and_hms(2026, 10, 20, 20, 0, 0)
            .unwrap()
            .timestamp_millis()
    }
    fn utc(y: i32, mo: u32, d: u32, h: u32, mi: u32) -> i64 {
        Utc.with_ymd_and_hms(y, mo, d, h, mi, 0)
            .unwrap()
            .timestamp_millis()
    }

    #[test]
    fn reads_session_week_and_models_from_captured_output() {
        let r = parse_usage(FIXTURE, now()).unwrap();
        let s = r.session.unwrap();
        assert_eq!((s.label.as_str(), s.percent), ("Session", 65.0));
        assert_eq!(
            s.resets_text.as_deref(),
            Some("Oct 20, 7:19pm (America/Los_Angeles)")
        );
        let w = r.week.unwrap();
        assert_eq!((w.label.as_str(), w.percent), ("Week (all models)", 75.0));
        assert_eq!(r.models.len(), 1);
        assert_eq!(
            (r.models[0].label.as_str(), r.models[0].percent),
            ("Week (Fable)", 0.0)
        );
    }

    #[test]
    fn places_reset_time_in_its_own_zone() {
        let r = parse_usage(FIXTURE, now()).unwrap();
        assert_eq!(r.session.unwrap().resets_at, Some(utc(2026, 10, 21, 2, 19)));
        assert_eq!(r.week.unwrap().resets_at, Some(utc(2026, 10, 22, 12, 59)));
    }

    #[test]
    fn accepts_missing_minutes_or_zone_and_rolls_past_dates_to_next_year() {
        let n = now();
        assert_eq!(
            parse_reset_time("Oct 22, 6am (America/Los_Angeles)", n),
            Some(utc(2026, 10, 22, 13, 0))
        );
        assert_eq!(
            parse_reset_time("Jan 3, 12pm (UTC)", n),
            Some(utc(2027, 1, 3, 12, 0))
        );
        assert_eq!(parse_reset_time("Oct 22, 6am (Not/AZone)", n), None);
        assert_eq!(parse_reset_time("soon", n), None);
    }

    #[test]
    fn a_date_less_than_a_day_past_stays_in_this_year() {
        // Oct 20 6am LA is 14:00 UTC, 6 h before now: still this year.
        assert_eq!(
            parse_reset_time("Oct 20, 6am (America/Los_Angeles)", now()),
            Some(utc(2026, 10, 20, 13, 0))
        );
    }

    #[test]
    fn fractional_percent_and_line_without_reset() {
        let r = parse_usage("Current session: 12.5% used\n", now()).unwrap();
        let s = r.session.unwrap();
        assert_eq!((s.percent, s.resets_text, s.resets_at), (12.5, None, None));
    }

    #[test]
    fn none_without_limit_lines() {
        assert!(parse_usage("Something else entirely", now()).is_none());
    }

    #[test]
    fn finds_a_rate_limit_line() {
        assert_eq!(
            rate_limited_line("x\nUsage is rate limited until 3:05pm\n").as_deref(),
            Some("Usage is rate limited until 3:05pm")
        );
        assert_eq!(rate_limited_line(FIXTURE), None);
    }
}
