// SPDX-License-Identifier: MIT

mod app;
mod sound;
mod timer;

fn main() -> cosmic::iced::Result {
    cosmic::applet::run::<app::Timer>(())
}
