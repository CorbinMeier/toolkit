// SPDX-License-Identifier: MIT
//! The sampler thread and what it shares with the client threads (spec §14.4): the latest sample as one
//! JSON string serialized once per tick, a ring of compact history points, and the token buckets.
//! Clients wait on a condvar and copy an `Arc<str>`; nothing is serialized per client.

use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use serde_json::{Value, json};

use crate::readout::data::Collector;
use crate::tokens::indexer::now_ms;
use crate::tokens::{TokenView, TokenWindow, sum_families};

/// The token buckets are re-sent to every client this often.
const TOKENS_EVERY: Duration = Duration::from_secs(30);

pub struct State {
    /// Counts samples; a client that has seen `seq` waits for a larger one.
    pub seq: u64,
    pub sample: Option<Arc<str>>,
    pub tokens_seq: u64,
    pub tokens: Arc<str>,
    history: VecDeque<Arc<str>>,
    pub clients: usize,
}

/// What a waiting client gets next.
pub enum Wake {
    Sample(Arc<str>, u64),
    Tokens(Arc<str>, u64),
    /// Nothing happened within the timeout: send a comment line.
    Idle,
    Stop,
}

pub struct Shared {
    state: Mutex<State>,
    cv: Condvar,
    stop: AtomicBool,
    cap: usize,
    max_clients: usize,
    interval_ms: u64,
    /// `"layout":…,"theme":…` as JSON members, fixed for the run.
    meta: String,
}

/// Counts a connected stream client; drop it when the client goes.
pub struct ClientGuard(Arc<Shared>);

impl Drop for ClientGuard {
    fn drop(&mut self) {
        self.0.lock().clients -= 1;
    }
}

impl Shared {
    pub fn new(
        interval_ms: u64,
        history_min: u32,
        max_clients: usize,
        meta: String,
    ) -> Arc<Shared> {
        let cap = (u64::from(history_min) * 60_000 / interval_ms.max(1)).max(2) as usize;
        Arc::new(Shared {
            state: Mutex::new(State {
                seq: 0,
                sample: None,
                tokens_seq: 0,
                tokens: Arc::from("{}"),
                history: VecDeque::with_capacity(cap),
                clients: 0,
            }),
            cv: Condvar::new(),
            stop: AtomicBool::new(false),
            cap,
            max_clients,
            interval_ms,
            meta,
        })
    }

    fn lock(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }

    pub fn stopped(&self) -> bool {
        self.stop.load(Ordering::Relaxed)
    }

    pub fn stop(&self) {
        self.stop.store(true, Ordering::Relaxed);
        self.cv.notify_all();
    }

    /// The newest sample, once there is one.
    pub fn latest(&self) -> Option<Arc<str>> {
        self.lock().sample.clone()
    }

    /// Blocks until the first sample exists (or the timeout passes).
    pub fn wait_ready(&self, timeout: Duration) -> bool {
        let g = self.lock();
        let (g, _) = self
            .cv
            .wait_timeout_while(g, timeout, |s| s.sample.is_none())
            .unwrap_or_else(|e| e.into_inner());
        g.sample.is_some()
    }

    fn publish(&self, sample: String, point: String, tokens: Option<String>) {
        let mut g = self.lock();
        g.sample = Some(Arc::from(sample));
        g.seq += 1;
        if g.history.len() == self.cap {
            g.history.pop_front();
        }
        g.history.push_back(Arc::from(point));
        if let Some(t) = tokens {
            g.tokens = Arc::from(t);
            g.tokens_seq += 1;
        }
        drop(g);
        self.cv.notify_all();
    }

    /// Takes a client slot, or `None` when the cap is reached.
    pub fn join(self: &Arc<Self>) -> Option<ClientGuard> {
        let mut g = self.lock();
        if g.clients >= self.max_clients {
            return None;
        }
        g.clients += 1;
        Some(ClientGuard(Arc::clone(self)))
    }

    pub fn clients(&self) -> usize {
        self.lock().clients
    }

    /// The `history` event's data and the sequence numbers it is current to. `https` says whether the
    /// client is on TLS; `tls` whether this server has a certificate at all.
    pub fn history_event(&self, tls: bool) -> (String, u64, u64) {
        let g = self.lock();
        let points: Vec<&str> = g.history.iter().map(|p| &**p).collect();
        let data = format!(
            "{{\"interval_ms\":{},\"history_min\":{},\"tls\":{tls},\"now\":{},{},\"tokens\":{},\"points\":[{}]}}",
            self.interval_ms,
            (self.cap as u64 * self.interval_ms).div_ceil(60_000),
            now_ms(),
            self.meta,
            g.tokens,
            points.join(",")
        );
        (data, g.seq, g.tokens_seq)
    }

