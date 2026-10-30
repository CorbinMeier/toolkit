// SPDX-License-Identifier: MIT
//! The Claude row: usage-server JSON fixtures, the feed file source, the usage server thread, and its colors per depth.
//! Never reads the real feed or `~/.claude`: every path is a temp dir.

mod common;

use std::io::{Read, Write};
use std::net::TcpListener;
use std::time::{Duration, Instant};

use claude_usage_core::feed::{self, Feed, Window};
use common::ui::{NOW, rig, server_fixture, set_server};
use common::{TempDir, write};
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::Color;
use vitals::claude::server::parse_status;
use vitals::claude::{ClaudeSource, ClaudeView, Source};
use vitals::config::PanelId;
use vitals::theme::Depth;

#[test]
fn the_four_server_fixtures_parse() {
    let fresh = parse_status(&server_fixture("fresh")).unwrap();
    let r = fresh.reading.as_ref().unwrap();
    assert_eq!(r.session.as_ref().unwrap().percent, 11.0);
    assert_eq!(r.week.as_ref().unwrap().percent, 41.0);
    assert_eq!(r.models.len(), 2);
    assert!(!fresh.stale);

    let stale = parse_status(&server_fixture("stale")).unwrap();
    assert!(stale.stale);

    let missing = parse_status(&server_fixture("missing_week")).unwrap();
    assert!(missing.reading.unwrap().week.is_none());

    let hot = parse_status(&server_fixture("hot")).unwrap();
    let r = hot.reading.unwrap();
    assert_eq!(r.session.unwrap().percent, 98.0);
    assert!(parse_status("not json").is_none());
}

#[test]
fn the_view_words_match_the_server() {
    common::ui::pin_tz();
    let mut v = ClaudeView::default();
    v.set_server(Ok(parse_status(&server_fixture("stale")).unwrap()));
    v.refresh(NOW);
    assert_eq!(v.session_reset, "37m");
    assert_eq!(v.week_reset, "3d 4h");
    assert_eq!(v.stale_text, "Stale - last read 10:30 AM");
    let m = v.merged.as_ref().unwrap();
    assert_eq!(m.source, Source::Server);
    assert!(m.stale);
    v.set_server(Ok(parse_status(&server_fixture("missing_week")).unwrap()));
    v.refresh(NOW + 120_000);
    assert!(v.week_reset.is_empty() && v.week_pace.is_none() && v.stale_text.is_empty());
    assert!(v.session_pace.unwrap() > 85.0);
}

fn feed_at(dir: &std::path::Path, written_at: i64, five: f64) {
    let f = Feed {
        written_at,
        five_hour: Some(Window {
            used_percentage: five,
            resets_at: NOW / 1000 + 600,
        }),
        seven_day: Some(Window {
            used_percentage: 20.0,
            resets_at: NOW / 1000 + 86_400,
        }),
    };
    feed::write_to(dir, &f).unwrap();
}

#[test]
fn the_feed_file_is_polled_by_mtime_every_five_seconds() {
    let t = TempDir::new("feed");
    let dir = t.join("claude-usage");
    let mut view = ClaudeView::default();
    let mut src = ClaudeSource::new(Some(dir.clone()), None);
    let t0 = Instant::now();
    // No file yet: nothing, and no crash.
    assert!(!src.poll(t0, &mut view) || view.feed().is_none());
    view.refresh(NOW);
    assert!(view.merged.is_none());

    feed_at(&dir, NOW / 1000, 30.0);
    // Within five seconds the file is not even looked at.
    assert!(!src.poll(t0 + Duration::from_secs(2), &mut view));
    assert!(src.poll(t0 + Duration::from_secs(6), &mut view));
    view.refresh(NOW);
    let m = view.merged.as_ref().unwrap();
    assert_eq!(m.source, Source::Feed);
    assert_eq!(m.reading.session.as_ref().unwrap().percent, 30.0);
    assert!(!m.stale);

    // Unchanged mtime: not read again.
    assert!(!src.poll(t0 + Duration::from_secs(12), &mut view));
    std::thread::sleep(Duration::from_millis(20));
    feed_at(&dir, NOW / 1000 + 5, 55.0);
    assert!(src.poll(t0 + Duration::from_secs(18), &mut view));
    view.refresh(NOW + 5_000);
    assert_eq!(
        view.merged
            .as_ref()
            .unwrap()
            .reading
            .session
            .as_ref()
            .unwrap()
            .percent,
        55.0
    );

    // An old file reads as stale.
    view.refresh(NOW + 3_600_000);
    assert!(view.merged.as_ref().unwrap().stale);
    src.shutdown();
}

#[test]
fn a_broken_feed_file_is_no_reading() {
    let t = TempDir::new("badfeed");
    let dir = t.join("claude-usage");
    write(&dir.join(feed::FILE_NAME), "{ nonsense");
    let mut view = ClaudeView::default();
    let mut src = ClaudeSource::new(Some(dir), None);
    src.poll(Instant::now(), &mut view);
    view.refresh(NOW);
    assert!(view.merged.is_none());
}

