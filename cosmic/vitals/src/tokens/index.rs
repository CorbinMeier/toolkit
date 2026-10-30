// SPDX-License-Identifier: MIT
//! The in-memory token index: per-file read offsets, the dedupe set and the time buckets.

use std::collections::{BTreeMap, HashMap};
use std::path::PathBuf;

use super::scan::Record;
use super::{ByFamily, Delta};

pub const MINUTE_MS: i64 = 60_000;
pub const HOUR_MS: i64 = 3_600_000;
pub const DAY_MS: i64 = 86_400_000;
/// Minute buckets are kept this long (spec §3.2).
pub const MINUTE_RETENTION_MS: i64 = 48 * HOUR_MS;
/// Hour buckets and dedupe ids are kept this long.
pub const HOUR_RETENTION_MS: i64 = 90 * DAY_MS;

/// The dedupe set: reply ids as 64-bit hashes, each packed with the reply's day into one `u64`
/// (48 bits of hash, 16 bits of day plus one so an entry is never 0). Open addressing with linear
/// probing, about 12 bytes per id. A false "seen" needs two ids to share 48 bits: roughly 1 in 10^9 per
/// reply at 450,000 live ids.
#[derive(Default)]
pub struct IdSet {
    slots: Vec<u64>,
    len: usize,
}

impl IdSet {
    fn pack(hash: u64, day: u16) -> u64 {
        (hash & !0xffff) | (u64::from(day) + 1)
    }

    fn slot_of(hash: u64, cap: usize) -> usize {
        // Multiply-shift maps the well-mixed hash onto any capacity, not just powers of two.
        ((u128::from(hash & !0xffff) * cap as u128) >> 64) as usize
    }

    pub fn len(&self) -> usize {
        self.len
    }

    pub fn is_empty(&self) -> bool {
        self.len == 0
    }

    /// Adds an id; false if it was already there.
    pub fn insert(&mut self, hash: u64, day: u16) -> bool {
        self.insert_entry(Self::pack(hash, day))
    }

    /// Adds a packed entry as returned by [`IdSet::entries`].
    pub fn insert_entry(&mut self, entry: u64) -> bool {
        if (self.len + 1) * 4 > self.slots.len() * 3 {
            self.grow((self.slots.len() * 3 / 2).max(4096));
        }
        let cap = self.slots.len();
        let mut i = Self::slot_of(entry, cap);
        loop {
            match self.slots[i] {
                0 => {
                    self.slots[i] = entry;
                    self.len += 1;
                    return true;
                }
                e if (e ^ entry) >> 16 == 0 => return false,
                _ => i = if i + 1 == cap { 0 } else { i + 1 },
            }
        }
    }

    fn grow(&mut self, cap: usize) {
        let old = std::mem::replace(&mut self.slots, vec![0; cap]);
        self.len = 0;
        old.into_iter().filter(|&e| e != 0).for_each(|e| {
            self.insert_entry(e);
        });
    }

    /// Forgets ids whose day is before `min_day`.
    pub fn retain_days(&mut self, min_day: u16) {
        let keep: Vec<u64> = self
            .entries()
            .filter(|e| (e & 0xffff) as u16 > min_day)
            .collect();
        if keep.len() == self.len {
            return;
        }
        self.slots.fill(0);
        self.len = 0;
        keep.into_iter().for_each(|e| {
            self.insert_entry(e);
        });
    }

    /// The packed entries, in no particular order.
    pub fn entries(&self) -> impl Iterator<Item = u64> + '_ {
        self.slots.iter().copied().filter(|&e| e != 0)
    }

    /// Room for `n` ids without growing.
    pub fn reserve(&mut self, n: usize) {
        let want = n * 4 / 3 + 1;
        if want > self.slots.len() {
            self.grow(want);
        }
    }
}

/// How far one file has been read.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FileState {
    pub dev: u64,
    pub ino: u64,
    /// Bytes consumed so far, always at a line boundary.
    pub offset: u64,
}

/// Everything the token panel needs, and everything that is saved to disk.
#[derive(Default)]
pub struct Index {
    pub files: HashMap<PathBuf, FileState>,
    pub minutes: BTreeMap<i64, ByFamily>,
    pub hours: BTreeMap<i64, ByFamily>,
    /// Hash of every counted reply id, with the day number (since epoch) of the reply.
    pub seen: IdSet,
    /// Changed since the last save.
    pub dirty: bool,
    dirty_minutes: Vec<i64>,
    dirty_hours: Vec<i64>,
}

impl Index {
    pub fn new() -> Self {
        Self::default()
    }

    /// Counts one reply unless its id was seen before or it is older than the retention. Returns whether
    /// it was counted.
    pub fn add(&mut self, rec: &Record, now_ms: i64) -> bool {
        if rec.ts_ms < now_ms - HOUR_RETENTION_MS || rec.counts.is_zero() {
            return false;
        }
        let day = (rec.ts_ms.div_euclid(DAY_MS)).clamp(0, i64::from(u16::MAX) - 1) as u16;
        if !self.seen.insert(rec.id_hash, day) {
            return false;
        }
        let fam = rec.family.index();
        let hour = rec.ts_ms.div_euclid(HOUR_MS);
        self.hours.entry(hour).or_default()[fam].add(&rec.counts);
        self.dirty_hours.push(hour);
        if rec.ts_ms >= now_ms - MINUTE_RETENTION_MS {
            let minute = rec.ts_ms.div_euclid(MINUTE_MS);
            self.minutes.entry(minute).or_default()[fam].add(&rec.counts);
            self.dirty_minutes.push(minute);
        }
        self.dirty = true;
        true
    }

