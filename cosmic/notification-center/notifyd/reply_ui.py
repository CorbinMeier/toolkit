"""Sender-defined reply actions, the UI side: what the pages may see and what they may send.

Shared by the toast host, the shade bridge and the center bridge, and free of GTK and of the other
host modules (they import this one).

Payload: `reply_of(n)` is the notification's `reply` dict from `model.to_web`, copied through
`clean_reply`, an allow-list. Even if the model ever grew a URL, body or token key, the page
would not receive it. Shape (see the backend contract):
  {choices: [{id, label}], text: {placeholder, max} | None, host, method, allowed, reason,
   confirm, answered: {choice?, text?, at, status} | None, error}

Messages from a page (untrusted), routed by `ReplyRouter.handle`:
  {type: "reply_choice",  id, choice}   choice is one of the notification's `reply.choices` ids
  {type: "reply_text",    id, text}     only when `reply.text` exists; str, <= text.max (<= 4000),
                                        no NUL
  {type: "reply_confirm", id, token}    token: str of <= 64 chars [A-Za-z0-9_-]
  {type: "reply_cancel",  id, token}
The result goes back to the page as window.nc.replyResult({id, kind, status, token?, reason?}),
status "sent" | "pending_confirm" | "refused" | "cancelled". Only a pending_confirm result carries
a token (the one-shot confirm token the page echoes back); it is never in a notification payload.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any

from notifyd.model import Notification, to_web

log = logging.getLogger("notifyd.reply")

ID_MAX = 2**32 - 1
HARD_TEXT_MAX = 4000
TOKEN = re.compile(r"[A-Za-z0-9_-]{1,64}")
STATUSES = ("sent", "pending_confirm", "refused")
KINDS = ("reply_choice", "reply_text", "reply_confirm", "reply_cancel")
MAX_CHOICES = 24
MAX_LABEL = 200
MAX_REASON = 300
MAX_ANSWER_TEXT = HARD_TEXT_MAX


def _valid_id(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= ID_MAX


def _str(v: Any, cap: int) -> str:
    return v[:cap] if isinstance(v, str) else ""


def _opt_str(v: Any, cap: int) -> str | None:
    return v[:cap] if isinstance(v, str) and v else None


def _num(v: Any) -> int | float | None:
    return v if isinstance(v, int | float) and not isinstance(v, bool) else None


def clean_reply(raw: Any) -> dict[str, Any] | None:
    """The allow-listed copy of a `reply` dict, or None. Unknown keys (url, body, token, ...) go."""
    if not isinstance(raw, dict):
        return None
    choices = []
    listed = raw.get("choices")
    for c in listed[:MAX_CHOICES] if isinstance(listed, list) else []:
        if isinstance(c, dict) and isinstance(c.get("id"), str) and c["id"]:
            choices.append({"id": c["id"][:MAX_LABEL], "label": _str(c.get("label"), MAX_LABEL)})
    text = raw.get("text")
    if isinstance(text, dict):
        mx = text.get("max")
        mx = mx if isinstance(mx, int) and not isinstance(mx, bool) and mx > 0 else HARD_TEXT_MAX
        text = {
            "placeholder": _str(text.get("placeholder"), MAX_LABEL),
            "max": min(mx, HARD_TEXT_MAX),
        }
    else:
        text = None
    ans = raw.get("answered")
    if isinstance(ans, dict):
        out: dict[str, Any] = {}
        if isinstance(ans.get("choice"), str):
            out["choice"] = ans["choice"][:MAX_LABEL]
        if isinstance(ans.get("text"), str):
            out["text"] = ans["text"][:MAX_ANSWER_TEXT]
        if (at := _num(ans.get("at"))) is not None:
            out["at"] = at
        st = ans.get("status")
        out["status"] = st if _num(st) is not None else _str(st, 64)
        ans = out
    else:
        ans = None
    return {
        "choices": choices,
        "text": text,
        "host": _str(raw.get("host"), MAX_LABEL),
        "method": _str(raw.get("method"), 16) or "POST",
        "allowed": raw.get("allowed") is True,
        "reason": _opt_str(raw.get("reason"), MAX_REASON),
        "confirm": raw.get("confirm") is True,
        "answered": ans,
        "error": _opt_str(raw.get("error"), MAX_REASON),
    }


def reply_of(n: Notification) -> dict[str, Any] | None:
    """The cleaned `reply` of `n` (None: the notification has no reply action)."""
    try:
        return clean_reply(to_web(n, []).get("reply"))
    except Exception:
        log.exception("reply payload of %s failed", getattr(n, "id", "?"))
        return None


def text_input_open(reply: dict[str, Any] | None) -> bool:
    """True when the page shows an editable text box for this reply."""
    return bool(reply and reply["text"] and reply["allowed"] and not reply["answered"])


def valid_text(text: Any, reply: dict[str, Any]) -> str | None:
    """`text` when the reply takes text and it is a str within the limit without NUL."""
    if not reply["text"] or not isinstance(text, str) or "\x00" in text:
        return None
    if len(text) > min(reply["text"]["max"], HARD_TEXT_MAX):
        return None
    return text


def clean_result(res: Any, kind: str) -> dict[str, Any]:
    """A callback's return value as the page gets it (never trusts the shape)."""
    if kind == "cancel":  # nothing to report beyond "stopped"
        return {"status": "cancelled"}
    if res is None and kind == "confirm":  # that callback need not return anything
        return {"status": "sent"}
    status = res.get("status") if isinstance(res, dict) else None
    if status not in STATUSES:
        return {"status": "refused", "reason": "no valid answer"}
    out: dict[str, Any] = {"status": status}
    if status == "pending_confirm":
        token = res.get("token")
        if isinstance(token, str) and TOKEN.fullmatch(token):
            out["token"] = token
        else:
            return {"status": "refused", "reason": "no valid confirmation token"}
    reason = _opt_str(res.get("reason"), MAX_REASON)
    if reason:
        out["reason"] = reason
    return out


