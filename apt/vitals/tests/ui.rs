// SPDX-License-Identifier: MIT
//! UI snapshots: every section with its parts on and off at 40x12, 80x24 and 200x50, graph rows 1/2/8,
//! the Claude section against the usage server fixtures, the popups and whole screens. Headless: ratatui buffers
//! only, UTC, fixed clock. Accept new snapshots once with `INSTA_UPDATE=always cargo test --test ui`.

mod common;

use common::ui::{NOW, panel_text, rig, screen_text, server_fixture, set_server};
use vitals::app::Overlay;
use vitals::config::{GraphStyle, PanelId, Part};
use vitals::input::Action;
use vitals::theme::Depth;

const SIZES: [(u16, u16); 3] = [(40, 12), (80, 24), (200, 50)];
const STYLES: [GraphStyle; 3] = [GraphStyle::Braille, GraphStyle::Block, GraphStyle::Ascii];

/// Parts on and off, and the timeline height.
const VARIANTS: [(&str, bool, bool, bool, u16); 7] = [
    ("all_r1", true, true, true, 1),
    ("all_r2", true, true, true, 2),
    ("all_r8", true, true, true, 8),
    ("no_timeline", false, true, true, 2),
    ("no_bars", true, false, true, 2),
    ("no_summary", true, true, false, 2),
    ("timeline_only", true, false, false, 2),
];

#[test]
fn every_section_with_its_parts_on_and_off_at_every_size() {
    for id in PanelId::ALL {
        for (name, graph, bars, summary, rows) in VARIANTS {
            for (w, h) in SIZES {
                let mut r = rig(Depth::True, |c| {
                    let s = c.common_mut(id);
                    (s.graph, s.bars, s.summary, s.graph_rows) = (graph, bars, summary, rows);
                });
                let text = panel_text(&mut r.app, id, w, h);
                insta::assert_snapshot!(format!("{}_{name}_{w}x{h}", id.name()), text);
            }
        }
    }
}

#[test]
fn every_section_in_every_graph_style() {
    for id in PanelId::ALL {
        for style in [GraphStyle::Block, GraphStyle::Ascii] {
            let mut r = rig(Depth::True, |c| {
                let s = c.common_mut(id);
                s.style = style;
                s.graph = true;
                s.graph_rows = 4;
            });
            let text = panel_text(&mut r.app, id, 80, 24);
            insta::assert_snapshot!(format!("{}_style_{}", id.name(), style.name()), text);
        }
    }
    assert_eq!(STYLES.len(), 3);
}

#[test]
fn network_and_memory_sections_in_v5_form() {
    let mut r = rig(Depth::True, |_| {});
    insta::assert_snapshot!(
        "network_split_bars",
        panel_text(&mut r.app, PanelId::Net, 100, 20)
    );
    insta::assert_snapshot!(
        "network_split_bars_wide",
        panel_text(&mut r.app, PanelId::Net, 200, 24)
    );
    insta::assert_snapshot!(
        "network_split_bars_narrow",
        panel_text(&mut r.app, PanelId::Net, 40, 12)
    );
    // Short: the `all` row stays, the interface rows go.
    insta::assert_snapshot!(
        "network_short",
        panel_text(&mut r.app, PanelId::Net, 100, 6)
    );
    // Aggregate only: `per_interface = false`.
    r.app.apply(Action::TogglePerIface);
    insta::assert_snapshot!(
        "network_all_only",
        panel_text(&mut r.app, PanelId::Net, 100, 20)
    );
    insta::assert_snapshot!("memory_v4", panel_text(&mut r.app, PanelId::Mem, 100, 10));
}

/// A network sampler with known rates: a burst, then about half of it, so the split bars sit at
/// different fill levels (wlo1 ~48% down and ~49% up, the adapter 50%, a bridge 100% down, one veth idle).
fn fake_net(dir: &common::TempDir) -> vitals::sample::net::NetSampler {
    use std::time::{Duration, Instant};
    let sys = dir.join("sys");
    let line =
        |name: &str, rx: u64, tx: u64| format!("{name}: {rx} 1 0 0 0 0 0 0 {tx} 1 0 0 0 0 0 0\n");
    let dev = |k: u64| {
        let mut t = String::from("Inter-|\n face |\n");
        // k = 0: start; 1: the burst; 2: about half of it.
        let (w_rx, w_tx, e_rx, e_tx) = match k {
            0 => (0, 0, 0, 0),
            1 => (2_400_000, 600_000, 160_000, 40_000),
            _ => (3_553_434, 896_960, 240_000, 60_000),
        };
        t += &line("lo", 0, 0);
        t += &line("wlo1", w_rx, w_tx);
        t += &line("enx0", e_rx, e_tx);
        t += &line("docker0", k * 50_000, 0);
        t += &line("veth1", 0, 0);
        t
    };
    for n in ["lo", "docker0", "veth1"] {
        common::write(&sys.join(format!("class/net/{n}/.keep")), "");
    }
    for n in ["wlo1", "enx0"] {
        common::write(&sys.join(format!("class/net/{n}/device/.keep")), "");
    }
    let root = vitals::sample::Root::new(dir.join("proc"), sys);
    common::write(&dir.join("proc/net/dev"), dev(0));
    let mut net = vitals::sample::net::NetSampler::new(&root).unwrap();
    let t0 = Instant::now();
    net.sample(t0).unwrap();
    common::write(&dir.join("proc/net/dev"), dev(1));
    net.sample(t0 + Duration::from_secs(1)).unwrap();
    common::write(&dir.join("proc/net/dev"), dev(2));
    net.sample(t0 + Duration::from_secs(2)).unwrap();
    net
}

