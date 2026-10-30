"""config.toml: the `Config` the daemon runs on, and the ONE table of per-key rules.

`FIELDS` is shared by the loader (`load_config`, which logs and falls back per key) and the
settings panel (`notifyd.settings`, which refuses a bad value). A rule exists only here.
Each field's `check` returns the value stored on `Config` (a "never" timeout is `None`) or raises
ValueError naming the rule. `restart` says whether the running daemon applies a saved change
(`Runtime.reload_config`): False means live, True means it needs `<repo>/bin/notifyd --restart`.
"""

from __future__ import annotations

import logging
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from notifyd.model import DEFAULT_TIMEOUT_MS, TimeoutPolicy

log = logging.getLogger("notifyd")

SHADE_WIDTH_RANGE = (240, 1000)
ANCHORS = ("top-left", "top-right", "bottom-left", "bottom-right", "top-center", "bottom-center")
SECTIONS = ("toasts", "shade", "history", "sound", "windows", "images")


@dataclass
class Config:
    anchor: str = "top-right"
    margin: int = 12
    retention_days: int | None = None
    sound_default: str | None = None
    master_volume: int = 100
    timeout_low: int | None = DEFAULT_TIMEOUT_MS[0]  # None = never
    timeout_normal: int | None = DEFAULT_TIMEOUT_MS[1]
    timeout_critical: int | None = DEFAULT_TIMEOUT_MS[2]
    respect_app_timeout: bool = True
    hover_leave_grace_ms: int = 1000
    hover_max_hold_ms: int = 120000  # safety net, 0 = off
    shade_anchor: str = "top-right"
    shade_margin: int = 8
    shade_width: int = 420
    shade_clear_toasts_on_open: bool = True
    prewarm: bool = True
    images_preview: bool = True

    def timeout_policy(self) -> TimeoutPolicy:
        return TimeoutPolicy(
            self.timeout_low, self.timeout_normal, self.timeout_critical, self.respect_app_timeout
        )


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _int_min(lo: int, rule: str) -> Callable[[object], int]:
    def check(v: object) -> int:
        if _is_int(v) and v >= lo:  # type: ignore[operator]
            return v  # type: ignore[return-value]
        raise ValueError(rule)

    return check


def _int_range(lo: int, hi: int) -> Callable[[object], int]:
    def check(v: object) -> int:
        if _is_int(v) and lo <= v <= hi:  # type: ignore[operator]
            return v  # type: ignore[return-value]
        raise ValueError(f"integer {lo}-{hi}")

    return check


def _enum(v: object) -> str:
    if isinstance(v, str) and v in ANCHORS:
        return v
    raise ValueError(f"one of {ANCHORS}")


def _bool(v: object) -> bool:
    if isinstance(v, bool):
        return v
    raise ValueError("true/false")


def _timeout(v: object) -> int | None:
    if v == "never":
        return None
    if _is_int(v) and v > 0:  # type: ignore[operator]
        return v  # type: ignore[return-value]
    raise ValueError('integer ms > 0 or "never"')


def _path_or_theme(v: object) -> str:
    if isinstance(v, str) and v.strip():
        return v.strip()
    raise ValueError("non-empty string")


@dataclass(frozen=True)
class Field:
    section: str
    key: str
    attr: str  # attribute on Config
    type: str  # int | bool | enum | timeout | path_or_theme | volume
    label: str
    help: str
    check: Callable[[object], object]
    restart: bool  # a saved change is NOT applied by the running daemon
    choices: tuple[str, ...] | None = None
    min: int | None = None
    max: int | None = None
    nullable: bool = False  # None removes the key (back to the default)

    def external(self, value: object) -> object:
        """A Config attribute value as it is written in config.toml and shown in the panel."""
        if self.type == "timeout" and value is None:
            return "never"
        return value


