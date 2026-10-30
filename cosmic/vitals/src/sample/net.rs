// SPDX-License-Identifier: MIT
//! Network: bytes/s per interface and summed over the included ones, with the 5-minute peaks for the
//! bars and the graph ceiling.
//!
//! Every interface in `/proc/net/dev` is tracked. Whether one counts toward the sum ("included") is the
//! physical-interface rule (`<sys>/class/net/<name>/device` exists: loopback, bridges, docker veths and
//! tunnels have no `device` entry) with the config's `include` and `exclude` lists on top (spec §3).

use std::io;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use crate::history::RollingMax;

use super::{KeptFile, Root, lines, parse_u64, seconds_between, words};

/// The graph ceiling never drops below this.
pub const PEAK_FLOOR_BPS: f64 = 100_000.0;
/// How far back the peak looks.
pub const PEAK_WINDOW: Duration = Duration::from_secs(5 * 60);
/// Room for 5 minutes at the fastest interval (250 ms) plus slack, for the summed peak.
const PEAK_CAPACITY: usize = 1300;
/// Per interface (a monotonic deque only grows while the rate keeps falling).
const IFACE_PEAK_CAPACITY: usize = 256;

/// Received and sent rates in bytes per second.
#[derive(Clone, Copy, Default, Debug, PartialEq)]
pub struct Rate {
    pub down_bps: f64,
    pub up_bps: f64,
}

/// One interface.
pub struct Iface {
    pub name: String,
    physical: bool,
    included: bool,
    seen: bool,
    prev: Option<(u64, u64)>,
    /// Counters when vitals first saw it (0 for one that appeared later: its counters start at 0).
    base: (u64, u64),
    rate: Rate,
    peaks: (RollingMax, RollingMax),
    /// The 5-minute ceiling as of the last sample.
    ceiling: Rate,
}

/// Whether an interface counts toward `all`: `exclude` wins, then `include`, else the physical rule.
pub fn is_included(name: &str, physical: bool, include: &[String], exclude: &[String]) -> bool {
    if exclude.iter().any(|n| n == name) {
        false
    } else if include.iter().any(|n| n == name) {
        true
    } else {
        physical
    }
}

impl Iface {
    /// Bytes per second over the last interval.
    pub fn rate(&self) -> Rate {
        self.rate
    }

    /// Received and sent bytes since boot, as of the last sample (`/proc/net/dev` counters).
    pub fn counters(&self) -> (u64, u64) {
        self.prev.unwrap_or_default()
    }

    /// Received and sent bytes since vitals started.
    pub fn since_start(&self) -> (u64, u64) {
        let (rx, tx) = self.counters();
        (
            rx.saturating_sub(self.base.0),
            tx.saturating_sub(self.base.1),
        )
    }

    /// True if it has a `device` entry in sysfs (the physical-interface rule).
    pub fn is_physical(&self) -> bool {
        self.physical
    }

    /// True if it counts toward the sum.
    pub fn included(&self) -> bool {
        self.included
    }

    /// Its own 5-minute ceiling per direction, at least [`PEAK_FLOOR_BPS`].
    pub fn peak(&self) -> Rate {
        self.ceiling
    }
}

pub struct NetSampler {
    file: KeptFile,
    sys_net: PathBuf,
    ifaces: Vec<Iface>,
    include: Vec<String>,
    exclude: Vec<String>,
    /// False until the first read: interfaces seen then start their byte count there.
    started: bool,
    last: Option<Instant>,
    total: Rate,
    peak_down: RollingMax,
    peak_up: RollingMax,
    ceiling: Rate,
}

/// Splits a `/proc/net/dev` line at its colon into the interface name and the counters.
fn split_line(line: &[u8]) -> Option<(&[u8], &[u8])> {
    let colon = memchr::memchr(b':', line)?;
    Some((line[..colon].trim_ascii(), &line[colon + 1..]))
}

fn new_peak() -> RollingMax {
    RollingMax::new(PEAK_WINDOW, PEAK_CAPACITY)
}

fn iface_peak() -> RollingMax {
    RollingMax::new(PEAK_WINDOW, IFACE_PEAK_CAPACITY)
}

impl NetSampler {
    pub fn new(root: &Root) -> io::Result<Self> {
        Ok(NetSampler {
            file: KeptFile::open(&root.proc.join("net/dev"), 8 * 1024)?,
            sys_net: root.sys.join("class/net"),
            ifaces: Vec::new(),
            include: Vec::new(),
            exclude: Vec::new(),
            started: false,
            last: None,
            total: Rate::default(),
            peak_down: new_peak(),
            peak_up: new_peak(),
            ceiling: Rate {
                down_bps: PEAK_FLOOR_BPS,
                up_bps: PEAK_FLOOR_BPS,
            },
        })
    }

    /// Sets the config's `include` and `exclude` lists. Allocates; call it when they change, not per tick.
    /// When the included set changes the summed history restarts (the old peak was of another sum).
    pub fn set_overrides(&mut self, include: &[String], exclude: &[String]) {
        self.include = include.to_vec();
        self.exclude = exclude.to_vec();
        let mut changed = false;
        for i in &mut self.ifaces {
            let now = is_included(&i.name, i.physical, include, exclude);
            changed |= now != i.included;
            i.included = now;
        }
        if changed {
            self.sum_included();
            self.peak_down = new_peak();
            self.peak_up = new_peak();
            self.ceiling = Rate {
                down_bps: PEAK_FLOOR_BPS,
                up_bps: PEAK_FLOOR_BPS,
            };
        }
    }

