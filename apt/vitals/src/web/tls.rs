// SPDX-License-Identifier: MIT
//! The TLS files you supply, loaded and reloaded (spec §14). vitals mints no certificates: it reads
//! `server.crt` and `server.key` for TLS and serves `ca.crt` for download. `ca.key` is never touched.

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime};

use rustls::ServerConfig;
use rustls::pki_types::pem::PemObject;
use rustls::pki_types::{CertificateDer, PrivateKeyDer};

/// How often the files' mtimes are looked at, at most.
pub const RECHECK: Duration = Duration::from_secs(60);

/// `$VITALS_DATA_DIR/tls`, else `~/.local/share/vitals/tls`.
pub fn default_dir(get: &dyn Fn(&str) -> Option<String>) -> Option<PathBuf> {
    let non_empty = |k: &str| get(k).filter(|v| !v.is_empty());
    match non_empty("VITALS_DATA_DIR") {
        Some(d) => Some(PathBuf::from(d).join("tls")),
        None => Some(PathBuf::from(non_empty("HOME")?).join(".local/share/vitals/tls")),
    }
}

type Stamp = [Option<(SystemTime, u64)>; 2];

fn stamp(dir: &Path) -> Stamp {
    ["server.crt", "server.key"].map(|f| {
        std::fs::metadata(dir.join(f))
            .ok()
            .and_then(|m| Some((m.modified().ok()?, m.len())))
    })
}

fn load(dir: &Path) -> Result<ServerConfig, String> {
    let certs = CertificateDer::pem_file_iter(dir.join("server.crt"))
        .map_err(|e| format!("server.crt: {e}"))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| format!("server.crt: {e}"))?;
    if certs.is_empty() {
        return Err("server.crt holds no certificate".into());
    }
    let key = PrivateKeyDer::from_pem_file(dir.join("server.key"))
        .map_err(|e| format!("server.key: {e}"))?;
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let mut cfg = ServerConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .map_err(|e| e.to_string())?
        .with_no_client_auth()
        .with_single_cert(certs, key)
        .map_err(|e| format!("certificate and key do not fit: {e}"))?;
    cfg.alpn_protocols = vec![b"http/1.1".to_vec()];
    Ok(cfg)
}

/// SHA-256 of the first certificate in a PEM file, as `AA:BB:...`.
pub fn fingerprint(pem: &Path) -> Option<String> {
    let der = CertificateDer::pem_file_iter(pem).ok()?.next()?.ok()?;
    let d = ring::digest::digest(&ring::digest::SHA256, &der);
    Some(
        d.as_ref()
            .iter()
            .map(|b| format!("{b:02X}"))
            .collect::<Vec<_>>()
            .join(":"),
    )
}

struct Inner {
    cfg: Option<Arc<ServerConfig>>,
    stamp: Stamp,
    checked: Option<Instant>,
    error: Option<String>,
}

/// The current TLS configuration, re-read when the files change (checked at most every `recheck`,
/// and only when a connection asks).
pub struct TlsStore {
    dir: PathBuf,
    recheck: Duration,
    inner: Mutex<Inner>,
}

impl TlsStore {
    pub fn new(dir: PathBuf, recheck: Duration) -> Self {
        let s = TlsStore {
            dir,
            recheck,
            inner: Mutex::new(Inner {
                cfg: None,
                stamp: [None, None],
                checked: None,
                error: None,
            }),
        };
        let _ = s.current();
        s
    }

    pub fn dir(&self) -> &Path {
        &self.dir
    }

    /// Why the files could not be loaded (the last attempt), if they could not.
    pub fn error(&self) -> Option<String> {
        self.inner.lock().ok()?.error.clone()
    }

    /// The configuration to accept TLS with, or `None` while there is no usable certificate. A file
    /// that changed is loaded again; one that fails to load (half-written) leaves the last
    /// good configuration in place and is tried again at the next check.
    pub fn current(&self) -> Option<Arc<ServerConfig>> {
        let mut g = self.inner.lock().ok()?;
        if g.checked.is_none_or(|t| t.elapsed() >= self.recheck) {
            g.checked = Some(Instant::now());
            let now = stamp(&self.dir);
            if now != g.stamp && now.iter().all(Option::is_some) {
                match load(&self.dir) {
                    Ok(c) => {
                        g.cfg = Some(Arc::new(c));
                        g.stamp = now;
                        g.error = None;
                    }
                    Err(e) => g.error = Some(e),
                }
            } else if now.iter().any(Option::is_none) && g.cfg.is_none() {
                g.error = Some(format!("no certificate in {}", self.dir.display()));
            }
        }
        g.cfg.clone()
    }
}
