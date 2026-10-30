// SPDX-License-Identifier: MIT
//! Color (spec §7): terminal depth detection, the built-in and custom themes, 101-entry gradient tables
//! computed once per theme, and the 256 / 16 / monochrome fallbacks.
//!
//! Nothing here reads the environment or the disk: the caller passes the values in, so tests are stable.

use ratatui::style::{Color, Modifier, Style};

pub type Rgb = (u8, u8, u8);

/// How many colors the terminal can show.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Depth {
    True,
    Ansi256,
    Ansi16,
    /// `NO_COLOR`: shapes only; danger is bold and reverse video.
    Mono,
}

/// `COLORTERM=truecolor|24bit` is 24-bit, `TERM` containing `256color` is 256, else 16; `NO_COLOR` wins.
pub fn detect_depth(get: &dyn Fn(&str) -> Option<String>) -> Depth {
    if get("NO_COLOR").is_some_and(|v| !v.is_empty()) {
        return Depth::Mono;
    }
    let colorterm = get("COLORTERM").unwrap_or_default();
    if colorterm == "truecolor" || colorterm == "24bit" {
        return Depth::True;
    }
    if get("TERM").is_some_and(|t| t.contains("256color")) {
        return Depth::Ansi256;
    }
    Depth::Ansi16
}

pub const CLAUDE_ORANGE: Rgb = (0xD9, 0x77, 0x57);
const CLAUDE_TRACK: Rgb = (0x3a, 0x26, 0x20);
const DANGER: Rgb = (0xef, 0x44, 0x44);

/// The xterm values of the 16 ANSI colors, with the `ratatui` color that names each.
const ANSI16: [(Rgb, Color); 16] = [
    ((0, 0, 0), Color::Black),
    ((205, 0, 0), Color::Red),
    ((0, 205, 0), Color::Green),
    ((205, 205, 0), Color::Yellow),
    ((0, 0, 238), Color::Blue),
    ((205, 0, 205), Color::Magenta),
    ((0, 205, 205), Color::Cyan),
    ((229, 229, 229), Color::Gray),
    ((127, 127, 127), Color::DarkGray),
    ((255, 0, 0), Color::LightRed),
    ((0, 255, 0), Color::LightGreen),
    ((255, 255, 0), Color::LightYellow),
    ((92, 92, 255), Color::LightBlue),
    ((255, 0, 255), Color::LightMagenta),
    ((0, 255, 255), Color::LightCyan),
    ((255, 255, 255), Color::White),
];

fn dist(a: Rgb, b: Rgb) -> u32 {
    let d = |x: u8, y: u8| u32::from(x.abs_diff(y)).pow(2);
    d(a.0, b.0) + d(a.1, b.1) + d(a.2, b.2)
}

/// Nearest entry of the 256-color palette (6x6x6 cube or the 24 grays).
pub fn nearest_256(c: Rgb) -> u8 {
    const LEVELS: [u8; 6] = [0, 95, 135, 175, 215, 255];
    let near = |v: u8| {
        (0..6usize)
            .min_by_key(|&i| LEVELS[i].abs_diff(v))
            .unwrap_or(0)
    };
    let (r, g, b) = (near(c.0), near(c.1), near(c.2));
    let cube = (LEVELS[r], LEVELS[g], LEVELS[b]);
    let cube_idx = 16 + 36 * r as u8 + 6 * g as u8 + b as u8;
    let mean = ((u16::from(c.0) + u16::from(c.1) + u16::from(c.2)) / 3) as u8;
    let gray_i = (u16::from(mean.saturating_sub(8)) / 10).min(23) as u8;
    let gv = 8 + 10 * gray_i;
    if dist(c, (gv, gv, gv)) < dist(c, cube) {
        232 + gray_i
    } else {
        cube_idx
    }
}

/// Nearest of the 16 ANSI colors.
pub fn nearest_16(c: Rgb) -> Color {
    ANSI16
        .iter()
        .min_by_key(|(rgb, _)| dist(c, *rgb))
        .map_or(Color::Reset, |&(_, col)| col)
}

