// SPDX-License-Identifier: MIT
//! Graph history: fixed-capacity ring buffers of `f32`, allocated once, and column downsampling by max.

use std::collections::VecDeque;
use std::time::{Duration, Instant};

/// Marks a column or point with no data yet (the graph leaves it blank).
pub const NO_DATA: f32 = f32::NAN;

/// How many points a window holds at the given sampling interval, at least 1.
pub fn points_for_window(window: Duration, interval: Duration) -> usize {
    let interval = interval.as_millis().max(1);
    ((window.as_millis() / interval) as usize).max(1)
}

/// Capacity that covers the longest window (30 minutes by spec §5) at the fastest interval (250 ms): 7,200.
pub const MAX_POINTS: usize = 30 * 60 * 4;

/// A ring buffer of `f32` samples with a fixed capacity: once full, a push overwrites the oldest.
pub struct Ring {
    buf: Box<[f32]>,
    /// Index the next push writes to.
    head: usize,
    len: usize,
}

impl Ring {
    /// Allocates the whole buffer now; `capacity` is raised to 1 if 0.
    pub fn new(capacity: usize) -> Self {
        Ring {
            buf: vec![NO_DATA; capacity.max(1)].into_boxed_slice(),
            head: 0,
            len: 0,
        }
    }

    pub fn capacity(&self) -> usize {
        self.buf.len()
    }

    pub fn len(&self) -> usize {
        self.len
    }

    pub fn is_empty(&self) -> bool {
        self.len == 0
    }

    pub fn clear(&mut self) {
        self.head = 0;
        self.len = 0;
    }

    /// Appends a sample, dropping the oldest when full. Never allocates.
    pub fn push(&mut self, value: f32) {
        self.buf[self.head] = value;
        self.head = (self.head + 1) % self.buf.len();
        self.len = (self.len + 1).min(self.buf.len());
    }

    /// The sample `age` steps back: 0 is the newest.
    pub fn get(&self, age: usize) -> Option<f32> {
        if age >= self.len {
            return None;
        }
        let cap = self.buf.len();
        Some(self.buf[(self.head + cap - 1 - age) % cap])
    }

    pub fn latest(&self) -> Option<f32> {
        self.get(0)
    }

    /// Samples from oldest to newest.
    pub fn iter(&self) -> impl Iterator<Item = f32> + '_ {
        (0..self.len).rev().filter_map(|age| self.get(age))
    }

    /// The largest sample over the newest `points` samples, ignoring gaps (`None` when there are none).
    pub fn max_over(&self, points: usize) -> Option<f32> {
        (0..points.min(self.len))
            .filter_map(|age| self.get(age))
            .filter(|v| !v.is_nan())
            .reduce(f32::max)
    }

    /// Collapses the newest `points` samples (the time window) into `width` columns, oldest on the left.
    /// A column holds the maximum of its samples, not the mean, so a short spike still shows. Columns
    /// reaching back before the first sample hold [`NO_DATA`], and when the window has fewer points than
    /// columns each point is stretched over several columns. `out` is cleared and refilled, so a reused
    /// vector never allocates again.
    pub fn columns(&self, points: usize, width: usize, out: &mut Vec<f32>) {
        out.clear();
        out.resize(width, NO_DATA);
        let n = points.max(1);
        for (c, slot) in out.iter_mut().enumerate() {
            // Window positions run 0 (oldest) to n-1 (newest); position j holds the sample of age n-1-j.
            let lo = c * n / width;
            let hi = ((c + 1) * n / width).max(lo + 1);
            let mut best = NO_DATA;
            for j in lo..hi {
                if let Some(v) = self.get(n - 1 - j)
                    && !v.is_nan()
                    && (best.is_nan() || v > best)
                {
                    best = v;
                }
            }
            *slot = best;
        }
    }
}

/// Collapses `src` (oldest first) into `width` columns by max, like [`Ring::columns`] but for a slice such
/// as the token buckets. Fewer values than columns are stretched; NaN counts as no data. `out` is refilled.
pub fn collapse(src: &[f32], width: usize, out: &mut Vec<f32>) {
    out.clear();
    out.resize(width, NO_DATA);
    let n = src.len();
    if n == 0 {
        return;
    }
    for (c, slot) in out.iter_mut().enumerate() {
        let lo = c * n / width;
        let hi = ((c + 1) * n / width).max(lo + 1).min(n);
        let mut best = NO_DATA;
        for &v in &src[lo.min(n - 1)..hi] {
            if !v.is_nan() && (best.is_nan() || v > best) {
                best = v;
            }
        }
        *slot = best;
    }
}

/// The maximum of a value over a sliding time window, e.g. the network graph ceiling (peak of the last
/// 5 minutes). A monotonic deque: O(1) amortized, allocated once for `capacity` samples.
pub struct RollingMax {
    window: Duration,
    items: VecDeque<(Instant, f64)>,
}

