// SPDX-License-Identifier: MIT
//! Mouse and key events to [`Action`]s (spec §6). Every mouse action has a key twin. The mouse side
//! goes through a hit map of rectangles that the layout pass rebuilds on every draw, so it is always
//! right after zoom, hiding a section or a resize.

use ratatui::crossterm::event::{
    KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};
use ratatui::layout::Rect;

use crate::app::Overlay;
use crate::config::{PanelId, Part};

/// What a screen cell belongs to.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Target {
    /// A header chip: show or hide the section.
    Chip(PanelId),
    Interval,
    Theme,
    /// The `[⚙]` button.
    Settings,
    /// The `[?]` button.
    Help,
    /// A section's title line.
    Title(PanelId),
    /// The token series name inside its title.
    Series,
    /// A section's timeline.
    Timeline(PanelId),
    /// A section's bars.
    Bars(PanelId),
    /// The rest of a section.
    Body(PanelId),
    /// Everything under the header while a popup is open: a click here closes the popup.
    Outside,
    /// A popup's own surface: clicks do nothing.
    Popup,
    /// The scrolling text of the help popup.
    HelpText,
    /// A control in a popup (a checkbox, a tab, `−`, `▲`): it does this.
    Do(Action),
}

/// Rectangles to targets, rebuilt every layout pass. A later rectangle wins where they overlap.
#[derive(Default)]
pub struct HitMap {
    items: Vec<(Rect, Target)>,
}

impl HitMap {
    pub fn clear(&mut self) {
        self.items.clear();
    }

    pub fn push(&mut self, r: Rect, t: Target) {
        if r.width > 0 && r.height > 0 {
            self.items.push((r, t));
        }
    }

    pub fn at(&self, x: u16, y: u16) -> Option<Target> {
        self.items
            .iter()
            .rev()
            .find(|(r, _)| x >= r.x && x < r.x + r.width && y >= r.y && y < r.y + r.height)
            .map(|&(_, t)| t)
    }

    pub fn len(&self) -> usize {
        self.items.len()
    }

    pub fn is_empty(&self) -> bool {
        self.items.is_empty()
    }