#[test]
fn network_split_bars_at_known_fill_levels() {
    let mut r = rig(Depth::True, |_| {});
    let dir = common::TempDir::new("fakenet");
    r.app.data.sys.net = fake_net(&dir);
    r.app.data.sys.net.set_overrides(&[], &[]);
    let text = panel_text(&mut r.app, PanelId::Net, 100, 12);
    insta::assert_snapshot!("network_known_rates", text);
    // `all` is wlo1 + enx0 (physical): about 1.2M down, 310K up.
    assert!(text.contains("all ") && text.contains("1.2M/s"), "{text}");
    insta::assert_snapshot!(
        "network_known_rates_40x12",
        panel_text(&mut r.app, PanelId::Net, 40, 12)
    );
    // Excluding the adapter: its checkbox empties and `all` shrinks to wlo1.
    r.app.apply(Action::ToggleIface(2));
    let text = panel_text(&mut r.app, PanelId::Net, 100, 12);
    assert!(text.contains("[ ]enx0") && text.contains("4 off"), "{text}");
    insta::assert_snapshot!("network_known_rates_enx0_off", text);
}

#[test]
fn token_series_and_windows() {
    use vitals::config::TokSeries;
    for series in [TokSeries::Total, TokSeries::Cache] {
        let mut r = rig(Depth::True, |c| {
            c.section.tokens.series = series;
            c.section.tokens.c.graph_rows = 4;
        });
        let text = panel_text(&mut r.app, PanelId::Tokens, 120, 20);
        insta::assert_snapshot!(format!("tokens_series_{}", series.name()), text);
    }
    for window in ["1h", "7d", "30d"] {
        let mut r = rig(Depth::True, |c| {
            c.section.tokens.window = window.into();
            c.section.tokens.c.graph_rows = 4;
        });
        let text = panel_text(&mut r.app, PanelId::Tokens, 120, 20);
        insta::assert_snapshot!(format!("tokens_window_{window}"), text);
    }
}

#[test]
fn the_claude_section_against_each_server_fixture() {
    for fixture in ["fresh", "stale", "missing_week", "hot"] {
        for (w, h) in SIZES {
            let mut r = rig(Depth::True, |_| {});
            set_server(&mut r.app, fixture);
            let text = panel_text(&mut r.app, PanelId::Claude, w, h);
            insta::assert_snapshot!(format!("claude_{fixture}_{w}x{h}"), text);
        }
    }
}

#[test]
fn the_claude_section_with_no_reading() {
    let mut r = rig(Depth::True, |_| {});
    r.app.data.claude = Default::default();
    r.app.data.claude.refresh(NOW);
    let text = panel_text(&mut r.app, PanelId::Claude, 80, 8);
    insta::assert_snapshot!("claude_none", text);
}

#[test]
fn the_claude_popup() {
    let mut r = rig(Depth::True, |_| {});
    r.app.ui.overlay = Overlay::Claude;
    insta::assert_snapshot!("popup_claude", screen_text(&mut r.app, 100, 30));
    set_server(&mut r.app, "stale");
    insta::assert_snapshot!("popup_claude_stale", screen_text(&mut r.app, 100, 30));
}

#[test]
fn the_help_popup_tabs() {
    let mut r = rig(Depth::True, |_| {});
    r.app.apply(Action::Help);
    insta::assert_snapshot!("help_cpu", screen_text(&mut r.app, 100, 30));
    r.app.apply(Action::HelpTab(3));
    insta::assert_snapshot!("help_network", screen_text(&mut r.app, 100, 30));
    r.app.apply(Action::HelpTab(6));
    insta::assert_snapshot!("help_controls", screen_text(&mut r.app, 100, 30));
    // Scrolled: the second page of a long tab, and a narrow screen with wrapped tabs.
    r.app.apply(Action::HelpTab(5));
    screen_text(&mut r.app, 100, 16);
    r.app.apply(Action::HelpScroll(8));
    insta::assert_snapshot!("help_claude_scrolled", screen_text(&mut r.app, 100, 16));
    insta::assert_snapshot!("help_narrow", screen_text(&mut r.app, 44, 20));
}