impl RollingMax {
    pub fn new(window: Duration, capacity: usize) -> Self {
        RollingMax {
            window,
            items: VecDeque::with_capacity(capacity.max(2)),
        }
    }

    /// Adds the reading taken at `now`.
    pub fn push(&mut self, now: Instant, value: f64) {
        while self.items.back().is_some_and(|&(_, v)| v <= value) {
            self.items.pop_back();
        }
        self.items.push_back((now, value));
        self.expire(now);
    }

    fn expire(&mut self, now: Instant) {
        while self
            .items
            .front()
            .is_some_and(|&(t, _)| now.saturating_duration_since(t) > self.window)
        {
            self.items.pop_front();
        }
    }

    /// The maximum of the readings within the window ending at `now`.
    pub fn max(&mut self, now: Instant) -> Option<f64> {
        self.expire(now);
        self.items.front().map(|&(_, v)| v)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn filled(cap: usize, values: &[f32]) -> Ring {
        let mut r = Ring::new(cap);
        values.iter().for_each(|&v| r.push(v));
        r
    }

    #[test]
    fn overwrites_the_oldest_when_full() {
        let r = filled(3, &[1.0, 2.0, 3.0, 4.0, 5.0]);
        assert_eq!(r.len(), 3);
        assert_eq!(r.iter().collect::<Vec<_>>(), vec![3.0, 4.0, 5.0]);
        assert_eq!(r.latest(), Some(5.0));
        assert_eq!(r.get(2), Some(3.0));
        assert_eq!(r.get(3), None);
    }

    #[test]
    fn columns_take_the_max_not_the_mean() {
        let r = filled(16, &[1.0, 9.0, 1.0, 1.0, 2.0, 2.0, 7.0, 2.0]);
        let mut out = Vec::new();
        r.columns(8, 4, &mut out);
        assert_eq!(out, vec![9.0, 1.0, 2.0, 7.0]);
    }

    #[test]
    fn columns_show_only_the_requested_window() {
        let r = filled(16, &[100.0, 1.0, 2.0, 3.0, 4.0]);
        let mut out = Vec::new();
        r.columns(4, 4, &mut out);
        assert_eq!(out, vec![1.0, 2.0, 3.0, 4.0]);
    }

    #[test]
    fn columns_before_the_first_sample_have_no_data() {
        let r = filled(16, &[5.0, 6.0]);
        let mut out = Vec::new();
        r.columns(4, 4, &mut out);
        assert!(out[0].is_nan() && out[1].is_nan());
        assert_eq!(&out[2..], &[5.0, 6.0]);
    }

    #[test]
    fn a_short_window_is_stretched_over_the_columns() {
        let r = filled(8, &[1.0, 2.0]);
        let mut out = Vec::new();
        r.columns(2, 4, &mut out);
        assert_eq!(out, vec![1.0, 1.0, 2.0, 2.0]);
    }

    #[test]
    fn columns_survive_wraparound() {
        let r = filled(4, &[1.0, 2.0, 3.0, 4.0, 5.0, 6.0]);
        let mut out = Vec::new();
        r.columns(4, 2, &mut out);
        assert_eq!(out, vec![4.0, 6.0]);
    }

    #[test]
    fn collapse_takes_the_max_and_stretches_short_slices() {
        let mut out = Vec::new();
        collapse(&[1.0, 5.0, 2.0, 2.0, 9.0, 0.0], 3, &mut out);
        assert_eq!(out, vec![5.0, 2.0, 9.0]);
        collapse(&[1.0, 2.0], 4, &mut out);
        assert_eq!(out, vec![1.0, 1.0, 2.0, 2.0]);
        collapse(&[], 2, &mut out);
        assert!(out.iter().all(|v| v.is_nan()));
    }

    #[test]
    fn window_points_follow_the_interval() {
        assert_eq!(
            points_for_window(Duration::from_secs(120), Duration::from_secs(1)),
            120
        );
        assert_eq!(
            points_for_window(Duration::from_secs(1800), Duration::from_millis(250)),
            MAX_POINTS
        );
    }

    #[test]
    fn rolling_max_forgets_old_peaks() {
        let t0 = Instant::now();
        let mut m = RollingMax::new(Duration::from_secs(300), 8);
        m.push(t0, 9.0);
        m.push(t0 + Duration::from_secs(100), 3.0);
        assert_eq!(m.max(t0 + Duration::from_secs(300)), Some(9.0));
        assert_eq!(m.max(t0 + Duration::from_secs(301)), Some(3.0));
        assert_eq!(m.max(t0 + Duration::from_secs(401)), None);
    }
}
