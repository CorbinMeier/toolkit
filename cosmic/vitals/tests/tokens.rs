// SPDX-License-Identifier: MIT
//! Token indexer tests: fixtures are synthetic transcripts in temp dirs; nothing reads the real `~/.claude`.

mod common;

use std::fs;
use std::io::Write;
use std::sync::mpsc::{self, Receiver};
use std::time::{Duration, Instant, SystemTime};

use common::{TempDir, assistant, now_ms, user_line, write};
use vitals::tokens::indexer::{Engine, IndexerConfig};
use vitals::tokens::{
    CacheState, Counts, Family, Indexer, Msg, Phase, TokenView, TokenWindow, sum_families,
};

struct Rig {
    dir: TempDir,
}

impl Rig {
    fn new() -> Self {
        Rig {
            dir: TempDir::new("tokens"),
        }
    }

    fn cfg(&self) -> IndexerConfig {
        let mut cfg = IndexerConfig::new(
            self.dir.join("claude/projects"),
            self.dir.join("cache/vitals/tokens-v1.bin"),
        );
        cfg.low_priority = false;
        cfg
    }

    fn file(&self, rel: &str) -> std::path::PathBuf {
        self.dir.join("claude/projects").join(rel)
    }

    fn engine(&self) -> (Engine, Receiver<Msg>) {
        let (tx, rx) = mpsc::channel();
        (Engine::new(self.cfg(), tx), rx)
    }
}

/// Runs the engine the way the thread does at start: load, scan, backfill everything, flush.
fn settle(e: &mut Engine, view: &mut TokenView, rx: &Receiver<Msg>) {
    let now = now_ms();
    e.load(now);
    e.rescan(now);
    while e.step(now) {}
    e.flush(now);
    drain(view, rx);
}

fn drain(view: &mut TokenView, rx: &Receiver<Msg>) {
    while let Ok(m) = rx.try_recv() {
        view.apply(m);
    }
}

fn lines(items: &[String]) -> String {
    items.iter().map(|l| format!("{l}\n")).collect()
}

fn total(view: &TokenView) -> Counts {
    let now = now_ms();
    sum_families(&view.sum_since(now - 3_600_000 * 24, now))
}

#[test]
fn counts_each_reply_once_across_blocks_and_files() {
    let rig = Rig::new();
    let t = now_ms() - 600_000;
    // One reply written as three lines (one per content block), plus a user line and noise.
    let a = [
        user_line(t),
        assistant(
            "A",
            "claude-opus-5-5",
            t,
            (2, 100, 1000, 50),
            "secret-text-one",
        ),
        assistant(
            "A",
            "claude-opus-5-5",
            t,
            (2, 100, 1000, 50),
            "secret-text-one",
        ),
        assistant(
            "A",
            "claude-opus-5-5",
            t,
            (2, 100, 1000, 50),
            "secret-text-one",
        ),
        assistant(
            "B",
            "claude-sonnet-5-5",
            t + 1000,
            (1, 0, 500, 20),
            "secret-text-two",
        ),
    ];
    write(&rig.file("proj1/s1.jsonl"), lines(&a));
    // A resumed session copies the earlier lines into a new file, and adds one of its own.
    let b = [
        a[1].clone(),
        a[4].clone(),
        assistant("C", "claude-haiku-5", t + 2000, (5, 0, 0, 7), "x"),
    ];
    write(&rig.file("proj2/subagents/s2.jsonl"), lines(&b));

    let (mut e, rx) = rig.engine();
    let mut view = TokenView::new();
    settle(&mut e, &mut view, &rx);

    let now = now_ms();
    let by = view.sum_since(now - 3_600_000, now);
    assert_eq!(
        by[Family::Opus.index()],
        Counts {
            input: 2,
            cache_write: 100,
            cache_read: 1000,
            output: 50
        }
    );
    assert_eq!(
        by[Family::Sonnet.index()],
        Counts {
            input: 1,
            cache_write: 0,
            cache_read: 500,
            output: 20
        }
    );
    assert_eq!(
        by[Family::Haiku.index()],
        Counts {
            input: 5,
            cache_write: 0,
            cache_read: 0,
            output: 7
        }
    );
    assert_eq!(view.progress().unwrap().phase, Phase::Live);
    assert_eq!(view.progress().unwrap().cache, CacheState::Fresh);
    assert_eq!(view.progress().unwrap().percent(), 100);
}

