// SPDX-License-Identifier: MIT

mod app;
mod bus;
mod model;

fn main() -> cosmic::iced::Result {
    cosmic::applet::run::<app::NotificationCenter>(())
}
