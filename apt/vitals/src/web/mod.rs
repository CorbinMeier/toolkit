// SPDX-License-Identifier: MIT
//! The web dashboard of `vitals --serve` (spec §14): one port, TLS sniffed from the first byte, a page
//! with uPlot charts, one JSON readout and a Server-Sent Events stream. Std threads and `std::net` only
//! (no async runtime); the one new dependency is `rustls` with the `ring` provider.
//!
//! - [`conf`]: the `[web]` config block and the access key.
//! - [`tls`]: the certificate files you supply, reloaded when they change.
//! - [`http`]: request parsing and responses.
//! - [`feed`]: the sampler thread and the state the client threads share.
//! - [`serve_main`]: what `vitals --serve` runs; [`Server`] is the same thing without the process.

pub mod conf;
mod feed;
mod http;
pub mod tls;

use std::io::{self, Write};
use std::net::{IpAddr, Ipv4Addr, SocketAddr, TcpListener, TcpStream};
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::time::Duration;

use rustls::{ServerConnection, StreamOwned};
use serde_json::json;

use crate::config::{self, Config, PanelId};
use crate::readout::data::Collector;
use crate::readout::{Env, Section};
use crate::theme::{self, Series, Theme, ThemeBook, ThemeInputs};
use feed::{Shared, Wake, spawn_sampler};
use http::{Conn, Request, ct_eq, esc, redirect, respond};
use ratatui::style::Color;
use tls::TlsStore;

const INDEX_HTML: &str = include_str!("../../web/index.html");
const APP_JS: &str = include_str!("../../web/app.js");
const APP_CSS: &str = include_str!("../../web/app.css");
const SETUP_HTML: &str = include_str!("../../web/setup.html");
const SW_JS: &str = include_str!("../../web/sw.js");
const MANIFEST: &str = include_str!("../../web/manifest.webmanifest");
const UPLOT_JS: &str = include_str!("../../web/vendor/uplot.min.js");
const UPLOT_CSS: &str = include_str!("../../web/vendor/uplot.min.css");
const ICON_SVG: &str = include_str!("../../web/icon.svg");
const ICON_192: &[u8] = include_bytes!("../../web/icon-192.png");
const ICON_512: &[u8] = include_bytes!("../../web/icon-512.png");
const ICON_MASKABLE: &[u8] = include_bytes!("../../web/icon-maskable-512.png");

/// Stream clients at once (spec §14.4).
pub const MAX_STREAMS: usize = 16;
/// Connections of any kind at once; beyond it new ones are dropped.
const MAX_CONNS: usize = 96;
/// Requests served on one connection before it is closed.
const MAX_REQUESTS: usize = 100;
/// Silence on an open stream longer than this gets a comment line.
const HEARTBEAT: Duration = Duration::from_secs(15);
/// Time a client gets to send its first byte and its request head.
const READ_TIMEOUT: Duration = Duration::from_secs(5);
/// A client that takes this long to accept a write is dropped (a laptop-sleep leftover).
const WRITE_TIMEOUT: Duration = Duration::from_secs(10);

/// Everything a server needs; [`serve_main`] fills it from the config and tests build their own.
pub struct Params {
    /// `ADDR:PORT`; port 0 picks a free one.
    pub listen: String,
    /// `Some` = `auth = "key"`; `None` = open (the default).
    pub key: Option<String>,
    /// Accept connections from public addresses too (`[web] allow_public`).
    pub allow_public: bool,
    pub tls_dir: Option<PathBuf>,
    /// How often the certificate files' mtimes are looked at, at most.
    pub tls_recheck: Duration,
    pub history_min: u32,
    pub interval: Duration,
    pub env: Env,
    pub tokens: bool,
    /// `"layout":…,"theme":…` as JSON members (see [`meta_json`]).
    pub meta: String,
    pub max_streams: usize,
}

