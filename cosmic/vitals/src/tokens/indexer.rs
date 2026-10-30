// SPDX-License-Identifier: MIT
//! The token indexer: one background thread at the lowest CPU and I/O priority (spec §3.2).
//!
//! - **Start:** load the saved index (a bad one is deleted and rebuilt), tell the UI everything in it.
//! - **Backfill:** read the files changed in the last 30 days, newest first, in slices of 16 MiB so the
//!   live tail never waits long. Progress is sent as it goes.
//! - **Live:** every 2 s stat the hot files (changed in the last 10 minutes) and read only the new bytes;
//!   every 30 s walk the tree for new files; every 60 s (and on shutdown) save the index.
//!
//! A line is only consumed once its newline arrived, so a half-written last line is held. A file whose
//! inode changed or that became shorter than the saved offset is read again from 0; the dedupe set keeps
//! that from double counting.
//!
//! [`Engine`] is the synchronous core (driven by hand in tests); [`Indexer`] runs it on a thread.

use std::collections::HashMap;
use std::fs;
use std::os::unix::fs::{FileExt, MetadataExt};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, Sender};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use super::index::{DAY_MS, FileState, Index};
use super::scan::scan_lines;
use super::store::{self, LoadError};
use super::view::TokenView;
use super::{CacheState, Msg, Phase, Progress};

/// Read this much per `pread`; a line longer than this makes the read grow.
const BLOCK: usize = 256 << 10;
/// Lines beyond this (a pasted image, say) are skipped: usage lines are a few hundred bytes.
const MAX_LINE: usize = 32 << 20;
/// One backfill step reads at most this much before the loop checks the clock again.
const STEP_BUDGET: u64 = 16 << 20;

/// Folders, intervals and priority of the indexer. [`IndexerConfig::from_env`] gives the real ones.
#[derive(Debug, Clone)]
pub struct IndexerConfig {
    /// `~/.claude/projects` or `$CLAUDE_CONFIG_DIR/projects`.
    pub projects_dir: PathBuf,
    /// `$XDG_CACHE_HOME/vitals/tokens-v1.bin`.
    pub cache_file: PathBuf,
    pub backfill_days: u32,
    pub tail_every: Duration,
    pub rescan_every: Duration,
    pub save_every: Duration,
    /// A file changed within this long is "hot" and checked on every tail.
    pub hot_for: Duration,
    /// Set nice 19 and the idle I/O class on the thread.
    pub low_priority: bool,
}

impl IndexerConfig {
    /// Spec defaults over the given folders.
    pub fn new(projects_dir: PathBuf, cache_file: PathBuf) -> Self {
        IndexerConfig {
            projects_dir,
            cache_file,
            backfill_days: 30,
            tail_every: Duration::from_secs(2),
            rescan_every: Duration::from_secs(30),
            save_every: Duration::from_secs(60),
            hot_for: Duration::from_secs(10 * 60),
            low_priority: true,
        }
    }

    /// Folders from the environment: `CLAUDE_CONFIG_DIR` (else `~/.claude`) and `XDG_CACHE_HOME` (else `~/.cache`).
    pub fn from_env() -> Self {
        let var = |k: &str| {
            std::env::var_os(k)
                .filter(|v| !v.is_empty())
                .map(PathBuf::from)
        };
        let home = var("HOME").unwrap_or_else(|| PathBuf::from("."));
        let claude = var("CLAUDE_CONFIG_DIR").unwrap_or_else(|| home.join(".claude"));
        let cache = var("XDG_CACHE_HOME").unwrap_or_else(|| home.join(".cache"));
        Self::new(claude.join("projects"), cache.join("vitals/tokens-v1.bin"))
    }
}

pub fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_millis() as i64)
}

/// What the directory scan knows about a file (not saved).
#[derive(Debug, Clone, Copy)]
struct FileMeta {
    dev: u64,
    ino: u64,
    size: u64,
    mtime_ms: i64,
    /// Size at which a read made no progress (an unfinished line): not tried again until the file grows.
    stalled_at: Option<u64>,
}

