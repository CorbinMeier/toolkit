// SPDX-License-Identifier: MIT
//! Command-line readouts (spec §9): JSON schema 2 key for key against fixture roots, a text snapshot per
//! section, exit codes, one failing section next to working ones, and `--watch`. The real `~/.claude`, feed
//! and usage server are never touched: every path is a temp dir and the usage server is a local fixture server or a closed port.

mod common;

use std::io::{Read, Write};
use std::net::TcpListener;
use std::process::Command;
use std::time::Duration;

use claude_usage_core::feed::{self, Feed, Window};
use common::ui::{pin_tz, server_fixture};
use common::{TempDir, assistant, copy_tree, fixture, now_ms, write};
use serde_json::{Value, json};
use vitals::readout::data::{
    ByModel, Claude, Cpu, DayPart, Device, Disk, Document, IndexState, Interface, Memory,
    ModelWeek, MountInfo, Network, Part, Tok, Tokens, WeekWindow, Window as Win,
};
use vitals::readout::{Env, Request, Section, run, text};
use vitals::sample::Root;
use vitals::tokens::IndexerConfig;

/// A machine in a temp dir: fixture `/proc` and `/sys`, a Claude config dir with one made-up transcript,
/// a feed folder, and a usage server that is either a fixture server or a closed port.
struct Rig {
    dir: TempDir,
    usage_url: String,
    feed: bool,
    net_include: Vec<String>,
    net_exclude: Vec<String>,
}

impl Rig {
    fn new() -> Self {
        let dir = TempDir::new("readout");
        copy_tree(&fixture("sys"), &dir.join("sys"));
        copy_tree(&fixture("proc/a"), &dir.join("proc"));
        // Mounts that exist on any machine; the space figures come from statvfs, so tests check types only.
        write(
            &dir.join("proc/self/mounts"),
            "/dev/nvme0n1p3 / ext4 rw 0 0\n/dev/nvme0n1p5 /tmp ext4 rw 0 0\n",
        );
        let now = now_ms();
        write(
            &dir.join("claude/projects/p/a.jsonl"),
            format!(
                "{}\n{}\n",
                assistant("a1", "claude-opus-5", now - 60_000, (10, 20, 300, 40), "hi"),
                assistant("a2", "claude-sonnet-5", now - 30_000, (1, 2, 3, 4), "hi"),
            ),
        );
        let feed_dir = dir.join("run/claude-usage");
        std::fs::create_dir_all(&feed_dir).unwrap();
        let mut rig = Rig {
            dir,
            usage_url: closed_port(),
            feed: true,
            net_include: Vec::new(),
            net_exclude: Vec::new(),
        };
        rig.write_feed(now / 1000);
        rig
    }

    fn write_feed(&mut self, written_at: i64) {
        let f = Feed {
            written_at,
            five_hour: Some(Window {
                used_percentage: 3.0,
                resets_at: written_at + 3 * 3600,
            }),
            seven_day: Some(Window {
                used_percentage: 41.0,
                resets_at: written_at + 3 * 86_400,
            }),
        };
        feed::write_to(&self.dir.join("run/claude-usage"), &f).unwrap();
    }

    fn no_feed(&mut self) {
        std::fs::remove_file(self.dir.join("run/claude-usage").join(feed::FILE_NAME)).unwrap();
        self.feed = false;
    }

    fn env(&self) -> Env {
        let mut indexer = IndexerConfig::new(
            self.dir.join("claude/projects"),
            self.dir.join("cache/vitals/tokens-v1.bin"),
        );
        indexer.low_priority = false;
        Env {
            root: Root::new(self.dir.join("proc"), self.dir.join("sys")),
            interval: Duration::from_millis(20),
            indexer,
            feed_dir: Some(self.dir.join("run/claude-usage")),
            usage_url: Some(self.usage_url.clone()),
            net_include: self.net_include.clone(),
            net_exclude: self.net_exclude.clone(),
        }
    }

