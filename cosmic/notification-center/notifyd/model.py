"""Shared notification record and its JSON shapes. Wave-0 contract: other modules build on this.

Decisions where the freedesktop spec (1.2) and docs/spec.md were silent:
- Actions arrive as a flat list [key, label, key, label, ...]. They are paired in order; a
  trailing unpaired element is dropped.
- Urgency comes from the `urgency` hint (a byte). A missing, non-integer or out-of-range value
  means NORMAL.
- `category` comes from the `category` hint and `desktop_entry` from the `desktop-entry` hint;
  both default to "" when absent.
- `expire_timeout` below -1 is treated like -1 (server default).
- `hints` is JSON-safe: tuples become lists, `bytes` become lists of ints, dict keys become
  strings. Image bytes (`image-data`, `image_data`, `icon_data`) are dropped, never stored;
  `image-path` is kept.
- `Notification.icon` for the web is `app_icon` (rules may overwrite `app_icon`).
- `timeout_ms` is the resolved timeout, computed once in `from_notify`; rules may overwrite it.
  None means the toast never expires.
- Timestamps are epoch seconds (float). Optional fields default to None.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any

IMAGE_HINTS = frozenset({"image-data", "image_data", "icon_data"})

# Default toast lifetime in ms per urgency (low, normal, critical). None: never expires.
# These are the config defaults; the live values come from a TimeoutPolicy.
DEFAULT_TIMEOUT_MS: dict[int, int | None] = {0: 5000, 1: 10000, 2: None}


class Urgency(IntEnum):
    LOW = 0
    NORMAL = 1
    CRITICAL = 2


class CloseReason(IntEnum):
    """Values of the NotificationClosed `reason` argument (spec 1.2)."""

    EXPIRED = 1
    DISMISSED = 2
    CLOSED = 3  # closed by a CloseNotification call
    UNDEFINED = 4


class State(StrEnum):
    SHOWN = "shown"
    BLOCKED = "blocked"
    DND = "dnd"
    STORE_ONLY = "store_only"


def _plain(value: Any) -> Any:
    """Make an unpacked GLib value JSON-safe."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if isinstance(value, bytes | bytearray):
        return list(value)
    return value


def hints_from_variants(variants: dict[str, Any]) -> dict[str, Any]:
    """Unpack an `a{sv}` hints dict (GLib.Variant values) into plain Python values.

    Image bytes are dropped (IMAGE_HINTS); `image-path` is kept.
    """
    return {k: _plain(v.unpack()) for k, v in variants.items() if k not in IMAGE_HINTS}


def _pair_actions(flat: list[str]) -> list[tuple[str, str]]:
    return [(flat[i], flat[i + 1]) for i in range(0, len(flat) - 1, 2)]


def _urgency(hints: dict[str, Any]) -> Urgency:
    raw = hints.get("urgency")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw in iter(Urgency):
        return Urgency(raw)
    return Urgency.NORMAL


class TimeoutPolicy:
    """How a toast's lifetime is chosen. Injected into the server; mutable so a config reload
    can swap the values in place (`update`) and later notifications see them.

    - expire_timeout 0 -> never, always.
    - expire_timeout > 0 -> itself when `respect_app`, else the urgency default.
    - expire_timeout < 0 (-1) -> the urgency default.
    Rules run afterwards and override the result.
    """

    def __init__(
        self,
        low: int | None = DEFAULT_TIMEOUT_MS[0],
        normal: int | None = DEFAULT_TIMEOUT_MS[1],
        critical: int | None = DEFAULT_TIMEOUT_MS[2],
        respect_app: bool = True,
    ) -> None:
        self.low, self.normal, self.critical, self.respect_app = low, normal, critical, respect_app

    def _key(self) -> tuple:
        return (self.low, self.normal, self.critical, self.respect_app)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, TimeoutPolicy) and self._key() == other._key()

    def __repr__(self) -> str:
        return f"TimeoutPolicy{self._key()!r}"

    def update(self, other: TimeoutPolicy) -> None:
        self.low, self.normal, self.critical, self.respect_app = other._key()

    def default_for(self, urgency: Urgency) -> int | None:
        return (self.low, self.normal, self.critical)[int(urgency)]

    def resolve(self, expire_timeout: int, urgency: Urgency) -> int | None:
        if expire_timeout == 0:
            return None
        if expire_timeout > 0 and self.respect_app:
            return expire_timeout
        return self.default_for(urgency)


def resolve_timeout(
    expire_timeout: int, urgency: Urgency, policy: TimeoutPolicy | None = None
) -> int | None:
    """-1 (or lower) -> urgency default, 0 -> None (never), >0 -> itself (policy permitting)."""
    return (policy or TimeoutPolicy()).resolve(expire_timeout, urgency)