/// The synchronous indexer core.
pub struct Engine {
    cfg: IndexerConfig,
    tx: Sender<Msg>,
    index: Index,
    known: HashMap<PathBuf, FileMeta>,
    /// Files with work to do, newest last (popped from the end).
    queue: Vec<PathBuf>,
    buf: Vec<u8>,
    progress: Progress,
    files_done: u32,
    bytes_done: u64,
    backfill_over: bool,
    /// The receiver is gone: the thread should end.
    pub closed: bool,
}

impl Engine {
    pub fn new(cfg: IndexerConfig, tx: Sender<Msg>) -> Self {
        Engine {
            cfg,
            tx,
            index: Index::new(),
            known: HashMap::new(),
            queue: Vec::new(),
            buf: Vec::new(),
            progress: Progress {
                phase: Phase::Loading,
                cache: CacheState::Fresh,
                files_done: 0,
                files_total: 0,
                bytes_done: 0,
                bytes_total: 0,
            },
            files_done: 0,
            bytes_done: 0,
            backfill_over: false,
            closed: false,
        }
    }

    fn send(&mut self, msg: Msg) {
        if self.tx.send(msg).is_err() {
            self.closed = true;
        }
    }

    /// Loads the saved index and sends it whole to the UI.
    pub fn load(&mut self, now_ms: i64) {
        self.progress.cache = match store::load(&self.cfg.cache_file) {
            Ok(index) => {
                self.index = index;
                self.index.prune(now_ms);
                CacheState::Loaded
            }
            Err(LoadError::Missing) => CacheState::Fresh,
            Err(LoadError::Bad(_)) => CacheState::Rebuilt,
        };
        let full = self.index.full_delta(now_ms);
        self.send(Msg::Progress(self.progress));
        self.send(Msg::Delta(full));
    }

    /// Walks the transcript folder, updates what is known about each file and rebuilds the work queue.
    pub fn rescan(&mut self, now_ms: i64) {
        let mut found: Vec<(PathBuf, fs::Metadata)> = Vec::new();
        walk(&self.cfg.projects_dir, &mut found);
        let mut known = HashMap::with_capacity(found.len());
        for (path, meta) in found {
            let stalled_at = self.known.get(&path).and_then(|k| {
                k.stalled_at
                    .filter(|&s| s == meta.len() && k.ino == meta.ino())
            });
            known.insert(
                path,
                FileMeta {
                    dev: meta.dev(),
                    ino: meta.ino(),
                    size: meta.len(),
                    mtime_ms: meta.mtime() * 1000 + meta.mtime_nsec() / 1_000_000,
                    stalled_at,
                },
            );
        }
        self.known = known;
        // Files that vanished are forgotten (their counts stay: those replies happened).
        let known = &self.known;
        self.index.files.retain(|p, _| known.contains_key(p));
        self.rebuild_queue(now_ms);
    }

    fn needs_work(&self, path: &Path, meta: &FileMeta, now_ms: i64) -> bool {
        if meta.stalled_at == Some(meta.size) {
            return false;
        }
        match self.index.files.get(path) {
            Some(st) => st.dev != meta.dev || st.ino != meta.ino || st.offset != meta.size,
            None => meta.mtime_ms >= now_ms - i64::from(self.cfg.backfill_days) * DAY_MS,
        }
    }

    fn rebuild_queue(&mut self, now_ms: i64) {
        let mut work: Vec<(i64, PathBuf, u64)> = self
            .known
            .iter()
            .filter(|(p, m)| self.needs_work(p, m, now_ms))
            .map(|(p, m)| {
                let done = self
                    .index
                    .files
                    .get(p)
                    .filter(|s| s.ino == m.ino && s.dev == m.dev)
                    .map_or(0, |s| s.offset);
                (m.mtime_ms, p.clone(), m.size.saturating_sub(done))
            })
            .collect();
        work.sort_by_key(|w| w.0);
        let remaining: u64 = work.iter().map(|w| w.2).sum();
        self.progress.files_total = self.files_done + work.len() as u32;
        self.progress.bytes_total = self.bytes_done + remaining;
        self.queue = work.into_iter().map(|w| w.1).collect();
    }