    fn sum_included(&mut self) {
        let mut total = Rate::default();
        for i in self.ifaces.iter().filter(|i| i.included) {
            total.down_bps += i.rate.down_bps;
            total.up_bps += i.rate.up_bps;
        }
        self.total = total;
    }

    /// Reads once. Rates need two samples, so the first call leaves them at 0.
    pub fn sample(&mut self, now: Instant) -> io::Result<()> {
        let buf = self.file.read()?;
        let dt = self.last.map(|t| seconds_between(t, now));
        for i in &mut self.ifaces {
            i.seen = false;
        }
        let mut total = Rate::default();
        for line in lines(buf) {
            let Some((name, counters)) = split_line(line) else {
                continue;
            };
            // Header lines have no numeric first field; a counters line has rx bytes first and tx bytes ninth.
            let mut w = words(counters);
            let Some(first) = w.next() else { continue };
            if !first.first().is_some_and(u8::is_ascii_digit) {
                continue;
            }
            let rx = parse_u64(first);
            let tx = parse_u64(w.nth(7).unwrap_or(b"0"));
            let idx = match self.ifaces.iter().position(|i| i.name.as_bytes() == name) {
                Some(idx) => idx,
                None => {
                    let name = String::from_utf8_lossy(name).into_owned();
                    let physical = self.sys_net.join(&name).join("device").exists();
                    let included = is_included(&name, physical, &self.include, &self.exclude);
                    self.ifaces.push(Iface {
                        name,
                        physical,
                        included,
                        seen: false,
                        prev: None,
                        base: if self.started { (0, 0) } else { (rx, tx) },
                        rate: Rate::default(),
                        peaks: (iface_peak(), iface_peak()),
                        ceiling: Rate {
                            down_bps: PEAK_FLOOR_BPS,
                            up_bps: PEAK_FLOOR_BPS,
                        },
                    });
                    self.ifaces.len() - 1
                }
            };
            let iface = &mut self.ifaces[idx];
            iface.seen = true;
            // A counter that went back (interface reset) reads 0.
            iface.rate = match (iface.prev, dt) {
                (Some((prx, ptx)), Some(dt)) => Rate {
                    down_bps: rx.saturating_sub(prx) as f64 / dt,
                    up_bps: tx.saturating_sub(ptx) as f64 / dt,
                },
                _ => Rate::default(),
            };
            iface.prev = Some((rx, tx));
            if iface.included {
                total.down_bps += iface.rate.down_bps;
                total.up_bps += iface.rate.up_bps;
            }
            let (down, up) = &mut iface.peaks;
            down.push(now, iface.rate.down_bps);
            up.push(now, iface.rate.up_bps);
            iface.ceiling = Rate {
                down_bps: down.max(now).unwrap_or(0.0).max(PEAK_FLOOR_BPS),
                up_bps: up.max(now).unwrap_or(0.0).max(PEAK_FLOOR_BPS),
            };
        }
        self.started = true;
        // Interfaces that vanished (docker veths come and go) are forgotten.
        self.ifaces.retain(|i| i.seen);
        self.total = total;
        self.peak_down.push(now, total.down_bps);
        self.peak_up.push(now, total.up_bps);
        self.ceiling = Rate {
            down_bps: self.peak_down.max(now).unwrap_or(0.0).max(PEAK_FLOOR_BPS),
            up_bps: self.peak_up.max(now).unwrap_or(0.0).max(PEAK_FLOOR_BPS),
        };
        self.last = Some(now);
        Ok(())
    }

    /// Rates summed over the included interfaces.
    pub fn total(&self) -> Rate {
        self.total
    }

    /// Every interface, in `/proc/net/dev` order.
    pub fn interfaces(&self) -> impl Iterator<Item = &Iface> {
        self.ifaces.iter()
    }

    /// The included interfaces.
    pub fn included(&self) -> impl Iterator<Item = &Iface> {
        self.ifaces.iter().filter(|i| i.included)
    }

    /// The physical interfaces (the default inclusion rule), in `/proc/net/dev` order.
    pub fn physical(&self) -> impl Iterator<Item = &Iface> {
        self.ifaces.iter().filter(|i| i.physical)
    }

    /// Bytes received and sent by the included interfaces since vitals started.
    pub fn since_start(&self) -> (u64, u64) {
        self.included().fold((0, 0), |(rx, tx), i| {
            let (r, t) = i.since_start();
            (rx + r, tx + t)
        })
    }

    /// Highest summed rate of the last 5 minutes, at least [`PEAK_FLOOR_BPS`] (the graph ceiling), as of
    /// the last [`NetSampler::sample`].
    pub fn peak(&self) -> Rate {
        self.ceiling
    }

    /// The same ceiling for one interface by name (`None` if there is none by that name).
    pub fn peak_of(&self, name: &str) -> Option<Rate> {
        self.ifaces
            .iter()
            .find(|i| i.name == name)
            .map(|i| i.ceiling)
    }
}
