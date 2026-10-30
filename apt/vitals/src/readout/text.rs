// SPDX-License-Identifier: MIT
//! Plain-text readouts (spec §9): per section a title, the readings as aligned `label  value` lines (the
//! section's bars, spec §4) and the summary line. Human units: `12.6G`, `1.6M/s`, `340K`.

use std::fmt::Write as _;

use chrono::{Local, TimeZone};

use super::data::{Claude, Cpu, Disk, Document, Memory, Network, Part, Tok, Tokens, Window};

/// Bytes with a binary unit: `0B`, `512B`, `12.6G`.
pub fn bytes(n: u64) -> String {
    if n < 1024 {
        return format!("{n}B");
    }
    let mut v = n as f64;
    let mut i = 0;
    while v >= 1024.0 && i < 4 {
        v /= 1024.0;
        i += 1;
    }
    format!("{v:.1}{}", ["B", "K", "M", "G", "T"][i])
}

/// Bytes per second: `1.6M/s`.
pub fn rate(n: u64) -> String {
    format!("{}/s", bytes(n))
}

/// Token counts: `12`, `340.0K`, `1.2M`, `1.25G`.
pub fn tokens(n: u64) -> String {
    match n {
        0..1_000 => n.to_string(),
        1_000..1_000_000 => format!("{:.1}K", n as f64 / 1e3),
        1_000_000..1_000_000_000 => format!("{:.1}M", n as f64 / 1e6),
        _ => format!("{:.2}G", n as f64 / 1e9),
    }
}

/// One section under construction: rows are `label  value`, aligned when it is finished.
struct Block {
    title: &'static str,
    rows: Vec<(String, String)>,
    summary: String,
}

impl Block {
    fn new(title: &'static str) -> Self {
        Block {
            title,
            rows: Vec::new(),
            summary: String::new(),
        }
    }

    fn row(&mut self, label: impl Into<String>, value: impl Into<String>) {
        self.rows.push((label.into(), value.into()));
    }

    /// Joins the parts with ` · `, as the TUI's summary line does.
    fn summary(&mut self, parts: &[String]) {
        self.summary = parts
            .iter()
            .filter(|p| !p.is_empty())
            .cloned()
            .collect::<Vec<_>>()
            .join(" · ");
    }

    fn finish(&self, out: &mut String) {
        let _ = writeln!(out, "{}", self.title);
        let w = self
            .rows
            .iter()
            .map(|r| r.0.chars().count())
            .max()
            .unwrap_or(0);
        for (label, value) in &self.rows {
            let _ = writeln!(out, "{label:<w$}  {value}");
        }
        if !self.summary.is_empty() {
            let _ = writeln!(out, "{}", self.summary);
        }
    }
}

/// The whole document: sections in schema order, a blank line between them.
pub fn render(doc: &Document) -> String {
    let mut blocks = Vec::new();
    if let Some(p) = &doc.cpu {
        blocks.push(part("CPU", p, cpu));
    }
    if let Some(p) = &doc.memory {
        blocks.push(part("Memory", p, memory));
    }
    if let Some(p) = &doc.disk {
        blocks.push(part("Disk", p, disk));
    }
    if let Some(p) = &doc.network {
        blocks.push(part("Network", p, network));
    }
    if let Some(p) = &doc.tokens {
        blocks.push(part("Tokens", p, tokens_block));
    }
    if let Some(p) = &doc.claude {
        blocks.push(part("Claude", p, |c| claude(c, doc.sampled_at)));
    }
    let mut out = String::new();
    for (i, b) in blocks.iter().enumerate() {
        if i > 0 {
            out.push('\n');
        }
        b.finish(&mut out);
    }
    out
}

fn part<T>(title: &'static str, p: &Part<T>, build: impl FnOnce(&T) -> Block) -> Block {
    match p {
        Part::Ok(v) => build(v),
        Part::Failed { error } => {
            let mut b = Block::new(title);
            b.row("error", error.clone());
            b
        }
    }
}

fn cpu(c: &Cpu) -> Block {
    let mut b = Block::new("CPU");
    b.row("all", format!("{:5.1}%", c.percent));
    for (i, t) in c.threads.iter().enumerate() {
        b.row(format!("cpu{i}"), format!("{t:5.1}%"));
    }
    let [l1, l5, l15] = c.load;
    b.summary(&[
        c.freq_ghz.map_or(String::new(), |f| format!("{f:.1}GHz")),
        c.temp_c.map_or(String::new(), |t| format!("{t:.0}°C")),
        format!("load {l1:.2} {l5:.2} {l15:.2}"),
        format!("{} threads", c.threads.len()),
    ]);
    b
}

fn memory(m: &Memory) -> Block {
    let pct = |n: u64, of: u64| {
        if of == 0 {
            0.0
        } else {
            n as f64 / of as f64 * 100.0
        }
    };
    let mut b = Block::new("Memory");
    let line = |n: u64, of: u64| format!("{:>7}  {:5.1}%", bytes(n), pct(n, of));
    b.row("used", line(m.used_bytes, m.total_bytes));
    b.row("cache", line(m.cached_bytes, m.total_bytes));
    b.row("available", line(m.available_bytes, m.total_bytes));
    b.row("swap", line(m.swap_used_bytes, m.swap_total_bytes));
    b.summary(&[
        format!("{} / {}", bytes(m.used_bytes), bytes(m.total_bytes)),
        format!("cache {}", bytes(m.cached_bytes)),
        format!("available {}", bytes(m.available_bytes)),
        format!(
            "swap {} / {}",
            bytes(m.swap_used_bytes),
            bytes(m.swap_total_bytes)
        ),
    ]);
    b
}