#[test]
fn the_global_toggles_in_the_header_and_settings() {
    let mut r = rig(Depth::True, |_| {});
    r.app.apply(Action::ToggleAll(Some(Part::Graph)));
    r.app.apply(Action::ToggleAll(Some(Part::Graph)));
    insta::assert_snapshot!("screen_all_graphs_off", screen_text(&mut r.app, 100, 30));
    let mut r = rig(Depth::True, |_| {});
    r.app.apply(Action::Settings);
    insta::assert_snapshot!("settings_all_row_mixed", screen_text(&mut r.app, 100, 30));
    insta::assert_snapshot!("settings_all_row_narrow", screen_text(&mut r.app, 60, 30));
}

#[test]
fn the_settings_popup() {
    let mut r = rig(Depth::True, |_| {});
    r.app.apply(Action::Settings);
    insta::assert_snapshot!("settings_wide", screen_text(&mut r.app, 100, 30));
    insta::assert_snapshot!("settings_narrow", screen_text(&mut r.app, 60, 30));
    // Short screen: the popup scrolls with the cursor.
    r.app.apply(Action::SettingsNav(0, 0));
    r.app.apply(Action::SettingsNav(0, 6));
    insta::assert_snapshot!(
        "settings_short_cursor_last_row",
        screen_text(&mut r.app, 60, 14)
    );
    // Changes show at once: Disk off, Network timeline off, Memory moved up.
    let mut r = rig(Depth::True, |_| {});
    r.app.apply(Action::Settings);
    r.app.apply(Action::TogglePanel(PanelId::Disk));
    r.app.apply(Action::TogglePart(PanelId::Net, Part::Graph));
    r.app.apply(Action::MoveSection(PanelId::Mem, -1));
    insta::assert_snapshot!("settings_after_changes", screen_text(&mut r.app, 120, 36));
}

#[test]
fn whole_screens() {
    for (w, h) in SIZES {
        let mut r = rig(Depth::True, |_| {});
        insta::assert_snapshot!(format!("screen_{w}x{h}"), screen_text(&mut r.app, w, h));
    }
    let mut r = rig(Depth::True, |_| {});
    insta::assert_snapshot!("screen_120x40", screen_text(&mut r.app, 120, 40));
    let mut r = rig(Depth::True, |c| {
        c.section.disk.c.enabled = false;
        c.section.cpu.c.graph = false;
        c.section.network.c.bars = false;
        c.section.tokens.c.summary = false;
        c.section.claude.c.graph = true;
    });
    insta::assert_snapshot!("screen_mixed_120x40", screen_text(&mut r.app, 120, 40));
    let mut r = rig(Depth::True, |c| c.layout = vitals::config::LayoutMode::Rows);
    insta::assert_snapshot!("screen_rows_at_80", screen_text(&mut r.app, 80, 40));
    let mut r = rig(Depth::True, |c| {
        c.layout = vitals::config::LayoutMode::Column
    });
    insta::assert_snapshot!("screen_column_at_130", screen_text(&mut r.app, 130, 60));
    // The screen is always filled: timelines share the spare rows (grow is a weight).
    let mut r = rig(Depth::True, |_| {});
    insta::assert_snapshot!(
        "screen_dynamic_fill_200x60",
        screen_text(&mut r.app, 200, 60)
    );
    insta::assert_snapshot!("screen_160x50", screen_text(&mut r.app, 160, 50));
    let mut r = rig(Depth::True, |c| {
        c.section.cpu.c.grow = 0;
        c.section.tokens.c.grow = 4;
    });
    insta::assert_snapshot!(
        "screen_grow_weights_100x40",
        screen_text(&mut r.app, 100, 40)
    );
    let mut r = rig(Depth::True, |_| {});
    r.app.apply(Action::Zoom(PanelId::Cpu));
    insta::assert_snapshot!("screen_zoomed_cpu", screen_text(&mut r.app, 100, 30));
    insta::assert_snapshot!("screen_zoomed_cpu_160x50", screen_text(&mut r.app, 160, 50));
    let mut r = rig(Depth::True, |_| {});
    r.app.apply(Action::Zoom(PanelId::Disk));
    insta::assert_snapshot!(
        "screen_zoomed_disk_mirrored",
        screen_text(&mut r.app, 120, 40)
    );
    let mut r = rig(Depth::True, |c| c.section.cpu.c.graph = false);
    r.app.apply(Action::Zoom(PanelId::Cpu));
    insta::assert_snapshot!(
        "screen_zoomed_with_graph_off",
        screen_text(&mut r.app, 100, 30)
    );
    let mut r = rig(Depth::True, |_| {});
    r.app.apply(Action::Zoom(PanelId::Claude));
    insta::assert_snapshot!("screen_zoomed_claude", screen_text(&mut r.app, 100, 30));
    insta::assert_snapshot!("screen_too_small", screen_text(&mut r.app, 39, 11));
}