class ReplyRouter:
    """Validates the four reply messages and calls the host's callbacks.

    `host` holds the attributes `on_reply_choice(id, choice)`, `on_reply_text(id, text)`,
    `on_reply_confirm(id, token)` and `on_reply_cancel(id, token)` (looked up on every message,
    so wiring them later works; None means disabled). `lookup(id)` is the cleaned reply of a
    known notification or None; `send(js)` runs a script on the page.
    """

    def __init__(
        self,
        host: Any,
        lookup: Callable[[int], dict[str, Any] | None],
        send: Callable[[str], None],
    ) -> None:
        self.host = host
        self.lookup = lookup
        self.send = send

    def enabled(self) -> bool:
        h = self.host
        return h.on_reply_choice is not None or h.on_reply_text is not None

    def present(self, reply: dict[str, Any] | None) -> dict[str, Any] | None:
        """`reply` as the page should get it: parts without a wired callback are dropped, and
        nothing at all (None) while no callback is wired."""
        if reply is None or not self.enabled():
            return None
        out = dict(reply)
        if self.host.on_reply_choice is None:
            out["choices"] = []
        if self.host.on_reply_text is None:
            out["text"] = None
        return out

    def deliver(self, id: int, kind: str, result: dict[str, Any], **extra: Any) -> dict[str, Any]:
        out = {"id": id, "kind": kind, **extra, **result}
        self.send(f"window.nc.replyResult({json.dumps(out, ensure_ascii=True)})")
        return out

    def handle(self, msg: dict) -> dict[str, Any] | None:
        """Route one message. Returns what was sent to the page, None when it was dropped."""
        mtype = msg.get("type")
        if mtype not in KINDS:
            return None
        id = msg.get("id")
        if not _valid_id(id):
            return None
        reply = self.lookup(id)
        if reply is None:
            return None
        kind = mtype.removeprefix("reply_")
        if kind in ("confirm", "cancel"):
            token = msg.get("token")
            if not isinstance(token, str) or not TOKEN.fullmatch(token):
                return None
            fn = self.host.on_reply_confirm if kind == "confirm" else self.host.on_reply_cancel
            return self._call(id, kind, fn, (id, token))
        if kind == "choice":
            choice = msg.get("choice")
            if not isinstance(choice, str) or choice not in {c["id"] for c in reply["choices"]}:
                return None
            return self._send(id, kind, reply, self.host.on_reply_choice, (id, choice))
        text = valid_text(msg.get("text"), reply)
        if text is None:
            return None
        return self._send(id, kind, reply, self.host.on_reply_text, (id, text))

    def _send(
        self, id: int, kind: str, reply: dict[str, Any], fn: Callable | None, args: tuple
    ) -> dict[str, Any]:
        if not reply["allowed"]:  # the page shows it disabled; do not even ask the backend
            reason = reply["reason"] or "not allowed"
            return self.deliver(id, kind, {"status": "refused", "reason": reason})
        return self._call(id, kind, fn, args)

    def _call(self, id: int, kind: str, fn: Callable | None, args: tuple) -> dict[str, Any]:
        if fn is None:
            return self.deliver(id, kind, {"status": "refused", "reason": "replies are disabled"})
        try:
            res = clean_result(fn(*args), kind)
        except Exception:
            log.exception("reply %s for %d failed", kind, id)
            res = {"status": "refused", "reason": "internal error"}
        return self.deliver(id, kind, res)


def bridge_attr(name: str) -> property:
    """A window-host property forwarding `name` to its `.bridge` (how `on_button` is done)."""

    def get(self: Any) -> Any:
        return getattr(self.bridge, name)

    def set_(self: Any, fn: Any) -> None:
        setattr(self.bridge, name, fn)

    return property(get, set_)


REPLY_CALLBACKS = ("on_reply_choice", "on_reply_text", "on_reply_confirm", "on_reply_cancel")

__all__ = [
    "KINDS",
    "REPLY_CALLBACKS",
    "bridge_attr",
    "ReplyRouter",
    "clean_reply",
    "clean_result",
    "reply_of",
    "text_input_open",
    "valid_text",
]