/// An RGB value as the terminal can show it.
pub fn map_color(depth: Depth, c: Rgb) -> Color {
    match depth {
        Depth::True => Color::Rgb(c.0, c.1, c.2),
        Depth::Ansi256 => Color::Indexed(nearest_256(c)),
        Depth::Ansi16 => nearest_16(c),
        Depth::Mono => Color::Reset,
    }
}

fn lerp(a: u8, b: u8, t: f32) -> u8 {
    (f32::from(a) + (f32::from(b) - f32::from(a)) * t).round() as u8
}

fn mix(a: Rgb, b: Rgb, t: f32) -> Rgb {
    (lerp(a.0, b.0, t), lerp(a.1, b.1, t), lerp(a.2, b.2, t))
}

/// A color along evenly spaced stops, `t` from 0 to 1.
fn along(stops: &[Rgb], t: f32) -> Rgb {
    match stops {
        [] => (128, 128, 128),
        [one] => *one,
        _ => {
            let t = t.clamp(0.0, 1.0) * (stops.len() - 1) as f32;
            let i = (t.floor() as usize).min(stops.len() - 2);
            mix(stops[i], stops[i + 1], t - i as f32)
        }
    }
}

/// A gradient by value: entry `p` is the color of a reading of `p` percent.
pub type Ramp = [Color; 101];

fn ramp(depth: Depth, stops: &[Rgb]) -> Ramp {
    std::array::from_fn(|p| map_color(depth, along(stops, p as f32 / 100.0)))
}

fn scaled(c: Rgb, k: f32) -> Rgb {
    let s = |v: u8| (f32::from(v) * k).round().min(255.0) as u8;
    (s(c.0), s(c.1), s(c.2))
}

/// The series that have their own gradient.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Series {
    Cpu,
    Mem,
    Cache,
    DiskRead,
    DiskWrite,
    NetDown,
    NetUp,
    TokIn,
    TokOut,
    TokCache,
    /// Claude session and week lines.
    Claude,
}

const SERIES: [Series; 11] = [
    Series::Cpu,
    Series::Mem,
    Series::Cache,
    Series::DiskRead,
    Series::DiskWrite,
    Series::NetDown,
    Series::NetUp,
    Series::TokIn,
    Series::TokOut,
    Series::TokCache,
    Series::Claude,
];

impl Series {
    fn key(self) -> &'static str {
        match self {
            Self::Cpu => "cpu",
            Self::Mem => "memory",
            Self::Cache => "cache",
            Self::DiskRead => "disk_read",
            Self::DiskWrite => "disk_write",
            Self::NetDown => "net_down",
            Self::NetUp => "net_up",
            Self::TokIn => "tokens_in",
            Self::TokOut => "tokens_out",
            Self::TokCache => "tokens_cache",
            Self::Claude => "claude",
        }
    }
}

