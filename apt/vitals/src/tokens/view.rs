// SPDX-License-Identifier: MIT
//! The UI side of the token index: apply the indexer's messages, answer window queries.

use std::collections::BTreeMap;

use super::index::{HOUR_MS, MINUTE_MS};
use super::{ByFamily, Counts, Delta, Msg, Progress, sum_families};

/// The token panel's time windows (spec §5): minute buckets up to 24 h, hour buckets beyond.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TokenWindow {
    H1,
    H6,
    H24,
    D7,
    D30,
}

impl TokenWindow {
    pub const ALL: [TokenWindow; 5] = [Self::H1, Self::H6, Self::H24, Self::D7, Self::D30];

    pub fn label(self) -> &'static str {
        match self {
            Self::H1 => "1h",
            Self::H6 => "6h",
            Self::H24 => "24h",
            Self::D7 => "7d",
            Self::D30 => "30d",
        }
    }

    pub fn from_label(s: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|w| w.label() == s)
    }

    /// Length of one bucket in milliseconds: a minute or an hour.
    pub fn bucket_ms(self) -> i64 {
        match self {
            Self::H1 | Self::H6 | Self::H24 => MINUTE_MS,
            Self::D7 | Self::D30 => HOUR_MS,
        }
    }

    /// Number of buckets in the window.
    pub fn buckets(self) -> usize {
        match self {
            Self::H1 => 60,
            Self::H6 => 360,
            Self::H24 => 1440,
            Self::D7 => 168,
            Self::D30 => 720,
        }
    }
}

/// Token buckets as the UI sees them. Fed by [`Msg`]s; never touches the disk.
#[derive(Default)]
pub struct TokenView {
    minutes: BTreeMap<i64, ByFamily>,
    hours: BTreeMap<i64, ByFamily>,
    progress: Option<Progress>,
    revision: u64,
}

impl TokenView {
    pub fn new() -> Self {
        Self::default()
    }

    /// Applies one message from the indexer.
    pub fn apply(&mut self, msg: Msg) {
        self.revision += 1;
        match msg {
            Msg::Progress(p) => self.progress = Some(p),
            Msg::Delta(Delta {
                minutes,
                hours,
                minute_floor,
                hour_floor,
            }) => {
                self.minutes.extend(minutes);
                self.hours.extend(hours);
                self.minutes = self.minutes.split_off(&minute_floor);
                self.hours = self.hours.split_off(&hour_floor);
            }
        }
    }

    /// Goes up whenever a message was applied: redraw the panel only when it changed.
    pub fn revision(&self) -> u64 {
        self.revision
    }

    /// The latest progress message, `None` until the thread has said anything.
    pub fn progress(&self) -> Option<&Progress> {
        self.progress.as_ref()
    }

    /// The window as dense buckets, oldest first; the last one holds `now_ms`. Buckets with no replies
    /// are zero. No allocation: the UI folds these into its own `f32` columns.
    pub fn series_iter(
        &self,
        window: TokenWindow,
        now_ms: i64,
    ) -> impl Iterator<Item = ByFamily> + '_ {
        let size = window.bucket_ms();
        let map = if size == MINUTE_MS {
            &self.minutes
        } else {
            &self.hours
        };
        let last = now_ms.div_euclid(size);
        let first = last - window.buckets() as i64 + 1;
        (first..=last).map(move |k| map.get(&k).copied().unwrap_or_default())
    }

    /// [`TokenView::series_iter`] collected into `out`, which is refilled.
    pub fn series(&self, window: TokenWindow, now_ms: i64, out: &mut Vec<ByFamily>) {
        out.clear();
        out.extend(self.series_iter(window, now_ms));
    }

    /// Totals per family from `from_ms` to `now_ms`. Ranges inside the minute retention are exact to the
    /// minute; older ones use hour buckets, so the hour that contains `from_ms` counts whole.
    pub fn sum_since(&self, from_ms: i64, now_ms: i64) -> ByFamily {
        let (map, size) = if now_ms - from_ms <= 47 * HOUR_MS {
            (&self.minutes, MINUTE_MS)
        } else {
            (&self.hours, HOUR_MS)
        };
        let mut total = ByFamily::default();
        for (_, by) in map.range(from_ms.div_euclid(size)..=now_ms.div_euclid(size)) {
            for (t, c) in total.iter_mut().zip(by) {
                t.add(c);
            }
        }
        total
    }

    /// Totals of the last `minutes` minutes (for the rate in the title).
    pub fn recent(&self, now_ms: i64, minutes: i64) -> Counts {
        sum_families(&self.sum_since(now_ms - minutes * MINUTE_MS, now_ms))
    }

    /// Totals from local midnight to now.
    pub fn today(&self, now_ms: i64) -> ByFamily {
        self.sum_since(super::local_midnight_ms(now_ms), now_ms)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tokens::Family;

    fn by(output: u64) -> ByFamily {
        let mut b = ByFamily::default();
        b[Family::Opus.index()].output = output;
        b
    }

    #[test]
    fn series_is_dense_and_ends_at_now() {
        let mut v = TokenView::new();
        let now = 100 * MINUTE_MS + 5;
        v.apply(Msg::Delta(Delta {
            minutes: vec![(100, by(7)), (98, by(3))],
            hours: vec![],
            minute_floor: 0,
            hour_floor: 0,
        }));
        let mut out = Vec::new();
        v.series(TokenWindow::H1, now, &mut out);
        assert_eq!(out.len(), 60);
        assert_eq!(out[59][0].output, 7);
        assert_eq!(out[58][0].output, 0);
        assert_eq!(out[57][0].output, 3);
    }

    #[test]
    fn deltas_replace_buckets_and_floors_expire_old_ones() {
        let mut v = TokenView::new();
        v.apply(Msg::Delta(Delta {
            minutes: vec![(5, by(1)), (10, by(1))],
            ..Delta::default()
        }));
        v.apply(Msg::Delta(Delta {
            minutes: vec![(10, by(9))],
            minute_floor: 8,
            ..Delta::default()
        }));
        let now = 10 * MINUTE_MS;
        assert_eq!(v.sum_since(0, now)[0].output, 9);
        assert_eq!(v.recent(now, 5).output, 9);
    }

    #[test]
    fn long_ranges_use_hour_buckets() {
        let mut v = TokenView::new();
        v.apply(Msg::Delta(Delta {
            hours: vec![(10, by(4)), (11, by(6))],
            ..Delta::default()
        }));
        assert_eq!(v.sum_since(10 * HOUR_MS, 200 * HOUR_MS)[0].output, 10);
    }
}
