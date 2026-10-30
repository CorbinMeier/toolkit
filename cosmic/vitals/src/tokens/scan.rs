// SPDX-License-Identifier: MIT
//! Transcript line scanner: a cheap `memchr` filter, then a minimal serde parse of just the usage fields.
//!
//! Only numbers leave a line. The message text, tool input and everything else is skipped by the parser
//! and never copied, kept or logged.

use std::borrow::Cow;
use std::sync::OnceLock;

use memchr::memmem;
use serde::Deserialize;

use super::{Counts, Family};

/// One reply's usage, ready for the index.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Record {
    /// Reply time, epoch milliseconds (UTC).
    pub ts_ms: i64,
    /// 64-bit hash of `message.id` (fallback `requestId`): the dedupe key.
    pub id_hash: u64,
    pub family: Family,
    pub counts: Counts,
}

#[derive(Deserialize)]
struct Line<'a> {
    #[serde(rename = "type", borrow)]
    kind: Option<Cow<'a, str>>,
    #[serde(borrow)]
    timestamp: Option<Cow<'a, str>>,
    #[serde(rename = "requestId", borrow)]
    request_id: Option<Cow<'a, str>>,
    #[serde(borrow)]
    message: Option<Message<'a>>,
}

#[derive(Deserialize)]
struct Message<'a> {
    #[serde(borrow)]
    id: Option<Cow<'a, str>>,
    #[serde(borrow)]
    model: Option<Cow<'a, str>>,
    usage: Option<Usage>,
}

#[derive(Deserialize)]
struct Usage {
    input_tokens: Option<u64>,
    cache_creation_input_tokens: Option<u64>,
    cache_read_input_tokens: Option<u64>,
    output_tokens: Option<u64>,
}

fn usage_finder() -> &'static memmem::Finder<'static> {
    static FINDER: OnceLock<memmem::Finder<'static>> = OnceLock::new();
    FINDER.get_or_init(|| memmem::Finder::new(b"\"usage\""))
}

/// FNV-1a with a final avalanche. Stable across Rust versions (the hashes are saved in the index) and
/// well spread, so the dedupe table can use the value itself as its hash.
pub fn hash_id(id: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for &b in id {
        h ^= u64::from(b);
        h = h.wrapping_mul(0x0000_0100_0000_01b3);
    }
    h ^= h >> 33;
    h = h.wrapping_mul(0xff51_afd7_ed55_8ccd);
    h ^= h >> 33;
    h
}

/// Days since 1970-01-01 of a civil date (Howard Hinnant's algorithm).
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// `2026-10-29T05:17:12.646Z` to epoch milliseconds. Accepts a `+hh:mm` offset instead of `Z`.
pub fn parse_timestamp_ms(s: &[u8]) -> Option<i64> {
    let num = |range: std::ops::Range<usize>| -> Option<i64> {
        let part = s.get(range)?;
        if part.is_empty() || !part.iter().all(u8::is_ascii_digit) {
            return None;
        }
        Some(part.iter().fold(0i64, |n, b| n * 10 + i64::from(b - b'0')))
    };
    if s.len() < 19 || s[4] != b'-' || s[7] != b'-' || !matches!(s[10], b'T' | b' ') {
        return None;
    }
    let (y, mo, d) = (num(0..4)?, num(5..7)?, num(8..10)?);
    let (h, mi, sec) = (num(11..13)?, num(14..16)?, num(17..19)?);
    if !(1..=12).contains(&mo) || !(1..=31).contains(&d) || h > 23 || mi > 59 || sec > 60 {
        return None;
    }
    let mut i = 19;
    let mut millis = 0i64;
    if s.get(i) == Some(&b'.') {
        i += 1;
        let mut scale = 100;
        while let Some(b) = s.get(i).filter(|b| b.is_ascii_digit()) {
            millis += i64::from(b - b'0') * scale;
            scale /= 10;
            i += 1;
        }
    }
    let offset_min = match s.get(i) {
        Some(b'+' | b'-') => {
            let sign = if s[i] == b'+' { 1 } else { -1 };
            let oh = num(i + 1..i + 3)?;
            let om = num(i + 4..i + 6).unwrap_or(0);
            sign * (oh * 60 + om)
        }
        _ => 0,
    };
    let secs = days_from_civil(y, mo, d) * 86_400 + h * 3600 + mi * 60 + sec - offset_min * 60;
    Some(secs * 1000 + millis)
}

/// Parses one transcript line. Returns `None` for anything that is not an assistant reply carrying
/// token numbers (almost every line, rejected by the `memchr` filter before any parsing).
pub fn parse_line(line: &[u8]) -> Option<Record> {
    usage_finder().find(line)?;
    let parsed: Line = serde_json::from_slice(line).ok()?;
    if parsed.kind.as_deref() != Some("assistant") {
        return None;
    }
    let message = parsed.message?;
    let usage = message.usage?;
    let key = message.id.as_deref().or(parsed.request_id.as_deref())?;
    let counts = Counts {
        input: usage.input_tokens.unwrap_or(0),
        cache_write: usage.cache_creation_input_tokens.unwrap_or(0),
        cache_read: usage.cache_read_input_tokens.unwrap_or(0),
        output: usage.output_tokens.unwrap_or(0),
    };
    Some(Record {
        ts_ms: parse_timestamp_ms(parsed.timestamp.as_deref()?.as_bytes())?,
        id_hash: hash_id(key.as_bytes()),
        family: Family::from_model(message.model.as_deref().unwrap_or("")),
        counts,
    })
}

