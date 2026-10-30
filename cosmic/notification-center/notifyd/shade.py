"""Dashboard shade: an Android-style drop-down of notifications on a layer-shell surface.

Split so everything but the widgets runs without a display:
- `ShadeState` persists which app groups are expanded (JSON, atomic write, tolerant read).
- `build_groups` / `ShadeBridge` build the payload from the store and validate and route the
  page's messages to the injected callbacks.
- `ShadeSurfaces` is the visibility state machine for the two surfaces. The invariant: the
  click-outside catcher is up only while the shade is up, on every close path and on errors.
- `ShadeWindow` owns the two layer-shell windows and the WebView (built lazily on first show).

Surfaces: the shade is a layer-shell OVERLAY surface (keyboard mode ON_DEMAND, so it takes no
focus until clicked), above a fully transparent TOP-layer catcher anchored to all four edges with
exclusive zone -1 (so it covers panels too). Any button press on the catcher closes the shade.

JS -> Python: script message handler `nc`; the page is untrusted (ids are ints, strings are
length-capped, links are allow-listed). Queries carry an int `req` and are answered with
window.nc.reply(req, obj):
  {type: "state", req}              -> {groups, unread, total, dnd}
  {type: "action", req, id, key}    -> {ok}
  {type: "prefs_get", req, app}     -> {prefs} | {error}
  {type: "prefs_set", req, app, prefs} -> {ok} | {ok: false, error}
  {type: "icon_preview", req, value}   -> {uri} | {uri: "", error}   (the ⚙ card's Icon field)
Commands (no reply):
  {type: "dismiss", ids} | {type: "dismiss_app", app} | {type: "clear_all"}
  {type: "mark_read", ids} | {type: "mark_read_app", app}
  {type: "button", id, button}  (a rule button; checked against the stored buttons, then
                                 `on_button(id, button)`; the shade does not close itself)
  {type: "details", id}  (opens the details page and closes the shade)
  {type: "open_center"}  (opens the center window and closes the shade)
  {type: "expand", app, expanded} | {type: "dnd", on} | {type: "link", href} | {type: "close"}
  {type: "size", width, height, room, rect: [x, y, w, h]}   (the card's layout box)
  {type: "reply_choice", id, choice} | {type: "reply_text", id, text}
  | {type: "reply_confirm", id, token} | {type: "reply_cancel", id, token}
                                (sender-defined reply actions; validated and routed by
                                 `notifyd.reply_ui.ReplyRouter` to the `on_reply_*` callbacks)
Python -> JS: window.nc.refresh() / setDnd(on) / setUserCss(text) / setLimits({maxHeight, width})
  / measure() (after every map: re-report the size)
  / replyResult({id, kind, status, token?, reason?})  (the outcome of a reply_* message)
  / imageResult({id, kind, ok})  (the outcome of an image_* message)
Image previews (`notifyd.image_ui`): each item carries `image` ({uri, mime, name} or null);
  {type: "image_copy" | "image_copy_path" | "image_open", id} go to `ImageService`.

Decisions where the brief was silent (see also the report):
- A collapsed group lists no items: its header shows the newest summary and a 2-line body. An
  expanded group lists every item with its full body.
- The unread count is the number of unread items in the shade, not the global badge count.
- "Clear all" and "dismiss all for an app" cover everything in the shade (up to MAX_IDS), not
  only the 200 rows the list shows.
- The page may only dismiss / mark read ids that are currently in the shade.
- Per-app prefs pass through unchanged; keys outside PREF_KEYS are dropped, and validation
  stays in `on_set_app_prefs` (a ValueError is shown inline).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from notifyd import icons
from notifyd.center import (
    MAX_IDS,
    MAX_TEXT,
    _js,
    _same_page,
    button_press,
    is_unread,
    link_allowed,
    preview,
    valid_id,
    valid_ids,
)
from notifyd.image_ui import KINDS as IMAGE_KINDS
from notifyd.image_ui import ImageService, register_scheme
from notifyd.model import Notification, button_web
from notifyd.opentrace import PAGE_PHASES, SETTLE_MS, OpenTrace, glib_schedule
from notifyd.reply_ui import KINDS as REPLY_KINDS
from notifyd.reply_ui import ReplyRouter, bridge_attr, reply_of
from notifyd.sanitize import body_runs
from notifyd.toasts import file_uri, parse_anchor, resolve_icon, teardown_webview

log = logging.getLogger("notifyd.shade")

DEFAULT_STATE_PATH = Path("~/.local/state/net.corbinmeier.notifications/shade.json").expanduser()
LIST_LIMIT = 200
HEIGHT_FRACTION = 0.8
DEFAULT_HEIGHT = 320  # first map, until the page reports its size (a frame or two)
DEFAULT_ROOM = 20  # transparent margin for the shadow; the page reports the real value
MAX_ROOM = 200
MAX_SIZE = 20000
ICON_SIZE = icons.SIZE_SHADE
PREF_KEYS = ("muted", "silent", "sound", "volume", "sticky", "blocked", "dnd_bypass", "icon")
MAX_PREF_TEXT = 4096
NAMESPACE = "net.corbinmeier.notifications.shade"


# ---- persisted expand state ------------------------------------------------------------------


class ShadeState:
    """Which app groups are expanded, kept in a small JSON file: {"expanded": {app: true}}.

    A missing, unreadable or malformed file means everything is collapsed. Writes are atomic
    (temp file in the same directory, then rename) and a failed write is logged, not raised.
    """

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path).expanduser()
        self._expanded: dict[str, bool] = self._load()

    def _load(self) -> dict[str, bool]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        exp = raw.get("expanded") if isinstance(raw, dict) else None
        if not isinstance(exp, dict):
            return {}
        return {k: True for k, v in exp.items() if isinstance(k, str) and v is True}

    def is_expanded(self, app: str) -> bool:
        return self._expanded.get(app, False)

    def set_expanded(self, app: str, expanded: bool) -> None:
        if expanded == self.is_expanded(app):
            return
        if expanded:
            self._expanded[app] = True
        else:
            self._expanded.pop(app, None)
        self.save()

    def save(self) -> bool:
        tmp: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".shade-", suffix=".tmp", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"expanded": self._expanded}, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            return True
        except OSError as exc:
            log.warning("could not save %s: %s", self.path, exc)
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return False


# ---- payloads --------------------------------------------------------------------------------


def shade_item(n: Notification, icon: str, runs: list[dict] | None = None) -> dict[str, Any]:
    """One row of the shade. `body` is sanitized runs, never raw markup."""
    runs = body_runs(n.body) if runs is None else runs
    return {
        "id": n.id,
        "app": n.app_name,
        "summary": n.summary,
        "body": runs,
        "preview": preview(runs),
        "icon": icon,
        "urgency": n.urgency.name.lower(),
        "state": str(n.state),
        "category": n.category,
        "tags": list(n.tags),
        "classes": list(n.classes),
        "highlights": [dict(h) for h in n.highlights],
        "actions": [{"key": k, "label": label} for k, label in n.actions],
        "buttons": [button_web(b) for b in n.buttons],
        "reply": reply_of(n),
        "received_at": n.received_at,
        "unread": is_unread(n),
        "expired": n.closed_at is not None,
    }


def build_groups(
    items: list[Notification],
    icon_for: Callable[[Notification], str],
    is_expanded: Callable[[str], bool] = lambda app: False,
) -> list[dict[str, Any]]:
    """One group per app, ordered by each group's newest item; items newest first.

    Ordering is by (received_at, id) so equal timestamps stay deterministic. `items` may arrive
    in any order.
    """
    ordered = sorted(items, key=lambda n: (n.received_at, n.id), reverse=True)
    by_app: dict[str, list[Notification]] = {}
    for n in ordered:  # dict keeps first-seen order, i.e. newest group first
        by_app.setdefault(n.app_name, []).append(n)
    groups = []
    for app, rows in by_app.items():
        payload = [shade_item(n, icon_for(n)) for n in rows]
        icon = next((p["icon"] for p in payload if p["icon"]), "")
        groups.append(
            {
                "app": app,
                "icon": icon,
                "count": len(payload),
                "unread": sum(1 for p in payload if p["unread"]),
                "newest_summary": payload[0]["summary"],
                "newest_at": payload[0]["received_at"],
                "expanded": bool(is_expanded(app)),
                "items": payload,
            }
        )
    return groups


def clean_prefs(prefs: Any) -> dict[str, Any] | None:
    """The known keys of a prefs object with sane scalar values, or None when not an object.
    Real validation stays with the consumer (`on_set_app_prefs` raises ValueError)."""
    if not isinstance(prefs, dict):
        return None
    out: dict[str, Any] = {}
    for k in PREF_KEYS:
        if k not in prefs:
            continue
        v = prefs[k]
        if v is None or isinstance(v, bool | int | float):
            out[k] = v
        elif isinstance(v, str) and len(v) <= MAX_PREF_TEXT:
            out[k] = v
        else:
            return None
    return out


def _app(v: Any) -> str | None:
    """A usable app name from the page, or None."""
    return v if isinstance(v, str) and 0 < len(v) <= MAX_TEXT else None


# ---- bridge (no display needed) --------------------------------------------------------------


class ShadeBridge:
    def __init__(
        self,
        store: Any,
        state: ShadeState,
        *,
        on_dismiss: Callable[[list[int]], None],
        on_mark_read: Callable[[list[int]], None],
        on_open_details: Callable[[int], None],
        on_open_center: Callable[[], None],
        on_invoke_action: Callable[[int, str], bool],
        get_dnd: Callable[[], bool],
        on_set_dnd: Callable[[bool], None],
        get_app_prefs: Callable[[str], dict],
        on_set_app_prefs: Callable[[str, dict], None],
        run_js: Callable[[str], None],
        open_link: Callable[[str], None],
        close: Callable[[], None],
        on_size: Callable[[dict], None] | None = None,
        on_perf: Callable[[str], None] | None = None,
        theme_lookup: Callable[[str], str | None] | None = None,
        on_button: Callable[[int, str], None] | None = None,
        on_reply_choice: Callable[[int, str], dict] | None = None,
        on_reply_text: Callable[[int, str], dict] | None = None,
        on_reply_confirm: Callable[[int, str], Any] | None = None,
        on_reply_cancel: Callable[[int, str], Any] | None = None,
    ) -> None:
        self.store = store
        self.state = state
        self.on_button = on_button  # None: the page hides rule buttons
        self.on_reply_choice = on_reply_choice  # all four None: the page gets no `reply`
        self.on_reply_text = on_reply_text
        self.on_reply_confirm = on_reply_confirm
        self.on_reply_cancel = on_reply_cancel
        self.replies = ReplyRouter(self, self._reply_lookup, lambda js: self.run_js(js))
        self.images: ImageService | None = None  # set by build_daemon; None: no previews
        self.on_dismiss = on_dismiss
        self.on_mark_read = on_mark_read
        self.on_open_details = on_open_details
        self.on_open_center = on_open_center
        self.on_invoke_action = on_invoke_action
        self.get_dnd = get_dnd
        self.on_set_dnd = on_set_dnd
        self.get_app_prefs = get_app_prefs
        self.on_set_app_prefs = on_set_app_prefs
        self.run_js = run_js
        self.open_link = open_link
        self.close = close
        self.on_size = on_size
        self.on_perf = on_perf  # None: the page's open-timing reports are dropped
        self._theme_lookup = theme_lookup or (lambda name: None)
        self._icons: dict[tuple[str, ...], str] = {}

    # ---- payload ----

    def icon_for(self, n: Notification) -> str:
        """The file:// URI, or "" (the page then draws a letter avatar). Misses are not cached,
        so a .desktop file installed later is found."""
        h = n.hints
        key = (
            n.app_name.casefold(),
            n.app_icon,
            str(h.get("image-path") or h.get("image_path") or ""),
            str(h.get("desktop-entry") or h.get("desktop_entry") or ""),
        )
        if key not in self._icons:
            uri = resolve_icon(n.app_icon, n.hints, self._theme_lookup, n.app_name)
            if not uri:
                return ""
            self._icons[key] = uri
        return self._icons[key]

    def payload(self) -> dict[str, Any]:
        items = self.store.shade_items(LIST_LIMIT)
        groups = build_groups(items, self.icon_for, self.state.is_expanded)
        by_id = {n.id: n for n in items}
        for g in groups:
            for item in g["items"]:
                item["reply"] = self.replies.present(item["reply"])
                item["image"] = self.images.info(by_id[item["id"]]) if self.images else None
        return {
            "groups": groups,
            "unread": sum(g["unread"] for g in groups),
            "total": sum(g["count"] for g in groups),
            "dnd": bool(self.get_dnd()),
            "buttons_enabled": self.on_button is not None,
        }

    def _reply_lookup(self, id: int) -> dict[str, Any] | None:
        n = self.store.get(id)
        return None if n is None else self.replies.present(reply_of(n))

    def _ids_in_shade(self, app: str | None = None) -> list[int]:
        rows = self.store.shade_items(MAX_IDS)
        return [n.id for n in rows if app is None or n.app_name == app]

    # ---- messages from the page ----

    def reply(self, req: int, data: dict[str, Any]) -> None:
        self.run_js(f"window.nc.reply({int(req)}, {_js(data)})")

    def refresh_page(self) -> None:
        self.run_js("window.nc.refresh()")

    def handle(self, msg: Any) -> None:
        """Dispatch one decoded message. Untrusted: anything malformed is dropped (or, for a
        request with a usable `req`, answered with {error})."""
        if not isinstance(msg, dict):
            return
        log.debug("page -> python: %s", json.dumps(msg, ensure_ascii=False)[:300])
        kind = msg.get("type")
        try:
            if kind in ("state", "action", "prefs_get", "prefs_set", "icon_preview"):
                self._request(kind, msg)
            elif kind in ("dismiss", "mark_read"):
                self._ids_command(kind, valid_ids(msg.get("ids")))
            elif kind in ("dismiss_app", "mark_read_app"):
                app = _app(msg.get("app"))
                if app is not None:
                    self._ids_command(kind.removesuffix("_app"), self._ids_in_shade(app))
            elif kind == "clear_all":
                self._ids_command("dismiss", self._ids_in_shade())
            elif kind == "button":
                self._press_button(msg)
            elif kind in REPLY_KINDS:
                self.replies.handle(msg)
            elif kind in IMAGE_KINDS:
                if self.images is not None:
                    self.images.handle(msg, self.run_js)
            elif kind == "details":
                id = msg.get("id")
                if valid_id(id):
                    self.close()
                    self.on_open_details(id)
            elif kind == "open_center":
                self.close()
                self.on_open_center()
            elif kind == "expand":
                app, on = _app(msg.get("app")), msg.get("expanded")
                if app is not None and isinstance(on, bool):
                    self.state.set_expanded(app, on)
            elif kind == "dnd":
                if isinstance(msg.get("on"), bool):
                    self.on_set_dnd(msg["on"])
            elif kind == "link":
                href = link_allowed(msg.get("href"))
                if href:
                    self.open_link(href)
            elif kind == "close":
                self.close()
            elif kind == "perf":
                if msg.get("phase") in PAGE_PHASES and self.on_perf is not None:
                    self.on_perf(msg["phase"])
            elif kind == "size":
                size = parse_size(msg)
                if size is not None and self.on_size is not None:
                    self.on_size(size)
            else:
                log.warning("ignored page message of type %r", kind)
        except Exception:
            log.exception("page message %r failed", kind)

    def _press_button(self, msg: dict) -> None:
        """A rule button: the id must be a known notification and the button one of its own."""
        if self.on_button is not None and (pair := button_press(self.store, msg)) is not None:
            self.on_button(*pair)

    def _ids_command(self, kind: str, ids: list[int] | None) -> None:
        """Dismiss / mark read the given ids, limited to what the shade currently lists."""
        if not ids:
            return
        shown = {n.id for n in self.store.shade_items(MAX_IDS)}
        ids = [i for i in dict.fromkeys(ids) if i in shown]
        if not ids:
            return
        (self.on_dismiss if kind == "dismiss" else self.on_mark_read)(ids)
        self.refresh_page()

    def _request(self, kind: str, msg: dict) -> None:
        req = msg.get("req")
        if not valid_id(req):
            return
        try:
            self.reply(req, self._answer(kind, msg))
        except Exception as exc:
            log.exception("request %r failed", kind)
            self.reply(req, {"error": str(exc) or exc.__class__.__name__})

    def _answer(self, kind: str, msg: dict) -> dict[str, Any]:
        if kind == "state":
            return self.payload()
        if kind == "action":
            id, key = msg.get("id"), msg.get("key")
            n = self.store.get(id) if valid_id(id) else None
            if not isinstance(key, str) or n is None or key not in {k for k, _ in n.actions}:
                return {"ok": False}
            return {"ok": bool(self.on_invoke_action(id, key))}
        if kind == "icon_preview":
            value = msg.get("value")
            if not isinstance(value, str) or len(value) > MAX_PREF_TEXT:
                return {"uri": "", "error": "bad request"}
            if not value.strip():
                return {"uri": ""}
            path = icons.icon_path(value, self._theme_lookup)
            if path:
                return {"uri": icons.file_uri(path)}
            return {"uri": "", "error": icons.icon_error(value, self._theme_lookup)}
        app = _app(msg.get("app"))
        if app is None:
            return {"error": "bad request"}
        if kind == "prefs_get":
            prefs = clean_prefs(self.get_app_prefs(app))
            return {"error": "settings unavailable"} if prefs is None else {"prefs": prefs}
        prefs = clean_prefs(msg.get("prefs"))  # prefs_set
        if prefs is None:
            return {"ok": False, "error": "bad request"}
        try:
            self.on_set_app_prefs(app, prefs)
        except ValueError as exc:
            return {"ok": False, "error": str(exc) or "invalid settings"}
        return {"ok": True}


def parse_size(msg: dict) -> dict[str, int] | None:
    """The page's layout report as ints, or None when anything is off."""
    nums = []
    for v in (msg.get("width"), msg.get("height"), msg.get("room")):
        if not isinstance(v, int | float) or isinstance(v, bool) or not 0 <= v <= MAX_SIZE:
            return None
        nums.append(int(v))
    rect = msg.get("rect")
    if not isinstance(rect, list) or len(rect) != 4:
        return None
    for v in rect:
        if not isinstance(v, int | float) or isinstance(v, bool) or not 0 <= v <= MAX_SIZE:
            return None
    w, h, room = nums
    return {
        "width": w,
        "height": h,
        "room": min(room, MAX_ROOM),
        "rect": [int(v) for v in rect],  # type: ignore[dict-item]
    }


