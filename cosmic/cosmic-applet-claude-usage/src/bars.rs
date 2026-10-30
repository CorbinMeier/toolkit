// SPDX-License-Identifier: MIT
//! The two vertical bars side by side (session left, daily right), drawn on one canvas. Fill is anchored
//! to the bottom and grows upward; the pace marker is a horizontal line at the elapsed share from the
//! bottom. Same layout on horizontal and vertical panels.

use cosmic::iced::{Color, Point, Rectangle, Size, mouse};
use cosmic::widget::canvas::{self, Geometry};

use claude_usage_core::pace::{self, Kind};
use claude_usage_core::reading::{HOT_PERCENT, Limit, Reading};

/// Bar width as a fraction of the panel's icon size (icon / BAR_WIDTH_DIV); the bar height is the icon size.
pub const BAR_WIDTH_DIV: f32 = 3.0;
/// Gap between the two bars as a fraction of the icon size (icon / BAR_GAP_DIV).
pub const BAR_GAP_DIV: f32 = 6.0;
/// Thickness of the pace marker line in logical px.
pub const MARKER_PX: f32 = 2.0;

/// What one bar shows.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct BarData {
    /// Fill share, 0-100 (capped; the text shows the raw number).
    pub fill: f64,
    /// Share of the window gone, 0-100; None draws no marker.
    pub elapsed: Option<f64>,
    /// 95% or more: the fill turns destructive.
    pub hot: bool,
}

pub fn bar_data(kind: Kind, limit: Option<&Limit>, now_ms: i64) -> Option<BarData> {
    let l = limit?;
    Some(BarData {
        fill: l.percent.clamp(0.0, 100.0),
        elapsed: pace::elapsed_percent(kind, l.resets_at, now_ms),
        hot: l.percent >= HOT_PERCENT,
    })
}

/// The right bar: the current day part's share of the week allowance, from the week limit.
pub fn daily_bar_data(week: Option<&Limit>, now_ms: i64) -> Option<BarData> {
    let l = week?;
    let part = pace::day_part(l.resets_at, now_ms)?;
    Some(BarData {
        fill: pace::daily_percent(l.percent, part.index).clamp(0.0, 100.0),
        elapsed: Some(part.elapsed_percent),
        // The week is the real lockout, so its warning lives here; passing a day's share is routine.
        hot: l.percent >= HOT_PERCENT,
    })
}

/// (session, daily) for a reading; both None before the first reading.
pub fn bars_of(reading: Option<&Reading>, now_ms: i64) -> (Option<BarData>, Option<BarData>) {
    let r = reading;
    (
        bar_data(Kind::Session, r.and_then(|r| r.session.as_ref()), now_ms),
        daily_bar_data(r.and_then(|r| r.week.as_ref()), now_ms),
    )
}

/// Fill rectangle (y, height) in px from the bar's top: anchored to the bottom, growing upward.
pub fn fill_span(height: f32, fill_percent: f64) -> (f32, f32) {
    let h = (height * (fill_percent / 100.0) as f32).clamp(0.0, height);
    (height - h, h)
}

/// Marker line y in px from the bar's top: `elapsed` percent of the height up from the bottom.
pub fn marker_y(height: f32, elapsed_percent: f64) -> f32 {
    let from_bottom = height * (elapsed_percent / 100.0) as f32;
    (height - from_bottom - MARKER_PX / 2.0).clamp(0.0, (height - MARKER_PX).max(0.0))
}

/// Panel bar sizes from the icon size: (bar width, bar height, gap between bars).
pub fn dimensions(icon: u16) -> (f32, f32, f32) {
    let icon = f32::from(icon);
    (
        (icon / BAR_WIDTH_DIV).round().max(3.0),
        icon,
        (icon / BAR_GAP_DIV).round().max(2.0),
    )
}

/// The canvas size (width, height).
pub fn canvas_size(icon: u16) -> (f32, f32) {
    let (w, h, gap) = dimensions(icon);
    (2.0 * w + gap, h)
}

/// White on a dark theme, near-black on a light one. Read from the theme on every draw, so live switches apply.
pub fn marker_color(is_dark: bool) -> Color {
    if is_dark {
        Color::WHITE
    } else {
        Color::from_rgb(0.1, 0.1, 0.1)
    }
}

pub struct Bars {
    pub session: Option<BarData>,
    pub daily: Option<BarData>,
    pub icon: u16,
}

fn color(c: cosmic::cosmic_theme::palette::Srgba) -> Color {
    Color::from_rgba(c.red, c.green, c.blue, c.alpha)
}

impl<Message> canvas::Program<Message, cosmic::Theme> for Bars {
    type State = ();

