// SPDX-License-Identifier: MIT
//! The web dashboard server (spec §14), headless: a server on 127.0.0.1:0 over fixture `/proc` and `/sys`,
//! a temp TLS folder with a CA and a leaf made by `openssl` in the test, and std/rustls clients. No browser,
//! no fixed ports, never the real usage server folder, never `claude`.

mod common;

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::Path;
use std::process::Command;
use std::sync::Arc;
use std::time::{Duration, Instant};

use claude_usage_core::feed::{self, Feed, Window};
use common::{TempDir, assistant, copy_tree, fixture, now_ms, write};
use rustls::pki_types::pem::PemObject;
use rustls::pki_types::{CertificateDer, ServerName};
use rustls::{ClientConfig, ClientConnection, RootCertStore, StreamOwned};
use serde_json::Value;
use vitals::config::Config;
use vitals::readout::Env;
use vitals::sample::Root;
use vitals::theme::{Depth, ThemeBook, ThemeInputs};
use vitals::tokens::IndexerConfig;
use vitals::web::{Params, Server, meta_json};

const KEY: &str = "test-key-0123456789abcd";

/// A fixture machine plus a TLS folder; the server runs until the rig drops.
struct Rig {
    dir: TempDir,
    server: Server,
}

impl Drop for Rig {
    fn drop(&mut self) {
        self.server.shutdown();
    }
}

fn openssl(dir: &Path, args: &[&str]) {
    let out = Command::new("openssl")
        .args(args)
        .current_dir(dir)
        .output()
        .expect("openssl must be installed to run the web tests");
    assert!(
        out.status.success(),
        "openssl {args:?}: {}",
        String::from_utf8_lossy(&out.stderr)
    );
}

/// `ca.crt`/`ca.key` once, then any number of leaves `server.crt`/`server.key` for 127.0.0.1.
fn make_ca(dir: &Path) {
    std::fs::create_dir_all(dir).unwrap();
    openssl(
        dir,
        &[
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-keyout",
            "ca.key",
            "-out",
            "ca.crt",
            "-subj",
            "/CN=vitals test CA",
            "-days",
            "2",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign",
        ],
    );
}

fn make_leaf(dir: &Path, cn: &str) {
    write(
        &dir.join("leaf.cnf"),
        "subjectAltName=IP:127.0.0.1,DNS:localhost\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n",
    );
    openssl(
        dir,
        &[
            "req",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-keyout",
            "server.key",
            "-out",
            "server.csr",
            "-subj",
            &format!("/CN={cn}"),
        ],
    );
    openssl(
        dir,
        &[
            "x509",
            "-req",
            "-in",
            "server.csr",
            "-CA",
            "ca.crt",
            "-CAkey",
            "ca.key",
            "-CAcreateserial",
            "-out",
            "server.crt",
            "-days",
            "2",
            "-extfile",
            "leaf.cnf",
        ],
    );
}

fn env(dir: &Path) -> Env {
    let mut indexer = IndexerConfig::new(
        dir.join("claude/projects"),
        dir.join("cache/vitals/tokens-v1.bin"),
    );
    indexer.low_priority = false;
    let closed = TcpListener::bind("127.0.0.1:0").unwrap();
    Env {
        root: Root::new(dir.join("proc"), dir.join("sys")),
        interval: Duration::from_millis(40),
        indexer,
        feed_dir: Some(dir.join("run/claude-usage")),
        usage_url: Some(format!("http://{}", closed.local_addr().unwrap())),
        net_include: vec![],
        net_exclude: vec![],
    }
}

/// `tls`: build a CA and leaf in a folder next to the machine; `None` leaves the folder missing.
fn rig(tls: bool, max_streams: usize) -> Rig {
    rig_with(tls, max_streams, Some(KEY))
}

/// `auth = "none"`: no key anywhere.
fn rig_open(tls: bool) -> Rig {
    rig_with(tls, 16, None)
}

