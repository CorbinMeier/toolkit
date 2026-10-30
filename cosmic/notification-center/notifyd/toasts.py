"""Toast host: one layer-shell OVERLAY surface holding one WebView with the whole stack.

Everything that needs no display (payload building, icon resolution, expiry bookkeeping,
message dispatch) lives in plain methods/classes so tests run without GTK windows. The window
and WebView are built lazily on the first `show`.

JS -> Python: script message handler `nc`, messages
  {type: "action", id, key} | {type: "closed", id} | {type: "link", href}
  | {type: "details", id} | {type: "button", id, button} | {type: "hover", inside: bool}
  | {type: "height", width, height, rects: [[x, y, w, h], ...]}
  | {type: "reply_choice", id, choice} | {type: "reply_text", id, text}
  | {type: "reply_confirm", id, token} | {type: "reply_cancel", id, token}
  | {type: "reply_focus", id, focused: bool}
A "button" message (a rule button, by its id such as "b0") is checked against the buttons the
toast was shown with, then handed to `on_button(id, button)`; the host never closes the toast
for it (the server decides, from the button's `close`). The payload carries `buttons_enabled`
(false while `on_button` is None; the page then hides the buttons).
Reply actions (`notifyd.reply_ui`): the four reply_* messages are validated and routed to
`on_reply_choice/on_reply_text/on_reply_confirm/on_reply_cancel` (None: the page gets no
`reply`); the outcome returns as window.nc.replyResult(obj). Two things hold a
toast open (no expiry): its reply text box has focus (`reply_focus`) or a confirmation is pending
(the pending_confirm result, until confirm/cancel/refusal). Released, an expired toast lingers
REPLY_LINGER_MS and closes. Keyboard: the surface takes NONE keyboard mode and refuses focus;
only while some toast shows an editable reply text box it is ON_DEMAND and accepts focus
(`_sync_keyboard`), and back to NONE as soon as the last such toast is gone.
Python -> JS: window.nc.show(obj) / window.nc.close(id) / window.nc.setUserCss(text)
  / window.nc.replyResult(obj) / window.nc.imageResult({id, kind, ok})
Image previews (`notifyd.image_ui`): `show` adds `image` ({uri, mime, name} or null) to the payload;
the page's `image_copy` / `image_copy_path` / `image_open` messages (active toast ids only) go to
`self.images` and never dismiss the toast, run an action or touch the hover hold.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from notifyd import icons
from notifyd.image_ui import KINDS as IMAGE_KINDS
from notifyd.image_ui import ImageService, register_scheme
from notifyd.model import CloseReason, Notification, to_web
from notifyd.reply_ui import ReplyRouter, clean_reply, text_input_open
from notifyd.sanitize import ALLOWED_SCHEMES, body_runs

ANCHORS = ("top-left", "top-right", "bottom-left", "bottom-right", "top-center", "bottom-center")
ICON_SIZE = icons.SIZE_TOAST
INITIAL_SIZE = (420, 1)
REPLY_LINGER_MS = 2500  # a toast whose expiry waited on a reply closes this long after release
log = logging.getLogger("notifyd")


def parse_anchor(anchor: str) -> tuple[str, str]:
    """'top-right' -> ('top', 'right'). Unknown values fall back to top-right."""
    if anchor not in ANCHORS:
        return ("top", "right")
    v, h = anchor.split("-")
    return (v, h)


def file_uri(path: str) -> str:
    return "file://" + quote(path, safe="/")


def resolve_icon(
    app_icon: str,
    hints: dict[str, Any],
    theme_lookup: Callable[[str], str | None],
    app_name: str = "",
) -> str:
    """The icon as a file:// URI, or "" when nothing resolves (see `notifyd.icons`)."""
    return icons.resolve(app_name, app_icon, hints, theme_lookup) or ""


def build_payload(n: Notification, icon_uri: str) -> dict[str, Any]:
    """Sanitize the body, build the web dict, and swap the raw icon for the resolved one."""
    payload = to_web(n, body_runs(n.body))
    payload["icon"] = icon_uri
    payload["reply"] = clean_reply(payload.get("reply"))
    return payload


