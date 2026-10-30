// SPDX-License-Identifier: MIT
//! `IndexerConfig::from_env` reads `CLAUDE_CONFIG_DIR` and `XDG_CACHE_HOME`. One test only: it edits the
//! process environment.

mod common;

use vitals::tokens::IndexerConfig;

#[test]
fn folders_come_from_the_environment() {
    let t = common::TempDir::new("env");
    // SAFETY: this is the only test in the binary, so no other thread reads the environment meanwhile.
    unsafe {
        std::env::set_var("CLAUDE_CONFIG_DIR", t.join("claude"));
        std::env::set_var("XDG_CACHE_HOME", t.join("cache"));
    }
    let cfg = IndexerConfig::from_env();
    assert_eq!(cfg.projects_dir, t.join("claude/projects"));
    assert_eq!(cfg.cache_file, t.join("cache/vitals/tokens-v1.bin"));
    unsafe {
        std::env::set_var("XDG_CACHE_HOME", "");
        std::env::set_var("HOME", t.join("home"));
        std::env::remove_var("CLAUDE_CONFIG_DIR");
    }
    let cfg = IndexerConfig::from_env();
    assert_eq!(cfg.projects_dir, t.join("home/.claude/projects"));
    assert_eq!(cfg.cache_file, t.join("home/.cache/vitals/tokens-v1.bin"));
}