    /// Runs `args` (the flag names, as typed) and returns the exit code and stdout.
    fn run(&self, args: &[&str]) -> (u8, String) {
        let mut req = Request::default();
        for a in args {
            if !req.flag(a) {
                panic!("not a flag: {a}");
            }
        }
        let mut out = Vec::new();
        let code = run(&req, &req.sections(false), &self.env(), &mut out);
        (code, String::from_utf8(out).unwrap())
    }
}

fn closed_port() -> String {
    let l = TcpListener::bind("127.0.0.1:0").unwrap();
    format!("http://{}", l.local_addr().unwrap())
}

/// Serves the usage server fixture to every request until the process ends.
fn server_server(name: &str) -> String {
    let body = server_fixture(name);
    let l = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", l.local_addr().unwrap());
    std::thread::spawn(move || {
        while let Ok((mut s, _)) = l.accept() {
            let mut req = [0u8; 2048];
            let _ = s.read(&mut req);
            let resp = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            let _ = s.write_all(resp.as_bytes());
        }
    });
    url
}

/// JSON schema 2 as shapes: `"int"`, `"float"`, `"string"`, `"bool"`, a `|null` suffix for readings that
/// may be absent, `[shape]` for a list, and an object for an object (its keys are exactly these).
fn schema() -> Value {
    let tok = json!({"input":"int","cache_write":"int","cache_read":"int","output":"int"});
    json!({
        "cpu": {"percent":"float","threads":["float"],"load":["float"],"freq_ghz":"float|null","temp_c":"float|null"},
        "memory": {"total_bytes":"int","used_bytes":"int","available_bytes":"int","cached_bytes":"int",
            "buffers_bytes":"int","used_percent":"float","swap_total_bytes":"int","swap_used_bytes":"int",
            "swap_percent":"float"},
        "disk": {"read_bps":"int","write_bps":"int","busy_percent":"float","temp_c":"float|null",
            "devices":[{"name":"string","read_bps":"int","write_bps":"int","busy_percent":"float"}],
            "mounts":[{"target":"string","source":"string","fstype":"string","disk":"string","total_bytes":"int",
                "used_bytes":"int","avail_bytes":"int","used_percent":"float"}]},
        "network": {"down_bps":"int","up_bps":"int","peak_down_bps":"int","peak_up_bps":"int",
            "interfaces":[{"name":"string","down_bps":"int","up_bps":"int","rx_bytes":"int","tx_bytes":"int","included":"bool"}]},
        "tokens": {"today":tok,"last_hour":tok,"per_min_5m":tok,
            "by_model_today":{"opus":tok,"sonnet":tok,"haiku":tok,"fable":tok,"other":tok},
            "index":{"phase":"string","percent":"float"}},
        "claude": {"source":"string","read_at":"int","stale":"bool",
            "session":{"percent":"float","resets_at":"int|null","elapsed_percent":"float|null"},
            "week":{"percent":"float","resets_at":"int|null","elapsed_percent":"float|null",
                "day_part":{"index":"int","percent":"float"}},
            "models":[{"label":"string","percent":"float","resets_at":"int|null"}]},
    })
}