# ---- visibility state machine (no display needed) ---------------------------------------------


class ShadeSurfaces:
    """Keeps the catcher and the shade in lockstep.

    Invariant: the catcher is up only while the shade is up. `show` raises the catcher first,
    then the shade; if either fails, both are hidden. `hide` always tries both (each in its own
    try) so no failure or re-entrant signal can leave the catcher up alone. Hiding either surface
    from outside (`on_shade_hidden` / `on_catcher_hidden`) closes the other.
    """

    def __init__(
        self,
        show_catcher: Callable[[], None],
        hide_catcher: Callable[[], None],
        show_shade: Callable[[], None],
        hide_shade: Callable[[], None],
    ) -> None:
        self._show_catcher = show_catcher
        self._hide_catcher = hide_catcher
        self._show_shade = show_shade
        self._hide_shade = hide_shade
        self.visible = False
        self._hiding = False

    def show(self) -> bool:
        if self.visible:
            return True
        try:
            self._show_catcher()
            self._show_shade()
        except Exception:
            log.exception("could not show the shade; hiding both surfaces")
            self.hide()
            return False
        self.visible = True
        return True

    def hide(self) -> None:
        if self._hiding:
            return
        self._hiding = True
        try:
            # shade first: the catcher must outlive it by no more than this call
            for fn in (self._hide_shade, self._hide_catcher):
                try:
                    fn()
                except Exception:
                    log.exception("hide failed")
        finally:
            self.visible = False
            self._hiding = False

    def toggle(self) -> None:
        if self.visible:
            self.hide()
        else:
            self.show()

    def on_catcher_press(self) -> None:
        self.hide()

    def on_shade_hidden(self) -> None:
        self.hide()

    def on_catcher_hidden(self) -> None:
        self.hide()