fn rig_with(tls: bool, max_streams: usize, key: Option<&str>) -> Rig {
    let dir = TempDir::new("web");
    copy_tree(&fixture("sys"), &dir.join("sys"));
    copy_tree(&fixture("proc/a"), &dir.join("proc"));
    write(
        &dir.join("proc/self/mounts"),
        "/dev/nvme0n1p3 / ext4 rw 0 0\n",
    );
    let now = now_ms();
    write(
        &dir.join("claude/projects/p/a.jsonl"),
        format!(
            "{}\n",
            assistant("a1", "claude-opus-5", now - 60_000, (10, 20, 300, 40), "hi")
        ),
    );
    let feed_dir = dir.join("run/claude-usage");
    std::fs::create_dir_all(&feed_dir).unwrap();
    let w = now / 1000;
    let f = Feed {
        written_at: w,
        five_hour: Some(Window {
            used_percentage: 3.0,
            resets_at: w + 3 * 3600,
        }),
        seven_day: Some(Window {
            used_percentage: 41.0,
            resets_at: w + 3 * 86_400,
        }),
    };
    feed::write_to(&feed_dir, &f).unwrap();
    if tls {
        make_ca(&dir.join("tls"));
        make_leaf(&dir.join("tls"), "leaf one");
    }
    let cfg = Config::default();
    let theme = ThemeBook::new(Depth::True, ThemeInputs::default(), vec![])
        .build("default")
        .0;
    let server = Server::start(Params {
        listen: "127.0.0.1:0".into(),
        key: key.map(str::to_owned),
        allow_public: false,
        tls_dir: Some(dir.join("tls")),
        tls_recheck: Duration::ZERO,
        history_min: 1,
        interval: Duration::from_millis(40),
        env: env(dir.path()),
        tokens: true,
        meta: meta_json(&cfg, &theme),
        max_streams,
    })
    .unwrap();
    Rig { dir, server }
}

struct Reply {
    status: u16,
    head: String,
    body: Vec<u8>,
}

impl Reply {
    fn header(&self, name: &str) -> Option<String> {
        self.head.lines().find_map(|l| {
            let (k, v) = l.split_once(':')?;
            k.eq_ignore_ascii_case(name).then(|| v.trim().to_owned())
        })
    }
    fn text(&self) -> String {
        String::from_utf8_lossy(&self.body).into_owned()
    }
}

fn parse(raw: &[u8]) -> Reply {
    let at = raw
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .expect("a response head");
    let head = String::from_utf8_lossy(&raw[..at]).into_owned();
    let status = head.split(' ').nth(1).unwrap().parse().unwrap();
    Reply {
        status,
        head,
        body: raw[at + 4..].to_vec(),
    }
}

fn request(path: &str, cookie: Option<&str>) -> String {
    let c = cookie
        .map(|c| format!("Cookie: {c}\r\n"))
        .unwrap_or_default();
    format!("GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n{c}Connection: close\r\n\r\n")
}

fn plain(addr: SocketAddr, path: &str, cookie: Option<&str>) -> Reply {
    let mut s = TcpStream::connect(addr).unwrap();
    s.set_read_timeout(Some(Duration::from_secs(10))).unwrap();
    s.write_all(request(path, cookie).as_bytes()).unwrap();
    let mut raw = Vec::new();
    s.read_to_end(&mut raw).unwrap();
    parse(&raw)
}

fn client_config(ca_pem: &Path) -> Arc<ClientConfig> {
    let mut roots = RootCertStore::empty();
    for c in CertificateDer::pem_file_iter(ca_pem).unwrap() {
        roots.add(c.unwrap()).unwrap();
    }
    Arc::new(
        ClientConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
            .with_safe_default_protocol_versions()
            .unwrap()
            .with_root_certificates(roots)
            .with_no_client_auth(),
    )
}

type Tls = StreamOwned<ClientConnection, TcpStream>;

