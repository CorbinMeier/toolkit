// SPDX-License-Identifier: MIT
//! Synthetic mouse and key events against a rendered screen: the hit map after zoom, hide and resize.

mod common;

use common::ui::{rig, screen_text};
use ratatui::crossterm::event::{
    KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};
use vitals::app::{App, Overlay};
use vitals::config::{GraphStyle, PanelId};
use vitals::input::{Action, Target};
use vitals::theme::Depth;

fn mouse(kind: MouseEventKind, x: u16, y: u16) -> MouseEvent {
    MouseEvent {
        kind,
        column: x,
        row: y,
        modifiers: KeyModifiers::NONE,
    }
}

fn left(app: &mut App, x: u16, y: u16) {
    app.handle_mouse(mouse(MouseEventKind::Down(MouseButton::Left), x, y));
}

fn press(app: &mut App, c: char) {
    app.handle_key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE));
}

/// Column and row of the first occurrence of `needle` in the rendered text.
fn find(text: &str, needle: &str) -> (u16, u16) {
    for (y, line) in text.lines().enumerate() {
        if let Some(b) = line.find(needle) {
            return (line[..b].chars().count() as u16, y as u16);
        }
    }
    panic!("{needle:?} not on screen:\n{text}");
}

fn right(app: &mut App, x: u16, y: u16) {
    app.handle_mouse(mouse(MouseEventKind::Down(MouseButton::Right), x, y));
}

fn esc(app: &mut App) {
    app.handle_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
}

fn key(app: &mut App, code: KeyCode) {
    app.handle_key(KeyEvent::new(code, KeyModifiers::NONE));
}

#[test]
fn header_chips_hide_and_show_sections() {
    let mut r = rig(Depth::True, |_| {});
    let app = &mut r.app;
    let text = screen_text(app, 120, 40);
    let (x, y) = find(&text, "[3 disk]");
    left(app, x + 2, y);
    assert!(!app.cfg.enabled(PanelId::Disk));
    assert!(
        app.cfg_changed,
        "a UI change is flagged for an immediate save"
    );
    let text = screen_text(app, 120, 40);
    assert!(!text.contains("┌ Disk"), "{text}");
    // The chip is still there, muted; clicking brings the section back.
    let (x, y) = find(&text, "[3 disk]");
    left(app, x + 1, y);
    assert!(app.cfg.enabled(PanelId::Disk));
    assert!(screen_text(app, 120, 40).contains("┌ Disk"));
}

#[test]
fn clicking_a_title_zooms_and_right_click_cycles_the_style() {
    let mut r = rig(Depth::True, |_| {});
    let app = &mut r.app;
    let text = screen_text(app, 120, 40);
    let (x, y) = find(&text, "┌ CPU");
    right(app, x + 3, y);
    assert_eq!(app.cfg.section.cpu.c.style, GraphStyle::Block);
    left(app, x + 3, y);
    assert_eq!(app.ui.zoom, Some(PanelId::Cpu));
    assert_eq!(app.ui.focus, PanelId::Cpu);
    let zoomed = screen_text(app, 120, 40);
    assert!(!zoomed.contains("┌ Memory"), "{zoomed}");
    // The title is at the top now; clicking it restores.
    let (zx, zy) = find(&zoomed, "┌ CPU");
    assert_eq!((zx, zy), (0, 1));
    left(app, zx + 3, zy);
    assert_eq!(app.ui.zoom, None);
}

