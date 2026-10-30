// SPDX-License-Identifier: MIT
//! Number formatting into a reused `String`, so drawing allocates nothing in steady state.

use std::fmt::Write;

// Widest output of each formatter, in cells. Bar value columns (spec §4, bars) are this wide and never
// sized from the current number, so a bar never changes width with the data. The unit tests below sweep
// each formatter against its constant.

/// [`percent`]: `100%`, up to `999%`.
pub const PERCENT_W: u16 = 4;
/// [`size`]: `1023K`, `1024K` after rounding, at most `9999T`.
pub const SIZE_W: u16 = 5;
/// [`rate`]: [`size`] plus `/s`.
pub const RATE_W: u16 = SIZE_W + 2;
/// [`count`]: `999`, `1000K` after rounding, at most `9999G`.
pub const COUNT_W: u16 = 5;

/// `512B`, `1.5K`, `140K`, `2.1M`: binary units, a decimal below 100.
pub fn size(out: &mut String, bytes: f64) {
    const UNITS: [&str; 5] = ["B", "K", "M", "G", "T"];
    let mut v = bytes.max(0.0);
    let mut i = 0;
    while v >= 1024.0 && i < UNITS.len() - 1 {
        v /= 1024.0;
        i += 1;
    }
    // The last unit has no bigger one to roll into: cap it so the width stays bounded.
    let v = if i == UNITS.len() - 1 {
        v.min(9999.0)
    } else {
        v
    };
    if i == 0 || v >= 99.95 {
        let _ = write!(out, "{v:.0}{}", UNITS[i]);
    } else {
        let _ = write!(out, "{v:.1}{}", UNITS[i]);
    }
}

pub fn rate(out: &mut String, bps: f64) {
    size(out, bps);
    out.push_str("/s");
}

/// Token counts: `12`, `340K`, `1.2M`, `48M`, `1.20G`.
pub fn count(out: &mut String, n: u64) {
    let (v, unit) = match n {
        0..1_000 => {
            let _ = write!(out, "{n}");
            return;
        }
        1_000..1_000_000 => (n as f64 / 1e3, "K"),
        1_000_000..1_000_000_000 => (n as f64 / 1e6, "M"),
        _ => (n as f64 / 1e9, "G"),
    };
    let v = if unit == "G" { v.min(9999.0) } else { v };
    if v >= 99.95 {
        let _ = write!(out, "{v:.0}{unit}");
    } else {
        let _ = write!(out, "{v:.1}{unit}");
    }
}

pub fn percent(out: &mut String, p: f32) {
    let _ = write!(out, "{:.0}%", p.clamp(0.0, 999.0));
}

pub fn window(out: &mut String, minutes: u32) {
    let _ = write!(out, "{minutes}m");
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(f: impl Fn(&mut String)) -> String {
        let mut o = String::new();
        f(&mut o);
        o
    }

    #[test]
    fn sizes_use_binary_units_and_drop_decimals_at_100() {
        assert_eq!(s(|o| size(o, 0.0)), "0B");
        assert_eq!(s(|o| size(o, 1536.0)), "1.5K");
        assert_eq!(s(|o| size(o, 140.0 * 1024.0)), "140K");
        assert_eq!(s(|o| size(o, 2.1 * 1048576.0)), "2.1M");
        assert_eq!(s(|o| rate(o, 3.0 * 1048576.0)), "3.0M/s");
    }

    #[test]
    fn counts_scale_by_thousands() {
        assert_eq!(s(|o| count(o, 12)), "12");
        assert_eq!(s(|o| count(o, 12_400)), "12.4K");
        assert_eq!(s(|o| count(o, 340_000)), "340K");
        assert_eq!(s(|o| count(o, 48_000_000)), "48.0M");
        assert_eq!(s(|o| count(o, 2_100_000_000)), "2.1G");
    }

    /// Values around every unit boundary, zero, fractions, huge and not-a-number.
    fn sweep() -> Vec<f64> {
        let mut v = vec![0.0, 0.4, 0.5, 1.0, 99.9, 1023.4, 1023.5, 1023.9, 1024.0];
        let mut unit = 1.0;
        for _ in 0..8 {
            for m in [
                0.9, 1.0, 9.94, 9.95, 99.94, 99.95, 100.0, 999.4, 999.5, 999.95, 1023.0, 1023.6,
                1024.0,
            ] {
                v.push(m * unit);
            }
            unit *= 1024.0;
        }
        v.extend([
            1e15,
            1e18,
            u64::MAX as f64,
            f64::MAX,
            f64::INFINITY,
            f64::NAN,
            -5.0,
        ]);
        v
    }

    #[test]
    fn value_formatters_never_exceed_their_column_width() {
        for v in sweep() {
            assert!(
                s(|o| size(o, v)).chars().count() <= usize::from(SIZE_W),
                "size {v}"
            );
            assert!(
                s(|o| rate(o, v)).chars().count() <= usize::from(RATE_W),
                "rate {v}"
            );
        }
        assert_eq!(s(|o| size(o, 1023.6)), "1024B");
        assert_eq!(s(|o| size(o, f64::MAX)), "9999T");
        let mut counts = vec![0u64, 1, 999, 1_000, 999_949, 999_950, 999_999, 1_000_000];
        counts.extend([999_949_999, 999_950_000, 1_000_000_000, u64::MAX]);
        for k in 1..19 {
            counts.push(10u64.pow(k) - 1);
            counts.push(10u64.pow(k));
        }
        for n in counts {
            assert!(
                s(|o| count(o, n)).chars().count() <= usize::from(COUNT_W),
                "count {n}"
            );
        }
        for p in [
            0.0f32,
            0.4,
            99.5,
            100.0,
            100.4,
            999.0,
            5000.0,
            f32::MAX,
            f32::INFINITY,
            f32::NAN,
            -3.0,
        ] {
            assert!(
                s(|o| percent(o, p)).chars().count() <= usize::from(PERCENT_W),
                "percent {p}"
            );
        }
    }
}
