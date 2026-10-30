// SPDX-License-Identifier: MIT
//! The readings of one command-line readout, shaped exactly as JSON schema 2 (spec §9), and the
//! [`Collector`] that fills them. The same structs feed the plain-text output, so both agree.
//!
//! Rounding happens here, once: percents and temperatures to 1 decimal, load and GHz to 2, bytes and
//! rates to whole numbers.

use std::io;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use claude_usage_core::feed;
use claude_usage_core::pace::{self, Kind};
use serde::Serialize;

use super::{Env, Section};
use crate::claude::merge;
use crate::claude::server::{self, ServerStatus};
use crate::sample::Root;
use crate::sample::cpu::CpuSampler;
use crate::sample::disk::DiskSampler;
use crate::sample::hwmon::Hwmon;
use crate::sample::mem::MemSampler;
use crate::sample::net::NetSampler;
use crate::tokens::indexer::now_ms;
use crate::tokens::{Counts, Family, Indexer, Phase, Progress, TokenView, sum_families};

/// The usage server is asked at most this often while `--watch` runs (spec §3).
const SERVER_EVERY: Duration = Duration::from_secs(60);
/// `--no-wait` still lets the saved index load; it gives up on that after this long.
const LOAD_WAIT: Duration = Duration::from_secs(10);

/// A section's readings, or why they could not be read (`{"error": "…"}`).
#[derive(Serialize, Debug, Clone, PartialEq)]
#[serde(untagged)]
pub enum Part<T> {
    Ok(T),
    Failed { error: String },
}

impl<T> Part<T> {
    fn failed(e: impl Into<String>) -> Self {
        Part::Failed { error: e.into() }
    }

    pub fn is_failed(&self) -> bool {
        matches!(self, Part::Failed { .. })
    }
}

/// One whole readout: the requested sections only, in schema order.
#[derive(Serialize, Debug, Clone, PartialEq)]
pub struct Document {
    pub schema: u32,
    pub sampled_at: i64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cpu: Option<Part<Cpu>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub memory: Option<Part<Memory>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub disk: Option<Part<Disk>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub network: Option<Part<Network>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tokens: Option<Part<Tokens>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub claude: Option<Part<Claude>>,
}

impl Document {
    pub const SCHEMA: u32 = 2;

    pub fn new(sampled_at: i64) -> Self {
        Document {
            schema: Self::SCHEMA,
            sampled_at,
            cpu: None,
            memory: None,
            disk: None,
            network: None,
            tokens: None,
            claude: None,
        }
    }

    /// True if any requested section carries an error (exit code 1).
    pub fn any_failed(&self) -> bool {
        self.cpu.as_ref().is_some_and(Part::is_failed)
            || self.memory.as_ref().is_some_and(Part::is_failed)
            || self.disk.as_ref().is_some_and(Part::is_failed)
            || self.network.as_ref().is_some_and(Part::is_failed)
            || self.tokens.as_ref().is_some_and(Part::is_failed)
            || self.claude.as_ref().is_some_and(Part::is_failed)
    }
}

#[derive(Serialize, Debug, Clone, PartialEq)]
pub struct Cpu {
    pub percent: f64,
    pub threads: Vec<f64>,
    pub load: [f64; 3],
    pub freq_ghz: Option<f64>,
    pub temp_c: Option<f64>,
}

#[derive(Serialize, Debug, Clone, PartialEq)]
pub struct Memory {
    pub total_bytes: u64,
    pub used_bytes: u64,
    pub available_bytes: u64,
    pub cached_bytes: u64,
    pub buffers_bytes: u64,
    pub used_percent: f64,
    pub swap_total_bytes: u64,
    pub swap_used_bytes: u64,
    pub swap_percent: f64,
}

#[derive(Serialize, Debug, Clone, PartialEq)]
pub struct Disk {
    pub read_bps: u64,
    pub write_bps: u64,
    pub busy_percent: f64,
    pub temp_c: Option<f64>,
    pub devices: Vec<Device>,
    pub mounts: Vec<MountInfo>,
}

#[derive(Serialize, Debug, Clone, PartialEq)]
pub struct Device {
    pub name: String,
    pub read_bps: u64,
    pub write_bps: u64,
    pub busy_percent: f64,
}