#[test]
fn request_id_is_the_fallback_key() {
    let rig = Rig::new();
    let t = now_ms() - 1000;
    let no_msg_id = |req: &str| {
        format!(
            r#"{{"type":"assistant","timestamp":"{}","requestId":"{req}","message":{{"model":"claude-opus-5-5","usage":{{"output_tokens":9}}}}}}"#,
            common::iso(t)
        )
    };
    write(
        &rig.file("p/a.jsonl"),
        lines(&[no_msg_id("r1"), no_msg_id("r1"), no_msg_id("r2")]),
    );
    let (mut e, rx) = rig.engine();
    let mut view = TokenView::new();
    settle(&mut e, &mut view, &rx);
    assert_eq!(total(&view).output, 18);
}

#[test]
fn a_half_written_last_line_waits_for_its_newline() {
    let rig = Rig::new();
    let t = now_ms() - 5000;
    let path = rig.file("p/live.jsonl");
    let first = assistant("L1", "claude-opus-5-5", t, (1, 0, 0, 10), "a");
    let second = assistant("L2", "claude-opus-5-5", t + 10, (1, 0, 0, 20), "b");
    let (head, tail) = second.split_at(second.len() / 2);
    write(&path, format!("{first}\n{head}"));

    let (mut e, rx) = rig.engine();
    let mut view = TokenView::new();
    settle(&mut e, &mut view, &rx);
    assert_eq!(total(&view).output, 10);

    // Still unfinished: nothing new, and the offset has not moved past the partial line.
    e.tail(now_ms());
    e.flush(now_ms());
    drain(&mut view, &rx);
    assert_eq!(total(&view).output, 10);

    let mut f = fs::OpenOptions::new().append(true).open(&path).unwrap();
    writeln!(f, "{tail}").unwrap();
    drop(f);
    e.tail(now_ms());
    e.flush(now_ms());
    drain(&mut view, &rx);
    assert_eq!(total(&view).output, 30);
}

#[test]
fn truncation_and_inode_change_reread_from_zero_without_double_counting() {
    let rig = Rig::new();
    let t = now_ms() - 5000;
    let path = rig.file("p/s.jsonl");
    let l1 = assistant("T1", "claude-opus-5-5", t, (1, 0, 0, 10), "a");
    let l2 = assistant("T2", "claude-opus-5-5", t + 1, (1, 0, 0, 20), "b");
    let l3 = assistant("T3", "claude-opus-5-5", t + 2, (1, 0, 0, 40), "c");
    write(&path, lines(&[l1.clone(), l2.clone()]));

    let (mut e, rx) = rig.engine();
    let mut view = TokenView::new();
    settle(&mut e, &mut view, &rx);
    assert_eq!(total(&view).output, 30);

    // Truncated and rewritten shorter (same inode), now holding one new reply: read again from 0.
    write(&path, lines(std::slice::from_ref(&l3)));
    assert!(fs::metadata(&path).unwrap().len() < (l1.len() + l2.len() + 2) as u64);
    e.rescan(now_ms());
    while e.step(now_ms()) {}
    e.tail(now_ms());
    e.flush(now_ms());
    drain(&mut view, &rx);
    assert_eq!(
        total(&view).output,
        70,
        "l1 and l2 stay counted once, l3 is added"
    );

    // Replaced by a new file (new inode) holding the same lines plus one: only the new one counts.
    let l4 = assistant("T4", "claude-opus-5-5", t + 3, (1, 0, 0, 80), "d");
    fs::remove_file(&path).unwrap();
    write(&path, lines(&[l1, l2, l3, l4]));
    e.rescan(now_ms());
    while e.step(now_ms()) {}
    e.flush(now_ms());
    drain(&mut view, &rx);
    assert_eq!(total(&view).output, 150);
}

