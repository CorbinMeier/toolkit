"""Daemon entry point: store + D-Bus server + toast host, owning both bus names.

Exit codes: 0 clean shutdown (SIGTERM/SIGINT), 2 name not acquired within 10 s or lost later
(the shim's restart loop retries), 1 startup failure.

Decisions where the brief was silent:
- Inherited fds are never touched (in particular DAEMON_NOTIFICATIONS_FD stays open).
- `--no-display` swaps the toast host for a null display (store + bus only) for tests and
  headless runs.
- Config files live in the config dir (parent of --config): rules.toml, app-settings.toml
  (UI-managed, applied before rules.toml), style.css, config.toml. A Gio.FileMonitor on the
  DIRECTORY (editors save by rename) feeds one ~300 ms debounce; on firing, the changed files
  are re-read and only what changed is applied.
- A reload that fails keeps the previous state: rules.toml and app-settings.toml keep their
  old state; an unusable config.toml keeps the old [sound] values. Failures are logged and
  signalled (RulesReloaded false).
- `[toasts]` timeouts, `respect_app_timeout` and `hover_leave_grace_ms` apply live (to
  notifications that arrive after the reload). `anchor`, `margin` and `[history]` changes are
  logged as "restart required" and not applied.
- `[sound] default` absent (or commented out) means the system default sound.
- The center window is built by a factory that receives the server (its callbacks are the
  server's shared operations). `--no-display` uses `NullCenter`.
- `--show-center` / `--show-notification ID` are bus clients: they call the running daemon and
  exit without starting one (exit 1 if no daemon answers).
- The shade is built by `shade_factory(server, runtime)`; `--no-display` uses `NullShade`.
  `shade_callbacks` is the one place its callbacks are defined: `on_dismiss` receives open and
  EXPIRED-closed ids mixed, open ones go through `server.dismiss` (signal + toast close), expired
  ones through `store.mark_dismissed_quiet` (no signal). The shade follows the server through
  observers (added / changed / DND); DND set from the shade goes through `server.set_dnd`, so the
  property signal fires, and the observer's `shade.set_dnd` is idempotent (no loop).
- Theme layering: toasts, shade and center get `theme.css + "\n" + style.css` (after default.css,
  which the pages link). Both files are watched. The customization panel's backend is
  `settings.SettingsService`, assigned as `center.settings` by `build_daemon`.
- `[shade] anchor`, `margin`, `width` are validated individually and applied live
  (`shade.configure`). Toast `anchor`/`margin` still need a restart.
- `--no-sound` swaps the SoundPlayer for a `NullPlayer`; `--no-display` implies it.
- Opening the shade (show, or a toggle that ends visible) closes every on-screen toast as EXPIRED
  via `server.clear_toasts_to_shade` (`[shade] clear_toasts_on_open`, live, default true).
- `--toggle-shade` is a bus client like `--show-center`.
- `--restart` (notifyd/restart.py) is a bus client that SIGTERMs only the daemon's Python process
  (found via GetConnectionUnixProcessID; refused unless cmdline is `/usr/bin/python3 -m notifyd`
  under our shim) and waits for a new pid to own both names. No `Restart()` bus method: the PID
  path is enough, and a method would skip the shim check.
- Workflow: `build_daemon` assigns `on_button` on display/shade/center to `server.press_button`
  and `center.get_runs` to `store.runs_for`; the Runner's `on_finished` refreshes center and
  shade. `--no-run` runs no scripts (rule or button); `--no-display` alone keeps the real Runner.
  The clipboard (Gtk) and link opener (Gio) are injected into the server; `--no-display` gets
  no-ops. Snoozed notifications are re-shown by `server.check_snoozes`, called once at startup
  and every 30 s. The runner is shut down (children TERMed, rows finished) before the store closes.
  `--press-button ID BUTTON` is a bus client like `--show-center`.
- Reply actions: `http-actions.toml` (the allowlist, notifyd/http_actions.py) is watched like the
  other config files; `Runtime.reload_http_actions` re-reads it (a bad file fails closed and is
  signalled as RulesReloaded false) and `Runtime.on_http_reloaded` (the server's `ui_changed`)
  refreshes the UIs. `build_daemon` assigns `on_reply_choice/_text/_confirm/_cancel` on the
  display, shade and center. `--reply-choice ID CHOICE` / `--reply-text ID TEXT` are bus clients
  that print the JSON result (exit 0 only for "sent"). `--no-run` does not affect them.
- Config values are validated one by one; a bad key falls back to its default, a file that does
  not parse falls back to all defaults. Both log an error and never crash.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from notifyd import icons  # noqa: E402
from notifyd.app_settings import AppPrefs, AppSettings, AppSettingsError  # noqa: E402
from notifyd.config import ANCHORS, SHADE_WIDTH_RANGE, Config, load_config  # noqa: E402,F401
from notifyd.dbus_server import CTL_IFACE, CTL_PATH, FDO_IFACE, NotificationServer  # noqa: E402
from notifyd.http_actions import HttpActions  # noqa: E402
from notifyd.image_ui import ImageService  # noqa: E402
from notifyd.rules import RulesError, RuleSet  # noqa: E402
from notifyd.runner import Runner  # noqa: E402
from notifyd.seen import DEFAULT_STATE_DIR, SeenMark  # noqa: E402
from notifyd.settings import SettingsService  # noqa: E402
from notifyd.sound import NullPlayer, SoundPlayer, choose  # noqa: E402
from notifyd.store import Store  # noqa: E402

log = logging.getLogger("notifyd")

APP_DIR = "net.corbinmeier.notifications"
DEFAULT_DB = f"~/.local/share/{APP_DIR}/history.db"
DEFAULT_CONFIG = f"~/.config/{APP_DIR}/config.toml"
NAME_TIMEOUT_S = 10
PREWARM_DELAY_S = 3  # after both names are acquired; the shade follows a second later
RELOAD_DEBOUNCE_MS = 300
WATCHED_FILES = (
    "rules.toml",
    "app-settings.toml",
    "theme.css",
    "style.css",
    "config.toml",
    "http-actions.toml",
)
PRUNE_INTERVAL_S = 86400
SNOOZE_CHECK_S = 30
EXIT_NAME_LOST = 2


class NullDisplay:
    """Display stand-in for --no-display: records nothing, shows nothing."""

    def __init__(self) -> None:
        self.on_action = lambda id, key: None
        self.on_closed = lambda id, reason: None
        self.on_details = lambda id: None
        self.on_button = lambda id, button_id: None

    def set_hover_grace(self, ms: int) -> None:
        pass

    def set_hover_max_hold(self, ms: int) -> None:
        pass

    def show(self, n) -> None:
        pass

    def close(self, id: int) -> None:
        pass

    def teardown(self) -> None:
        pass

    def close_all_expired(self) -> list[int]:
        return []

    def set_user_css(self, css_text: str) -> None:
        pass


class NullCenter:
    """Center stand-in for --no-display: the same methods, all no-ops."""

    def show(self) -> None:
        pass

    def hide(self) -> None:
        pass

    def teardown(self) -> None:
        pass

    def toggle(self) -> None:
        pass

    def show_notification(self, id: int) -> None:
        pass

    def notify_added(self, n) -> None:
        pass

    def refresh(self) -> None:
        pass

    def set_user_css(self, css_text: str) -> None:
        pass

    settings = None  # build_daemon assigns the SettingsService


class NullShade:
    """Shade stand-in for --no-display: the same methods, all no-ops."""

    def show(self) -> None:
        pass

    def hide(self) -> None:
        pass

    def teardown(self) -> None:
        pass

    def toggle(self) -> None:
        pass

    def is_visible(self) -> bool:
        return False

    def notify_added(self, n=None) -> None:
        pass

    def refresh(self) -> None:
        pass

    def set_user_css(self, css_text: str) -> None:
        pass

    def set_dnd(self, on: bool) -> None:
        pass

    def configure(self, *, anchor=None, margin=None, width=None) -> None:
        pass


PUMP_MAX_S = 1.0
PUMP_MAX_ITER = 1000


def pump_gtk(max_s: float = PUMP_MAX_S, max_iter: int = PUMP_MAX_ITER) -> int:
    """Run pending GTK events (so destroys and WebKit teardown finish), at most `max_s` seconds
    and `max_iter` iterations. Returns the iterations run."""
    from gi.repository import Gtk

    deadline = time.monotonic() + max_s
    n = 0
    while n < max_iter and time.monotonic() < deadline and Gtk.events_pending():
        Gtk.main_iteration_do(False)
        n += 1
    return n


def shutdown_daemon(
    *,
    monitor,
    runner,
    windows: list,
    pump,
    release_bus,
    store,
) -> None:
    """Everything after the main loop returns, in order: config monitor, runner, windows
    (`teardown()` each), event pump, bus (unown names, unregister), store. A failing step is
    logged and never stops the later ones, `store.close` included."""

    def attempt(what: str, fn) -> None:
        try:
            fn()
        except Exception:
            log.exception("shutdown: %s failed", what)

    if monitor is not None:
        attempt("config monitor", monitor.cancel)
    if runner is not None:
        attempt("runner", runner.shutdown)
    for name, window in windows:
        attempt(f"teardown {name}", window.teardown)
    if pump is not None:
        attempt("event pump", pump)
    attempt("bus release", release_bus)
    attempt("store", store.close)


def gtk_clipboard(text: str) -> None:
    """Put `text` on the Wayland/X clipboard and ask the clipboard manager to keep it."""
    import gi

    gi.require_version("Gdk", "3.0")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gdk, Gtk

    cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    cb.set_text(text, -1)
    cb.store()


def launch_uri(uri: str) -> None:
    Gio.AppInfo.launch_default_for_uri(uri, None)


def read_style(path: Path) -> str:
    """style.css contents; missing or unreadable means no user CSS."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError) as e:
        log.error("style %s unusable (%s); using no user CSS", path, e)
        return ""


