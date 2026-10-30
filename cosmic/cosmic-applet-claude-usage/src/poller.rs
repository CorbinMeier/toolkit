// SPDX-License-Identifier: MIT
//! The usage poll (port of an earlier TypeScript implementation (`poller.ts`)): runs `claude -p /usage`, keeps the last good reading.

use std::path::PathBuf;
use std::process::Stdio;
use std::time::Duration;

use claude_usage_core::feed::{self, Feed};
use claude_usage_core::parse;
use claude_usage_core::reading::{Limit, Reading};

/// `--no-session-persistence` keeps each poll from writing a session transcript under ~/.claude/projects.
pub const CLAUDE_USAGE_ARGS: [&str; 5] = [
    "-p",
    "/usage",
    "--output-format",
    "json",
    "--no-session-persistence",
];
pub const RUN_TIMEOUT: Duration = Duration::from_secs(60);
/// `claude /usage` takes ~16 s and ~0.5 GB per run: it only runs as the fallback, at most this often.
pub const USAGE_INTERVAL: Duration = Duration::from_secs(15 * 60);

/// What the last polls produced. The reading survives failures; `error` marks it stale.
#[derive(Debug, Default)]
pub struct Status {
    pub reading: Option<Reading>,
    /// ms since the Unix epoch of the last good reading.
    pub read_at: Option<i64>,
    pub error: Option<String>,
}

impl Status {
    /// Adopt a fresh feed when it is newer than the current reading (newest reading wins).
    pub fn apply_feed(&mut self, f: &Feed, now_ms: i64) {
        let at = f.written_at * 1000;
        if feed::is_fresh(f, now_ms / 1000) && self.read_at.is_none_or(|r| at >= r) {
            let mut reading = feed::to_reading(f);
            // A null window is not evidence of a reset (the first write of a session can omit one):
            // keep the current value while its reset time is still ahead.
            if let Some(old) = &self.reading {
                let keep = |new: &mut Option<Limit>, old: &Option<Limit>, absent: bool| {
                    if absent
                        && let Some(o) = old
                            .as_ref()
                            .filter(|o| o.resets_at.is_some_and(|r| r > now_ms))
                    {
                        *new = Some(o.clone());
                    }
                };
                keep(&mut reading.session, &old.session, f.five_hour.is_none());
                keep(&mut reading.week, &old.week, f.seven_day.is_none());
            }
            self.reading = Some(reading);
            self.read_at = Some(at);
            self.error = None;
        }
    }

    /// A failure after at least one good reading.
    pub fn stale(&self) -> bool {
        self.error.is_some() && self.reading.is_some()
    }

    /// Fold in one run's outcome: `Ok(result text)` or `Err(why the run failed)`.
    pub fn apply(&mut self, outcome: Result<String, String>, now_ms: i64) {
        match outcome {
            Ok(text) => {
                let limited = parse::rate_limited_line(&text);
                match (parse::parse_usage(&text, now_ms), limited) {
                    (Some(reading), None) => {
                        self.reading = Some(reading);
                        self.read_at = Some(now_ms);
                        self.error = None;
                    }
                    (_, Some(line)) => self.error = Some(format!("Rate limited: {line}")),
                    (None, None) => {
                        self.error = Some("The usage output had no limit lines".to_owned())
                    }
                }
            }
            Err(e) => {
                self.error = Some(format!(
                    "claude /usage failed: {}",
                    e.lines().next().unwrap_or("")
                ))
            }
        }
    }
}

/// `$HOME/.local/bin/claude` when it exists (the panel's PATH may lack `~/.local/bin`), else `claude` on PATH.
pub fn claude_path() -> PathBuf {
    if let Some(home) = std::env::var_os("HOME") {
        let p = PathBuf::from(home).join(".local/bin/claude");
        if p.is_file() {
            return p;
        }
    }
    PathBuf::from("claude")
}

/// The `result` string of `claude --output-format json` output.
pub fn extract_result(stdout: &str) -> Result<String, String> {
    let v: serde_json::Value =
        serde_json::from_str(stdout).map_err(|e| format!("bad JSON: {e}"))?;
    v.get("result")
        .and_then(|r| r.as_str())
        .map(str::to_owned)
        .ok_or_else(|| "no result text".to_owned())
}

