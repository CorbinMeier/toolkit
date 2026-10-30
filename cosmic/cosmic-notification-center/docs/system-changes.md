# System changes

Every change made outside this repo, with the command that reverts it.
`scripts/install.sh` and `scripts/uninstall.sh` append rows here; nothing else writes this table.

| date | change | file | revert command |
|---|---|---|---|
| 2026-10-25 | create shim (installed by scripts/install.sh) | `~/.local/bin/cosmic-notifications` | `scripts/uninstall.sh --activate` (or `rm ~/.local/bin/cosmic-notifications && killall cosmic-notifications`) |
| 2026-10-25 | create config dir and default config.toml | `~/.config/net.corbinmeier.notifications/` | `scripts/uninstall.sh --purge` (or `rm -r ~/.config/net.corbinmeier.notifications`) |
| 2026-10-25 | create data dir (history.db appears on first run) | `~/.local/share/net.corbinmeier.notifications/` | `scripts/uninstall.sh --purge` (or `rm -r ~/.local/share/net.corbinmeier.notifications`) |
| 2026-10-25 | create shim (installed by scripts/install.sh) | `~/.local/bin/cosmic-notifications` | `scripts/uninstall.sh --activate` (or `rm ~/.local/bin/cosmic-notifications && killall cosmic-notifications`) |
| 2026-10-25 | create config dir and default config.toml | `~/.config/net.corbinmeier.notifications/` | `scripts/uninstall.sh --purge` (or `rm -r ~/.config/net.corbinmeier.notifications`) |
| 2026-10-25 | create default rules.toml (never overwritten) | `~/.config/net.corbinmeier.notifications/rules.toml` | `rm ~/.config/net.corbinmeier.notifications/rules.toml` |
| 2026-10-25 | create default style.css (never overwritten) | `~/.config/net.corbinmeier.notifications/style.css` | `rm ~/.config/net.corbinmeier.notifications/style.css` |
| 2026-10-25 | create data dir (history.db appears on first run) | `~/.local/share/net.corbinmeier.notifications/` | `scripts/uninstall.sh --purge` (or `rm -r ~/.local/share/net.corbinmeier.notifications`) |
| 2026-10-25 | create shim (installed by scripts/install.sh) | `~/.local/bin/cosmic-notifications` | `scripts/uninstall.sh --activate` (or `rm ~/.local/bin/cosmic-notifications && killall cosmic-notifications`) |
| 2026-10-25 | create config dir and default config.toml | `~/.config/net.corbinmeier.notifications/` | `scripts/uninstall.sh --purge` (or `rm -r ~/.config/net.corbinmeier.notifications`) |
| 2026-10-25 | create data dir (history.db appears on first run) | `~/.local/share/net.corbinmeier.notifications/` | `scripts/uninstall.sh --purge` (or `rm -r ~/.local/share/net.corbinmeier.notifications`) |
| 2026-10-25 | install panel applet binary (scripts/install-applet.sh) | `~/.local/bin/cosmic-applet-notification-center` | `scripts/uninstall-applet.sh` (or `rm ~/.local/bin/cosmic-applet-notification-center`) |
| 2026-10-25 | install panel applet desktop entry | `~/.local/share/applications/net.corbinmeier.CosmicAppletNotifications.desktop` | `scripts/uninstall-applet.sh` (or `rm ~/.local/share/applications/net.corbinmeier.CosmicAppletNotifications.desktop`) |
| 2026-10-25 | install launcher desktop entry (scripts/install-launcher.sh) | `~/.local/share/applications/net.corbinmeier.NotificationCenter.desktop` | `scripts/uninstall-launcher.sh` (or `rm ~/.local/share/applications/net.corbinmeier.NotificationCenter.desktop`) |
| 2026-10-25 | install launcher icon (scripts/install-launcher.sh) | `~/.local/share/icons/hicolor/scalable/apps/net.corbinmeier.NotificationCenter.svg` | `scripts/uninstall-launcher.sh` (or `rm ~/.local/share/icons/hicolor/scalable/apps/net.corbinmeier.NotificationCenter.svg`) |
| 2026-10-25 | install launcher symbolic icon (scripts/install-launcher.sh) | `~/.local/share/icons/hicolor/symbolic/apps/net.corbinmeier.NotificationCenter-symbolic.svg` | `scripts/uninstall-launcher.sh` (or `rm ~/.local/share/icons/hicolor/symbolic/apps/net.corbinmeier.NotificationCenter-symbolic.svg`) |
| 2026-10-25 | install panel applet binary (scripts/install-applet.sh) | `~/.local/bin/cosmic-applet-notification-center` | `scripts/uninstall-applet.sh` (or `rm ~/.local/bin/cosmic-applet-notification-center`) |
| 2026-10-25 | install panel applet desktop entry | `~/.local/share/applications/net.corbinmeier.CosmicAppletNotifications.desktop` | `scripts/uninstall-applet.sh` (or `rm ~/.local/share/applications/net.corbinmeier.CosmicAppletNotifications.desktop`) |
| 2026-10-26 | install panel applet binary (scripts/install-applet.sh) | `~/.local/bin/cosmic-applet-notification-center` | `scripts/uninstall-applet.sh` (or `rm ~/.local/bin/cosmic-applet-notification-center`) |
| 2026-10-26 | install panel applet desktop entry | `~/.local/share/applications/net.corbinmeier.CosmicAppletNotifications.desktop` | `scripts/uninstall-applet.sh` (or `rm ~/.local/share/applications/net.corbinmeier.CosmicAppletNotifications.desktop`) |
| 2026-10-26 | install panel applet binary (scripts/install-applet.sh) | `~/.local/bin/cosmic-applet-notification-center` | `scripts/uninstall-applet.sh` (or `rm ~/.local/bin/cosmic-applet-notification-center`) |
| 2026-10-26 | install panel applet desktop entry | `~/.local/share/applications/net.corbinmeier.CosmicAppletNotifications.desktop` | `scripts/uninstall-applet.sh` (or `rm ~/.local/share/applications/net.corbinmeier.CosmicAppletNotifications.desktop`) |
| 2026-10-26 | install applet icon (scripts/install-applet.sh) | `~/.local/share/icons/hicolor/symbolic/apps/net.corbinmeier.NotificationCenter-symbolic.svg` | `scripts/uninstall-applet.sh` (or `rm ~/.local/share/icons/hicolor/symbolic/apps/net.corbinmeier.NotificationCenter-symbolic.svg`) |
| 2026-10-26 | install applet icon (scripts/install-applet.sh) | `~/.local/share/icons/hicolor/symbolic/apps/net.corbinmeier.NotificationCenter-new-symbolic.svg` | `scripts/uninstall-applet.sh` (or `rm ~/.local/share/icons/hicolor/symbolic/apps/net.corbinmeier.NotificationCenter-new-symbolic.svg`) |
| 2026-10-26 | install applet icon (scripts/install-applet.sh) | `~/.local/share/icons/hicolor/symbolic/apps/net.corbinmeier.NotificationCenter-dnd-symbolic.svg` | `scripts/uninstall-applet.sh` (or `rm ~/.local/share/icons/hicolor/symbolic/apps/net.corbinmeier.NotificationCenter-dnd-symbolic.svg`) |