#[test]
fn scrolling_a_timeline_changes_its_window() {
    let mut r = rig(Depth::True, |_| {});
    let app = &mut r.app;
    let text = screen_text(app, 120, 40);
    let (x, y) = find(&text, "┌ Disk");
    app.handle_mouse(mouse(MouseEventKind::ScrollUp, x + 5, y + 1));
    assert_eq!(app.cfg.section.disk.window_min, 5);
    app.handle_mouse(mouse(MouseEventKind::ScrollDown, x + 5, y + 1));
    app.handle_mouse(mouse(MouseEventKind::ScrollDown, x + 5, y + 1));
    assert_eq!(app.cfg.section.disk.window_min, 1);
    // Scrolling the bars does nothing.
    app.handle_mouse(mouse(MouseEventKind::ScrollUp, x + 5, y + 4));
    assert_eq!(app.cfg.section.disk.window_min, 1);
    // Tokens has its own windows.
    let (tx, ty) = find(&screen_text(app, 120, 40), "┌ Tokens");
    app.handle_mouse(mouse(MouseEventKind::ScrollUp, tx + 5, ty + 1));
    assert_eq!(app.cfg.section.tokens.window, "7d");
    // Scrolling the interval chip.
    let (ix, iy) = find(&screen_text(app, 120, 40), " 1s ");
    app.handle_mouse(mouse(MouseEventKind::ScrollUp, ix + 1, iy));
    assert_eq!(app.cfg.interval_ms, 2000);
    left(app, ix + 1, iy);
    assert_eq!(app.cfg.interval_ms, 5000);
}

#[test]
fn clicking_a_body_focuses_it() {
    let mut r = rig(Depth::True, |_| {});
    let app = &mut r.app;
    let text = screen_text(app, 120, 40);
    let (x, y) = find(&text, "┌ Network");
    left(app, x + 5, y + 2);
    assert_eq!(app.ui.focus, PanelId::Net);
    assert_eq!(app.ui.zoom, None);
}

#[test]
fn the_hit_map_follows_zoom_hide_and_resize() {
    let mut r = rig(Depth::True, |_| {});
    let app = &mut r.app;
    let wide = screen_text(app, 120, 40);
    let (wx, _) = find(&wide, "┌ Memory");
    assert!(wx > 0);
    // Narrow: one column, Memory under CPU at x = 0; its title still zooms it.
    let narrow = screen_text(app, 80, 60);
    let (nx, ny) = find(&narrow, "┌ Memory");
    assert_eq!(nx, 0);
    left(app, nx + 3, ny);
    assert_eq!(app.ui.zoom, Some(PanelId::Mem));
    esc(app);
    assert_eq!(app.ui.zoom, None);
    // A click where Memory used to be hits whatever is there now, not a stale target.
    screen_text(app, 80, 60);
    let before = app.cfg.clone();
    left(app, wx + 3, 1);
    assert_eq!(app.cfg.section.memory, before.section.memory);
    // Hiding a section moves the others up; their targets follow.
    press(app, '1');
    let text = screen_text(app, 120, 40);
    assert!(!text.contains("┌ CPU"));
    let (mx, my) = find(&text, "┌ Memory");
    assert_eq!((mx, my), (0, 1));
    left(app, mx + 3, my);
    assert_eq!(app.ui.zoom, Some(PanelId::Mem));
}

#[test]
fn the_token_series_targets() {
    let mut r = rig(Depth::True, |_| {});
    let app = &mut r.app;
    // The title chip, the timeline and the key.
    let (x, y) = find(&screen_text(app, 120, 40), "[io]");
    left(app, x + 1, y);
    assert_eq!(app.cfg.section.tokens.series.name(), "total");
    left(app, x + 1, y + 1);
    assert_eq!(app.cfg.section.tokens.series.name(), "cache");
    press(app, 'v');
    assert_eq!(app.cfg.section.tokens.series.name(), "io");
}

#[test]
fn the_claude_bars_open_the_details_popup_and_any_click_closes_it() {
    let mut r = rig(Depth::True, |_| {});
    let app = &mut r.app;
    let text = screen_text(app, 120, 40);
    let (_, y) = find(&text, "5 Hour");
    left(app, 5, y);
    assert_eq!(app.ui.overlay, Overlay::Claude);
    let popup = screen_text(app, 120, 40);
    assert!(
        popup.contains("Claude usage")
            && popup.contains("5 Hour : 11%")
            && popup.contains("source: server"),
        "{popup}"
    );
    left(app, 1, 10);
    assert_eq!(app.ui.overlay, Overlay::None);
    press(app, 'c');
    assert_eq!(app.ui.overlay, Overlay::Claude);
    esc(app);
    assert_eq!(app.ui.overlay, Overlay::None);
}

