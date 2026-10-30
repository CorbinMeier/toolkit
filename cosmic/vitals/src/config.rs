// SPDX-License-Identifier: MIT
//! `$XDG_CONFIG_HOME/vitals/config.toml` (spec §9): sections, rows, v1 conversion, load with a warning on
//! a bad file, atomic save.

use std::io::Write;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

/// The six sections, in the order of the digit keys and header chips (`number()`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PanelId {
    Cpu,
    Mem,
    Disk,
    Net,
    Tokens,
    Claude,
}

impl PanelId {
    pub const ALL: [PanelId; 6] = [
        Self::Cpu,
        Self::Mem,
        Self::Disk,
        Self::Net,
        Self::Tokens,
        Self::Claude,
    ];

    pub fn index(self) -> usize {
        self as usize
    }

    /// 1 to 6, as on the keyboard.
    pub fn number(self) -> u8 {
        self as u8 + 1
    }

    pub fn from_number(n: u8) -> Option<PanelId> {
        Self::ALL.get(usize::from(n).checked_sub(1)?).copied()
    }

    /// The short name (header chips, snapshot names).
    pub fn name(self) -> &'static str {
        match self {
            Self::Cpu => "cpu",
            Self::Mem => "mem",
            Self::Disk => "disk",
            Self::Net => "net",
            Self::Tokens => "tokens",
            Self::Claude => "claude",
        }
    }

    /// The name used in the config (`[section.memory]`, `sections = ["network"]`).
    pub fn key(self) -> &'static str {
        match self {
            Self::Mem => "memory",
            Self::Net => "network",
            p => p.name(),
        }
    }

    /// The name shown in a section's title and the settings popup.
    pub fn title(self) -> &'static str {
        match self {
            Self::Cpu => "CPU",
            Self::Mem => "Memory",
            Self::Disk => "Disk",
            Self::Net => "Network",
            Self::Tokens => "Tokens",
            Self::Claude => "Claude",
        }
    }

    /// The section a config name stands for (the short names work too).
    pub fn parse_key(s: &str) -> Option<PanelId> {
        Self::ALL
            .into_iter()
            .find(|p| s.eq_ignore_ascii_case(p.key()) || s.eq_ignore_ascii_case(p.name()))
    }

    /// The next section in numeric order, wrapping.
    pub fn step(self, back: bool) -> PanelId {
        let n = Self::ALL.len();
        Self::ALL[(self.index() + if back { n - 1 } else { 1 }) % n]
    }
}

/// v1 panel modes, read only to convert old config files.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Mode {
    Graph,
    Bars,
    Meter,
    Hidden,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum GraphStyle {
    Braille,
    Block,
    Ascii,
}

impl GraphStyle {
    pub fn next(self) -> GraphStyle {
        match self {
            Self::Braille => Self::Block,
            Self::Block => Self::Ascii,
            Self::Ascii => Self::Braille,
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            Self::Braille => "braille",
            Self::Block => "block",
            Self::Ascii => "ascii",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum LayoutMode {
    Auto,
    /// The config's rows, whatever the width (`grid` in v1 files).
    #[serde(alias = "grid")]
    Rows,
    Column,
}

impl LayoutMode {
    pub fn parse(s: &str) -> Option<LayoutMode> {
        match s {
            "auto" => Some(Self::Auto),
            "rows" | "grid" => Some(Self::Rows),
            "column" => Some(Self::Column),
            _ => None,
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            Self::Auto => "auto",
            Self::Rows => "rows",
            Self::Column => "column",
        }
    }

    pub fn next(self) -> LayoutMode {
        match self {
            Self::Auto => Self::Rows,
            Self::Rows => Self::Column,
            Self::Column => Self::Auto,
        }
    }
}

/// Which token series the Tokens timeline shows (`v` cycles).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TokSeries {
    Io,
    Total,
    Cache,
}

impl TokSeries {
    pub fn next(self) -> TokSeries {
        match self {
            Self::Io => Self::Total,
            Self::Total => Self::Cache,
            Self::Cache => Self::Io,
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            Self::Io => "io",
            Self::Total => "total",
            Self::Cache => "cache",
        }
    }
}

/// Refresh intervals the header chip steps through.
pub const INTERVALS_MS: [u64; 5] = [250, 500, 1000, 2000, 5000];
/// System timeline windows in minutes.
pub const WINDOWS_MIN: [u32; 5] = [1, 2, 5, 10, 30];
/// Timeline height limits in rows.
pub const GRAPH_ROWS: std::ops::RangeInclusive<u16> = 1..=8;

/// The entry of `list` nearest to `v`.
fn snap<T: Copy + Into<u64>>(list: &[T], v: u64) -> T {
    *list
        .iter()
        .min_by_key(|&&x| x.into().abs_diff(v))
        .expect("non-empty list")
}

/// Moves one step along `list` from the entry nearest to `v`, stopping at the ends.
pub fn step_in<T: Copy + Into<u64>>(list: &[T], v: u64, up: bool) -> T {
    let at = list.iter().position(|&x| x.into() == snap(list, v).into());
    let i = at.unwrap_or(0);
    let j = if up {
        (i + 1).min(list.len() - 1)
    } else {
        i.saturating_sub(1)
    };
    list[j]
}

/// A part across all sections: on everywhere, off everywhere, or mixed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tri {
    All,
    None,
    Mixed,
}

/// The three parts of a section.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Part {
    Graph,
    Bars,
    Summary,
}