    /// Reads new bytes of one file and counts what they hold. Returns bytes consumed.
    fn catch_up(&mut self, path: &Path, budget: u64, now_ms: i64) -> u64 {
        let Ok(meta) = fs::metadata(path) else {
            return 0;
        };
        let (dev, ino, size) = (meta.dev(), meta.ino(), meta.len());
        let km = self.known.entry(path.to_path_buf()).or_insert(FileMeta {
            dev,
            ino,
            size,
            mtime_ms: meta.mtime() * 1000,
            stalled_at: None,
        });
        if km.ino != ino || km.dev != dev || km.size != size {
            km.stalled_at = None;
        }
        (km.dev, km.ino, km.size) = (dev, ino, size);
        km.mtime_ms = meta.mtime() * 1000 + meta.mtime_nsec() / 1_000_000;
        let stalled = km.stalled_at == Some(size);

        let st = self
            .index
            .files
            .entry(path.to_path_buf())
            .or_insert(FileState {
                dev,
                ino,
                offset: 0,
            });
        if st.dev != dev || st.ino != ino || size < st.offset {
            *st = FileState {
                dev,
                ino,
                offset: 0,
            };
            self.index.dirty = true;
        }
        let start = st.offset;
        if start >= size || stalled {
            return 0;
        }
        let Ok(file) = fs::File::open(path) else {
            return 0;
        };
        let mut buf = std::mem::take(&mut self.buf);
        let index = &mut self.index;
        let end = pump(&file, start, size, budget, &mut buf, &mut |rec| {
            index.add(&rec, now_ms);
        });
        self.buf = buf;
        let advanced = end - start;
        if let Some(st) = self.index.files.get_mut(path) {
            st.offset = end;
        }
        if let Some(km) = self.known.get_mut(path) {
            km.stalled_at = (advanced == 0).then_some(size);
        }
        if advanced > 0 {
            self.index.dirty = true;
        }
        advanced
    }

    /// One slice of the backfill: up to 16 MiB of the newest file with work. False when the queue is empty.
    pub fn step(&mut self, now_ms: i64) -> bool {
        let Some(path) = self.queue.last().cloned() else {
            self.finish_backfill();
            return false;
        };
        let advanced = self.catch_up(&path, STEP_BUDGET, now_ms);
        self.bytes_done += advanced;
        // A step that read nothing cannot do better on a retry (unreadable file, unfinished line): the file
        // leaves the queue, and a rescan queues it again if it changes.
        let finished = advanced == 0
            || match (self.index.files.get(&path), self.known.get(&path)) {
                (Some(st), Some(km)) => st.offset >= km.size,
                _ => true,
            };
        if finished {
            self.queue.pop();
            self.files_done += 1;
        }
        self.progress.bytes_total = self.progress.bytes_total.max(self.bytes_done);
        self.progress.files_total = self.progress.files_total.max(self.files_done);
        if self.queue.is_empty() {
            self.finish_backfill();
        }
        true
    }

    fn finish_backfill(&mut self) {
        if !self.backfill_over {
            self.backfill_over = true;
            // Hand the backfill's freed memory back: the idle process should stay small.
            // SAFETY: no arguments that can be invalid.
            unsafe {
                libc::malloc_trim(0);
            }
        }
    }

