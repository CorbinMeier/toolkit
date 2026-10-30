// SPDX-License-Identifier: MIT
//! Claude Code token usage (spec §3.2): scan the local transcripts, dedupe replies, keep time buckets.
//!
//! - [`scan`]: finds the `usage` numbers in transcript lines (the only things ever read from a line).
//! - [`index`]: dedupe set, per-file read offsets and the minute and hour buckets.
//! - [`store`]: the versioned binary index on disk.
//! - [`indexer`]: the low-priority background thread. Start it with [`Indexer::spawn`].
//! - [`view`]: the UI side. [`TokenView`] applies the thread's messages and answers window queries.
//!
//! The UI never waits: every tick it calls [`Indexer::drain_into`] (non-blocking) and then reads the view.
//! Message content is never stored, logged or sent: a [`Counts`] is all that leaves a line.

pub mod index;
pub mod indexer;
pub mod scan;
pub mod store;
pub mod view;

pub use indexer::{Indexer, IndexerConfig};
pub use view::{TokenView, TokenWindow};

/// Model family of a reply, from the model id (`claude-opus-5-5` is [`Family::Opus`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Family {
    Opus,
    Sonnet,
    Haiku,
    Fable,
    Other,
}

/// Number of [`Family`] variants: the length of a [`ByFamily`].
pub const FAMILIES: usize = 5;

impl Family {
    pub const ALL: [Family; FAMILIES] = [
        Family::Opus,
        Family::Sonnet,
        Family::Haiku,
        Family::Fable,
        Family::Other,
    ];

    /// Position in a [`ByFamily`].
    pub fn index(self) -> usize {
        self as usize
    }

    pub fn name(self) -> &'static str {
        match self {
            Family::Opus => "opus",
            Family::Sonnet => "sonnet",
            Family::Haiku => "haiku",
            Family::Fable => "fable",
            Family::Other => "other",
        }
    }

    /// Classifies a model id by substring, ignoring case; unknown ids are [`Family::Other`].
    pub fn from_model(model: &str) -> Family {
        let has = |needle: &str| {
            model
                .as_bytes()
                .windows(needle.len())
                .any(|w| w.eq_ignore_ascii_case(needle.as_bytes()))
        };
        if has("opus") {
            Family::Opus
        } else if has("sonnet") {
            Family::Sonnet
        } else if has("haiku") {
            Family::Haiku
        } else if has("fable") {
            Family::Fable
        } else {
            Family::Other
        }
    }
}

/// The four token counts of the spec.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Counts {
    /// `input_tokens`: new, uncached input.
    pub input: u64,
    /// `cache_creation_input_tokens`.
    pub cache_write: u64,
    /// `cache_read_input_tokens`.
    pub cache_read: u64,
    /// `output_tokens` (thinking tokens are already inside it).
    pub output: u64,
}

impl Counts {
    pub fn add(&mut self, other: &Counts) {
        self.input = self.input.saturating_add(other.input);
        self.cache_write = self.cache_write.saturating_add(other.cache_write);
        self.cache_read = self.cache_read.saturating_add(other.cache_read);
        self.output = self.output.saturating_add(other.output);
    }

    pub fn is_zero(&self) -> bool {
        *self == Counts::default()
    }

    /// What the graph shows upward: input plus cache write.
    pub fn new_input(&self) -> u64 {
        self.input.saturating_add(self.cache_write)
    }

    /// All four counts.
    pub fn all(&self) -> u64 {
        self.new_input()
            .saturating_add(self.cache_read)
            .saturating_add(self.output)
    }
}

/// Counts per model family, indexed by [`Family::index`].
pub type ByFamily = [Counts; FAMILIES];

/// Sum over the families.
pub fn sum_families(by: &ByFamily) -> Counts {
    let mut sum = Counts::default();
    by.iter().for_each(|c| sum.add(c));
    sum
}

/// Local midnight (epoch ms) of the day containing `now_ms`: the "today" boundary of spec §3.2.
pub fn local_midnight_ms(now_ms: i64) -> i64 {
    use chrono::{Local, TimeZone};
    let Some(now) = Local.timestamp_millis_opt(now_ms).single() else {
        return now_ms - now_ms.rem_euclid(86_400_000);
    };
    now.date_naive()
        .and_hms_opt(0, 0, 0)
        .and_then(|midnight| Local.from_local_datetime(&midnight).earliest())
        .map_or(now_ms - now_ms.rem_euclid(86_400_000), |t| {
            t.timestamp_millis()
        })
}

/// Messages from the indexer thread to the UI thread.
#[derive(Debug, Clone)]
pub enum Msg {
    /// Where the indexer is: loading the cache, backfilling, or caught up.
    Progress(Progress),
    /// Buckets that changed since the last message (full bucket values, so applying twice is harmless).
    Delta(Delta),
}

/// Changed buckets plus the retention floors, so the receiver can drop what the indexer dropped.
#[derive(Debug, Clone, Default)]
pub struct Delta {
    /// `(minutes since epoch, counts)`.
    pub minutes: Vec<(i64, ByFamily)>,
    /// `(hours since epoch, counts)`.
    pub hours: Vec<(i64, ByFamily)>,
    /// Minute buckets below this are expired.
    pub minute_floor: i64,
    /// Hour buckets below this are expired.
    pub hour_floor: i64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Phase {
    /// Reading the saved index.
    Loading,
    /// First-run (or rebuild) backfill, or catching up after the program was closed.
    Backfill,
    /// Caught up; only the live tail runs.
    Live,
}

/// How the saved index was found at start.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CacheState {
    /// Loaded and trusted.
    Loaded,
    /// There was none (first run).
    Fresh,
    /// It was unreadable, damaged or from another version: deleted and rebuilt.
    Rebuilt,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Progress {
    pub phase: Phase,
    pub cache: CacheState,
    pub files_done: u32,
    pub files_total: u32,
    pub bytes_done: u64,
    pub bytes_total: u64,
}

impl Progress {
    /// Backfill progress 0 to 100 by bytes (100 once caught up or when there was nothing to read).
    pub fn percent(&self) -> u8 {
        if self.phase == Phase::Live || self.bytes_total == 0 {
            return 100;
        }
        (self.bytes_done.min(self.bytes_total) * 100 / self.bytes_total) as u8
    }

    pub fn is_backfilling(&self) -> bool {
        self.phase != Phase::Live
    }
}