fn disk(d: &Disk) -> Block {
    let mut b = Block::new("Disk");
    for m in &d.mounts {
        b.row(
            m.target.clone(),
            format!(
                "{:5.1}%  {} / {}",
                m.used_percent,
                bytes(m.used_bytes),
                bytes(m.total_bytes)
            ),
        );
    }
    for v in &d.devices {
        b.row(
            v.name.clone(),
            format!(
                "busy {:5.1}%  read {}  write {}",
                v.busy_percent,
                rate(v.read_bps),
                rate(v.write_bps)
            ),
        );
    }
    let root_free = d
        .mounts
        .iter()
        .find(|m| m.target == "/")
        .map_or(String::new(), |m| {
            format!("/ free {}", bytes(m.avail_bytes))
        });
    b.summary(&[
        format!("read {}", rate(d.read_bps)),
        format!("write {}", rate(d.write_bps)),
        format!("busy {:.1}%", d.busy_percent),
        d.temp_c.map_or(String::new(), |t| format!("NVMe {t:.0}°C")),
        root_free,
    ]);
    b
}

fn network(n: &Network) -> Block {
    let mut b = Block::new("Network");
    b.row(
        "all".to_owned(),
        format!("down {}  up {}", rate(n.down_bps), rate(n.up_bps)),
    );
    for i in &n.interfaces {
        b.row(
            format!("{} {}", if i.included { "[x]" } else { "[ ]" }, i.name),
            format!("down {}  up {}", rate(i.down_bps), rate(i.up_bps)),
        );
    }
    let on = n.interfaces.iter().filter(|i| i.included);
    let (rx, tx) = on
        .clone()
        .fold((0, 0), |(a, b), i| (a + i.rx_bytes, b + i.tx_bytes));
    b.summary(&[
        format!("down {}", rate(n.down_bps)),
        format!("up {}", rate(n.up_bps)),
        format!("since boot: down {} up {}", bytes(rx), bytes(tx)),
        on.map(|i| i.name.as_str()).collect::<Vec<_>>().join(","),
    ]);
    b
}

fn tokens_block(t: &Tokens) -> Block {
    let mut b = Block::new("Tokens");
    let per = &t.per_min_5m;
    let min = |n: u64| format!("{}/min", tokens(n));
    b.row("input", min(per.input));
    b.row("output", min(per.output));
    b.row("cache write", min(per.cache_write));
    b.row("cache read", min(per.cache_read));
    let total = t.today.all();
    for (name, tok) in t.by_model_today.rows() {
        if tok.all() > 0 {
            let share = tok.all() as f64 / total as f64 * 100.0;
            b.row(name, format!("{share:5.1}%  {}", tokens(tok.all())));
        }
    }
    let Tok {
        input,
        cache_write,
        cache_read,
        output,
    } = t.today;
    b.summary(&[
        format!(
            "today in {} · out {} · cache {}",
            tokens(input),
            tokens(output),
            tokens(cache_write + cache_read)
        ),
        format!("{}/min", tokens(per.all())),
        if t.index.phase == "live" {
            String::new()
        } else {
            format!("indexing… {:.0}%", t.index.percent)
        },
    ]);
    b
}

/// A clock time: `2:14 PM`, with the weekday when it is not today.
fn clock(ms: i64, now_ms: i64) -> String {
    let (Some(t), Some(n)) = (
        Local.timestamp_millis_opt(ms).single(),
        Local.timestamp_millis_opt(now_ms).single(),
    ) else {
        return String::new();
    };
    if t.date_naive() == n.date_naive() {
        t.format("%-I:%M %p").to_string()
    } else {
        t.format("%a %-I:%M %p").to_string()
    }
}

fn age(ms: i64, now_ms: i64) -> String {
    let s = ((now_ms - ms) / 1000).max(0);
    match s {
        0..60 => format!("{s}s"),
        60..3600 => format!("{}m", s / 60),
        3600..86_400 => format!("{}h", s / 3600),
        _ => format!("{}d", s / 86_400),
    }
}

fn window_value(w: &Window, now: i64) -> String {
    let mut v = format!("{:5.1}%", w.percent);
    if let Some(at) = w.resets_at {
        let _ = write!(
            v,
            "  resets {} (in {})",
            clock(at, now),
            claude_usage_core::words::reset_short(Some(at), now)
        );
    }
    if let Some(e) = w.elapsed_percent {
        let _ = write!(v, "  {e:.0}% of the window gone");
    }
    v
}

fn claude(c: &Claude, now: i64) -> Block {
    let mut b = Block::new("Claude");
    if let Some(s) = &c.session {
        b.row("session", window_value(s, now));
    }
    if let Some(w) = &c.week {
        let as_window = Window {
            percent: w.percent,
            resets_at: w.resets_at,
            elapsed_percent: w.elapsed_percent,
        };
        b.row("week", window_value(&as_window, now));
        if let Some(d) = &w.day_part {
            b.row(
                "day",
                format!("{:5.1}%  day {} of 7", d.percent, d.index + 1),
            );
        }
    }
    for m in &c.models {
        let mut v = format!("{:5.1}%", m.percent);
        if let Some(at) = m.resets_at {
            let _ = write!(v, "  resets {}", clock(at, now));
        }
        b.row(m.label.clone(), v);
    }
    let resets = |label: &str, at: Option<i64>| {
        at.map_or(String::new(), |at| {
            format!("{label} resets {}", clock(at, now))
        })
    };
    b.summary(&[
        resets("session", c.session.as_ref().and_then(|s| s.resets_at)),
        resets("week", c.week.as_ref().and_then(|w| w.resets_at)),
        format!("{}, {} ago", c.source, age(c.read_at, now)),
        if c.stale {
            claude_usage_core::words::stale_words(c.read_at)
        } else {
            String::new()
        },
    ]);
    b
}
