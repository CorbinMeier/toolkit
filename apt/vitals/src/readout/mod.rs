// SPDX-License-Identifier: MIT
//! Command-line section readouts (spec §9): `vitals --cpu --json`, `--all`, `--watch` and friends.
//!
//! - [`Request`]: what the flags asked for. [`dispatch`] is the one call `main` makes.
//! - [`data`]: the readings, shaped as JSON schema 2, and the [`data::Collector`] that samples them.
//! - [`text`], [`json`]: the two output formats of one [`data::Document`].

pub mod data;
pub mod json;
pub mod text;

use std::io::{self, Write};
use std::path::PathBuf;
use std::time::{Duration, Instant};

use crate::config;
use crate::sample::Root;
use crate::tokens::IndexerConfig;
use data::{Collector, Document};

pub use data::wait_for_backfill;

/// A section a readout can print, in the order they are printed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Section {
    Cpu,
    Memory,
    Disk,
    Network,
    Tokens,
    Claude,
}

impl Section {
    pub const ALL: [Section; 6] = [
        Section::Cpu,
        Section::Memory,
        Section::Disk,
        Section::Network,
        Section::Tokens,
        Section::Claude,
    ];

    fn bit(self) -> u8 {
        1 << self as u8
    }
}

/// What the readout flags asked for.
#[derive(Debug, Clone, Default)]
pub struct Request {
    sections: u8,
    /// `--all` (or `--dump`): every section; `--no-tokens` drops Tokens from it.
    all: bool,
    pub json: bool,
    pub watch: bool,
    pub no_wait: bool,
    /// Hidden test hook: stop `--watch` after this many readouts.
    pub count: Option<u32>,
}

impl Request {
    /// Takes the flags that need no value. False if `arg` is not one of them.
    pub fn flag(&mut self, arg: &str) -> bool {
        match arg {
            "--cpu" => self.add(Section::Cpu),
            "--mem" | "--memory" => self.add(Section::Memory),
            "--disk" => self.add(Section::Disk),
            "--net" | "--network" => self.add(Section::Network),
            "--tokens" => self.add(Section::Tokens),
            "--claude" => self.add(Section::Claude),
            "--all" | "--dump" => self.all = true,
            "--json" => self.json = true,
            "--watch" => self.watch = true,
            "--no-wait" => self.no_wait = true,
            _ => return false,
        }
        true
    }

    pub fn add(&mut self, s: Section) {
        self.sections |= s.bit();
    }

    /// The sections to print, in schema order.
    pub fn sections(&self, tokens_off: bool) -> Vec<Section> {
        Section::ALL
            .into_iter()
            .filter(|s| {
                self.sections & s.bit() != 0 || (self.all && !(tokens_off && *s == Section::Tokens))
            })
            .collect()
    }

    /// True if a section flag was given: the TUI does not open.
    pub fn requested(&self) -> bool {
        self.all || self.sections != 0
    }

    /// True if a flag that only means something with a section was given.
    fn has_modifiers(&self) -> bool {
        self.json || self.watch || self.no_wait || self.count.is_some()
    }
}

/// Where the readings come from. [`Env::from_environment`] is the live machine; tests build their own.
#[derive(Debug, Clone)]
pub struct Env {
    pub root: Root,
    pub interval: Duration,
    pub indexer: IndexerConfig,
    pub feed_dir: Option<PathBuf>,
    pub usage_url: Option<String>,
    /// The config's `[section.network]` `include` and `exclude` lists (which interfaces `all` sums).
    pub net_include: Vec<String>,
    pub net_exclude: Vec<String>,
}

impl Env {
    /// `/proc`, `/sys`, the real config (for the usage server URL and the interval), `CLAUDE_CONFIG_DIR`,
    /// `XDG_CACHE_HOME` and the applet's feed folder. `interval_ms` is the `--interval` flag.
    pub fn from_environment(interval_ms: Option<u64>) -> Env {
        let get = |k: &str| std::env::var(k).ok();
        let cfg = config::config_dir(&get)
            .map(|d| config::load(&d.join("config.toml")).0)
            .unwrap_or_default();
        let ms = interval_ms.unwrap_or(cfg.interval_ms).clamp(250, 5000);
        Env {
            root: Root::system(),
            interval: Duration::from_millis(ms),
            indexer: IndexerConfig::from_env(),
            feed_dir: claude_usage_core::feed::feed_dir(),
            usage_url: Some(cfg.usage_url().to_owned()),
            net_include: cfg.section.network.include.clone(),
            net_exclude: cfg.section.network.exclude.clone(),
        }
    }
}

/// The one call `main` makes after parsing. `None`: no readout was asked for, open the TUI. `Some(code)`:
/// the readout ran (or the flags were wrong) and this is the exit code (0 ok, 1 a section failed, 2 bad flags).
pub fn dispatch(
    req: &Request,
    interval_ms: Option<u64>,
    tokens_off: bool,
    once: bool,
) -> Option<u8> {
    if !req.requested() {
        if req.has_modifiers() {
            eprintln!(
                "vitals: --json, --watch, --no-wait and --count need a section flag (--cpu, --all, ...)"
            );
            return Some(2);
        }
        return None;
    }
    if once {
        eprintln!("vitals: --once prints a TUI frame; it cannot be combined with section flags");
        return Some(2);
    }
    let want = req.sections(tokens_off);
    if want.is_empty() {
        eprintln!("vitals: no section left to print (--no-tokens removed the only one)");
        return Some(2);
    }
    let env = Env::from_environment(interval_ms);
    let mut out = io::stdout().lock();
    Some(run(req, &want, &env, &mut out))
}

/// Prints the readout(s) to `out`: once, or every interval with `--watch`. Returns the exit code
/// (0, or 1 if any requested section could not be read in any readout).
pub fn run(req: &Request, want: &[Section], env: &Env, out: &mut dyn Write) -> u8 {
    let mut col = Collector::new(want, env);
    let mut failed = false;
    let mut emit = |col: &mut Collector, out: &mut dyn Write| -> io::Result<()> {
        let doc = col.document();
        failed |= doc.any_failed();
        write_doc(&doc, req, out)
    };
    // The indexer has been working since `Collector::new`; rates are measured after it is done waiting.
    col.wait_for_tokens(req.no_wait);
    col.sample(Instant::now());
    let result = (|| -> io::Result<()> {
        if req.watch {
            let mut n = 0u32;
            loop {
                std::thread::sleep(env.interval);
                col.sample(Instant::now());
                emit(&mut col, out)?;
                n += 1;
                if req.count.is_some_and(|c| n >= c) {
                    return Ok(());
                }
            }
        } else {
            if col.needs_two_samples() {
                std::thread::sleep(env.interval);
                col.sample(Instant::now());
            }
            emit(&mut col, out)
        }
    })();
    col.finish();
    match result {
        // A closed pipe (`| head`) is a normal way to stop a watch.
        Err(e) if e.kind() != io::ErrorKind::BrokenPipe => {
            eprintln!("vitals: {e}");
            1
        }
        _ => u8::from(failed),
    }
}

fn write_doc(doc: &Document, req: &Request, out: &mut dyn Write) -> io::Result<()> {
    if req.json {
        writeln!(out, "{}", json::render(doc))?;
    } else {
        write!(out, "{}", text::render(doc))?;
        if req.watch {
            writeln!(out)?;
        }
    }
    out.flush()
}