def teardown_webview(ucm: Any, handler_id: Any, view: Any, windows: tuple[Any, ...]) -> None:
    """Explicit WebView teardown for `teardown()`: each step runs on its own, failures are logged.

    Disconnects the script handler, unregisters "nc", `try_close()`s the view where WebKit has
    it, then destroys the windows. Safe with None for every part (never built).
    """
    steps: list[tuple[str, Callable[[], Any]]] = []
    if ucm is not None:
        if handler_id is not None:
            steps.append(("disconnect handler", lambda: ucm.disconnect(handler_id)))
        steps.append(("unregister handler", lambda: ucm.unregister_script_message_handler("nc")))
    if view is not None and hasattr(view, "try_close"):
        steps.append(("view try_close", view.try_close))
    for win in windows:
        if win is not None:
            steps.append(("window destroy", win.destroy))
    for what, fn in steps:
        try:
            fn()
        except Exception:
            log.exception("teardown: %s failed", what)


REPLY_KINDS = ("reply_choice", "reply_text", "reply_confirm", "reply_cancel", "reply_focus")


def link_allowed(uri: str) -> bool:
    try:
        return urlsplit(uri).scheme.lower() in ALLOWED_SCHEMES
    except ValueError:
        return False


class ExpiryTimers:
    """One-shot timers keyed by notification id. `schedule`/`cancel` are injectable for tests."""

    def __init__(
        self,
        schedule: Callable[[int, Callable[[], None]], Any] | None = None,
        cancel: Callable[[Any], None] | None = None,
    ) -> None:
        if schedule is None or cancel is None:
            from gi.repository import GLib

            def schedule(ms: int, fn: Callable[[], None]) -> Any:
                return GLib.timeout_add(ms, lambda: (fn(), False)[1])

            cancel = GLib.source_remove
        self._schedule = schedule
        self._cancel = cancel
        self._handles: dict[int, Any] = {}

    def start(self, id: int, ms: int | None, fn: Callable[[int], None]) -> None:
        """(Re)start the timer for `id`. None cancels it and leaves the toast sticky."""
        self.cancel(id)
        if ms is None:
            return
        self._handles[id] = self._schedule(max(1, int(ms)), lambda: self._fire(id, fn))

    def _fire(self, id: int, fn: Callable[[int], None]) -> None:
        self._handles.pop(id, None)
        fn(id)

    def cancel(self, id: int) -> None:
        handle = self._handles.pop(id, None)
        if handle is not None:
            self._cancel(handle)

    def pending(self) -> set[int]:
        return set(self._handles)