struct Ctx {
    key: Option<String>,
    allow_public: bool,
    /// When each refused address was last logged (once per address per hour).
    refused: std::sync::Mutex<std::collections::HashMap<IpAddr, std::time::Instant>>,
    tls: Option<TlsStore>,
    shared: Arc<Shared>,
    conns: AtomicUsize,
    closing: AtomicBool,
}

/// A running server: the accept loop and the sampler run on their own threads.
pub struct Server {
    addr: SocketAddr,
    ctx: Arc<Ctx>,
    sampler: std::sync::Mutex<Option<std::thread::JoinHandle<()>>>,
}

impl Server {
    /// Binds, starts the sampler and the accept loop, and returns once the first sample is ready.
    pub fn start(p: Params) -> io::Result<Server> {
        let listener = TcpListener::bind(&p.listen)?;
        let addr = listener.local_addr()?;
        let want: Vec<Section> = Section::ALL
            .into_iter()
            .filter(|s| p.tokens || *s != Section::Tokens)
            .collect();
        let col = Collector::new(&want, &p.env);
        let interval_ms = p.interval.as_millis().max(1) as u64;
        let shared = Shared::new(interval_ms, p.history_min, p.max_streams, p.meta);
        let sampler = spawn_sampler(col, Arc::clone(&shared), p.interval);
        let ctx = Arc::new(Ctx {
            key: p.key,
            allow_public: p.allow_public,
            refused: Default::default(),
            tls: p.tls_dir.map(|d| TlsStore::new(d, p.tls_recheck)),
            shared,
            conns: AtomicUsize::new(0),
            closing: AtomicBool::new(false),
        });
        let accept_ctx = Arc::clone(&ctx);
        std::thread::Builder::new()
            .name("vitals-accept".into())
            .spawn(move || accept_loop(&listener, &accept_ctx))?;
        ctx.shared
            .wait_ready(p.interval * 3 + Duration::from_secs(10));
        Ok(Server {
            addr,
            ctx,
            sampler: std::sync::Mutex::new(sampler),
        })
    }

    pub fn local_addr(&self) -> SocketAddr {
        self.addr
    }

    /// True while a usable certificate is loaded.
    pub fn tls_loaded(&self) -> bool {
        self.ctx.tls.as_ref().is_some_and(|t| t.current().is_some())
    }

    /// Stream clients connected right now.
    pub fn clients(&self) -> usize {
        self.ctx.shared.clients()
    }

    /// Waits up to `limit` for the sampler thread to finish (it saves the token index on its way out).
    /// True if it finished.
    pub fn wait_stopped(&self, limit: Duration) -> bool {
        let Some(h) = self.sampler.lock().ok().and_then(|mut g| g.take()) else {
            return true;
        };
        let end = std::time::Instant::now() + limit;
        while !h.is_finished() && std::time::Instant::now() < end {
            std::thread::sleep(Duration::from_millis(20));
        }
        let done = h.is_finished();
        if done {
            let _ = h.join();
        }
        done
    }

    /// Stops the sampler (the token index is saved) and the accept loop; open streams end.
    pub fn shutdown(&self) {
        self.ctx.closing.store(true, Ordering::Relaxed);
        self.ctx.shared.stop();
        // Wake the accept call.
        let _ = TcpStream::connect_timeout(&self.addr, Duration::from_millis(500));
    }
}

struct ConnGuard<'a>(&'a AtomicUsize);

impl Drop for ConnGuard<'_> {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::Relaxed);
    }
}

