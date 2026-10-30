// SPDX-License-Identifier: MIT
//! Short words for the stale line and the reset countdown.

use chrono::{Local, TimeZone};

/// "Stale - last read 2:14 PM" in local time.
pub fn stale_words(read_at_ms: i64) -> String {
    match Local.timestamp_millis_opt(read_at_ms).single() {
        Some(t) => format!("Stale - last read {}", t.format("%-I:%M %p")),
        None => "Stale".to_owned(),
    }
}

/// The reset in a few characters: "35m", "12h 33m", "3d 4h"; empty when unknown (port of an earlier TypeScript implementation (`resetShort`)).
pub fn reset_short(resets_at_ms: Option<i64>, now_ms: i64) -> String {
    let Some(at) = resets_at_ms else {
        return String::new();
    };
    let mins = ((at - now_ms) as f64 / 60_000.0).round().max(0.0) as i64;
    if mins < 60 {
        format!("{mins}m")
    } else if mins < 24 * 60 {
        format!("{}h {}m", mins / 60, mins % 60)
    } else {
        format!("{}d {}h", mins / 1440, mins % 1440 / 60)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stale_words_name_the_read_time() {
        let t = Local
            .with_ymd_and_hms(2026, 10, 27, 14, 14, 0)
            .unwrap()
            .timestamp_millis();
        assert_eq!(stale_words(t), "Stale - last read 2:14 PM");
    }

    #[test]
    fn reset_short_scales_from_minutes_to_days() {
        let now = 1_000_000;
        let at = |mins: i64| Some(now + mins * 60_000);
        assert_eq!(reset_short(at(35), now), "35m");
        assert_eq!(reset_short(at(12 * 60 + 33), now), "12h 33m");
        assert_eq!(reset_short(at(3 * 1440 + 4 * 60 + 20), now), "3d 4h");
        assert_eq!(reset_short(at(1440), now), "1d 0h");
        assert_eq!(reset_short(at(-5), now), "0m");
    }

    #[test]
    fn reset_short_is_empty_when_unknown() {
        assert_eq!(reset_short(None, 1_000_000), "");
    }
}
