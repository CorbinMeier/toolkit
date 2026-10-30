// SPDX-License-Identifier: MIT
//! The status-line feed. Claude Code pipes JSON (with `rate_limits` for subscribers) to its statusLine
//! command after every reply; the applet's `--statusline` mode stores the limits in a small
//! file, and the applet reads that file instead of running the heavy `claude /usage`.

use std::io::{Read, Write};
use std::path::{Path, PathBuf};

use serde_json::{Value, json};

use crate::reading::{Limit, Reading};

/// A feed older than this is stale and the applet falls back to `claude /usage`.
pub const FRESH_SECS: i64 = 15 * 60;

/// One rate-limit window as Claude Code reports it.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Window {
    pub used_percentage: f64,
    /// Unix epoch seconds.
    pub resets_at: i64,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Feed {
    /// Unix epoch seconds.
    pub written_at: i64,
    pub five_hour: Option<Window>,
    pub seven_day: Option<Window>,
}

fn window(v: &Value) -> Option<Window> {
    Some(Window {
        used_percentage: v.get("used_percentage")?.as_f64()?,
        resets_at: v.get("resets_at")?.as_f64()? as i64,
    })
}

/// The feed for a statusLine stdin payload; None when `rate_limits` has neither window.
pub fn from_stdin(stdin: &str, now_secs: i64) -> Option<Feed> {
    let v: Value = serde_json::from_str(stdin).ok()?;
    let limits = v.get("rate_limits")?;
    let five_hour = limits.get("five_hour").and_then(window);
    let seven_day = limits.get("seven_day").and_then(window);
    if five_hour.is_none() && seven_day.is_none() {
        return None;
    }
    Some(Feed {
        written_at: now_secs,
        five_hour,
        seven_day,
    })
}

fn window_json(w: Option<Window>) -> Value {
    w.map_or(
        Value::Null,
        |w| json!({"used_percentage": w.used_percentage, "resets_at": w.resets_at}),
    )
}

pub fn to_json(f: &Feed) -> String {
    json!({"written_at": f.written_at, "five_hour": window_json(f.five_hour), "seven_day": window_json(f.seven_day)})
        .to_string()
}

pub fn from_json(text: &str) -> Option<Feed> {
    let v: Value = serde_json::from_str(text).ok()?;
    Some(Feed {
        written_at: v.get("written_at")?.as_i64()?,
        five_hour: v.get("five_hour").and_then(window),
        seven_day: v.get("seven_day").and_then(window),
    })
}

/// Fresh when written within the last 15 minutes.
pub fn is_fresh(f: &Feed, now_secs: i64) -> bool {
    now_secs - f.written_at <= FRESH_SECS
}

/// The feed as a reading. A window absent from the feed is 0% with no reset time. No per-model weeks.
pub fn to_reading(f: &Feed) -> Reading {
    let limit = |label: &str, w: Option<Window>| Limit {
        label: label.to_owned(),
        percent: w.map_or(0.0, |w| w.used_percentage),
        resets_text: None,
        resets_at: w.map(|w| w.resets_at * 1000),
    };
    Reading {
        session: Some(limit("Session", f.five_hour)),
        week: Some(limit("Week (all models)", f.seven_day)),
        models: Vec::new(),
    }
}

/// `$XDG_RUNTIME_DIR/claude-usage`, else `$HOME/.cache/claude-usage`.
pub fn feed_dir() -> Option<PathBuf> {
    if let Some(d) = std::env::var_os("XDG_RUNTIME_DIR").filter(|d| !d.is_empty()) {
        return Some(PathBuf::from(d).join("claude-usage"));
    }
    std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".cache/claude-usage"))
}

pub const FILE_NAME: &str = "rate_limits.json";

/// Write the feed atomically (temp file + rename) into `dir`, created 0700.
pub fn write_to(dir: &Path, f: &Feed) -> std::io::Result<()> {
    use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
    std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(dir)?;
    let tmp = dir.join(format!("{FILE_NAME}.tmp{}", std::process::id()));
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(&tmp)?;
    file.write_all(to_json(f).as_bytes())?;
    drop(file);
    std::fs::rename(&tmp, dir.join(FILE_NAME)).inspect_err(|_| {
        let _ = std::fs::remove_file(&tmp);
    })
}

pub fn read_from(dir: &Path) -> Option<Feed> {
    from_json(&std::fs::read_to_string(dir.join(FILE_NAME)).ok()?)
}