#[test]
fn the_index_resumes_from_the_saved_offsets() {
    let rig = Rig::new();
    let t = now_ms() - 5000;
    let path = rig.file("p/s.jsonl");
    write(
        &path,
        lines(&[assistant("R1", "claude-opus-5-5", t, (1, 0, 0, 10), "a")]),
    );
    {
        let (mut e, rx) = rig.engine();
        let mut view = TokenView::new();
        settle(&mut e, &mut view, &rx);
        e.save(now_ms());
        assert_eq!(total(&view).output, 10);
    }
    assert!(rig.dir.join("cache/vitals/tokens-v1.bin").exists());
    // Another reply arrives while vitals was closed.
    let mut f = fs::OpenOptions::new().append(true).open(&path).unwrap();
    writeln!(
        f,
        "{}",
        assistant("R2", "claude-opus-5-5", t + 1, (1, 0, 0, 20), "b")
    )
    .unwrap();
    drop(f);

    let (mut e, rx) = rig.engine();
    let mut view = TokenView::new();
    e.load(now_ms());
    drain(&mut view, &rx);
    assert_eq!(view.progress().unwrap().cache, CacheState::Loaded);
    assert_eq!(
        total(&view).output,
        10,
        "the saved buckets are sent before any file is read"
    );
    e.rescan(now_ms());
    let mut read_steps = 0;
    while e.step(now_ms()) {
        read_steps += 1;
    }
    assert_eq!(read_steps, 1, "only the file that grew is read");
    e.flush(now_ms());
    drain(&mut view, &rx);
    assert_eq!(total(&view).output, 30);
    assert_eq!(view.progress().unwrap().phase, Phase::Live);
}

#[test]
fn a_bad_index_is_deleted_and_rebuilt() {
    for garbage in [b"not an index at all".to_vec(), Vec::new(), {
        // A real index with one flipped byte.
        let rig = Rig::new();
        write(
            &rig.file("p/s.jsonl"),
            lines(&[assistant(
                "G",
                "claude-opus-5-5",
                now_ms(),
                (1, 0, 0, 1),
                "a",
            )]),
        );
        let (mut e, rx) = rig.engine();
        let mut v = TokenView::new();
        settle(&mut e, &mut v, &rx);
        e.save(now_ms());
        let mut bytes = fs::read(rig.dir.join("cache/vitals/tokens-v1.bin")).unwrap();
        let mid = bytes.len() / 2;
        bytes[mid] ^= 0xff;
        bytes
    }] {
        let rig = Rig::new();
        let t = now_ms() - 5000;
        write(
            &rig.file("p/s.jsonl"),
            lines(&[assistant("G1", "claude-opus-5-5", t, (1, 0, 0, 10), "a")]),
        );
        let cache = rig.dir.join("cache/vitals/tokens-v1.bin");
        write(&cache, &garbage);
        let (mut e, rx) = rig.engine();
        let mut view = TokenView::new();
        settle(&mut e, &mut view, &rx);
        assert_eq!(view.progress().unwrap().cache, CacheState::Rebuilt);
        assert!(!cache.exists(), "the bad file is deleted");
        assert_eq!(total(&view).output, 10, "rebuilt from the transcripts");
        e.save(now_ms());
        assert!(cache.exists(), "and a good one is written");
    }
}

