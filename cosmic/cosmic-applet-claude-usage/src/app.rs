// SPDX-License-Identifier: MIT

use std::time::{Duration, Instant};

use cosmic::{
    Element, Task, app,
    iced::{Length, Subscription},
    surface,
    widget::{self, canvas},
};

use crate::bars::{self, Bars};
use crate::poller::{self, Schedule, Status};
use claude_usage_core::feed;

const APP_ID: &str = "net.corbinmeier.CosmicAppletClaudeUsage";
/// Re-renders the pace markers and checks whether a poll is due.
const TICK: Duration = Duration::from_secs(60);

pub struct ClaudeUsage {
    core: app::Core,
    status: Status,
    schedule: Schedule,
    /// ms since the Unix epoch, refreshed on every tick and poll.
    now_ms: i64,
}

#[derive(Debug, Clone)]
pub enum Message {
    Tick,
    Polled(Result<String, String>),
    // Constructed by libcosmic's surface machinery (the hover tooltip), not by this crate.
    #[allow(dead_code)]
    Surface(surface::Action<Message>),
}

fn now_ms() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

fn poll_task() -> app::Task<Message> {
    cosmic::task::future(async { Message::Polled(poller::run_claude().await) })
}

impl ClaudeUsage {
    /// Take the status-line feed into the status; true when the feed is fresh (no `/usage` run needed).
    fn read_feed(&mut self) -> bool {
        let Some(f) = feed::read() else {
            return false;
        };
        self.status.apply_feed(&f, self.now_ms);
        feed::is_fresh(&f, self.now_ms / 1000)
    }
}

impl cosmic::Application for ClaudeUsage {
    type Message = Message;
    type Executor = cosmic::SingleThreadExecutor;
    type Flags = ();
    const APP_ID: &'static str = APP_ID;

    fn core(&self) -> &app::Core {
        &self.core
    }

    fn core_mut(&mut self) -> &mut app::Core {
        &mut self.core
    }

    fn init(core: app::Core, _flags: Self::Flags) -> (Self, app::Task<Message>) {
        let mut app = ClaudeUsage {
            core,
            status: Status::default(),
            schedule: Schedule::default(),
            now_ms: now_ms(),
        };
        // First reading at startup: the feed if fresh, else /usage.
        let task = if !app.read_feed() && app.schedule.try_start(Instant::now()) {
            poll_task()
        } else {
            Task::none()
        };
        (app, task)
    }

    fn style(&self) -> Option<cosmic::iced::theme::Style> {
        Some(cosmic::applet::style())
    }

    fn subscription(&self) -> Subscription<Message> {
        cosmic::iced::time::every(TICK).map(|_| Message::Tick)
    }

    fn update(&mut self, message: Message) -> app::Task<Message> {
        match message {
            Message::Tick => {
                self.now_ms = now_ms();
                if !self.read_feed() && self.schedule.try_start(Instant::now()) {
                    return poll_task();
                }
            }
            Message::Polled(outcome) => {
                self.now_ms = now_ms();
                self.status.apply(outcome, self.now_ms);
                self.schedule.finish();
            }
            Message::Surface(a) => {
                return cosmic::task::message(cosmic::Action::Surface(a));
            }
        }
        Task::none()
    }

    fn view(&self) -> Element<'_, Message> {
        let applet = &self.core.applet;
        let horizontal = applet.is_horizontal();
        let icon = applet.suggested_size(true).0;
        let (session, daily) = bars::bars_of(self.status.reading.as_ref(), self.now_ms);
        let (w, h) = bars::canvas_size(icon);
        let bars = canvas(Bars {
            session,
            daily,
            icon,
        })
        .width(Length::Fixed(w))
        .height(Length::Fixed(h));
        // Same footprint as a stock applet button: the icon size plus the suggested padding on the cross axis.
        let (major, minor) = applet.suggested_padding(true);
        let (pad_x, pad_y) = if horizontal {
            (major, minor)
        } else {
            (minor, major)
        };
        let slot = u32::from(icon) + 2 * u32::from(if horizontal { pad_y } else { pad_x });
        let padded = widget::container(bars).padding([0, pad_x]);
        let content: Element<'_, Message> = if horizontal {
            padded.center_y(Length::Fixed(slot as f32)).into()
        } else {
            padded
                .padding([pad_y, 0])
                .center_x(Length::Fixed(slot as f32))
                .into()
        };
        applet
            .applet_tooltip::<Message>(
                content,
                claude_usage_core::readout::text(
                    self.status.reading.as_ref(),
                    self.status.read_at,
                    self.status.stale(),
                    self.now_ms,
                ),
                false,
                Message::Surface,
                None,
            )
            .into()
    }
}