    /// Drops buckets, ids and files past their retention.
    pub fn prune(&mut self, now_ms: i64) {
        let (minute_floor, hour_floor) = floors(now_ms);
        let before = self.minutes.len() + self.hours.len() + self.seen.len();
        self.minutes = self.minutes.split_off(&minute_floor);
        self.hours = self.hours.split_off(&hour_floor);
        let day_floor = ((now_ms - HOUR_RETENTION_MS).div_euclid(DAY_MS)).max(0) as u16;
        self.seen.retain_days(day_floor);
        if self.minutes.len() + self.hours.len() + self.seen.len() != before {
            self.dirty = true;
        }
    }

    /// The buckets changed since the last call, or `None` if nothing changed.
    pub fn take_delta(&mut self, now_ms: i64) -> Option<Delta> {
        if self.dirty_minutes.is_empty() && self.dirty_hours.is_empty() {
            return None;
        }
        let mut dirty_minutes = std::mem::take(&mut self.dirty_minutes);
        let mut dirty_hours = std::mem::take(&mut self.dirty_hours);
        dirty_minutes.sort_unstable();
        dirty_minutes.dedup();
        dirty_hours.sort_unstable();
        dirty_hours.dedup();
        let (minute_floor, hour_floor) = floors(now_ms);
        Some(Delta {
            minutes: dirty_minutes
                .iter()
                .filter_map(|k| self.minutes.get(k).map(|v| (*k, *v)))
                .collect(),
            hours: dirty_hours
                .iter()
                .filter_map(|k| self.hours.get(k).map(|v| (*k, *v)))
                .collect(),
            minute_floor,
            hour_floor,
        })
    }

    /// Every bucket, as one delta: what a fresh receiver needs after the index was loaded.
    pub fn full_delta(&self, now_ms: i64) -> Delta {
        let (minute_floor, hour_floor) = floors(now_ms);
        Delta {
            minutes: self.minutes.iter().map(|(k, v)| (*k, *v)).collect(),
            hours: self.hours.iter().map(|(k, v)| (*k, *v)).collect(),
            minute_floor,
            hour_floor,
        }
    }
}

/// Oldest minute and hour bucket numbers still inside the retention.
pub fn floors(now_ms: i64) -> (i64, i64) {
    (
        (now_ms - MINUTE_RETENTION_MS).div_euclid(MINUTE_MS),
        (now_ms - HOUR_RETENTION_MS).div_euclid(HOUR_MS),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tokens::{Counts, Family};

    const NOW: i64 = 1_793_251_032_646;

    fn rec(id: u64, ts_ms: i64, output: u64) -> Record {
        Record {
            ts_ms,
            id_hash: crate::tokens::scan::hash_id(&id.to_le_bytes()),
            family: Family::Sonnet,
            counts: Counts {
                output,
                ..Counts::default()
            },
        }
    }

    #[test]
    fn counts_each_id_once() {
        let mut ix = Index::new();
        assert!(ix.add(&rec(1, NOW, 10), NOW));
        assert!(!ix.add(&rec(1, NOW, 10), NOW));
        assert!(ix.add(&rec(2, NOW, 5), NOW));
        let minute = NOW / MINUTE_MS;
        assert_eq!(ix.minutes[&minute][Family::Sonnet.index()].output, 15);
    }

    #[test]
    fn old_replies_go_to_hours_only_and_ancient_ones_nowhere() {
        let mut ix = Index::new();
        assert!(ix.add(&rec(1, NOW - 3 * DAY_MS, 7), NOW));
        assert!(ix.minutes.is_empty());
        assert_eq!(ix.hours.len(), 1);
        assert!(!ix.add(&rec(2, NOW - 100 * DAY_MS, 7), NOW));
    }

    #[test]
    fn prune_drops_what_expired() {
        let mut ix = Index::new();
        ix.add(&rec(1, NOW - HOUR_MS, 1), NOW);
        let later = NOW + 49 * HOUR_MS;
        ix.prune(later);
        assert!(ix.minutes.is_empty());
        assert_eq!(ix.hours.len(), 1);
        ix.prune(NOW + 91 * DAY_MS);
        assert!(ix.hours.is_empty() && ix.seen.is_empty());
    }

    #[test]
    fn delta_holds_changed_buckets_once() {
        let mut ix = Index::new();
        ix.add(&rec(1, NOW, 1), NOW);
        ix.add(&rec(2, NOW + 1000, 1), NOW);
        let d = ix.take_delta(NOW).unwrap();
        assert_eq!(d.minutes.len(), 1);
        assert_eq!(d.minutes[0].1[Family::Sonnet.index()].output, 2);
        assert!(ix.take_delta(NOW).is_none());
    }
}