/// Checks `v` against `shape`; `path` names the place in failure messages. Floats must be rounded to
/// 1 decimal when the key says percent.
fn check(v: &Value, shape: &Value, path: &str) {
    match shape {
        Value::String(s) => {
            let (kind, nullable) = match s.strip_suffix("|null") {
                Some(k) => (k, true),
                None => (s.as_str(), false),
            };
            if v.is_null() {
                assert!(nullable, "{path} is null but not allowed to be");
                return;
            }
            let ok = match kind {
                "int" => v.is_u64() || v.is_i64(),
                "float" => v.is_number(),
                "string" => v.is_string(),
                "bool" => v.is_boolean(),
                other => panic!("bad shape {other}"),
            };
            assert!(ok, "{path}: {v} is not {s}");
            if path.ends_with("percent") {
                let x = v.as_f64().unwrap();
                assert!(
                    ((x * 10.0).round() - x * 10.0).abs() < 1e-6,
                    "{path}: {x} not 1 decimal"
                );
            }
        }
        Value::Array(a) => {
            let items = v
                .as_array()
                .unwrap_or_else(|| panic!("{path} is not a list"));
            for (i, item) in items.iter().enumerate() {
                check(item, &a[0], &format!("{path}[{i}]"));
            }
        }
        Value::Object(o) => {
            let m = v
                .as_object()
                .unwrap_or_else(|| panic!("{path} is not an object: {v}"));
            let mut have: Vec<_> = m.keys().collect();
            let mut want: Vec<_> = o.keys().collect();
            have.sort();
            want.sort();
            assert_eq!(have, want, "{path}: keys differ");
            for (k, s) in o {
                check(&m[k], s, &format!("{path}.{k}"));
            }
        }
        _ => panic!("bad shape"),
    }
}

fn parse(out: &str) -> Value {
    serde_json::from_str(out.trim()).unwrap_or_else(|e| panic!("not JSON ({e}): {out}"))
}

#[test]
fn every_section_matches_schema_2_key_for_key() {
    let mut rig = Rig::new();
    rig.usage_url = server_server("fresh");
    let schema = schema();
    for (flag, key) in [
        ("--cpu", "cpu"),
        ("--mem", "memory"),
        ("--disk", "disk"),
        ("--net", "network"),
        ("--tokens", "tokens"),
        ("--claude", "claude"),
    ] {
        let (code, out) = rig.run(&[flag, "--json"]);
        assert_eq!(code, 0, "{flag}: {out}");
        let v = parse(&out);
        assert_eq!(v["schema"], 2);
        assert!(
            v["sampled_at"].as_i64().unwrap() > 1_700_000_000_000,
            "epoch ms"
        );
        let keys: Vec<_> = v.as_object().unwrap().keys().cloned().collect();
        assert_eq!(
            keys.len(),
            3,
            "{flag}: only the requested section: {keys:?}"
        );
        check(&v[key], &schema[key], key);
    }
}

#[test]
fn all_prints_every_section_in_schema_order() {
    let mut rig = Rig::new();
    rig.usage_url = server_server("fresh");
    let (code, out) = rig.run(&["--all", "--json"]);
    assert_eq!(code, 0, "{out}");
    let v = parse(&out);
    // serde_json sorts keys on parse, so the order is read off the text.
    let at = |k: &str| out.find(&format!("\"{k}\":")).unwrap();
    let order = [
        "schema",
        "sampled_at",
        "cpu",
        "memory",
        "disk",
        "network",
        "tokens",
        "claude",
    ];
    assert!(order.windows(2).all(|w| at(w[0]) < at(w[1])), "{out}");
    assert_eq!(v.as_object().unwrap().len(), order.len());
    let schema = schema();
    for (k, s) in schema.as_object().unwrap() {
        check(&v[k], s, k);
    }
    // The fixture transcript: two replies today, one opus and one sonnet.
    assert_eq!(v["tokens"]["today"]["output"], 44);
    assert_eq!(v["tokens"]["by_model_today"]["opus"]["cache_read"], 300);
    assert_eq!(v["tokens"]["by_model_today"]["sonnet"]["input"], 1);
    assert_eq!(v["tokens"]["index"]["phase"], "live");
    // The fixture meminfo and loadavg.
    assert!(v["memory"]["total_bytes"].as_u64().unwrap() > 0);
    assert_eq!(v["cpu"]["load"].as_array().unwrap().len(), 3);
}

