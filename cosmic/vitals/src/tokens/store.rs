// SPDX-License-Identifier: MIT
//! The on-disk token index: `$XDG_CACHE_HOME/vitals/tokens-v1.bin`.
//!
//! Layout (little endian; `v` is a LEB128 varint): magic `VTKN`, version byte, saved-at ms, then the files
//! (path, dev, ino, offset), the minute buckets, the hour buckets, the dedupe ids (48-bit hash and day packed in a `u64`), and last a
//! 64-bit FNV-1a checksum of everything before it. Writes go to a temp file renamed over the target, so a
//! crash never leaves half an index. A missing, damaged or other-version file is deleted and rebuilt.

use std::collections::HashMap;
use std::ffi::OsString;
use std::fs;
use std::io::{self, Write};
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::path::{Path, PathBuf};

use super::index::{FileState, Index};
use super::{ByFamily, Counts, FAMILIES};

const MAGIC: &[u8; 4] = b"VTKN";
/// Bump when the layout changes; older files are then rebuilt.
pub const VERSION: u8 = 1;

/// Why a saved index was not used.
#[derive(Debug, PartialEq, Eq)]
pub enum LoadError {
    Missing,
    /// Wrong magic, other version, bad checksum or truncated.
    Bad(&'static str),
}

fn checksum(bytes: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for &b in bytes {
        h ^= u64::from(b);
        h = h.wrapping_mul(0x0000_0100_0000_01b3);
    }
    h
}

fn put_varint(out: &mut Vec<u8>, mut n: u64) {
    while n >= 0x80 {
        out.push((n as u8 & 0x7f) | 0x80);
        n >>= 7;
    }
    out.push(n as u8);
}

fn put_family(out: &mut Vec<u8>, by: &ByFamily) {
    for c in by {
        for n in [c.input, c.cache_write, c.cache_read, c.output] {
            put_varint(out, n);
        }
    }
}

/// Serializes the index (without touching the disk).
pub fn encode(index: &Index, saved_at_ms: i64) -> Vec<u8> {
    let mut out = Vec::with_capacity(64 + index.seen.len() * 10 + index.minutes.len() * 64);
    out.extend_from_slice(MAGIC);
    out.push(VERSION);
    out.extend_from_slice(&saved_at_ms.to_le_bytes());

    put_varint(&mut out, index.files.len() as u64);
    // Sorted, so identical content gives identical bytes.
    let mut files: Vec<_> = index.files.iter().collect();
    files.sort_by(|a, b| a.0.cmp(b.0));
    for (path, st) in files {
        let p = path.as_os_str().as_bytes();
        put_varint(&mut out, p.len() as u64);
        out.extend_from_slice(p);
        put_varint(&mut out, st.dev);
        put_varint(&mut out, st.ino);
        put_varint(&mut out, st.offset);
    }
    for map in [&index.minutes, &index.hours] {
        put_varint(&mut out, map.len() as u64);
        for (k, by) in map {
            out.extend_from_slice(&k.to_le_bytes());
            put_family(&mut out, by);
        }
    }
    put_varint(&mut out, index.seen.len() as u64);
    for entry in index.seen.entries() {
        out.extend_from_slice(&entry.to_le_bytes());
    }
    let sum = checksum(&out);
    out.extend_from_slice(&sum.to_le_bytes());
    out
}

struct Reader<'a> {
    buf: &'a [u8],
    pos: usize,
}

impl<'a> Reader<'a> {
    fn take(&mut self, n: usize) -> Result<&'a [u8], LoadError> {
        let end = self.pos.checked_add(n).filter(|&e| e <= self.buf.len());
        let end = end.ok_or(LoadError::Bad("truncated"))?;
        let s = &self.buf[self.pos..end];
        self.pos = end;
        Ok(s)
    }

    fn u64_le(&mut self) -> Result<u64, LoadError> {
        Ok(u64::from_le_bytes(self.take(8)?.try_into().unwrap()))
    }

    fn varint(&mut self) -> Result<u64, LoadError> {
        let mut n = 0u64;
        for shift in (0..64).step_by(7) {
            let b = self.take(1)?[0];
            n |= u64::from(b & 0x7f) << shift;
            if b & 0x80 == 0 {
                return Ok(n);
            }
        }
        Err(LoadError::Bad("varint too long"))
    }

    fn family(&mut self) -> Result<ByFamily, LoadError> {
        let mut by = [Counts::default(); FAMILIES];
        for c in &mut by {
            *c = Counts {
                input: self.varint()?,
                cache_write: self.varint()?,
                cache_read: self.varint()?,
                output: self.varint()?,
            };
        }
        Ok(by)
    }
}

