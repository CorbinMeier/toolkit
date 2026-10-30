// SPDX-License-Identifier: MIT
//! The `[web]` block of the config (spec §14).

use serde::{Deserialize, Serialize};

/// Default listen address: all interfaces, one port above the usage server default (4317).
pub const DEFAULT_LISTEN: &str = "0.0.0.0:4318";

/// Who may read the dashboard once connected (spec §14.3).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Auth {
    /// Every endpoint is open (the default); the private-network guard is the only gate.
    #[default]
    None,
    /// The access key in a cookie or `?key=`.
    Key,
}

impl Serialize for Auth {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(match self {
            Auth::None => "none",
            Auth::Key => "key",
        })
    }
}

impl<'de> Deserialize<'de> for Auth {
    /// `"none"` opens everything; any other value fails closed to `"key"` (a typo must not open the dashboard).
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let s = String::deserialize(d)?;
        Ok(if s.trim().eq_ignore_ascii_case("none") {
            Auth::None
        } else {
            Auth::Key
        })
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct WebConf {
    /// `ADDR:PORT` for `vitals --serve`.
    pub listen: String,
    /// `"none"` (default): no key, every endpoint open. `"key"`: the access key below is required.
    pub auth: Auth,
    /// Accept connections from any address, not only private networks (default false).
    pub allow_public: bool,
    /// The access key (`auth = "key"` only): 128 random bits, url-safe base64. Generated and written back on
    /// the first `--serve` in that mode; ignored (and left alone) under `auth = "none"`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub key: Option<String>,
    /// Minutes of graph history sent to a client on connect.
    pub history_min: u32,
    /// A TLS folder you supply (`ca.crt`, `server.crt`, `server.key`). Default `$VITALS_DATA_DIR/tls`, else
    /// `~/.local/share/vitals/tls`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tls_dir: Option<String>,
}

impl Default for WebConf {
    fn default() -> Self {
        WebConf {
            listen: DEFAULT_LISTEN.into(),
            auth: Auth::None,
            allow_public: false,
            key: None,
            history_min: 10,
            tls_dir: None,
        }
    }
}

impl WebConf {
    /// `history_min` clamped to what the server keeps (1 to 60 minutes).
    pub fn history_minutes(&self) -> u32 {
        self.history_min.clamp(1, 60)
    }
}

/// True for the addresses the guard accepts: loopback, RFC 1918, link-local, CGNAT 100.64/10 (Tailscale),
/// IPv6 loopback, ULA fc00::/7 and link-local fe80::/10. An IPv4-mapped IPv6 address is judged as IPv4.
pub fn is_private(ip: std::net::IpAddr) -> bool {
    use std::net::IpAddr;
    match ip {
        IpAddr::V4(a) => {
            let o = a.octets();
            a.is_loopback()
                || a.is_private()
                || a.is_link_local()
                || (o[0] == 100 && (o[1] & 0xc0) == 64)
        }
        IpAddr::V6(a) => {
            if let Some(v4) = a.to_ipv4_mapped() {
                return is_private(IpAddr::V4(v4));
            }
            let s = a.segments()[0];
            a.is_loopback() || (s & 0xfe00) == 0xfc00 || (s & 0xffc0) == 0xfe80
        }
    }
}

/// 16 random bytes from the kernel as 22 url-safe base64 characters (no padding).
pub fn generate_key() -> std::io::Result<String> {
    use std::io::Read;
    let mut b = [0u8; 16];
    std::fs::File::open("/dev/urandom")?.read_exact(&mut b)?;
    Ok(base64url(&b))
}

/// URL-safe base64 without padding.
pub fn base64url(bytes: &[u8]) -> String {
    const A: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for c in bytes.chunks(3) {
        let n = (u32::from(c[0]) << 16)
            | (u32::from(*c.get(1).unwrap_or(&0)) << 8)
            | u32::from(*c.get(2).unwrap_or(&0));
        for i in 0..=c.len() {
            out.push(A[(n >> (18 - 6 * i) & 63) as usize] as char);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base64url_matches_known_vectors() {
        assert_eq!(base64url(b""), "");
        assert_eq!(base64url(b"f"), "Zg");
        assert_eq!(base64url(b"fo"), "Zm8");
        assert_eq!(base64url(b"foobar"), "Zm9vYmFy");
        assert_eq!(base64url(&[0xfb, 0xff]), "-_8");
    }

    #[test]
    fn auth_defaults_to_none_and_unknown_values_fail_closed() {
        let w: WebConf = toml::from_str("").unwrap();
        assert_eq!((w.auth, w.allow_public), (Auth::None, false));
        let w: WebConf = toml::from_str("auth = \"key\"").unwrap();
        assert_eq!(w.auth, Auth::Key);
        let w: WebConf = toml::from_str("auth = \"keyy\"").unwrap();
        assert_eq!(w.auth, Auth::Key);
        let w: WebConf = toml::from_str("auth = \"none\"\nkey = \"x\"").unwrap();
        assert_eq!((w.auth, w.key.as_deref()), (Auth::None, Some("x")));
    }

    #[test]
    fn keys_are_22_chars_and_differ() {
        let (a, b) = (generate_key().unwrap(), generate_key().unwrap());
        assert_eq!(a.len(), 22);
        assert!(
            a.bytes()
                .all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_')
        );
        assert_ne!(a, b);
    }
}
