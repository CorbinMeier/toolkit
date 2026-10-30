// SPDX-License-Identifier: MIT
//! The little HTTP/1.1 the dashboard needs: parse one GET request, write one response, `Connection: close`.
//! No keep-alive, no chunking, no request bodies.

use std::io::{self, Read, Write};
use std::net::TcpStream;

use rustls::{ServerConnection, StreamOwned};

thread_local! {
    /// Whether the response being written keeps the connection open (set per request by the connection thread).
    static KEEP: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

/// Chooses `connection: keep-alive` or `close` for the responses written next on this thread.
pub fn set_keep(keep: bool) {
    KEEP.set(keep);
}

/// Largest request head accepted (request line plus headers).
const MAX_HEAD: usize = 16 * 1024;

/// A connection, plain or TLS, behind one `Read + Write`.
pub enum Conn {
    Plain(TcpStream),
    Tls(Box<StreamOwned<ServerConnection, TcpStream>>),
}

impl Read for Conn {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        match self {
            Conn::Plain(s) => s.read(buf),
            Conn::Tls(s) => s.read(buf),
        }
    }
}

impl Write for Conn {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        match self {
            Conn::Plain(s) => s.write(buf),
            Conn::Tls(s) => s.write(buf),
        }
    }

    fn flush(&mut self) -> io::Result<()> {
        match self {
            Conn::Plain(s) => s.flush(),
            Conn::Tls(s) => s.flush(),
        }
    }
}

impl Conn {
    /// Ends a TLS session cleanly (close_notify) and flushes; best effort.
    pub fn finish(&mut self) {
        if let Conn::Tls(s) = self {
            s.conn.send_close_notify();
        }
        let _ = self.flush();
    }
}

/// What the routes need from a request.
#[derive(Debug, Default, Clone, PartialEq)]
pub struct Request {
    pub method: String,
    pub path: String,
    pub query: String,
    pub cookie: String,
    pub host: String,
    /// The client sent `Connection: close`.
    pub close: bool,
}

impl Request {
    pub fn is_head(&self) -> bool {
        self.method == "HEAD"
    }

    /// A query parameter's raw value (the dashboard's own values never need percent-decoding).
    pub fn param(&self, name: &str) -> Option<&str> {
        self.query
            .split('&')
            .filter_map(|kv| kv.split_once('='))
            .find(|(k, _)| *k == name)
            .map(|(_, v)| v)
    }

    /// A cookie's value.
    pub fn cookie_value(&self, name: &str) -> Option<&str> {
        self.cookie
            .split(';')
            .filter_map(|kv| kv.trim().split_once('='))
            .find(|(k, _)| *k == name)
            .map(|(_, v)| v)
    }
}

/// Reads and parses one request head. `Err` is a reason to drop the connection (or answer 400).
pub fn read_request(r: &mut impl Read) -> Result<Request, String> {
    let mut buf = Vec::with_capacity(1024);
    let mut chunk = [0u8; 1024];
    loop {
        if let Some(end) = find_head_end(&buf) {
            buf.truncate(end);
            break;
        }
        if buf.len() > MAX_HEAD {
            return Err("request head too large".into());
        }
        let n = r.read(&mut chunk).map_err(|e| e.to_string())?;
        if n == 0 {
            return Err("closed before a full request".into());
        }
        buf.extend_from_slice(&chunk[..n]);
    }
    let text = String::from_utf8_lossy(&buf);
    let mut lines = text.split("\r\n");
    let line = lines.next().unwrap_or("");
    let mut parts = line.split(' ');
    let (Some(method), Some(target), Some(version)) = (parts.next(), parts.next(), parts.next())
    else {
        return Err("bad request line".into());
    };
    if !version.starts_with("HTTP/1.") {
        return Err("not HTTP/1.x".into());
    }
    let (path, query) = target.split_once('?').unwrap_or((target, ""));
    let mut req = Request {
        method: method.to_owned(),
        path: path.to_owned(),
        query: query.to_owned(),
        ..Request::default()
    };
    for l in lines {
        let Some((name, value)) = l.split_once(':') else {
            continue;
        };
        match name.trim().to_ascii_lowercase().as_str() {
            "cookie" => {
                if !req.cookie.is_empty() {
                    req.cookie.push_str("; ");
                }
                req.cookie.push_str(value.trim());
            }
            "host" => req.host = value.trim().to_owned(),
            "connection" => req.close = value.trim().eq_ignore_ascii_case("close"),
            _ => {}
        }
    }
    Ok(req)
}

