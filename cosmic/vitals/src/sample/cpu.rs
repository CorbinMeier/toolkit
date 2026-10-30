// SPDX-License-Identifier: MIT
//! CPU: per-thread percent, their mean, load averages and the average frequency.
//!
//! Definitions: busy = total of the first 8 fields − (idle + iowait), percent =
//! Δbusy / Δtotal, a thread missing from the older sample reads 0, a counter that went back reads 0.

use std::io;

use super::{KeptFile, Root, lines, parse_f32, parse_u64, words};

#[derive(Clone, Copy, Default, Debug, PartialEq, Eq)]
struct Ticks {
    busy: u64,
    total: u64,
}

/// Parses the `cpuN` lines of `/proc/stat` (the all-CPU `cpu` line is skipped) into `out`.
fn parse_stat(buf: &[u8], out: &mut Vec<Ticks>) {
    out.clear();
    for line in lines(buf) {
        let is_thread = line.len() > 3 && line.starts_with(b"cpu") && line[3].is_ascii_digit();
        if !is_thread {
            continue;
        }
        let (mut total, mut idle) = (0u64, 0u64);
        for (i, w) in words(line).skip(1).take(8).enumerate() {
            let v = parse_u64(w);
            total = total.saturating_add(v);
            if i == 3 || i == 4 {
                idle = idle.saturating_add(v);
            }
        }
        out.push(Ticks {
            busy: total.saturating_sub(idle),
            total,
        });
    }
}

/// Reads CPU figures; call [`CpuSampler::sample`] once per tick.
pub struct CpuSampler {
    stat: KeptFile,
    loadavg: Option<KeptFile>,
    freq: Vec<KeptFile>,
    prev: Vec<Ticks>,
    cur: Vec<Ticks>,
    percent: Vec<f32>,
    mean: f32,
    load: [f32; 3],
    freq_ghz: Option<f32>,
    samples: u32,
}

impl CpuSampler {
    /// Opens `/proc/stat`, `/proc/loadavg` and every `cpufreq/policy*/scaling_cur_freq` found.
    /// Only `/proc/stat` is required.
    pub fn new(root: &Root) -> io::Result<Self> {
        let stat = KeptFile::open(&root.proc.join("stat"), 16 * 1024)?;
        let loadavg = KeptFile::open(&root.proc.join("loadavg"), 256).ok();
        let mut freq = Vec::new();
        let policies = root.sys.join("devices/system/cpu/cpufreq");
        if let Ok(dir) = std::fs::read_dir(policies) {
            let mut paths: Vec<_> = dir
                .flatten()
                .filter(|e| e.file_name().to_string_lossy().starts_with("policy"))
                .map(|e| e.path().join("scaling_cur_freq"))
                .collect();
            paths.sort();
            freq.extend(paths.iter().filter_map(|p| KeptFile::open(p, 64).ok()));
        }
        Ok(CpuSampler {
            stat,
            loadavg,
            freq,
            prev: Vec::new(),
            cur: Vec::new(),
            percent: Vec::new(),
            mean: 0.0,
            load: [0.0; 3],
            freq_ghz: None,
            samples: 0,
        })
    }

    /// Reads once. The first call only stores the counters, so percents read 0 until the second.
    pub fn sample(&mut self) -> io::Result<()> {
        parse_stat(self.stat.read()?, &mut self.cur);
        let have_prev = self.samples > 0;
        self.percent.resize(self.cur.len(), 0.0);
        let mut sum = 0.0f32;
        for (i, now) in self.cur.iter().enumerate() {
            let pct = match self.prev.get(i) {
                Some(before) if have_prev && now.total > before.total => {
                    let busy = now.busy.saturating_sub(before.busy) as f64;
                    let total = (now.total - before.total) as f64;
                    (busy / total * 100.0).clamp(0.0, 100.0) as f32
                }
                _ => 0.0,
            };
            self.percent[i] = pct;
            sum += pct;
        }
        self.mean = if self.cur.is_empty() {
            0.0
        } else {
            sum / self.cur.len() as f32
        };
        std::mem::swap(&mut self.prev, &mut self.cur);
        self.samples = self.samples.saturating_add(1);

        if let Some(file) = self.loadavg.as_mut()
            && let Ok(buf) = file.read()
        {
            for (slot, w) in self.load.iter_mut().zip(words(buf)) {
                *slot = parse_f32(w);
            }
        }
        let mut khz = 0u64;
        let mut count = 0u32;
        for file in &mut self.freq {
            if let Ok(buf) = file.read() {
                khz += parse_u64(buf);
                count += 1;
            }
        }
        self.freq_ghz = (count > 0).then(|| (khz as f64 / count as f64 / 1_000_000.0) as f32);
        Ok(())
    }

    /// False until two samples were taken; before that every percent is a placeholder 0.
    pub fn ready(&self) -> bool {
        self.samples >= 2
    }

    /// Percent busy per logical thread (0 to 100), in `/proc/stat` order.
    pub fn percent(&self) -> &[f32] {
        &self.percent
    }

    /// The mean of the threads: the "total" CPU figure.
    pub fn mean(&self) -> f32 {
        self.mean
    }

    /// Load averages over 1, 5 and 15 minutes.
    pub fn load(&self) -> [f32; 3] {
        self.load
    }

    /// Average current frequency over the cpufreq policies in GHz, `None` where cpufreq is absent.
    pub fn freq_ghz(&self) -> Option<f32> {
        self.freq_ghz
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_one_entry_per_thread_and_skips_the_all_cpu_line() {
        let text = b"cpu  400 0 200 1400 0 0 0 0 0 0\ncpu0 100 0 50 700 0 0 0 0 0 0\ncpu1 300 0 150 700 0 0 0 0 0 0\nintr 12345\nctxt 99\n";
        let mut out = Vec::new();
        parse_stat(text, &mut out);
        assert_eq!(
            out,
            vec![
                Ticks {
                    busy: 150,
                    total: 850
                },
                Ticks {
                    busy: 450,
                    total: 1150
                }
            ]
        );
    }

    #[test]
    fn iowait_counts_as_idle() {
        let mut out = Vec::new();
        parse_stat(b"cpu0 10 0 10 70 10 0 0 0\n", &mut out);
        assert_eq!(
            out,
            vec![Ticks {
                busy: 20,
                total: 100
            }]
        );
    }
}
