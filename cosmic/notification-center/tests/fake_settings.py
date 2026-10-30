"""An in-memory stand-in for `notifyd.settings.SettingsService` (same method names and shapes),
used by the bridge tests and `scripts/dev-center-demo.py`. It writes nothing to disk."""

from __future__ import annotations

from typing import Any

from notifyd import theme


class SettingsError(Exception):
    """Mirrors the real one: `.errors` is {field: message}."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


CONFIG: dict[str, dict[str, Any]] = {
    "toasts": {
        "anchor": "top-right",
        "margin": 16,
        "max_visible": 5,
        "timeout_low": 5000,
        "timeout_normal": 8000,
        "timeout_critical": 0,
        "respect_app_timeout": True,
        "hover_leave_grace_ms": 600,
    },
    "shade": {"anchor": "top-right", "margin": 8, "width": 420},
    "history": {"retention_days": 90, "max_rows": 20000},
    "sound": {"default": "message-new-instant", "master_volume": 80},
    "windows": {"prewarm": True},
    "images": {"preview": True},
}

ANCHORS = ["top-left", "top-right", "bottom-left", "bottom-right"]


def _m(type_: str, label: str, help_: str = "", restart: bool = False, **kw: Any) -> dict:
    return {"type": type_, "label": label, "help": help_, "restart": restart, **kw}


CONFIG_META: dict[str, dict[str, dict[str, Any]]] = {
    "toasts": {
        "anchor": _m("enum", "Anchor corner", "Where toasts appear.", True, choices=ANCHORS),
        "margin": _m("int", "Margin (px)", "Distance from the screen edge.", True, min=0, max=200),
        "max_visible": _m("int", "Max visible toasts", "", False, min=1, max=20),
        "timeout_low": _m("timeout", "Timeout, low (ms)", "A number, or never."),
        "timeout_normal": _m("timeout", "Timeout, normal (ms)", "A number, or never."),
        "timeout_critical": _m("timeout", "Timeout, critical (ms)", "0 or never keeps it."),
        "respect_app_timeout": _m("bool", "Respect the app's own timeout"),
        "hover_leave_grace_ms": _m("int", "Hover grace (ms)", "", False, min=0, max=5000),
    },
    "shade": {
        "anchor": _m("enum", "Anchor corner", "", False, choices=ANCHORS),
        "margin": _m("int", "Margin (px)", "", False, min=0, max=200),
        "width": _m("int", "Width (px)", "", False, min=240, max=900),
    },
    "history": {
        "retention_days": _m("int", "Keep history for (days)", "", True, min=1, max=3650),
        "max_rows": _m("int", "Maximum rows", "", True, min=100, max=1000000),
    },
    "sound": {
        "default": _m("path_or_theme", "Default sound", "A sound-theme event or a file path."),
        "master_volume": _m("volume", "Master volume", "", False, min=0, max=100),
    },
    "windows": {"prewarm": _m("bool", "Prewarm windows", "", True)},
    "images": {"preview": _m("bool", "Image previews", "", False)},
}


class FakeSettings:
    def __init__(self) -> None:
        self.theme_values: dict[str, str] = {}
        self.style_css = "/* your own CSS */\n.toast { }\n"
        self.config = {s: dict(v) for s, v in CONFIG.items()}
        self.calls: list[tuple] = []
        self.fail_with: Exception | None = None  # raised by every call when set

    def _go(self, *call: Any) -> None:
        self.calls.append(call)
        if self.fail_with is not None:
            raise self.fail_with

    def get(self) -> dict[str, Any]:
        self._go("get")
        return {
            "theme": {
                "values": dict(self.theme_values),
                "variables": [
                    {
                        "name": v.name,
                        "type": v.type,
                        "default": v.default,
                        "label": v.label,
                        "group": v.group,
                        "lo": v.lo,
                        "hi": v.hi,
                    }
                    for v in theme.VARIABLES
                ],
                "presets": [{"name": k, "label": theme.PRESET_LABELS[k]} for k in theme.PRESETS],
                "active_preset": next(
                    (k for k, v in theme.PRESETS.items() if v == self.theme_values), ""
                ),
            },
            "style_css": self.style_css,
            "config": {s: dict(v) for s, v in self.config.items()},
            "config_meta": CONFIG_META,
            "sound_themes": ["message-new-instant", "bell", "complete", "dialog-warning"],
            "fonts": ["Inter", "Noto Sans", "Fira Mono", "VT323", "DejaVu Sans Mono"],
        }

    def save_theme(self, values: dict[str, str]) -> dict[str, Any]:
        self._go("save_theme", dict(values))
        errors: dict[str, str] = {}
        clean: dict[str, str] = {}
        for name, value in values.items():
            try:
                clean[name] = theme.validate(name, value)
            except ValueError as exc:
                errors[name] = str(exc)
        if errors:
            raise SettingsError(errors)
        self.theme_values = clean
        return self.get()["theme"]

    def preset_values(self, name: str) -> dict[str, str]:
        self._go("preset_values", name)
        if name not in theme.PRESETS:
            raise SettingsError({"": f"unknown preset {name!r}"})
        return dict(theme.PRESETS[name])

    def save_style(self, text: str) -> None:
        self._go("save_style", text)
        if "@import" in text:
            raise SettingsError({"style": "@import is not allowed"})
        self.style_css = text

    def save_config(self, section: str, values: dict[str, Any]) -> dict[str, Any]:
        self._go("save_config", section, dict(values))
        meta = CONFIG_META[section]
        errors = {k: "unknown key" for k in values if k not in meta}
        for k, v in values.items():
            m = meta.get(k)
            if m and m["type"] in ("int", "volume") and v is not None:
                if isinstance(v, bool) or not isinstance(v, int):
                    errors[k] = "must be an integer"
                elif not m.get("min", 0) <= v <= m.get("max", 100):
                    errors[k] = f"must be {m.get('min', 0)} to {m.get('max', 100)}"
        if errors:
            raise SettingsError(errors)
        self.config[section].update({k: v for k, v in values.items() if v is not None})
        return {"restart_required": [k for k in values if meta[k]["restart"]]}

    def preview_sound(self, spec: str, volume: int) -> dict[str, Any]:
        self._go("preview_sound", spec, volume)
        if spec and "/" in spec and not spec.endswith(".oga"):
            return {"ok": False, "resolved": "", "error": f"no such file: {spec}"}
        return {"ok": True, "resolved": spec or "system default", "error": ""}

    def send_test(self, kind: str) -> int:
        self._go("send_test", kind)
        return 4242

    # ---- rules (records the call; answers a canned list) ---------------------------------------

    RULES = [
        {
            "index": 0,
            "name": "example sound",
            "enabled": True,
            "data": {"name": "example sound", "app": "Example App", "sound": "bell"},
            "summary": "app = Example App \u2192 sound",
            "source": '[[rule]]\nname = "example sound"\n',
        }
    ]

    def rules_get(self) -> dict[str, Any]:
        self._go("rules_get")
        return {
            "rules": list(self.RULES),
            "error": None,
            "editable": True,
            "path": "/fake/rules.toml",
        }

    def _rules_write(self, *call: Any) -> dict[str, Any]:
        self._go(*call)
        return self.rules_get()

    def rules_update(self, index: int, data: dict, expect: str | None = None) -> dict[str, Any]:
        if data.get("name") == "bad":
            self._go("rules_update", index, data, expect)
            raise SettingsError({"app_regex": "not a valid regex"})
        return self._rules_write("rules_update", index, data, expect)

    def rules_add(self, data: dict, position: int | None = None) -> dict[str, Any]:
        return self._rules_write("rules_add", data, position)

    def rules_delete(self, index: int, expect: str | None = None) -> dict[str, Any]:
        return self._rules_write("rules_delete", index, expect)

    def rules_move(self, index: int, to: int, expect: str | None = None) -> dict[str, Any]:
        return self._rules_write("rules_move", index, to, expect)

    def rules_set_enabled(self, index: int, on: bool, expect: str | None = None) -> dict[str, Any]:
        return self._rules_write("rules_set_enabled", index, on, expect)

    def rules_get_source(self) -> dict[str, Any]:
        self._go("rules_get_source")
        return {"text": '[[rule]]\nname = "example sound"\n', "path": "/fake/rules.toml"}

    def rules_save_source(self, text: str) -> dict[str, Any]:
        self._go("rules_save_source", text)
        if "bad" in text:
            raise SettingsError({"source": "line 1: not valid"})
        return self.rules_get()

    def rules_test(self, data: dict, sample: dict, index: int | None = None) -> dict[str, Any]:
        self._go("rules_test", data, sample, index)
        return {"matched": True, "result": {"rule": {"app": sample.get("app", "")}, "full": None}}
