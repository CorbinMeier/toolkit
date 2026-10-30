// SPDX-License-Identifier: MIT
//! Samplers for `/proc` and `/sys`.
//!
//! Every file is opened once and kept open. A tick reads it again from offset 0 into a reused buffer
//! and parses the bytes in place, so a steady-state tick allocates nothing (spec §3). Each sampler takes
//! a [`Root`] so tests can point it at fixture trees instead of the live machine.

pub mod cpu;
pub mod disk;
pub mod hwmon;
pub mod mem;
pub mod net;

use std::fs::File;
use std::io;
use std::os::unix::fs::FileExt;
use std::path::{Path, PathBuf};
use std::time::Instant;

/// Where `/proc` and `/sys` live. Tests use a fixture tree with the same layout.
#[derive(Debug, Clone)]
pub struct Root {
    pub proc: PathBuf,
    pub sys: PathBuf,
}

impl Root {
    /// The live machine: `/proc` and `/sys`.
    pub fn system() -> Self {
        Self::new("/proc", "/sys")
    }

    pub fn new(proc: impl Into<PathBuf>, sys: impl Into<PathBuf>) -> Self {
        Root {
            proc: proc.into(),
            sys: sys.into(),
        }
    }
}

impl Default for Root {
    fn default() -> Self {
        Self::system()
    }
}

/// Every sampler together, sampled in one call. The first call only primes the counters: rates read 0
/// and [`cpu::CpuSampler::ready`] is false until the second call.
pub struct System {
    pub cpu: cpu::CpuSampler,
    pub mem: mem::MemSampler,
    pub disk: disk::DiskSampler,
    pub net: net::NetSampler,
    pub hwmon: hwmon::Hwmon,
}

impl System {
    /// Opens every file once. Only `/proc/stat`, `meminfo`, `diskstats` and `net/dev` are required;
    /// frequency and temperature sensors are optional.
    pub fn new(root: &Root) -> io::Result<Self> {
        Ok(System {
            cpu: cpu::CpuSampler::new(root)?,
            mem: mem::MemSampler::new(root)?,
            disk: disk::DiskSampler::new(root)?,
            net: net::NetSampler::new(root)?,
            hwmon: hwmon::Hwmon::new(root),
        })
    }

    /// Reads every source once. A source that fails to read keeps its last values; the first error is
    /// returned after all of them were tried.
    pub fn sample(&mut self, now: Instant) -> io::Result<()> {
        let results = [
            self.cpu.sample(),
            self.mem.sample(),
            self.disk.sample(now),
            self.net.sample(now),
            self.hwmon.sample(),
        ];
        results.into_iter().collect()
    }
}

/// A file kept open, read again from offset 0 into a buffer that is only ever grown.
pub(crate) struct KeptFile {
    file: File,
    buf: Vec<u8>,
}

impl KeptFile {
    pub(crate) fn open(path: &Path, capacity: usize) -> io::Result<Self> {
        Ok(KeptFile {
            file: File::open(path)?,
            buf: vec![0; capacity],
        })
    }

    /// The file's current contents. `pread` at offset 0 is the `seek(0)` + `read` of the spec in one syscall.
    /// The buffer doubles whenever the file fills it, so it settles after the first ticks.
    pub(crate) fn read(&mut self) -> io::Result<&[u8]> {
        loop {
            let n = self.file.read_at(&mut self.buf, 0)?;
            if n < self.buf.len() {
                return Ok(&self.buf[..n]);
            }
            let doubled = self.buf.len() * 2;
            self.buf.resize(doubled, 0);
        }
    }

    pub(crate) fn raw_fd(&self) -> i32 {
        use std::os::fd::AsRawFd;
        self.file.as_raw_fd()
    }
}

/// Lines of a buffer without allocating; a trailing newline does not yield an empty last line.
pub(crate) fn lines(buf: &[u8]) -> impl Iterator<Item = &[u8]> {
    let mut rest = buf;
    std::iter::from_fn(move || {
        if rest.is_empty() {
            return None;
        }
        match memchr::memchr(b'\n', rest) {
            Some(i) => {
                let line = &rest[..i];
                rest = &rest[i + 1..];
                Some(line)
            }
            None => Some(std::mem::take(&mut rest)),
        }
    })
}

/// Whitespace-separated words of a line, empty ones skipped.
pub(crate) fn words(line: &[u8]) -> impl Iterator<Item = &[u8]> {
    line.split(|b| b.is_ascii_whitespace())
        .filter(|w| !w.is_empty())
}

/// Unsigned decimal; anything that is not a digit ends the number, no digits reads 0.
pub(crate) fn parse_u64(word: &[u8]) -> u64 {
    let mut n: u64 = 0;
    for &b in word {
        if !b.is_ascii_digit() {
            break;
        }
        n = n.saturating_mul(10).saturating_add(u64::from(b - b'0'));
    }
    n
}

/// A decimal float such as `1.25`; unreadable text reads 0.
pub(crate) fn parse_f32(word: &[u8]) -> f32 {
    std::str::from_utf8(word)
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(0.0)
}

/// Seconds between two instants, never below 1 ms so a rate never divides by zero.
pub(crate) fn seconds_between(earlier: Instant, later: Instant) -> f64 {
    later
        .saturating_duration_since(earlier)
        .as_secs_f64()
        .max(0.001)
}