#[test]
fn the_header_buttons_open_settings_and_help_and_are_never_dropped() {
    for width in [40u16, 56, 80, 120] {
        let mut r = rig(Depth::True, |_| {});
        let app = &mut r.app;
        let text = screen_text(app, width, 24);
        let head = text.lines().next().unwrap();
        assert!(head.ends_with("[⚙] [?]"), "{width}: {head:?}");
        let (gx, gy) = find(&text, "[⚙]");
        left(app, gx + 1, gy);
        assert_eq!(app.ui.overlay, Overlay::Settings, "{width}");
        let (hx, hy) = find(&screen_text(app, width, 24), "[?]");
        left(app, hx + 1, hy);
        assert_eq!(app.ui.overlay, Overlay::Help, "{width}");
        left(app, gx + 1, gy);
        assert_eq!(app.ui.overlay, Overlay::Settings);
        left(app, gx + 1, gy);
        assert_eq!(app.ui.overlay, Overlay::None);
    }
}

/// Clicks every control of the settings popup and checks what it did.
#[test]
fn every_settings_control_is_reachable_and_does_its_job() {
    for (w, h) in [(100u16, 30u16), (60, 40)] {
        let mut r = rig(Depth::True, |_| {});
        let app = &mut r.app;
        press(app, ',');
        assert_eq!(app.ui.overlay, Overlay::Settings);
        screen_text(app, w, h);
        let controls: Vec<_> = app
            .hits
            .items()
            .iter()
            .filter_map(|&(rect, t)| match t {
                Target::Do(a) => Some((rect, a)),
                _ => None,
            })
            .collect();
        // 6 sections x 8 controls, the All row's 4, the last row's 5, the 2 global header toggles and the
        // Network block: `per interface` and one checkbox per listed interface.
        let net = 1 + app.data.sys.net.interfaces().count();
        assert_eq!(controls.len(), 6 * 8 + 4 + 5 + 2 + net, "{w}x{h}");
        for (rect, action) in controls {
            // Every control's cell maps to its own action.
            assert_eq!(app.hits.at(rect.x, rect.y), Some(Target::Do(action)));
            let before = app.cfg.clone();
            left(app, rect.x, rect.y);
            let changed = app.cfg != before;
            match action {
                Action::GraphRows(PanelId::Cpu, -1) => {
                    // 2 rows -> 1
                    assert_eq!(app.cfg.section.cpu.c.graph_rows, 1);
                }
                Action::MoveSection(PanelId::Cpu, -1) | Action::MoveSection(PanelId::Claude, 1) => {
                    assert!(!changed, "the ends cannot move further");
                }
                _ => assert!(changed, "{action:?} did nothing at {w}x{h}"),
            }
            assert_eq!(
                app.ui.overlay,
                Overlay::Settings,
                "{action:?} closed the popup"
            );
            // Put things back for the next control and redraw (positions can shift).
            app.cfg = before;
            screen_text(app, w, h);
        }
    }
}

#[test]
fn settings_toggle_parts_rows_and_order_live() {
    let mut r = rig(Depth::True, |_| {});
    let app = &mut r.app;
    press(app, ',');
    let text = screen_text(app, 120, 40);
    let (x, y) = find(&text, "[x] braille");
    // The first line with a braille toggle is CPU's.
    left(app, x + 1, y);
    assert!(!app.cfg.section.cpu.c.graph);
    // Rows + on Memory (second line): 2 -> 3 -> ... -> 8, stopping at 8.
    for _ in 0..8 {
        let hits: Vec<_> = app
            .hits
            .items()
            .iter()
            .filter(|(_, t)| *t == Target::Do(Action::GraphRows(PanelId::Mem, 1)))
            .cloned()
            .collect();
        left(app, hits[0].0.x, hits[0].0.y);
    }
    assert_eq!(app.cfg.section.memory.c.graph_rows, 8);
    // Move Disk up: it swaps with Memory in the reading order, the rows keep their shapes.
    screen_text(app, 120, 40);
    let down: Vec<_> = app
        .hits
        .items()
        .iter()
        .filter(|(_, t)| *t == Target::Do(Action::MoveSection(PanelId::Disk, -1)))
        .cloned()
        .collect();
    left(app, down[0].0.x, down[0].0.y);
    assert_eq!(app.cfg.row[0].sections, ["cpu", "disk"]);
    assert_eq!(app.cfg.row[1].sections, ["memory", "network"]);
    let shown = screen_text(app, 120, 40);
    assert!(shown.contains("Settings"));
    // A click outside closes it; a click inside on no control does not.
    let (px, py) = find(&shown, "arrows move");
    left(app, px, py);
    assert_eq!(app.ui.overlay, Overlay::Settings);
    left(app, 2, 20);
    assert_eq!(app.ui.overlay, Overlay::None);
}