fn tls_stream(addr: SocketAddr, ca_pem: &Path) -> Tls {
    let tcp = TcpStream::connect(addr).unwrap();
    tcp.set_read_timeout(Some(Duration::from_secs(10))).unwrap();
    let name = ServerName::IpAddress(std::net::IpAddr::from([127, 0, 0, 1]).into());
    StreamOwned::new(
        ClientConnection::new(client_config(ca_pem), name).unwrap(),
        tcp,
    )
}

fn https(addr: SocketAddr, ca_pem: &Path, path: &str, cookie: Option<&str>) -> Reply {
    let mut s = tls_stream(addr, ca_pem);
    s.write_all(request(path, cookie).as_bytes()).unwrap();
    let mut raw = Vec::new();
    s.read_to_end(&mut raw).unwrap();
    parse(&raw)
}

/// Reads an event stream until `needle` shows up (or ten seconds pass); returns what arrived.
fn read_until(r: &mut impl Read, needle: &str) -> String {
    let mut got = Vec::new();
    let mut buf = [0u8; 8192];
    let end = Instant::now() + Duration::from_secs(10);
    while Instant::now() < end {
        match r.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => got.extend_from_slice(&buf[..n]),
            Err(e) => panic!(
                "stream read failed: {e}; got {}",
                String::from_utf8_lossy(&got)
            ),
        }
        if String::from_utf8_lossy(&got).contains(needle) {
            break;
        }
    }
    String::from_utf8_lossy(&got).into_owned()
}

fn event_data(stream: &str, name: &str) -> Value {
    let marker = format!("event: {name}\ndata: ");
    let at = stream
        .find(&marker)
        .unwrap_or_else(|| panic!("no {name} event in {stream}"))
        + marker.len();
    let line = stream[at..].lines().next().unwrap();
    serde_json::from_str(line).unwrap_or_else(|e| panic!("{name} event is not JSON: {e}"))
}

#[test]
fn plain_http_serves_only_the_setup_page_and_the_certificate() {
    let r = rig(true, 16);
    let addr = r.server.local_addr();
    assert!(r.server.tls_loaded());
    let setup = plain(addr, "/setup", None);
    assert_eq!(setup.status, 200);
    let text = setup.text();
    assert!(text.contains("Download certificate") && text.contains("Open dashboard (HTTPS)"));
    // The fingerprint shown is the CA's SHA-256: 32 bytes as AA:BB:...
    let fp = Command::new("openssl")
        .args(["x509", "-in"])
        .arg(r.dir.join("tls/ca.crt"))
        .args(["-noout", "-fingerprint", "-sha256"])
        .output()
        .unwrap();
    let want = String::from_utf8(fp.stdout).unwrap();
    let want = want.trim().split_once('=').unwrap().1.to_owned();
    assert!(
        text.contains(&want),
        "fingerprint {want} not on the setup page"
    );
    assert!(
        text.contains("https://127.0.0.1"),
        "the HTTPS button points at the host the client used"
    );

    let cert = plain(addr, "/certificate.crt", None);
    assert_eq!(cert.status, 200);
    assert_eq!(
        cert.header("content-type").as_deref(),
        Some("application/x-x509-ca-cert")
    );
    assert!(
        cert.header("content-disposition")
            .unwrap()
            .contains("vitals-certificate.crt")
    );
    assert_eq!(cert.body, std::fs::read(r.dir.join("tls/ca.crt")).unwrap());

    for path in [
        "/",
        "/vitals",
        "/vitals/stream",
        "/app.js",
        "/?key=test-key-0123456789abcd",
    ] {
        let redirected = plain(addr, path, None);
        assert_eq!(redirected.status, 302, "{path}");
        assert_eq!(
            redirected.header("location").as_deref(),
            Some("/setup"),
            "{path}"
        );
    }
}

