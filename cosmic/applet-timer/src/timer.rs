// SPDX-License-Identifier: MIT

use std::time::{Duration, Instant};

/// Timer state. Remaining time is always derived from the deadline.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum State {
    Idle,
    /// Countdown reached zero; alarm sounds until the user dismisses it.
    Ringing,
    Running {
        deadline: Instant,
    },
}

/// Whole seconds left, rounded up so a fresh 5:00 timer reads 5:00.
pub fn remaining_secs(deadline: Instant, now: Instant) -> u64 {
    let rem = deadline.saturating_duration_since(now);
    rem.as_secs() + u64::from(rem.subsec_nanos() > 0)
}

pub fn is_finished(deadline: Instant, now: Instant) -> bool {
    now >= deadline
}

/// `M:SS` under an hour, `H:MM:SS` at an hour or more.
pub fn format_secs(total: u64) -> String {
    let (h, m, s) = (total / 3600, (total % 3600) / 60, total % 60);
    if h > 0 {
        format!("{h}:{m:02}:{s:02}")
    } else {
        format!("{m}:{s:02}")
    }
}

pub fn to_secs(h: u32, m: u32, s: u32) -> u64 {
    u64::from(h) * 3600 + u64::from(m) * 60 + u64::from(s)
}

pub fn duration(h: u32, m: u32, s: u32) -> Duration {
    Duration::from_secs(to_secs(h, m, s))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn formats() {
        assert_eq!(format_secs(300), "5:00");
        assert_eq!(format_secs(59), "0:59");
        assert_eq!(format_secs(3599), "59:59");
        assert_eq!(format_secs(3600), "1:00:00");
        assert_eq!(format_secs(3661), "1:01:01");
    }

    #[test]
    fn remaining_rounds_up() {
        let now = Instant::now();
        let d = now + Duration::from_secs(300);
        assert_eq!(remaining_secs(d, now), 300);
        assert_eq!(remaining_secs(d, now + Duration::from_millis(1)), 300);
        assert_eq!(remaining_secs(d, now + Duration::from_millis(1001)), 299);
        assert_eq!(remaining_secs(d, d), 0);
        assert!(is_finished(d, d));
        assert!(!is_finished(d, now));
    }
}