class Runtime:
    """Live-reloadable state: the RuleSet, the SoundPlayer, the user CSS.

    Everything runs on the GLib main loop, so swapping `rules` is atomic for `process`.
    """

    def __init__(self, config_path: Path, display, config: Config, player=None) -> None:
        self.dir = config_path.parent
        self.config_path = config_path
        self.display = display
        self.center = NullCenter()  # run() swaps in the real one after build_daemon
        self.shade = NullShade()  # likewise
        self.config = config
        # Handed to the server; reload_config updates it in place for later notifications.
        self.timeouts = config.timeout_policy()
        self.rules = RuleSet.empty()
        self.rules_error: str | None = None
        # Per-app settings (app-settings.toml): applied before `rules`, which therefore wins.
        self.app_settings = AppSettings()
        self.app_rules = RuleSet.empty()
        self.app_settings_error: str | None = None
        self._app_bytes: bytes | None = None  # file content last applied (load or our own save)
        self.player = (
            player
            if player is not None
            else SoundPlayer(config.sound_default, config.master_volume)
        )
        self._last_sound_error: str | None = None
        self.on_rules_reloaded = lambda ok, error: None
        self.http_actions = HttpActions(
            config_path.parent
        )  # run() hands the same one to the server
        self.on_http_reloaded = lambda: None

    def load_initial_rules(self) -> None:
        try:
            self.rules = RuleSet.load(self.dir / "rules.toml")
            log.info("loaded %d rules", len(self.rules))
        except RulesError as e:
            self.rules_error = str(e)
            log.error("rules.toml: %s; starting with no rules", e)

    @property
    def app_settings_path(self) -> Path:
        return self.dir / "app-settings.toml"

    def _read_app_file(self) -> bytes | None:
        try:
            return self.app_settings_path.read_bytes()
        except FileNotFoundError:
            return None

    def _load_app_settings(self) -> tuple[AppSettings, RuleSet, bytes | None]:
        raw = self._read_app_file()
        if raw is None:
            return AppSettings(), RuleSet.empty(), None
        try:
            settings = AppSettings.loads(raw.decode("utf-8"))
        except UnicodeDecodeError as e:
            raise AppSettingsError(f"not valid UTF-8: {e}") from e
        return settings, settings.to_ruleset(), raw

    def load_initial_app_settings(self) -> None:
        try:
            self.app_settings, self.app_rules, self._app_bytes = self._load_app_settings()
            log.info("loaded app settings for %d apps", len(self.app_settings))
        except AppSettingsError as e:
            self.app_settings_error = f"app-settings.toml: {e}"
            log.error("%s; starting with no app settings", self.app_settings_error)

    def reload_app_settings(self) -> None:
        try:
            raw = self._read_app_file()
            if raw == self._app_bytes:
                if self.app_settings_error is not None:  # a bad edit was reverted
                    self.app_settings_error = None
                    self.on_rules_reloaded(True, "")
                return  # our own write, or an event with no content change
            settings, rules, raw = self._load_app_settings()
        except (AppSettingsError, OSError) as e:
            msg = f"app-settings.toml: {e}"
            self.app_settings_error = msg
            log.error("%s; keeping previous app settings", msg)
            self.on_rules_reloaded(False, msg)
            return
        self.app_settings, self.app_rules, self._app_bytes = settings, rules, raw
        self.app_settings_error = None
        log.info("app-settings.toml reloaded: %d apps", len(settings))
        self.on_rules_reloaded(True, "")

    def get_app_prefs(self, app: str) -> dict:
        return self.app_settings.get(app).to_dict()

    def set_app_prefs(self, app: str, prefs: dict) -> None:
        """Validate, save atomically, apply now. Raises ValueError (bad input) or OSError."""
        parsed = AppPrefs.from_dict(prefs)
        if parsed.icon is not None:
            err = icons.icon_error(parsed.icon, icons.gtk_theme_lookup(icons.SIZE_SHADE))
            if err:
                raise ValueError(err)
        new = self.app_settings.copy()
        new.set(app, parsed)
        rules = new.to_ruleset()
        new.save(self.app_settings_path)  # raises before any swap if the write fails
        self.app_settings, self.app_rules = new, rules
        self._app_bytes = self._read_app_file()  # the monitor event for this write is a no-op
        self.app_settings_error = None
        log.info("app settings saved for %r", app)

    def app_prefs_apps(self) -> list[str]:
        return self.app_settings.apps()

    def process(self, n):
        return self.rules.apply(self.app_rules.apply(n))

    def play(self, n) -> None:
        self.player.play(*choose(n))
        err = self.player.last_error
        if err and err != self._last_sound_error:
            self._last_sound_error = err
            log.error("sound: %s", err)

    def apply_style(self) -> None:
        # Layers after default.css (which the pages link): theme.css, then style.css (wins).
        css = read_style(self.dir / "theme.css") + "\n" + read_style(self.dir / "style.css")
        self.display.set_user_css(css)
        self.center.set_user_css(css)
        self.shade.set_user_css(css)
        log.info("theme.css + style.css applied (%d bytes)", len(css))

    def reload_rules(self) -> None:
        try:
            new = RuleSet.load(self.dir / "rules.toml")
        except RulesError as e:
            self.rules_error = str(e)
            log.error("rules.toml reload failed, keeping previous rules: %s", e)
            self.on_rules_reloaded(False, str(e))
            return
        self.rules = new
        self.rules_error = None
        log.info("rules.toml reloaded: %d rules", len(new))
        self.on_rules_reloaded(True, "")

    def reload_http_actions(self) -> None:
        err = self.http_actions.load()
        self.on_rules_reloaded(err is None, err or "")
        self.on_http_reloaded()  # the UIs re-read what is allowed

    def reload_config(self) -> None:
        new = load_config(self.config_path)
        old = self.config
        if (new.sound_default, new.master_volume) != (old.sound_default, old.master_volume):
            self.player.set_default(new.sound_default)
            self.player.set_master_volume(new.master_volume)
            log.info(
                "config.toml reloaded: sound default=%r master_volume=%d",
                new.sound_default,
                new.master_volume,
            )
        else:
            log.info("config.toml reloaded: [sound] unchanged")
        new_timeouts = new.timeout_policy()
        if (
            new_timeouts != self.timeouts
            or new.hover_leave_grace_ms != old.hover_leave_grace_ms
            or new.hover_max_hold_ms != old.hover_max_hold_ms
        ):
            self.timeouts.update(new_timeouts)
            self.display.set_hover_grace(new.hover_leave_grace_ms)
            self.display.set_hover_max_hold(new.hover_max_hold_ms)
            log.info(
                "config.toml reloaded: toast timeouts low=%s normal=%s critical=%s "
                "respect_app_timeout=%s hover_leave_grace_ms=%d hover_max_hold_ms=%d",
                new.timeout_low,
                new.timeout_normal,
                new.timeout_critical,
                new.respect_app_timeout,
                new.hover_leave_grace_ms,
                new.hover_max_hold_ms,
            )
        if (new.anchor, new.margin, new.retention_days, new.prewarm) != (
            old.anchor,
            old.margin,
            old.retention_days,
            old.prewarm,
        ):
            log.warning(
                "config.toml [toasts]/[history]/[windows] changed: restart required: "
                "run `%s --restart`",
                RESTART_CMD,
            )
        shade_now = (new.shade_anchor, new.shade_margin, new.shade_width)
        if shade_now != (old.shade_anchor, old.shade_margin, old.shade_width):
            self.shade.configure(
                anchor=new.shade_anchor, margin=new.shade_margin, width=new.shade_width
            )
            old.shade_anchor, old.shade_margin, old.shade_width = shade_now
            log.info("config.toml reloaded: shade anchor=%s margin=%d width=%d", *shade_now)
        # Keep the running anchor/margin/history values; [sound] and the timeouts are live.
        old.shade_clear_toasts_on_open = new.shade_clear_toasts_on_open
        old.images_preview = new.images_preview  # read live by the ImageService
        old.sound_default, old.master_volume = new.sound_default, new.master_volume
        old.timeout_low, old.timeout_normal = new.timeout_low, new.timeout_normal
        old.timeout_critical = new.timeout_critical
        old.respect_app_timeout = new.respect_app_timeout
        old.hover_leave_grace_ms = new.hover_leave_grace_ms
        old.hover_max_hold_ms = new.hover_max_hold_ms

    def watch(self) -> Gio.FileMonitor:
        """Monitor the config DIRECTORY (editors save via rename) with a 300 ms debounce."""
        pending: set[str] = set()
        timer = {"id": 0}

        def fire() -> bool:
            timer["id"] = 0
            names = set(pending)
            pending.clear()
            for name, action in (
                ("rules.toml", self.reload_rules),
                ("app-settings.toml", self.reload_app_settings),
                ("theme.css", self.apply_style),
                ("style.css", self.apply_style),
                ("config.toml", self.reload_config),
                ("http-actions.toml", self.reload_http_actions),
            ):
                if name in names:
                    try:
                        action()
                    except Exception:
                        log.exception("reload of %s failed", name)
            return GLib.SOURCE_REMOVE

        def changed(_mon, file, other, _event):
            names = {f.get_basename() for f in (file, other) if f is not None}
            hit = names & set(WATCHED_FILES)
            if not hit:
                return
            pending.update(hit)
            if timer["id"]:
                GLib.source_remove(timer["id"])
            timer["id"] = GLib.timeout_add(RELOAD_DEBOUNCE_MS, fire)

        monitor = Gio.File.new_for_path(str(self.dir)).monitor_directory(
            Gio.FileMonitorFlags.WATCH_MOVES, None
        )
        monitor.connect("changed", changed)
        return monitor