#[test]
fn https_needs_the_key_then_a_cookie_does_it() {
    let r = rig(true, 16);
    let (addr, ca) = (r.server.local_addr(), r.dir.join("tls/ca.crt"));
    for path in ["/", "/?key=wrong", "/vitals", "/app.js", "/vitals/stream"] {
        let denied = https(addr, &ca, path, None);
        assert_eq!(denied.status, 401, "{path}");
        assert!(denied.text().contains("vitals --serve"));
    }
    let wrong = https(addr, &ca, "/", Some("vitals_key=nope"));
    assert_eq!(wrong.status, 401);

    let set = https(addr, &ca, &format!("/?key={KEY}"), None);
    assert_eq!(set.status, 302);
    assert_eq!(set.header("location").as_deref(), Some("/"));
    let cookie = set.header("set-cookie").unwrap();
    assert!(
        cookie.starts_with(&format!("vitals_key={KEY};")),
        "{cookie}"
    );
    for part in ["HttpOnly", "Secure", "SameSite=Strict", "Path=/"] {
        assert!(cookie.contains(part), "{part} missing from {cookie}");
    }

    let jar = format!("vitals_key={KEY}");
    let page = https(addr, &ca, "/", Some(&jar));
    assert_eq!(page.status, 200);
    assert!(page.text().contains("<title>vitals</title>"));
    assert!(page.header("content-security-policy").is_some());
    for path in ["/app.js", "/app.css", "/uplot.js", "/uplot.css"] {
        assert_eq!(https(addr, &ca, path, Some(&jar)).status, 200, "{path}");
    }
    // Open to anyone: what an installed app fetches before it has a cookie.
    for (path, ct) in [
        ("/manifest.webmanifest", "application/manifest+json"),
        ("/icon.svg", "image/svg+xml"),
        ("/icon-192.png", "image/png"),
        ("/icon-512.png", "image/png"),
        ("/sw.js", "text/javascript; charset=utf-8"),
        ("/setup", "text/html; charset=utf-8"),
        ("/certificate.crt", "application/x-x509-ca-cert"),
    ] {
        let open = https(addr, &ca, path, None);
        assert_eq!(open.status, 200, "{path}");
        assert_eq!(open.header("content-type").as_deref(), Some(ct), "{path}");
    }
    let manifest: Value =
        serde_json::from_slice(&https(addr, &ca, "/manifest.webmanifest", None).body).unwrap();
    assert_eq!(manifest["display"], "standalone");

    // The key may also ride in the query on the data endpoints (curl).
    assert_eq!(
        https(addr, &ca, &format!("/vitals?key={KEY}"), None).status,
        200
    );
    // Read-only: anything but GET and HEAD is refused.
    let mut s = tls_stream(addr, &ca);
    s.write_all(
        format!("POST /vitals?key={KEY} HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n")
            .as_bytes(),
    )
    .unwrap();
    let mut raw = Vec::new();
    s.read_to_end(&mut raw).unwrap();
    assert_eq!(parse(&raw).status, 405);
}

#[test]
fn vitals_is_the_readout_object() {
    let r = rig(true, 16);
    let (addr, ca) = (r.server.local_addr(), r.dir.join("tls/ca.crt"));
    let reply = https(addr, &ca, &format!("/vitals?key={KEY}"), None);
    assert_eq!(reply.status, 200);
    assert_eq!(
        reply.header("content-type").as_deref(),
        Some("application/json")
    );
    let v: Value = serde_json::from_slice(&reply.body).unwrap();
    assert!(v["schema"].as_u64().unwrap() >= 1);
    assert!(v["sampled_at"].as_i64().unwrap() > 0);
    for section in ["cpu", "memory", "disk", "network", "tokens", "claude"] {
        assert!(
            v.get(section).is_some_and(Value::is_object),
            "{section} missing in {v}"
        );
    }
    assert!(v["cpu"]["threads"].as_array().unwrap().len() > 1);
    assert!(v["memory"]["used_percent"].is_number());
    assert!(v["claude"]["session"]["percent"].is_number());
    assert!(v["tokens"]["today"].is_object());
    // The same text `vitals --all --json` prints: one line, schema first.
    assert!(reply.text().starts_with("{\"schema\":"));
}

