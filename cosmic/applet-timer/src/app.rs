// SPDX-License-Identifier: MIT

use std::sync::LazyLock;
use std::time::{Duration, Instant};

use cosmic::{
    Element, Task, app,
    iced::{
        Alignment, Length, Subscription,
        platform_specific::shell::wayland::commands::popup::destroy_popup,
        widget::{column, row},
        window,
    },
    surface, theme,
    widget::{Id, autosize, button, text},
};

use crate::sound::Ringer;
use crate::timer::{self, State};

const APP_ID: &str = "com.corbinmeier.CosmicAppletTimer";
static AUTOSIZE_MAIN_ID: LazyLock<Id> = LazyLock::new(|| Id::new("timer-autosize-main"));

pub struct Timer {
    core: app::Core,
    popup: Option<window::Id>,
    state: State,
    /// Last-used duration (in memory only); default 5:00.
    hours: u32,
    minutes: u32,
    seconds: u32,
    /// The duration of the countdown currently running, for the notification.
    running_secs: u64,
    /// Updated on every tick so the view re-renders.
    now: Instant,
    /// Flash phase while Ringing.
    flash: bool,
    ringer: Option<Ringer>,
}

#[derive(Debug, Clone)]
pub enum Message {
    TogglePopup,
    CloseRequested(window::Id),
    Tick,
    SetHours(u32),
    SetMinutes(u32),
    SetSeconds(u32),
    Start,
    Cancel,
    Dismiss,
    Flash,
    // Constructed by libcosmic's surface machinery, not by this crate.
    #[allow(dead_code)]
    Surface(surface::Action<Message>),
}

impl Timer {
    fn remaining(&self) -> Option<u64> {
        match self.state {
            State::Idle | State::Ringing => None,
            State::Running { deadline } => Some(timer::remaining_secs(deadline, self.now)),
        }
    }

    fn close_popup(&mut self) -> app::Task<Message> {
        match self.popup.take() {
            Some(p) => destroy_popup(p),
            None => Task::none(),
        }
    }

    fn panel_content(&self) -> Element<'_, Message> {
        let applet = &self.core.applet;
        let ringing = matches!(self.state, State::Ringing);
        let Some(secs) = self.remaining().or(ringing.then_some(0)) else {
            return applet
                .icon_button("clock-symbolic")
                .on_press_down(Message::TogglePopup)
                .into();
        };
        let label = timer::format_secs(secs);
        let content: Element<'_, Message> = if applet.is_horizontal() {
            row![
                applet.text(label),
                cosmic::widget::space::vertical().height(Length::Fixed(
                    (applet.suggested_size(true).1 + 2 * applet.suggested_padding(true).1) as f32
                ))
            ]
            .align_y(Alignment::Center)
            .into()
        } else {
            // Vertical panels: stack the pieces, like the stock time applet.
            let pieces: Vec<Element<'_, Message>> = label
                .split(':')
                .map(|p| applet.text(p.to_owned()).into())
                .collect();
            column![
                cosmic::iced::widget::Column::with_children(pieces)
                    .align_x(Alignment::Center)
                    .spacing(4),
                cosmic::widget::space::horizontal().width(Length::Fixed(
                    (applet.suggested_size(true).0 + 2 * applet.suggested_padding(true).1) as f32
                ))
            ]
            .align_x(Alignment::Center)
            .into()
        };
        let pad = applet.suggested_padding(true).0;
        let (class, msg) = if ringing {
            (flash_class(self.flash), Message::Dismiss)
        } else {
            (cosmic::theme::Button::AppletIcon, Message::TogglePopup)
        };
        button::custom(content)
            .padding(if applet.is_horizontal() {
                [0, pad]
            } else {
                [pad, 0]
            })
            .on_press_down(msg)
            .class(class)
            .into()
    }
}