/// `grow` is a weight; a `true`/`false` left by a v2 file means nothing and reads as the default 1.
fn grow_weight<'de, D: serde::Deserializer<'de>>(d: D) -> Result<u16, D::Error> {
    #[derive(Deserialize)]
    #[serde(untagged)]
    enum Grow {
        Weight(u16),
        Legacy(#[allow(dead_code)] bool),
    }
    Ok(match Grow::deserialize(d)? {
        Grow::Weight(w) => w,
        Grow::Legacy(_) => 1,
    })
}

/// The keys every section has.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Common {
    pub enabled: bool,
    /// The timeline (the "braille" toggle in the settings popup).
    pub graph: bool,
    pub bars: bool,
    pub summary: bool,
    pub graph_rows: u16,
    pub style: GraphStyle,
    /// Weight for the spare screen rows, given to this section's timeline (0 keeps it at its minimum).
    /// A boolean from a v2 file is ignored (weight 1).
    #[serde(deserialize_with = "grow_weight")]
    pub grow: u16,
    /// Bars shows only the headline bar(s): what a v1 `meter` panel becomes.
    pub headline_only: bool,
}

impl Default for Common {
    fn default() -> Self {
        Common {
            enabled: true,
            graph: true,
            bars: true,
            summary: true,
            graph_rows: 2,
            style: GraphStyle::Braille,
            grow: 1,
            headline_only: false,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct CpuConf {
    #[serde(flatten)]
    pub c: Common,
    pub window_min: u32,
    pub per_thread: bool,
}

impl Default for CpuConf {
    fn default() -> Self {
        CpuConf {
            c: Common::default(),
            window_min: 2,
            per_thread: true,
        }
    }
}

/// Memory and disk: the common keys and the time window.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct WindowConf {
    #[serde(flatten)]
    pub c: Common,
    pub window_min: u32,
}

impl Default for WindowConf {
    fn default() -> Self {
        WindowConf {
            c: Common::default(),
            window_min: 2,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct NetConf {
    #[serde(flatten)]
    pub c: Common,
    pub window_min: u32,
    /// One bar per interface under `all` (false: the `all` bar only).
    pub per_interface: bool,
    /// Interfaces that count toward `all` although they are not physical (spec §3).
    pub include: Vec<String>,
    /// Interfaces that do not count toward `all` although they are physical.
    pub exclude: Vec<String>,
}

impl NetConf {
    /// Turns one interface's inclusion on or off, given what the default rule says about it. Keeps the
    /// lists to explicit overrides only.
    pub fn set_included(&mut self, name: &str, on: bool, physical: bool) {
        self.include.retain(|n| n != name);
        self.exclude.retain(|n| n != name);
        if on && !physical {
            self.include.push(name.to_owned());
        } else if !on && physical {
            self.exclude.push(name.to_owned());
        }
    }
}

impl Default for NetConf {
    fn default() -> Self {
        NetConf {
            c: Common::default(),
            window_min: 2,
            per_interface: true,
            include: Vec::new(),
            exclude: Vec::new(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct TokensConf {
    #[serde(flatten)]
    pub c: Common,
    /// `1h`, `6h`, `24h`, `7d` or `30d`.
    pub window: String,
    pub series: TokSeries,
}

impl Default for TokensConf {
    fn default() -> Self {
        TokensConf {
            c: Common::default(),
            window: "24h".into(),
            series: TokSeries::Io,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct ClaudeConf {
    #[serde(flatten)]
    pub c: Common,
    pub usage_url: String,
}

impl Default for ClaudeConf {
    fn default() -> Self {
        ClaudeConf {
            c: Common {
                graph: false,
                ..Common::default()
            },
            usage_url: "http://127.0.0.1:4317".into(),
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Sections {
    pub cpu: CpuConf,
    pub memory: WindowConf,
    pub disk: WindowConf,
    pub network: NetConf,
    pub tokens: TokensConf,
    pub claude: ClaudeConf,
}

/// One `[[row]]`: section names left to right and their relative widths.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Row {
    pub sections: Vec<String>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub widths: Vec<u16>,
}

fn row(names: &[&str], widths: &[u16]) -> Row {
    Row {
        sections: names.iter().map(|s| (*s).to_owned()).collect(),
        widths: widths.to_vec(),
    }
}

pub fn default_rows() -> Vec<Row> {
    vec![
        row(&["cpu", "memory"], &[1, 1]),
        row(&["disk", "network"], &[1, 1]),
        row(&["tokens"], &[]),
        row(&["claude"], &[]),
    ]
}

/// One v1 `[panel.*]` table (only the keys that still mean something).
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct LegacyPanel {
    pub mode: Option<Mode>,
    pub style: Option<GraphStyle>,
    pub window_min: Option<u32>,
    pub per_thread: Option<bool>,
    pub interface: Option<String>,
    pub window: Option<String>,
    pub series: Option<TokSeries>,
    pub usage_url: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct LegacyPanels {
    pub cpu: LegacyPanel,
    pub memory: LegacyPanel,
    pub disk: LegacyPanel,
    pub network: LegacyPanel,
    pub claude: LegacyPanel,
    pub tokens: LegacyPanel,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Config {
    pub interval_ms: u64,
    pub theme: String,
    pub mouse: bool,
    pub layout: LayoutMode,
    /// `#rrggbb` for the `mono` theme.
    pub accent: Option<String>,
    pub row: Vec<Row>,
    pub section: Sections,
    /// `[web]`: the dashboard served by `vitals --serve` (spec §14).
    pub web: crate::web::conf::WebConf,
    /// A v1 file's panels: converted into `section` on load, never written.
    #[serde(skip_serializing)]
    pub panel: Option<LegacyPanels>,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            interval_ms: 1000,
            theme: "default".into(),
            mouse: true,
            layout: LayoutMode::Auto,
            accent: None,
            row: default_rows(),
            section: Sections::default(),
            web: Default::default(),
            panel: None,
        }
    }
}

/// Sections in one row of the resolved layout.
#[derive(Debug, Clone, Copy)]
pub struct RowSpec {
    pub items: [(PanelId, u16); 6],
    pub n: usize,
}

impl RowSpec {
    pub fn items(&self) -> &[(PanelId, u16)] {
        &self.items[..self.n]
    }
}

/// The rows with unknown, repeated and (optionally) disabled sections removed; fixed size, no heap.
#[derive(Debug, Clone, Copy)]
pub struct Resolved {
    pub rows: [RowSpec; 6],
    pub n: usize,
}

impl Resolved {
    pub fn rows(&self) -> &[RowSpec] {
        &self.rows[..self.n]
    }

    /// The sections in reading order (row by row, left to right).
    pub fn flat(&self) -> impl Iterator<Item = PanelId> + '_ {
        self.rows()
            .iter()
            .flat_map(|r| r.items().iter().map(|i| i.0))
    }
}

impl Config {
    /// Parses a config file's text. A v1 file is converted; a `[section.claude]` without `graph` keeps the
    /// Claude default (timeline off).
    pub fn from_toml(text: &str) -> Result<Config, String> {
        let mut table: toml::Table = toml::from_str(text).map_err(|e| e.message().to_owned())?;
        if let Some(toml::Value::Table(sections)) = table.get_mut("section")
            && let Some(toml::Value::Table(claude)) = sections.get_mut("claude")
        {
            claude.entry("graph").or_insert(toml::Value::Boolean(false));
        }
        let mut c: Config = table
            .try_into()
            .map_err(|e: toml::de::Error| e.message().to_owned())?;
        c.convert_v1();
        c.sanitize();
        Ok(c)
    }

    /// Turns a v1 `[panel.*]` section into the v2 keys: graph = all parts, bars = no timeline,
    /// meter = bars with the headline bar only, hidden = disabled.
    fn convert_v1(&mut self) {
        let Some(l) = self.panel.take() else { return };
        let s = &mut self.section;
        let conv = |c: &mut Common, p: &LegacyPanel, default: Mode| {
            if let Some(st) = p.style {
                c.style = st;
            }
            let mode = p.mode.unwrap_or(default);
            *c = Common {
                enabled: mode != Mode::Hidden,
                graph: mode == Mode::Graph || (mode == Mode::Hidden && c.graph),
                bars: true,
                summary: mode != Mode::Meter,
                headline_only: mode == Mode::Meter,
                ..c.clone()
            };
        };
        conv(&mut s.cpu.c, &l.cpu, Mode::Graph);
        conv(&mut s.memory.c, &l.memory, Mode::Graph);
        conv(&mut s.disk.c, &l.disk, Mode::Graph);
        conv(&mut s.network.c, &l.network, Mode::Graph);
        conv(&mut s.tokens.c, &l.tokens, Mode::Graph);
        // v1's own default for the Claude row was `meter`: that is the v2 default, not a headline-only choice.
        conv(&mut s.claude.c, &l.claude, Mode::Graph);
        if l.claude.mode.is_none() || l.claude.mode == Some(Mode::Meter) {
            s.claude.c = Common {
                style: s.claude.c.style,
                ..ClaudeConf::default().c
            };
        }
        if let Some(v) = l.cpu.window_min {
            s.cpu.window_min = v;
        }
        if let Some(v) = l.cpu.per_thread {
            s.cpu.per_thread = v;
        }
        if let Some(v) = l.memory.window_min {
            s.memory.window_min = v;
        }
        if let Some(v) = l.disk.window_min {
            s.disk.window_min = v;
        }
        if let Some(v) = l.network.window_min {
            s.network.window_min = v;
        }
        if let Some(v) = l.tokens.window {
            s.tokens.window = v;
        }
        if let Some(v) = l.tokens.series {
            s.tokens.series = v;
        }
        if let Some(v) = l.claude.usage_url {
            s.claude.usage_url = v;
        }
    }

    /// Clamps hand-edited numbers to the values the UI steps through.
    pub fn sanitize(&mut self) {
        self.interval_ms = snap(&INTERVALS_MS, self.interval_ms);
        let s = &mut self.section;
        for w in [
            &mut s.cpu.window_min,
            &mut s.memory.window_min,
            &mut s.disk.window_min,
            &mut s.network.window_min,
        ] {
            *w = snap(&WINDOWS_MIN, u64::from(*w));
        }
        for p in PanelId::ALL {
            let c = self.common_mut(p);
            c.graph_rows = c.graph_rows.clamp(*GRAPH_ROWS.start(), *GRAPH_ROWS.end());
        }
        if crate::tokens::TokenWindow::from_label(&self.section.tokens.window).is_none() {
            self.section.tokens.window = "24h".into();
        }
    }

    pub fn common(&self, p: PanelId) -> &Common {
        let s = &self.section;
        match p {
            PanelId::Cpu => &s.cpu.c,
            PanelId::Mem => &s.memory.c,
            PanelId::Disk => &s.disk.c,
            PanelId::Net => &s.network.c,
            PanelId::Tokens => &s.tokens.c,
            PanelId::Claude => &s.claude.c,
        }
    }

    pub fn common_mut(&mut self, p: PanelId) -> &mut Common {
        let s = &mut self.section;
        match p {
            PanelId::Cpu => &mut s.cpu.c,
            PanelId::Mem => &mut s.memory.c,
            PanelId::Disk => &mut s.disk.c,
            PanelId::Net => &mut s.network.c,
            PanelId::Tokens => &mut s.tokens.c,
            PanelId::Claude => &mut s.claude.c,
        }
    }

    pub fn style(&self, p: PanelId) -> GraphStyle {
        self.common(p).style
    }

    pub fn enabled(&self, p: PanelId) -> bool {
        self.common(p).enabled
    }

    pub fn part(&self, p: PanelId, part: Part) -> bool {
        let c = self.common(p);
        match part {
            Part::Graph => c.graph,
            Part::Bars => c.bars,
            Part::Summary => c.summary,
        }
    }

    pub fn toggle_part(&mut self, p: PanelId, part: Part) {
        let c = self.common_mut(p);
        let f = match part {
            Part::Graph => &mut c.graph,
            Part::Bars => &mut c.bars,
            Part::Summary => &mut c.summary,
        };
        *f = !*f;
    }

    /// Whether a part (`None`: the section itself) is on in all, none or only some of the six sections.
    pub fn tri(&self, part: Option<Part>) -> Tri {
        let on = |p: PanelId| match part {
            Some(x) => self.part(p, x),
            None => self.enabled(p),
        };
        let n = PanelId::ALL.iter().filter(|&&p| on(p)).count();
        match n {
            0 => Tri::None,
            6 => Tri::All,
            _ => Tri::Mixed,
        }
    }

    /// Sets a part (`None`: enabled) of every section: off if it is on everywhere, else on everywhere.
    pub fn toggle_all(&mut self, part: Option<Part>) {
        let to = self.tri(part) != Tri::All;
        for p in PanelId::ALL {
            match part {
                Some(Part::Graph) => self.common_mut(p).graph = to,
                Some(Part::Bars) => self.common_mut(p).bars = to,
                Some(Part::Summary) => self.common_mut(p).summary = to,
                None => self.set_enabled(p, to),
            }
        }
    }

    /// Turns a section on or off. Turning one on that no row names gives it a row of its own.
    pub fn set_enabled(&mut self, p: PanelId, on: bool) {
        self.common_mut(p).enabled = on;
        if on && !self.resolve(false).flat().any(|q| q == p) {
            self.row.push(row(&[p.key()], &[]));
        }
    }

    /// The system timeline window in minutes (Claude and Tokens have their own).
    pub fn window_min(&self, p: PanelId) -> Option<u32> {
        let s = &self.section;
        match p {
            PanelId::Cpu => Some(s.cpu.window_min),
            PanelId::Mem => Some(s.memory.window_min),
            PanelId::Disk => Some(s.disk.window_min),
            PanelId::Net => Some(s.network.window_min),
            _ => None,
        }
    }

    pub fn set_window_min(&mut self, p: PanelId, m: u32) {
        let s = &mut self.section;
        match p {
            PanelId::Cpu => s.cpu.window_min = m,
            PanelId::Mem => s.memory.window_min = m,
            PanelId::Disk => s.disk.window_min = m,
            PanelId::Net => s.network.window_min = m,
            _ => {}
        }
    }

    pub fn usage_url(&self) -> &str {
        &self.section.claude.usage_url
    }

    /// The rows as drawn: first occurrence of each known section, disabled ones left out when
    /// `only_enabled`, rows left with nothing dropped.
    pub fn resolve(&self, only_enabled: bool) -> Resolved {
        let empty = RowSpec {
            items: [(PanelId::Cpu, 1); 6],
            n: 0,
        };
        let mut out = Resolved {
            rows: [empty; 6],
            n: 0,
        };
        let mut seen = [false; 6];
        for r in &self.row {
            let mut spec = empty;
            for (i, name) in r.sections.iter().enumerate() {
                let Some(p) = PanelId::parse_key(name) else {
                    continue;
                };
                if std::mem::replace(&mut seen[p.index()], true) {
                    continue;
                }
                if only_enabled && !self.enabled(p) {
                    continue;
                }
                let w = r.widths.get(i).copied().filter(|&w| w > 0).unwrap_or(1);
                spec.items[spec.n] = (p, w);
                spec.n += 1;
            }
            if spec.n > 0 && out.n < 6 {
                out.rows[out.n] = spec;
                out.n += 1;
            }
        }
        out
    }

    /// Sections the layout shows, in reading order.
    pub fn visible(&self) -> impl Iterator<Item = PanelId> + '_ {
        let r = self.resolve(true);
        (0..r.n).flat_map(move |i| (0..r.rows[i].n).map(move |j| r.rows[i].items[j].0))
    }

    pub fn is_visible(&self, p: PanelId) -> bool {
        self.enabled(p) && self.visible().any(|q| q == p)
    }

    /// Moves a section one place earlier (`-1`) or later (`1`) among the enabled sections in reading
    /// order. Rows keep their shapes: the sections flow through the slots.
    pub fn move_section(&mut self, p: PanelId, delta: i8) -> bool {
        let mut slots: Vec<(usize, usize, PanelId)> = Vec::new();
        let mut seen = [false; 6];
        for (ri, r) in self.row.iter().enumerate() {
            for (ci, name) in r.sections.iter().enumerate() {
                if let Some(q) = PanelId::parse_key(name)
                    && !std::mem::replace(&mut seen[q.index()], true)
                    && self.enabled(q)
                {
                    slots.push((ri, ci, q));
                }
            }
        }
        let Some(k) = slots.iter().position(|s| s.2 == p) else {
            return false;
        };
        let j = k as i64 + i64::from(delta);
        if j < 0 || j as usize >= slots.len() {
            return false;
        }
        let (a, b) = (slots[k], slots[j as usize]);
        let (na, nb) = (b.2.key().to_owned(), a.2.key().to_owned());
        self.row[a.0].sections[a.1] = na;
        self.row[b.0].sections[b.1] = nb;
        true
    }
}

/// A warning for names in the rows that are unknown or listed twice (loaded configs only).
pub fn row_warnings(rows: &[Row]) -> Option<String> {
    let mut unknown: Vec<&str> = Vec::new();
    let mut dup: Vec<&str> = Vec::new();
    let mut seen = [false; 6];
    for name in rows.iter().flat_map(|r| &r.sections) {
        match PanelId::parse_key(name) {
            None => unknown.push(name),
            Some(p) => {
                if std::mem::replace(&mut seen[p.index()], true) {
                    dup.push(name);
                }
            }
        }
    }
    let mut parts = Vec::new();
    if !unknown.is_empty() {
        parts.push(format!("unknown section {}", unknown.join(", ")));
    }
    if !dup.is_empty() {
        parts.push(format!("section listed twice: {}", dup.join(", ")));
    }
    (!parts.is_empty()).then(|| format!("config rows: {}", parts.join("; ")))
}

/// `$XDG_CONFIG_HOME/vitals` (else `$HOME/.config/vitals`), from the given environment lookup.
pub fn config_dir(get: &dyn Fn(&str) -> Option<String>) -> Option<PathBuf> {
    let non_empty = |k: &str| get(k).filter(|v| !v.is_empty());
    let base = match non_empty("XDG_CONFIG_HOME") {
        Some(x) => PathBuf::from(x),
        None => PathBuf::from(non_empty("HOME")?).join(".config"),
    };
    Some(base.join("vitals"))
}

/// Reads the config. A missing file is the defaults; a bad one is the defaults plus a one-line warning.
pub fn load(path: &Path) -> (Config, Option<String>) {
    let text = match std::fs::read_to_string(path) {
        Ok(t) => t,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return (Config::default(), None),
        Err(e) => return (Config::default(), Some(format!("config unreadable: {e}"))),
    };
    match Config::from_toml(&text) {
        Ok(c) => {
            let warn = row_warnings(&c.row);
            (c, warn)
        }
        Err(e) => {
            let first = e.lines().next().unwrap_or("parse error");
            (
                Config::default(),
                Some(format!("config.toml ignored: {first}")),
            )
        }
    }
}

/// The TUI's save: like [`save`], but `[web]` is taken from the file on disk, because only
/// `vitals --serve` writes it (the key) and a running TUI's copy may be older.
pub fn save_keeping_web(path: &Path, cfg: &Config) -> std::io::Result<()> {
    let mut out = cfg.clone();
    if let Some(disk) = std::fs::read_to_string(path)
        .ok()
        .and_then(|t| Config::from_toml(&t).ok())
    {
        out.web = disk.web;
    }
    save(path, &out)
}

/// Writes the config atomically: a temp file next to it, then a rename.
pub fn save(path: &Path, cfg: &Config) -> std::io::Result<()> {
    let dir = path.parent().unwrap_or(Path::new("."));
    std::fs::create_dir_all(dir)?;
    let text = toml::to_string(cfg).map_err(std::io::Error::other)?;
    let tmp = dir.join(format!("config.toml.tmp{}", std::process::id()));
    let mut f = std::fs::File::create(&tmp)?;
    f.write_all(text.as_bytes())?;
    f.sync_all()?;
    drop(f);
    std::fs::rename(&tmp, path).inspect_err(|_| {
        let _ = std::fs::remove_file(&tmp);
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips_through_toml() {
        let mut c = Config {
            theme: "ramp".into(),
            accent: Some("#7aa2f7".into()),
            ..Config::default()
        };
        c.section.disk.c.graph = false;
        c.section.cpu.c.graph_rows = 5;
        c.section.network.include = vec!["tailscale0".into()];
        c.section.network.exclude = vec!["enx0".into()];
        c.section.network.per_interface = false;
        c.section.tokens.series = TokSeries::Cache;
        c.section.tokens.window = "7d".into();
        c.layout = LayoutMode::Rows;
        let text = toml::to_string(&c).unwrap();
        assert!(
            text.contains("[[row]]") && text.contains("[section.cpu]"),
            "{text}"
        );
        assert_eq!(Config::from_toml(&text).unwrap(), c);
    }

    #[test]
    fn partial_files_fill_in_defaults() {
        let c = Config::from_toml("interval_ms = 500\n[section.cpu]\nbars = false").unwrap();
        assert_eq!(c.interval_ms, 500);
        assert!(!c.section.cpu.c.bars && c.section.cpu.c.graph);
        assert_eq!(c.section.cpu.window_min, 2);
        assert!(
            !c.section.claude.c.graph,
            "Claude's timeline stays off by default"
        );
        let c = Config::from_toml("[section.claude]\nsummary = false").unwrap();
        assert!(!c.section.claude.c.graph && !c.section.claude.c.summary);
        assert_eq!(c.row, default_rows());
    }

    #[test]
    fn numbers_snap_to_the_steps() {
        let c =
            Config::from_toml("interval_ms = 900\n[section.cpu]\nwindow_min = 7\ngraph_rows = 40")
                .unwrap();
        assert_eq!(c.interval_ms, 1000);
        assert_eq!(c.section.cpu.window_min, 5);
        assert_eq!(c.section.cpu.c.graph_rows, 8);
        assert_eq!(step_in(&INTERVALS_MS, 1000, true), 2000);
        assert_eq!(step_in(&INTERVALS_MS, 5000, true), 5000);
        assert_eq!(step_in(&WINDOWS_MIN, 1, false), 1);
    }

    #[test]
    fn grow_is_a_weight_and_v2_booleans_are_ignored() {
        let c = Config::from_toml("[section.cpu]\ngrow = false\n[section.disk]\ngrow = 0\n[section.network]\ngrow = 3\n[section.tokens]\ngrow = true\n").unwrap();
        let g = |p| c.common(p).grow;
        assert_eq!(
            [
                g(PanelId::Cpu),
                g(PanelId::Disk),
                g(PanelId::Net),
                g(PanelId::Tokens),
                g(PanelId::Mem)
            ],
            [1, 0, 3, 1, 1]
        );
    }

    #[test]
    fn global_toggles_have_a_mixed_state() {
        let mut c = Config::default();
        assert_eq!(c.tri(Some(Part::Bars)), Tri::All);
        assert_eq!(
            c.tri(Some(Part::Graph)),
            Tri::Mixed,
            "Claude's timeline is off by default"
        );
        c.toggle_all(Some(Part::Graph));
        assert_eq!(
            c.tri(Some(Part::Graph)),
            Tri::All,
            "mixed turns on everywhere"
        );
        c.toggle_all(Some(Part::Graph));
        assert_eq!(c.tri(Some(Part::Graph)), Tri::None);
        c.toggle_all(None);
        assert_eq!(c.tri(None), Tri::None);
        c.toggle_all(None);
        assert_eq!(c.tri(None), Tri::All);
    }

    #[test]
    fn old_network_interface_key_is_ignored_and_overrides_round_trip() {
        let c = Config::from_toml("[section.network]\ninterface = \"all\"\nper_interface = false\ninclude = [\"tailscale0\"]\nexclude = [\"enx0\"]\n").unwrap();
        let n = &c.section.network;
        assert!(!n.per_interface);
        assert_eq!(
            (&n.include[..], &n.exclude[..]),
            (&["tailscale0".to_owned()][..], &["enx0".to_owned()][..])
        );
        let d = Config::from_toml("[section.network]\ninterface = \"wlo1\"\n").unwrap();
        assert!(d.section.network.per_interface && d.section.network.include.is_empty());
        let again = Config::from_toml(&toml::to_string(&c).unwrap()).unwrap();
        assert_eq!(again.section.network, c.section.network);
        assert!(!toml::to_string(&c).unwrap().contains("\ninterface = "));
    }

    #[test]
    fn set_included_keeps_only_explicit_overrides() {
        let mut n = NetConf::default();
        n.set_included("tailscale0", true, false);
        n.set_included("enx0", false, true);
        assert_eq!(
            (n.include.as_slice(), n.exclude.as_slice()),
            (&["tailscale0".to_owned()][..], &["enx0".to_owned()][..])
        );
        // Back to what the default rule says: the override disappears.
        n.set_included("tailscale0", false, false);
        n.set_included("enx0", true, true);
        assert!(n.include.is_empty() && n.exclude.is_empty());
    }

    #[test]
    fn v1_panels_are_converted() {
        let c = Config::from_toml(
            "layout = \"grid\"\n[panel.cpu]\nmode = \"bars\"\nstyle = \"block\"\nwindow_min = 5\n\
             [panel.memory]\nmode = \"meter\"\n[panel.disk]\nmode = \"hidden\"\n\
             [panel.network]\nmode = \"graph\"\ninterface = \"eth0\"\n\
             [panel.claude]\nmode = \"meter\"\n[panel.tokens]\nmode = \"graph\"\nwindow = \"6h\"\n",
        )
        .unwrap();
        assert_eq!(c.layout, LayoutMode::Rows);
        let cpu = &c.section.cpu;
        assert!(cpu.c.enabled && !cpu.c.graph && cpu.c.bars && cpu.c.summary);
        assert_eq!((cpu.c.style, cpu.window_min), (GraphStyle::Block, 5));
        let mem = &c.section.memory.c;
        assert!(!mem.graph && mem.bars && !mem.summary && mem.headline_only);
        assert!(!c.section.disk.c.enabled);
        let net = &c.section.network;
        assert!(net.c.graph && net.c.bars && net.c.summary && net.per_interface);
        assert_eq!(c.section.claude.c, ClaudeConf::default().c);
        assert_eq!(c.section.tokens.window, "6h");
        assert!(c.panel.is_none());
        let saved = toml::to_string(&c).unwrap();
        assert!(!saved.contains("[panel"), "{saved}");
    }

    #[test]
    fn rows_resolve_in_reading_order_and_warn() {
        let mut c = Config {
            row: vec![
                row(&["cpu", "bogus", "mem"], &[3, 1, 1]),
                row(&["cpu", "network"], &[]),
            ],
            ..Config::default()
        };
        let r = c.resolve(true);
        assert_eq!(r.n, 2);
        assert_eq!(r.rows()[0].items(), &[(PanelId::Cpu, 3), (PanelId::Mem, 1)]);
        assert_eq!(r.rows()[1].items(), &[(PanelId::Net, 1)]);
        let w = row_warnings(&c.row).unwrap();
        assert!(
            w.contains("unknown section bogus") && w.contains("listed twice: cpu"),
            "{w}"
        );
        assert!(row_warnings(&default_rows()).is_none());
        c.section.network.c.enabled = false;
        assert_eq!(c.resolve(true).n, 1);
        assert_eq!(c.resolve(false).n, 2);
    }

    #[test]
    fn sections_move_through_the_row_shapes() {
        let mut c = Config::default();
        assert!(c.move_section(PanelId::Disk, -1));
        let names: Vec<_> = c.resolve(true).flat().collect();
        assert_eq!(
            names,
            [
                PanelId::Cpu,
                PanelId::Disk,
                PanelId::Mem,
                PanelId::Net,
                PanelId::Tokens,
                PanelId::Claude
            ]
        );
        assert_eq!(c.row[0].sections, ["cpu", "disk"]);
        assert!(
            !c.move_section(PanelId::Cpu, -1),
            "the first cannot go earlier"
        );
        // A disabled neighbour is skipped.
        c.section.memory.c.enabled = false;
        assert!(c.move_section(PanelId::Net, -1));
        assert_eq!(c.row[0].sections, ["cpu", "network"]);
        assert_eq!(c.row[1].sections, ["memory", "disk"].map(String::from));
    }

    #[test]
    fn enabling_a_section_no_row_names_adds_a_row() {
        let mut c = Config::default();
        c.row.retain(|r| r.sections != ["claude"]);
        c.set_enabled(PanelId::Claude, false);
        c.set_enabled(PanelId::Claude, true);
        assert_eq!(c.row.last().unwrap().sections, ["claude"]);
        assert!(c.is_visible(PanelId::Claude));
    }
}