fn find_head_end(buf: &[u8]) -> Option<usize> {
    buf.windows(4).position(|w| w == b"\r\n\r\n").map(|i| i + 2)
}

/// Headers every response carries.
const COMMON: &str = "x-content-type-options: nosniff\r\nreferrer-policy: no-referrer\r\n\
content-security-policy: default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; \
connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'\r\n";

/// Writes one complete response. `head_only` sends the headers (with the real length) and no body.
pub fn respond(
    w: &mut impl Write,
    status: u16,
    content_type: &str,
    extra: &[(&str, String)],
    body: &[u8],
    head_only: bool,
) -> io::Result<()> {
    let reason = match status {
        200 => "OK",
        302 => "Found",
        400 => "Bad Request",
        401 => "Unauthorized",
        404 => "Not Found",
        405 => "Method Not Allowed",
        503 => "Service Unavailable",
        _ => "Status",
    };
    let mut head = format!(
        "HTTP/1.1 {status} {reason}\r\ncontent-type: {content_type}\r\ncontent-length: {}\r\n\
connection: {}\r\n{COMMON}",
        body.len(),
        if KEEP.get() { "keep-alive" } else { "close" }
    );
    for (k, v) in extra {
        head.push_str(&format!("{k}: {v}\r\n"));
    }
    head.push_str("\r\n");
    w.write_all(head.as_bytes())?;
    if !head_only {
        w.write_all(body)?;
    }
    w.flush()
}

/// A 302 to `to`.
pub fn redirect(
    w: &mut impl Write,
    to: &str,
    extra: &[(&str, String)],
    head_only: bool,
) -> io::Result<()> {
    let mut headers = vec![
        ("location", to.to_owned()),
        ("cache-control", "no-store".to_owned()),
    ];
    headers.extend(extra.iter().map(|(k, v)| (*k, v.clone())));
    respond(
        w,
        302,
        "text/plain; charset=utf-8",
        &headers,
        b"",
        head_only,
    )
}

/// Constant-time equality (no early exit on the first differing byte; the length may leak).
pub fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

/// Escapes text for an HTML attribute or body.
pub fn esc(s: &str) -> String {
    s.chars()
        .map(|c| match c {
            '&' => "&amp;".to_owned(),
            '<' => "&lt;".to_owned(),
            '>' => "&gt;".to_owned(),
            '"' => "&quot;".to_owned(),
            '\'' => "&#39;".to_owned(),
            c => c.to_string(),
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_a_request_head() {
        let raw = b"GET /vitals?key=abc&x=1 HTTP/1.1\r\nHost: 10.1.1.54:4318\r\nCookie: a=b; vitals_key=zzz\r\n\r\n";
        let r = read_request(&mut &raw[..]).unwrap();
        assert_eq!((r.method.as_str(), r.path.as_str()), ("GET", "/vitals"));
        assert_eq!(r.param("key"), Some("abc"));
        assert_eq!(r.param("nope"), None);
        assert_eq!(r.cookie_value("vitals_key"), Some("zzz"));
        assert_eq!(r.host, "10.1.1.54:4318");
    }

    #[test]
    fn rejects_garbage_and_truncation() {
        assert!(read_request(&mut &b"nonsense\r\n\r\n"[..]).is_err());
        assert!(read_request(&mut &b"GET / HTTP/1.1\r\nHost: x"[..]).is_err());
    }

    #[test]
    fn compares_in_constant_shape() {
        assert!(ct_eq(b"abc", b"abc"));
        assert!(!ct_eq(b"abc", b"abd"));
        assert!(!ct_eq(b"abc", b"ab"));
    }
}