@dataclass
class Notification:
    id: int
    app_name: str
    replaces_id: int
    app_icon: str
    summary: str
    body: str  # raw, unsanitized
    actions: list[tuple[str, str]]  # (key, label)
    hints: dict[str, Any]  # plain Python values, no image bytes
    expire_timeout: int  # as the sender gave it
    urgency: Urgency
    category: str
    desktop_entry: str
    received_at: float
    state: State = State.SHOWN
    tags: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    highlights: list[dict[str, str]] = field(default_factory=list)  # {"pattern", "class"}
    timeout_ms: int | None = None  # resolved; None = never expires
    sound: str | None = None
    matched_rules: list[str] = field(default_factory=list)
    # RUNTIME-ONLY, set by the rules engine and read by the daemon in the same process. Not
    # persisted: the store has explicit columns for neither, and they are not in to_web().
    dnd_bypass: bool = False  # a rule marked this notification to show/sound during DND
    sound_volume: int | None = None  # 0-100 from a rule's `volume`; None = unset
    # Rule `run` entries, RUNTIME-ONLY (not persisted): {"rule", "cmd": str | list[str],
    # "when": "arrive" | "shown", "cooldown": int s, "timeout": int s}. Read by the runner.
    runs: list[dict[str, Any]] = field(default_factory=list)
    # Rule buttons, in final order with ids b0, b1, ... (persisted by the store from schema v2):
    # {"id", "label", "run": str | list[str] | None, "builtin": str | None,
    #  "minutes": int | None, "close": bool}. Exactly one of run / builtin is set.
    buttons: list[dict[str, Any]] = field(default_factory=list)
    read_at: float | None = None
    closed_at: float | None = None
    close_reason: CloseReason | None = None
    # RUNTIME-ONLY: why the `x-nc-actions` hint was ignored (set on arrival; the web payload
    # re-derives it from the hint). The answered state lives in hints["x-nc-answered"].
    reply_error: str | None = None

    @classmethod
    def from_notify(
        cls,
        id: int,
        app_name: str,
        replaces_id: int,
        app_icon: str,
        summary: str,
        body: str,
        actions_flat: list[str],
        hints: dict[str, Any],
        expire_timeout: int,
        received_at: float,
        timeouts: TimeoutPolicy | None = None,
    ) -> Notification:
        """Build a record from Notify arguments. `hints` is already plain (hints_from_variants).

        `timeouts` is the config-driven policy; None means the built-in defaults.
        """
        urgency = _urgency(hints)
        return cls(
            id=id,
            app_name=app_name,
            replaces_id=replaces_id,
            app_icon=app_icon,
            summary=summary,
            body=body,
            actions=_pair_actions(list(actions_flat)),
            hints=hints,
            expire_timeout=expire_timeout,
            urgency=urgency,
            category=str(hints.get("category", "")),
            desktop_entry=str(hints.get("desktop-entry", "")),
            received_at=received_at,
            timeout_ms=resolve_timeout(expire_timeout, urgency, timeouts),
        )


def _button_web(b: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": b["id"],
        "label": b["label"],
        "kind": "builtin" if b.get("builtin") else "run",
        "close": bool(b.get("close", True)),
    }
    if b.get("builtin"):
        out["builtin"] = b["builtin"]
    if b.get("minutes") is not None:
        out["minutes"] = b["minutes"]
    return out


button_web = _button_web  # public name for the other web payload builders


def to_web(n: Notification, body_runs: list[dict[str, Any]]) -> dict[str, Any]:
    """The JSON-ready dict the web layer receives.

    `body_runs` is the sanitized body, produced by the sanitizer (never raw markup): a list of
    runs `{"text": str, "b"?: true, "i"?: true, "u"?: true, "href"?: str}`. Flags are present
    only when set; `text` is always plain text for a DOM text node. The web layer never sees
    `n.body`.

    Keys: id, app, summary, body (the runs), icon, urgency ("low"|"normal"|"critical"),
    category, tags, classes, highlights ([{pattern, class}]), actions ([{key, label}]),
    timeout_ms (null = never), received_at (epoch seconds), buttons
    ([{id, label, kind: "run"|"builtin", builtin?, minutes?, close}]). Commands (`run`) are never
    sent to the web layer; a button is pressed by its id.

    `reply`: null without an `x-nc-actions` hint, else {choices: [{id, label}], text:
    {placeholder, max} | null, host, method, allowed, reason, confirm, answered, error}; the URL
    path, body and token never leave the daemon (notifyd/http_actions.py).
    """
    reply = None
    if "x-nc-actions" in n.hints:
        from notifyd import http_actions  # lazy: keeps this module free of gi

        reply = http_actions.reply_for_web(n)
    return {
        "id": n.id,
        "app": n.app_name,
        "summary": n.summary,
        "body": body_runs,
        "icon": n.app_icon,
        "urgency": n.urgency.name.lower(),
        "category": n.category,
        "tags": list(n.tags),
        "classes": list(n.classes),
        "highlights": [dict(h) for h in n.highlights],
        "actions": [{"key": k, "label": label} for k, label in n.actions],
        "timeout_ms": n.timeout_ms,
        "received_at": n.received_at,
        "buttons": [_button_web(b) for b in n.buttons],
        "reply": reply,
    }
