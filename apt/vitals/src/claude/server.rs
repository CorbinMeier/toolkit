// SPDX-License-Identifier: MIT
//! The usage server `GET /api/usage` client (spec §3): a hand-written HTTP/1.1 request over `TcpStream` with
//! 500 ms timeouts, run on its own thread at most once every 60 s. The draw path never touches it.

use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::sync::mpsc::{self, Receiver, Sender};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use claude_usage_core::reading::{Limit, Reading};
use serde::Deserialize;

pub const TIMEOUT: Duration = Duration::from_millis(500);
/// The usage server is asked at most this often (spec §3).
pub const EVERY: Duration = Duration::from_secs(60);
const MAX_BODY: usize = 1 << 20;
/// A slow server may not hold the thread longer than this in total.
const DEADLINE: Duration = Duration::from_secs(3);

#[derive(Deserialize)]
struct WireLimit {
    label: String,
    percent: f64,
    resets_text: Option<String>,
    resets_at: Option<i64>,
}

#[derive(Deserialize)]
struct WireReading {
    session: Option<WireLimit>,
    week: Option<WireLimit>,
    #[serde(default)]
    models: Vec<WireLimit>,
}

#[derive(Deserialize)]
struct WireStatus {
    reading: Option<WireReading>,
    read_at: Option<i64>,
    #[serde(default)]
    stale: bool,
    error: Option<String>,
}

fn limit(w: WireLimit) -> Limit {
    Limit {
        label: w.label,
        percent: w.percent,
        resets_text: w.resets_text,
        resets_at: w.resets_at,
    }
}

/// What the usage server said: the last good reading, when it was taken, and whether it is old.
#[derive(Debug, Clone, PartialEq)]
pub struct ServerStatus {
    pub reading: Option<Reading>,
    /// When the usage server took the reading, ms.
    pub read_at: Option<i64>,
    pub stale: bool,
    pub error: Option<String>,
}

/// Parses the JSON body of `/api/usage`.
pub fn parse_status(body: &str) -> Option<ServerStatus> {
    let w: WireStatus = serde_json::from_str(body).ok()?;
    Some(ServerStatus {
        reading: w.reading.map(|r| Reading {
            session: r.session.map(limit),
            week: r.week.map(limit),
            models: r.models.into_iter().map(limit).collect(),
        }),
        read_at: w.read_at,
        stale: w.stale,
        error: w.error,
    })
}

/// `http://host:port` split into the host, the port and the `host:port` text.
fn split_url(url: &str) -> Result<(String, u16), String> {
    let rest = url
        .strip_prefix("http://")
        .ok_or("usage_url must start with http://")?;
    let hostport = rest.split('/').next().unwrap_or(rest);
    match hostport.rsplit_once(':') {
        Some((h, p)) => Ok((h.to_owned(), p.parse().map_err(|_| "bad usage_url port")?)),
        None => Ok((hostport.to_owned(), 80)),
    }
}

/// Decodes a `Transfer-Encoding: chunked` body; `None` if it is malformed.
pub fn decode_chunked(mut body: &[u8]) -> Option<Vec<u8>> {
    let mut out = Vec::new();
    loop {
        let eol = body.windows(2).position(|w| w == b"\r\n")?;
        let size_text = std::str::from_utf8(&body[..eol]).ok()?;
        let size = usize::from_str_radix(size_text.split(';').next()?.trim(), 16).ok()?;
        body = &body[eol + 2..];
        if size == 0 {
            return Some(out);
        }
        if body.len() < size + 2 {
            return None;
        }
        out.extend_from_slice(&body[..size]);
        body = &body[size + 2..];
    }
}

/// Splits a raw HTTP/1.1 response into its status code and body.
fn split_response(raw: &[u8]) -> Result<(u16, Vec<u8>), String> {
    let end = raw
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .ok_or("incomplete response")?;
    let head = String::from_utf8_lossy(&raw[..end]).to_ascii_lowercase();
    let status = head
        .lines()
        .next()
        .and_then(|l| l.split_whitespace().nth(1))
        .and_then(|c| c.parse().ok())
        .ok_or("bad status line")?;
    let body = &raw[end + 4..];
    if head.contains("transfer-encoding: chunked") {
        return Ok((status, decode_chunked(body).ok_or("bad chunked body")?));
    }
    Ok((status, body.to_vec()))
}

/// One GET of `base_url` + `path`, body as text. Connect, read and write each time out after 500 ms.
pub fn get(base_url: &str, path: &str) -> Result<String, String> {
    let (host, port) = split_url(base_url)?;
    let addr = (host.as_str(), port)
        .to_socket_addrs()
        .map_err(|e| e.to_string())?
        .next()
        .ok_or("no address")?;
    let mut s = TcpStream::connect_timeout(&addr, TIMEOUT).map_err(|e| e.to_string())?;
    s.set_read_timeout(Some(TIMEOUT))
        .map_err(|e| e.to_string())?;
    s.set_write_timeout(Some(TIMEOUT))
        .map_err(|e| e.to_string())?;
    let request = format!(
        "GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nAccept: application/json\r\nConnection: close\r\n\r\n"
    );
    s.write_all(request.as_bytes()).map_err(|e| e.to_string())?;
    let started = Instant::now();
    let mut raw = Vec::new();
    let mut chunk = [0u8; 4096];
    loop {
        match s.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => raw.extend_from_slice(&chunk[..n]),
            Err(e) => return Err(e.to_string()),
        }
        if raw.len() > MAX_BODY || started.elapsed() > DEADLINE {
            return Err("response too large or too slow".into());
        }
    }
    let (status, body) = split_response(&raw)?;
    if status != 200 {
        return Err(format!("usage server answered {status}"));
    }
    String::from_utf8(body).map_err(|_| "body is not UTF-8".into())
}

