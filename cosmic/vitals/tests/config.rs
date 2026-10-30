// SPDX-License-Identifier: MIT
//! Config load and save on disk, and the theme files; everything in temp dirs.

mod common;

use common::{TempDir, write};
use ratatui::style::Color;
use vitals::config::{self, Config, config_dir};
use vitals::theme::{Depth, ThemeBook, ThemeInputs};

#[test]
fn a_missing_file_is_the_defaults_without_a_warning() {
    let t = TempDir::new("cfg-missing");
    let (c, warn) = config::load(&t.join("vitals/config.toml"));
    assert_eq!(c, Config::default());
    assert!(warn.is_none());
}

#[test]
fn a_bad_file_is_the_defaults_with_a_header_warning() {
    let t = TempDir::new("cfg-bad");
    let path = t.join("vitals/config.toml");
    write(&path, "interval_ms = \"fast\"\n");
    let (c, warn) = config::load(&path);
    assert_eq!(c, Config::default());
    let w = warn.unwrap();
    assert!(w.starts_with("config.toml ignored:"), "{w}");
    write(&path, "this is [not toml");
    assert!(config::load(&path).1.is_some());
    // The bad file is left alone: it is only replaced when the user quits and a save happens.
    assert_eq!(std::fs::read_to_string(&path).unwrap(), "this is [not toml");
}

#[test]
fn save_is_atomic_and_round_trips() {
    let t = TempDir::new("cfg-save");
    let path = t.join("vitals/config.toml");
    let mut c = Config::default();
    c.section.cpu.c.bars = false;
    c.section.cpu.c.graph_rows = 4;
    c.row[0].widths = vec![2, 1];
    c.theme = "mono".into();
    config::save(&path, &c).unwrap();
    config::save(&path, &c).unwrap();
    let names: Vec<_> = std::fs::read_dir(path.parent().unwrap())
        .unwrap()
        .map(|e| e.unwrap().file_name().into_string().unwrap())
        .collect();
    assert_eq!(names, vec!["config.toml"], "no temp file is left behind");
    let (back, warn) = config::load(&path);
    assert!(warn.is_none());
    assert_eq!(back, c);
    let text = std::fs::read_to_string(&path).unwrap();
    assert!(
        text.contains("[section.cpu]")
            && text.contains("bars = false")
            && text.contains("[[row]]")
            && text.contains("widths = [2, 1]"),
        "{text}"
    );
}

#[test]
fn the_path_follows_xdg_config_home_then_home() {
    let env = |pairs: &'static [(&str, &str)]| {
        move |k: &str| {
            pairs
                .iter()
                .find(|(n, _)| *n == k)
                .map(|(_, v)| (*v).to_owned())
        }
    };
    assert_eq!(
        config_dir(&env(&[("XDG_CONFIG_HOME", "/x"), ("HOME", "/h")])).unwrap(),
        std::path::Path::new("/x/vitals")
    );
    assert_eq!(
        config_dir(&env(&[("XDG_CONFIG_HOME", ""), ("HOME", "/h")])).unwrap(),
        std::path::Path::new("/h/.config/vitals")
    );
    assert!(config_dir(&env(&[])).is_none());
}

#[test]
fn custom_themes_are_read_from_the_themes_folder_and_cycle_after_the_built_ins() {
    let t = TempDir::new("themes");
    write(
        &t.join("themes/ocean.toml"),
        "accent = \"#102030\"\n[series]\ncpu = [\"#001122\", \"#ddeeff\"]\n",
    );
    write(&t.join("themes/broken.toml"), "accent = \"nope\"\n");
    write(&t.join("themes/notes.txt"), "ignored");
    let custom = ThemeBook::read_custom(&t.join("themes"));
    let book = ThemeBook::new(Depth::True, ThemeInputs::default(), custom);
    assert_eq!(
        book.names().collect::<Vec<_>>(),
        ["default", "ramp", "mono", "broken", "ocean"]
    );
    assert_eq!(book.next_name("mono"), "broken");
    assert_eq!(book.next_name("ocean"), "default");
    let (ocean, warn) = book.build("ocean");
    assert!(warn.is_none());
    assert_eq!(ocean.accent, Color::Rgb(0x10, 0x20, 0x30));
    let (fallback, warn) = book.build("broken");
    assert_eq!(fallback.name, "default");
    assert!(warn.unwrap().starts_with("theme broken:"));
    assert!(book.build("nope").1.unwrap().contains("unknown theme"));
}

#[test]
fn the_cosmic_accent_comes_from_the_active_mode() {
    let t = TempDir::new("cosmic");
    let cosmic = t.join("cosmic");
    let accent = |r: &str, g: &str, b: &str| {
        format!("(\n base: (\n red: {r},\n green: {g},\n blue: {b},\n alpha: 1.0,\n ),\n)")
    };
    write(
        &cosmic.join("com.system76.CosmicTheme.Dark/v1/accent"),
        accent("1.0", "0.0", "0.0"),
    );
    write(
        &cosmic.join("com.system76.CosmicTheme.Light/v1/accent"),
        accent("0.0", "0.0", "1.0"),
    );
    assert_eq!(ThemeBook::read_cosmic_accent(t.path()), Some((255, 0, 0)));
    write(
        &cosmic.join("com.system76.CosmicTheme.Mode/v1/is_dark"),
        "false",
    );
    assert_eq!(ThemeBook::read_cosmic_accent(t.path()), Some((0, 0, 255)));
    assert_eq!(ThemeBook::read_cosmic_accent(&t.join("nowhere")), None);
}

#[test]
fn a_v1_file_is_converted_on_load_and_saved_as_v2() {
    let t = TempDir::new("cfg-v1");
    let path = t.join("vitals/config.toml");
    write(
        &path,
        "layout = \"grid\"\n[panel.cpu]\nmode = \"bars\"\n[panel.disk]\nmode = \"hidden\"\n",
    );
    let (c, warn) = config::load(&path);
    assert!(warn.is_none());
    assert!(!c.section.cpu.c.graph && c.section.cpu.c.bars);
    assert!(!c.section.disk.c.enabled);
    config::save(&path, &c).unwrap();
    let text = std::fs::read_to_string(&path).unwrap();
    assert!(
        text.contains("[section.cpu]") && !text.contains("[panel"),
        "{text}"
    );
    assert_eq!(config::load(&path).0, c);
}

#[test]
fn unknown_and_repeated_sections_in_the_rows_warn_and_are_skipped() {
    let t = TempDir::new("cfg-rows");
    let path = t.join("vitals/config.toml");
    write(
        &path,
        "[[row]]\nsections = [\"cpu\", \"gpu\"]\n[[row]]\nsections = [\"cpu\", \"disk\"]\n",
    );
    let (c, warn) = config::load(&path);
    let w = warn.unwrap();
    assert!(
        w.contains("unknown section gpu") && w.contains("listed twice: cpu"),
        "{w}"
    );
    let flat: Vec<_> = c.resolve(true).flat().collect();
    assert_eq!(flat, [config::PanelId::Cpu, config::PanelId::Disk]);
}
