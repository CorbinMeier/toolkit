// SPDX-License-Identifier: MIT

//! Pure state logic: what the panel button and popup show for a given daemon state.

use serde::Deserialize;

/// The project's own symbolic icons, installed by `scripts/install-applet.sh` into
/// `~/.local/share/icons/hicolor/symbolic/apps/`.
pub const ICON_BELL: &str = "net.corbinmeier.NotificationCenter-symbolic";
pub const ICON_BELL_NEW: &str = "net.corbinmeier.NotificationCenter-new-symbolic";
pub const ICON_BELL_DND: &str = "net.corbinmeier.NotificationCenter-dnd-symbolic";

/// Stock icons in the Cosmic and Pop themes (`/usr/share/icons/*/scalable/status/`), used when
/// the project's icons are not installed (theme lookup misses).
pub const ICON_FALLBACK: &str = "notification-symbolic";
pub const ICON_FALLBACK_DND: &str = "notification-disabled-symbolic";

pub const TOOLTIP_OFFLINE: &str = "Notification daemon not running";

/// How many recent notifications the popup lists.
pub const POPUP_ROWS: u32 = 5;

/// One row of `GetHistory`. Unknown keys are ignored.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct HistoryItem {
    pub id: u32,
    #[serde(default)]
    pub app: String,
    #[serde(default)]
    pub summary: String,
    #[serde(default)]
    pub read_at: Option<f64>,
}

impl HistoryItem {
    pub fn is_unread(&self) -> bool {
        self.read_at.is_none()
    }

    /// Single-line label: `app: summary` (either half may be missing), newlines flattened.
    pub fn label(&self) -> String {
        let flat = |s: &str| s.split_whitespace().collect::<Vec<_>>().join(" ");
        let (app, summary) = (flat(&self.app), flat(&self.summary));
        match (app.is_empty(), summary.is_empty()) {
            (false, false) => format!("{app}: {summary}"),
            (false, true) => app,
            (true, false) => summary,
            (true, true) => format!("Notification {}", self.id),
        }
    }
}

/// Parse the `GetHistory` JSON, keeping at most `max` rows. Bad JSON gives an empty list.
pub fn parse_history(json: &str, max: usize) -> Vec<HistoryItem> {
    serde_json::from_str::<Vec<HistoryItem>>(json)
        .map(|mut v| {
            v.truncate(max);
            v
        })
        .unwrap_or_default()
}

/// D-Bus error names a daemon without `ToggleShade` answers with: the left click then falls back
/// to `ToggleCenter`.
pub fn is_missing_method(error_name: &str) -> bool {
    matches!(
        error_name,
        "org.freedesktop.DBus.Error.UnknownMethod" | "org.freedesktop.DBus.Error.UnknownInterface"
    )
}

/// What the applet knows about the daemon.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Status {
    pub online: bool,
    /// Notifications that arrived since the shade or center was last opened (the badge).
    pub unseen: u32,
    pub unread: u32,
    pub dnd: bool,
    pub recent: Vec<HistoryItem>,
}

impl Status {
    pub fn offline() -> Self {
        Self::default()
    }