def _mark_seen_on_open(server, window, methods, visible_attr: str, on_open=None) -> None:
    """Wrap `window.<method>` so that once the window is on screen the server's watermark moves
    to now (UnseenCount -> 0). `visible_attr` is a bool property (center) or a method (shade);
    `toggle` counts only when it ended up visible, `show*` always. `on_open()` runs first in
    the same cases (the shade clears its toasts there)."""

    def is_visible() -> bool:
        v = getattr(window, visible_attr, False)
        return bool(v() if callable(v) else v)

    for name in methods:
        original = getattr(window, name)

        def wrapper(*a, _orig=original, _always=(name != "toggle"), **kw):
            result = _orig(*a, **kw)
            if _always or is_visible():
                if on_open is not None:
                    on_open()
                server.mark_seen()
                mark = getattr(window, "trace_mark", None)
                if mark is not None:
                    mark("tail")  # open-latency line: the handlers after the show returned
            return result

        setattr(window, name, wrapper)


def build_daemon(
    connection,
    store,
    display,
    center_factory=None,
    shade_factory=None,
    runtime=None,
    **server_kwargs,
) -> NotificationServer:
    """Register both interfaces on `connection`; wire the display, center and shade to the server.

    `center_factory(server)` builds the center window (it needs the server's shared operations
    as its callbacks); without one the center is a `NullCenter`. The center is `server.center`.
    `shade_factory(server, runtime)` builds the dashboard shade (`server.shade`; `NullShade`
    without one). The shade is built after the center, which its callbacks use. When a `runtime`
    is given, its `center` and `shade` are set too.
    Does not own any bus name; the caller does.
    """
    server = NotificationServer(connection, store, display, **server_kwargs)
    center = center_factory(server) if center_factory is not None else NullCenter()
    # Opening the center marks everything seen. Wrapped before the shade is built, so the shade's
    # own "Open notification center" / details callbacks and display.on_details get the wrapper.
    _mark_seen_on_open(server, center, ("show", "toggle", "show_notification"), "visible")
    server.center = center
    if runtime is not None:
        runtime.center = center
        # The customization panel's backend; the center bridge calls it by attribute.
        center.settings = SettingsService(runtime, server, runtime.dir)
    shade = shade_factory(server, runtime) if shade_factory is not None else NullShade()
    server.shade = shade

    def clear_toasts() -> None:
        # Live: reads the runtime's current config on every open (no runtime: default on).
        if runtime is None or runtime.config.shade_clear_toasts_on_open:
            server.clear_toasts_to_shade()

    _mark_seen_on_open(server, shade, ("show", "toggle"), "is_visible", clear_toasts)
    if runtime is not None:
        runtime.shade = shade
    display.on_action = server.on_action
    display.on_closed = server.on_closed
    display.on_details = center.show_notification  # option c: a body click without `default`
    display.on_button = server.press_button
    shade.on_button = server.press_button
    center.on_button = server.press_button
    for host in (display, shade, center):  # reply actions: assigned by attribute, like on_button
        host.on_reply_choice = server.reply_choice
        host.on_reply_text = server.reply_text
        host.on_reply_confirm = server.confirm_reply
        host.on_reply_cancel = server.cancel_reply
    # Image previews: one service for all three hosts (an `images` attribute, None = off); it
    # reads `[images] preview` live. Copy path and open use the server's own clipboard/opener.
    images = ImageService(
        store.get,
        enabled=lambda: runtime is None or runtime.config.images_preview,
        copy_text=server_kwargs.get("clipboard"),
        open_uri=server_kwargs.get("opener"),
    )
    for host in (display, shade, center):
        host.images = images
    center.get_runs = store.runs_for
    runner = server_kwargs.get("runner")
    if runner is not None and getattr(runner, "on_finished", None) is None:
        runner.on_finished = server.run_finished
    server.on_toggle_center = center.toggle
    server.on_show_center = center.show
    server.on_toggle_shade = shade.toggle
    server.on_show_shade = shade.show
    server.on_show_notification = center.show_notification
    server.add_added_observer(center.notify_added)
    server.add_added_observer(shade.notify_added)
    server.add_changed_observer(shade.refresh)
    server.add_dnd_observer(shade.set_dnd)
    return server


