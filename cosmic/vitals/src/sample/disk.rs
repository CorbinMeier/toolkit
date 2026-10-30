// SPDX-License-Identifier: MIT
//! Disk: I/O rates and busy % over whole disks (`/proc/diskstats`), and space per real mount (`statvfs`).

use std::ffi::CString;
use std::io;
use std::time::{Duration, Instant};

use super::{KeptFile, Root, lines, parse_u64, seconds_between, words};

const SECTOR_BYTES: u64 = 512;
/// Space is read again at least this often even when no mount changed.
pub const SPACE_REFRESH: Duration = Duration::from_secs(30);

/// Whole disks only: partitions would count their bytes twice, and loop, ram and device-mapper devices
/// are not hardware (the whole-disk name filter).
pub fn is_whole_disk(name: &[u8]) -> bool {
    let digits = |s: &[u8]| !s.is_empty() && s.iter().all(u8::is_ascii_digit);
    let letters = |s: &[u8]| !s.is_empty() && s.iter().all(u8::is_ascii_lowercase);
    for prefix in [&b"sd"[..], b"vd", b"xvd", b"hd"] {
        if let Some(rest) = name.strip_prefix(prefix) {
            return letters(rest);
        }
    }
    if let Some(rest) = name.strip_prefix(b"mmcblk") {
        return digits(rest);
    }
    if let Some(rest) = name.strip_prefix(b"nvme") {
        // nvme<digits>n<digits>
        return rest
            .iter()
            .position(|&b| b == b'n')
            .is_some_and(|n| digits(&rest[..n]) && digits(&rest[n + 1..]));
    }
    false
}

/// The whole disk a partition device path such as `/dev/nvme0n1p3` or `/dev/sda1` belongs to
/// (`nvme0n1`, `sda`); an empty string for anything else (device-mapper, network filesystems).
pub fn parent_disk(source: &str) -> String {
    let name = source.strip_prefix("/dev/").unwrap_or(source);
    if is_whole_disk(name.as_bytes()) {
        return name.to_string();
    }
    // Partition suffix: trailing digits, plus the `p` before them for nvme and mmcblk.
    let stem = name.trim_end_matches(|c: char| c.is_ascii_digit());
    let stem = if name.starts_with("nvme") || name.starts_with("mmcblk") {
        stem.strip_suffix('p').unwrap_or(stem)
    } else {
        stem
    };
    if stem.len() < name.len() && is_whole_disk(stem.as_bytes()) {
        stem.to_string()
    } else {
        String::new()
    }
}

#[derive(Clone, Copy, Default)]
struct Counters {
    read_bytes: u64,
    write_bytes: u64,
    io_ticks_ms: u64,
}

/// One whole disk.
pub struct DiskDev {
    pub name: String,
    prev: Option<Counters>,
    seen: bool,
    pub read_bps: f64,
    pub write_bps: f64,
    /// Time the device had I/O in flight, as a percent of the interval (0 to 100).
    pub busy_pct: f32,
}

/// Rates over all whole disks.
#[derive(Clone, Copy, Default, Debug, PartialEq)]
pub struct DiskRates {
    pub read_bps: f64,
    pub write_bps: f64,
    /// The busiest disk's busy %.
    pub busy_pct: f32,
}

/// A real mounted filesystem and its space (used = (blocks − bfree) × frsize).
#[derive(Debug, Clone)]
pub struct Mount {
    /// Device path, e.g. `/dev/nvme0n1p3`.
    pub source: String,
    pub target: String,
    pub fstype: String,
    /// Whole disk it lives on (`nvme0n1`), empty when it is not a plain partition.
    pub disk: String,
    pub total_bytes: u64,
    pub used_bytes: u64,
    /// Bytes a normal user can still write (`f_bavail`).
    pub avail_bytes: u64,
    path: CString,
}

impl Mount {
    /// Used as a percent of total.
    pub fn used_percent(&self) -> f32 {
        if self.total_bytes == 0 {
            0.0
        } else {
            (self.used_bytes as f64 / self.total_bytes as f64 * 100.0) as f32
        }
    }
}

/// Filesystems that are not storage, or that could block the UI thread (network ones hang `statvfs`
/// when the server is gone).
const SKIPPED_FS: &[&str] = &[
    "autofs",
    "bpf",
    "cgroup",
    "cgroup2",
    "configfs",
    "debugfs",
    "devpts",
    "devtmpfs",
    "efivarfs",
    "fusectl",
    "hugetlbfs",
    "mqueue",
    "nsfs",
    "overlay",
    "proc",
    "pstore",
    "ramfs",
    "securityfs",
    "squashfs",
    "sysfs",
    "tmpfs",
    "tracefs",
    "binfmt_misc",
    "rpc_pipefs",
    "selinuxfs",
    "cifs",
    "nfs",
    "nfs4",
    "smb3",
    "9p",
    "afs",
    "ceph",
    "fuse.sshfs",
    "fuse.portal",
    "fuse.gvfsd-fuse",
    "iso9660",
];