#[test]
fn files_older_than_the_backfill_window_are_not_read() {
    let rig = Rig::new();
    let t = now_ms() - 5000;
    let old = rig.file("p/old.jsonl");
    write(
        &old,
        lines(&[assistant("O1", "claude-opus-5-5", t, (1, 0, 0, 10), "a")]),
    );
    let forty_days = SystemTime::now() - Duration::from_secs(40 * 86_400);
    fs::File::options()
        .write(true)
        .open(&old)
        .unwrap()
        .set_modified(forty_days)
        .unwrap();
    write(
        &rig.file("p/new.jsonl"),
        lines(&[assistant("N1", "claude-opus-5-5", t, (1, 0, 0, 5), "a")]),
    );
    let (mut e, rx) = rig.engine();
    let mut view = TokenView::new();
    settle(&mut e, &mut view, &rx);
    assert_eq!(total(&view).output, 5);
}

#[test]
fn backfill_goes_newest_file_first_and_reports_progress() {
    let rig = Rig::new();
    let t = now_ms() - 5000;
    for (i, name) in ["a", "b", "c"].iter().enumerate() {
        let p = rig.file(&format!("p/{name}.jsonl"));
        write(
            &p,
            lines(&[assistant(
                name,
                "claude-opus-5-5",
                t,
                (1, 0, 0, 1 << i),
                "x",
            )]),
        );
        let age = SystemTime::now() - Duration::from_secs(3600 * (3 - i as u64));
        fs::File::options()
            .write(true)
            .open(&p)
            .unwrap()
            .set_modified(age)
            .unwrap();
    }
    let (mut e, rx) = rig.engine();
    let mut view = TokenView::new();
    e.load(now_ms());
    e.rescan(now_ms());
    assert!(e.step(now_ms()));
    // The newest file (c, output 4) was read first.
    e.flush(now_ms());
    drain(&mut view, &rx);
    assert_eq!(total(&view).output, 4);
    let p = view.progress().unwrap();
    assert_eq!(
        (p.phase, p.files_done, p.files_total),
        (Phase::Backfill, 1, 3)
    );
    assert!(p.percent() > 0 && p.percent() < 100);
    while e.step(now_ms()) {}
    e.flush(now_ms());
    drain(&mut view, &rx);
    assert_eq!(total(&view).output, 7);
    assert_eq!(view.progress().unwrap().phase, Phase::Live);
}

#[test]
fn message_content_never_reaches_the_index_or_the_messages() {
    let rig = Rig::new();
    let t = now_ms() - 5000;
    write(
        &rig.file("p/s.jsonl"),
        lines(&[assistant(
            "S1",
            "claude-opus-5-5",
            t,
            (1, 2, 3, 4),
            "TOP-SECRET-MARKER-TEXT",
        )]),
    );
    let (mut e, rx) = rig.engine();
    let mut view = TokenView::new();
    e.load(now_ms());
    e.rescan(now_ms());
    while e.step(now_ms()) {}
    e.flush(now_ms());
    e.save(now_ms());
    let mut debug = String::new();
    while let Ok(m) = rx.try_recv() {
        debug.push_str(&format!("{m:?}"));
        view.apply(m);
    }
    let saved = fs::read(rig.dir.join("cache/vitals/tokens-v1.bin")).unwrap();
    assert!(!saved.windows(10).any(|w| w == b"TOP-SECRET"));
    assert!(!debug.contains("SECRET"));
}

#[test]
fn a_line_longer_than_the_limit_is_skipped_not_fatal() {
    let rig = Rig::new();
    let t = now_ms() - 5000;
    let path = rig.file("p/big.jsonl");
    write(&path, "");
    let mut f = fs::OpenOptions::new().append(true).open(&path).unwrap();
    writeln!(
        f,
        "{}",
        assistant("B1", "claude-opus-5-5", t, (1, 0, 0, 1), "a")
    )
    .unwrap();
    // A 33 MB line (a pasted image, say) that even contains the word "usage".
    let junk = vec![b'x'; 33 << 20];
    f.write_all(b"{\"type\":\"user\",\"usage\":\"").unwrap();
    f.write_all(&junk).unwrap();
    f.write_all(b"\"}\n").unwrap();
    writeln!(
        f,
        "{}",
        assistant("B2", "claude-opus-5-5", t + 1, (1, 0, 0, 2), "b")
    )
    .unwrap();
    drop(f);
    let (mut e, rx) = rig.engine();
    let mut view = TokenView::new();
    settle(&mut e, &mut view, &rx);
    assert_eq!(total(&view).output, 3);
}