fn accept_loop(listener: &TcpListener, ctx: &Arc<Ctx>) {
    for conn in listener.incoming() {
        if ctx.closing.load(Ordering::Relaxed) {
            break;
        }
        let Ok(tcp) = conn else { continue };
        if ctx.closing.load(Ordering::Relaxed) {
            break;
        }
        if !ctx.admits(&tcp) {
            continue;
        }
        if ctx.conns.fetch_add(1, Ordering::Relaxed) >= MAX_CONNS {
            ctx.conns.fetch_sub(1, Ordering::Relaxed);
            continue;
        }
        let ctx = Arc::clone(ctx);
        let spawned = std::thread::Builder::new()
            .name("vitals-conn".into())
            .spawn({
                let ctx = Arc::clone(&ctx);
                move || {
                    let _guard = ConnGuard(&ctx.conns);
                    let _ = handle(&ctx, tcp);
                }
            });
        if spawned.is_err() {
            ctx.conns.fetch_sub(1, Ordering::Relaxed);
        }
    }
}

/// One connection: sniff TLS from the first byte, read one request, answer it.
fn handle(ctx: &Ctx, tcp: TcpStream) -> io::Result<()> {
    tcp.set_read_timeout(Some(READ_TIMEOUT))?;
    tcp.set_write_timeout(Some(WRITE_TIMEOUT))?;
    let _ = tcp.set_nodelay(true);
    let mut first = [0u8; 1];
    if tcp.peek(&mut first)? == 0 {
        return Ok(());
    }
    let tls_cfg = ctx.tls.as_ref().and_then(TlsStore::current);
    let tls_loaded = tls_cfg.is_some();
    // 0x16 is a TLS handshake record; no HTTP method starts with it.
    let secure = first[0] == 0x16;
    let mut conn = if secure {
        let Some(cfg) = tls_cfg else { return Ok(()) };
        let sc = ServerConnection::new(cfg).map_err(io::Error::other)?;
        Conn::Tls(Box::new(StreamOwned::new(sc, tcp)))
    } else {
        Conn::Plain(tcp)
    };
    let view = View {
        secure,
        tls_loaded,
        // With a certificate, plain HTTP is only the way to fetch it.
        setup_only: !secure && tls_loaded,
    };
    // Keep-alive: up to MAX_REQUESTS requests per connection, each within the read timeout of the last
    // response. The event stream never keeps alive (it owns the connection until it ends).
    let mut result = Ok(());
    for _ in 0..MAX_REQUESTS {
        let Ok(req) = http::read_request(&mut conn) else {
            break;
        };
        let keep =
            !req.close && req.path != "/vitals/stream" && !ctx.closing.load(Ordering::Relaxed);
        http::set_keep(keep);
        result = route(ctx, &req, &mut conn, view);
        if !keep || result.is_err() {
            break;
        }
    }
    http::set_keep(false);
    conn.finish();
    result
}

#[derive(Clone, Copy)]
struct View {
    secure: bool,
    tls_loaded: bool,
    setup_only: bool,
}

/// How long a refused address stays quiet in the log.
const REFUSAL_LOG_EVERY: Duration = Duration::from_secs(3600);

impl Ctx {
    /// The private-network guard: false (and the stream is dropped, closing it) for a public peer.
    fn admits(&self, tcp: &TcpStream) -> bool {
        let Ok(peer) = tcp.peer_addr() else {
            return false;
        };
        if self.allow_public || conf::is_private(peer.ip()) {
            return true;
        }
        let ip = peer.ip();
        let now = std::time::Instant::now();
        let mut log = self.refused.lock().unwrap_or_else(|e| e.into_inner());
        if log.len() > 1024 {
            log.retain(|_, t| now.duration_since(*t) < REFUSAL_LOG_EVERY);
        }
        let due = log
            .get(&ip)
            .is_none_or(|t| now.duration_since(*t) >= REFUSAL_LOG_EVERY);
        if due {
            log.insert(ip, now);
            eprintln!(
                "vitals: refused a connection from {ip} (not a private network; [web] allow_public)"
            );
        }
        false
    }
}

fn key_ok(key: &str, given: &str) -> bool {
    ct_eq(given.as_bytes(), key.as_bytes())
}

