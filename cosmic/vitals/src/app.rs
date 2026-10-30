// SPDX-License-Identifier: MIT
//! The application state: configuration, sampled data, graph history and UI state, plus the one place
//! that turns an [`Action`] into a change. No terminal here, so every path is testable headless.

use std::cell::Cell;
use std::time::{Duration, Instant};

use ratatui::crossterm::event::{KeyEvent, MouseEvent};

use crate::claude::ClaudeView;
use crate::config::{Config, GRAPH_ROWS, INTERVALS_MS, PanelId, WINDOWS_MIN, step_in};
use crate::history::{MAX_POINTS, Ring, points_for_window};
use crate::input::{Action, HitMap, key_action, mouse_action};
use crate::sample::System;
use crate::sample::net::Rate;
use crate::theme::{Theme, ThemeBook};
use crate::tokens::{TokenView, TokenWindow};
use crate::ui::{help_text, settings};

/// Graph history: the UI owns one ring per series (the sampler keeps none).
pub struct Rings {
    pub cpu: Ring,
    pub mem: Ring,
    pub disk_read: Ring,
    pub disk_write: Ring,
    pub net_down: Ring,
    pub net_up: Ring,
    pub claude_session: Ring,
    pub claude_week: Ring,
}

impl Rings {
    fn new() -> Self {
        let r = || Ring::new(MAX_POINTS);
        Rings {
            cpu: r(),
            mem: r(),
            disk_read: r(),
            disk_write: r(),
            net_down: r(),
            net_up: r(),
            claude_session: r(),
            claude_week: r(),
        }
    }
}

/// Everything sampled or read, which the widgets only read.
pub struct Data {
    pub sys: System,
    pub rings: Rings,
    pub tokens: TokenView,
    pub claude: ClaudeView,
    /// Wall clock of the last tick, ms since the epoch (injected, so tests are stable).
    pub now_ms: i64,
    /// `HH:MM` local time of the last tick.
    pub clock: String,
    /// Claude session resets seen while running (for the Tokens graph marks).
    pub session_resets: Vec<i64>,
    last_session_reset: Option<i64>,
}