#[test]
fn claude_reads_the_feed_first_and_takes_models_from_the_server() {
    let mut rig = Rig::new();
    rig.usage_url = server_server("fresh");
    let (code, out) = rig.run(&["--claude", "--json"]);
    assert_eq!(code, 0, "{out}");
    let c = &parse(&out)["claude"];
    assert_eq!(c["source"], "feed");
    assert_eq!(c["stale"], false);
    assert_eq!(c["session"]["percent"], 3.0);
    assert_eq!(c["week"]["percent"], 41.0);
    let labels: Vec<_> = c["models"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| m["label"].clone())
        .collect();
    assert_eq!(labels, [json!("Week (Fable)"), json!("Week (Sonnet)")]);
    assert_eq!(c["week"]["day_part"]["index"], 4);
}

#[test]
fn claude_falls_back_to_the_server_and_null_marks_absent_readings() {
    let mut rig = Rig::new();
    rig.no_feed();
    rig.usage_url = server_server("missing_week");
    let (code, out) = rig.run(&["--claude", "--json"]);
    assert_eq!(code, 0, "{out}");
    let c = &parse(&out)["claude"];
    assert_eq!(c["source"], "server");
    assert!(c["week"].is_null());
    assert!(c["session"]["percent"].is_number());
}

#[test]
fn claude_with_no_feed_and_no_server_is_an_error_and_exit_1() {
    let mut rig = Rig::new();
    rig.no_feed();
    let (code, out) = rig.run(&["--claude", "--cpu", "--json"]);
    assert_eq!(code, 1);
    let v = parse(&out);
    assert!(
        v["claude"]["error"]
            .as_str()
            .unwrap()
            .starts_with("no reading")
    );
    assert_eq!(v["claude"].as_object().unwrap().len(), 1);
    assert!(
        v["cpu"]["percent"].is_number(),
        "the other section still prints"
    );
}

#[test]
fn a_missing_proc_file_fails_only_its_section() {
    let rig = Rig::new();
    std::fs::remove_file(rig.dir.join("proc/stat")).unwrap();
    let (code, out) = rig.run(&["--cpu", "--mem", "--json"]);
    assert_eq!(code, 1);
    let v = parse(&out);
    let e = v["cpu"]["error"].as_str().unwrap();
    assert!(e.contains("stat"), "{e}");
    assert_eq!(v["cpu"].as_object().unwrap().len(), 1);
    assert!(v["memory"]["total_bytes"].as_u64().unwrap() > 0);

    let (code, out) = rig.run(&["--cpu", "--mem"]);
    assert_eq!(code, 1);
    assert!(out.starts_with("CPU\nerror  cannot read "), "{out}");
    assert!(out.contains("\nMemory\nused"), "{out}");
}

#[test]
fn text_output_has_a_title_aligned_rows_and_a_summary_per_section() {
    let rig = Rig::new();
    let (code, out) = rig.run(&["--mem", "--net"]);
    assert_eq!(code, 0);
    let mut parts = out.split("\n\n");
    let mem = parts.next().unwrap();
    assert!(mem.starts_with("Memory\nused       "), "{mem}");
    assert_eq!(mem.lines().count(), 6, "title, four rows, summary: {mem}");
    assert!(parts.next().unwrap().starts_with("Network\n"));
}

#[test]
fn network_lists_every_interface_and_applies_the_inclusion_config() {
    let mut rig = Rig::new();
    let included = |rig: &Rig| -> Vec<String> {
        let (_, out) = rig.run(&["--net", "--json"]);
        let v: Value = serde_json::from_str(&out).unwrap();
        assert_eq!(v["schema"], 2);
        let all = v["network"]["interfaces"].as_array().unwrap();
        let listed: Vec<_> = all.iter().map(|i| i["name"].as_str().unwrap()).collect();
        for n in ["lo", "docker0", "veth5382c7e", "wlo1", "enx3c8cf8ff16ee"] {
            assert!(listed.contains(&n), "{n} is listed: {listed:?}");
        }
        all.iter()
            .filter(|i| i["included"] == true)
            .map(|i| i["name"].as_str().unwrap().to_owned())
            .collect()
    };
    // The default rule: physical interfaces only.
    let mut names = included(&rig);
    names.sort();
    assert_eq!(names, ["enx3c8cf8ff16ee", "wlo1"]);
    // Overrides on top of it.
    rig.net_include = vec!["docker0".into()];
    rig.net_exclude = vec!["wlo1".into()];
    let mut names = included(&rig);
    names.sort();
    assert_eq!(names, ["docker0", "enx3c8cf8ff16ee"]);
    let (_, text) = rig.run(&["--net"]);
    assert!(
        text.contains("all ") && text.contains("[x] docker0") && text.contains("[ ] wlo1"),
        "{text}"
    );
}

