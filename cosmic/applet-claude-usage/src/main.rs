// SPDX-License-Identifier: MIT

mod app;
mod bars;
mod poller;

fn main() -> cosmic::iced::Result {
    // Status-line mode runs on every Claude Code reply: no GUI init, no output, never fails.
    if std::env::args().nth(1).as_deref() == Some("--statusline") {
        claude_usage_core::feed::run_statusline();
        return Ok(());
    }
    cosmic::applet::run::<app::ClaudeUsage>(())
}