impl cosmic::Application for Timer {
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
        (
            Timer {
                core,
                popup: None,
                state: State::Idle,
                hours: 0,
                minutes: 5,
                seconds: 0,
                running_secs: 0,
                now: Instant::now(),
                flash: false,
                ringer: None,
            },
            Task::none(),
        )
    }

    fn on_close_requested(&self, id: window::Id) -> Option<Message> {
        Some(Message::CloseRequested(id))
    }

    fn style(&self) -> Option<cosmic::iced::theme::Style> {
        Some(cosmic::applet::style())
    }

    fn subscription(&self) -> Subscription<Message> {
        match self.state {
            State::Running { .. } => {
                cosmic::iced::time::every(Duration::from_secs(1)).map(|_| Message::Tick)
            }
            State::Ringing => {
                cosmic::iced::time::every(Duration::from_millis(500)).map(|_| Message::Flash)
            }
            State::Idle => Subscription::none(),
        }
    }

    fn update(&mut self, message: Message) -> app::Task<Message> {
        match message {
            Message::TogglePopup => {
                if self.popup.is_some() {
                    return self.close_popup();
                }
                return cosmic::surface::surface_task(cosmic::surface::action::app_popup(
                    |_| Default::default(),
                    |app: &mut Self| {
                        let new_id = window::Id::unique();
                        app.popup = Some(new_id);
                        let mut settings = app.core.applet.get_popup_settings(
                            app.core.main_window_id().unwrap(),
                            new_id,
                            None,
                            None,
                            None,
                        );
                        settings.positioner.size = None;
                        settings
                    },
                    None,
                ));
            }
            Message::CloseRequested(id) => {
                if self.popup == Some(id) {
                    self.popup = None;
                }
            }
            Message::SetHours(v) => self.hours = v,
            Message::SetMinutes(v) => self.minutes = v,
            Message::SetSeconds(v) => self.seconds = v,
            Message::Start => {
                if matches!(self.state, State::Idle)
                    && timer::to_secs(self.hours, self.minutes, self.seconds) > 0
                {
                    let d = timer::duration(self.hours, self.minutes, self.seconds);
                    self.now = Instant::now();
                    self.running_secs = d.as_secs();
                    self.state = State::Running {
                        deadline: self.now + d,
                    };
                    return self.close_popup();
                }
            }
            Message::Cancel => {
                self.state = State::Idle;
                return self.close_popup();
            }
            Message::Dismiss => {
                self.state = State::Idle;
                if let Some(r) = self.ringer.take() {
                    r.stop();
                }
            }
            Message::Flash => self.flash = !self.flash,
            Message::Tick => {
                self.now = Instant::now();
                if let State::Running { deadline } = self.state
                    && timer::is_finished(deadline, self.now)
                {
                    self.state = State::Ringing;
                    self.flash = false;
                    self.ringer = Some(Ringer::start());
                    notify_finished(self.running_secs);
                }
            }
            Message::Surface(a) => {
                return cosmic::task::message(cosmic::Action::Surface(a));
            }
        }
        Task::none()
    }

    fn view(&self) -> Element<'_, Message> {
        autosize::autosize(self.panel_content(), AUTOSIZE_MAIN_ID.clone()).into()
    }

    fn view_window(&self, _id: window::Id) -> Element<'_, Message> {
        let spacing = theme::active().cosmic().spacing;
        let body: Element<'_, Message> = match self.remaining() {
            Some(secs) => cosmic::widget::column::with_children(vec![
                text::title1(timer::format_secs(secs)).into(),
                button::standard("Cancel").on_press(Message::Cancel).into(),
            ])
            .spacing(spacing.space_s)
            .align_x(Alignment::Center)
            .into(),
            None => {
                let field = |label: &'static str,
                             v: u32,
                             max: u32,
                             f: fn(u32) -> Message|
                 -> Element<'_, Message> {
                    cosmic::widget::column::with_children(vec![
                        cosmic::widget::spin_button(format!("{v:02}"), v, 1, 0, max, f).into(),
                        text::caption(label).into(),
                    ])
                    .spacing(spacing.space_xxs)
                    .align_x(Alignment::Center)
                    .into()
                };
                cosmic::widget::column::with_children(vec![
                    row![
                        field("Hours", self.hours, 99, Message::SetHours),
                        field("Minutes", self.minutes, 59, Message::SetMinutes),
                        field("Seconds", self.seconds, 59, Message::SetSeconds),
                    ]
                    .spacing(spacing.space_s)
                    .into(),
                    button::suggested("Start").on_press(Message::Start).into(),
                ])
                .spacing(spacing.space_s)
                .align_x(Alignment::Center)
                .into()
            }
        };
        self.core
            .applet
            .popup_container(cosmic::widget::container(body).padding(spacing.space_s))
            .into()
    }
}

fn notify_finished(secs: u64) {
    let body = format!("Duration: {}", timer::format_secs(secs));
    // show() is blocking D-Bus I/O: keep it off the UI thread.
    std::thread::spawn(move || {
        if let Err(e) = notify_rust::Notification::new()
            .appname("Timer")
            .summary("Timer finished")
            .body(&body)
            .icon("clock-symbolic")
            .show()
        {
            eprintln!("cosmic-applet-timer: notification failed: {e}");
        }
    });
}

/// Panel button style for the Ringing state: red/yellow with black content.
fn flash_class(flash: bool) -> cosmic::theme::Button {
    use cosmic::iced::{Background, Color};
    let bg = if flash {
        Color::from_rgb(1.0, 0.85, 0.0)
    } else {
        Color::from_rgb(0.9, 0.1, 0.1)
    };
    let style = move || {
        let mut st = cosmic::widget::button::Style::new();
        st.background = Some(Background::Color(bg));
        st.text_color = Some(Color::BLACK);
        st.icon_color = Some(Color::BLACK);
        st
    };
    cosmic::theme::Button::Custom {
        active: Box::new(move |_, _| style()),
        disabled: Box::new(move |_| style()),
        hovered: Box::new(move |_, _| style()),
        pressed: Box::new(move |_, _| style()),
    }
}