/// `path` plus the query without its `key` parameter (what a `?key=` URL redirects to under `auth = "none"`).
fn clean_target(req: &Request) -> String {
    let rest: Vec<&str> = req
        .query
        .split('&')
        .filter(|kv| !kv.is_empty() && kv.split('=').next() != Some("key"))
        .collect();
    if rest.is_empty() {
        req.path.clone()
    } else {
        format!("{}?{}", req.path, rest.join("&"))
    }
}

fn route(ctx: &Ctx, req: &Request, w: &mut Conn, v: View) -> io::Result<()> {
    let head = req.is_head();
    if req.method != "GET" && req.method != "HEAD" {
        return respond(
            w,
            405,
            "text/plain",
            &[("allow", "GET, HEAD".into())],
            b"",
            head,
        );
    }
    let asset = |w: &mut Conn, ct: &str, body: &[u8]| {
        respond(
            w,
            200,
            ct,
            &[("cache-control", "no-cache".into())],
            body,
            head,
        )
    };
    // Open to everyone: the setup page, the CA, and the files an installed app needs before it has a cookie.
    match req.path.as_str() {
        "/certificate.crt" => return serve_ca(ctx, w, head),
        "/setup" => return serve_setup(ctx, req, w, v),
        _ if v.setup_only => return redirect(w, "/setup", &[], head),
        "/manifest.webmanifest" => {
            return asset(w, "application/manifest+json", MANIFEST.as_bytes());
        }
        "/icon.svg" => return asset(w, "image/svg+xml", ICON_SVG.as_bytes()),
        "/icon-192.png" => return asset(w, "image/png", ICON_192),
        "/icon-512.png" => return asset(w, "image/png", ICON_512),
        "/icon-maskable-512.png" => return asset(w, "image/png", ICON_MASKABLE),
        "/sw.js" => {
            return respond(
                w,
                200,
                "text/javascript; charset=utf-8",
                &[
                    ("cache-control", "no-cache".into()),
                    ("service-worker-allowed", "/".into()),
                ],
                SW_JS.as_bytes(),
                head,
            );
        }
        _ => {}
    }
    match &ctx.key {
        // auth = "none": nothing to check; a stale `?key=` is dropped from the address.
        None => {
            if req.param("key").is_some() {
                return redirect(w, &clean_target(req), &[], head);
            }
        }
        Some(key) => {
            let by_param = req.param("key").is_some_and(|k| key_ok(key, k));
            let by_cookie = req
                .cookie_value("vitals_key")
                .is_some_and(|k| key_ok(key, k));
            if req.path == "/" && by_param {
                let secure = if v.secure { "; Secure" } else { "" };
                let cookie = format!(
                    "vitals_key={key}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000{secure}"
                );
                return redirect(w, "/", &[("set-cookie", cookie)], head);
            }
            if !(by_param || by_cookie) {
                return respond(
                    w,
                    401,
                    "text/html; charset=utf-8",
                    &[("cache-control", "no-store".into())],
                    UNAUTHORIZED.as_bytes(),
                    head,
                );
            }
        }
    }
    match req.path.as_str() {
        "/" => asset(w, "text/html; charset=utf-8", INDEX_HTML.as_bytes()),
        "/app.js" => asset(w, "text/javascript; charset=utf-8", APP_JS.as_bytes()),
        "/app.css" => asset(w, "text/css; charset=utf-8", APP_CSS.as_bytes()),
        "/uplot.js" => asset(w, "text/javascript; charset=utf-8", UPLOT_JS.as_bytes()),
        "/uplot.css" => asset(w, "text/css; charset=utf-8", UPLOT_CSS.as_bytes()),
        "/vitals" => match ctx.shared.latest() {
            Some(s) => respond(
                w,
                200,
                "application/json",
                &[("cache-control", "no-store".into())],
                s.as_bytes(),
                head,
            ),
            None => respond(
                w,
                503,
                "application/json",
                &[],
                b"{\"error\":\"warming up\"}",
                head,
            ),
        },
        "/vitals/stream" => stream(ctx, w, v, head),
        _ => respond(w, 404, "text/plain", &[], b"not found", head),
    }
}