/// The applet's feed, if present and readable.
pub fn read() -> Option<Feed> {
    read_from(&feed_dir()?)
}

/// `--statusline`: stdin JSON in, feed file out, nothing on stdout, every error ignored.
pub fn run_statusline() {
    let mut input = String::new();
    if std::io::stdin().read_to_string(&mut input).is_err() {
        return;
    }
    let now = chrono::Utc::now().timestamp();
    if let (Some(feed), Some(dir)) = (from_stdin(&input, now), feed_dir()) {
        let _ = write_to(&dir, &feed);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const BOTH: &str = r#"{"model":{"id":"x"},"rate_limits":{"five_hour":{"used_percentage":23.5,"resets_at":1792000000},"seven_day":{"used_percentage":41.2,"resets_at":1792400000},"spend_limit":{"used":1}}}"#;
    const SEVEN_ONLY: &str =
        r#"{"rate_limits":{"seven_day":{"used_percentage":8,"resets_at":1792400000}}}"#;
    const NONE: &str = r#"{"model":{"id":"x"},"cwd":"/tmp"}"#;

    #[test]
    fn parses_both_windows_and_ignores_spend_limit() {
        let f = from_stdin(BOTH, 1000).unwrap();
        assert_eq!(f.written_at, 1000);
        assert_eq!(
            f.five_hour,
            Some(Window {
                used_percentage: 23.5,
                resets_at: 1_792_000_000
            })
        );
        assert_eq!(
            f.seven_day,
            Some(Window {
                used_percentage: 41.2,
                resets_at: 1_792_400_000
            })
        );
    }

    #[test]
    fn parses_a_payload_with_only_seven_day() {
        let f = from_stdin(SEVEN_ONLY, 5).unwrap();
        assert_eq!(f.five_hour, None);
        assert_eq!(f.seven_day.unwrap().used_percentage, 8.0);
    }

    #[test]
    fn writes_nothing_without_rate_limits_or_windows_or_valid_json() {
        assert_eq!(from_stdin(NONE, 1), None);
        assert_eq!(from_stdin(r#"{"rate_limits":{"spend_limit":{}}}"#, 1), None);
        assert_eq!(from_stdin(r#"{"rate_limits":{}}"#, 1), None);
        assert_eq!(from_stdin("not json", 1), None);
        assert_eq!(from_stdin("", 1), None);
    }

    #[test]
    fn json_round_trips_with_null_for_a_missing_window() {
        let f = from_stdin(SEVEN_ONLY, 77).unwrap();
        let text = to_json(&f);
        assert!(text.contains(r#""five_hour":null"#), "{text}");
        assert_eq!(from_json(&text), Some(f));
        assert_eq!(from_json("{}"), None);
    }

    #[test]
    fn freshness_is_fifteen_minutes() {
        let f = from_stdin(BOTH, 10_000).unwrap();
        assert!(is_fresh(&f, 10_000 + FRESH_SECS));
        assert!(!is_fresh(&f, 10_000 + FRESH_SECS + 1));
    }

    #[test]
    fn reading_converts_seconds_to_ms_and_zeroes_absent_windows() {
        let r = to_reading(&from_stdin(SEVEN_ONLY, 1).unwrap());
        let s = r.session.unwrap();
        assert_eq!((s.percent, s.resets_at), (0.0, None));
        let w = r.week.unwrap();
        assert_eq!((w.percent, w.resets_at), (8.0, Some(1_792_400_000_000)));
        assert_eq!(w.label, "Week (all models)");
        assert!(r.models.is_empty());
    }

    #[test]
    fn writes_atomically_into_a_private_dir_and_reads_back() {
        use std::os::unix::fs::PermissionsExt;
        let dir = std::env::temp_dir()
            .join(format!("claude-usage-test-{}", std::process::id()))
            .join("claude-usage");
        let f = from_stdin(BOTH, 42).unwrap();
        write_to(&dir, &f).unwrap();
        write_to(&dir, &f).unwrap();
        assert_eq!(read_from(&dir), Some(f));
        assert_eq!(
            std::fs::metadata(&dir).unwrap().permissions().mode() & 0o777,
            0o700
        );
        let names: Vec<_> = std::fs::read_dir(&dir)
            .unwrap()
            .map(|e| e.unwrap().file_name())
            .collect();
        assert_eq!(names.len(), 1, "no temp file left: {names:?}");
        std::fs::remove_dir_all(dir.parent().unwrap()).unwrap();
    }
}