impl Data {
    /// Bytes received and sent by the included interfaces since vitals started.
    pub fn net_since_start(&self) -> (u64, u64) {
        self.sys.net.since_start()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Overlay {
    None,
    Help,
    Claude,
    Settings,
}

pub struct UiState {
    pub focus: PanelId,
    pub zoom: Option<PanelId>,
    pub overlay: Overlay,
    pub paused: bool,
    /// One line for the header (a bad config or theme file).
    pub warn: Option<String>,
    /// The help popup's tab (0-5 a section, 6 the controls) and how many lines it is scrolled.
    pub help_tab: usize,
    pub help_scroll: u16,
    /// The most the help text can scroll, written by the last draw (clamps `help_scroll`).
    pub help_max: Cell<u16>,
    /// The settings control the arrow keys are on: (row, column). `None` until a cursor key is used.
    pub settings_cursor: Option<(usize, usize)>,
}

/// Buffers reused by every draw, so a steady-state frame allocates nothing.
#[derive(Default)]
pub struct Scratch {
    pub text: String,
    pub text2: String,
    pub cols: Vec<f32>,
    pub cols2: Vec<f32>,
    pub up: Vec<f32>,
    pub down: Vec<f32>,
    pub cache: Vec<f32>,
    pub stack: Vec<[f32; 4]>,
    pub stack2: Vec<[f32; 4]>,
    pub marks: Vec<i64>,
}

pub struct App {
    pub cfg: Config,
    pub theme: Theme,
    pub themes: ThemeBook,
    pub data: Data,
    pub ui: UiState,
    pub hits: HitMap,
    pub scratch: Scratch,
    pub quit: bool,
    /// Set when `M` flipped `cfg.mouse`: the loop applies it to the terminal.
    pub mouse_changed: bool,
    /// Set when an action changed the config: the loop saves it at once (spec §9).
    pub cfg_changed: bool,
}

/// Local `HH:MM` for the header, with no allocation and no `chrono`.
fn local_clock(now_ms: i64, out: &mut String) {
    let secs: libc::time_t = (now_ms / 1000) as libc::time_t;
    // SAFETY: `tm` is plain data that `localtime_r` fills; both pointers are valid for the call.
    let tm = unsafe {
        let mut tm: libc::tm = std::mem::zeroed();
        if libc::localtime_r(&secs, &mut tm).is_null() {
            return;
        }
        tm
    };
    out.clear();
    let (h, m) = (tm.tm_hour as u8, tm.tm_min as u8);
    out.push((b'0' + h / 10) as char);
    out.push((b'0' + h % 10) as char);
    out.push(':');
    out.push((b'0' + m / 10) as char);
    out.push((b'0' + m % 10) as char);
}

impl App {
    pub fn new(cfg: Config, themes: ThemeBook, sys: System, now_ms: i64) -> App {
        let (theme, theme_warn) = themes.build(&cfg.theme);
        let mut app = App {
            cfg,
            theme,
            themes,
            data: Data {
                sys,
                rings: Rings::new(),
                tokens: TokenView::new(),
                claude: ClaudeView::default(),
                now_ms,
                clock: String::with_capacity(5),
                session_resets: Vec::new(),
                last_session_reset: None,
            },
            ui: UiState {
                focus: PanelId::Cpu,
                zoom: None,
                overlay: Overlay::None,
                paused: false,
                warn: theme_warn,
                help_tab: 0,
                help_scroll: 0,
                help_max: Cell::new(0),
                settings_cursor: None,
            },
            hits: HitMap::default(),
            scratch: Scratch::default(),
            quit: false,
            mouse_changed: false,
            cfg_changed: false,
        };
        if !app.is_visible(app.ui.focus) {
            app.ui.focus = app.cfg.visible().next().unwrap_or(PanelId::Cpu);
        }
        local_clock(now_ms, &mut app.data.clock);
        app.sync_net();
        app
    }

    /// Hands the config's `include` and `exclude` lists to the network sampler.
    fn sync_net(&mut self) {
        let n = &self.cfg.section.network;
        self.data.sys.net.set_overrides(&n.include, &n.exclude);
    }

    /// A section is shown when it is enabled and a row names it.
    pub fn is_visible(&self, p: PanelId) -> bool {
        self.cfg.is_visible(p)
    }

    pub fn interval(&self) -> Duration {
        Duration::from_millis(self.cfg.interval_ms)
    }

    /// The next visible section after `from` in reading order (`from` itself if it is the only one).
    fn next_visible(&self, from: PanelId, back: bool) -> PanelId {
        // Walk the whole reading order so a section that was just hidden still has its place.
        let order: Vec<PanelId> = self.cfg.resolve(false).flat().collect();
        let n = order.len();
        let Some(i) = order.iter().position(|&p| p == from) else {
            return self.cfg.visible().next().unwrap_or(from);
        };
        (1..n)
            .map(|k| order[(i + if back { n - k } else { k }) % n])
            .find(|&p| self.cfg.is_visible(p))
            .unwrap_or(from)
    }

    /// One tick: sample (unless paused), push the graph history, refresh the Claude strings and the clock.
    /// Token messages are drained by the caller into `data.tokens` before this.
    pub fn tick(&mut self, now: Instant, now_ms: i64) {
        self.data.now_ms = now_ms;
        local_clock(now_ms, &mut self.data.clock);
        if !self.ui.paused {
            let _ = self.data.sys.sample(now);
            self.push_history();
        }
        self.data.claude.refresh(now_ms);
        self.note_session_reset(now_ms);
        if !self.ui.paused {
            let (s, w) = self
                .data
                .claude
                .merged
                .as_ref()
                .map_or((f32::NAN, f32::NAN), |m| {
                    let p = |l: &Option<claude_usage_core::reading::Limit>| {
                        l.as_ref().map_or(f32::NAN, |l| l.percent as f32)
                    };
                    (p(&m.reading.session), p(&m.reading.week))
                });
            self.data.rings.claude_session.push(s);
            self.data.rings.claude_week.push(w);
        }
    }

    fn push_history(&mut self) {
        let d = &mut self.data;
        d.rings.cpu.push(d.sys.cpu.mean());
        d.rings.mem.push(d.sys.mem.mem().used_percent());
        let disk = d.sys.disk.rates();
        d.rings.disk_read.push(disk.read_bps as f32);
        d.rings.disk_write.push(disk.write_bps as f32);
        let net = self.net_rate();
        self.data.rings.net_down.push(net.down_bps as f32);
        self.data.rings.net_up.push(net.up_bps as f32);
    }

    /// The summed rates of the included network interfaces.
    pub fn net_rate(&self) -> Rate {
        self.data.sys.net.total()
    }

    /// Remembers a session reset that passed while vitals was running.
    fn note_session_reset(&mut self, now_ms: i64) {
        let cur = self
            .data
            .claude
            .merged
            .as_ref()
            .and_then(|m| m.reading.session.as_ref())
            .and_then(|s| s.resets_at);
        if let (Some(old), new) = (self.data.last_session_reset, cur)
            && new != Some(old)
            && old <= now_ms
            && !self.data.session_resets.contains(&old)
        {
            if self.data.session_resets.len() >= 64 {
                self.data.session_resets.remove(0);
            }
            self.data.session_resets.push(old);
        }
        if cur.is_some() {
            self.data.last_session_reset = cur;
        }
    }

    /// Graph points for a system panel's window at the current interval.
    pub fn window_points(&self, p: PanelId) -> usize {
        let min = self.cfg.window_min(p).unwrap_or(2);
        points_for_window(Duration::from_secs(u64::from(min) * 60), self.interval())
    }

    pub fn token_window(&self) -> TokenWindow {
        TokenWindow::from_label(&self.cfg.section.tokens.window).unwrap_or(TokenWindow::H24)
    }

    pub fn handle_key(&mut self, key: KeyEvent) {
        if let Some(a) = key_action(key, self.ui.focus, self.ui.overlay) {
            self.apply(a);
        }
    }

    pub fn handle_mouse(&mut self, ev: MouseEvent) {
        if !self.cfg.mouse {
            return;
        }
        if let Some(a) = mouse_action(&self.hits, ev) {
            self.apply(a);
        }
    }

    /// Does an action and notes whether it changed the config (so the loop saves it).
    pub fn apply(&mut self, action: Action) {
        let before = self.cfg.clone();
        self.apply_inner(action);
        if self.cfg != before {
            self.cfg_changed = true;
        }
    }

    fn toggle_overlay(&mut self, o: Overlay) {
        self.ui.overlay = if self.ui.overlay == o {
            Overlay::None
        } else {
            o
        };
        self.ui.settings_cursor = None;
        self.ui.help_scroll = 0;
    }

    fn apply_inner(&mut self, action: Action) {
        let focus_on = |app: &mut App, p: PanelId| {
            if app.is_visible(p) {
                app.ui.focus = p;
            }
        };
        match action {
            Action::TogglePanel(p) => self.toggle_panel(p),
            Action::TogglePart(p, part) => {
                self.cfg.toggle_part(p, part);
                focus_on(self, p);
            }
            Action::ToggleAll(part) => self.toggle_all(part),
            Action::GraphRows(p, d) => {
                let c = self.cfg.common_mut(p);
                c.graph_rows = c
                    .graph_rows
                    .saturating_add_signed(i16::from(d))
                    .clamp(*GRAPH_ROWS.start(), *GRAPH_ROWS.end());
                focus_on(self, p);
            }
            Action::NextStyle(p) => {
                let next = self.cfg.style(p).next();
                self.cfg.common_mut(p).style = next;
                focus_on(self, p);
            }
            Action::AllStyles => {
                let next = self.cfg.style(PanelId::Cpu).next();
                for p in PanelId::ALL {
                    self.cfg.common_mut(p).style = next;
                }
            }
            Action::Window(p, d) => {
                self.step_window(p, d > 0);
                focus_on(self, p);
            }
            Action::Zoom(p) => {
                if self.is_visible(p) {
                    self.ui.zoom = if self.ui.zoom == Some(p) {
                        None
                    } else {
                        Some(p)
                    };
                    self.ui.focus = p;
                }
            }
            Action::Focus(p) => focus_on(self, p),
            Action::FocusStep { back } => {
                self.ui.focus = self.next_visible(self.ui.focus, back);
            }
            Action::MoveSection(p, d) => {
                self.cfg.move_section(p, d);
            }
            Action::Interval(d) => {
                self.cfg.interval_ms = step_in(&INTERVALS_MS, self.cfg.interval_ms, d > 0);
            }
            Action::CycleInterval => {
                let i = INTERVALS_MS.iter().position(|&x| x == self.cfg.interval_ms);
                self.cfg.interval_ms = INTERVALS_MS[(i.unwrap_or(0) + 1) % INTERVALS_MS.len()];
            }
            Action::NextTheme => {
                let next = self.themes.next_name(&self.cfg.theme);
                let (theme, warn) = self.themes.build(&next);
                self.cfg.theme = next;
                self.theme = theme;
                self.ui.warn = warn;
            }
            Action::NextLayout => self.cfg.layout = self.cfg.layout.next(),
            Action::ToggleIface(i) => self.toggle_iface(i),
            Action::TogglePerIface => {
                let n = &mut self.cfg.section.network;
                n.per_interface = !n.per_interface;
            }
            Action::NextSeries => {
                let t = &mut self.cfg.section.tokens;
                t.series = t.series.next();
                focus_on(self, PanelId::Tokens);
            }
            Action::ClaudePopup => self.toggle_overlay(Overlay::Claude),
            Action::Settings => self.toggle_overlay(Overlay::Settings),
            Action::Help => {
                let opening = self.ui.overlay != Overlay::Help;
                self.toggle_overlay(Overlay::Help);
                if opening {
                    self.ui.help_tab = self.ui.focus.index();
                }
            }
            Action::HelpTab(i) => {
                self.ui.help_tab = i.min(help_text::TABS - 1);
                self.ui.help_scroll = 0;
            }
            Action::HelpTabStep(d) => {
                let n = help_text::TABS as i64;
                self.ui.help_tab = (self.ui.help_tab as i64 + i64::from(d)).rem_euclid(n) as usize;
                self.ui.help_scroll = 0;
            }
            Action::HelpScroll(d) => {
                let max = i32::from(self.ui.help_max.get());
                let v = (i32::from(self.ui.help_scroll) + i32::from(d)).clamp(0, max);
                self.ui.help_scroll = v as u16;
            }
            Action::SettingsNav(dx, dy) => {
                let (r, c) = self.ui.settings_cursor.unwrap_or((0, 0));
                let started = self.ui.settings_cursor.is_some();
                let row = if started {
                    (r as i64 + i64::from(dy)).clamp(0, settings::ROWS as i64 - 1) as usize
                } else {
                    r
                };
                let cols = settings::cols(row, self.data.sys.net.interfaces().count()) as i64;
                let col = if started {
                    (c as i64 + i64::from(dx)).clamp(0, cols - 1) as usize
                } else {
                    c
                };
                self.ui.settings_cursor = Some((row, col.min(cols as usize - 1)));
            }
            Action::SettingsPress => {
                if let Some((r, c)) = self.ui.settings_cursor
                    && let Some(a) = settings::control(r, c)
                {
                    self.apply_inner(a);
                }
            }
            Action::ClosePopup => self.ui.overlay = Overlay::None,
            Action::Pause => self.ui.paused = !self.ui.paused,
            Action::ToggleMouse => {
                self.cfg.mouse = !self.cfg.mouse;
                self.mouse_changed = true;
            }
            Action::Escape => {
                if self.ui.overlay != Overlay::None {
                    self.ui.overlay = Overlay::None;
                } else {
                    self.ui.zoom = None;
                }
            }
            Action::Quit => self.quit = true,
        }
    }

    fn toggle_all(&mut self, part: Option<crate::config::Part>) {
        self.cfg.toggle_all(part);
        if !self.is_visible(self.ui.focus) {
            self.ui.focus = self.cfg.visible().next().unwrap_or(self.ui.focus);
        }
        if self.ui.zoom.is_some_and(|z| !self.is_visible(z)) {
            self.ui.zoom = None;
        }
    }

    fn toggle_panel(&mut self, p: PanelId) {
        if self.cfg.enabled(p) {
            self.cfg.set_enabled(p, false);
            if self.ui.zoom == Some(p) {
                self.ui.zoom = None;
            }
            if self.ui.focus == p {
                self.ui.focus = self.next_visible(p, false);
            }
        } else {
            self.cfg.set_enabled(p, true);
            self.ui.focus = p;
        }
    }

    fn step_window(&mut self, p: PanelId, up: bool) {
        match p {
            PanelId::Tokens => {
                let all = TokenWindow::ALL;
                let i = all
                    .iter()
                    .position(|&w| w == self.token_window())
                    .unwrap_or(2);
                let j = if up {
                    (i + 1).min(all.len() - 1)
                } else {
                    i.saturating_sub(1)
                };
                self.cfg.section.tokens.window = all[j].label().to_owned();
            }
            PanelId::Claude => {}
            _ => {
                if let Some(cur) = self.cfg.window_min(p) {
                    self.cfg
                        .set_window_min(p, step_in(&WINDOWS_MIN, u64::from(cur), up));
                }
            }
        }
    }

    /// Counts the `i`-th listed network interface toward `all`, or stops counting it.
    fn toggle_iface(&mut self, i: usize) {
        let Some((name, physical, included)) = self
            .data
            .sys
            .net
            .interfaces()
            .nth(i)
            .map(|f| (f.name.clone(), f.is_physical(), f.included()))
        else {
            return;
        };
        self.cfg
            .section
            .network
            .set_included(&name, !included, physical);
        self.sync_net();
        self.data.rings.net_down.clear();
        self.data.rings.net_up.clear();
    }
}