/// Run a command to completion within `timeout`; stdout on success.
pub async fn run_command(
    program: &std::path::Path,
    args: &[&str],
    timeout: Duration,
) -> Result<String, String> {
    let child = tokio::process::Command::new(program)
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .map_err(|e| e.to_string())?;
    let out = tokio::time::timeout(timeout, child.wait_with_output())
        .await
        .map_err(|_| format!("timed out after {}s", timeout.as_secs()))?
        .map_err(|e| e.to_string())?;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr);
        let first = err.lines().find(|l| !l.trim().is_empty()).unwrap_or("");
        return Err(format!(
            "{}{}{first}",
            out.status,
            if first.is_empty() { "" } else { ": " }
        ));
    }
    String::from_utf8(out.stdout).map_err(|e| e.to_string())
}

/// The real run: `claude -p /usage ...` and its `result` text.
pub async fn run_claude() -> Result<String, String> {
    let stdout = run_command(&claude_path(), &CLAUDE_USAGE_ARGS, RUN_TIMEOUT).await?;
    extract_result(&stdout)
}

/// A tick this close before the due time counts as due (ticks are 60 s apart).
const SLACK: Duration = Duration::from_secs(5);

/// Never lets two `/usage` runs overlap, and starts one at most every `USAGE_INTERVAL`.
#[derive(Debug, Default)]
pub struct Schedule {
    running: bool,
    last_start: Option<std::time::Instant>,
}

impl Schedule {
    /// True once when a run should start now (and marks it running).
    pub fn try_start(&mut self, now: std::time::Instant) -> bool {
        if self.running
            || self
                .last_start
                .is_some_and(|t| now + SLACK < t + USAGE_INTERVAL)
        {
            return false;
        }
        self.running = true;
        self.last_start = Some(now);
        true
    }