    /// DND wins over unseen; unseen wins over the plain bell.
    pub fn icon(&self) -> &'static str {
        if self.dnd {
            ICON_BELL_DND
        } else if self.unseen > 0 {
            ICON_BELL_NEW
        } else {
            ICON_BELL
        }
    }

    /// The stock icon to fall back to when `icon()` is not in the theme.
    pub fn fallback_icon(&self) -> &'static str {
        if self.dnd {
            ICON_FALLBACK_DND
        } else {
            ICON_FALLBACK
        }
    }

    /// Number shown beside the bell: the "new" count, nothing at 0, exact up to 999.
    pub fn badge(&self) -> Option<String> {
        match self.unseen {
            0 => None,
            1..=999 => Some(self.unseen.to_string()),
            _ => Some("999+".to_owned()),
        }
    }

    pub fn tooltip(&self) -> String {
        if !self.online {
            return TOOLTIP_OFFLINE.to_owned();
        }
        let dnd = if self.dnd { ", Do Not Disturb" } else { "" };
        format!("{}{dnd}", self.header())
    }

    /// Popup header.
    pub fn header(&self) -> String {
        format!("{} new \u{b7} {} unread", self.unseen, self.unread)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn item(id: u32, app: &str, summary: &str, read: Option<f64>) -> HistoryItem {
        HistoryItem {
            id,
            app: app.into(),
            summary: summary.into(),
            read_at: read,
        }
    }

    fn st(unseen: u32, dnd: bool) -> Status {
        Status {
            online: true,
            unseen,
            unread: 0,
            dnd,
            recent: vec![],
        }
    }

    #[test]
    fn badge_hidden_at_zero_and_capped() {
        assert_eq!(st(0, false).badge(), None);
        assert_eq!(st(1, false).badge().as_deref(), Some("1"));
        assert_eq!(st(99, false).badge().as_deref(), Some("99"));
        assert_eq!(st(100, false).badge().as_deref(), Some("100"));
        assert_eq!(st(999, false).badge().as_deref(), Some("999"));
        assert_eq!(st(1000, false).badge().as_deref(), Some("999+"));
        assert_eq!(st(5000, false).badge().as_deref(), Some("999+"));
    }

    #[test]
    fn badge_follows_unseen_not_unread() {
        let s = Status {
            online: true,
            unseen: 0,
            unread: 250,
            ..Status::default()
        };
        assert_eq!(s.badge(), None);
    }

    #[test]
    fn icon_follows_dnd_then_unseen() {
        let id = "net.corbinmeier.NotificationCenter";
        assert_eq!(st(0, false).icon(), format!("{id}-symbolic"));
        assert_eq!(st(7, false).icon(), format!("{id}-new-symbolic"));
        assert_eq!(st(0, true).icon(), format!("{id}-dnd-symbolic"));
        assert_eq!(st(7, true).icon(), format!("{id}-dnd-symbolic"));
        assert_eq!(st(0, false).fallback_icon(), "notification-symbolic");
        assert_eq!(st(7, false).fallback_icon(), "notification-symbolic");
        assert_eq!(
            st(7, true).fallback_icon(),
            "notification-disabled-symbolic"
        );
    }

    #[test]
    fn tooltip_offline_and_online() {
        assert_eq!(
            Status::offline().tooltip(),
            "Notification daemon not running"
        );
        assert_eq!(st(3, false).tooltip(), "3 new \u{b7} 0 unread");
        assert_eq!(
            st(0, true).tooltip(),
            "0 new \u{b7} 0 unread, Do Not Disturb"
        );
        let s = Status {
            online: true,
            unseen: 2,
            unread: 41,
            ..Status::default()
        };
        assert_eq!(s.tooltip(), "2 new \u{b7} 41 unread");
    }

    #[test]
    fn offline_default_is_not_online() {
        let s = Status::offline();
        assert!(!s.online);
        assert_eq!(s.badge(), None);
    }

    #[test]
    fn popup_header() {
        let s = Status {
            online: true,
            unseen: 3,
            unread: 12,
            ..Status::default()
        };
        assert_eq!(s.header(), "3 new \u{b7} 12 unread");
    }

    #[test]
    fn label_is_single_line() {
        assert_eq!(item(1, "Mail", "Hi", None).label(), "Mail: Hi");
        assert_eq!(item(1, "Mail", "a\nb  c", None).label(), "Mail: a b c");
        assert_eq!(item(2, "", "Only", None).label(), "Only");
        assert_eq!(item(3, "App", "", None).label(), "App");
        assert_eq!(item(4, "", "", None).label(), "Notification 4");
    }

    #[test]
    fn unread_is_read_at_null() {
        assert!(item(1, "a", "b", None).is_unread());
        assert!(!item(1, "a", "b", Some(10.5)).is_unread());
    }

    #[test]
    fn parse_history_keeps_order_and_truncates() {
        let json = r#"[
            {"id":9,"app":"A","summary":"s9","read_at":null,"state":"x"},
            {"id":8,"app":"B","summary":"s8","read_at":1700000000.5},
            {"id":7,"app":"C","summary":"s7","read_at":null}
        ]"#;
        let v = parse_history(json, 2);
        assert_eq!(v.len(), 2);
        assert_eq!(v[0].id, 9);
        assert!(v[0].is_unread());
        assert!(!v[1].is_unread());
    }

    #[test]
    fn missing_method_errors_trigger_the_toggle_center_fallback() {
        assert!(is_missing_method(
            "org.freedesktop.DBus.Error.UnknownMethod"
        ));
        assert!(is_missing_method(
            "org.freedesktop.DBus.Error.UnknownInterface"
        ));
        // A real failure of ToggleShade must not silently open the window instead.
        assert!(!is_missing_method("org.freedesktop.DBus.Error.Failed"));
        assert!(!is_missing_method("org.freedesktop.DBus.Error.NoReply"));
        assert!(!is_missing_method(""));
    }

    #[test]
    fn parse_history_tolerates_garbage() {
        assert!(parse_history("not json", 5).is_empty());
        assert!(parse_history("{}", 5).is_empty());
        assert!(parse_history("[]", 5).is_empty());
    }
}
