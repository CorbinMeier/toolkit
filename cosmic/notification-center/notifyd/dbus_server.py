"""D-Bus server: org.freedesktop.Notifications (spec 1.2) and net.corbinmeier.Notifications1.

This module registers objects on a connection it is given; it never owns a bus name
(`__main__` does, via Gio.bus_own_name_on_connection).

Decisions where the brief was silent:
- A `process` hook result of BLOCKED or STORE_ONLY wins over DND (the state stays as the hook
  set it); DND only applies to notifications that would otherwise be shown. No urgency exception.
- on_closed is idempotent: it is ignored for an unknown id or one already closed in the store,
  so a display that reports a close during display.close() does not produce a second signal.
- GetHistory rejects limit > MAX_HISTORY_LIMIT with org.freedesktop.DBus.Error.InvalidArgs.
- Replacing an id that exists in the store re-records it in place with the new content.
- DND: a notification with `dnd_bypass` that would otherwise be SHOWN is shown despite DND.
- The sound hook runs once per Notify, after display.show, only for SHOWN notifications. A
  replace of an id that is still on screen (stored, not closed, state shown) does not re-sound;
  replacing one that was DND-suppressed or already closed counts as new and sounds. A hook that
  raises is logged and never fails the Notify.
- Rule scripts (`n.runs`) go to `runner.run(trigger="rule")` after display and sound:
  `when="arrive"` for every recorded notification (blocked, store_only and DND too),
  `when="shown"` only for
  displayed ones. Like the sound, a replace of an id that is still on screen runs nothing again
  (neither arrive nor shown). A runner that raises is logged and never fails the Notify.
- `press_button` is the one implementation behind toast, shade, center and bus button presses.
  An unknown id or button is logged and ignored. A button `run` is recorded with the first
  matched rule's name (or "") and a 30 s timeout. After the button, `close` dismisses the
  notification if it is still open (snooze already closed it).
- Snooze: the toast is removed quietly (no NotificationClosed: the notification is pending), the
  row stays open in the store, and the shade drops it through `store.shade_items`. `check_snoozes`
  re-shows due ones with sound, whatever DND says (the user asked for it), unless the row is
  blocked/store_only; it clears closed_at/close_reason. Any close of a snoozed notification
  (dismiss, app CloseNotification) cancels its snooze.
- Reply actions (`x-nc-actions`, notifyd/http_actions.py): `reply_choice` / `reply_text` /
  `confirm_reply` / `cancel_reply` return a dict {status: "sent"|"pending_confirm"|"refused"|
  "cancelled", token?, expires_in?, reason?}; "sent" means the request was started. A finished
  send is recorded in `runs` (trigger "button"), a 2xx stores hints["x-nc-answered"] and closes
  the notification as DISMISSED unless the endpoint says `close = false`; any outcome refreshes
  the observers and the center. One send at a time per notification; an answered one refuses
  more. A sender-supplied `x-nc-answered` hint is dropped on arrival (only we may set it).
  Pending confirmations are in memory, single use, bound to the notification id, and expire
  after REPLY_CONFIRM_TTL; the bus has no confirm method.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gi.repository import Gio, GLib

from notifyd import http_actions as http_actions_module
from notifyd import sanitize
from notifyd.model import (
    CloseReason,
    Notification,
    State,
    TimeoutPolicy,
    hints_from_variants,
    to_web,
)
from notifyd.seen import SeenMark

log = logging.getLogger("notifyd.dbus")

FDO_IFACE = "org.freedesktop.Notifications"
FDO_PATH = "/org/freedesktop/Notifications"
CTL_IFACE = "net.corbinmeier.Notifications1"
CTL_PATH = "/net/corbinmeier/Notifications1"
PROPS_IFACE = "org.freedesktop.DBus.Properties"

CAPABILITIES = [
    "body",
    "body-markup",
    "body-hyperlinks",
    "actions",
    "icon-static",
    "persistence",
    "sound",
]
SERVER_INFO = ("cosmic-notification-center", "net.corbinmeier", "0.1.0", "1.2")
MAX_HISTORY_LIMIT = 1000
REPLY_CONFIRM_TTL = 60.0  # seconds a pending confirmation stays valid

_IFACE_DIR = Path(__file__).resolve().parent / "interfaces"


class InvalidArgs(Exception):
    """Raised by handlers for caller mistakes; becomes org.freedesktop.DBus.Error.InvalidArgs."""


def _load_interface(name: str) -> Gio.DBusInterfaceInfo:
    xml = (_IFACE_DIR / f"{name}.xml").read_text()
    return Gio.DBusNodeInfo.new_for_xml(xml).lookup_interface(name)


def _hints_from_params(hints: GLib.Variant) -> dict[str, Any]:
    variants: dict[str, GLib.Variant] = {}
    for i in range(hints.n_children()):
        entry = hints.get_child_value(i)
        variants[entry.get_child_value(0).get_string()] = entry.get_child_value(1).get_variant()
    return hints_from_variants(variants)


class NotificationServer:
    def __init__(
        self,
        connection: Gio.DBusConnection,
        store,
        display,
        *,
        clock: Callable[[], float] = time.time,
        on_toggle_center: Callable[[], None] | None = None,
        on_show_center: Callable[[], None] | None = None,
        on_toggle_shade: Callable[[], None] | None = None,
        on_show_shade: Callable[[], None] | None = None,
        on_show_notification: Callable[[int], None] | None = None,
        process: Callable[[Notification], Notification] | None = None,
        sound: Callable[[Notification], None] | None = None,
        timeouts: TimeoutPolicy | None = None,
        runner=None,
        clipboard: Callable[[str], None] | None = None,
        opener: Callable[[str], None] | None = None,
        seen=None,
        http_actions: http_actions_module.HttpActions | None = None,
    ) -> None:
        self._http = http_actions
        http_actions_module.set_active(http_actions)
        self._reply_busy: set[int] = set()
        self._reply_pending: dict[str, dict] = {}
        self._runner = runner
        self._clipboard = clipboard
        self._opener = opener
        self._conn = connection
        self._store = store
        self._display = display
        self._clock = clock
        self.on_toggle_center = on_toggle_center
        self.on_show_center = on_show_center
        self.on_toggle_shade = on_toggle_shade
        self.on_show_shade = on_show_shade
        self.on_show_notification = on_show_notification
        self._process = process
        self._sound = sound
        self._timeouts = timeouts if timeouts is not None else TimeoutPolicy()
        self._added_observers: list[Callable[[Notification], None]] = []
        self._changed_observers: list[Callable[[], None]] = []
        self._dnd_observers: list[Callable[[bool], None]] = []
        self._dnd = False  # in memory; phase 5 persists it
        self._unread = store.unread_count()
        self._seen = seen if seen is not None else SeenMark(None, clock())
        self._unseen = store.unseen_count(self._seen.at)
        self._reg_ids: list[int] = []
        self._fdo_info = _load_interface(FDO_IFACE)
        self._ctl_info = _load_interface(CTL_IFACE)
        self._reg_ids.append(
            connection.register_object(
                FDO_PATH, self._fdo_info, self._on_fdo_call, self._get_prop, self._set_prop
            )
        )
        self._reg_ids.append(
            connection.register_object(
                CTL_PATH, self._ctl_info, self._on_ctl_call, self._get_prop, self._set_prop
            )
        )

    # ---- lifecycle -------------------------------------------------------------------------

    def unregister(self) -> None:
        for reg_id in self._reg_ids:
            self._conn.unregister_object(reg_id)
        self._reg_ids = []

    def add_added_observer(self, cb: Callable[[Notification], None]) -> None:
        """Call `cb(n)` after every recorded notification, whatever its state (shown, blocked,
        DND, store_only). Observer errors are logged and never fail the Notify."""
        self._added_observers.append(cb)

    def add_changed_observer(self, cb: Callable[[], None]) -> None:
        """Call `cb()` after every change to what the store lists that did not come from a new
        notification: mark_read, delete, clear_history, and a notification closing (dismiss,
        expiry, CloseNotification). Errors are logged."""
        self._changed_observers.append(cb)

    def add_dnd_observer(self, cb: Callable[[bool], None]) -> None:
        """Call `cb(on)` when Do Not Disturb actually changes, whoever changed it."""
        self._dnd_observers.append(cb)

    def notify_changed(self) -> None:
        """Tell the changed observers the store changed (for outside writes too). Also
        recomputes UnseenCount: the shade's quiet dismissal of expired rows ends here."""
        self._refresh_unseen()
        for observer in self._changed_observers:
            try:
                observer()
            except Exception:
                log.exception("changed observer failed")

    @property
    def store(self):
        return self._store

    def now(self) -> float:
        return self._clock()

    @property
    def dnd(self) -> bool:
        return self._dnd

    def set_dnd(self, on: bool) -> None:
        """Set Do Not Disturb: PropertiesChanged and the DND observers fire only on a change."""
        on = bool(on)
        if on == self._dnd:
            return
        self._dnd = on
        self._props_changed({"DoNotDisturb": GLib.Variant("b", on)})
        for observer in self._dnd_observers:
            try:
                observer(on)
            except Exception:
                log.exception("dnd observer failed")

    def emit_rules_reloaded(self, ok: bool, error: str) -> None:
        self._emit(CTL_PATH, CTL_IFACE, "RulesReloaded", GLib.Variant("(bs)", (ok, error)))

    # ---- callbacks from the display --------------------------------------------------------

    def on_action(self, id: int, key: str) -> None:
        """Emit ActionInvoked. The display closes the toast and then reports on_closed."""
        self._emit(FDO_PATH, FDO_IFACE, "ActionInvoked", GLib.Variant("(us)", (id, key)))

    def on_closed(self, id: int, reason: CloseReason) -> None:
        n = self._store.get(id)
        if n is None or n.closed_at is not None:
            return
        self._store.mark_closed(id, reason, self._clock())
        self._store.clear_snooze(id)
        self._emit(
            FDO_PATH, FDO_IFACE, "NotificationClosed", GLib.Variant("(uu)", (id, int(reason)))
        )
        self._refresh_unseen()
        self.notify_changed()

    # ---- shared operations (bus methods and the center window both call these) ---------------

    def mark_read(self, ids: list[int]) -> None:
        self._store.mark_read(list(ids), self._clock())
        self._refresh_unread()
        self._refresh_unseen()
        self.notify_changed()

    def delete(self, ids: list[int]) -> None:
        self._store.delete(list(ids))
        if self._http is not None:
            self._http.forget(ids)
        self._refresh_unread()
        self._refresh_unseen()
        self.notify_changed()

    def clear_history(self) -> None:
        self._store.clear()
        if self._http is not None:
            self._http.forget_all()
        self._refresh_unread()
        self._refresh_unseen()
        self.notify_changed()

    def mark_seen(self) -> None:
        """The user looked: move the watermark to now and drop UnseenCount to 0."""
        self._seen.set(self._clock())
        self._refresh_unseen()

    def clear_toasts_to_shade(self) -> list[int]:
        """Close every toast on screen as EXPIRED (the shade lists them; nothing is dismissed
        or marked read). The display removes them quietly; each id gets one on_closed here."""
        ids = list(self._display.close_all_expired())
        for id in ids:
            self.on_closed(id, CloseReason.EXPIRED)
        return ids

    def dismiss(self, id: int) -> None:
        self._display.close(id)
        self.on_closed(id, CloseReason.DISMISSED)
        self._refresh_unread()
        self._refresh_unseen()

    # ---- buttons, runs and snooze ----------------------------------------------------------

    def run_finished(self, run_id: int, notification_id: int) -> None:
        """A run's row changed (Runner.on_finished): refresh the shade and an open center."""
        self.notify_changed()
        center = getattr(self, "center", None)
        refresh = getattr(center, "refresh", None)
        if refresh is not None:
            try:
                refresh()
            except Exception:
                log.exception("center refresh failed")

    def ui_changed(self) -> None:
        """Refresh every UI: the changed observers (shade) and the center."""
        self.notify_changed()
        refresh = getattr(getattr(self, "center", None), "refresh", None)
        if refresh is not None:
            try:
                refresh()
            except Exception:
                log.exception("center refresh failed")

    # ---- reply actions (sender-defined HTTP choices and free-text reply) --------------------

    @staticmethod
    def _refused(reason: str) -> dict:
        return {"status": "refused", "reason": reason}

    def reply_choice(self, id: int, choice_id: str) -> dict:
        return self._reply(id, choice_id, None)

    def reply_text(self, id: int, text: str) -> dict:
        return self._reply(id, None, text)

    def _purge_pending(self) -> None:
        now = self._clock()
        for tok in [t for t, p in self._reply_pending.items() if p["expires"] <= now]:
            del self._reply_pending[tok]

    def _reply(
        self, id: int, choice: str | None, text: str | None, confirmed: bool = False
    ) -> dict:
        try:
            return self._reply_inner(id, choice, text, confirmed)
        except Exception:
            log.exception("reply on id=%s failed", id)
            return self._refused("internal error")

    def _reply_inner(self, id, choice, text, confirmed) -> dict:
        ha = self._http
        n = self._store.get(id)
        if ha is None or n is None:
            return self._refused("no such notification" if ha else "reply actions are unavailable")
        self._purge_pending()
        if isinstance(n.hints.get(http_actions_module.ANSWERED_KEY), dict):
            return self._refused("already answered")
        if id in self._reply_busy:
            return self._refused("a reply is already being sent")
        reason = ha.check(n, choice, text)
        if reason is not None:
            return self._refused(reason)
        plan = ha.plan(n)
        if plan is not None and plan.confirm and not confirmed:
            for tok in [t for t, p in self._reply_pending.items() if p["id"] == id]:
                del self._reply_pending[tok]  # one pending confirmation per notification
            token = secrets.token_urlsafe(12)
            self._reply_pending[token] = {
                "id": id,
                "choice": choice,
                "text": text,
                "expires": self._clock() + REPLY_CONFIRM_TTL,
            }
            return {"status": "pending_confirm", "token": token, "expires_in": REPLY_CONFIRM_TTL}
        ha.set_error(id, None)
        self._reply_busy.add(id)
        reason = ha.send(n, choice, text, on_done=lambda res: self._reply_done(id, res))
        if reason is not None:
            self._reply_busy.discard(id)
            return self._refused(reason)
        return {"status": "sent"}

    def confirm_reply(self, id: int, token: str) -> dict:
        self._purge_pending()
        pending = self._reply_pending.get(token)
        if pending is None or pending["id"] != id:
            return self._refused("the confirmation expired or is unknown")
        del self._reply_pending[token]
        return self._reply(id, pending["choice"], pending["text"], confirmed=True)

    def cancel_reply(self, id: int, token: str) -> dict:
        self._purge_pending()
        pending = self._reply_pending.get(token)
        if pending is None or pending["id"] != id:
            return self._refused("the confirmation expired or is unknown")
        del self._reply_pending[token]
        return {"status": "cancelled"}

    def _reply_done(self, id: int, res) -> None:
        """A send finished (main thread): record the run, then answered + close on a 2xx."""
        try:
            self._reply_done_inner(id, res)
        except Exception:
            log.exception("finishing reply on id=%d failed", id)
        finally:
            self._reply_busy.discard(id)
            self.ui_changed()

    def _reply_done_inner(self, id: int, res) -> None:
        self._store.add_run(
            id,
            rule="",
            trigger="button",
            cmd={"http": {"method": res.method, "url": res.url}},
            started_at=res.started_at,
            button_label=res.label,
            finished_at=res.finished_at,
            exit_code=res.status,
            duration_ms=res.duration_ms,
            output_tail=res.output[: http_actions_module.OUTPUT_TAIL],
        )
        if not res.ok:
            msg = res.error or f"HTTP {res.status}"
            if self._http is not None and self._store.get(id) is not None:
                self._http.set_error(id, f"the reply failed: {msg}")
            return
        if self._http is not None:
            self._http.set_error(id, None)  # answered: the old failure is moot
        record = http_actions_module.answered_record(
            res.choice, res.text, self._clock(), res.status
        )
        self._store.set_hint(id, http_actions_module.ANSWERED_KEY, record)
        n = self._store.get(id)
        if res.close and n is not None and n.closed_at is None:
            self.dismiss(id)

    def press_button(self, id: int, button_id: str) -> None:
        n = self._store.get(id)
        b = None if n is None else next((x for x in n.buttons if x.get("id") == button_id), None)
        if n is None or b is None:
            log.warning("press_button: unknown notification %s or button %r", id, button_id)
            return
        log.info("button id=%d %s (%r)", id, button_id, b.get("label"))
        try:
            snoozed = self._do_button(n, b)
        except Exception:
            log.exception("button %s on id=%d failed", button_id, id)
            return
        if b.get("close") and not snoozed:
            now_n = self._store.get(id)
            if now_n is not None and now_n.closed_at is None:
                self.dismiss(id)

    def _do_button(self, n: Notification, b: dict) -> bool:
        """Do what the button says. True if it snoozed (which already closed the toast)."""
        builtin = b.get("builtin")
        if not builtin:
            if self._runner is None:
                log.info("button run skipped: no runner")
                return False
            rule = n.matched_rules[0] if n.matched_rules else ""
            spec = {"cmd": b["run"], "rule": rule, "timeout": 30}
            self._runner.run(n, spec, trigger="button", label=b["label"])
            return False
        if builtin == "details":
            if self.on_show_notification is not None:
                self.on_show_notification(n.id)
        elif builtin in ("copy-body", "copy-summary"):
            text = n.summary if builtin == "copy-summary" else self._plain_body(n)
            if self._clipboard is not None:
                self._clipboard(text)
        elif builtin == "open-link":
            href = next((r["href"] for r in sanitize.body_runs(n.body) if r.get("href")), None)
            if href is None:
                log.info("open-link: no link in id=%d", n.id)
            elif self._opener is not None:
                self._opener(href)
        elif builtin == "mark-read":
            self.mark_read([n.id])
        elif builtin == "dismiss":
            self.dismiss(n.id)
        elif builtin == "snooze":
            minutes = int(b.get("minutes") or 0)
            self._store.snooze(n.id, self._clock() + minutes * 60)
            self._display.close(n.id)  # quiet: no callbacks, the notification is only pending
            self.notify_changed()
            return True
        else:
            log.warning("unknown builtin %r", builtin)
        return False

    @staticmethod
    def _plain_body(n: Notification) -> str:
        return "".join(r.get("text", "") for r in sanitize.body_runs(n.body))

    def check_snoozes(self) -> int:
        """Re-show every due snoozed notification. Returns how many were handled."""
        due = self._store.due_snoozes(self._clock())
        for nid in due:
            self._store.clear_snooze(nid)
            n = self._store.get(nid)
            if n is None:
                continue
            n.closed_at = None
            n.close_reason = None
            if n.state in (State.BLOCKED, State.STORE_ONLY):
                self._store.record(n)
                continue
            n.state = State.SHOWN
            self._store.record(n)
            self._display.show(n)
            log.info("snooze over: re-shown id=%d", nid)
            if self._sound is not None:
                try:
                    self._sound(n)
                except Exception:
                    log.exception("sound hook failed id=%d", nid)
            for observer in self._added_observers:
                try:
                    observer(n)
                except Exception:
                    log.exception("added observer failed id=%d", nid)
        if due:
            self._refresh_unread()
            self._refresh_unseen()
            self.notify_changed()
        return len(due)

    def invoke_action(self, id: int, key: str) -> bool:
        """Invoke an app action from the center. False (sender gone) unless the notification is
        still open. Mirrors a toast action click: ActionInvoked, the toast closes, then the
        notification closes as dismissed."""
        n = self._store.get(id)
        if n is None or n.closed_at is not None:
            return False
        self.on_action(id, key)
        self.dismiss(id)
        return True

    # ---- dispatch --------------------------------------------------------------------------

    def _emit(self, path: str, iface: str, name: str, params: GLib.Variant) -> None:
        try:
            self._conn.emit_signal(None, path, iface, name, params)
        except GLib.Error:
            log.exception("emit %s failed", name)

    def _dispatch(self, table, method, params, invocation) -> None:
        handler = table.get(method)
        if handler is None:
            invocation.return_dbus_error(
                "org.freedesktop.DBus.Error.UnknownMethod", f"No method {method}"
            )
            return
        try:
            result = handler(params)
        except InvalidArgs as e:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.InvalidArgs", str(e))
        except Exception as e:
            log.exception("handler %s failed", method)
            invocation.return_dbus_error("org.freedesktop.DBus.Error.Failed", str(e))
        else:
            invocation.return_value(result)

    def _on_fdo_call(self, conn, sender, path, iface, method, params, invocation) -> None:
        table = {
            "Notify": self._notify,
            "CloseNotification": self._close_notification,
            "GetCapabilities": lambda p: GLib.Variant("(as)", (CAPABILITIES,)),
            "GetServerInformation": lambda p: GLib.Variant("(ssss)", SERVER_INFO),
        }
        self._dispatch(table, method, params, invocation)

    def _on_ctl_call(self, conn, sender, path, iface, method, params, invocation) -> None:
        table = {
            "ToggleCenter": lambda p: self._call_cb(self.on_toggle_center),
            "ShowCenter": lambda p: self._call_cb(self.on_show_center),
            "ToggleShade": lambda p: self._call_cb(self.on_toggle_shade),
            "ShowShade": lambda p: self._call_cb(self.on_show_shade),
            "ShowNotification": self._show_notification,
            "MarkRead": self._mark_read,
            "Dismiss": self._dismiss,
            "Delete": self._delete,
            "ClearHistory": self._clear_history,
            "GetHistory": self._get_history,
            "PressButton": self._press_button,
            "GetRuns": self._get_runs,
            "ReplyChoice": self._reply_choice_call,
            "ReplyText": self._reply_text_call,
            "MarkSeen": lambda p: self.mark_seen(),
        }
        self._dispatch(table, method, params, invocation)

    @staticmethod
    def _call_cb(cb: Callable[[], None] | None) -> None:
        if cb is not None:
            cb()
        return None

    # ---- org.freedesktop.Notifications -----------------------------------------------------

    # ActivationToken is declared in the XML but never emitted: no xdg-activation support yet.

    def _notify(self, params: GLib.Variant) -> GLib.Variant:
        app_name, replaces_id, app_icon, summary, body, actions, _hints, expire = params.unpack()
        hints = _hints_from_params(params.get_child_value(6))
        hints.pop(http_actions_module.ANSWERED_KEY, None)  # only the daemon may set this
        existing = self._store.get(replaces_id) if replaces_id > 0 else None
        on_screen = (
            existing is not None and existing.closed_at is None and existing.state == State.SHOWN
        )
        if existing is not None:
            nid = replaces_id
            if self._http is not None:
                self._http.set_error(nid, None)  # replaced: a new notification, a clean slate
        else:
            nid = self._store.next_id()
        n = Notification.from_notify(
            nid,
            app_name,
            replaces_id,
            app_icon,
            summary,
            body,
            list(actions),
            hints,
            expire,
            self._clock(),
            self._timeouts,
        )
        if self._process is not None:
            n = self._process(n)
        if self._http is not None:
            try:
                self._http.on_arrival(n)
            except Exception:
                log.exception("reply hint check failed id=%d", nid)
        show = False
        if n.state in (State.BLOCKED, State.STORE_ONLY):
            pass
        elif self._dnd and not n.dnd_bypass:
            n.state = State.DND
        else:
            n.state = State.SHOWN
            show = True
        self._store.record(n)
        log.info("recorded id=%d app=%r state=%s", nid, app_name, n.state)
        for observer in self._added_observers:
            try:
                observer(n)
            except Exception:
                log.exception("added observer failed id=%d", nid)
        if show:
            self._display.show(n)
            log.info("shown id=%d", nid)
            if self._sound is not None and not on_screen:
                try:
                    self._sound(n)
                except Exception:
                    log.exception("sound hook failed id=%d", nid)
        if self._runner is not None and not on_screen:
            self._start_runs(n, show)
        runs = sanitize.body_runs(n.body)
        self._emit(
            CTL_PATH,
            CTL_IFACE,
            "NotificationAdded",
            GLib.Variant("(us)", (nid, json.dumps(to_web(n, runs)))),
        )
        self._refresh_unread()
        self._refresh_unseen()
        return GLib.Variant("(u)", (nid,))

    def notify(
        self,
        app_name: str,
        summary: str,
        body: str = "",
        actions: tuple[str, ...] = (),
        urgency: int = 1,
        expire_timeout: int = -1,
    ) -> int:
        """Post a notification from inside the daemon through the same path as a bus Notify
        (rules, app settings, DND, sound, store, display). Returns its id."""
        params = GLib.Variant(
            "(susssasa{sv}i)",
            (
                app_name,
                0,
                "",
                summary,
                body,
                list(actions),
                {"urgency": GLib.Variant("y", urgency)},
                expire_timeout,
            ),
        )
        return self._notify(params).unpack()[0]

    def _start_runs(self, n: Notification, shown: bool) -> None:
        for spec in n.runs:
            if spec.get("when", "arrive") == "shown" and not shown:
                continue
            try:
                self._runner.run(n, spec, trigger="rule")
            except Exception:
                log.exception("rule run failed id=%d rule=%r", n.id, spec.get("rule"))

    def _close_notification(self, params: GLib.Variant) -> None:
        (nid,) = params.unpack()
        if self._store.get(nid) is None:
            return None
        self._display.close(nid)
        self.on_closed(nid, CloseReason.CLOSED)
        return None

    # ---- net.corbinmeier.Notifications1 ----------------------------------------------------

    def _show_notification(self, params: GLib.Variant) -> None:
        """Open the center on a details page. An unknown id is passed on; the window says so."""
        (nid,) = params.unpack()
        if self.on_show_notification is not None:
            self.on_show_notification(nid)
        return None

    def _press_button(self, params: GLib.Variant) -> None:
        nid, button_id = params.unpack()
        self.press_button(nid, button_id)

    def _reply_choice_call(self, params: GLib.Variant) -> GLib.Variant:
        nid, choice = params.unpack()
        return GLib.Variant("(s)", (json.dumps(self.reply_choice(nid, choice)),))

    def _reply_text_call(self, params: GLib.Variant) -> GLib.Variant:
        nid, text = params.unpack()
        return GLib.Variant("(s)", (json.dumps(self.reply_text(nid, text)),))

    def _get_runs(self, params: GLib.Variant) -> GLib.Variant:
        (nid,) = params.unpack()
        return GLib.Variant("(s)", (json.dumps(self._store.runs_for(nid)),))

    def _mark_read(self, params: GLib.Variant) -> None:
        (ids,) = params.unpack()
        self.mark_read(ids)

    def _delete(self, params: GLib.Variant) -> None:
        (ids,) = params.unpack()
        self.delete(ids)

    def _clear_history(self, params: GLib.Variant) -> None:
        self.clear_history()

    def _dismiss(self, params: GLib.Variant) -> None:
        (nid,) = params.unpack()
        self.dismiss(nid)

    def _get_history(self, params: GLib.Variant) -> GLib.Variant:
        query, offset, limit = params.unpack()
        if limit > MAX_HISTORY_LIMIT:
            raise InvalidArgs(f"limit must be <= {MAX_HISTORY_LIMIT}")
        out = []
        for n in self._store.history(query, offset, limit):
            d = to_web(n, sanitize.body_runs(n.body))
            d |= {
                "read_at": n.read_at,
                "closed_at": n.closed_at,
                "close_reason": None if n.close_reason is None else int(n.close_reason),
                "state": str(n.state),
            }
            out.append(d)
        return GLib.Variant("(s)", (json.dumps(out),))

    # ---- properties ------------------------------------------------------------------------

    def _refresh_unread(self) -> None:
        count = self._store.unread_count()
        if count != self._unread:
            self._unread = count
            self._props_changed({"UnreadCount": GLib.Variant("u", count)})

    def _refresh_unseen(self) -> None:
        count = self._store.unseen_count(self._seen.at)
        if count != self._unseen:
            self._unseen = count
            self._props_changed({"UnseenCount": GLib.Variant("u", count)})

    def _props_changed(self, changed: dict[str, GLib.Variant]) -> None:
        self._emit(
            CTL_PATH,
            PROPS_IFACE,
            "PropertiesChanged",
            GLib.Variant("(sa{sv}as)", (CTL_IFACE, changed, [])),
        )

    def _get_prop(self, conn, sender, path, iface, name):
        if iface == CTL_IFACE and name == "UnreadCount":
            return GLib.Variant("u", self._unread)
        if iface == CTL_IFACE and name == "UnseenCount":
            return GLib.Variant("u", self._unseen)
        if iface == CTL_IFACE and name == "DoNotDisturb":
            return GLib.Variant("b", self._dnd)
        return None

    def _set_prop(self, conn, sender, path, iface, name, value) -> bool:
        if iface != CTL_IFACE or name != "DoNotDisturb":
            return False
        self.set_dnd(value.get_boolean())
        return True