#[test]
fn json_floats_are_rounded_and_there_is_one_line() {
    let rig = Rig::new();
    let (_, out) = rig.run(&["--cpu", "--mem", "--json"]);
    assert_eq!(out.trim_end().lines().count(), 1);
    assert!(out.ends_with('\n'));
    assert!(!out.contains("00000"), "unrounded float: {out}");
}

#[test]
fn watch_emits_one_line_per_interval() {
    let rig = Rig::new();
    let mut req = Request::default();
    for f in ["--cpu", "--watch", "--json"] {
        req.flag(f);
    }
    req.count = Some(3);
    let mut out = Vec::new();
    let code = run(&req, &req.sections(false), &rig.env(), &mut out);
    assert_eq!(code, 0);
    let text = String::from_utf8(out).unwrap();
    let lines: Vec<_> = text.lines().collect();
    assert_eq!(lines.len(), 3);
    let times: Vec<i64> = lines
        .iter()
        .map(|l| parse(l)["sampled_at"].as_i64().unwrap())
        .collect();
    assert!(times.windows(2).all(|w| w[1] > w[0]), "{times:?}");

    // Text: blocks separated by a blank line.
    let mut req = Request::default();
    for f in ["--mem", "--watch"] {
        req.flag(f);
    }
    req.count = Some(2);
    let mut out = Vec::new();
    run(&req, &req.sections(false), &rig.env(), &mut out);
    let text = String::from_utf8(out).unwrap();
    assert_eq!(text.matches("Memory\n").count(), 2);
    assert!(text.contains("\n\nMemory\n"));
}

#[test]
fn no_tokens_drops_tokens_from_all_only() {
    let mut req = Request::default();
    req.flag("--all");
    assert_eq!(req.sections(true).len(), 5);
    assert!(!req.sections(true).contains(&Section::Tokens));
    req.flag("--tokens");
    assert_eq!(req.sections(true).len(), 6);
    let mut dump = Request::default();
    dump.flag("--dump");
    assert_eq!(dump.sections(false).len(), 6);
}

fn bin(args: &[&str]) -> (i32, String, String) {
    let t = TempDir::new("readout-bin");
    write(
        &t.join("config/vitals/config.toml"),
        "[panel.claude]\nusage_url = \"http://127.0.0.1:1\"\n",
    );
    let o = Command::new(env!("CARGO_BIN_EXE_vitals"))
        .args(args)
        .env_remove("WAYLAND_DISPLAY")
        .env_remove("DISPLAY")
        .env("HOME", t.join("home"))
        .env("XDG_CONFIG_HOME", t.join("config"))
        .env("XDG_CACHE_HOME", t.join("cache"))
        .env("XDG_RUNTIME_DIR", t.join("run"))
        .env("CLAUDE_CONFIG_DIR", t.join("claude"))
        .output()
        .unwrap();
    (
        o.status.code().unwrap(),
        String::from_utf8(o.stdout).unwrap(),
        String::from_utf8(o.stderr).unwrap(),
    )
}

#[test]
fn bad_flags_exit_2() {
    for args in [
        &["--bogus"][..],
        &["--cpu", "--interval", "fast"],
        &["--cpu", "--watch", "--count", "0"],
        &["--json"],
        &["--watch"],
        &["--cpu", "--once"],
    ] {
        let (code, out, err) = bin(args);
        assert_eq!(code, 2, "{args:?}: {out}{err}");
        assert!(out.is_empty(), "{args:?}: nothing on stdout");
        assert!(!err.is_empty(), "{args:?}: says why");
    }
}

