// SPDX-License-Identifier: MIT

use std::any::TypeId;
use std::sync::LazyLock;
use std::time::Duration;

use cosmic::{
    Element, Task, app,
    iced::{
        Alignment, Length, Subscription,
        futures::SinkExt,
        platform_specific::shell::wayland::commands::popup::destroy_popup,
        widget::{column, row},
        window,
    },
    surface, theme,
    widget::{Id, autosize, button, mouse_area, text, toggler},
};
use futures_util::StreamExt;
use zbus::Connection;

use crate::bus;
use crate::model::{HistoryItem, Status};

const APP_ID: &str = "net.corbinmeier.CosmicAppletNotifications";
const POPUP_WIDTH: f32 = 340.0;
static AUTOSIZE_MAIN_ID: LazyLock<Id> = LazyLock::new(|| Id::new("notifications-autosize-main"));

pub struct NotificationCenter {
    core: app::Core,
    popup: Option<window::Id>,
    status: Status,
    conn: Option<Connection>,
}

#[derive(Debug, Clone)]
pub enum Message {
    /// Left click on the bell: the dashboard shade.
    ToggleShade,
    /// Right or middle click on the bell.
    TogglePopup,
    CloseRequested(window::Id),
    /// The subscription's bus connection, kept for method calls.
    Connected(Connection),
    Status(Status),
    SetDnd(bool),
    OpenCenter,
    OpenNotification(u32),
    /// A bus call finished; errors were already logged.
    Done,
    // Constructed by libcosmic's surface machinery, not by this crate.
    #[allow(dead_code)]
    Surface(surface::Action<Message>),
}

impl NotificationCenter {
    fn close_popup(&mut self) -> app::Task<Message> {
        match self.popup.take() {
            Some(p) => destroy_popup(p),
            None => Task::none(),
        }
    }

    /// Run a bus call off the UI loop. Without a connection (subscription not up yet) it is a no-op.
    fn call<F, Fut>(&self, what: &'static str, f: F) -> app::Task<Message>
    where
        F: FnOnce(Connection) -> Fut + Send + 'static,
        Fut: Future<Output = zbus::Result<()>> + Send + 'static,
    {
        let Some(conn) = self.conn.clone() else {
            return Task::none();
        };
        cosmic::task::future(async move {
            if let Err(e) = f(conn).await {
                eprintln!("cosmic-applet-notification-center: {what} failed: {e}");
            }
            Message::Done
        })
    }

    fn panel_content(&self) -> Element<'_, Message> {
        let applet = &self.core.applet;
        let status = &self.status;
        let (icon_size, _) = applet.suggested_size(true);
        let pad = applet.suggested_padding(true).0;

        let bell: Element<'_, Message> = cosmic::widget::icon::from_name(status.icon())
            .fallback(Some(cosmic::widget::icon::IconFallback::Names(vec![
                status.fallback_icon().into(),
            ])))
            .symbolic(true)
            .size(icon_size)
            .icon()
            .into();
        let content: Element<'_, Message> = match status.badge() {
            Some(badge) if applet.is_horizontal() => row![bell, applet.text(badge)]
                .spacing(2)
                .align_y(Alignment::Center)
                .into(),
            Some(badge) => column![bell, applet.text(badge)]
                .spacing(2)
                .align_x(Alignment::Center)
                .into(),
            None => bell,
        };

        let mut btn = button::custom(content)
            .padding(if applet.is_horizontal() {
                [0, pad]
            } else {
                [pad, 0]
            })
            .class(cosmic::theme::Button::AppletIcon);
        // No press handler while the daemon is away: the button renders in its greyed state.
        if status.online {
            btn = btn.on_press_down(Message::ToggleShade);
        }

        let mut area = mouse_area(btn);
        if status.online {
            area = area
                .on_right_press(Message::TogglePopup)
                .on_middle_press(Message::TogglePopup);
        }
        applet
            .applet_tooltip(
                area,
                status.tooltip(),
                self.popup.is_some(),
                Message::Surface,
                None,
            )
            .into()
    }

    fn row_button(item: &HistoryItem) -> Element<'_, Message> {
        let mut label =
            text::body(item.label()).wrapping(cosmic::iced::widget::text::Wrapping::None);
        if item.is_unread() {
            label = label.font(cosmic::font::bold());
        }
        button::custom(label)
            .class(cosmic::theme::Button::MenuItem)
            .width(Length::Fill)
            .on_press(Message::OpenNotification(item.id))
            .into()
    }
}