    /// Checks the hot files for new bytes.
    pub fn tail(&mut self, now_ms: i64) {
        let hot_after = now_ms - self.cfg.hot_for.as_millis() as i64;
        let hot: Vec<PathBuf> = self
            .known
            .iter()
            .filter(|(_, m)| m.mtime_ms >= hot_after)
            .map(|(p, _)| p.clone())
            .collect();
        for path in hot {
            // A backfill candidate still waiting in the queue is left to the queue.
            if !self.backfill_over && self.queue.contains(&path) {
                continue;
            }
            self.catch_up(&path, 64 << 20, now_ms);
        }
    }

    /// Sends what changed: buckets, and progress when it moved. Cheap when nothing did.
    pub fn flush(&mut self, now_ms: i64) {
        if let Some(delta) = self.index.take_delta(now_ms) {
            self.send(Msg::Delta(delta));
        }
        if self.queue.is_empty() && self.buf.capacity() > BLOCK {
            // A buffer that grew for a long line is not kept while idle.
            self.buf = Vec::new();
        }
        let p = self.progress();
        if p != self.progress {
            self.progress = p;
            self.send(Msg::Progress(p));
        }
    }

    /// Prunes and saves if anything changed since the last save.
    pub fn save(&mut self, now_ms: i64) {
        if !self.index.dirty {
            return;
        }
        self.index.prune(now_ms);
        // A failed save (read-only cache folder, full disk) is not fatal: the next one tries again.
        if store::save(&self.cfg.cache_file, &self.index, now_ms).is_ok() {
            self.index.dirty = false;
        }
    }

    /// True while there are files left to backfill.
    pub fn busy(&self) -> bool {
        !self.queue.is_empty()
    }

    pub fn progress(&self) -> Progress {
        let mut p = self.progress;
        p.files_done = self.files_done;
        p.bytes_done = self.bytes_done;
        p.phase = if self.backfill_over {
            Phase::Live
        } else {
            Phase::Backfill
        };
        p
    }

    pub fn index(&self) -> &Index {
        &self.index
    }
}

/// Recursively lists `*.jsonl` files (symlinks are not followed).
fn walk(dir: &Path, out: &mut Vec<(PathBuf, fs::Metadata)>) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let Ok(kind) = entry.file_type() else {
            continue;
        };
        let path = entry.path();
        if kind.is_dir() {
            walk(&path, out);
        } else if kind.is_file()
            && path.extension().is_some_and(|e| e == "jsonl")
            && let Ok(meta) = entry.metadata()
        {
            out.push((path, meta));
        }
    }
}

/// Reads complete lines from `start`, up to about `budget` bytes, giving each record to `on_record`.
/// Returns the offset after the last consumed line. Over-long lines are skipped; an unfinished last line
/// stops the read there (it is read again once its newline is on disk).
fn pump(
    file: &fs::File,
    start: u64,
    size: u64,
    budget: u64,
    buf: &mut Vec<u8>,
    on_record: &mut dyn FnMut(super::scan::Record),
) -> u64 {
    let mut off = start;
    let mut block = BLOCK;
    while off < size && off - start < budget {
        if buf.len() < block {
            buf.resize(block, 0);
        }
        let Ok(n) = file.read_at(&mut buf[..block], off) else {
            break;
        };
        if n == 0 {
            break;
        }
        let used = scan_lines(&buf[..n], on_record);
        if used > 0 {
            off += used as u64;
            block = BLOCK;
        } else if n == block {
            // No newline in a whole block: a long line. Widen the read, and past the limit skip the line.
            if block < MAX_LINE {
                block *= 2;
            } else if let Some(next) = skip_line(file, off + n as u64, size, buf) {
                off = next;
                block = BLOCK;
            } else {
                break;
            }
        } else {
            break; // unfinished line at the end of the file
        }
    }
    off
}

/// Offset just after the next newline at or past `pos`, or `None` if the file ends first.
fn skip_line(file: &fs::File, mut pos: u64, size: u64, buf: &mut [u8]) -> Option<u64> {
    while pos < size {
        let n = file.read_at(buf, pos).ok().filter(|&n| n > 0)?;
        if let Some(i) = memchr::memchr(b'\n', &buf[..n]) {
            return Some(pos + i as u64 + 1);
        }
        pos += n as u64;
    }
    None
}