const UNAUTHORIZED: &str = "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">\
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>vitals</title>\
<style>body{font:16px system-ui,sans-serif;background:#10131a;color:#d8dce6;margin:0;padding:24px;max-width:34em}\
input,button{font:inherit;padding:12px;min-height:44px;border-radius:8px;border:1px solid #6c7286;background:#1a1f2b;color:inherit}\
button{background:#D97757;color:#10131a;border:0;font-weight:600}</style>\
<h1>vitals</h1><p>This dashboard needs its key. Open the URL that <code>vitals --serve</code> printed \
(it ends in <code>?key=…</code>), or paste the key here.</p>\
<form method=\"get\" action=\"/\"><input name=\"key\" autocomplete=\"off\" autocapitalize=\"off\" placeholder=\"key\"> \
<button>Open</button></form></html>";

fn serve_ca(ctx: &Ctx, w: &mut Conn, head: bool) -> io::Result<()> {
    let ca = ctx
        .tls
        .as_ref()
        .and_then(|t| std::fs::read(t.dir().join("ca.crt")).ok());
    match ca {
        Some(pem) => respond(
            w,
            200,
            "application/x-x509-ca-cert",
            &[(
                "content-disposition",
                "attachment; filename=\"vitals-certificate.crt\"".into(),
            )],
            &pem,
            head,
        ),
        None => respond(
            w,
            404,
            "text/plain",
            &[],
            b"no certificate authority file (ca.crt)",
            head,
        ),
    }
}

fn serve_setup(ctx: &Ctx, req: &Request, w: &mut Conn, v: View) -> io::Result<()> {
    let ca = ctx.tls.as_ref().map(|t| t.dir().join("ca.crt"));
    let fp = ca.as_deref().and_then(tls::fingerprint);
    let host = req.host.as_str();
    let host_ok = !host.is_empty()
        && host
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | ':' | '-' | '[' | ']'));
    let url = if host_ok {
        format!("https://{host}/")
    } else {
        "/".to_owned()
    };
    let note = match (&fp, v.tls_loaded) {
        (Some(_), true) => "",
        (_, false) => {
            "No certificate is loaded: provide a certificate folder, or the dashboard runs without HTTPS \
(no install-as-app, no keep-awake)."
        }
        (None, true) => {
            "The certificate authority file (ca.crt) is missing, so there is nothing to download."
        }
    };
    let body = SETUP_HTML
        .replace("{{HTTPS_URL}}", &esc(&url))
        .replace(
            "{{FINGERPRINT}}",
            &esc(fp.as_deref().unwrap_or("not available")),
        )
        .replace("{{NOTE}}", &esc(note));
    respond(
        w,
        200,
        "text/html; charset=utf-8",
        &[("cache-control", "no-store".into())],
        body.as_bytes(),
        req.is_head(),
    )
}

/// Server-Sent Events: `history` first, then a `sample` per tick, `tokens` every 30 s, a comment every 15 s
/// of silence. Returns when the client goes or the server stops.
fn stream(ctx: &Ctx, w: &mut Conn, v: View, head: bool) -> io::Result<()> {
    let Some(_guard) = ctx.shared.join() else {
        return respond(
            w,
            503,
            "application/json",
            &[("retry-after", "10".into())],
            b"{\"error\":\"too many clients\"}",
            head,
        );
    };
    let headers = "HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\ncache-control: no-store\r\n\
x-accel-buffering: no\r\nconnection: close\r\nx-content-type-options: nosniff\r\n\r\nretry: 3000\r\n\r\n";
    w.write_all(headers.as_bytes())?;
    if head {
        return w.flush();
    }
    let (data, mut seq, mut tseq) = ctx.shared.history_event(v.tls_loaded);
    let event = |w: &mut Conn, name: &str, data: &str| -> io::Result<()> {
        w.write_all(format!("event: {name}\ndata: {data}\n\n").as_bytes())?;
        w.flush()
    };
    event(w, "history", &data)?;
    loop {
        match ctx.shared.wait_next(seq, tseq, HEARTBEAT) {
            Wake::Sample(s, n) => {
                seq = n;
                event(w, "sample", &s)?;
            }
            Wake::Tokens(t, n) => {
                tseq = n;
                event(w, "tokens", &t)?;
            }
            Wake::Idle => {
                w.write_all(b": keep-alive\n\n")?;
                w.flush()?;
            }
            Wake::Stop => return Ok(()),
        }
    }
}

// ---- what the page is told about the server's layout and theme ----

fn hex(c: Color) -> Option<String> {
    match c {
        Color::Rgb(r, g, b) => Some(format!("#{r:02x}{g:02x}{b:02x}")),
        _ => None,
    }
}

/// `"layout":{…},"theme":{…}` for the `history` event: the config's rows, section toggles and the theme's colors.
pub fn meta_json(cfg: &Config, theme: &Theme) -> String {
    let resolved = cfg.resolve(true);
    let rows: Vec<_> = resolved
        .rows()
        .iter()
        .map(|r| {
            r.items()
                .iter()
                .map(|(p, w)| json!({"id": p.key(), "w": w}))
                .collect::<Vec<_>>()
        })
        .collect();
    let sections: serde_json::Map<_, _> = PanelId::ALL
        .into_iter()
        .map(|p| {
            let c = cfg.common(p);
            (
                p.key().to_owned(),
                json!({
                    "title": p.title(), "enabled": c.enabled, "graph": c.graph, "bars": c.bars,
                    "summary": c.summary, "window_min": cfg.window_min(p),
                }),
            )
        })
        .collect();
    let ramp = |s: Series| {
        [0.0f32, 50.0, 100.0]
            .map(|p| hex(theme.ramp(s, p)))
            .to_vec()
    };
    let series = json!({
        "cpu": ramp(Series::Cpu), "memory": ramp(Series::Mem), "cache": ramp(Series::Cache),
        "disk_read": ramp(Series::DiskRead), "disk_write": ramp(Series::DiskWrite),
        "net_down": ramp(Series::NetDown), "net_up": ramp(Series::NetUp),
        "tokens_in": ramp(Series::TokIn), "tokens_out": ramp(Series::TokOut),
        "tokens_cache": ramp(Series::TokCache), "claude": ramp(Series::Claude),
    });
    let layout = json!({
        "rows": rows, "sections": sections, "tokens_window": cfg.section.tokens.window,
    });
    let colors = json!({
        "name": theme.name, "fg": hex(theme.fg), "title": hex(theme.title), "border": hex(theme.border),
        "muted": hex(theme.muted), "accent": hex(theme.accent), "danger": hex(theme.danger),
        "claude": hex(theme.claude), "series": series,
    });
    format!("\"layout\":{layout},\"theme\":{colors}")
}

fn build_theme(cfg: &Config) -> Theme {
    let get = |k: &str| std::env::var(k).ok();
    let dir = config::config_dir(&get);
    let config_home = dir.as_ref().and_then(|d| d.parent().map(PathBuf::from));
    let inputs = ThemeInputs {
        accent: cfg.accent.as_deref().and_then(theme::parse_hex),
        cosmic_accent: config_home
            .as_deref()
            .and_then(ThemeBook::read_cosmic_accent),
    };
    let custom = dir
        .as_ref()
        .map(|d| ThemeBook::read_custom(&d.join("themes")))
        .unwrap_or_default();
    ThemeBook::new(theme::Depth::True, inputs, custom)
        .build(&cfg.theme)
        .0
}

// ---- `vitals --serve` ----

/// The flags `--serve` takes.
#[derive(Debug, Default, Clone)]
pub struct ServeArgs {
    pub listen: Option<String>,
    pub interval_ms: Option<u64>,
    pub theme: Option<String>,
    pub tokens_off: bool,
}

static SIGNALLED: AtomicBool = AtomicBool::new(false);

extern "C" fn on_signal(_: libc::c_int) {
    SIGNALLED.store(true, Ordering::Relaxed);
}

fn install_signal_handlers() {
    for sig in [libc::SIGINT, libc::SIGTERM, libc::SIGHUP] {
        // SAFETY: the handler only stores to an atomic, which is async-signal-safe.
        unsafe {
            let mut sa: libc::sigaction = std::mem::zeroed();
            sa.sa_sigaction = on_signal as extern "C" fn(libc::c_int) as usize;
            libc::sigemptyset(&mut sa.sa_mask);
            libc::sigaction(sig, &sa, std::ptr::null_mut());
        }
    }
}

/// The non-loopback IPv4 addresses of the up interfaces, skipping container bridges.
pub fn lan_ipv4() -> Vec<Ipv4Addr> {
    let mut out = Vec::new();
    let mut list: *mut libc::ifaddrs = std::ptr::null_mut();
    // SAFETY: getifaddrs fills `list` with a linked list we only read and then free with freeifaddrs.
    unsafe {
        if libc::getifaddrs(&mut list) != 0 {
            return out;
        }
        let mut p = list;
        while !p.is_null() {
            let a = &*p;
            p = a.ifa_next;
            if a.ifa_addr.is_null() || i32::from((*a.ifa_addr).sa_family) != libc::AF_INET {
                continue;
            }
            let up = a.ifa_flags & (libc::IFF_UP as u32) != 0;
            let name = std::ffi::CStr::from_ptr(a.ifa_name).to_string_lossy();
            let bridge = ["lo", "docker", "br-", "veth", "virbr", "cni", "flannel"]
                .iter()
                .any(|p| name.starts_with(p));
            if !up || bridge {
                continue;
            }
            let sin = &*(a.ifa_addr as *const libc::sockaddr_in);
            let ip = Ipv4Addr::from(u32::from_be(sin.sin_addr.s_addr));
            if !ip.is_loopback() && !out.contains(&ip) {
                out.push(ip);
            }
        }
        libc::freeifaddrs(list);
    }
    out
}

fn hostname() -> Option<String> {
    let mut buf = [0u8; 256];
    // SAFETY: the buffer is valid for its length; the result is NUL-terminated within it.
    let ok = unsafe { libc::gethostname(buf.as_mut_ptr().cast(), buf.len() - 1) } == 0;
    let end = buf.iter().position(|&b| b == 0)?;
    (ok && end > 0).then(|| String::from_utf8_lossy(&buf[..end]).into_owned())
}

/// The URLs to print: one per LAN address (or the bound address), then `<host>.local`; `?key=` only under `auth = "key"`.
pub fn dashboard_urls(addr: SocketAddr, https: bool, key: Option<&str>) -> Vec<String> {
    let scheme = if https { "https" } else { "http" };
    let mut hosts: Vec<String> = match addr.ip() {
        IpAddr::V4(ip) if ip.is_unspecified() => {
            lan_ipv4().iter().map(ToString::to_string).collect()
        }
        ip if ip.is_unspecified() => lan_ipv4().iter().map(ToString::to_string).collect(),
        ip => vec![ip.to_string()],
    };
    if addr.ip().is_unspecified()
        && let Some(h) = hostname()
    {
        hosts.push(format!("{}.local", h.trim_end_matches(".local")));
    }
    hosts
        .into_iter()
        .map(|h| match key {
            Some(k) => format!("{scheme}://{h}:{}/?key={k}", addr.port()),
            None => format!("{scheme}://{h}:{}/", addr.port()),
        })
        .collect()
}

/// The key to enforce: `None` under `auth = "none"` (nothing minted, the config file untouched, a stored
/// `key` ignored); under `auth = "key"` the stored key, minted and saved to `path` when missing.
pub fn resolve_key(
    cfg: &mut Config,
    path: Option<&std::path::Path>,
) -> Result<Option<String>, String> {
    if cfg.web.auth != conf::Auth::Key {
        return Ok(None);
    }
    if let Some(k) = cfg.web.key.clone().filter(|k| !k.is_empty()) {
        return Ok(Some(k));
    }
    let k = conf::generate_key().map_err(|e| format!("cannot make a key: {e}"))?;
    cfg.web.key = Some(k.clone());
    let Some(p) = path else {
        return Err("no config directory (HOME unset): cannot keep a key".into());
    };
    config::save(p, cfg).map_err(|e| format!("cannot save the key to {}: {e}", p.display()))?;
    eprintln!(
        "vitals: made a new access key and saved it to {}",
        p.display()
    );
    Ok(Some(k))
}

/// Runs the dashboard server until SIGINT or SIGTERM. Prints the dashboard URLs.
pub fn serve_main(a: &ServeArgs) -> Result<(), String> {
    let get = |k: &str| std::env::var(k).ok();
    let path = config::config_dir(&get).map(|d| d.join("config.toml"));
    let (mut cfg, warn) = path
        .as_deref()
        .map_or((Config::default(), None), config::load);
    if let Some(w) = warn {
        eprintln!("vitals: {w}");
    }
    let key = resolve_key(&mut cfg, path.as_deref())?;
    if let Some(t) = &a.theme {
        cfg.theme = t.clone();
    }
    let listen = a.listen.clone().unwrap_or_else(|| cfg.web.listen.clone());
    let tls_dir = cfg
        .web
        .tls_dir
        .as_ref()
        .map(PathBuf::from)
        .or_else(|| tls::default_dir(&get));
    let env = Env::from_environment(a.interval_ms);
    let params = Params {
        listen,
        key: key.clone(),
        allow_public: cfg.web.allow_public,
        tls_dir,
        tls_recheck: tls::RECHECK,
        history_min: cfg.web.history_minutes(),
        interval: env.interval,
        env,
        tokens: !a.tokens_off,
        meta: meta_json(&cfg, &build_theme(&cfg)),
        max_streams: MAX_STREAMS,
    };
    let tls_dir_shown = params.tls_dir.clone();
    install_signal_handlers();
    let server = Server::start(params).map_err(|e| format!("cannot listen: {e}"))?;
    let https = server.tls_loaded();
    let addr = server.local_addr();
    println!("vitals dashboard listening on {addr}");
    if https {
        println!("HTTPS and plain HTTP (setup page and certificate only) share this port.");
    } else {
        println!(
            "No certificate ({}): plain HTTP only, so no install-as-app and no keep-awake.",
            tls_dir_shown.map_or("no folder".into(), |d| d.display().to_string())
        );
    }
    if key.is_some() {
        println!("Access key required (auth = \"key\"; the key is in the URL, keep it private):");
    } else {
        println!("Open one of these on the device (no key; private networks only):");
    }
    for u in dashboard_urls(addr, https, key.as_deref()) {
        println!("  {u}");
    }
    if https {
        println!(
            "First time on a device: open http://<address>:{}/setup to install the certificate.",
            addr.port()
        );
    }
    let _ = std::io::stdout().flush();
    while !SIGNALLED.load(Ordering::Relaxed) {
        std::thread::sleep(Duration::from_millis(200));
    }
    server.shutdown();
    // The sampler saves the token index on its way out: wait for it, up to 5 s.
    if !server.wait_stopped(Duration::from_secs(5)) {
        eprintln!("vitals: the token index was still saving after 5 s");
    }
    Ok(())
}