    /// A run ended.
    pub fn finish(&mut self) {
        self.running = false;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    const FIXTURE: &str = include_str!("../tests/fixtures/usage-output.txt");
    const NOW: i64 = 1_792_000_000_000; // Oct 2026

    fn fixture_at(now: i64) -> Status {
        let mut s = Status::default();
        s.apply(Ok(FIXTURE.to_owned()), now);
        s
    }

    #[test]
    fn command_does_not_persist_a_session_transcript() {
        assert!(CLAUDE_USAGE_ARGS.contains(&"--no-session-persistence"));
        assert_eq!(&CLAUDE_USAGE_ARGS[..2], ["-p", "/usage"]);
    }

    #[test]
    fn a_good_reading_is_stored() {
        let s = fixture_at(NOW);
        assert!(!s.stale());
        assert_eq!((s.read_at, s.error.as_deref()), (Some(NOW), None));
    }

    #[test]
    fn keeps_the_last_good_reading_stale_on_failure_or_rate_limit() {
        let mut s = fixture_at(NOW);
        let cases = [
            (Err("spawn claude ENOENT".to_owned()), "spawn claude ENOENT"),
            (
                Ok("Usage rate limited until 3:05pm".to_owned()),
                "rate limited until 3:05pm",
            ),
            (Ok("no limits here".to_owned()), "no limit lines"),
        ];
        for (outcome, words) in cases {
            s.apply(outcome, NOW + 1000);
            assert!(s.stale());
            assert_eq!(s.read_at, Some(NOW));
            assert_eq!(
                s.reading
                    .as_ref()
                    .unwrap()
                    .session
                    .as_ref()
                    .unwrap()
                    .percent,
                65.0
            );
            assert!(s.error.as_deref().unwrap().contains(words), "{:?}", s.error);
        }
    }

    #[test]
    fn a_failure_with_no_reading_is_not_stale_but_has_the_error() {
        let mut s = Status::default();
        s.apply(Err("boom\nsecond line".to_owned()), NOW);
        assert!(!s.stale());
        assert_eq!(s.error.as_deref(), Some("claude /usage failed: boom"));
    }

    #[test]
    fn a_good_run_clears_the_error() {
        let mut s = fixture_at(NOW);
        s.apply(Err("x".to_owned()), NOW + 1);
        s.apply(Ok(FIXTURE.to_owned()), NOW + 2);
        assert_eq!((s.error, s.read_at), (None, Some(NOW + 2)));
    }

    #[test]
    fn schedule_runs_at_startup_never_overlaps_and_waits_15_minutes() {
        let t0 = std::time::Instant::now();
        let mut sch = Schedule::default();
        assert!(sch.try_start(t0), "first poll at startup");
        assert!(
            !sch.try_start(t0 + Duration::from_secs(3600)),
            "no overlap while running"
        );
        sch.finish();
        assert!(!sch.try_start(t0 + Duration::from_secs(60)));
        assert!(sch.try_start(t0 + USAGE_INTERVAL - Duration::from_secs(2)));
    }

    fn feed_at(secs: i64) -> Feed {
        feed::from_stdin(r#"{"rate_limits":{"five_hour":{"used_percentage":10,"resets_at":1},"seven_day":{"used_percentage":20,"resets_at":2}}}"#, secs).unwrap()
    }

    #[test]
    fn a_fresh_feed_becomes_the_reading_and_drops_per_model_weeks() {
        let mut s = fixture_at(NOW);
        assert_eq!(s.reading.as_ref().unwrap().models.len(), 1);
        s.apply_feed(&feed_at(NOW / 1000 + 30), NOW + 60_000);
        assert_eq!(s.read_at, Some(NOW + 30_000));
        assert!(s.reading.as_ref().unwrap().models.is_empty());
        assert_eq!(s.reading.unwrap().session.unwrap().percent, 10.0);
    }

    #[test]
    fn a_null_window_keeps_the_current_value_until_its_reset_has_passed() {
        let only_seven = |secs| {
            feed::from_stdin(
                r#"{"rate_limits":{"seven_day":{"used_percentage":20,"resets_at":2}}}"#,
                secs,
            )
            .unwrap()
        };
        // The fixture session (65%) resets in the future relative to its own parse time.
        let mut s = fixture_at(NOW);
        let reset = s
            .reading
            .as_ref()
            .unwrap()
            .session
            .as_ref()
            .unwrap()
            .resets_at
            .unwrap();
        s.apply_feed(&only_seven(NOW / 1000 + 1), NOW + 1000);
        let r = s.reading.as_ref().unwrap();
        assert_eq!(
            r.session.as_ref().unwrap().percent,
            65.0,
            "kept: reset still ahead"
        );
        assert_eq!(
            r.week.as_ref().unwrap().percent,
            20.0,
            "present window taken from the feed"
        );
        // Past the session's reset the null window means 0% with no reset time.
        let mut s = fixture_at(NOW);
        let later = reset + 1000;
        s.apply_feed(&only_seven(later / 1000), later);
        let session = s.reading.unwrap().session.unwrap();
        assert_eq!((session.percent, session.resets_at), (0.0, None));
        // No current value: 0% with no reset.
        let mut s = Status::default();
        s.apply_feed(&only_seven(NOW / 1000), NOW);
        assert_eq!(s.reading.unwrap().session.unwrap().percent, 0.0);
    }

    #[test]
    fn a_stale_or_older_feed_is_ignored_and_a_good_feed_clears_the_error() {
        let mut s = fixture_at(NOW);
        s.apply_feed(&feed_at(NOW / 1000 - 10), NOW);
        assert_eq!(s.read_at, Some(NOW), "older than the /usage reading");
        s.apply_feed(&feed_at(NOW / 1000 - 3600), NOW + 3_600_000);
        assert_eq!(s.read_at, Some(NOW), "stale feed");
        s.apply(Err("x".into()), NOW);
        assert!(s.stale());
        s.apply_feed(&feed_at(NOW / 1000 + 5), NOW + 10_000);
        assert!(!s.stale());
        s.apply(Ok(FIXTURE.to_owned()), NOW + 20_000);
        assert_eq!(s.read_at, Some(NOW + 20_000));
    }

    #[test]
    fn extracts_the_result_string() {
        assert_eq!(
            extract_result(r#"{"type":"result","result":"hi"}"#).unwrap(),
            "hi"
        );
        assert!(extract_result(r#"{"result":3}"#).is_err());
        assert!(extract_result("not json").is_err());
    }

    #[tokio::test]
    async fn run_command_returns_stdout_fails_on_exit_code_and_times_out() {
        let t = Duration::from_secs(5);
        let out = run_command(Path::new("/bin/sh"), &["-c", "printf ok"], t).await;
        assert_eq!(out.unwrap(), "ok");
        let err = run_command(Path::new("/bin/sh"), &["-c", "echo bad >&2; exit 3"], t)
            .await
            .unwrap_err();
        assert!(err.contains("bad"), "{err}");
        let err = run_command(
            Path::new("/bin/sh"),
            &["-c", "sleep 5"],
            Duration::from_millis(100),
        )
        .await
        .unwrap_err();
        assert!(err.contains("timed out"), "{err}");
        assert!(
            run_command(Path::new("/nonexistent/claude"), &[], t)
                .await
                .is_err()
        );
    }
}