#[test]
fn the_settings_keyboard_cursor_presses_controls() {
    let mut r = rig(Depth::True, |_| {});
    let app = &mut r.app;
    press(app, ',');
    key(app, KeyCode::Right); // the first arrow shows the cursor on CPU's checkbox
    assert_eq!(app.ui.settings_cursor, Some((0, 0)));
    key(app, KeyCode::Right);
    press(app, ' '); // CPU timeline off
    assert!(!app.cfg.section.cpu.c.graph);
    key(app, KeyCode::Down);
    key(app, KeyCode::Left);
    key(app, KeyCode::Enter); // Memory checkbox off
    assert!(!app.cfg.enabled(PanelId::Mem));
    for _ in 0..6 {
        key(app, KeyCode::Down);
    }
    assert_eq!(
        app.ui.settings_cursor,
        Some((7, 0)),
        "the last row has fewer columns"
    );
    key(app, KeyCode::Right);
    key(app, KeyCode::Right);
    key(app, KeyCode::Enter);
    assert_eq!(app.cfg.theme, "ramp");
    for _ in 0..9 {
        key(app, KeyCode::Right);
    }
    assert_eq!(app.ui.settings_cursor, Some((7, 4)));
    key(app, KeyCode::Enter);
    assert_eq!(app.cfg.section.cpu.c.style, GraphStyle::Block);
    assert_eq!(app.cfg.section.claude.c.style, GraphStyle::Block);
    esc(app);
    assert_eq!(app.ui.overlay, Overlay::None);
}

#[test]
fn the_help_popup_tabs_scroll_and_close() {
    let mut r = rig(Depth::True, |_| {});
    let app = &mut r.app;
    press(app, '3'); // disk off then on, focus on disk
    press(app, '3');
    press(app, '?');
    assert_eq!(app.ui.overlay, Overlay::Help);
    assert_eq!(
        app.ui.help_tab,
        PanelId::Disk.index(),
        "opens on the focused section"
    );
    let text = screen_text(app, 100, 24);
    assert!(
        text.contains("io_ticks") && text.contains("Controls"),
        "{text}"
    );
    // Click the Network tab.
    let (x, y) = find(&text, "Network");
    left(app, x + 1, y);
    assert_eq!(app.ui.help_tab, PanelId::Net.index());
    key(app, KeyCode::Right);
    assert_eq!(app.ui.help_tab, PanelId::Tokens.index());
    app.apply(Action::HelpTab(0));
    key(app, KeyCode::Left);
    assert_eq!(app.ui.help_tab, 6, "tabs wrap around to Controls");
    key(app, KeyCode::Right);
    assert_eq!(app.ui.help_tab, 0);
    app.apply(Action::HelpTab(PanelId::Net.index()));
    // Scrolling by key and wheel, clamped to the text.
    screen_text(app, 100, 12);
    key(app, KeyCode::Down);
    key(app, KeyCode::PageDown);
    assert_eq!(app.ui.help_scroll, 11);
    app.handle_mouse(mouse(MouseEventKind::ScrollUp, 40, 8));
    assert_eq!(app.ui.help_scroll, 8);
    key(app, KeyCode::End);
    assert_eq!(app.ui.help_scroll, app.ui.help_max.get());
    key(app, KeyCode::Home);
    assert_eq!(app.ui.help_scroll, 0);
    left(app, 0, 5);
    assert_eq!(app.ui.overlay, Overlay::None);
}