# ---- window (needs a display) ----------------------------------------------------------------


class ShadeWindow:
    def __init__(
        self,
        store: Any,
        web_dir: Path,
        *,
        on_dismiss: Callable[[list[int]], None],
        on_mark_read: Callable[[list[int]], None],
        on_open_details: Callable[[int], None],
        on_open_center: Callable[[], None],
        on_invoke_action: Callable[[int, str], bool],
        get_dnd: Callable[[], bool],
        on_set_dnd: Callable[[bool], None],
        get_app_prefs: Callable[[str], dict],
        on_set_app_prefs: Callable[[str, dict], None],
        anchor: str = "top-right",
        margin: int = 8,
        width: int = 420,
        state_path: Path = DEFAULT_STATE_PATH,
        on_button: Callable[[int, str], None] | None = None,
        on_reply_choice: Callable[[int, str], dict] | None = None,
        on_reply_text: Callable[[int, str], dict] | None = None,
        on_reply_confirm: Callable[[int, str], Any] | None = None,
        on_reply_cancel: Callable[[int, str], Any] | None = None,
    ) -> None:
        self.web_dir = Path(web_dir)
        self.anchor = anchor
        self.margin = margin
        self.width = width
        self.state = ShadeState(state_path)
        self._user_css: str | None = None
        self._ready = False
        self._stale = False
        self._pending_js: list[str] = []
        self._window: Any = None
        self._catcher: Any = None
        self._view: Any = None
        self._ucm: Any = None
        self._ucm_handler: Any = None
        self._closed = False
        self._page_uri = ""
        self._room = DEFAULT_ROOM
        self._rect: list[int] | None = None
        self._size = (0, 0)
        self._mapped = False  # the surface has been on screen once (sizes before that are junk)
        self.trace = OpenTrace(
            "shade", ("rows", "painted"), settle_ms=SETTLE_MS, schedule=glib_schedule
        )
        self.surfaces = ShadeSurfaces(
            self._map_catcher, self._unmap_catcher, self._map_shade, self._unmap_shade
        )
        self._dnd = get_dnd
        self.bridge = ShadeBridge(
            store,
            self.state,
            on_dismiss=on_dismiss,
            on_mark_read=on_mark_read,
            on_open_details=on_open_details,
            on_open_center=on_open_center,
            on_invoke_action=on_invoke_action,
            get_dnd=get_dnd,
            on_set_dnd=on_set_dnd,
            get_app_prefs=get_app_prefs,
            on_set_app_prefs=on_set_app_prefs,
            run_js=self._eval,
            open_link=self._open_link,
            close=self.hide,
            on_size=self._apply_size,
            on_perf=self.trace.mark,
            theme_lookup=self._theme_lookup,
            on_button=on_button,
            on_reply_choice=on_reply_choice,
            on_reply_text=on_reply_text,
            on_reply_confirm=on_reply_confirm,
            on_reply_cancel=on_reply_cancel,
        )

    @property
    def on_button(self) -> Callable[[int, str], None] | None:
        return self.bridge.on_button

    @on_button.setter
    def on_button(self, fn: Callable[[int, str], None] | None) -> None:
        self.bridge.on_button = fn

    on_reply_choice = bridge_attr("on_reply_choice")
    on_reply_text = bridge_attr("on_reply_text")
    on_reply_confirm = bridge_attr("on_reply_confirm")
    on_reply_cancel = bridge_attr("on_reply_cancel")
    images = bridge_attr("images")

    # ---- public API ----

    def show(self) -> None:
        if self._closed:
            return
        self.trace.begin(self._window is None)
        try:
            self._ensure_view()
        except Exception:
            log.exception("could not build the shade")
            self.surfaces.hide()
            return
        # fresh rows and relative times on every open (the first load reads the store itself)
        if self._ready:
            self._run("window.nc.refresh()")
        self._stale = False
        if self.surfaces.show():
            self.trace.mark("present")
            self._eval("window.ncPaint.arm()")
            # sizes reported before the map were dropped: ask the page for a fresh, trusted one
            self._eval("window.nc.measure()")

    def prewarm(self) -> None:
        """Build the surfaces and the WebView hidden (nothing is mapped: no catcher, no input
        grab) so the first open skips the web process spawn and the page load."""
        if self._closed:
            return
        try:
            self._ensure_view()
        except Exception:
            log.exception("could not prewarm the shade")

    def trace_mark(self, phase: str) -> None:
        self.trace.mark(phase)

    def hide(self) -> None:
        self.surfaces.hide()

    def teardown(self) -> None:
        """Tear down the WebView, the shade window and the catcher. Idempotent; the shade
        never builds a window afterwards. Safe when it was never shown."""
        if self._closed:
            return
        self._closed = True
        try:
            self.surfaces.hide()  # while the windows still exist
        except Exception:
            log.exception("teardown: surfaces.hide failed")
        ucm, handler, view = self._ucm, self._ucm_handler, self._view
        win, catcher = self._window, self._catcher
        self._ucm = self._ucm_handler = self._view = None
        self._window = self._catcher = None
        self._ready = False
        self._pending_js = []
        teardown_webview(ucm, handler, view, (win, catcher))

    def toggle(self) -> None:
        if self.is_visible():
            self.hide()
        else:
            self.show()

    def is_visible(self) -> bool:
        return self.surfaces.visible

    def notify_added(self, n: Notification | None = None) -> None:
        """A notification arrived: refresh while open, flag stale while hidden."""
        self.refresh()

    def refresh(self) -> None:
        if self.is_visible():
            self._eval("window.nc.refresh()")
        else:
            self._stale = True

    def set_user_css(self, css: str) -> None:
        self._user_css = css
        self._eval(f"window.nc.setUserCss({_js(css)})")

    def set_dnd(self, on: bool) -> None:
        self._eval(f"window.nc.setDnd({_js(bool(on))})")

    def configure(
        self, *, anchor: str | None = None, margin: int | None = None, width: int | None = None
    ) -> None:
        if anchor is not None:
            self.anchor = anchor
        if margin is not None:
            self.margin = margin
        if width is not None:
            self.width = width
        if self._window is not None:
            self._apply_anchor()
            self._send_limits()

    def save_snapshot(self, path: str | Path, done: Callable[[bool], None] | None = None) -> None:
        """Write a PNG of the page (dev/QA aid). `done(ok)` runs when finished."""
        from gi.repository import WebKit2

        def finish(view: Any, result: Any) -> None:
            ok = False
            try:
                surface = view.get_snapshot_finish(result)
                surface.write_to_png(str(path))
                ok = True
            except Exception as exc:
                print(f"shade: snapshot failed: {exc}", file=sys.stderr)
            if done is not None:
                done(ok)

        self._view.get_snapshot(
            WebKit2.SnapshotRegion.VISIBLE, WebKit2.SnapshotOptions.NONE, None, finish
        )

    # ---- plumbing ----

    def _eval(self, script: str) -> None:
        if self._closed:
            return
        if not self._ready or self._view is None:
            self._pending_js.append(script)
            return
        self._run(script)

    def _run(self, script: str) -> None:
        self._view.evaluate_javascript(script, -1, None, None, None, None, None)

    def _map_catcher(self) -> None:
        self._catcher.show_all()
        log.info("catcher shown")

    def _unmap_catcher(self) -> None:
        if self._catcher is not None:
            self._catcher.hide()
            log.info("catcher hidden")

    def _map_shade(self) -> None:
        w = self.width + 2 * self._room
        h = self._size[1] or DEFAULT_HEIGHT
        self._resize(w, h)
        self._window.show_all()
        self._window.present()
        self._mapped = True
        log.info("shade shown")

    def _unmap_shade(self) -> None:
        if self._window is not None:
            self._window.hide()
            log.info("shade hidden")

    def _resize(self, w: int, h: int) -> None:
        self._view.set_size_request(w, h)
        self._window.resize(w, h)

    def _monitor_height(self) -> int:
        try:
            from gi.repository import Gdk

            display = Gdk.Display.get_default()
            mon = display.get_primary_monitor() or display.get_monitor(0)
            return int(mon.get_geometry().height)
        except Exception:
            return 800

    def _limits_script(self) -> str:
        max_h = max(200, int(self._monitor_height() * HEIGHT_FRACTION) - 2 * self._room)
        return f"window.nc.setLimits({_js({'maxHeight': max_h, 'width': self.width})})"

    def _send_limits(self) -> None:
        self._eval(self._limits_script())

    def _apply_size(self, size: dict) -> None:
        """The page reported its card box: resize the surface to it, clicks only on the card."""
        if self._window is None or not (self._mapped or self.surfaces.visible):
            return  # a prewarmed page lays out in an unmapped view: its size means nothing
        self._size = (size["width"], size["height"])
        if size["room"] != self._room:
            self._room = size["room"]
            self._apply_anchor()
        if size["height"] > 0 and size["width"] > 0:
            self._resize(size["width"], size["height"])
        self._rect = size["rect"]
        self._set_input_region()
        self.trace.mark("resized", last=True, after="rows")

    def _set_input_region(self) -> None:
        """Clicks on the transparent shadow room fall through to the catcher below."""
        gdk_win = self._window.get_window() if self._window is not None else None
        if gdk_win is None or not self._rect:
            return
        try:
            import cairo

            x, y, w, h = self._rect
            region = cairo.Region(cairo.RectangleInt(x, y, w, h))
            gdk_win.input_shape_combine_region(region, 0, 0)
        except Exception as exc:
            print(f"shade: input region unavailable: {exc}", file=sys.stderr)

    def _apply_anchor(self) -> None:
        from gi.repository import GtkLayerShell

        vert, horiz = parse_anchor(self.anchor)
        edges = {
            "top": GtkLayerShell.Edge.TOP,
            "bottom": GtkLayerShell.Edge.BOTTOM,
            "left": GtkLayerShell.Edge.LEFT,
            "right": GtkLayerShell.Edge.RIGHT,
        }
        for name, edge in edges.items():
            on = name in (vert, horiz)
            GtkLayerShell.set_anchor(self._window, edge, on)
            # the page's transparent shadow room sits inside the surface: pull it back out
            GtkLayerShell.set_margin(self._window, edge, self.margin - self._room if on else 0)

    def _theme_lookup(self, name: str) -> str | None:
        return icons.gtk_theme_lookup(ICON_SIZE)(name)

    def _open_link(self, href: str) -> None:
        try:
            from gi.repository import Gio

            Gio.AppInfo.launch_default_for_uri(href, None)
        except Exception as exc:
            print(f"shade: could not open link: {exc}", file=sys.stderr)

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

        # the click-outside catcher: TOP layer, all four edges, exclusive zone -1, transparent
        catcher = Gtk.Window()
        GtkLayerShell.init_for_window(catcher)
        GtkLayerShell.set_layer(catcher, GtkLayerShell.Layer.TOP)
        GtkLayerShell.set_namespace(catcher, NAMESPACE + "-catcher")
        GtkLayerShell.set_keyboard_mode(catcher, GtkLayerShell.KeyboardMode.NONE)
        GtkLayerShell.set_exclusive_zone(catcher, -1)
        for edge in (
            GtkLayerShell.Edge.TOP,
            GtkLayerShell.Edge.BOTTOM,
            GtkLayerShell.Edge.LEFT,
            GtkLayerShell.Edge.RIGHT,
        ):
            GtkLayerShell.set_anchor(catcher, edge, True)
        catcher.set_app_paintable(True)
        if (rgba := catcher.get_screen().get_rgba_visual()) is not None:
            catcher.set_visual(rgba)
        catcher.set_accept_focus(False)
        catcher.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        catcher.connect("draw", self._on_catcher_draw)
        catcher.connect("button-press-event", self._on_catcher_press)
        catcher.connect("hide", lambda *_a: self.surfaces.on_catcher_hidden())
        catcher.connect("delete-event", lambda *_a: (self.surfaces.hide(), True)[1])

        # the shade: OVERLAY, ON_DEMAND keyboard (no focus until clicked or a key is used)
        win = Gtk.Window()
        GtkLayerShell.init_for_window(win)
        GtkLayerShell.set_layer(win, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_namespace(win, NAMESPACE)
        GtkLayerShell.set_keyboard_mode(win, GtkLayerShell.KeyboardMode.ON_DEMAND)
        GtkLayerShell.set_exclusive_zone(win, -1)
        win.set_app_paintable(True)
        if (rgba := win.get_screen().get_rgba_visual()) is not None:
            win.set_visual(rgba)
        win.connect("key-press-event", self._on_key_press)
        win.connect("hide", lambda *_a: self.surfaces.on_shade_hidden())
        win.connect("map-event", lambda *_a: (self._set_input_region(), False)[1])
        win.connect("delete-event", lambda *_a: (self.surfaces.hide(), True)[1])

        if self.bridge.images is not None:
            register_scheme(self.bridge.images)  # before the view exists; once per process
        ucm = WebKit2.UserContentManager()
        ucm.register_script_message_handler("nc")
        self._ucm_handler = ucm.connect("script-message-received::nc", self._on_script_message)
        self._ucm = ucm
        view = WebKit2.WebView.new_with_user_content_manager(ucm)
        view.set_background_color(Gdk.RGBA(0, 0, 0, 0))
        settings = view.get_settings()
        settings.set_enable_javascript(True)
        settings.set_allow_file_access_from_file_urls(False)
        settings.set_allow_universal_access_from_file_urls(False)
        view.connect("context-menu", lambda *_a: True)
        view.connect("decide-policy", self._on_decide_policy)
        view.connect("load-changed", self._on_load_changed)

        self._page_uri = file_uri(str((self.web_dir / "shade.html").resolve()))
        self._window, self._catcher, self._view = win, catcher, view
        self._apply_anchor()
        win.add(view)
        view.show()
        view.load_uri(self._page_uri)

    def _on_catcher_draw(self, widget: Any, cr: Any) -> bool:
        import cairo

        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.paint()
        return True

    def _on_catcher_press(self, *_a: Any) -> bool:
        self.surfaces.on_catcher_press()
        return True

    def _on_key_press(self, _w: Any, event: Any) -> bool:
        from gi.repository import Gdk

        if event.keyval == Gdk.KEY_Escape:
            self.surfaces.hide()
            return True
        return False

    def _on_script_message(self, _ucm: Any, value: Any) -> None:
        try:
            if hasattr(value, "get_js_value"):  # legacy JavascriptResult wrapper
                value = value.get_js_value()
            msg = json.loads(value.to_json(0))
        except Exception as exc:
            print(f"shade: bad page message: {exc}", file=sys.stderr)
            return
        self.bridge.handle(msg)

    def _on_load_changed(self, _view: Any, event: Any) -> None:
        from gi.repository import WebKit2

        if event != WebKit2.LoadEvent.FINISHED or self._ready:
            return
        self._ready = True
        self._run(self._limits_script())
        if self._user_css is not None:
            self._run(f"window.nc.setUserCss({_js(self._user_css)})")
        pending, self._pending_js = self._pending_js, []
        for script in pending:
            if "setUserCss" in script and self._user_css is not None:
                continue  # already applied above with the latest text
            self._run(script)

    def _on_decide_policy(self, _view: Any, decision: Any, dtype: Any) -> bool:
        """The WebView never leaves our own page."""
        from gi.repository import WebKit2

        if dtype == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri()
            if _same_page(uri, self._page_uri):
                decision.use()
            else:
                decision.ignore()
            return True
        if dtype == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
            decision.ignore()
            return True
        return False


__all__ = ["ShadeWindow", "ShadeBridge", "ShadeState", "ShadeSurfaces", "build_groups"]