/// Decodes the `\040`-style octal escapes `/proc/self/mounts` uses for spaces and the like.
fn unescape(field: &[u8]) -> String {
    let mut out = Vec::with_capacity(field.len());
    let mut i = 0;
    while i < field.len() {
        let octal = field
            .get(i + 1..i + 4)
            .filter(|d| d.iter().all(|c| (b'0'..=b'7').contains(c)));
        if field[i] == b'\\'
            && let Some(d) = octal
        {
            out.push(((d[0] - b'0') << 6) | ((d[1] - b'0') << 3) | (d[2] - b'0'));
            i += 4;
        } else {
            out.push(field[i]);
            i += 1;
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Real mounts of `/proc/self/mounts`: block-device-backed, not a pseudo or network filesystem, and one
/// entry per device (the first mount point listed wins, so `/` beats a later bind mount). Space is 0
/// until [`Mount`]s are passed through `statvfs`.
pub fn parse_mounts(buf: &[u8]) -> Vec<Mount> {
    let mut found: Vec<Mount> = Vec::new();
    for line in lines(buf) {
        let mut w = words(line);
        let (Some(source), Some(target), Some(fstype)) = (w.next(), w.next(), w.next()) else {
            continue;
        };
        let fstype = String::from_utf8_lossy(fstype).into_owned();
        if SKIPPED_FS.contains(&fstype.as_str()) || !source.starts_with(b"/dev/") {
            continue;
        }
        let source = unescape(source);
        if found.iter().any(|m| m.source == source) {
            continue;
        }
        let target = unescape(target);
        let Ok(path) = CString::new(target.as_bytes()) else {
            continue;
        };
        found.push(Mount {
            disk: parent_disk(&source),
            source,
            target,
            fstype,
            total_bytes: 0,
            used_bytes: 0,
            avail_bytes: 0,
            path,
        });
    }
    found
}

/// Fills a mount's space with `statvfs`; false (figures left as they were) when the call fails.
fn stat_space(m: &mut Mount) -> bool {
    // SAFETY: `path` is a valid NUL-terminated string and `st` is a plain-old-data out parameter.
    let st = unsafe {
        let mut st: libc::statvfs = std::mem::zeroed();
        if libc::statvfs(m.path.as_ptr(), &mut st) != 0 {
            return false;
        }
        st
    };
    let unit = if st.f_frsize > 0 {
        st.f_frsize
    } else {
        st.f_bsize
    } as u64;
    m.total_bytes = st.f_blocks as u64 * unit;
    m.used_bytes = (st.f_blocks as u64).saturating_sub(st.f_bfree as u64) * unit;
    m.avail_bytes = st.f_bavail as u64 * unit;
    true
}

pub struct DiskSampler {
    stats: KeptFile,
    mounts_file: Option<KeptFile>,
    devices: Vec<DiskDev>,
    last: Option<Instant>,
    rates: DiskRates,
    mounts: Vec<Mount>,
    space_at: Option<Instant>,
}

impl DiskSampler {
    pub fn new(root: &Root) -> io::Result<Self> {
        Ok(DiskSampler {
            stats: KeptFile::open(&root.proc.join("diskstats"), 8 * 1024)?,
            mounts_file: KeptFile::open(&root.proc.join("self/mounts"), 16 * 1024).ok(),
            devices: Vec::new(),
            last: None,
            rates: DiskRates::default(),
            mounts: Vec::new(),
            space_at: None,
        })
    }

    /// Reads once. Rates need two samples, so the first call leaves them at 0. Mount space is refreshed
    /// when the kernel reports a mount change (`POLLPRI` on `/proc/self/mounts`) or every 30 s.
    pub fn sample(&mut self, now: Instant) -> io::Result<()> {
        let buf = self.stats.read()?;
        let dt = self.last.map(|t| seconds_between(t, now));
        for d in &mut self.devices {
            d.seen = false;
        }
        let mut rates = DiskRates::default();
        for line in lines(buf) {
            let mut w = words(line);
            let Some(name) = w.nth(2) else { continue };
            if !is_whole_disk(name) {
                continue;
            }
            // After the name: reads, merged, sectors read (3rd), ... sectors written (7th), ..., io_ticks (10th).
            let mut f = w;
            let read_bytes = parse_u64(f.nth(2).unwrap_or(b"0")) * SECTOR_BYTES;
            let write_bytes = parse_u64(f.nth(3).unwrap_or(b"0")) * SECTOR_BYTES;
            let io_ticks_ms = parse_u64(f.nth(2).unwrap_or(b"0"));
            let now_counters = Counters {
                read_bytes,
                write_bytes,
                io_ticks_ms,
            };
            let idx = match self.devices.iter().position(|d| d.name.as_bytes() == name) {
                Some(idx) => idx,
                None => {
                    self.devices.push(DiskDev {
                        name: String::from_utf8_lossy(name).into_owned(),
                        prev: None,
                        seen: false,
                        read_bps: 0.0,
                        write_bps: 0.0,
                        busy_pct: 0.0,
                    });
                    self.devices.len() - 1
                }
            };
            let dev = &mut self.devices[idx];
            dev.seen = true;
            (dev.read_bps, dev.write_bps, dev.busy_pct) = match (dev.prev, dt) {
                (Some(p), Some(dt)) => (
                    now_counters.read_bytes.saturating_sub(p.read_bytes) as f64 / dt,
                    now_counters.write_bytes.saturating_sub(p.write_bytes) as f64 / dt,
                    (now_counters.io_ticks_ms.saturating_sub(p.io_ticks_ms) as f64 / (dt * 1000.0)
                        * 100.0)
                        .clamp(0.0, 100.0) as f32,
                ),
                _ => (0.0, 0.0, 0.0),
            };
            dev.prev = Some(now_counters);
            rates.read_bps += dev.read_bps;
            rates.write_bps += dev.write_bps;
            rates.busy_pct = rates.busy_pct.max(dev.busy_pct);
        }
        self.devices.retain(|d| d.seen);
        self.rates = rates;
        self.last = Some(now);
        self.refresh_space(now);
        Ok(())
    }

    fn refresh_space(&mut self, now: Instant) {
        let due = self
            .space_at
            .is_none_or(|t| now.saturating_duration_since(t) >= SPACE_REFRESH);
        let changed = self.mounts_file.as_ref().is_some_and(|f| {
            let mut p = libc::pollfd {
                fd: f.raw_fd(),
                events: libc::POLLPRI | libc::POLLERR,
                revents: 0,
            };
            // SAFETY: one valid pollfd, zero timeout.
            unsafe {
                libc::poll(&mut p, 1, 0) > 0 && p.revents & (libc::POLLPRI | libc::POLLERR) != 0
            }
        });
        if self.space_at.is_none() || changed {
            if let Some(Ok(buf)) = self.mounts_file.as_mut().map(KeptFile::read) {
                self.mounts = parse_mounts(buf);
            }
        } else if !due {
            return;
        }
        self.mounts.iter_mut().for_each(|m| {
            stat_space(m);
        });
        self.space_at = Some(now);
    }

    /// Whole disks with their current rates.
    pub fn devices(&self) -> &[DiskDev] {
        &self.devices
    }

    /// Read and write bytes/s summed over the whole disks, and the busiest disk's busy %.
    pub fn rates(&self) -> DiskRates {
        self.rates
    }

    /// For tests: lets them pin the space figures, which come from `statvfs` on the live machine.
    #[doc(hidden)]
    pub fn mounts_mut(&mut self) -> &mut [Mount] {
        &mut self.mounts
    }

    /// Real mounts with their space, one per device.
    pub fn mounts(&self) -> &[Mount] {
        &self.mounts
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn whole_disk_filter_matches_the_server_regex() {
        for yes in [
            "sda", "sdab", "vda", "xvda", "hda", "nvme0n1", "nvme12n3", "mmcblk0",
        ] {
            assert!(is_whole_disk(yes.as_bytes()), "{yes}");
        }
        for no in [
            "sda1",
            "nvme0n1p3",
            "loop0",
            "dm-0",
            "zram0",
            "mmcblk0p1",
            "sd",
            "nvme0",
            "ram0",
        ] {
            assert!(!is_whole_disk(no.as_bytes()), "{no}");
        }
    }

    #[test]
    fn parent_disk_strips_partition_suffixes() {
        assert_eq!(parent_disk("/dev/nvme0n1p3"), "nvme0n1");
        assert_eq!(parent_disk("/dev/sda1"), "sda");
        assert_eq!(parent_disk("/dev/mmcblk0p2"), "mmcblk0");
        assert_eq!(parent_disk("/dev/sda"), "sda");
        assert_eq!(parent_disk("/dev/mapper/vg-root"), "");
    }

    #[test]
    fn mounts_skip_pseudo_filesystems_and_count_a_device_once() {
        let text = std::fs::read("tests/fixtures/proc/a/self/mounts").unwrap();
        let m = parse_mounts(&text);
        let seen: Vec<_> = m
            .iter()
            .map(|m| (m.source.as_str(), m.target.as_str()))
            .collect();
        assert_eq!(
            seen,
            vec![
                ("/dev/nvme0n1p3", "/"),
                ("/dev/nvme0n1p2", "/recovery"),
                ("/dev/nvme0n1p1", "/boot/efi"),
                ("/dev/sdb1", "/mnt/with space"),
            ]
        );
        assert_eq!(m[0].disk, "nvme0n1");
    }
}