#[test]
fn the_binary_prints_a_readout_and_exits_0() {
    let (code, out, _) = bin(&["--mem", "--json"]);
    assert_eq!(code, 0);
    check(&parse(&out)["memory"], &schema()["memory"], "memory");
    // Nothing is configured: no feed, a closed usage-server port. The section fails, the exit code says so.
    let (code, out, _) = bin(&["--claude", "--json"]);
    assert_eq!(code, 1);
    assert!(parse(&out)["claude"]["error"].is_string());
}

#[test]
fn help_lists_the_readout_flags() {
    let (code, out, _) = bin(&["--help"]);
    assert_eq!(code, 0);
    for f in [
        "--cpu",
        "--mem",
        "--disk",
        "--net",
        "--tokens",
        "--claude",
        "--all",
        "--json",
        "--watch",
        "--no-wait",
        "--dump",
    ] {
        assert!(out.contains(f), "{f} missing from --help");
    }
}

// ---- text snapshots: fixed documents, so the figures never change ----

const T0: i64 = 1_793_253_540_000; // 2026-10-29 06:39 UTC

fn doc() -> Document {
    pin_tz();
    Document::new(T0)
}

#[test]
fn text_cpu() {
    let mut d = doc();
    d.cpu = Some(Part::Ok(Cpu {
        percent: 41.2,
        threads: vec![62.0, 18.1, 5.0, 100.0],
        load: [6.65, 7.57, 12.06],
        freq_ghz: Some(3.1),
        temp_c: Some(68.0),
    }));
    insta::assert_snapshot!(text::render(&d));
}

#[test]
fn text_cpu_without_sensors() {
    let mut d = doc();
    d.cpu = Some(Part::Ok(Cpu {
        percent: 0.0,
        threads: vec![0.0],
        load: [0.0, 0.0, 0.0],
        freq_ghz: None,
        temp_c: None,
    }));
    insta::assert_snapshot!(text::render(&d));
}

#[test]
fn text_memory() {
    let mut d = doc();
    d.memory = Some(Part::Ok(Memory {
        total_bytes: 32_760_000_000,
        used_bytes: 13_526_000_000,
        available_bytes: 19_234_000_000,
        cached_bytes: 8_100_000_000,
        buffers_bytes: 400_000_000,
        used_percent: 41.3,
        swap_total_bytes: 8_589_934_592,
        swap_used_bytes: 0,
        swap_percent: 0.0,
    }));
    insta::assert_snapshot!(text::render(&d));
}

#[test]
fn text_disk() {
    let mut d = doc();
    d.disk = Some(Part::Ok(Disk {
        read_bps: 1_300_000,
        write_bps: 1_677_721,
        busy_percent: 3.2,
        temp_c: Some(43.0),
        devices: vec![Device {
            name: "nvme0n1".into(),
            read_bps: 1_300_000,
            write_bps: 1_677_721,
            busy_percent: 3.2,
        }],
        mounts: vec![
            MountInfo {
                target: "/".into(),
                source: "/dev/nvme0n1p3".into(),
                fstype: "ext4".into(),
                disk: "nvme0n1".into(),
                total_bytes: 511_000_000_000,
                used_bytes: 368_000_000_000,
                avail_bytes: 117_000_000_000,
                used_percent: 72.0,
            },
            MountInfo {
                target: "/boot/efi".into(),
                source: "/dev/nvme0n1p1".into(),
                fstype: "vfat".into(),
                disk: "nvme0n1".into(),
                total_bytes: 1_069_547_520,
                used_bytes: 474_000_000,
                avail_bytes: 595_000_000,
                used_percent: 44.3,
            },
        ],
    }));
    insta::assert_snapshot!(text::render(&d));
}