def dismiss_from_shade(server: NotificationServer, ids: list[int]) -> None:
    """The shade lists open notifications and EXPIRED-closed ones; it passes both here.

    Open ones are dismissed for real (toast closes, NotificationClosed(DISMISSED) is signalled).
    Expired ones are already closed and signalled: they only become DISMISSED in the store.
    """
    open_ids, expired = [], []
    for i in dict.fromkeys(ids):
        n = server.store.get(i)
        if n is None:
            continue
        (open_ids if n.closed_at is None else expired).append(i)
    for i in open_ids:
        server.dismiss(i)
    if expired:
        server.store.mark_dismissed_quiet(expired, server.now())
        server.notify_changed()


def shade_callbacks(server: NotificationServer, runtime) -> dict:
    """The keyword callbacks `ShadeWindow` takes, wired to the server, center and runtime."""
    return {
        "on_dismiss": lambda ids: dismiss_from_shade(server, ids),
        "on_mark_read": server.mark_read,
        "on_open_details": server.center.show_notification,
        "on_open_center": server.center.show,
        "on_invoke_action": server.invoke_action,
        "get_dnd": lambda: server.dnd,
        "on_set_dnd": server.set_dnd,
        "get_app_prefs": runtime.get_app_prefs,
        "on_set_app_prefs": runtime.set_app_prefs,
    }


