// SPDX-License-Identifier: MIT
//! Spec §8: zero allocations per tick in steady state, checked with a counting allocator.

mod common;

use std::alloc::{GlobalAlloc, Layout, System as SystemAlloc};
use std::cell::Cell;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{Duration, Instant};

use common::{TempDir, copy_tree, fixture};
use vitals::history::Ring;
use vitals::sample::{Root, System};

struct Counting;

static ALLOCS: AtomicUsize = AtomicUsize::new(0);

thread_local! {
    /// Only the thread that armed the counter is counted (the test harness has other threads).
    static ARMED: Cell<bool> = const { Cell::new(false) };
}

fn count() {
    if ARMED.try_with(Cell::get).unwrap_or(false) {
        ALLOCS.fetch_add(1, Ordering::Relaxed);
    }
}

// SAFETY: forwards every call to the system allocator unchanged.
unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, l: Layout) -> *mut u8 {
        count();
        unsafe { SystemAlloc.alloc(l) }
    }
    unsafe fn dealloc(&self, p: *mut u8, l: Layout) {
        unsafe { SystemAlloc.dealloc(p, l) }
    }
    unsafe fn realloc(&self, p: *mut u8, l: Layout, n: usize) -> *mut u8 {
        count();
        unsafe { SystemAlloc.realloc(p, l, n) }
    }
    unsafe fn alloc_zeroed(&self, l: Layout) -> *mut u8 {
        count();
        unsafe { SystemAlloc.alloc_zeroed(l) }
    }
}

#[global_allocator]
static A: Counting = Counting;

fn counted<R>(f: impl FnOnce() -> R) -> (R, usize) {
    let before = ALLOCS.load(Ordering::Relaxed);
    ARMED.with(|a| a.set(true));
    let r = f();
    ARMED.with(|a| a.set(false));
    (r, ALLOCS.load(Ordering::Relaxed) - before)
}

#[test]
fn a_steady_state_tick_allocates_nothing() {
    // The counter itself works: an allocation inside `counted` is seen.
    let (_, n) = counted(|| std::hint::black_box(Vec::<u8>::with_capacity(64)));
    assert!(n >= 1);
    let t = TempDir::new("alloc");
    copy_tree(&fixture("sys"), &t.join("sys"));
    let a = fixture("proc/a");
    let b = fixture("proc/b");
    copy_tree(&a, &t.join("proc"));
    let mut sys = System::new(&Root::new(t.join("proc"), t.join("sys"))).unwrap();
    let mut ring = Ring::new(7200);
    let mut cols = Vec::new();
    let t0 = Instant::now();
    // Warm-up ticks may grow buffers, discover devices and read the mounts.
    for i in 0..3 {
        copy_tree(if i % 2 == 0 { &b } else { &a }, &t.join("proc"));
        sys.sample(t0 + Duration::from_secs(i)).unwrap();
        ring.push(sys.cpu.mean());
        ring.columns(120, 100, &mut cols);
    }
    let mut total = 0;
    for i in 3..30 {
        copy_tree(if i % 2 == 0 { &b } else { &a }, &t.join("proc"));
        let ((), n) = counted(|| {
            sys.sample(t0 + Duration::from_secs(i)).unwrap();
            ring.push(sys.cpu.mean());
            ring.columns(120, 100, &mut cols);
        });
        total += n;
    }
    assert_eq!(
        total, 0,
        "sampling allocated {total} times over 27 steady-state ticks"
    );
    assert!(sys.cpu.ready());
}

#[test]
fn a_steady_state_tick_and_draw_allocate_nothing() {
    use common::ui::{NOW, rig};
    use ratatui::buffer::Buffer;
    use ratatui::layout::Rect;
    use vitals::theme::Depth;

    let mut r = rig(Depth::True, |_| {});
    let app = &mut r.app;
    let t0 = Instant::now();
    for size in [(200u16, 50u16), (80, 24), (40, 12)] {
        let area = Rect::new(0, 0, size.0, size.1);
        let mut buf = Buffer::empty(area);
        // Warm-up: scratch strings and vectors reach their final capacity, layouts are cached.
        for i in 1..4 {
            app.tick(t0 + Duration::from_secs(i), NOW + i as i64 * 1000);
            vitals::ui::render(app, area, &mut buf);
        }
        let ((), n) = counted(|| {
            for i in 4..24 {
                app.tick(t0 + Duration::from_secs(i), NOW + i as i64 * 1000);
                vitals::ui::render(app, area, &mut buf);
            }
        });
        assert_eq!(
            n, 0,
            "tick + draw at {}x{} allocated {n} times in 20 frames",
            size.0, size.1
        );
    }
}

#[test]
fn terminal_draw_into_a_test_backend_allocates_nothing_while_the_data_changes() {
    use common::ui::{NOW, rig};
    use ratatui::Terminal;
    use ratatui::backend::TestBackend;
    use vitals::theme::Depth;

    let mut r = rig(Depth::True, |_| {});
    let app = &mut r.app;
    let t0 = Instant::now();
    let mut term = Terminal::new(TestBackend::new(200, 50)).unwrap();
    let frame = |app: &mut vitals::app::App, term: &mut Terminal<TestBackend>, i: u64| {
        app.tick(t0 + Duration::from_secs(i), NOW + i as i64 * 1000);
        app.data.rings.cpu.push((i * 37 % 100) as f32);
        app.data.rings.net_down.push((i * 53 % 90) as f32 * 1e3);
        term.draw(|f| {
            let a = f.area();
            vitals::ui::render(app, a, f.buffer_mut());
        })
        .unwrap();
    };
    for i in 1..4 {
        frame(app, &mut term, i);
    }
    let snapshot = |t: &Terminal<TestBackend>| vitals::frame::to_plain(t.backend().buffer());
    let before = snapshot(&term);
    let ((), n) = counted(|| {
        for i in 4..14 {
            frame(app, &mut term, i);
        }
    });
    assert_ne!(before, snapshot(&term), "the frames must really change");
    // Spec §8: ratatui 0.30 flushes through `Buffer::diff_iter`, which does not allocate, so a whole
    // `Terminal::draw` is allocation-free in steady state, not only our widgets.
    assert_eq!(n, 0, "{n} allocations over 10 changing frames");
}

/// Spec §8: the settings and help popups draw without allocating too. (The Claude details popup does
/// allocate: `readout::lines` builds its strings. It is a popup, not part of the steady state.)
#[test]
fn the_settings_and_help_popups_allocate_nothing() {
    use common::ui::{NOW, rig};
    use ratatui::buffer::Buffer;
    use ratatui::layout::Rect;
    use vitals::input::Action;
    use vitals::theme::Depth;

    for open in [Action::Settings, Action::Help] {
        let mut r = rig(Depth::True, |_| {});
        let app = &mut r.app;
        app.apply(open);
        let t0 = Instant::now();
        let area = Rect::new(0, 0, 100, 30);
        let mut buf = Buffer::empty(area);
        for i in 1..4 {
            app.tick(t0 + Duration::from_secs(i), NOW + i as i64 * 1000);
            vitals::ui::render(app, area, &mut buf);
        }
        let ((), n) = counted(|| {
            for i in 4..14 {
                app.tick(t0 + Duration::from_secs(i), NOW + i as i64 * 1000);
                vitals::ui::render(app, area, &mut buf);
            }
        });
        assert_eq!(n, 0, "{open:?} allocated {n} times in 10 frames");
    }
}