/// Gradient stops of the `default` (btop-like) theme.
fn default_stops(s: Series) -> Vec<Rgb> {
    let dim = |c: Rgb| scaled(c, 0.4);
    match s {
        Series::Cpu => vec![(0x4c, 0xc9, 0x5e), (0xe5, 0xc0, 0x3a), (0xe5, 0x48, 0x48)],
        Series::Mem => vec![(0x2a, 0xb5, 0xa5), (0x9b, 0x6b, 0xe0)],
        Series::Cache => vec![dim((0x5b, 0x8f, 0xa8)), (0x5b, 0x8f, 0xa8)],
        Series::DiskRead => vec![dim((0x3a, 0xd6, 0xf0)), (0x3a, 0xd6, 0xf0)],
        Series::DiskWrite => vec![dim((0xa6, 0xe2, 0x2e)), (0xa6, 0xe2, 0x2e)],
        Series::NetDown => vec![dim((0x4a, 0x8a, 0xff)), (0x4a, 0x8a, 0xff)],
        Series::NetUp => vec![dim((0xe0, 0x50, 0xe0)), (0xe0, 0x50, 0xe0)],
        Series::TokIn => vec![dim((0xE8, 0xB4, 0x8F)), (0xE8, 0xB4, 0x8F)],
        Series::TokOut => vec![dim(CLAUDE_ORANGE), CLAUDE_ORANGE],
        Series::TokCache => vec![dim((0x8A, 0x5A, 0x44)), (0xB0, 0x7A, 0x5C)],
        Series::Claude => vec![dim(CLAUDE_ORANGE), CLAUDE_ORANGE],
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Theme {
    pub name: String,
    pub depth: Depth,
    pub fg: Color,
    pub title: Color,
    pub border: Color,
    pub muted: Color,
    pub accent: Color,
    pub danger: Color,
    pub claude: Color,
    pub claude_track: Color,
    ramps: Vec<Ramp>,
    /// Temperature colors: normal, warm, hot.
    temps: [Color; 3],
}

/// Raw colors of a theme before they are mapped to a depth.
struct Palette {
    fg: Option<Rgb>,
    title: Rgb,
    border: Rgb,
    muted: Rgb,
    accent: Rgb,
    danger: Rgb,
    claude: Rgb,
    stops: Vec<(Series, Vec<Rgb>)>,
}

impl Palette {
    fn default_theme() -> Palette {
        Palette {
            fg: None,
            title: (0xd8, 0xdc, 0xe6),
            border: (0x6c, 0x72, 0x86),
            muted: (0x8a, 0x90, 0xa0),
            accent: (0x56, 0xb6, 0xc2),
            danger: DANGER,
            claude: CLAUDE_ORANGE,
            stops: SERIES.iter().map(|&s| (s, default_stops(s))).collect(),
        }
    }

    /// The load-colour ramp: black at 0, the accent at 85, white at 100.
    fn ramp(accent: Rgb) -> Palette {
        let ramp_stops = [(0, 0, 0), accent, (255, 255, 255)];
        // The accent sits at 85%: stops are evenly spaced, so build the ramp from sampled points.
        let mut stops = Vec::new();
        for p in 0..=20 {
            let v = p as f32 * 5.0;
            let c = if v <= 85.0 {
                mix(ramp_stops[0], ramp_stops[1], v / 85.0)
            } else {
                mix(ramp_stops[1], ramp_stops[2], (v - 85.0) / 15.0)
            };
            stops.push(c);
        }
        let mut p = Palette::default_theme();
        for (s, st) in &mut p.stops {
            if !matches!(
                s,
                Series::TokIn | Series::TokOut | Series::TokCache | Series::Claude
            ) {
                *st = stops.clone();
            }
        }
        p.accent = accent;
        p
    }

    /// Shades of one hue.
    fn mono(accent: Rgb) -> Palette {
        let mut p = Palette::default_theme();
        for (s, st) in &mut p.stops {
            if !matches!(
                s,
                Series::TokIn | Series::TokOut | Series::TokCache | Series::Claude
            ) {
                *st = vec![scaled(accent, 0.3), accent];
            }
        }
        p.accent = accent;
        p
    }
}

pub fn parse_hex(s: &str) -> Option<Rgb> {
    let h = s.trim().strip_prefix('#')?;
    if h.len() != 6 {
        return None;
    }
    let v = u32::from_str_radix(h, 16).ok()?;
    Some(((v >> 16) as u8, (v >> 8) as u8, v as u8))
}

/// The COSMIC accent from the text of `.../CosmicTheme.{Dark,Light}/v1/accent` (RON with float channels).
pub fn parse_cosmic_accent(text: &str) -> Option<Rgb> {
    let base = &text[text.find("base")?..];
    let chan = |name: &str| -> Option<u8> {
        let at = base.find(name)? + name.len();
        let rest = base[at..].trim_start_matches([':', ' ']);
        let end = rest.find([',', ')', '\n'])?;
        let v: f32 = rest[..end].trim().parse().ok()?;
        Some((v.clamp(0.0, 1.0) * 255.0).round() as u8)
    };
    Some((chan("red")?, chan("green")?, chan("blue")?))
}

/// Names of the built-in themes, in cycle order.
pub const BUILTIN: [&str; 3] = ["default", "ramp", "mono"];

/// What a theme build needs beyond its name.
#[derive(Debug, Clone, Default)]
pub struct ThemeInputs {
    /// `accent = "#rrggbb"` from the config, for `mono`.
    pub accent: Option<Rgb>,
    /// The COSMIC accent, for `ramp`.
    pub cosmic_accent: Option<Rgb>,
}

impl Theme {
    /// A built-in theme by name, `None` for an unknown name.
    pub fn builtin(name: &str, depth: Depth, inputs: &ThemeInputs) -> Option<Theme> {
        let palette = match name {
            "default" => Palette::default_theme(),
            "ramp" => Palette::ramp(inputs.cosmic_accent.unwrap_or(DANGER)),
            "mono" => Palette::mono(inputs.accent.unwrap_or((0x7a, 0xa2, 0xf7))),
            _ => return None,
        };
        Some(Self::from_palette(name, depth, palette))
    }

    /// A custom theme from `~/.config/vitals/themes/<name>.toml`: any of `fg`, `title`, `border`, `muted`,
    /// `accent`, `danger`, `claude` as `#rrggbb`, and `[series]` gradient stop lists. Unlisted entries come
    /// from `default`.
    pub fn custom(name: &str, text: &str, depth: Depth) -> Result<Theme, String> {
        let table: toml::Table = toml::from_str(text).map_err(|e| e.message().to_string())?;
        let mut p = Palette::default_theme();
        let hex = |k: &str| -> Result<Option<Rgb>, String> {
            match table.get(k).and_then(|v| v.as_str()) {
                None => Ok(None),
                Some(s) => parse_hex(s)
                    .map(Some)
                    .ok_or_else(|| format!("{k}: expected #rrggbb")),
            }
        };
        if let Some(c) = hex("fg")? {
            p.fg = Some(c);
        }
        for (key, slot) in [
            ("title", &mut p.title),
            ("border", &mut p.border),
            ("muted", &mut p.muted),
            ("accent", &mut p.accent),
            ("danger", &mut p.danger),
            ("claude", &mut p.claude),
        ] {
            if let Some(c) = hex(key)? {
                *slot = c;
            }
        }
        if let Some(series) = table.get("series").and_then(|v| v.as_table()) {
            for (s, stops) in &mut p.stops {
                let Some(list) = series.get(s.key()).and_then(|v| v.as_array()) else {
                    continue;
                };
                let parsed: Option<Vec<Rgb>> = list
                    .iter()
                    .map(|v| v.as_str().and_then(parse_hex))
                    .collect();
                match parsed {
                    Some(v) if !v.is_empty() => *stops = v,
                    _ => return Err(format!("series.{}: expected #rrggbb colors", s.key())),
                }
            }
        }
        Ok(Self::from_palette(name, depth, p))
    }

    fn from_palette(name: &str, depth: Depth, p: Palette) -> Theme {
        let m = |c: Rgb| map_color(depth, c);
        let claude = match depth {
            Depth::Ansi256 if p.claude == CLAUDE_ORANGE => Color::Indexed(173),
            Depth::Ansi16 if p.claude == CLAUDE_ORANGE => Color::Yellow,
            _ => m(p.claude),
        };
        let mut ramps = vec![[Color::Reset; 101]; SERIES.len()];
        for (s, stops) in &p.stops {
            let i = SERIES.iter().position(|x| x == s).unwrap_or(0);
            ramps[i] = ramp(depth, stops);
        }
        let ansi = |c: Rgb, named: Color| {
            if depth == Depth::Ansi16 { named } else { m(c) }
        };
        Theme {
            name: name.to_owned(),
            depth,
            fg: p.fg.map_or(Color::Reset, m),
            title: m(p.title),
            border: m(p.border),
            muted: if depth == Depth::Ansi16 {
                Color::DarkGray
            } else {
                m(p.muted)
            },
            accent: m(p.accent),
            danger: if depth == Depth::Ansi16 {
                Color::Red
            } else {
                m(p.danger)
            },
            claude,
            claude_track: if depth == Depth::Ansi16 {
                Color::DarkGray
            } else {
                m(CLAUDE_TRACK)
            },
            ramps,
            temps: [
                ansi((0x8a, 0x90, 0xa0), Color::DarkGray),
                ansi((0xe5, 0xc0, 0x3a), Color::Yellow),
                ansi((0xe5, 0x48, 0x48), Color::Red),
            ],
        }
    }

    /// The color of a reading of `percent` (0 to 100) in a series.
    pub fn ramp(&self, s: Series, percent: f32) -> Color {
        let i = SERIES.iter().position(|&x| x == s).unwrap_or(0);
        let p = if percent.is_nan() { 0.0 } else { percent };
        self.ramps[i][p.clamp(0.0, 100.0).round() as usize]
    }

    /// Temperatures: below 60 C muted, 60 to 80 yellow, above 80 red.
    pub fn temp(&self, c: f32) -> Color {
        self.temps[usize::from(c >= 60.0) + usize::from(c > 80.0)]
    }

    pub fn is_mono(&self) -> bool {
        self.depth == Depth::Mono
    }

    pub fn plain(&self) -> Style {
        Style::new().fg(self.fg)
    }

    pub fn muted_style(&self) -> Style {
        let s = Style::new().fg(self.muted);
        if self.is_mono() {
            s.add_modifier(Modifier::DIM)
        } else {
            s
        }
    }

    pub fn title_style(&self) -> Style {
        Style::new().fg(self.title).add_modifier(Modifier::BOLD)
    }

    pub fn border_style(&self, focused: bool) -> Style {
        if focused {
            Style::new().fg(self.accent).add_modifier(Modifier::BOLD)
        } else {
            Style::new().fg(self.border)
        }
    }

    /// Danger: red and bold, or bold reverse video without color.
    pub fn danger_style(&self) -> Style {
        if self.is_mono() {
            Style::new().add_modifier(Modifier::BOLD | Modifier::REVERSED)
        } else {
            Style::new().fg(self.danger).add_modifier(Modifier::BOLD)
        }
    }

    pub fn claude_style(&self) -> Style {
        Style::new().fg(self.claude)
    }
}

/// Every theme the `t` key cycles through: the built-ins, then custom ones from the themes folder.
#[derive(Debug, Clone)]
pub struct ThemeBook {
    pub depth: Depth,
    pub inputs: ThemeInputs,
    /// `(name, file text)` of the custom themes, sorted by name.
    custom: Vec<(String, String)>,
}

impl ThemeBook {
    pub fn new(depth: Depth, inputs: ThemeInputs, custom: Vec<(String, String)>) -> Self {
        ThemeBook {
            depth,
            inputs,
            custom,
        }
    }

    /// Reads `<dir>/*.toml` (the custom themes). A missing folder is no custom themes.
    pub fn read_custom(dir: &std::path::Path) -> Vec<(String, String)> {
        let mut found: Vec<(String, String)> = std::fs::read_dir(dir)
            .into_iter()
            .flatten()
            .flatten()
            .filter_map(|e| {
                let path = e.path();
                let name = path.file_stem()?.to_str()?.to_owned();
                (path.extension()? == "toml").then_some(())?;
                Some((name, std::fs::read_to_string(&path).ok()?))
            })
            .collect();
        found.sort();
        found
    }

    /// The COSMIC accent: the active mode's `accent` file under `<config>/cosmic`.
    pub fn read_cosmic_accent(config_home: &std::path::Path) -> Option<Rgb> {
        let cosmic = config_home.join("cosmic");
        let dark = std::fs::read_to_string(cosmic.join("com.system76.CosmicTheme.Mode/v1/is_dark"))
            .map_or(true, |t| t.trim() != "false");
        let mode = if dark { "Dark" } else { "Light" };
        let text = std::fs::read_to_string(
            cosmic.join(format!("com.system76.CosmicTheme.{mode}/v1/accent")),
        )
        .ok()?;
        parse_cosmic_accent(&text)
    }

    pub fn names(&self) -> impl Iterator<Item = &str> {
        BUILTIN
            .iter()
            .copied()
            .chain(self.custom.iter().map(|(n, _)| n.as_str()))
    }

    /// The theme by name; an unknown or broken name falls back to `default`, with the reason.
    pub fn build(&self, name: &str) -> (Theme, Option<String>) {
        if let Some(t) = Theme::builtin(name, self.depth, &self.inputs) {
            return (t, None);
        }
        let fallback = || Theme::builtin("default", self.depth, &self.inputs).expect("built-in");
        match self.custom.iter().find(|(n, _)| n == name) {
            Some((n, text)) => match Theme::custom(n, text, self.depth) {
                Ok(t) => (t, None),
                Err(e) => (fallback(), Some(format!("theme {name}: {e}"))),
            },
            None => (fallback(), Some(format!("unknown theme {name}"))),
        }
    }

    /// The name after `current` in the cycle, wrapping.
    pub fn next_name(&self, current: &str) -> String {
        let names: Vec<&str> = self.names().collect();
        let i = names
            .iter()
            .position(|&n| n == current)
            .map_or(0, |i| i + 1);
        names[i % names.len()].to_owned()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn env<'a>(pairs: &'a [(&'a str, &'a str)]) -> impl Fn(&str) -> Option<String> + 'a {
        move |k| {
            pairs
                .iter()
                .find(|(n, _)| *n == k)
                .map(|(_, v)| (*v).to_owned())
        }
    }

    #[test]
    fn depth_follows_colorterm_then_term_and_no_color_wins() {
        assert_eq!(
            detect_depth(&env(&[("COLORTERM", "truecolor")])),
            Depth::True
        );
        assert_eq!(detect_depth(&env(&[("COLORTERM", "24bit")])), Depth::True);
        assert_eq!(
            detect_depth(&env(&[("TERM", "xterm-256color")])),
            Depth::Ansi256
        );
        assert_eq!(detect_depth(&env(&[("TERM", "linux")])), Depth::Ansi16);
        assert_eq!(detect_depth(&env(&[])), Depth::Ansi16);
        assert_eq!(
            detect_depth(&env(&[("COLORTERM", "truecolor"), ("NO_COLOR", "1")])),
            Depth::Mono
        );
        assert_eq!(
            detect_depth(&env(&[("COLORTERM", "truecolor"), ("NO_COLOR", "")])),
            Depth::True
        );
    }

    #[test]
    fn colors_map_to_the_nearest_palette_entry() {
        assert_eq!(nearest_256((0, 0, 0)), 16);
        assert_eq!(nearest_256((255, 255, 255)), 231);
        assert_eq!(nearest_256((255, 0, 0)), 196);
        assert_eq!(nearest_256((128, 128, 128)), 244);
        assert_eq!(nearest_16((250, 10, 10)), Color::LightRed);
        assert_eq!(nearest_16((10, 10, 10)), Color::Black);
        assert_eq!(map_color(Depth::True, (1, 2, 3)), Color::Rgb(1, 2, 3));
        assert_eq!(map_color(Depth::Mono, (1, 2, 3)), Color::Reset);
    }

    #[test]
    fn claude_orange_has_its_documented_fallbacks() {
        let i = ThemeInputs::default();
        let t = |d| Theme::builtin("default", d, &i).unwrap();
        assert_eq!(t(Depth::True).claude, Color::Rgb(0xD9, 0x77, 0x57));
        assert_eq!(t(Depth::Ansi256).claude, Color::Indexed(173));
        assert_eq!(t(Depth::Ansi16).claude, Color::Yellow);
        assert_eq!(t(Depth::Mono).claude, Color::Reset);
        // Every built-in theme keeps the Claude row orange.
        for name in BUILTIN {
            let th = Theme::builtin(name, Depth::True, &i).unwrap();
            assert_eq!(th.claude, Color::Rgb(0xD9, 0x77, 0x57), "{name}");
        }
    }

    #[test]
    fn ramps_run_by_value() {
        let t = Theme::builtin("default", Depth::True, &ThemeInputs::default()).unwrap();
        assert_eq!(t.ramp(Series::Cpu, 0.0), Color::Rgb(0x4c, 0xc9, 0x5e));
        assert_eq!(t.ramp(Series::Cpu, 100.0), Color::Rgb(0xe5, 0x48, 0x48));
        assert_eq!(t.ramp(Series::Cpu, f32::NAN), t.ramp(Series::Cpu, 0.0));
        assert_eq!(t.ramp(Series::Cpu, 500.0), t.ramp(Series::Cpu, 100.0));
    }

    #[test]
    fn the_ramp_theme_is_black_accent_white() {
        let i = ThemeInputs {
            cosmic_accent: Some((255, 173, 0)),
            ..ThemeInputs::default()
        };
        let t = Theme::builtin("ramp", Depth::True, &i).unwrap();
        assert_eq!(t.ramp(Series::Cpu, 0.0), Color::Rgb(0, 0, 0));
        assert_eq!(t.ramp(Series::Cpu, 85.0), Color::Rgb(255, 173, 0));
        assert_eq!(t.ramp(Series::Cpu, 100.0), Color::Rgb(255, 255, 255));
        let fallback = Theme::builtin("ramp", Depth::True, &ThemeInputs::default()).unwrap();
        assert_eq!(
            fallback.ramp(Series::Mem, 85.0),
            Color::Rgb(0xef, 0x44, 0x44)
        );
    }

    #[test]
    fn mono_uses_one_hue() {
        let i = ThemeInputs {
            accent: Some((100, 200, 100)),
            ..ThemeInputs::default()
        };
        let t = Theme::builtin("mono", Depth::True, &i).unwrap();
        assert_eq!(t.ramp(Series::Cpu, 100.0), Color::Rgb(100, 200, 100));
        assert_eq!(t.ramp(Series::NetUp, 100.0), Color::Rgb(100, 200, 100));
    }

    #[test]
    fn cosmic_accent_reads_the_base_channels() {
        let text = "(\n    base: (\n        red: 1.0,\n        green: 0.6784314,\n        blue: 0.0,\n        alpha: 1.0,\n    ),\n    hover: (\n        red: 0.5,\n        green: 0.5,\n        blue: 0.5,\n    ),\n)";
        assert_eq!(parse_cosmic_accent(text), Some((255, 173, 0)));
        assert_eq!(parse_cosmic_accent("nonsense"), None);
    }

    #[test]
    fn custom_themes_override_and_reject_bad_colors() {
        let ok = "accent = \"#102030\"\n[series]\ncpu = [\"#000000\", \"#ffffff\"]";
        let t = Theme::custom("mine", ok, Depth::True).unwrap();
        assert_eq!(t.accent, Color::Rgb(0x10, 0x20, 0x30));
        assert_eq!(t.ramp(Series::Cpu, 100.0), Color::Rgb(255, 255, 255));
        assert!(Theme::custom("x", "accent = \"red\"", Depth::True).is_err());
        assert!(Theme::custom("x", "[series]\ncpu = [\"nope\"]", Depth::True).is_err());
    }

    #[test]
    fn temperatures_use_a_fixed_scale() {
        let t = Theme::builtin("default", Depth::True, &ThemeInputs::default()).unwrap();
        assert_eq!(t.temp(40.0), t.temps[0]);
        assert_eq!(t.temp(60.0), t.temps[1]);
        assert_eq!(t.temp(80.0), t.temps[1]);
        assert_eq!(t.temp(81.0), t.temps[2]);
    }

    #[test]
    fn mono_danger_is_bold_reverse() {
        let t = Theme::builtin("default", Depth::Mono, &ThemeInputs::default()).unwrap();
        let s = t.danger_style();
        assert!(s.add_modifier.contains(Modifier::REVERSED | Modifier::BOLD));
    }
}