class HoverHold:
    """Hover state machine: no toast closes while the pointer is over the stack.

    A toast whose expiry fires while held is deferred (`expired` returns False). When the pointer
    leaves, the hold lasts `grace_ms` more; re-entering during the grace holds again. When the
    grace runs out, every deferred toast is passed to `release`. Timer injection as in
    ExpiryTimers, so this runs without GTK.

    Sources: "gtk" (enter/leave-notify-event, the source of truth), "js" (page mouseenter/leave,
    secondary), "safety", "hide", "region", "empty" (internal releases). A leave from any source
    releases. Safety net: when the pointer has been inside for `max_hold_ms` with no `touch()`
    (motion/enter refresh), the hold is released, so a missed leave can never pin toasts forever.
    0 disables it. Every state change is logged at INFO with the toast count.
    """

    GRACE_KEY = 0  # ExpiryTimers key for the grace timer (notification ids are > 0)
    SAFETY_KEY = -1  # ... and for the safety timer

    def __init__(
        self,
        release: Callable[[int], None],
        grace_ms: int = 1000,
        schedule: Callable[[int, Callable[[], None]], Any] | None = None,
        cancel: Callable[[Any], None] | None = None,
        max_hold_ms: int = 120000,
        now: Callable[[], float] = time.monotonic,
        count: Callable[[], int] | None = None,
    ) -> None:
        self.grace_ms = grace_ms
        self.max_hold_ms = max_hold_ms
        self._release = release
        self._now = now
        self._count = count or (lambda: 0)
        self._timers = ExpiryTimers(schedule, cancel)
        self._inside = False
        self._touched = 0.0
        self._deferred: list[int] = []

    @property
    def held(self) -> bool:
        return self._inside or self.GRACE_KEY in self._timers.pending()

    @property
    def inside(self) -> bool:
        return self._inside

    # ---- inputs -----------------------------------------------------------------------

    def gtk_crossing(self, entering: bool, inferior: bool = False) -> None:
        """enter/leave-notify-event. An INFERIOR leave (window -> child WebView) is no leave."""
        if inferior and not entering:
            self.touch()  # still inside, just moved into the child
            return
        self.set_inside(entering, "gtk")  # an INFERIOR enter means the pointer is inside

    def set_inside(self, inside: bool, source: str = "js") -> None:
        if inside == self._inside:
            if inside:
                self.touch()
            return
        self._inside = inside
        if inside:
            self._timers.cancel(self.GRACE_KEY)
            self.touch()
            log.info("hover enter source=%s toasts=%d", source, self._count())
        else:
            self._timers.cancel(self.SAFETY_KEY)
            self._timers.start(self.GRACE_KEY, self.grace_ms, self._grace_over)
            log.info(
                "hover leave source=%s toasts=%d deferred=%d grace_ms=%d",
                source,
                self._count(),
                len(self._deferred),
                self.grace_ms,
            )

    def touch(self) -> None:
        """Pointer activity (enter/motion): push the safety release out."""
        if not self._inside:
            return
        self._touched = self._now()
        if self.max_hold_ms > 0 and self.SAFETY_KEY not in self._timers.pending():
            self._timers.start(self.SAFETY_KEY, self.max_hold_ms, self._safety_fire)

    def _safety_fire(self, _key: int) -> None:
        if not self._inside:
            return
        if self.max_hold_ms <= 0:
            return
        remaining = self.max_hold_ms - (self._now() - self._touched) * 1000
        if remaining > 1:  # activity since the timer was armed: wait out the rest
            self._timers.start(self.SAFETY_KEY, int(remaining), self._safety_fire)
            return
        log.warning(
            "hover safety release: no pointer activity for %d ms, leave never arrived toasts=%d",
            self.max_hold_ms,
            self._count(),
        )
        self.set_inside(False, "safety")

    def set_max_hold(self, ms: int) -> None:
        self.max_hold_ms = ms
        self._timers.cancel(self.SAFETY_KEY)
        if self._inside:
            self.touch()

    def release_now(self, source: str) -> None:
        """Drop the hold with no grace (window hidden, pointer no longer over any toast)."""
        if not (self._inside or self.GRACE_KEY in self._timers.pending()):
            return
        was_inside = self._inside
        self._inside = False
        self._timers.cancel(self.GRACE_KEY)
        self._timers.cancel(self.SAFETY_KEY)
        log.info(
            "hover release source=%s toasts=%d inside=%s deferred=%d",
            source,
            self._count(),
            was_inside,
            len(self._deferred),
        )
        self._grace_over(self.GRACE_KEY)

    def _grace_over(self, _key: int) -> None:
        ids, self._deferred = self._deferred, []
        if ids:
            log.info("hover grace over: releasing %d deferred toast(s)", len(ids))
        for id in ids:
            self._release(id)

    def expired(self, id: int) -> bool:
        """True when `id` may close now. False: deferred until the hold ends."""
        if not self.held:
            return True
        if id not in self._deferred:
            self._deferred.append(id)
        return False

    def forget(self, id: int) -> None:
        if id in self._deferred:
            self._deferred.remove(id)

    def reset(self) -> None:
        """No toasts left: drop the hover state (the pointer's leave may never be reported)."""
        if self._inside:
            log.info("hover release source=empty toasts=0")
        self._inside = False
        self._deferred = []
        self._timers.cancel(self.GRACE_KEY)
        self._timers.cancel(self.SAFETY_KEY)

    def deferred(self) -> list[int]:
        return list(self._deferred)