#[test]
fn text_network() {
    let mut d = doc();
    d.network = Some(Part::Ok(Network {
        down_bps: 1_677_721,
        up_bps: 12_288,
        peak_down_bps: 5_000_000,
        peak_up_bps: 102_400,
        interfaces: vec![
            Interface {
                name: "wlo1".into(),
                down_bps: 1_677_721,
                up_bps: 12_288,
                rx_bytes: 52_655_403,
                tx_bytes: 7_045_565,
                included: true,
            },
            Interface {
                name: "enx3c8cf8ff16ee".into(),
                down_bps: 0,
                up_bps: 0,
                rx_bytes: 3_318_699_393,
                tx_bytes: 1_533_549_208,
                included: false,
            },
        ],
    }));
    insta::assert_snapshot!(text::render(&d));
}

fn tokens(phase: &'static str, percent: f64) -> Tokens {
    Tokens {
        today: Tok {
            input: 741,
            cache_write: 2_100_000,
            cache_read: 34_400_000,
            output: 239_000,
        },
        last_hour: Tok::default(),
        per_min_5m: Tok {
            input: 41,
            cache_write: 121_000,
            cache_read: 2_200_000,
            output: 15_000,
        },
        by_model_today: ByModel {
            opus: Tok {
                input: 300,
                cache_write: 1_000_000,
                cache_read: 8_000_000,
                output: 100_000,
            },
            sonnet: Tok {
                input: 441,
                cache_write: 1_100_000,
                cache_read: 26_400_000,
                output: 139_000,
            },
            ..ByModel::default()
        },
        index: IndexState { phase, percent },
    }
}

#[test]
fn text_tokens_live() {
    let mut d = doc();
    d.tokens = Some(Part::Ok(tokens("live", 100.0)));
    insta::assert_snapshot!(text::render(&d));
}

#[test]
fn text_tokens_while_indexing() {
    let mut d = doc();
    d.tokens = Some(Part::Ok(tokens("backfill", 38.0)));
    insta::assert_snapshot!(text::render(&d));
}

#[test]
fn text_claude() {
    let mut d = doc();
    d.claude = Some(Part::Ok(Claude {
        source: "feed",
        read_at: T0 - 120_000,
        stale: false,
        session: Some(Win {
            percent: 3.0,
            resets_at: Some(T0 + 4 * 3_600_000),
            elapsed_percent: Some(20.0),
        }),
        week: Some(WeekWindow {
            percent: 99.0,
            resets_at: Some(T0 + 2 * 86_400_000),
            elapsed_percent: Some(71.4),
            day_part: Some(DayPart {
                index: 5,
                percent: 93.0,
            }),
        }),
        models: vec![ModelWeek {
            label: "Week (Fable)".into(),
            percent: 0.0,
            resets_at: Some(T0 + 2 * 86_400_000),
        }],
    }));
    insta::assert_snapshot!(text::render(&d));
}

#[test]
fn text_claude_stale_with_missing_week() {
    let mut d = doc();
    d.claude = Some(Part::Ok(Claude {
        source: "server",
        read_at: T0 - 3 * 3_600_000,
        stale: true,
        session: Some(Win {
            percent: 11.0,
            resets_at: Some(T0 + 60_000),
            elapsed_percent: None,
        }),
        week: None,
        models: vec![],
    }));
    insta::assert_snapshot!(text::render(&d));
}

#[test]
fn text_sections_are_separated_by_a_blank_line_and_errors_show() {
    let mut d = doc();
    d.cpu = Some(Part::Failed {
        error: "cannot read /proc/stat: No such file or directory (os error 2)".into(),
    });
    d.memory = Some(Part::Ok(Memory {
        total_bytes: 1024,
        used_bytes: 512,
        available_bytes: 512,
        cached_bytes: 0,
        buffers_bytes: 0,
        used_percent: 50.0,
        swap_total_bytes: 0,
        swap_used_bytes: 0,
        swap_percent: 0.0,
    }));
    insta::assert_snapshot!(text::render(&d));
}