#[test]
fn the_server_thread_fetches_in_the_background() {
    let body = server_fixture("hot");
    let l = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", l.local_addr().unwrap());
    let server = std::thread::spawn(move || {
        let (mut s, _) = l.accept().unwrap();
        let mut req = [0u8; 2048];
        let _ = s.read(&mut req).unwrap();
        let resp = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        s.write_all(resp.as_bytes()).unwrap();
    });
    let mut view = ClaudeView::default();
    let mut src = ClaudeSource::new(None, Some(url));
    let deadline = Instant::now() + Duration::from_secs(5);
    while view.merged.is_none() && Instant::now() < deadline {
        src.poll(Instant::now(), &mut view);
        view.refresh(NOW);
        std::thread::sleep(Duration::from_millis(10));
    }
    let m = view.merged.as_ref().expect("the usage server answered");
    assert_eq!(m.source, Source::Server);
    assert_eq!(m.reading.session.as_ref().unwrap().percent, 98.0);
    server.join().unwrap();
    src.shutdown();
}

#[test]
fn an_unreachable_server_leaves_the_error_for_the_popup() {
    let l = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", l.local_addr().unwrap());
    drop(l);
    let mut view = ClaudeView::default();
    let mut src = ClaudeSource::new(None, Some(url));
    let deadline = Instant::now() + Duration::from_secs(5);
    while view.server_error.is_none() && Instant::now() < deadline {
        src.poll(Instant::now(), &mut view);
        std::thread::sleep(Duration::from_millis(10));
    }
    assert!(view.server_error.is_some());
    view.refresh(NOW);
    assert!(view.merged.is_none());
    src.shutdown();
}

/// Renders the Claude section with only its bars (rows 1 and on, inside the frame).
fn bars_buffer(depth: Depth, fixture: &str, w: u16) -> Buffer {
    let mut r = rig(depth, |c| {
        c.section.claude.c.graph = false;
        c.section.claude.c.summary = false;
    });
    set_server(&mut r.app, fixture);
    let area = Rect::new(0, 0, w, 6);
    let mut buf = Buffer::empty(area);
    vitals::ui::render_one(&mut r.app, PanelId::Claude, area, &mut buf);
    buf
}

fn row_text(buf: &Buffer, y: u16) -> String {
    (0..buf.area.width)
        .map(|x| buf[(x, y)].symbol().to_owned())
        .collect()
}

/// The first full-block cell of a row.
fn fill_fg(buf: &Buffer, y: u16) -> Option<Color> {
    (1..buf.area.width - 1)
        .map(|x| &buf[(x, y)])
        .find(|c| c.symbol() == "█")
        .map(|c| c.fg)
}

#[test]
fn the_fill_is_claude_orange_and_danger_red_when_over_pace_or_hot() {
    let fresh = bars_buffer(Depth::True, "fresh", 100);
    // 5 Hour is 11% used against 88% expected: orange. Weekly 41% / 55%: orange.
    assert!(row_text(&fresh, 1).contains("5 Hour"));
    assert_eq!(fill_fg(&fresh, 1), Some(Color::Rgb(0xD9, 0x77, 0x57)));
    assert!(row_text(&fresh, 3).contains("Weekly"));
    assert_eq!(fill_fg(&fresh, 3), Some(Color::Rgb(0xD9, 0x77, 0x57)));
    // The hot fixture has 98% used somewhere: red fill.
    let hot = bars_buffer(Depth::True, "hot", 100);
    let red = (1..5)
        .filter(|&y| fill_fg(&hot, y) == Some(Color::Rgb(0xef, 0x44, 0x44)))
        .count();
    assert!(
        red >= 1,
        "{}",
        (1..5)
            .map(|y| row_text(&hot, y))
            .collect::<Vec<_>>()
            .join("\n")
    );
}

#[test]
fn colors_fall_back_by_depth() {
    let c256 = bars_buffer(Depth::Ansi256, "fresh", 100);
    assert_eq!(fill_fg(&c256, 1), Some(Color::Indexed(173)));
    let c16 = bars_buffer(Depth::Ansi16, "fresh", 100);
    assert_eq!(fill_fg(&c16, 1), Some(Color::Yellow));
    let mono = bars_buffer(Depth::Mono, "hot", 100);
    // No colors anywhere; the track is a shade.
    assert!((1..5).all(|y| (1..99).all(|x| mono[(x, y)].fg == Color::Reset)));
    assert!((1..5).any(|y| row_text(&mono, y).contains('░')));
}

#[test]
fn rows_show_used_over_expected_and_the_resets_and_one_pace_marker_each() {
    let buf = bars_buffer(Depth::True, "fresh", 100);
    let five = row_text(&buf, 1);
    assert!(
        five.contains("11% / 88%") && five.contains("12:37 PM (37m)"),
        "{five}"
    );
    let week = row_text(&buf, 3);
    assert!(
        week.contains("41% / 55%") && week.contains("Sat 4:20 PM (3d 4h)"),
        "{week}"
    );
    // A pace marker inside each bar (not the frame's edges), in the theme's foreground.
    for y in 1..5 {
        let inner: String = row_text(&buf, y).chars().skip(1).take(98).collect();
        assert_eq!(inner.matches('│').count(), 1, "row {y}: {inner}");
    }
    // No split bar, day ticks or dark track.
    let all: String = (1..5).map(|y| row_text(&buf, y)).collect();
    assert!(!all.contains('▏'), "no day ticks: {all}");
}