#[derive(Serialize, Debug, Clone, PartialEq)]
pub struct MountInfo {
    pub target: String,
    pub source: String,
    pub fstype: String,
    pub disk: String,
    pub total_bytes: u64,
    pub used_bytes: u64,
    pub avail_bytes: u64,
    pub used_percent: f64,
}

#[derive(Serialize, Debug, Clone, PartialEq)]
pub struct Network {
    pub down_bps: u64,
    pub up_bps: u64,
    pub peak_down_bps: u64,
    pub peak_up_bps: u64,
    pub interfaces: Vec<Interface>,
}

#[derive(Serialize, Debug, Clone, PartialEq)]
pub struct Interface {
    pub name: String,
    pub down_bps: u64,
    pub up_bps: u64,
    pub rx_bytes: u64,
    pub tx_bytes: u64,
    /// True if it counts toward the totals above (config applied).
    pub included: bool,
}

/// The four token counts.
#[derive(Serialize, Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Tok {
    pub input: u64,
    pub cache_write: u64,
    pub cache_read: u64,
    pub output: u64,
}

impl From<Counts> for Tok {
    fn from(c: Counts) -> Self {
        Tok {
            input: c.input,
            cache_write: c.cache_write,
            cache_read: c.cache_read,
            output: c.output,
        }
    }
}

impl Tok {
    pub fn all(&self) -> u64 {
        self.input + self.cache_write + self.cache_read + self.output
    }
}

#[derive(Serialize, Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct ByModel {
    pub opus: Tok,
    pub sonnet: Tok,
    pub haiku: Tok,
    pub fable: Tok,
    pub other: Tok,
}

impl ByModel {
    /// Name and counts in schema order.
    pub fn rows(&self) -> [(&'static str, Tok); 5] {
        [
            ("opus", self.opus),
            ("sonnet", self.sonnet),
            ("haiku", self.haiku),
            ("fable", self.fable),
            ("other", self.other),
        ]
    }
}

#[derive(Serialize, Debug, Clone, PartialEq)]
pub struct IndexState {
    /// `loading`, `backfill` or `live`.
    pub phase: &'static str,
    pub percent: f64,
}

#[derive(Serialize, Debug, Clone, PartialEq)]
pub struct Tokens {
    pub today: Tok,
    pub last_hour: Tok,
    pub per_min_5m: Tok,
    pub by_model_today: ByModel,
    pub index: IndexState,
}

#[derive(Serialize, Debug, Clone, PartialEq)]
pub struct Window {
    pub percent: f64,
    pub resets_at: Option<i64>,
    pub elapsed_percent: Option<f64>,
}

#[derive(Serialize, Debug, Clone, PartialEq)]
pub struct DayPart {
    pub index: i64,
    pub percent: f64,
}

#[derive(Serialize, Debug, Clone, PartialEq)]
pub struct WeekWindow {
    pub percent: f64,
    pub resets_at: Option<i64>,
    pub elapsed_percent: Option<f64>,
    pub day_part: Option<DayPart>,
}

#[derive(Serialize, Debug, Clone, PartialEq)]
pub struct ModelWeek {
    pub label: String,
    pub percent: f64,
    pub resets_at: Option<i64>,
}

#[derive(Serialize, Debug, Clone, PartialEq)]
pub struct Claude {
    /// `feed` or `server`.
    pub source: &'static str,
    pub read_at: i64,
    pub stale: bool,
    pub session: Option<Window>,
    pub week: Option<WeekWindow>,
    pub models: Vec<ModelWeek>,
}

/// Rounds to `places` decimals (a float that prints as `41.2`, not `41.20000076`).
fn round(x: f64, places: i32) -> f64 {
    let k = 10f64.powi(places);
    (x * k).round() / k
}

fn r1(x: impl Into<f64>) -> f64 {
    round(x.into(), 1)
}

fn whole(x: f64) -> u64 {
    x.max(0.0).round() as u64
}

/// A sampler that may be off (section not requested) or may have failed to open or to read.
struct Slot<T> {
    sampler: Option<T>,
    error: Option<String>,
}

impl<T> Slot<T> {
    fn new(wanted: bool, opened: impl FnOnce() -> Result<T, String>) -> Self {
        if !wanted {
            return Slot {
                sampler: None,
                error: None,
            };
        }
        match opened() {
            Ok(s) => Slot {
                sampler: Some(s),
                error: None,
            },
            Err(e) => Slot {
                sampler: None,
                error: Some(e),
            },
        }
    }