#[test]
fn keys_do_what_their_mouse_twins_do() {
    let mut r = rig(Depth::True, |_| {});
    let app = &mut r.app;
    app.handle_key(KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE));
    assert_eq!(app.ui.focus, PanelId::Mem);
    press(app, 'g');
    assert!(!app.cfg.section.memory.c.graph);
    press(app, 'b');
    assert!(!app.cfg.section.memory.c.bars);
    press(app, 's');
    assert!(!app.cfg.section.memory.c.summary);
    press(app, 'g');
    press(app, '}');
    press(app, '}');
    assert_eq!(app.cfg.section.memory.c.graph_rows, 4);
    for _ in 0..9 {
        press(app, '{');
    }
    assert_eq!(app.cfg.section.memory.c.graph_rows, 1);
    press(app, 'S');
    assert_eq!(app.cfg.section.memory.c.style, GraphStyle::Block);
    press(app, 'm');
    assert_eq!(
        app.cfg.section.memory.c.style,
        GraphStyle::Block,
        "the old mode key is gone"
    );
    press(app, '+');
    assert_eq!(app.cfg.section.memory.window_min, 5);
    press(app, '-');
    press(app, '-');
    assert_eq!(app.cfg.section.memory.window_min, 1);
    press(app, ']');
    assert_eq!(app.cfg.interval_ms, 2000);
    press(app, '[');
    press(app, '[');
    assert_eq!(app.cfg.interval_ms, 500);
    press(app, 't');
    assert_eq!(app.cfg.theme, "ramp");
    press(app, 't');
    press(app, 't');
    assert_eq!(app.cfg.theme, "default");
    press(app, 'p');
    assert!(app.ui.paused);
    press(app, '2');
    assert!(!app.cfg.enabled(PanelId::Mem));
    assert_eq!(
        app.ui.focus,
        PanelId::Disk,
        "focus moves off a hidden section"
    );
    press(app, '@');
    assert_eq!(app.ui.zoom, None, "a hidden section cannot be zoomed");
    press(app, '#');
    assert_eq!(app.ui.zoom, Some(PanelId::Disk));
    esc(app);
    assert_eq!(app.ui.zoom, None);
    press(app, '?');
    assert_eq!(app.ui.overlay, Overlay::Help);
    press(app, '?');
    assert_eq!(app.ui.overlay, Overlay::None);
    press(app, ',');
    assert_eq!(app.ui.overlay, Overlay::Settings);
    press(app, ',');
    assert_eq!(app.ui.overlay, Overlay::None);
    press(app, 'q');
    assert!(app.quit);
}

#[test]
fn m_capital_turns_mouse_capture_off_and_on() {
    let mut r = rig(Depth::True, |_| {});
    let app = &mut r.app;
    let text = screen_text(app, 120, 40);
    let (x, y) = find(&text, "┌ CPU");
    press(app, 'M');
    assert!(!app.cfg.mouse && std::mem::take(&mut app.mouse_changed));
    left(app, x + 3, y);
    assert_eq!(
        app.ui.zoom, None,
        "mouse events are ignored while capture is off"
    );
    assert!(screen_text(app, 120, 40).contains("mouse off"));
    press(app, 'M');
    assert!(app.cfg.mouse && app.mouse_changed);
}

#[test]
fn ctrl_c_quits() {
    let mut r = rig(Depth::True, |_| {});
    r.app
        .handle_key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL));
    assert!(r.app.quit);
}