/// Fetches and parses `/api/usage`.
pub fn fetch(base_url: &str) -> Result<ServerStatus, String> {
    let body = get(base_url, "/api/usage")?;
    parse_status(&body).ok_or_else(|| "unreadable /api/usage JSON".into())
}

/// The background fetcher: one request now, then one every [`EVERY`], results over a channel.
pub struct ServerThread {
    pub rx: Receiver<Result<ServerStatus, String>>,
    stop: Sender<()>,
    handle: Option<JoinHandle<()>>,
}

impl ServerThread {
    pub fn spawn(base_url: String) -> std::io::Result<ServerThread> {
        let (tx, rx) = mpsc::channel();
        let (stop, stopped) = mpsc::channel::<()>();
        let handle = thread::Builder::new()
            .name("vitals-usage-server".into())
            .spawn(move || {
                loop {
                    if tx.send(fetch(&base_url)).is_err() {
                        return;
                    }
                    // A message or a closed channel both mean stop; only a timeout means ask again.
                    if stopped.recv_timeout(EVERY) != Err(mpsc::RecvTimeoutError::Timeout) {
                        return;
                    }
                }
            })?;
        Ok(ServerThread {
            rx,
            stop,
            handle: Some(handle),
        })
    }

    /// Stops the thread. A request in flight ends within its 500 ms timeouts.
    pub fn shutdown(mut self) {
        let _ = self.stop.send(());
        if let Some(h) = self.handle.take() {
            let _ = h.join();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    #[test]
    fn chunked_bodies_decode() {
        let body = b"5\r\nhello\r\n6;ext=1\r\n world\r\n0\r\n\r\n";
        assert_eq!(decode_chunked(body).unwrap(), b"hello world");
        assert!(decode_chunked(b"zz\r\nabc").is_none());
        assert!(decode_chunked(b"5\r\nhe").is_none());
    }

    #[test]
    fn urls_split_into_host_and_port() {
        assert_eq!(
            split_url("http://127.0.0.1:4317").unwrap(),
            ("127.0.0.1".into(), 4317)
        );
        assert_eq!(
            split_url("http://localhost/x").unwrap(),
            ("localhost".into(), 80)
        );
        assert!(split_url("https://x").is_err());
        assert!(split_url("http://x:notaport").is_err());
    }

    fn serve_once(response: &'static [u8]) -> String {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", l.local_addr().unwrap());
        thread::spawn(move || {
            let (mut s, _) = l.accept().unwrap();
            let mut req = [0u8; 1024];
            let n = s.read(&mut req).unwrap();
            assert!(
                std::str::from_utf8(&req[..n])
                    .unwrap()
                    .starts_with("GET /api/usage HTTP/1.1\r\n")
            );
            s.write_all(response).unwrap();
        });
        url
    }

    #[test]
    fn fetches_with_a_content_length_body() {
        let body = r#"{"reading":null,"read_at":null,"stale":false,"error":"no run yet"}"#;
        let resp: &'static [u8] = Box::leak(
            format!("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}", body.len())
                .into_bytes()
                .into_boxed_slice(),
        );
        let st = fetch(&serve_once(resp)).unwrap();
        assert_eq!(st.error.as_deref(), Some("no run yet"));
        assert!(st.reading.is_none());
    }

    #[test]
    fn fetches_a_chunked_body_and_reports_http_errors() {
        let url = serve_once(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\n{\"re\r\n32\r\nading\":null,\"read_at\":5,\"stale\":true,\"error\":null}\r\n0\r\n\r\n",
        );
        let st = fetch(&url).unwrap();
        assert_eq!((st.read_at, st.stale), (Some(5), true));
        let url = serve_once(b"HTTP/1.1 503 Unavailable\r\nContent-Length: 0\r\n\r\n");
        assert_eq!(fetch(&url).unwrap_err(), "usage server answered 503");
    }

    #[test]
    fn a_closed_port_fails_fast() {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", l.local_addr().unwrap());
        drop(l);
        let t = Instant::now();
        assert!(fetch(&url).is_err());
        assert!(t.elapsed() < Duration::from_secs(2));
    }

    #[test]
    fn a_silent_server_times_out_in_500_ms() {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", l.local_addr().unwrap());
        let t = Instant::now();
        // The listener accepts at the OS level but never answers.
        assert!(fetch(&url).is_err());
        assert!(t.elapsed() < Duration::from_secs(2));
        drop(l);
    }
}