    /// Waits for something newer than `(seq, tokens_seq)`, at most `timeout`.
    pub fn wait_next(&self, seq: u64, tokens_seq: u64, timeout: Duration) -> Wake {
        let g = self.lock();
        let (g, _) = self
            .cv
            .wait_timeout_while(g, timeout, |s| {
                !self.stopped() && s.seq <= seq && s.tokens_seq <= tokens_seq
            })
            .unwrap_or_else(|e| e.into_inner());
        if self.stopped() {
            Wake::Stop
        } else if g.seq > seq {
            Wake::Sample(g.sample.clone().unwrap_or_else(|| Arc::from("{}")), g.seq)
        } else if g.tokens_seq > tokens_seq {
            Wake::Tokens(g.tokens.clone(), g.tokens_seq)
        } else {
            Wake::Idle
        }
    }
}

/// One history point from a sample's JSON: just the figures the timelines draw.
pub fn point(v: &Value) -> String {
    let f = |p: &str| v.pointer(p).cloned().unwrap_or(Value::Null);
    let mut ifs = serde_json::Map::new();
    if let Some(list) = v.pointer("/network/interfaces").and_then(Value::as_array) {
        for i in list {
            if let Some(name) = i.get("name").and_then(Value::as_str) {
                ifs.insert(name.to_owned(), json!([i["down_bps"], i["up_bps"]]));
            }
        }
    }
    json!({
        "t": f("/sampled_at"),
        "cpu": f("/cpu/percent"),
        "th": f("/cpu/threads"),
        "mem": f("/memory/used_percent"),
        "dr": f("/disk/read_bps"),
        "dw": f("/disk/write_bps"),
        "busy": f("/disk/busy_percent"),
        "nd": f("/network/down_bps"),
        "nu": f("/network/up_bps"),
        "if": ifs,
    })
    .to_string()
}

/// Sparse token buckets: `[bucket_start_ms, new_input, output, cache_read]` for every non-empty minute of
/// the last 24 h (`min`) and hour of the last 30 days (`hour`).
pub fn tokens_json(view: &TokenView, now: i64) -> String {
    let rows = |w: TokenWindow| -> Vec<Value> {
        let size = w.bucket_ms();
        let first = now.div_euclid(size) - w.buckets() as i64 + 1;
        view.series_iter(w, now)
            .enumerate()
            .filter_map(|(i, by)| {
                let c = sum_families(&by);
                (!c.is_zero()).then(|| {
                    json!([
                        (first + i as i64) * size,
                        c.new_input(),
                        c.output,
                        c.cache_read
                    ])
                })
            })
            .collect()
    };
    json!({"min": rows(TokenWindow::H24), "hour": rows(TokenWindow::D30)}).to_string()
}

/// Starts the sampler thread: samples every `interval`, publishes until the server stops. The collector
/// is dropped (the token index saved) on the way out.
pub fn spawn_sampler(
    mut col: Collector,
    shared: Arc<Shared>,
    interval: Duration,
) -> Option<std::thread::JoinHandle<()>> {
    std::thread::Builder::new()
        .name("vitals-sampler".into())
        .spawn(move || {
            let mut next_tokens = Instant::now();
            // Rates need two samples one interval apart.
            col.sample(Instant::now());
            let mut first = true;
            while !shared.stopped() {
                let wake = Instant::now() + interval;
                while !shared.stopped() && Instant::now() < wake {
                    std::thread::sleep(Duration::from_millis(20).min(interval));
                }
                if shared.stopped() {
                    break;
                }
                col.sample(Instant::now());
                let doc = col.document();
                // The same text `vitals --all --json` prints, so a client reads one schema everywhere.
                let sample = crate::readout::json::render(&doc);
                let Ok(v) = serde_json::from_str::<Value>(&sample) else {
                    continue;
                };
                let point = point(&v);
                let tokens = (first || Instant::now() >= next_tokens).then(|| {
                    next_tokens = Instant::now() + TOKENS_EVERY;
                    tokens_json(col.tokens_view(), now_ms())
                });
                first = false;
                shared.publish(sample, point, tokens);
            }
            col.finish();
        })
        .ok()
}