#[test]
fn the_stream_sends_history_then_samples() {
    let r = rig(true, 16);
    let (addr, ca) = (r.server.local_addr(), r.dir.join("tls/ca.crt"));
    // Let a few ticks build a history first.
    std::thread::sleep(Duration::from_millis(400));
    let mut s = tls_stream(addr, &ca);
    s.write_all(request(&format!("/vitals/stream?key={KEY}"), None).as_bytes())
        .unwrap();
    let got = read_until(&mut s, "event: sample");
    assert!(got.starts_with("HTTP/1.1 200 OK"), "{got}");
    assert!(got.contains("content-type: text/event-stream"));
    let history_at = got.find("event: history").expect("history first");
    assert!(history_at < got.find("event: sample").unwrap());
    let h = event_data(&got, "history");
    assert_eq!(h["tls"], true);
    assert_eq!(h["interval_ms"], 40);
    let points = h["points"].as_array().unwrap();
    assert!(!points.is_empty(), "history carries the ticks so far");
    let p = &points[0];
    for k in [
        "t", "cpu", "th", "mem", "dr", "dw", "busy", "nd", "nu", "if",
    ] {
        assert!(p.get(k).is_some(), "point lacks {k}: {p}");
    }
    // Layout rows, section toggles and the theme's colors travel with it.
    assert_eq!(h["layout"]["rows"][0][0]["id"], "cpu");
    assert_eq!(h["layout"]["sections"]["claude"]["graph"], false);
    assert_eq!(h["theme"]["claude"], "#d97757");
    assert!(
        h["theme"]["series"]["cpu"][2]
            .as_str()
            .unwrap()
            .starts_with('#')
    );
    assert!(h["tokens"]["min"].is_array());
    let sample = event_data(&got, "sample");
    assert!(
        sample["sampled_at"].as_i64().unwrap() >= points.last().unwrap()["t"].as_i64().unwrap()
    );
    assert!(sample["cpu"].is_object());
}

#[test]
fn without_a_certificate_folder_it_is_plain_http_with_the_banner_flag() {
    let r = rig(false, 16);
    let addr = r.server.local_addr();
    assert!(!r.server.tls_loaded());
    // The dashboard itself is served over plain HTTP (key still required, cookie without Secure).
    assert_eq!(plain(addr, "/", None).status, 401);
    let set = plain(addr, &format!("/?key={KEY}"), None);
    assert_eq!(set.status, 302);
    let cookie = set.header("set-cookie").unwrap();
    assert!(
        cookie.contains("HttpOnly") && !cookie.contains("Secure"),
        "{cookie}"
    );
    assert_eq!(
        plain(addr, "/", Some(&format!("vitals_key={KEY}"))).status,
        200
    );
    // The certificate is not there to download; the setup page says so.
    assert_eq!(plain(addr, "/certificate.crt", None).status, 404);
    assert!(
        plain(addr, "/setup", None)
            .text()
            .contains("No certificate is loaded")
    );

    let mut s = TcpStream::connect(addr).unwrap();
    s.set_read_timeout(Some(Duration::from_secs(10))).unwrap();
    s.write_all(request(&format!("/vitals/stream?key={KEY}"), None).as_bytes())
        .unwrap();
    let got = read_until(&mut s, "event: sample");
    assert_eq!(event_data(&got, "history")["tls"], false);

    // A TLS handshake gets nowhere: the connection is closed.
    let tcp = TcpStream::connect(addr).unwrap();
    tcp.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    let ca = {
        let d = TempDir::new("web-other-ca");
        make_ca(d.path());
        d
    };
    let name = ServerName::IpAddress(std::net::IpAddr::from([127, 0, 0, 1]).into());
    let mut t = StreamOwned::new(
        ClientConnection::new(client_config(&ca.join("ca.crt")), name).unwrap(),
        tcp,
    );
    assert!(
        t.write_all(request("/", None).as_bytes()).is_err()
            || t.read(&mut [0u8; 16]).map_or(true, |n| n == 0)
    );
}