    /// Every rectangle with its target (for tests that check each control is reachable).
    pub fn items(&self) -> &[(Rect, Target)] {
        &self.items
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    /// Show or hide a section.
    TogglePanel(PanelId),
    TogglePart(PanelId, Part),
    /// Timeline height: fewer (-1) or more (+1) rows.
    GraphRows(PanelId, i8),
    NextStyle(PanelId),
    /// A part of every section on or off at once (`None`: the sections themselves).
    ToggleAll(Option<Part>),
    /// The settings popup's `style ▸`: every section to the next style.
    AllStyles,
    /// Time window longer (+1) or shorter (-1).
    Window(PanelId, i8),
    /// Zoom a section to the whole screen, or restore if it already is.
    Zoom(PanelId),
    Focus(PanelId),
    FocusStep {
        back: bool,
    },
    /// Move a section one place earlier (-1) or later (+1) in the reading order.
    MoveSection(PanelId, i8),
    /// Refresh interval slower (+1) or faster (-1).
    Interval(i8),
    /// Click on the interval chip: the next interval, wrapping.
    CycleInterval,
    NextTheme,
    NextLayout,
    /// Count the n-th listed network interface toward `all`, or stop (checkbox in a row or in settings).
    ToggleIface(usize),
    /// Network: one bar per interface, or `all` only.
    TogglePerIface,
    NextSeries,
    ClaudePopup,
    Settings,
    Help,
    HelpTab(usize),
    HelpTabStep(i8),
    HelpScroll(i16),
    /// Arrow keys inside the settings popup.
    SettingsNav(i8, i8),
    /// Space or Enter inside the settings popup.
    SettingsPress,
    /// A click outside a popup.
    ClosePopup,
    Pause,
    ToggleMouse,
    /// `Esc`: close a popup, else restore the screen from a zoom.
    Escape,
    Quit,
}

pub fn mouse_action(hits: &HitMap, ev: MouseEvent) -> Option<Action> {
    let target = hits.at(ev.column, ev.row)?;
    match ev.kind {
        MouseEventKind::Down(MouseButton::Left) => Some(match target {
            Target::Chip(p) => Action::TogglePanel(p),
            Target::Interval => Action::CycleInterval,
            Target::Theme => Action::NextTheme,
            Target::Settings => Action::Settings,
            Target::Help => Action::Help,
            Target::Title(p) => Action::Zoom(p),
            Target::Series | Target::Timeline(PanelId::Tokens) => Action::NextSeries,
            Target::Bars(PanelId::Claude) => Action::ClaudePopup,
            Target::Timeline(p) | Target::Bars(p) | Target::Body(p) => Action::Focus(p),
            Target::Outside => Action::ClosePopup,
            Target::Popup | Target::HelpText => return None,
            Target::Do(a) => a,
        }),
        MouseEventKind::Down(MouseButton::Right) => match target {
            Target::Title(p) | Target::Timeline(p) => Some(Action::NextStyle(p)),
            Target::Series => Some(Action::NextStyle(PanelId::Tokens)),
            _ => None,
        },
        MouseEventKind::ScrollUp | MouseEventKind::ScrollDown => {
            let up = ev.kind == MouseEventKind::ScrollUp;
            let d = if up { 1 } else { -1 };
            match target {
                Target::Interval => Some(Action::Interval(d)),
                Target::Timeline(p) if p != PanelId::Claude => Some(Action::Window(p, d)),
                Target::HelpText => Some(Action::HelpScroll(if up { -3 } else { 3 })),
                _ => None,
            }
        }
        _ => None,
    }
}

/// The key map. `focus` is the focused section, which `g`, `b`, `s`, `S`, `{`, `}`, `+` and `-` act on.
pub fn key_action(key: KeyEvent, focus: PanelId, overlay: Overlay) -> Option<Action> {
    let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
    // A popup takes the cursor keys.
    match (overlay, key.code) {
        (Overlay::Help, KeyCode::Left) => return Some(Action::HelpTabStep(-1)),
        (Overlay::Help, KeyCode::Right) => return Some(Action::HelpTabStep(1)),
        (Overlay::Help, KeyCode::Up) => return Some(Action::HelpScroll(-1)),
        (Overlay::Help, KeyCode::Down) => return Some(Action::HelpScroll(1)),
        (Overlay::Help, KeyCode::PageUp) => return Some(Action::HelpScroll(-10)),
        (Overlay::Help, KeyCode::PageDown) => return Some(Action::HelpScroll(10)),
        (Overlay::Help, KeyCode::Home) => return Some(Action::HelpScroll(i16::MIN)),
        (Overlay::Help, KeyCode::End) => return Some(Action::HelpScroll(i16::MAX)),
        (Overlay::Settings, KeyCode::Left) => return Some(Action::SettingsNav(-1, 0)),
        (Overlay::Settings, KeyCode::Right) => return Some(Action::SettingsNav(1, 0)),
        (Overlay::Settings, KeyCode::Up) => return Some(Action::SettingsNav(0, -1)),
        (Overlay::Settings, KeyCode::Down) => return Some(Action::SettingsNav(0, 1)),
        (Overlay::Settings, KeyCode::Enter | KeyCode::Char(' ')) => {
            return Some(Action::SettingsPress);
        }
        _ => {}
    }
    Some(match key.code {
        KeyCode::Char('c' | 'C') if ctrl => Action::Quit,
        KeyCode::Char(c) if ctrl && c != ' ' => return None,
        KeyCode::Char('q') => Action::Quit,
        KeyCode::Esc => Action::Escape,
        KeyCode::Tab => Action::FocusStep { back: false },
        KeyCode::BackTab => Action::FocusStep { back: true },
        KeyCode::Char(c @ '1'..='6') if key.modifiers.contains(KeyModifiers::SHIFT) => {
            Action::Zoom(PanelId::from_number(c as u8 - b'0')?)
        }
        KeyCode::Char(c @ '1'..='6') => Action::TogglePanel(PanelId::from_number(c as u8 - b'0')?),
        // Shift + digit on a US layout arrives as the shifted symbol.
        KeyCode::Char(c @ ('!' | '@' | '#' | '$' | '%' | '^')) => {
            let n = "!@#$%^".find(c)? as u8 + 1;
            Action::Zoom(PanelId::from_number(n)?)
        }
        KeyCode::Char('G') => Action::ToggleAll(Some(Part::Graph)),
        KeyCode::Char('B') => Action::ToggleAll(Some(Part::Bars)),
        KeyCode::Char('g') => Action::TogglePart(focus, Part::Graph),
        KeyCode::Char('b') => Action::TogglePart(focus, Part::Bars),
        KeyCode::Char('s') => Action::TogglePart(focus, Part::Summary),
        KeyCode::Char('{') => Action::GraphRows(focus, -1),
        KeyCode::Char('}') => Action::GraphRows(focus, 1),
        KeyCode::Char('S') => Action::NextStyle(focus),
        KeyCode::Char('+' | '=') => Action::Window(focus, 1),
        KeyCode::Char('-' | '_') => Action::Window(focus, -1),
        KeyCode::Char('v') => Action::NextSeries,
        KeyCode::Char('t') => Action::NextTheme,
        KeyCode::Char('c') => Action::ClaudePopup,
        KeyCode::Char('p') => Action::Pause,
        KeyCode::Char('M') => Action::ToggleMouse,
        KeyCode::Char('[') => Action::Interval(-1),
        KeyCode::Char(']') => Action::Interval(1),
        KeyCode::Char(',') => Action::Settings,
        KeyCode::Char('?') => Action::Help,
        _ => return None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::crossterm::event::KeyEventKind;

    fn key(code: KeyCode, mods: KeyModifiers) -> KeyEvent {
        let mut k = KeyEvent::new(code, mods);
        k.kind = KeyEventKind::Press;
        k
    }

    fn click(kind: MouseEventKind, x: u16, y: u16) -> MouseEvent {
        MouseEvent {
            kind,
            column: x,
            row: y,
            modifiers: KeyModifiers::NONE,
        }
    }

    #[test]
    fn later_rectangles_win_and_edges_are_exclusive() {
        let mut h = HitMap::default();
        h.push(Rect::new(0, 0, 10, 5), Target::Body(PanelId::Cpu));
        h.push(Rect::new(0, 0, 10, 1), Target::Title(PanelId::Cpu));
        h.push(Rect::new(3, 0, 0, 1), Target::Settings);
        assert_eq!(h.at(0, 0), Some(Target::Title(PanelId::Cpu)));
        assert_eq!(h.at(0, 1), Some(Target::Body(PanelId::Cpu)));
        assert_eq!(h.at(10, 1), None);
        assert_eq!(h.at(9, 4), Some(Target::Body(PanelId::Cpu)));
        assert_eq!(h.at(9, 5), None);
        assert_eq!(h.len(), 2, "empty rectangles are dropped");
    }

    #[test]
    fn mouse_buttons_map_to_the_spec_table() {
        let mut h = HitMap::default();
        h.push(Rect::new(0, 0, 5, 1), Target::Chip(PanelId::Mem));
        h.push(Rect::new(0, 1, 5, 1), Target::Title(PanelId::Disk));
        h.push(Rect::new(0, 2, 5, 1), Target::Timeline(PanelId::Disk));
        h.push(Rect::new(0, 3, 5, 1), Target::Bars(PanelId::Net));
        h.push(Rect::new(0, 4, 5, 1), Target::Bars(PanelId::Claude));
        h.push(Rect::new(0, 5, 5, 1), Target::Timeline(PanelId::Tokens));
        h.push(Rect::new(0, 6, 5, 1), Target::Interval);
        h.push(Rect::new(0, 7, 5, 1), Target::Body(PanelId::Cpu));
        h.push(Rect::new(0, 8, 5, 1), Target::Settings);
        h.push(
            Rect::new(0, 9, 5, 1),
            Target::Do(Action::GraphRows(PanelId::Cpu, 1)),
        );
        let l = MouseEventKind::Down(MouseButton::Left);
        let r = MouseEventKind::Down(MouseButton::Right);
        let m = |k, y| mouse_action(&h, click(k, 1, y));
        assert_eq!(m(l, 0), Some(Action::TogglePanel(PanelId::Mem)));
        assert_eq!(m(l, 1), Some(Action::Zoom(PanelId::Disk)));
        assert_eq!(m(r, 1), Some(Action::NextStyle(PanelId::Disk)));
        assert_eq!(m(r, 2), Some(Action::NextStyle(PanelId::Disk)));
        assert_eq!(m(l, 2), Some(Action::Focus(PanelId::Disk)));
        assert_eq!(
            m(MouseEventKind::ScrollUp, 2),
            Some(Action::Window(PanelId::Disk, 1))
        );
        assert_eq!(m(l, 3), Some(Action::Focus(PanelId::Net)));
        assert_eq!(m(l, 4), Some(Action::ClaudePopup));
        assert_eq!(m(MouseEventKind::ScrollUp, 4), None);
        assert_eq!(m(l, 5), Some(Action::NextSeries));
        assert_eq!(m(MouseEventKind::ScrollDown, 6), Some(Action::Interval(-1)));
        assert_eq!(m(l, 7), Some(Action::Focus(PanelId::Cpu)));
        assert_eq!(m(l, 8), Some(Action::Settings));
        assert_eq!(m(l, 9), Some(Action::GraphRows(PanelId::Cpu, 1)));
        assert_eq!(mouse_action(&h, click(l, 9, 19)), None);
        assert_eq!(m(MouseEventKind::Moved, 1), None);
    }

    #[test]
    fn keys_are_twins_of_the_mouse_actions() {
        let f = PanelId::Net;
        let none = KeyModifiers::NONE;
        let k = |c| key_action(key(KeyCode::Char(c), none), f, Overlay::None);
        assert_eq!(k('g'), Some(Action::TogglePart(f, Part::Graph)));
        assert_eq!(k('b'), Some(Action::TogglePart(f, Part::Bars)));
        assert_eq!(k('s'), Some(Action::TogglePart(f, Part::Summary)));
        assert_eq!(k('G'), Some(Action::ToggleAll(Some(Part::Graph))));
        assert_eq!(k('B'), Some(Action::ToggleAll(Some(Part::Bars))));
        assert_eq!(k('{'), Some(Action::GraphRows(f, -1)));
        assert_eq!(k('}'), Some(Action::GraphRows(f, 1)));
        assert_eq!(k('S'), Some(Action::NextStyle(f)));
        assert_eq!(k('m'), None, "the old mode cycle is gone");
        assert_eq!(k('+'), Some(Action::Window(f, 1)));
        assert_eq!(k('-'), Some(Action::Window(f, -1)));
        assert_eq!(k('3'), Some(Action::TogglePanel(PanelId::Disk)));
        assert_eq!(k('5'), Some(Action::TogglePanel(PanelId::Tokens)));
        assert_eq!(k('#'), Some(Action::Zoom(PanelId::Disk)));
        assert_eq!(
            key_action(
                key(KeyCode::Char('2'), KeyModifiers::SHIFT),
                f,
                Overlay::None
            ),
            Some(Action::Zoom(PanelId::Mem))
        );
        assert_eq!(k('v'), Some(Action::NextSeries));
        assert_eq!(k('i'), None, "`i` no longer cycles interfaces (v5)");
        assert_eq!(k('t'), Some(Action::NextTheme));
        assert_eq!(k('c'), Some(Action::ClaudePopup));
        assert_eq!(k('p'), Some(Action::Pause));
        assert_eq!(k('M'), Some(Action::ToggleMouse));
        assert_eq!(k('['), Some(Action::Interval(-1)));
        assert_eq!(k(']'), Some(Action::Interval(1)));
        assert_eq!(k(','), Some(Action::Settings));
        assert_eq!(k('?'), Some(Action::Help));
        assert_eq!(k('q'), Some(Action::Quit));
        assert_eq!(k('7'), None);
        assert_eq!(
            key_action(
                key(KeyCode::Char('c'), KeyModifiers::CONTROL),
                f,
                Overlay::None
            ),
            Some(Action::Quit)
        );
        assert_eq!(
            key_action(key(KeyCode::Esc, none), f, Overlay::None),
            Some(Action::Escape)
        );
        assert_eq!(
            key_action(key(KeyCode::BackTab, KeyModifiers::SHIFT), f, Overlay::None),
            Some(Action::FocusStep { back: true })
        );
    }

    #[test]
    fn popups_take_the_cursor_keys() {
        let f = PanelId::Cpu;
        let k = |c, o| key_action(key(c, KeyModifiers::NONE), f, o);
        assert_eq!(
            k(KeyCode::Right, Overlay::Help),
            Some(Action::HelpTabStep(1))
        );
        assert_eq!(k(KeyCode::Down, Overlay::Help), Some(Action::HelpScroll(1)));
        assert_eq!(
            k(KeyCode::Down, Overlay::Settings),
            Some(Action::SettingsNav(0, 1))
        );
        assert_eq!(
            k(KeyCode::Char(' '), Overlay::Settings),
            Some(Action::SettingsPress)
        );
        assert_eq!(k(KeyCode::Down, Overlay::None), None);
        assert_eq!(k(KeyCode::Char(' '), Overlay::None), None);
    }
}