#[test]
fn the_global_bars_and_graphs_toggles_work_by_mouse_and_key() {
    let mut r = rig(Depth::True, |_| {});
    let app = &mut r.app;
    let text = screen_text(app, 120, 40);
    let (x, y) = find(&text, "[▤ bars]");
    left(app, x + 2, y);
    assert!(
        PanelId::ALL
            .iter()
            .all(|&p| !app.cfg.part(p, vitals::config::Part::Bars))
    );
    assert!(app.cfg_changed);
    let text = screen_text(app, 120, 40);
    assert!(!text.contains("all ░"), "no bars drawn:\n{text}");
    left(app, x + 2, y);
    assert!(
        PanelId::ALL
            .iter()
            .all(|&p| app.cfg.part(p, vitals::config::Part::Bars))
    );
    // Claude's timeline is off by default, so graphs are mixed: one click turns them on everywhere.
    let (gx, gy) = find(&screen_text(app, 120, 40), "[⣿ graphs]");
    left(app, gx + 2, gy);
    assert!(app.cfg.section.claude.c.graph);
    press(app, 'G');
    assert!(
        PanelId::ALL
            .iter()
            .all(|&p| !app.cfg.part(p, vitals::config::Part::Graph))
    );
    press(app, 'B');
    assert!(
        PanelId::ALL
            .iter()
            .all(|&p| !app.cfg.part(p, vitals::config::Part::Bars))
    );
}

#[test]
fn network_rows_have_checkboxes_that_toggle_inclusion() {
    let mut r = rig(Depth::True, |_| {});
    let app = &mut r.app;
    // Zoomed, so the whole grid is on screen.
    app.apply(Action::Zoom(PanelId::Net));
    let text = screen_text(app, 120, 40);
    assert!(text.contains("all "), "{text}");
    let (x, y) = find(&text, "[x]wlo1");
    assert!(
        text.contains("[ ]docker0") && text.contains("[x]enx3c8cf8…"),
        "{text}"
    );
    let before = app.data.sys.net.total();
    // A click on the checkbox excludes the physical interface: an `exclude` override, saved at once.
    left(app, x + 1, y);
    assert_eq!(app.cfg.section.network.exclude, ["wlo1"]);
    assert!(app.cfg_changed, "the config is saved at once");
    assert!(screen_text(app, 120, 40).contains("[ ]wlo1"));
    assert!(app.data.sys.net.total().down_bps <= before.down_bps);
    // And back: the override disappears, the default rule applies again.
    let (x, y) = find(&screen_text(app, 120, 40), "[ ]wlo1");
    left(app, x + 1, y);
    assert!(app.cfg.section.network.exclude.is_empty());
    // A non-physical interface is added to `include`.
    let (x, y) = find(&screen_text(app, 120, 40), "[ ]docker0");
    left(app, x + 1, y);
    assert_eq!(app.cfg.section.network.include, ["docker0"]);
    // `i` does nothing any more.
    press(app, 'i');
    assert_eq!(app.cfg.section.network.include, ["docker0"]);
}

#[test]
fn network_per_interface_toggle_from_the_settings_popup() {
    let mut r = rig(Depth::True, |_| {});
    let app = &mut r.app;
    press(app, ',');
    let text = screen_text(app, 120, 50);
    assert!(
        text.contains("per interface") && text.contains("counted in all:"),
        "{text}"
    );
    assert!(text.contains("[ ] veth5382c7e"), "{text}");
    // Mouse: the checkbox of one interface in the popup.
    let (x, y) = find(&text, "[ ] docker0");
    left(app, x + 1, y);
    assert_eq!(app.cfg.section.network.include, ["docker0"]);
    let text = screen_text(app, 120, 50);
    assert!(text.contains("[x] docker0"), "{text}");
    // Keyboard twin: arrows to the `per interface` control, Space presses it.
    for _ in 0..9 {
        key(app, KeyCode::Down);
    }
    assert!(app.cfg.section.network.per_interface);
    key(app, KeyCode::Char(' '));
    assert!(!app.cfg.section.network.per_interface);
    assert!(app.cfg_changed);
    // One more right and Space flips the first listed interface (lo).
    key(app, KeyCode::Right);
    key(app, KeyCode::Char(' '));
    assert!(app.cfg.section.network.include.iter().any(|n| n == "lo"));
}