#[test]
fn reordering_moves_a_section_through_the_rows() {
    let mut r = rig(Depth::True, |_| {});
    r.app.apply(Action::MoveSection(PanelId::Tokens, -1));
    r.app.apply(Action::MoveSection(PanelId::Tokens, -1));
    insta::assert_snapshot!("screen_reordered", screen_text(&mut r.app, 120, 40));
    assert_eq!(r.app.cfg.row[1].sections, ["tokens", "disk"]);
}

#[test]
fn a_v1_config_draws_like_its_conversion() {
    let c = vitals::config::Config::from_toml(
        "[panel.cpu]\nmode = \"meter\"\n[panel.disk]\nmode = \"bars\"\n[panel.network]\nmode = \"hidden\"\n",
    )
    .unwrap();
    let mut r = rig(Depth::True, |cfg| *cfg = c);
    insta::assert_snapshot!("screen_from_v1_config", screen_text(&mut r.app, 120, 40));
}

/// v8.5: a model week at 0% and a model with no tokens are not drawn; a non-zero one is. The label column
/// does not move when a row appears, and the readouts are unchanged (spec §4).
#[test]
fn models_with_no_usage_are_hidden() {
    use vitals::tokens::{ByFamily, Delta, Family, Msg};
    let mut r = rig(Depth::True, |_| {});
    // Claude: the fresh fixture has Fable at 0% and Sonnet at 7%.
    let text = panel_text(&mut r.app, PanelId::Claude, 100, 8);
    assert!(
        !text.contains("Fable"),
        "0% model week must be hidden:\n{text}"
    );
    for l in ["5 Hour", "Daily", "Weekly", "Sonnet"] {
        assert!(text.contains(l), "{l} must show:\n{text}");
    }
    let bar_x = |t: &str, l: &str| {
        t.lines()
            .find(|x| x.contains(l))
            .and_then(|x| x.find('░').or(x.find('█')))
    };
    let before = bar_x(&text, "Weekly");
    // 0.2% reads "0%" in the readout (a row there) but is not drawn.
    let faint = server_fixture("fresh").replacen("\"percent\":0,", "\"percent\":0.2,", 1);
    r.app
        .data
        .claude
        .set_server(Ok(vitals::claude::server::parse_status(&faint).unwrap()));
    r.app.data.claude.refresh(NOW);
    assert!(
        r.app
            .data
            .claude
            .rows
            .iter()
            .any(|x| x.label == "Fable" && x.hidden)
    );
    let text = panel_text(&mut r.app, PanelId::Claude, 100, 8);
    assert!(
        !text.contains("Fable"),
        "a model week that rounds to 0% must be hidden:\n{text}"
    );
    assert_eq!(
        bar_x(&text, "Weekly"),
        before,
        "label column moved:\n{text}"
    );
    let raised = server_fixture("fresh").replacen("\"percent\":0,", "\"percent\":3,", 1);
    r.app
        .data
        .claude
        .set_server(Ok(vitals::claude::server::parse_status(&raised).unwrap()));
    r.app.data.claude.refresh(NOW);
    let text = panel_text(&mut r.app, PanelId::Claude, 100, 8);
    assert!(
        text.contains("Fable"),
        "a model above 0% must show:\n{text}"
    );
    assert_eq!(
        bar_x(&text, "Weekly"),
        before,
        "label column moved:\n{text}"
    );
    // Tokens: the fixture's day has only opus and sonnet.
    let text = panel_text(&mut r.app, PanelId::Tokens, 100, 12);
    for f in ["haiku", "fable", "other"] {
        assert!(
            !text.contains(f),
            "{f} has no tokens and must be hidden:\n{text}"
        );
    }
    for l in ["opus", "sonnet", "input", "output", "cache wr", "cache rd"] {
        assert!(text.contains(l), "{l} must show:\n{text}");
    }
    let before = bar_x(&text, "opus");
    let mut by = ByFamily::default();
    by[Family::Fable.index()].output = 5_000;
    r.app.data.tokens.apply(Msg::Delta(Delta {
        minutes: vec![(NOW / 60_000, by)],
        hours: vec![],
        minute_floor: 0,
        hour_floor: 0,
    }));
    let text = panel_text(&mut r.app, PanelId::Tokens, 100, 12);
    assert!(
        text.contains("fable"),
        "a model with tokens must show:\n{text}"
    );
    assert_eq!(bar_x(&text, "opus"), before, "label column moved:\n{text}");
}
