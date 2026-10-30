// SPDX-License-Identifier: MIT
//! Memory from `/proc/meminfo`, in bytes, with the standard definitions.

use std::io;

use super::{KeptFile, Root, lines, parse_u64, words};

/// One reading of `/proc/meminfo`. All figures are bytes.
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq)]
pub struct Mem {
    pub total: u64,
    pub available: u64,
    /// `Cached` + `SReclaimable`.
    pub cached: u64,
    pub buffers: u64,
    pub swap_total: u64,
    pub swap_free: u64,
}

impl Mem {
    /// In use: `MemTotal − MemAvailable`.
    pub fn used(&self) -> u64 {
        self.total.saturating_sub(self.available)
    }

    /// Swap in use: `SwapTotal − SwapFree`.
    pub fn swap_used(&self) -> u64 {
        self.swap_total.saturating_sub(self.swap_free)
    }

    /// `used` as a percent of `total` (0 when the total is unknown).
    pub fn used_percent(&self) -> f32 {
        percent(self.used(), self.total)
    }

    /// Swap in use as a percent of the swap total.
    pub fn swap_percent(&self) -> f32 {
        percent(self.swap_used(), self.swap_total)
    }
}

fn percent(part: u64, whole: u64) -> f32 {
    if whole == 0 {
        0.0
    } else {
        (part as f64 / whole as f64 * 100.0).clamp(0.0, 100.0) as f32
    }
}

/// Parses the text of `/proc/meminfo`; keys it does not know are skipped.
pub fn parse_meminfo(buf: &[u8]) -> Mem {
    let mut mem = Mem::default();
    let mut cached = 0;
    let mut reclaimable = 0;
    for line in lines(buf) {
        let Some(colon) = memchr::memchr(b':', line) else {
            continue;
        };
        // Values are kB; a line with no unit would be bytes, but meminfo never has one for these keys.
        let kb = || parse_u64(words(&line[colon + 1..]).next().unwrap_or(b"0")) * 1024;
        match &line[..colon] {
            b"MemTotal" => mem.total = kb(),
            b"MemAvailable" => mem.available = kb(),
            b"Cached" => cached = kb(),
            b"SReclaimable" => reclaimable = kb(),
            b"Buffers" => mem.buffers = kb(),
            b"SwapTotal" => mem.swap_total = kb(),
            b"SwapFree" => mem.swap_free = kb(),
            _ => {}
        }
    }
    mem.cached = cached + reclaimable;
    mem
}

/// Keeps `/proc/meminfo` open and parses it on each tick.
pub struct MemSampler {
    file: KeptFile,
    mem: Mem,
}

impl MemSampler {
    pub fn new(root: &Root) -> io::Result<Self> {
        Ok(MemSampler {
            file: KeptFile::open(&root.proc.join("meminfo"), 4096)?,
            mem: Mem::default(),
        })
    }

    pub fn sample(&mut self) -> io::Result<()> {
        self.mem = parse_meminfo(self.file.read()?);
        Ok(())
    }

    /// The latest reading.
    pub fn mem(&self) -> Mem {
        self.mem
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const MEMINFO: &[u8] = b"MemTotal:       16000000 kB\nMemFree:         1000000 kB\nMemAvailable:    4000000 kB\nBuffers:          100 kB\nCached:           900 kB\nSwapTotal:       2000 kB\nSwapFree:        1500 kB\nSReclaimable:     100 kB\n";

    #[test]
    fn reads_memory_in_bytes() {
        let m = parse_meminfo(MEMINFO);
        assert_eq!(m.total, 16_000_000 * 1024);
        assert_eq!(m.available, 4_000_000 * 1024);
        assert_eq!(m.cached, 1000 * 1024);
        assert_eq!(m.buffers, 100 * 1024);
        assert_eq!(m.swap_total, 2000 * 1024);
        assert_eq!(m.swap_free, 1500 * 1024);
        assert_eq!(m.used(), 12_000_000 * 1024);
        assert_eq!(m.swap_used(), 500 * 1024);
    }

    #[test]
    fn missing_keys_read_zero_and_never_underflow() {
        let m = parse_meminfo(b"MemTotal: 10 kB\nMemAvailable: 99 kB\n");
        assert_eq!(m.used(), 0);
        assert_eq!(parse_meminfo(b"").used_percent(), 0.0);
    }
}
