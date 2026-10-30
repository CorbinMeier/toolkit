// SPDX-License-Identifier: MIT
//! The real binary in a pty (`script`), with no display variables and every directory in a temp dir:
//! the terminal is restored on Ctrl+C and on a panic, and `--once` prints one frame. No window ever opens.

mod common;

use std::io::Write;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use common::TempDir;

const ENTER_ALT: &str = "\x1b[?1049h";
const LEAVE_ALT: &str = "\x1b[?1049l";
const MOUSE_ON: &str = "\x1b[?1000h";
const MOUSE_OFF: &str = "\x1b[?1000l";

/// A config that points the usage server at a closed port, so no test talks to a real usage server on this machine.
fn offline_config(t: &TempDir) {
    common::write(
        &t.join("config/vitals/config.toml"),
        "[panel.claude]\nusage_url = \"http://127.0.0.1:1\"\n",
    );
}

fn vitals_cmd(t: &TempDir, args: &str, extra_env: &[(&str, &str)]) -> Command {
    offline_config(t);
    let mut c = Command::new("script");
    let bin = env!("CARGO_BIN_EXE_vitals");
    c.arg("-qfc")
        .arg(format!("stty cols 100 rows 30; exec {bin} {args}"))
        .arg("/dev/null")
        .env_remove("WAYLAND_DISPLAY")
        .env_remove("DISPLAY")
        .env("HOME", t.join("home"))
        .env("XDG_CONFIG_HOME", t.join("config"))
        .env("XDG_CACHE_HOME", t.join("cache"))
        .env("XDG_RUNTIME_DIR", t.join("run"))
        .env("CLAUDE_CONFIG_DIR", t.join("claude"))
        .env("COLORTERM", "truecolor")
        .env("TERM", "xterm-256color")
        .env_remove("NO_COLOR")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    for (k, v) in extra_env {
        c.env(k, v);
    }
    c
}

/// Runs to the end (killing it after 15 s), optionally sending `keys` once it has drawn.
fn run(mut cmd: Command, keys: Option<&[u8]>) -> (String, bool) {
    let mut child = cmd.spawn().expect("script is installed");
    let mut stdin = child.stdin.take().unwrap();
    if let Some(k) = keys {
        std::thread::sleep(Duration::from_millis(1500));
        let _ = stdin.write_all(k);
        let _ = stdin.flush();
    }
    let start = Instant::now();
    let mut timed_out = false;
    loop {
        if child.try_wait().unwrap().is_some() {
            break;
        }
        if start.elapsed() > Duration::from_secs(15) {
            let _ = child.kill();
            timed_out = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    let out = child.wait_with_output().unwrap();
    (String::from_utf8_lossy(&out.stdout).into_owned(), timed_out)
}

#[test]
fn ctrl_c_restores_the_terminal() {
    let t = TempDir::new("pty-ctrlc");
    let (out, timed_out) = run(vitals_cmd(&t, "--no-tokens", &[]), Some(b"\x03"));
    assert!(!timed_out, "Ctrl+C did not end the program");
    assert!(out.contains(ENTER_ALT) && out.contains(MOUSE_ON), "{out:?}");
    assert!(
        out.contains(LEAVE_ALT) && out.contains(MOUSE_OFF),
        "terminal not restored: {out:?}"
    );
    assert!(out.contains("vitals"), "a frame was drawn");
    assert!(
        !out.contains("?1003h") && !out.contains("?1002h"),
        "mouse motion reporting must stay off: {out:?}"
    );
    // The config is saved on quit, atomically, into the temp dir.
    assert!(t.join("config/vitals/config.toml").exists());
}

#[test]
fn q_quits_and_the_config_remembers_ui_changes() {
    let t = TempDir::new("pty-q");
    let (_, timed_out) = run(
        vitals_cmd(&t, "--no-tokens --theme mono", &[]),
        Some(b"btq"),
    );
    assert!(!timed_out);
    let saved = std::fs::read_to_string(t.join("config/vitals/config.toml")).unwrap();
    assert!(
        saved.contains("[section.cpu]") && saved.contains("bars = false"),
        "the b key turned off the CPU bars: {saved}"
    );
    // `--theme mono` is for this run only; `t` moved from mono to the first custom theme or default.
    assert!(!saved.contains("theme = \"mono\""), "{saved}");
}

#[cfg(debug_assertions)]
#[test]
fn a_panic_restores_the_terminal_before_its_message() {
    let t = TempDir::new("pty-panic");
    let (out, timed_out) = run(
        vitals_cmd(&t, "--no-tokens", &[("VITALS_TEST_PANIC", "1")]),
        None,
    );
    assert!(!timed_out);
    let leave = out.find(LEAVE_ALT).expect("left the alternate screen");
    let msg = out
        .find("VITALS_TEST_PANIC")
        .expect("panic message printed");
    assert!(
        leave < msg,
        "the screen must be restored before the message prints: {out:?}"
    );
    assert!(out.contains(MOUSE_OFF));
}

#[test]
fn once_prints_one_plain_frame_of_the_requested_size() {
    let t = TempDir::new("once");
    offline_config(&t);
    let out = Command::new(env!("CARGO_BIN_EXE_vitals"))
        .args([
            "--once",
            "--no-tokens",
            "--size",
            "100x30",
            "--interval",
            "250",
        ])
        .env_remove("WAYLAND_DISPLAY")
        .env_remove("DISPLAY")
        .env("HOME", t.join("home"))
        .env("XDG_CONFIG_HOME", t.join("config"))
        .env("XDG_CACHE_HOME", t.join("cache"))
        .env("XDG_RUNTIME_DIR", t.join("run"))
        .env("CLAUDE_CONFIG_DIR", t.join("claude"))
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let text = String::from_utf8(out.stdout).unwrap();
    assert_eq!(text.lines().count(), 30);
    assert!(text.lines().all(|l| l.chars().count() <= 100));
    assert!(!text.contains('\x1b'), "piped output has no escapes");
    assert!(text.contains("┌ CPU") && text.contains("┌ Memory") && text.contains("[1 cpu]"));
}

#[test]
fn bad_flags_are_reported() {
    let out = Command::new(env!("CARGO_BIN_EXE_vitals"))
        .arg("--layout")
        .arg("nope")
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&out.stderr).contains("bad --layout"));
    let out = Command::new(env!("CARGO_BIN_EXE_vitals"))
        .arg("--version")
        .output()
        .unwrap();
    assert!(String::from_utf8_lossy(&out.stdout).starts_with("vitals "));
}
