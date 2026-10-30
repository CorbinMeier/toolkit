// SPDX-License-Identifier: MIT
//! Shared test helpers: temp dirs, fixture copies, synthetic transcript lines.
#![allow(dead_code)]

pub mod ui;

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU32, Ordering};

/// A temp directory removed on drop.
pub struct TempDir(pub PathBuf);

impl TempDir {
    pub fn new(tag: &str) -> Self {
        static N: AtomicU32 = AtomicU32::new(0);
        let dir = std::env::temp_dir().join(format!(
            "vitals-test-{tag}-{}-{}",
            std::process::id(),
            N.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir_all(&dir).unwrap();
        TempDir(dir)
    }

    pub fn path(&self) -> &Path {
        &self.0
    }

    pub fn join(&self, p: impl AsRef<Path>) -> PathBuf {
        self.0.join(p)
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

/// Writes a file, creating parent folders.
pub fn write(path: &Path, contents: impl AsRef<[u8]>) {
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, contents).unwrap();
}

/// Copies a folder tree.
pub fn copy_tree(from: &Path, to: &Path) {
    fs::create_dir_all(to).unwrap();
    for e in fs::read_dir(from).unwrap().flatten() {
        let dest = to.join(e.file_name());
        if e.file_type().unwrap().is_dir() {
            copy_tree(&e.path(), &dest);
        } else {
            fs::copy(e.path(), dest).unwrap();
        }
    }
}

pub fn fixture(rel: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures")
        .join(rel)
}

/// ISO 8601 UTC with milliseconds, as Claude Code writes timestamps.
pub fn iso(ts_ms: i64) -> String {
    chrono::DateTime::from_timestamp_millis(ts_ms)
        .unwrap()
        .format("%Y-%m-%dT%H:%M:%S%.3fZ")
        .to_string()
}

/// A made-up assistant line (no real content). `text` goes in the message body to prove it is never kept.
pub fn assistant(
    id: &str,
    model: &str,
    ts_ms: i64,
    usage: (u64, u64, u64, u64),
    text: &str,
) -> String {
    let (i, cw, cr, o) = usage;
    format!(
        r#"{{"type":"assistant","timestamp":"{}","requestId":"req_{id}","message":{{"id":"msg_{id}","model":"{model}","usage":{{"input_tokens":{i},"cache_creation_input_tokens":{cw},"cache_read_input_tokens":{cr},"output_tokens":{o},"service_tier":"standard"}},"content":[{{"type":"text","text":"{text}"}}]}}}}"#,
        iso(ts_ms)
    )
}

pub fn user_line(ts_ms: i64) -> String {
    format!(
        r#"{{"type":"user","timestamp":"{}","message":{{"role":"user","content":"hello"}}}}"#,
        iso(ts_ms)
    )
}

pub fn now_ms() -> i64 {
    vitals::tokens::indexer::now_ms()
}
