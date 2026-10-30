// SPDX-License-Identifier: MIT
//! vitals: a lightweight btop-style terminal dashboard.
//!
//! - [`sample`]: kept-open `/proc` and `/sys` readers (CPU, memory, disk, network, temperatures).
//! - [`history`]: fixed-capacity ring buffers and column downsampling for the graphs.
//! - [`tokens`]: the Claude Code token indexer (transcript scan, dedupe, buckets, on-disk index).
//! - [`claude`]: the Claude usage row's data (feed file, usage-server client, merge).
//! - [`readout`]: the command-line section readouts (`--cpu`, `--json`, `--watch`, ...).
//! - [`config`], [`theme`]: the TOML config, colors and themes.
//! - [`web`]: the dashboard server of `vitals --serve` (HTTP, TLS, Server-Sent Events).
//! - [`app`], [`input`], [`ui`]: state, the mouse and key map, and the widgets.

pub mod app;
pub mod claude;
pub mod config;
pub mod frame;
pub mod history;
pub mod input;
pub mod readout;
pub mod sample;
pub mod theme;
pub mod tokens;
pub mod ui;
pub mod web;