fn leaf_der(addr: SocketAddr, ca: &Path) -> Vec<u8> {
    let mut s = tls_stream(addr, ca);
    s.write_all(request("/setup", None).as_bytes()).unwrap();
    let mut raw = Vec::new();
    s.read_to_end(&mut raw).unwrap();
    s.conn.peer_certificates().unwrap()[0].as_ref().to_vec()
}

#[test]
fn a_replaced_leaf_is_picked_up_without_a_restart() {
    let r = rig(true, 16);
    let (addr, ca) = (r.server.local_addr(), r.dir.join("tls/ca.crt"));
    let before = leaf_der(addr, &ca);
    assert_eq!(
        before,
        leaf_der(addr, &ca),
        "same leaf while the files are unchanged"
    );
    std::thread::sleep(Duration::from_millis(30));
    // The usage server reissues the leaf under the same CA.
    make_leaf(&r.dir.join("tls"), "leaf two");
    let after = leaf_der(addr, &ca);
    assert_ne!(before, after, "the new leaf is served to new connections");
    assert!(r.server.tls_loaded());
}

#[test]
fn a_half_written_leaf_keeps_the_last_good_one() {
    let r = rig(true, 16);
    let (addr, ca) = (r.server.local_addr(), r.dir.join("tls/ca.crt"));
    let before = leaf_der(addr, &ca);
    std::thread::sleep(Duration::from_millis(30));
    write(
        &r.dir.join("tls/server.crt"),
        "-----BEGIN CERTIFICATE-----\ngarbage\n",
    );
    assert_eq!(leaf_der(addr, &ca), before);
}

#[test]
fn stream_clients_are_capped_and_released() {
    let r = rig(false, 2);
    let addr = r.server.local_addr();
    let open = || {
        let mut s = TcpStream::connect(addr).unwrap();
        s.set_read_timeout(Some(Duration::from_secs(10))).unwrap();
        s.write_all(request(&format!("/vitals/stream?key={KEY}"), None).as_bytes())
            .unwrap();
        s
    };
    let mut a = open();
    let mut b = open();
    assert!(read_until(&mut a, "event: history").contains("200 OK"));
    assert!(read_until(&mut b, "event: history").contains("200 OK"));
    assert_eq!(r.server.clients(), 2);
    let mut c = open();
    let mut raw = Vec::new();
    c.read_to_end(&mut raw).unwrap();
    assert_eq!(parse(&raw).status, 503);
    // Closing a client frees its slot (the next sample write notices the closed socket).
    drop(a);
    let end = Instant::now() + Duration::from_secs(5);
    while r.server.clients() > 1 && Instant::now() < end {
        std::thread::sleep(Duration::from_millis(50));
    }
    assert_eq!(r.server.clients(), 1);
    let mut d = open();
    assert!(read_until(&mut d, "event: history").contains("200 OK"));
}