class ToastHost:
    def __init__(
        self,
        web_dir: Path,
        on_action: Callable[[int, str], None],
        on_closed: Callable[[int, CloseReason], None],
        anchor: str = "top-right",
        margin: int = 12,
        on_details: Callable[[int], None] | None = None,
        hover_leave_grace_ms: int = 1000,
        hover_max_hold_ms: int = 120000,
        on_button: Callable[[int, str], None] | None = None,
        on_reply_choice: Callable[[int, str], dict] | None = None,
        on_reply_text: Callable[[int, str], dict] | None = None,
        on_reply_confirm: Callable[[int, str], Any] | None = None,
        on_reply_cancel: Callable[[int, str], Any] | None = None,
    ) -> None:
        self.web_dir = Path(web_dir)
        self.on_action = on_action
        self.on_closed = on_closed
        self.on_details = on_details  # None: no details page yet, a body click dismisses
        self.on_button = on_button  # None: rule buttons are hidden on the page
        self.on_reply_choice = on_reply_choice  # all four None: the reply block is hidden
        self.on_reply_text = on_reply_text
        self.on_reply_confirm = on_reply_confirm
        self.on_reply_cancel = on_reply_cancel
        self.images: ImageService | None = None  # set by build_daemon; None: no previews
        self._replies: dict[int, dict] = {}  # id -> cleaned `reply` the toast was shown with
        self._reply_focus: set[int] = set()  # reply text box focused
        self._reply_confirm: set[int] = set()  # confirmation pending
        self._reply_deferred: set[int] = set()  # expiry reached while a reply held the toast
        self._keyboard = False  # surface is in ON_DEMAND keyboard mode
        self._router = ReplyRouter(self, self._reply_lookup, lambda js: self._eval(js))
        self._hover = HoverHold(
            self._hover_release,
            hover_leave_grace_ms,
            max_hold_ms=hover_max_hold_ms,
            count=lambda: len(self._active),
        )
        self._pointer: tuple[float, float] | None = None
        self._rects: list[tuple[int, ...]] = []
        self.anchor = anchor
        self.margin = margin
        self._active: set[int] = set()
        self._buttons: dict[int, set[str]] = {}  # id -> button ids it was shown with
        self._timers = None
        self._user_css: str | None = None
        self._ready = False
        self._pending_js: list[str] = []
        self._window: Any = None
        self._view: Any = None
        self._ucm: Any = None
        self._ucm_handler: Any = None
        self._closed = False
        self._size = (0, 0)

    # ---- public API -------------------------------------------------------------------

    def show(self, n: Notification) -> None:
        self._ensure_view()
        payload = build_payload(
            n, resolve_icon(n.app_icon, n.hints, self._theme_lookup, n.app_name)
        )
        payload["buttons_enabled"] = self.on_button is not None
        payload["reply"] = self._router.present(payload["reply"])
        payload["image"] = self.images.info(n) if self.images is not None else None
        self._buttons[n.id] = {b["id"] for b in payload["buttons"]}
        self._active.add(n.id)
        self._hover.forget(n.id)  # a replace restarts the lifetime
        self._reply_forget(n.id)  # the page rebuilt the block: its focus and confirm are gone
        if payload["reply"] is not None:
            self._replies[n.id] = payload["reply"]
        self._timers_obj().start(n.id, n.timeout_ms, self._expire)
        self._sync_keyboard()
        self._reveal()
        self._eval(f"window.nc.show({_js(payload)})")

    def close(self, id: int) -> None:
        """Remove toast `id` without firing callbacks (the server already knows)."""
        self._remove(id)

    def teardown(self) -> None:
        """Tear the whole host down (timers, script handler, WebView, window).

        Idempotent, safe when never shown; the host never builds a window afterwards.
        """
        if self._closed:
            return
        self._closed = True
        if self._timers is not None:
            for tid in list(self._active):
                self._timers.cancel(tid)
        self._active.clear()
        self._buttons.clear()
        self._replies.clear()
        self._reply_focus.clear()
        self._reply_confirm.clear()
        self._reply_deferred.clear()
        self._keyboard = False
        self._hover.reset()
        ucm, handler, view, win = self._ucm, self._ucm_handler, self._view, self._window
        self._ucm = self._ucm_handler = self._view = self._window = None
        self._ready = False
        self._pending_js = []
        teardown_webview(ucm, handler, view, (win,))

    def close_all_expired(self) -> list[int]:
        """Remove every toast on screen, sticky and hovered ones included, without callbacks.

        Returns the ids so the caller reports each as EXPIRED exactly once (the shade just
        opened and now lists them). Timers and hover state are cleaned by `_remove`.
        """
        ids = sorted(self._active)
        for id in ids:
            self._remove(id)
        return ids

    def set_hover_grace(self, ms: int) -> None:
        """Applies from the next pointer leave."""
        self._hover.grace_ms = ms

    def set_hover_max_hold(self, ms: int) -> None:
        """Safety net: release a hover with no pointer activity for this long (0 = off)."""
        self._hover.set_max_hold(ms)

    def set_user_css(self, css_text: str) -> None:
        self._user_css = css_text
        self._eval(f"window.nc.setUserCss({_js(css_text)})")

    # ---- logic without a display ------------------------------------------------------

    def _timers_obj(self) -> ExpiryTimers:
        if self._timers is None:
            self._timers = ExpiryTimers()
        return self._timers

    def _remove(self, id: int) -> bool:
        """Drop `id` everywhere. True if it was active."""
        self._timers_obj().cancel(id)
        self._hover.forget(id)
        was = id in self._active
        self._active.discard(id)
        self._buttons.pop(id, None)
        self._reply_forget(id)
        self._sync_keyboard()
        if was and not self._active:
            self._hover.reset()
        if was:
            self._eval(f"window.nc.close({int(id)})")
        return was

    def _expire(self, id: int) -> None:
        """Timer ran out: close unless a reply holds it or the stack is hovered (then when the
        hold ends)."""
        if id not in self._active:
            return
        if self._reply_held(id):
            self._reply_deferred.add(id)
        elif self._hover.expired(id):
            self._expire_now(id)

    def _hover_release(self, id: int) -> None:
        """The hover hold ended for a deferred toast: a reply may still be holding it."""
        if id in self._active and self._reply_held(id):
            self._reply_deferred.add(id)
            return
        self._expire_now(id)

    # ---- reply actions: hold, keyboard ------------------------------------------------

    def _reply_lookup(self, id: int) -> dict | None:
        return self._replies.get(id) if id in self._active else None

    def _reply_held(self, id: int) -> bool:
        return id in self._reply_focus or id in self._reply_confirm

    def _reply_forget(self, id: int) -> None:
        self._replies.pop(id, None)
        self._reply_focus.discard(id)
        self._reply_confirm.discard(id)
        self._reply_deferred.discard(id)

    def _reply_check_release(self, id: int) -> None:
        """After a hold ended: a toast whose expiry already passed now gets a short linger."""
        if id in self._active and id in self._reply_deferred and not self._reply_held(id):
            self._reply_deferred.discard(id)
            self._timers_obj().start(id, REPLY_LINGER_MS, self._expire)

    def wants_keyboard(self) -> bool:
        """True while some toast on screen has an editable reply text box."""
        return any(text_input_open(self._replies.get(i)) for i in self._active)

    def _sync_keyboard(self) -> None:
        want = self.wants_keyboard()
        if want != self._keyboard:
            self._keyboard = want
            self._apply_keyboard(want)
            log.info("toasts keyboard mode %s", "ON_DEMAND" if want else "NONE")

    def _apply_keyboard(self, on: bool) -> None:
        """GTK side of `_sync_keyboard`: ON_DEMAND plus focus acceptance, or NONE and none."""
        if self._window is None or self._view is None:
            return
        from gi.repository import GtkLayerShell

        mode = GtkLayerShell.KeyboardMode
        GtkLayerShell.set_keyboard_mode(self._window, mode.ON_DEMAND if on else mode.NONE)
        self._window.set_accept_focus(on)
        self._view.set_can_focus(on)

    def _on_reply_message(self, kind: str, msg: dict) -> None:
        id = msg.get("id")
        if kind == "reply_focus":
            focused = msg.get("focused")
            if (
                isinstance(focused, bool)
                and isinstance(id, int)
                and not isinstance(id, bool)
                and id in self._active
            ):
                if focused and text_input_open(self._replies.get(id)):
                    self._reply_focus.add(id)
                else:
                    self._reply_focus.discard(id)
                    self._reply_check_release(id)
            return
        out = self._router.handle(msg)
        if out is None or id not in self._active:
            return
        if out["status"] == "pending_confirm":
            self._reply_confirm.add(id)
        else:  # sent, refused or cancelled: nothing is pending any more
            self._reply_confirm.discard(id)
            self._reply_check_release(id)

    def _expire_now(self, id: int) -> None:
        if self._remove(id):
            self.on_closed(id, CloseReason.EXPIRED)

    def _on_message(self, msg: Any) -> None:
        """Dispatch one decoded message from the page. Untrusted: validate everything."""
        if not isinstance(msg, dict):
            return
        kind = msg.get("type")
        id = msg.get("id")
        if kind == "height":
            self._on_height(msg)
        elif kind == "hover":
            inside = msg.get("inside")
            if isinstance(inside, bool):
                self._hover.set_inside(inside, "js")
        elif kind in REPLY_KINDS:
            self._on_reply_message(kind, msg)
        elif kind == "link":
            href = msg.get("href")
            if isinstance(href, str) and link_allowed(href):
                self._open_link(href)
        elif isinstance(id, int) and not isinstance(id, bool) and id in self._active:
            if kind == "action" and isinstance(msg.get("key"), str):
                self._remove(id)
                self.on_action(id, msg["key"])
                self.on_closed(id, CloseReason.DISMISSED)
            elif kind == "closed":
                self._remove(id)
                self.on_closed(id, CloseReason.DISMISSED)
            elif kind == "button":
                button = msg.get("button")
                if (
                    self.on_button is not None
                    and isinstance(button, str)
                    and button in self._buttons.get(id, ())
                ):
                    self.on_button(id, button)  # the toast stays; the server may close it
            elif kind in IMAGE_KINDS:
                if self.images is not None:
                    self.images.handle(msg, self._eval)  # the toast stays: no dismiss, no action
            elif kind == "details":
                if self.on_details is not None:
                    self.on_details(id)  # the toast stays open
                else:  # no details page wired: behave as before, the click dismisses
                    self._remove(id)
                    self.on_closed(id, CloseReason.DISMISSED)

    def _on_height(self, msg: dict) -> None:
        w, h = msg.get("width"), msg.get("height")
        if not all(isinstance(v, int | float) and not isinstance(v, bool) for v in (w, h)):
            return
        w, h = min(max(0, int(w)), 4000), min(max(0, int(h)), 8000)
        rects = [
            tuple(int(c) for c in r)
            for r in msg.get("rects", [])
            if isinstance(r, list) and len(r) == 4 and all(isinstance(c, int | float) for c in r)
        ]
        self._apply_size(w, h, rects)

    # ---- GTK side (needs a display) ---------------------------------------------------

    # Hover truth is GTK's enter/leave-notify-event. WebKit does not reliably synthesize DOM
    # mouseleave when the pointer leaves a layer-shell surface (input region = toast rects), so
    # the page's messages are only a secondary signal. Wayland gives no reliable global pointer
    # query, so "pointer left the toasts" is known from crossing events and the last motion
    # position only (see `_pointer_in_rects`).

    def _on_gtk_crossing(self, _w: Any, event: Any) -> bool:
        from gi.repository import Gdk

        entering = event.type == Gdk.EventType.ENTER_NOTIFY
        inferior = event.detail == Gdk.NotifyType.INFERIOR
        self._gtk_crossing(entering, inferior, (event.x, event.y))
        return False

    def _on_gtk_motion(self, _w: Any, event: Any) -> bool:
        self._gtk_motion((event.x, event.y))
        return False

    def _gtk_crossing(
        self, entering: bool, inferior: bool = False, pos: tuple[float, float] | None = None
    ) -> None:
        if not inferior:
            self._pointer = pos if entering else None
        self._hover.gtk_crossing(entering, inferior)

    def _gtk_motion(self, pos: tuple[float, float]) -> None:
        self._pointer = pos
        if not self._hover.inside:
            self._hover.set_inside(True, "gtk")  # motion without an enter: we are over it
        else:
            self._hover.touch()

    def _pointer_in_rects(self) -> bool:
        if self._pointer is None:
            return True  # unknown: keep the hold (the crossing events / safety net decide)
        px, py = self._pointer
        return any(x <= px < x + w and y <= py < y + h for x, y, w, h, *_ in self._rects)

    def _on_window_hide(self, *_a: Any) -> None:
        self._pointer = None
        self._hover.release_now("hide")

    def _theme_lookup(self, name: str) -> str | None:
        return icons.gtk_theme_lookup(ICON_SIZE)(name)

    def _open_link(self, href: str) -> None:
        try:
            from gi.repository import Gio

            Gio.AppInfo.launch_default_for_uri(href, None)
        except Exception as exc:
            print(f"toasts: could not open link: {exc}", file=sys.stderr)

    def _eval(self, script: str) -> None:
        if self._closed:
            return
        if not self._ready or self._view is None:
            self._pending_js.append(script)
            return
        self._run(script)

    def _run(self, script: str) -> None:
        self._view.evaluate_javascript(script, -1, None, None, None, None, None)

    def _reveal(self) -> None:
        """Map the surface (at a minimal size) so the page lays out and reports its height."""
        if self._window is not None and not self._window.get_visible():
            w, h = self._size if self._size[1] else INITIAL_SIZE
            self._resize(w or INITIAL_SIZE[0], h)
            self._window.show_all()

    def _resize(self, w: int, h: int) -> None:
        self._view.set_size_request(w, h)
        self._window.resize(w, h)

    def _apply_size(self, w: int, h: int, rects: list[tuple[int, ...]]) -> None:
        if self._window is None:
            return
        if h <= 0 or not self._active:
            self._size = (0, 0)
            self._rects = []
            self._window.hide()
            return
        self._size = (w, h)
        self._resize(w, h)
        self._rects = rects
        if not self._window.get_visible():
            self._window.show_all()
        self._set_input_region(rects)
        if self._hover.inside and not self._pointer_in_rects():
            self._hover.release_now("region")

    def _set_input_region(self, rects: list[tuple[int, ...]]) -> None:
        """Clicks outside the toasts (shadow room, gaps) fall through to the window below."""
        gdk_win = self._window.get_window()
        if gdk_win is None:
            return
        try:
            import cairo

            region = cairo.Region()
            for x, y, w, h in rects:
                region.union(cairo.RectangleInt(x, y, w, h))
            gdk_win.input_shape_combine_region(region, 0, 0)
        except Exception as exc:
            print(f"toasts: input region unavailable: {exc}", file=sys.stderr)

    def _ensure_view(self) -> None:
        if self._window is not None or self._closed:
            return
        import gi

        gi.require_version("Gdk", "3.0")
        gi.require_version("Gtk", "3.0")
        gi.require_version("GtkLayerShell", "0.1")
        gi.require_version("WebKit2", "4.1")
        gi.require_version("JavaScriptCore", "4.1")
        from gi.repository import Gdk, Gtk, GtkLayerShell, JavaScriptCore, WebKit2  # noqa: F401

        if Gdk.Display.get_default() is None:
            raise RuntimeError("no Wayland display (is WAYLAND_DISPLAY set?)")

        win = Gtk.Window()
        GtkLayerShell.init_for_window(win)
        GtkLayerShell.set_layer(win, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_namespace(win, "net.corbinmeier.notifications.toasts")
        # NONE and no focus: toasts never take the keyboard. `_apply_keyboard` flips both only
        # while a toast shows a reply text box.
        GtkLayerShell.set_keyboard_mode(win, GtkLayerShell.KeyboardMode.NONE)
        GtkLayerShell.set_exclusive_zone(win, -1)
        vert, horiz = parse_anchor(self.anchor)
        edges = {"top": GtkLayerShell.Edge.TOP, "bottom": GtkLayerShell.Edge.BOTTOM}
        edges |= {"left": GtkLayerShell.Edge.LEFT, "right": GtkLayerShell.Edge.RIGHT}
        for edge in (vert, horiz):
            if edge in edges:  # "center" anchors nothing on that axis
                GtkLayerShell.set_anchor(win, edges[edge], True)
                GtkLayerShell.set_margin(win, edges[edge], self.margin)

        win.set_app_paintable(True)
        if (rgba := win.get_screen().get_rgba_visual()) is not None:
            win.set_visual(rgba)
        win.set_accept_focus(False)
        win.add_events(HOVER_MASK(Gdk))
        win.connect("enter-notify-event", self._on_gtk_crossing)
        win.connect("leave-notify-event", self._on_gtk_crossing)
        win.connect("motion-notify-event", self._on_gtk_motion)
        win.connect("hide", self._on_window_hide)

        if self.images is not None:
            register_scheme(self.images)  # before the view exists; once per process
        ucm = WebKit2.UserContentManager()
        ucm.register_script_message_handler("nc")
        self._ucm_handler = ucm.connect("script-message-received::nc", self._on_script_message)
        self._ucm = ucm
        view = WebKit2.WebView.new_with_user_content_manager(ucm)
        view.set_background_color(Gdk.RGBA(0, 0, 0, 0))
        view.set_can_focus(False)
        view.add_events(HOVER_MASK(Gdk))
        view.connect("enter-notify-event", self._on_gtk_crossing)
        view.connect("leave-notify-event", self._on_gtk_crossing)
        view.connect("motion-notify-event", self._on_gtk_motion)
        settings = view.get_settings()
        settings.set_enable_javascript(True)
        settings.set_allow_file_access_from_file_urls(False)
        settings.set_allow_universal_access_from_file_urls(False)
        view.connect("context-menu", lambda *_a: True)
        view.connect("decide-policy", self._on_decide_policy)
        view.connect("load-changed", self._on_load_changed)

        self._page_uri = file_uri(str((self.web_dir / "toasts.html").resolve()))
        self._window, self._view = win, view
        win.add(view)
        view.show()
        view.load_uri(self._page_uri)

    def _on_script_message(self, _ucm: Any, value: Any) -> None:
        try:
            if hasattr(value, "get_js_value"):  # legacy JavascriptResult wrapper
                value = value.get_js_value()
            msg = json.loads(value.to_json(0))
        except Exception as exc:
            print(f"toasts: bad page message: {exc}", file=sys.stderr)
            return
        self._on_message(msg)

    def _on_load_changed(self, _view: Any, event: Any) -> None:
        from gi.repository import WebKit2

        if event != WebKit2.LoadEvent.FINISHED or self._ready:
            return
        self._ready = True
        if self._user_css is not None:
            self._run(f"window.nc.setUserCss({_js(self._user_css)})")
        pending, self._pending_js = self._pending_js, []
        for script in pending:
            if "setUserCss" in script and self._user_css is not None:
                continue  # already applied above with the latest text
            self._run(script)

    def _on_decide_policy(self, _view: Any, decision: Any, dtype: Any) -> bool:
        """The WebView never navigates away from our own page."""
        from gi.repository import WebKit2

        if dtype == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri()
            if uri == self._page_uri:
                decision.use()
            else:
                decision.ignore()
            return True
        if dtype == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
            decision.ignore()
            return True
        return False  # responses etc.: default handling


def HOVER_MASK(Gdk: Any) -> Any:
    return (
        Gdk.EventMask.ENTER_NOTIFY_MASK
        | Gdk.EventMask.LEAVE_NOTIFY_MASK
        | Gdk.EventMask.POINTER_MOTION_MASK
    )


def _js(value: Any) -> str:
    """A JS literal for `value`; ASCII-only so no line-separator surprises."""
    return json.dumps(value, ensure_ascii=True)


__all__ = ["ToastHost", "ExpiryTimers", "HoverHold", "build_payload", "resolve_icon"]