#[cfg(target_os = "linux")]
fn lower_priority() {
    // SAFETY: plain syscalls on the calling thread with constant arguments.
    unsafe {
        let tid = libc::syscall(libc::SYS_gettid);
        libc::setpriority(libc::PRIO_PROCESS, tid as libc::id_t, 19);
        // I/O priority: class IDLE (3) shifted into the top bits, data 0.
        libc::syscall(libc::SYS_ioprio_set, 1, tid, 3 << 13);
    }
}

/// Handle on the running indexer thread. Dropping it stops the thread (after a final save).
pub struct Indexer {
    rx: Receiver<Msg>,
    stop: Arc<AtomicBool>,
    handle: Option<JoinHandle<()>>,
}

impl Indexer {
    /// Starts the thread. It begins by loading the saved index, so the first messages arrive at once.
    pub fn spawn(cfg: IndexerConfig) -> std::io::Result<Indexer> {
        let (tx, rx) = mpsc::channel();
        let stop = Arc::new(AtomicBool::new(false));
        let flag = Arc::clone(&stop);
        let handle = thread::Builder::new()
            .name("vitals-tokens".into())
            .spawn(move || run(cfg, tx, flag))?;
        Ok(Indexer {
            rx,
            stop,
            handle: Some(handle),
        })
    }

    /// The raw channel, for a caller that wants to wait on it (`recv_timeout`).
    pub fn receiver(&self) -> &Receiver<Msg> {
        &self.rx
    }

    /// Applies every waiting message to `view` without blocking. True if anything arrived.
    pub fn drain_into(&self, view: &mut TokenView) -> bool {
        let mut any = false;
        while let Ok(msg) = self.rx.try_recv() {
            view.apply(msg);
            any = true;
        }
        any
    }

    /// Asks the thread to stop, waits for its final save and returns.
    pub fn shutdown(mut self) {
        self.stop_and_join();
    }

    fn stop_and_join(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(h) = self.handle.take() {
            h.thread().unpark();
            let _ = h.join();
        }
    }
}

impl Drop for Indexer {
    fn drop(&mut self) {
        self.stop_and_join();
    }
}

fn run(cfg: IndexerConfig, tx: Sender<Msg>, stop: Arc<AtomicBool>) {
    if cfg.low_priority {
        lower_priority();
    }
    let (tail_every, rescan_every, save_every) = (cfg.tail_every, cfg.rescan_every, cfg.save_every);
    let mut e = Engine::new(cfg, tx);
    e.load(now_ms());
    e.rescan(now_ms());
    e.flush(now_ms());

    let started = Instant::now();
    let (mut next_tail, mut next_rescan, mut next_save, mut next_flush) = (
        started + tail_every,
        started + rescan_every,
        started + save_every,
        started,
    );
    while !stop.load(Ordering::Relaxed) && !e.closed {
        let now = Instant::now();
        let wall = now_ms();
        if now >= next_rescan {
            e.rescan(wall);
            next_rescan = now + rescan_every;
        }
        if now >= next_tail {
            e.tail(wall);
            next_tail = now + tail_every;
        }
        let worked = e.step(wall);
        // While backfilling, messages are batched to four a second; once live they go out at once.
        if !worked || now >= next_flush {
            e.flush(wall);
            next_flush = now + Duration::from_millis(250);
        }
        if now >= next_save {
            e.save(wall);
            next_save = now + save_every;
        }
        if !worked {
            let wake = next_tail.min(next_rescan).min(next_save);
            thread::park_timeout(
                wake.saturating_duration_since(Instant::now())
                    .min(Duration::from_millis(500)),
            );
        }
    }
    e.tail(now_ms());
    e.flush(now_ms());
    e.save(now_ms());
}