#[test]
fn a_connection_serves_several_requests_until_told_to_close() {
    let r = rig(true, 16);
    let (addr, ca) = (r.server.local_addr(), r.dir.join("tls/ca.crt"));
    let mut s = tls_stream(addr, &ca);
    let ask = |path: &str, close: bool| {
        let c = if close { "Connection: close\r\n" } else { "" };
        format!("GET {path} HTTP/1.1\r\nHost: x\r\n{c}\r\n")
    };
    // One response at a time (a browser does not pipeline): head, then content-length bytes.
    let read_one = |s: &mut Tls| -> String {
        let mut got = Vec::new();
        let mut b = [0u8; 1];
        while !got.ends_with(b"\r\n\r\n") {
            assert_eq!(s.read(&mut b).unwrap(), 1, "closed early");
            got.push(b[0]);
        }
        let head = String::from_utf8(got).unwrap();
        let n: usize = head
            .lines()
            .find_map(|l| l.strip_prefix("content-length: "))
            .unwrap()
            .parse()
            .unwrap();
        let mut body = vec![0u8; n];
        s.read_exact(&mut body).unwrap();
        head
    };
    s.write_all(ask("/manifest.webmanifest", false).as_bytes())
        .unwrap();
    let one = read_one(&mut s);
    s.write_all(ask("/icon.svg", false).as_bytes()).unwrap();
    let two = read_one(&mut s);
    s.write_all(ask("/setup", true).as_bytes()).unwrap();
    let three = read_one(&mut s);
    for h in [&one, &two, &three] {
        assert!(h.starts_with("HTTP/1.1 200 OK"), "{h}");
    }
    assert!(one.contains("connection: keep-alive") && two.contains("connection: keep-alive"));
    assert!(three.contains("connection: close"));
    let mut rest = Vec::new();
    s.read_to_end(&mut rest).unwrap();
}

#[test]
fn a_tui_save_keeps_the_web_block_that_serve_wrote() {
    let dir = TempDir::new("web-save");
    let path = dir.join("vitals/config.toml");
    // `--serve` mints a key and saves it.
    let mut served = Config::default();
    served.web.key = Some("minted-by-serve".into());
    vitals::config::save(&path, &served).unwrap();
    // A TUI that started earlier has no key in memory; the user changes a setting and it saves.
    let mut tui = Config::default();
    tui.section.cpu.window_min = 5;
    vitals::config::save_keeping_web(&path, &tui).unwrap();
    let (back, warn) = vitals::config::load(&path);
    assert!(warn.is_none());
    assert_eq!(back.web.key.as_deref(), Some("minted-by-serve"));
    assert_eq!(back.section.cpu.window_min, 5);
}

// ---- v9: no key by default, private-network guard (spec §14.3) ----

#[test]
fn auth_none_serves_everything_without_a_key_or_cookie() {
    let r = rig_open(true);
    let ca = r.dir.join("tls/ca.crt");
    let addr = r.server.local_addr();
    for path in ["/", "/app.js", "/app.css", "/vitals"] {
        let reply = https(addr, &ca, path, None);
        assert_eq!(reply.status, 200, "{path}");
        assert!(
            reply.header("set-cookie").is_none(),
            "{path} sets no cookie"
        );
    }
    let page = https(addr, &ca, "/", None).text();
    assert!(!page.to_lowercase().contains("paste") && !page.contains("vitals_key"));
    let mut s = tls_stream(addr, &ca);
    s.write_all(request("/vitals/stream", None).as_bytes())
        .unwrap();
    let head = read_until(&mut s, "event: history");
    assert!(head.starts_with("HTTP/1.1 200"), "{head}");
    // The setup page's dashboard link carries no key.
    let setup = plain(addr, "/setup", None).text();
    assert!(!setup.contains("key="));
}

#[test]
fn auth_none_plain_http_without_a_certificate_is_open_too() {
    let r = rig_open(false);
    let addr = r.server.local_addr();
    assert_eq!(plain(addr, "/", None).status, 200);
    assert_eq!(plain(addr, "/vitals", None).status, 200);
}

#[test]
fn a_key_in_the_url_is_dropped_by_a_redirect_under_auth_none() {
    let r = rig_open(false);
    let addr = r.server.local_addr();
    let a = plain(addr, "/?key=whatever", None);
    assert_eq!(a.status, 302);
    assert_eq!(a.header("location").as_deref(), Some("/"));
    assert!(a.header("set-cookie").is_none());
    let b = plain(addr, "/vitals?x=1&key=abc&y=2", None);
    assert_eq!(b.status, 302);
    assert_eq!(b.header("location").as_deref(), Some("/vitals?x=1&y=2"));
}

