// SPDX-License-Identifier: MIT
//! The usage reading types and the "hot" threshold.

/// Session or week at this percent or more is shown as hot (destructive color).
pub const HOT_PERCENT: f64 = 95.0;

/// One usage limit: label ("Session", "Week (all models)", "Week (Fable)"), percent used, reset time.
#[derive(Debug, Clone, PartialEq)]
pub struct Limit {
    pub label: String,
    pub percent: f64,
    pub resets_text: Option<String>,
    /// Reset instant, ms since the Unix epoch.
    pub resets_at: Option<i64>,
}

/// The session limit, the all-models week and any per-model weeks.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Reading {
    pub session: Option<Limit>,
    pub week: Option<Limit>,
    pub models: Vec<Limit>,
}