_T_HELP = "Milliseconds, or never. Applies to notifications that arrive after saving."
FIELDS: tuple[Field, ...] = (
    Field(
        "toasts", "anchor", "anchor", "enum", "Toast anchor",
        "Screen corner or edge the toasts stack from.",
        _enum, True, choices=ANCHORS,
    ),
    Field(
        "toasts", "margin", "margin", "int", "Toast margin",
        "Pixels between the toast stack and the screen edge.",
        _int_min(0, "integer >= 0"), True, min=0,
    ),
    Field(
        "toasts", "timeout_low", "timeout_low", "timeout", "Low urgency timeout",
        _T_HELP, _timeout, False, min=1,
    ),
    Field(
        "toasts", "timeout_normal", "timeout_normal", "timeout", "Normal urgency timeout",
        _T_HELP, _timeout, False, min=1,
    ),
    Field(
        "toasts", "timeout_critical", "timeout_critical", "timeout", "Critical urgency timeout",
        _T_HELP, _timeout, False, min=1,
    ),
    Field(
        "toasts", "respect_app_timeout", "respect_app_timeout", "bool", "Respect app timeouts",
        "An app's own timeout (above 0) wins over the defaults above.",
        _bool, False,
    ),
    Field(
        "toasts", "hover_leave_grace_ms", "hover_leave_grace_ms", "int", "Hover grace (ms)",
        "After the pointer leaves, toasts that ran out wait this long before closing.",
        _int_min(0, "integer ms >= 0"), False, min=0,
    ),
    Field(
        "toasts", "hover_max_hold_ms", "hover_max_hold_ms", "int", "Hover safety release (ms)",
        "Release a hover hold after this long with no pointer movement. 0 turns it off.",
        _int_min(0, "integer ms >= 0, 0 = off"), False, min=0,
    ),
    Field(
        "shade", "anchor", "shade_anchor", "enum", "Shade anchor",
        "Screen corner or edge the shade opens from.",
        _enum, False, choices=ANCHORS,
    ),
    Field(
        "shade", "margin", "shade_margin", "int", "Shade margin",
        "Pixels between the shade and the screen edge.",
        _int_min(0, "integer >= 0"), False, min=0,
    ),
    Field(
        "shade", "width", "shade_width", "int", "Shade width",
        "Shade width in pixels.",
        _int_range(*SHADE_WIDTH_RANGE), False, min=SHADE_WIDTH_RANGE[0], max=SHADE_WIDTH_RANGE[1],
    ),
    Field(
        "shade", "clear_toasts_on_open", "shade_clear_toasts_on_open", "bool",
        "Clear toasts when the shade opens",
        "Opening the shade closes every toast on screen, sticky ones included. "
        "They stay listed in the shade.",
        _bool, False,
    ),
    Field(
        "history", "retention_days", "retention_days", "int", "Keep history (days)",
        "Delete history older than this many days. Empty keeps history forever.",
        _int_min(1, "integer > 0"), True, min=1, nullable=True,
    ),
    Field(
        "sound", "default", "sound_default", "path_or_theme", "Default sound",
        "A file path (mp3, wav, ogg, flac) or a sound theme event name. Empty: system default.",
        _path_or_theme, False, nullable=True,
    ),
    Field(
        "sound", "master_volume", "master_volume", "volume", "Master volume",
        "0-100, applied on top of any per-rule volume.",
        _int_range(0, 100), False, min=0, max=100,
    ),
    Field(
        "windows", "prewarm", "prewarm", "bool", "Prewarm windows",
        "A few seconds after startup, build the center and shade hidden so the first open is "
        "quick. Costs memory for two idle web processes.",
        _bool, True,
    ),
    Field(
        "images", "preview", "images_preview", "bool", "Image previews",
        "Show a thumbnail and Copy buttons when a notification carries an image: an image-path "
        "hint, or a body that is just the path of an image file (screenshot tools).",
        _bool, False,
    ),
)  # fmt: skip
BY_KEY: dict[tuple[str, str], Field] = {(f.section, f.key): f for f in FIELDS}


def load_config(path: Path) -> Config:
    """Read config.toml. Missing file means defaults; anything invalid logs and falls back."""
    cfg = Config()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return cfg
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        log.error("config %s unusable (%s); using defaults", path, e)
        return cfg
    tables = {s: data.get(s, {}) for s in SECTIONS}
    if not all(isinstance(t, dict) for t in tables.values()):
        log.error(
            "config %s: [toasts]/[history]/[sound]/[shade]/[windows]/[images] must be tables;"
            " using defaults",
            path,
        )
        return cfg
    for f in FIELDS:
        table = tables[f.section]
        if f.key not in table:
            continue
        raw = table[f.key]
        try:
            setattr(cfg, f.attr, f.check(raw))
        except ValueError as e:
            log.error(
                "config: %s.%s %r invalid (%s); using %r",
                f.section,
                f.key,
                raw,
                e,
                f.external(getattr(cfg, f.attr)),
            )
    return cfg