#[test]
fn the_indexer_thread_backfills_then_tails_live_appends() {
    let rig = Rig::new();
    let t = now_ms() - 5000;
    let path = rig.file("p/live.jsonl");
    write(
        &path,
        lines(&[assistant("Z1", "claude-sonnet-5-5", t, (1, 0, 0, 10), "a")]),
    );
    let mut cfg = rig.cfg();
    cfg.tail_every = Duration::from_millis(50);
    cfg.rescan_every = Duration::from_millis(200);
    cfg.save_every = Duration::from_millis(100);
    let indexer = Indexer::spawn(cfg).unwrap();
    let mut view = TokenView::new();
    let wait = |view: &mut TokenView, ok: &dyn Fn(&TokenView) -> bool| {
        let deadline = Instant::now() + Duration::from_secs(10);
        while Instant::now() < deadline {
            let _ = indexer
                .receiver()
                .recv_timeout(Duration::from_millis(20))
                .map(|m| view.apply(m));
            indexer.drain_into(view);
            if ok(view) {
                return;
            }
        }
        panic!("timed out");
    };
    wait(&mut view, &|v| {
        v.progress().is_some_and(|p| p.phase == Phase::Live) && total(v).output == 10
    });

    let mut f = fs::OpenOptions::new().append(true).open(&path).unwrap();
    writeln!(
        f,
        "{}",
        assistant("Z2", "claude-sonnet-5-5", now_ms(), (1, 0, 0, 5), "b")
    )
    .unwrap();
    drop(f);
    wait(&mut view, &|v| total(v).output == 15);

    // A brand-new hot file is found by the rescan.
    write(
        &rig.file("q/new.jsonl"),
        lines(&[assistant(
            "Z3",
            "claude-haiku-5",
            now_ms(),
            (1, 0, 0, 100),
            "c",
        )]),
    );
    wait(&mut view, &|v| total(v).output == 115);
    let mut series = Vec::new();
    view.series(TokenWindow::H1, now_ms(), &mut series);
    assert_eq!(series.len(), 60);
    assert_eq!(
        series.iter().map(|b| sum_families(b).output).sum::<u64>(),
        115
    );

    indexer.shutdown();
    assert!(
        rig.dir.join("cache/vitals/tokens-v1.bin").exists(),
        "saved on shutdown"
    );
}

#[test]
fn an_unreadable_file_does_not_stall_the_backfill() {
    use std::os::unix::fs::PermissionsExt;
    let rig = Rig::new();
    let t = now_ms() - 5000;
    let locked = rig.file("p/locked.jsonl");
    write(
        &locked,
        lines(&[assistant("U1", "claude-opus-5-5", t, (1, 0, 0, 99), "a")]),
    );
    fs::set_permissions(&locked, fs::Permissions::from_mode(0o000)).unwrap();
    write(
        &rig.file("p/ok.jsonl"),
        lines(&[assistant("U2", "claude-opus-5-5", t, (1, 0, 0, 5), "b")]),
    );
    let (mut e, rx) = rig.engine();
    let mut view = TokenView::new();
    // Running as root would read the locked file; skip the assertion then.
    let readable = fs::File::open(&locked).is_ok();
    settle(&mut e, &mut view, &rx);
    assert_eq!(view.progress().unwrap().phase, Phase::Live);
    assert_eq!(total(&view).output, if readable { 104 } else { 5 });
}
