// SPDX-License-Identifier: MIT
//! Pace math (port of an earlier TypeScript implementation (`pace.ts`)): how much of a window is gone, and the sentence for it.

use chrono::{Datelike, Duration, Local, NaiveDate, TimeZone};

/// Which window a limit covers: the 5-hour session or the all-models week.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Session,
    Week,
}

const HOUR_MS: i64 = 3_600_000;

impl Kind {
    fn window_ms(self) -> i64 {
        match self {
            Kind::Session => 5 * HOUR_MS,
            Kind::Week => 7 * 24 * HOUR_MS,
        }
    }
}

fn local_six(date: NaiveDate) -> Option<i64> {
    let wall = date.and_hms_opt(6, 0, 0)?;
    Some(
        Local
            .from_local_datetime(&wall)
            .earliest()?
            .timestamp_millis(),
    )
}

/// The most recent Thursday 06:00 local time at or before `now_ms`: where a week starts when Claude
/// gives no reset time. The week is a fixed 168 h, so across a DST change this is off by under 1%.
pub fn last_thursday_six(now_ms: i64) -> Option<i64> {
    let today = Local.timestamp_millis_opt(now_ms).single()?.date_naive();
    let back = (today.weekday().num_days_from_sunday() as i64 - 4 + 7) % 7;
    let mut date = today - Duration::days(back);
    let mut at = local_six(date)?;
    if at > now_ms {
        date -= Duration::days(7);
        at = local_six(date)?;
    }
    Some(at)
}

/// The share of the window already gone, 0-100; None when it cannot be told (a session with no reset time).
pub fn elapsed_percent(kind: Kind, resets_at: Option<i64>, now_ms: i64) -> Option<f64> {
    let length = kind.window_ms();
    let start = match (resets_at, kind) {
        (Some(r), _) => r - length,
        (None, Kind::Week) => last_thursday_six(now_ms)?,
        (None, Kind::Session) => return None,
    };
    Some((((now_ms - start) as f64 / length as f64) * 100.0).clamp(0.0, 100.0))
}

const DAY_MS: i64 = 24 * HOUR_MS;

/// Percentage points of the week allowance per 24 h part (the week cut into 7).
pub const DAY_SHARE: f64 = 100.0 / 7.0;

/// The current 24 h part of the week window.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct DayPart {
    /// 0-6, clamped so an overrun window cannot index past the last part.
    pub index: i64,
    pub start_ms: i64,
    /// Share of this part already gone, 0-100.
    pub elapsed_percent: f64,
}

/// The week's current day part; None when the week start cannot be told.
pub fn day_part(week_resets_at: Option<i64>, now_ms: i64) -> Option<DayPart> {
    let week_start = match week_resets_at {
        Some(r) => r - Kind::Week.window_ms(),
        None => last_thursday_six(now_ms)?,
    };
    let index = ((now_ms - week_start).div_euclid(DAY_MS)).clamp(0, 6);
    let start_ms = week_start + index * DAY_MS;
    let elapsed_percent = ((now_ms - start_ms) as f64 / DAY_MS as f64 * 100.0).clamp(0.0, 100.0);
    Some(DayPart {
        index,
        start_ms,
        elapsed_percent,
    })
}

/// Raw share of day part `index`'s allowance used, in percent, floored at 0 (under the linear budget).
/// Overage carries forward: a heavy day stays above 100 until the linear budget catches up.
pub fn daily_percent(week_percent: f64, index: i64) -> f64 {
    ((week_percent - index as f64 * DAY_SHARE) / DAY_SHARE * 100.0).max(0.0)
}

/// Round half up, like JS `Math.round`.
pub fn round(x: f64) -> i64 {
    (x + 0.5).floor() as i64
}

#[cfg(test)]
mod tests {
    use super::*;

    const HOUR: i64 = HOUR_MS;
    const DAY: i64 = 24 * HOUR_MS;
    /// A Wednesday, local.
    fn now() -> i64 {
        Local
            .with_ymd_and_hms(2026, 10, 28, 12, 0, 0)
            .unwrap()
            .timestamp_millis()
    }
    fn local(d: u32, h: u32, mi: u32) -> i64 {
        Local
            .with_ymd_and_hms(2026, 10, d, h, mi, 0)
            .unwrap()
            .timestamp_millis()
    }