def make_shade_factory(web_dir: Path):
    from notifyd.shade import ShadeWindow

    def factory(server: NotificationServer, runtime):
        cfg = runtime.config
        return ShadeWindow(
            server.store,
            web_dir,
            anchor=cfg.shade_anchor,
            margin=cfg.shade_margin,
            width=cfg.shade_width,
            **shade_callbacks(server, runtime),
        )

    return factory


def make_center_factory(store, web_dir: Path):
    from notifyd.center import CenterWindow

    def factory(server: NotificationServer):
        return CenterWindow(
            store,
            web_dir,
            on_invoke_action=server.invoke_action,
            on_dismiss=server.dismiss,
            on_mark_read=server.mark_read,
            on_delete=server.delete,
            on_clear=server.clear_history,
        )

    return factory


RESTART_CMD = str(Path(__file__).resolve().parent.parent / "bin" / "notifyd")


def connect(bus_address: str | None) -> Gio.DBusConnection:
    if bus_address is None:
        return Gio.bus_get_sync(Gio.BusType.SESSION, None)
    flags = (
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
    )
    return Gio.DBusConnection.new_for_address_sync(bus_address, flags, None, None)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    root = Path(__file__).resolve().parent.parent
    p = argparse.ArgumentParser(prog="notifyd", description="Notification daemon")
    p.add_argument("--db", default=DEFAULT_DB, help="history database (default %(default)s)")
    p.add_argument("--config", default=DEFAULT_CONFIG, help="config.toml (default %(default)s)")
    p.add_argument(
        "--state-dir",
        default=None,
        help=f"dir for seen.json (default {DEFAULT_STATE_DIR}; with a custom --db, the db's dir)",
    )
    p.add_argument("--web-dir", default=str(root / "web"), help="web assets (default %(default)s)")
    p.add_argument(
        "--bus-address", default=None, help="connect to this bus instead of the session bus"
    )
    p.add_argument("--no-display", action="store_true", help="store + bus only, no toast windows")
    p.add_argument(
        "--show-center", action="store_true", help="toggle the center of the running daemon, exit"
    )
    p.add_argument(
        "--toggle-shade", action="store_true", help="toggle the shade of the running daemon, exit"
    )
    p.add_argument(
        "--no-sound",
        action="store_true",
        help="never play notification sounds (implied by --no-display)",
    )
    p.add_argument(
        "--show-notification",
        type=int,
        metavar="ID",
        help="open the details page of a notification in the running daemon, exit",
    )
    p.add_argument(
        "--no-run",
        action="store_true",
        help="never run rule or button scripts (the Runner is not created)",
    )
    p.add_argument(
        "--press-button",
        nargs=2,
        metavar=("ID", "BUTTON"),
        help="press a button (b0, b1, ...) of a notification in the running daemon, exit",
    )
    p.add_argument(
        "--reply-choice",
        nargs=2,
        metavar=("ID", "CHOICE"),
        help="send a notification's reply choice (x-nc-actions) via the running daemon, exit",
    )
    p.add_argument(
        "--reply-text",
        nargs=2,
        metavar=("ID", "TEXT"),
        help="send a free-text reply (x-nc-actions) via the running daemon, exit",
    )
    p.add_argument(
        "--restart",
        action="store_true",
        help="restart the running daemon (only its Python process; the shim restarts it)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def client_call(args: argparse.Namespace) -> int:
    """--show-center / --toggle-shade / --show-notification: call the running daemon, exit."""
    if args.show_center:
        method, params = "ToggleCenter", None
    elif args.toggle_shade:
        method, params = "ToggleShade", None
    elif args.press_button is not None:
        try:
            nid = int(args.press_button[0])
        except ValueError:
            nid = -1
        if not 0 <= nid <= 2**32 - 1:
            print("notifyd: notification id out of range", file=sys.stderr)
            return 1
        method, params = "PressButton", GLib.Variant("(us)", (nid, args.press_button[1]))
    elif args.reply_choice is not None or args.reply_text is not None:
        return reply_call(args)
    else:
        if not 0 <= args.show_notification <= 2**32 - 1:
            print("notifyd: notification id out of range", file=sys.stderr)
            return 1
        method, params = "ShowNotification", GLib.Variant("(u)", (args.show_notification,))
    try:
        connection = connect(args.bus_address)
        connection.call_sync(
            CTL_IFACE, CTL_PATH, CTL_IFACE, method, params, None, Gio.DBusCallFlags.NONE, 5000, None
        )
        connection.close_sync(None)
    except GLib.Error as e:
        print(f"notifyd: {method} failed (is the daemon running?): {e.message}", file=sys.stderr)
        return 1
    return 0


def reply_call(args: argparse.Namespace) -> int:
    """--reply-choice / --reply-text: ask the running daemon, print its JSON, exit 0 if sent."""
    choice = args.reply_choice is not None
    raw_id, value = args.reply_choice if choice else args.reply_text
    try:
        nid = int(raw_id)
    except ValueError:
        nid = -1
    if not 0 <= nid <= 2**32 - 1:
        print("notifyd: notification id out of range", file=sys.stderr)
        return 1
    method = "ReplyChoice" if choice else "ReplyText"
    try:
        connection = connect(args.bus_address)
        reply = connection.call_sync(
            CTL_IFACE, CTL_PATH, CTL_IFACE, method, GLib.Variant("(us)", (nid, value)),
            GLib.VariantType("(s)"), Gio.DBusCallFlags.NONE, 5000, None,
        )  # fmt: skip
        connection.close_sync(None)
    except GLib.Error as e:
        print(f"notifyd: {method} failed (is the daemon running?): {e.message}", file=sys.stderr)
        return 1
    out = reply.unpack()[0]
    print(out)
    try:
        return 0 if json.loads(out).get("status") == "sent" else 1
    except ValueError:
        return 1


def run(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser()
    config = load_config(config_path)
    store = Store(Path(args.db).expanduser())
    loop = GLib.MainLoop()
    result = {"code": 0, "stopping": False}
    runner = None

    def prune() -> bool:
        try:
            n = store.prune(config.retention_days, time.time())
            if n:
                log.info("pruned %d notifications older than %s days", n, config.retention_days)
                runtime.http_actions.sweep(store.existing_ids)
        except Exception:
            log.exception("prune failed")
        return True

    def stop(code: int) -> None:
        if not result["stopping"]:
            result["stopping"] = True
            result["code"] = code
            loop.quit()

    # Signals and the "starting" line come before any bus work: a peer that sees our name
    # appear may SIGTERM us at once, and the default action would kill us with no log line.
    def on_signal(signum: int):
        log.info("signal %d: shutting down", signum)
        stop(0)
        return GLib.SOURCE_REMOVE

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, on_signal, signal.SIGTERM)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, on_signal, signal.SIGINT)

    log.info(
        "starting (db=%s, display=%s, sound=%s, run=%s)",
        args.db,
        "off" if args.no_display else "on",
        "off" if args.no_display or args.no_sound else "on",
        "off" if args.no_run else "on",
    )
    try:
        center_factory = shade_factory = None
        clipboard, opener = (lambda text: None), (lambda uri: None)
        if args.no_display:
            display = NullDisplay()
        else:
            clipboard, opener = gtk_clipboard, launch_uri
            from notifyd.toasts import ToastHost

            display = ToastHost(
                Path(args.web_dir),
                lambda i, k: None,
                lambda i, r: None,
                anchor=config.anchor,
                margin=config.margin,
                hover_leave_grace_ms=config.hover_leave_grace_ms,
                hover_max_hold_ms=config.hover_max_hold_ms,
            )
            center_factory = make_center_factory(store, Path(args.web_dir))
            shade_factory = make_shade_factory(Path(args.web_dir))
        no_sound = args.no_sound or args.no_display
        runtime = Runtime(config_path, display, config, player=NullPlayer() if no_sound else None)
        runtime.load_initial_rules()
        runtime.load_initial_app_settings()
        connection = connect(args.bus_address)
        runner = None if args.no_run else Runner(store)
        if args.state_dir is not None:
            state_dir = Path(args.state_dir).expanduser()
        elif args.db == DEFAULT_DB:
            state_dir = Path(DEFAULT_STATE_DIR).expanduser()
        else:
            state_dir = Path(args.db).expanduser().parent
        seen = SeenMark(state_dir, time.time())
        server = build_daemon(
            connection,
            store,
            display,
            process=runtime.process,
            sound=runtime.play,
            timeouts=runtime.timeouts,
            center_factory=center_factory,
            shade_factory=shade_factory,
            runtime=runtime,
            runner=runner,
            http_actions=runtime.http_actions,
            clipboard=clipboard,
            opener=opener,
            seen=seen,
        )
        runtime.apply_style()  # toasts and center get the same CSS text
        runtime.on_rules_reloaded = server.emit_rules_reloaded
        runtime.on_http_reloaded = server.ui_changed
        runtime.http_actions.load()
        monitor = None
        if config_path.parent.is_dir():
            monitor = runtime.watch()
        else:
            log.warning("config dir %s missing; hot reload disabled", config_path.parent)
        if runtime.rules_error is not None:
            err = runtime.rules_error

            def announce_rules_error() -> bool:
                server.emit_rules_reloaded(False, err)
                return GLib.SOURCE_REMOVE

            GLib.idle_add(announce_rules_error)
        if runtime.app_settings_error is not None:
            app_err = runtime.app_settings_error

            def announce_app_error() -> bool:
                server.emit_rules_reloaded(False, app_err)
                return GLib.SOURCE_REMOVE

            GLib.idle_add(announce_app_error)
    except Exception:
        log.exception("startup failed")
        if runner is not None:
            runner.shutdown()
        store.close()
        return 1

    prune()
    GLib.timeout_add_seconds(PRUNE_INTERVAL_S, prune)

    def snoozes() -> bool:
        try:
            server.check_snoozes()
        except Exception:
            log.exception("snooze check failed")
        return GLib.SOURCE_CONTINUE

    def snoozes_once() -> bool:
        snoozes()
        return GLib.SOURCE_REMOVE

    GLib.idle_add(snoozes_once)
    GLib.timeout_add_seconds(SNOOZE_CHECK_S, snoozes)

    owned: set[str] = set()
    names = (FDO_IFACE, CTL_IFACE)

    def prewarm(window) -> bool:
        try:
            start = time.monotonic()
            window.prewarm()
            ms = (time.monotonic() - start) * 1000
            log.info("prewarmed %s view in %d ms", window.trace.name, ms)
        except Exception:
            log.exception("prewarm failed")
        return GLib.SOURCE_REMOVE

    def schedule_prewarm() -> None:
        # Hidden builds only (nothing is mapped), staggered so the web processes spawn apart.
        if args.no_display or not runtime.config.prewarm:
            return
        for i, window in enumerate((server.center, server.shade)):
            if hasattr(window, "prewarm"):
                GLib.timeout_add_seconds(PREWARM_DELAY_S + i, prewarm, window)

    def acquired(_conn, name):
        owned.add(name)
        log.info("acquired %s", name)
        if len(owned) == len(names):
            schedule_prewarm()

    def lost(_conn, name):
        if result["stopping"]:
            return
        log.error("lost or could not acquire %s", name)
        stop(EXIT_NAME_LOST)

    owner_ids = [
        Gio.bus_own_name_on_connection(
            connection,
            name,
            Gio.BusNameOwnerFlags.ALLOW_REPLACEMENT | Gio.BusNameOwnerFlags.REPLACE,
            acquired,
            lost,
        )
        for name in names
    ]

    def name_deadline() -> bool:
        if len(owned) < len(names):
            log.error("names not acquired within %d s (have %s)", NAME_TIMEOUT_S, sorted(owned))
            stop(EXIT_NAME_LOST)
        return False

    GLib.timeout_add_seconds(NAME_TIMEOUT_S, name_deadline)

    loop.run()

    def release_bus() -> None:
        for oid in owner_ids:
            Gio.bus_unown_name(oid)
        server.unregister()

    shutdown_daemon(
        monitor=monitor,
        runner=runner,
        windows=[("center", server.center), ("shade", server.shade), ("toasts", display)],
        pump=None if args.no_display else pump_gtk,
        release_bus=release_bus,
        store=store,
    )
    log.info("exit %d", result["code"])
    return result["code"]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="notifyd: %(levelname)s %(name)s: %(message)s",
    )
    if args.restart:
        from notifyd import restart

        return restart.run(args.bus_address, connect)
    if (
        args.show_center
        or args.toggle_shade
        or args.show_notification is not None
        or args.press_button is not None
        or args.reply_choice is not None
        or args.reply_text is not None
    ):
        return client_call(args)
    return run(args)


if __name__ == "__main__":
    # Inherited fds (DAEMON_NOTIFICATIONS_FD etc.) are deliberately left alone.
    sys.exit(main())