    fn sample(&mut self, read: impl FnOnce(&mut T) -> io::Result<()>, what: &str) {
        if let Some(s) = &mut self.sampler {
            self.error = read(s).err().map(|e| format!("cannot read {what}: {e}"));
        }
    }
}

fn open_err(root: &std::path::Path, file: &str, e: io::Error) -> String {
    format!("cannot read {}: {e}", root.join(file).display())
}

/// Opens the samplers (and the token indexer) for the requested sections and turns them into a
/// [`Document`] on demand.
pub struct Collector {
    want: Vec<Section>,
    cpu: Slot<CpuSampler>,
    mem: Slot<MemSampler>,
    disk: Slot<DiskSampler>,
    net: Slot<NetSampler>,
    hwmon: Hwmon,
    indexer: Result<Option<Indexer>, String>,
    view: TokenView,
    feed_dir: Option<PathBuf>,
    usage_url: Option<String>,
    server: Option<(Instant, Result<ServerStatus, String>)>,
}

impl Collector {
    pub fn new(want: &[Section], env: &Env) -> Self {
        let has = |s: Section| want.contains(&s);
        let Root { proc, .. } = &env.root;
        let indexer = if has(Section::Tokens) {
            Indexer::spawn(env.indexer.clone())
                .map(Some)
                .map_err(|e| format!("cannot start the token indexer: {e}"))
        } else {
            Ok(None)
        };
        Collector {
            want: want.to_vec(),
            cpu: Slot::new(has(Section::Cpu), || {
                CpuSampler::new(&env.root).map_err(|e| open_err(proc, "stat", e))
            }),
            mem: Slot::new(has(Section::Memory), || {
                MemSampler::new(&env.root).map_err(|e| open_err(proc, "meminfo", e))
            }),
            disk: Slot::new(has(Section::Disk), || {
                DiskSampler::new(&env.root).map_err(|e| open_err(proc, "diskstats", e))
            }),
            net: Slot::new(has(Section::Network), || {
                NetSampler::new(&env.root)
                    .map(|mut n| {
                        n.set_overrides(&env.net_include, &env.net_exclude);
                        n
                    })
                    .map_err(|e| open_err(proc, "net/dev", e))
            }),
            hwmon: Hwmon::new(&env.root),
            indexer,
            view: TokenView::new(),
            feed_dir: env.feed_dir.clone(),
            usage_url: env.usage_url.clone(),
            server: None,
        }
    }

    /// True if any requested section is a rate (needs two samples one interval apart).
    pub fn needs_two_samples(&self) -> bool {
        self.want
            .iter()
            .any(|s| matches!(s, Section::Cpu | Section::Disk | Section::Network))
    }

    /// Reads every requested `/proc` and `/sys` source once.
    pub fn sample(&mut self, now: Instant) {
        self.cpu.sample(|s| s.sample(), "the CPU counters");
        self.mem.sample(|s| s.sample(), "meminfo");
        self.disk.sample(|s| s.sample(now), "diskstats");
        self.net.sample(|s| s.sample(now), "net/dev");
        let _ = self.hwmon.sample();
        if let Ok(Some(ix)) = &self.indexer {
            ix.drain_into(&mut self.view);
        }
    }

    /// Waits for the token index: until it has caught up (progress on stderr), or with `no_wait` only
    /// until the saved index is loaded.
    pub fn wait_for_tokens(&mut self, no_wait: bool) {
        let Ok(Some(ix)) = &self.indexer else { return };
        if no_wait {
            let give_up = Instant::now() + LOAD_WAIT;
            while Instant::now() < give_up {
                ix.drain_into(&mut self.view);
                if self
                    .view
                    .progress()
                    .is_some_and(|p| p.phase != Phase::Loading)
                {
                    break;
                }
                std::thread::sleep(Duration::from_millis(10));
            }
        } else {
            wait_for_backfill(ix, &mut self.view);
        }
    }

    /// Stops the indexer thread (it saves the index on the way out).
    pub fn finish(self) {
        if let Ok(Some(ix)) = self.indexer {
            ix.shutdown();
        }
    }

    /// The token buckets as far as they have been indexed (the web server's history).
    pub fn tokens_view(&self) -> &TokenView {
        &self.view
    }