    #[test]
    fn session_is_0_at_start_50_in_the_middle_100_at_the_end() {
        let n = now();
        assert_eq!(
            elapsed_percent(Kind::Session, Some(n + 5 * HOUR), n),
            Some(0.0)
        );
        assert_eq!(
            elapsed_percent(Kind::Session, Some(n + 5 * HOUR / 2), n),
            Some(50.0)
        );
        assert_eq!(elapsed_percent(Kind::Session, Some(n), n), Some(100.0));
    }

    #[test]
    fn clamps_before_the_start_and_after_the_end() {
        let n = now();
        assert_eq!(elapsed_percent(Kind::Week, Some(n + 8 * DAY), n), Some(0.0));
        assert_eq!(elapsed_percent(Kind::Week, Some(n - HOUR), n), Some(100.0));
    }

    #[test]
    fn jumps_back_to_the_start_when_a_new_reading_moves_the_reset_forward() {
        let n = now();
        assert_eq!(
            elapsed_percent(Kind::Session, Some(n - HOUR), n),
            Some(100.0)
        );
        assert_eq!(
            elapsed_percent(Kind::Session, Some(n + 5 * HOUR), n),
            Some(0.0)
        );
    }

    #[test]
    fn no_session_marker_without_reset_and_week_from_last_thursday_six() {
        let n = now();
        assert_eq!(elapsed_percent(Kind::Session, None, n), None);
        let start = local(22, 6, 0);
        let want = (n - start) as f64 / (7 * DAY) as f64 * 100.0;
        assert!((elapsed_percent(Kind::Week, None, n).unwrap() - want).abs() < 1e-9);
    }

    #[test]
    fn last_thursday_six_is_today_after_six_a_week_earlier_before_six() {
        assert_eq!(last_thursday_six(local(29, 7, 0)), Some(local(29, 6, 0)));
        assert_eq!(last_thursday_six(local(29, 5, 59)), Some(local(22, 6, 0)));
    }

    #[test]
    fn day_part_marker_and_daily_follow_the_worked_examples() {
        let n = now();
        // 3 days + 12 h into the week.
        let reset = n - 3 * DAY - 12 * HOUR + 7 * DAY;
        let d = day_part(Some(reset), n).unwrap();
        assert_eq!(d.index, 3);
        assert_eq!(d.start_ms, n - 12 * HOUR);
        assert_eq!(d.elapsed_percent, 50.0);
        assert_eq!(daily_percent(12.0, d.index), 0.0);
        assert!((daily_percent(50.0, d.index) - 50.0).abs() < 1e-9);
        // 1 day + 0 h in, 40% used.
        let d = day_part(Some(n - DAY + 7 * DAY), n).unwrap();
        assert_eq!((d.index, d.elapsed_percent), (1, 0.0));
        assert!((daily_percent(40.0, d.index) - 180.0).abs() < 1e-9);
    }

    #[test]
    fn day_index_clamps_before_the_week_and_past_its_end() {
        let n = now();
        // Clock skew: the week has not started yet.
        let d = day_part(Some(n + 8 * DAY), n).unwrap();
        assert_eq!((d.index, d.start_ms, d.elapsed_percent), (0, n + DAY, 0.0));
        // Overrun: 9 days in sticks to the last part, marker pegged at 100.
        let d = day_part(Some(n - 2 * DAY), n).unwrap();
        assert_eq!((d.index, d.elapsed_percent), (6, 100.0));
    }

    #[test]
    fn day_part_without_a_reset_starts_at_last_thursday_six() {
        let n = now();
        let d = day_part(None, n).unwrap();
        // Thu 22nd 06:00 to Wed 28th 12:00 = 6 days + 6 h.
        assert_eq!(d.index, 6);
        assert_eq!(d.start_ms, local(28, 6, 0));
        assert_eq!(d.elapsed_percent, 25.0);
    }
}