#[test]
fn auth_key_keeps_the_old_behavior() {
    let r = rig_with(true, 16, Some(KEY));
    let ca = r.dir.join("tls/ca.crt");
    let addr = r.server.local_addr();
    for path in ["/", "/vitals", "/vitals/stream"] {
        let reply = https(addr, &ca, path, None);
        assert_eq!(reply.status, 401, "{path}");
    }
    let set = https(addr, &ca, &format!("/?key={KEY}"), None);
    assert_eq!(set.status, 302);
    assert!(
        set.header("set-cookie")
            .unwrap()
            .starts_with(&format!("vitals_key={KEY};"))
    );
    let jar = format!("vitals_key={KEY}");
    assert_eq!(https(addr, &ca, "/", Some(&jar)).status, 200);
    assert_eq!(https(addr, &ca, "/vitals", Some(&jar)).status, 200);
}

#[test]
fn no_key_is_minted_or_written_under_auth_none() {
    let dir = TempDir::new("web-nokey");
    let path = dir.join("vitals/config.toml");
    // A config from the running install: a key minted by an older version.
    write(&path, "[web]\nkey = \"old-minted-key\"\n");
    let before = std::fs::read_to_string(&path).unwrap();
    let (mut cfg, _) = vitals::config::load(&path);
    assert_eq!(cfg.web.auth, vitals::web::conf::Auth::None);
    assert_eq!(
        vitals::web::resolve_key(&mut cfg, Some(&path)).unwrap(),
        None
    );
    assert_eq!(
        std::fs::read_to_string(&path).unwrap(),
        before,
        "config untouched"
    );
    assert_eq!(cfg.web.key.as_deref(), Some("old-minted-key"));

    // No config file at all: still nothing written.
    let none = dir.join("other/config.toml");
    let mut fresh = Config::default();
    assert_eq!(
        vitals::web::resolve_key(&mut fresh, Some(&none)).unwrap(),
        None
    );
    assert!(!none.exists() && fresh.web.key.is_none());

    // auth = "key" mints and saves one.
    let mut keyed = Config::default();
    keyed.web.auth = vitals::web::conf::Auth::Key;
    let k = vitals::web::resolve_key(&mut keyed, Some(&none))
        .unwrap()
        .unwrap();
    assert_eq!(k.len(), 22);
    let (back, _) = vitals::config::load(&none);
    assert_eq!(back.web.key.as_deref(), Some(k.as_str()));
    assert_eq!(back.web.auth, vitals::web::conf::Auth::Key);
}

#[test]
fn the_private_network_classifier_covers_every_range() {
    use vitals::web::conf::is_private;
    let ok = |s: &str| is_private(s.parse().unwrap());
    for a in [
        "127.0.0.1",
        "127.9.9.9",
        "10.0.0.1",
        "10.255.255.255",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.54",
        "169.254.1.1",
        "100.64.0.1",
        "100.127.255.255",
        "::1",
        "fc00::1",
        "fd12:3456::1",
        "fe80::1",
        "febf::1",
        "::ffff:192.168.1.5",
        "::ffff:127.0.0.1",
    ] {
        assert!(ok(a), "{a} should be private");
    }
    for a in [
        "8.8.8.8",
        "1.1.1.1",
        "172.15.255.255",
        "172.32.0.1",
        "192.167.1.1",
        "100.63.255.255",
        "100.128.0.1",
        "169.253.1.1",
        "11.0.0.1",
        "2001:4860::8888",
        "fec0::1",
        "fe00::1",
        "::ffff:8.8.8.8",
        "0.0.0.0",
    ] {
        assert!(!ok(a), "{a} should be public");
    }
}

#[test]
fn a_loopback_connection_passes_the_guard() {
    let r = rig_open(false);
    let addr = r.server.local_addr();
    assert!(addr.ip().is_loopback());
    assert_eq!(plain(addr, "/vitals", None).status, 200);
}