    /// The readings as of now.
    pub fn document(&mut self) -> Document {
        let now = now_ms();
        let mut doc = Document::new(now);
        for s in self.want.clone() {
            match s {
                Section::Cpu => doc.cpu = Some(self.cpu_part()),
                Section::Memory => doc.memory = Some(self.mem_part()),
                Section::Disk => doc.disk = Some(self.disk_part()),
                Section::Network => doc.network = Some(self.net_part()),
                Section::Tokens => doc.tokens = Some(self.tokens_part(now)),
                Section::Claude => doc.claude = Some(self.claude_part(now)),
            }
        }
        doc
    }

    fn cpu_part(&self) -> Part<Cpu> {
        let Some(cpu) = &self.cpu.sampler else {
            return Part::failed(self.cpu.error.clone().unwrap_or_default());
        };
        if let Some(e) = &self.cpu.error {
            return Part::failed(e.clone());
        }
        let [l1, l5, l15] = cpu.load();
        Part::Ok(Cpu {
            percent: r1(cpu.mean()),
            threads: cpu.percent().iter().map(|&p| r1(p)).collect(),
            load: [
                round(l1.into(), 2),
                round(l5.into(), 2),
                round(l15.into(), 2),
            ],
            freq_ghz: cpu.freq_ghz().map(|f| round(f.into(), 2)),
            temp_c: self.hwmon.cpu_c().map(r1),
        })
    }

    fn mem_part(&self) -> Part<Memory> {
        let Some(mem) = &self.mem.sampler else {
            return Part::failed(self.mem.error.clone().unwrap_or_default());
        };
        if let Some(e) = &self.mem.error {
            return Part::failed(e.clone());
        }
        let m = mem.mem();
        Part::Ok(Memory {
            total_bytes: m.total,
            used_bytes: m.used(),
            available_bytes: m.available,
            cached_bytes: m.cached,
            buffers_bytes: m.buffers,
            used_percent: r1(m.used_percent()),
            swap_total_bytes: m.swap_total,
            swap_used_bytes: m.swap_used(),
            swap_percent: r1(m.swap_percent()),
        })
    }

    fn disk_part(&self) -> Part<Disk> {
        let Some(disk) = &self.disk.sampler else {
            return Part::failed(self.disk.error.clone().unwrap_or_default());
        };
        if let Some(e) = &self.disk.error {
            return Part::failed(e.clone());
        }
        let r = disk.rates();
        Part::Ok(Disk {
            read_bps: whole(r.read_bps),
            write_bps: whole(r.write_bps),
            busy_percent: r1(r.busy_pct),
            temp_c: self.hwmon.nvme_c().map(r1),
            devices: disk
                .devices()
                .iter()
                .map(|d| Device {
                    name: d.name.clone(),
                    read_bps: whole(d.read_bps),
                    write_bps: whole(d.write_bps),
                    busy_percent: r1(d.busy_pct),
                })
                .collect(),
            mounts: disk
                .mounts()
                .iter()
                .map(|m| MountInfo {
                    target: m.target.clone(),
                    source: m.source.clone(),
                    fstype: m.fstype.clone(),
                    disk: m.disk.clone(),
                    total_bytes: m.total_bytes,
                    used_bytes: m.used_bytes,
                    avail_bytes: m.avail_bytes,
                    used_percent: r1(m.used_percent()),
                })
                .collect(),
        })
    }

    fn net_part(&self) -> Part<Network> {
        let Some(net) = &self.net.sampler else {
            return Part::failed(self.net.error.clone().unwrap_or_default());
        };
        if let Some(e) = &self.net.error {
            return Part::failed(e.clone());
        }
        let (t, p) = (net.total(), net.peak());
        Part::Ok(Network {
            down_bps: whole(t.down_bps),
            up_bps: whole(t.up_bps),
            peak_down_bps: whole(p.down_bps),
            peak_up_bps: whole(p.up_bps),
            interfaces: net
                .interfaces()
                .map(|i| {
                    let r = i.rate();
                    let (rx, tx) = i.counters();
                    Interface {
                        name: i.name.clone(),
                        down_bps: whole(r.down_bps),
                        up_bps: whole(r.up_bps),
                        rx_bytes: rx,
                        tx_bytes: tx,
                        included: i.included(),
                    }
                })
                .collect(),
        })
    }