/// Parses bytes written by [`encode`].
pub fn decode(buf: &[u8]) -> Result<Index, LoadError> {
    if buf.len() < MAGIC.len() + 1 + 8 + 8 || &buf[..4] != MAGIC {
        return Err(LoadError::Bad("not an index"));
    }
    if buf[4] != VERSION {
        return Err(LoadError::Bad("other version"));
    }
    let (body, tail) = buf.split_at(buf.len() - 8);
    if checksum(body) != u64::from_le_bytes(tail.try_into().unwrap()) {
        return Err(LoadError::Bad("checksum"));
    }
    let mut r = Reader {
        buf: body,
        pos: 5 + 8,
    };
    let mut index = Index::new();

    let nfiles = r.varint()?;
    let mut files = HashMap::new();
    for _ in 0..nfiles {
        let len = r.varint()? as usize;
        let path = PathBuf::from(OsString::from_vec(r.take(len)?.to_vec()));
        let (dev, ino, offset) = (r.varint()?, r.varint()?, r.varint()?);
        files.insert(path, FileState { dev, ino, offset });
    }
    index.files = files;
    for which in 0..2 {
        let n = r.varint()?;
        for _ in 0..n {
            let key = r.u64_le()? as i64;
            let by = r.family()?;
            let map = if which == 0 {
                &mut index.minutes
            } else {
                &mut index.hours
            };
            map.insert(key, by);
        }
    }
    let nids = r.varint()?;
    index.seen.reserve(nids.min(10_000_000) as usize);
    for _ in 0..nids {
        index.seen.insert_entry(r.u64_le()?);
    }
    if r.pos != body.len() {
        return Err(LoadError::Bad("trailing bytes"));
    }
    Ok(index)
}

/// Reads the index at `path`. A file that cannot be used is deleted (so the next save starts clean) and
/// reported as [`LoadError::Bad`].
pub fn load(path: &Path) -> Result<Index, LoadError> {
    let bytes = match fs::read(path) {
        Ok(b) => b,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Err(LoadError::Missing),
        Err(_) => {
            let _ = fs::remove_file(path);
            return Err(LoadError::Bad("unreadable"));
        }
    };
    decode(&bytes).inspect_err(|_| {
        let _ = fs::remove_file(path);
    })
}

/// Writes the index atomically: a temp file in the same folder, then a rename over the target.
pub fn save(path: &Path, index: &Index, saved_at_ms: i64) -> io::Result<()> {
    let dir = path.parent().unwrap_or(Path::new("."));
    fs::create_dir_all(dir)?;
    let tmp = dir.join(format!(
        ".{}.tmp{}",
        path.file_name()
            .map_or_else(|| "index".into(), |n| n.to_string_lossy()),
        std::process::id()
    ));
    let result = (|| {
        let mut f = fs::File::create(&tmp)?;
        f.write_all(&encode(index, saved_at_ms))?;
        f.flush()?;
        fs::rename(&tmp, path)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tokens::Family;
    use crate::tokens::scan::Record;

    fn sample_index() -> Index {
        let now = 1_793_251_032_646;
        let mut ix = Index::new();
        for i in 0..50u64 {
            ix.add(
                &Record {
                    ts_ms: now - (i as i64) * 90_000,
                    id_hash: i.wrapping_mul(0x9e37_79b9_7f4a_7c15),
                    family: Family::ALL[(i % 5) as usize],
                    counts: Counts {
                        input: i,
                        cache_write: i * 1000,
                        cache_read: i * 1_000_000,
                        output: 300 + i,
                    },
                },
                now,
            );
        }
        ix.files.insert(
            PathBuf::from("/a/b c.jsonl"),
            FileState {
                dev: 66,
                ino: 12345,
                offset: 999_999_999_999,
            },
        );
        ix
    }

    #[test]
    fn round_trips() {
        let ix = sample_index();
        let back = decode(&encode(&ix, 7)).unwrap();
        assert_eq!(back.files, ix.files);
        assert_eq!(back.minutes, ix.minutes);
        assert_eq!(back.hours, ix.hours);
        let sorted = |s: &Index| {
            let mut v: Vec<u64> = s.seen.entries().collect();
            v.sort_unstable();
            v
        };
        assert_eq!(sorted(&back), sorted(&ix));
    }

    #[test]
    fn damage_is_detected() {
        let bytes = encode(&sample_index(), 7);
        let mut flipped = bytes.clone();
        flipped[40] ^= 1;
        assert!(matches!(decode(&flipped), Err(LoadError::Bad(_))));
        assert!(matches!(
            decode(&bytes[..bytes.len() - 3]),
            Err(LoadError::Bad(_))
        ));
        let mut other = bytes.clone();
        other[4] = VERSION + 1;
        assert_eq!(decode(&other).err(), Some(LoadError::Bad("other version")));
        assert!(matches!(decode(b""), Err(LoadError::Bad(_))));
    }
}