/// Parses every complete line of `buf` and hands each record to `on_record`. Returns how many bytes were
/// consumed: up to and including the last newline. An unfinished last line is left for the next read.
pub fn scan_lines(buf: &[u8], on_record: &mut dyn FnMut(Record)) -> usize {
    let Some(end) = memchr::memrchr(b'\n', buf).map(|i| i + 1) else {
        return 0;
    };
    let mut start = 0;
    for nl in memchr::memchr_iter(b'\n', &buf[..end]) {
        if let Some(rec) = parse_line(&buf[start..nl]) {
            on_record(rec);
        }
        start = nl + 1;
    }
    end
}

#[cfg(test)]
mod tests {
    use super::*;

    const LINE: &str = r#"{"type":"assistant","timestamp":"2026-10-29T05:17:12.646Z","requestId":"req_a1","message":{"id":"msg_a1","model":"claude-opus-5-5","usage":{"input_tokens":2,"cache_creation_input_tokens":40973,"cache_read_input_tokens":28604,"output_tokens":292,"service_tier":"standard"},"content":[{"type":"text","text":"hello \"usage\" world"}]},"cwd":"/x"}"#;

    #[test]
    fn parses_an_assistant_line() {
        let r = parse_line(LINE.as_bytes()).unwrap();
        assert_eq!(r.family, Family::Opus);
        assert_eq!(
            r.counts,
            Counts {
                input: 2,
                cache_write: 40973,
                cache_read: 28604,
                output: 292
            }
        );
        assert_eq!(r.ts_ms, 1_793_251_032_646);
        assert_eq!(r.id_hash, hash_id(b"msg_a1"));
    }

    #[test]
    fn falls_back_to_request_id_without_a_message_id() {
        let line = LINE.replace(r#""id":"msg_a1","#, "");
        assert_eq!(
            parse_line(line.as_bytes()).unwrap().id_hash,
            hash_id(b"req_a1")
        );
    }

    #[test]
    fn ignores_lines_that_are_not_assistant_usage() {
        assert!(parse_line(br#"{"type":"user","message":{"content":"x"}}"#).is_none());
        assert!(
            parse_line(br#"{"type":"user","message":{"content":"mentions \"usage\" here"}}"#)
                .is_none()
        );
        let user_with_usage = LINE.replace("assistant", "user");
        assert!(parse_line(user_with_usage.as_bytes()).is_none());
        assert!(parse_line(b"not json with \"usage\" in it").is_none());
        assert!(parse_line(b"").is_none());
    }

    #[test]
    fn missing_counts_read_zero_and_nulls_are_tolerated() {
        let line = r#"{"type":"assistant","timestamp":"2026-10-29T05:17:12Z","message":{"id":"m","model":"x","usage":{"output_tokens":5,"input_tokens":null}}}"#;
        let r = parse_line(line.as_bytes()).unwrap();
        assert_eq!(
            r.counts,
            Counts {
                output: 5,
                ..Counts::default()
            }
        );
        assert_eq!(r.family, Family::Other);
    }

    #[test]
    fn timestamps_parse_with_and_without_fractions_and_offsets() {
        assert_eq!(parse_timestamp_ms(b"1970-01-01T00:00:00Z"), Some(0));
        assert_eq!(parse_timestamp_ms(b"1970-01-01T00:00:01.5Z"), Some(1500));
        assert_eq!(
            parse_timestamp_ms(b"2026-10-29T05:17:12.646Z"),
            Some(1_793_251_032_646)
        );
        assert_eq!(parse_timestamp_ms(b"1970-01-01T01:00:00+01:00"), Some(0));
        assert_eq!(
            parse_timestamp_ms(b"2000-02-29T12:00:00Z"),
            Some(951_825_600_000)
        );
        assert_eq!(parse_timestamp_ms(b"garbage"), None);
        assert_eq!(parse_timestamp_ms(b"2026-13-01T00:00:00Z"), None);
    }

    #[test]
    fn families_come_from_the_model_id() {
        assert_eq!(Family::from_model("claude-opus-5-5"), Family::Opus);
        assert_eq!(
            Family::from_model("claude-3-5-HAIKU-20241022"),
            Family::Haiku
        );
        assert_eq!(Family::from_model("claude-sonnet-5-5"), Family::Sonnet);
        assert_eq!(Family::from_model("claude-fable-1"), Family::Fable);
        assert_eq!(Family::from_model("<synthetic>"), Family::Other);
    }

    #[test]
    fn scan_leaves_an_unfinished_last_line() {
        let text = format!("{LINE}\n{}", &LINE[..60]);
        let mut n = 0;
        let used = scan_lines(text.as_bytes(), &mut |_| n += 1);
        assert_eq!(n, 1);
        assert_eq!(used, LINE.len() + 1);
        assert_eq!(scan_lines(LINE.as_bytes(), &mut |_| n += 1), 0);
    }
}