    fn tokens_part(&self, now: i64) -> Part<Tokens> {
        if let Err(e) = &self.indexer {
            return Part::failed(e.clone());
        }
        let by = self.view.today(now);
        let get = |f: Family| Tok::from(by[f.index()]);
        let per_min = self.view.recent(now, 5);
        let avg = |n: u64| whole(n as f64 / 5.0);
        Part::Ok(Tokens {
            today: sum_families(&by).into(),
            last_hour: sum_families(&self.view.sum_since(now - 3_600_000, now)).into(),
            per_min_5m: Tok {
                input: avg(per_min.input),
                cache_write: avg(per_min.cache_write),
                cache_read: avg(per_min.cache_read),
                output: avg(per_min.output),
            },
            by_model_today: ByModel {
                opus: get(Family::Opus),
                sonnet: get(Family::Sonnet),
                haiku: get(Family::Haiku),
                fable: get(Family::Fable),
                other: get(Family::Other),
            },
            index: index_state(self.view.progress()),
        })
    }

    /// The feed file, then the usage server; never `claude`. The usage server is asked again only after a minute.
    fn claude_part(&mut self, now: i64) -> Part<Claude> {
        let feed = self.feed_dir.as_deref().and_then(feed::read_from);
        if let Some(url) = &self.usage_url
            && self
                .server
                .as_ref()
                .is_none_or(|(at, _)| at.elapsed() >= SERVER_EVERY)
        {
            self.server = Some((Instant::now(), server::fetch(url)));
        }
        let server_ok = self.server.as_ref().and_then(|(_, r)| r.as_ref().ok());
        let Some(m) = merge(feed.as_ref(), server_ok, now / 1000) else {
            let server_says = match self.server.as_ref().map(|(_, r)| r) {
                Some(Err(e)) => format!("server: {e}"),
                Some(Ok(h)) => format!("server: {}", h.error.as_deref().unwrap_or("no reading")),
                None => "server: not asked".to_owned(),
            };
            let feed_says = if feed.is_some() { "empty" } else { "missing" };
            return Part::failed(format!("no reading (feed {feed_says}; {server_says})"));
        };
        let r = &m.reading;
        let week = r.week.as_ref().map(|w| {
            let day = pace::day_part(w.resets_at, now);
            WeekWindow {
                percent: r1(w.percent),
                resets_at: w.resets_at,
                elapsed_percent: pace::elapsed_percent(Kind::Week, w.resets_at, now).map(r1),
                day_part: day.map(|d| DayPart {
                    index: d.index,
                    percent: r1(pace::daily_percent(w.percent, d.index)),
                }),
            }
        });
        Part::Ok(Claude {
            source: m.source.name(),
            read_at: m.read_at_ms,
            stale: m.stale,
            session: r.session.as_ref().map(|s| Window {
                percent: r1(s.percent),
                resets_at: s.resets_at,
                elapsed_percent: pace::elapsed_percent(Kind::Session, s.resets_at, now).map(r1),
            }),
            week,
            models: r
                .models
                .iter()
                .map(|l| ModelWeek {
                    label: l.label.clone(),
                    percent: r1(l.percent),
                    resets_at: l.resets_at,
                })
                .collect(),
        })
    }
}

fn index_state(p: Option<&Progress>) -> IndexState {
    match p {
        None => IndexState {
            phase: "loading",
            percent: 0.0,
        },
        Some(p) => IndexState {
            phase: match p.phase {
                Phase::Loading => "loading",
                Phase::Backfill => "backfill",
                Phase::Live => "live",
            },
            percent: f64::from(p.percent()),
        },
    }
}

/// Applies the indexer's messages until it has caught up, with progress on stderr.
pub fn wait_for_backfill(indexer: &Indexer, view: &mut TokenView) {
    use std::sync::mpsc::RecvTimeoutError;
    let mut last_report = Instant::now();
    loop {
        match indexer.receiver().recv_timeout(Duration::from_millis(250)) {
            Ok(msg) => view.apply(msg),
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => break,
        }
        indexer.drain_into(view);
        let Some(p) = view.progress() else { continue };
        if p.phase == Phase::Live {
            break;
        }
        if last_report.elapsed() >= Duration::from_secs(2) {
            eprintln!(
                "indexing... {}%  ({}/{} files)",
                p.percent(),
                p.files_done,
                p.files_total
            );
            last_report = Instant::now();
        }
    }
}