impl cosmic::Application for NotificationCenter {
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
            NotificationCenter {
                core,
                popup: None,
                status: Status::offline(),
                conn: None,
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
        struct BusWatch;
        Subscription::run_with(TypeId::of::<BusWatch>(), |_| {
            cosmic::iced::stream::channel(
                16,
                |mut out: cosmic::iced::futures::channel::mpsc::Sender<Message>| async move {
                    // Reconnect only if the bus connection itself ended; daemon restarts are handled
                    // inside `bus::updates` by the name-owner signal.
                    loop {
                        match bus::connect().await {
                            Ok(conn) => {
                                _ = out.send(Message::Connected(conn.clone())).await;
                                match bus::updates(&conn).await {
                                    Ok(mut updates) => {
                                        while let Some(st) = updates.next().await {
                                            _ = out.send(Message::Status(st)).await;
                                        }
                                    }
                                    Err(e) => eprintln!(
                                        "cosmic-applet-notification-center: subscribe failed: {e}"
                                    ),
                                }
                            }
                            Err(e) => {
                                eprintln!(
                                    "cosmic-applet-notification-center: bus connect failed: {e}"
                                )
                            }
                        }
                        _ = out.send(Message::Status(Status::offline())).await;
                        tokio::time::sleep(Duration::from_secs(5)).await;
                    }
                },
            )
        })
    }

    fn update(&mut self, message: Message) -> app::Task<Message> {
        match message {
            Message::ToggleShade => {
                let close = self.close_popup();
                // Optimistic: the daemon marks everything seen when the shade opens, and its
                // PropertiesChanged confirms. After the call the state is refetched, so a click
                // that closed the shade (no change on the daemon) cannot leave a stale 0.
                self.status.unseen = 0;
                let Some(conn) = self.conn.clone() else {
                    return close;
                };
                return Task::batch([
                    close,
                    cosmic::task::future(async move {
                        // Older daemons without ToggleShade: fall back to the window.
                        if let Err(e) = bus::toggle_shade_or_center(&conn).await {
                            eprintln!("cosmic-applet-notification-center: ToggleShade failed: {e}");
                        }
                        Message::Status(bus::snapshot(&conn).await)
                    }),
                ]);
            }
            Message::TogglePopup => {
                if self.popup.is_some() {
                    return self.close_popup();
                }
                // Looking at the popup counts as seeing.
                self.status.unseen = 0;
                let seen = self.call("MarkSeen", |c| async move { bus::mark_seen(&c).await });
                let popup = cosmic::surface::surface_task(cosmic::surface::action::app_popup(
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
                return Task::batch([seen, popup]);
            }
            Message::CloseRequested(id) => {
                if self.popup == Some(id) {
                    self.popup = None;
                }
            }
            Message::Connected(conn) => self.conn = Some(conn),
            Message::Status(s) => self.status = s,
            Message::SetDnd(on) => {
                // Optimistic; the daemon's PropertiesChanged then confirms or corrects it.
                self.status.dnd = on;
                return self.call("set DoNotDisturb", move |c| async move {
                    bus::set_dnd(&c, on).await
                });
            }
            Message::OpenCenter => {
                let close = self.close_popup();
                return Task::batch([
                    close,
                    self.call("ShowCenter", |c| async move { bus::show_center(&c).await }),
                ]);
            }
            Message::OpenNotification(id) => {
                let close = self.close_popup();
                return Task::batch([
                    close,
                    self.call("ShowNotification", move |c| async move {
                        // Older daemons without ShowNotification: fall back to the window itself.
                        if bus::show_notification(&c, id).await.is_err() {
                            bus::show_center(&c).await?;
                        }
                        Ok(())
                    }),
                ]);
            }
            Message::Done => {}
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
        let mut list = cosmic::widget::column::with_capacity(self.status.recent.len() + 1);
        if self.status.recent.is_empty() {
            list = list.push(text::caption("No notifications"));
        }
        for item in &self.status.recent {
            list = list.push(Self::row_button(item));
        }
        let body = cosmic::widget::column::with_children(vec![
            text::heading(self.status.header()).into(),
            cosmic::widget::divider::horizontal::default().into(),
            list.into(),
            cosmic::widget::divider::horizontal::default().into(),
            toggler(self.status.dnd)
                .label("Do Not Disturb".to_owned())
                .on_toggle(Message::SetDnd)
                .into(),
            button::standard("Open notification center")
                .on_press(Message::OpenCenter)
                .width(Length::Fill)
                .into(),
        ])
        .spacing(spacing.space_xs);
        self.core
            .applet
            .popup_container(
                cosmic::widget::container(body)
                    .padding(spacing.space_s)
                    .width(Length::Fixed(POPUP_WIDTH)),
            )
            .into()
    }
}