    fn draw(
        &self,
        _state: &(),
        renderer: &cosmic::Renderer,
        theme: &cosmic::Theme,
        bounds: Rectangle,
        _cursor: mouse::Cursor,
    ) -> Vec<Geometry> {
        let cosmic = theme.cosmic();
        let mut track = color(cosmic.palette.neutral_5);
        track.a = 0.5;
        let accent = color(cosmic.accent_color());
        let destructive = color(cosmic.destructive_color());
        let (width, height, gap) = dimensions(self.icon);
        let mut frame = canvas::Frame::new(renderer, bounds.size());
        for (i, bar) in [self.session, self.daily].into_iter().enumerate() {
            let x = i as f32 * (width + gap);
            frame.fill_rectangle(Point::new(x, 0.0), Size::new(width, height), track);
            let Some(bar) = bar else { continue };
            let (y, h) = fill_span(height, bar.fill);
            if h > 0.0 {
                let c = if bar.hot { destructive } else { accent };
                frame.fill_rectangle(Point::new(x, y), Size::new(width, h), c);
            }
            if let Some(e) = bar.elapsed {
                let c = marker_color(cosmic.is_dark);
                let y = marker_y(height, e);
                frame.fill_rectangle(Point::new(x, y), Size::new(width, MARKER_PX), c);
            }
        }
        vec![frame.into_geometry()]
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use claude_usage_core::reading::Limit;

    fn limit(percent: f64, resets_at: Option<i64>) -> Limit {
        Limit {
            label: "x".into(),
            percent,
            resets_text: None,
            resets_at,
        }
    }

    #[test]
    fn fill_is_anchored_at_the_bottom_and_grows_upward() {
        assert_eq!(fill_span(100.0, 0.0), (100.0, 0.0));
        assert_eq!(fill_span(100.0, 25.0), (75.0, 25.0));
        assert_eq!(fill_span(100.0, 100.0), (0.0, 100.0));
    }

    #[test]
    fn marker_is_measured_from_the_bottom_and_stays_inside() {
        assert_eq!(marker_y(100.0, 0.0), 98.0);
        assert_eq!(marker_y(100.0, 50.0), 49.0);
        assert_eq!(marker_y(100.0, 100.0), 0.0);
        // Fill 60% with 40% of the window gone: the fill top is above the line = ahead of pace.
        let (fill_top, _) = fill_span(100.0, 60.0);
        assert!(fill_top < marker_y(100.0, 40.0));
        // Fill 20%: the line is above the fill top = behind pace.
        let (fill_top, _) = fill_span(100.0, 20.0);
        assert!(marker_y(100.0, 40.0) < fill_top);
    }

    #[test]
    fn bar_data_caps_the_fill_marks_hot_and_needs_a_reset_for_the_session_marker() {
        let d = bar_data(Kind::Session, Some(&limit(120.0, None)), 0).unwrap();
        assert_eq!((d.fill, d.hot, d.elapsed), (100.0, true, None));
        let d = bar_data(
            Kind::Session,
            Some(&limit(94.9, Some(5 * 3_600_000 / 2))),
            0,
        )
        .unwrap();
        assert_eq!((d.hot, d.elapsed), (false, Some(50.0)));
        assert!(bar_data(Kind::Session, None, 0).is_none());
        assert_eq!(bars_of(None, 0), (None, None));
    }

    const DAY: i64 = 24 * 3_600_000;

    #[test]
    fn daily_bar_caps_the_fill_floors_at_zero_and_marks_the_day_part() {
        // Reset so that now (0) is 1 day + 0 h into the week.
        let reset = 6 * DAY;
        let d = daily_bar_data(Some(&limit(40.0, Some(reset))), 0).unwrap();
        assert_eq!((d.fill, d.elapsed, d.hot), (100.0, Some(0.0), false));
        // 3 days + 12 h in.
        let reset = 7 * DAY - 3 * DAY - 12 * 3_600_000;
        let d = daily_bar_data(Some(&limit(12.0, Some(reset))), 0).unwrap();
        assert_eq!((d.fill, d.elapsed), (0.0, Some(50.0)));
        let d = daily_bar_data(Some(&limit(50.0, Some(reset))), 0).unwrap();
        assert!((d.fill - 50.0).abs() < 1e-9);
        assert!(daily_bar_data(None, 0).is_none());
    }

    #[test]
    fn daily_bar_is_hot_from_the_week_percent_not_the_daily_number() {
        // 1 day in, 40% used: daily number 180, week not hot.
        let d = daily_bar_data(Some(&limit(40.0, Some(6 * DAY))), 0).unwrap();
        assert!(!d.hot);
        // Day 0, 94.9% used: daily number far above 95, still not hot.
        let d = daily_bar_data(Some(&limit(94.9, Some(7 * DAY))), 0).unwrap();
        assert_eq!((d.fill, d.hot), (100.0, false));
        assert!(
            daily_bar_data(Some(&limit(95.0, Some(7 * DAY))), 0)
                .unwrap()
                .hot
        );
        // Day 6, 5% used: under budget, fill 0, not hot.
        let d = daily_bar_data(Some(&limit(5.0, Some(DAY))), 0).unwrap();
        assert_eq!((d.fill, d.hot), (0.0, false));
    }

    #[test]
    fn marker_is_white_on_dark_and_dark_on_light() {
        assert_eq!(marker_color(true), Color::WHITE);
        assert!(marker_color(false).r < 0.5);
    }

    #[test]
    fn dimensions_follow_the_icon_size_with_session_left_of_daily() {
        // width icon/3, height icon, gap icon/6; same on every panel orientation.
        assert_eq!(dimensions(24), (8.0, 24.0, 4.0));
        assert_eq!(canvas_size(24), (20.0, 24.0));
        assert_eq!(dimensions(8).0, 3.0);
    }
}
