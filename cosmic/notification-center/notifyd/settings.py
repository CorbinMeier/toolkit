"""Backend of the customization panel: theme, style.css, config.toml, sounds, test notifications.

`SettingsService(runtime, server, config_dir)` is assigned to `center.settings` by `build_daemon`;
the center bridge calls it by attribute. It never reloads anything itself: every save writes a
file atomically and the config-dir watcher (`Runtime.watch`) hot-applies it, exactly as a
hand edit would.

Decisions where the brief was silent:
- `save_theme` treats a None or "" value as "no override" (the variable is dropped). Values equal
  to the default are kept as sent; only `active_preset` ignores them (a preset that sets a
  variable to its default still matches).
- Unknown variable, section or key is an error keyed by that name. A None config value removes
  the key (back to the default) for the keys that allow it (`history.retention_days`,
  `sound.default`); for any other key it is an error.
- `restart_required` lists bare keys (not "section.key"); a key is listed only when the saved
  value differs from what the RUNNING daemon uses and the daemon does not apply it live.
- `get()["config"]` is read from config.toml, not from the running Config, so a saved value that
  still waits for a restart shows as saved.
- Rules (`rules_*`): block-level edits of rules.toml through `notifyd.rules_edit` (comments and
  untouched rules are preserved byte for byte). Every write validates the whole file with the
  daemon's own `RuleSet.from_toml` first; a bad edit raises SettingsError keyed by the rule field
  ("app_regex", "buttons[1].label", "hint.x-status"; "" general; "source" for the raw editor) and
  writes nothing. Each write call answers like `rules_get()`. `rules_get` still lists the rules of
  a file that fails validation (the problem is in "error"); a file whose layout the editor cannot
  list answers rules [] and `editable` false (use the raw source).
- `rules_test` is a pure dry run: it builds a Notification from the sample and applies RuleSets in
  memory. No store, runner, sound, toast or file is touched. "rule" is the draft alone (forced
  enabled), "full" is the saved file with the draft in place of rule `index` (appended when
  `index` is None) after app-settings, i.e. what the daemon would do.
- `preview_sound` plays nothing and reports ok False when an explicit file is missing (no
  fallback to the default sound). `resolved` is the file path, the theme event name or "none".
"""

from __future__ import annotations

import glob
import logging
import os
import subprocess
import time
import tomllib
from pathlib import Path
from typing import Any

from notifyd import rules_edit, theme
from notifyd.config import BY_KEY, FIELDS, SECTIONS, load_config
from notifyd.config_edit import atomic_write, update_file
from notifyd.model import Notification, to_web
from notifyd.rules import RulesError, RuleSet
from notifyd.rules_edit import RulesEditError
from notifyd.sanitize import body_runs

log = logging.getLogger("notifyd.settings")

STYLE_MAX_BYTES = 256 * 1024
SOUND_GLOBS = ("/usr/share/sounds/*/stereo", "~/.local/share/sounds/*/stereo")
SOUND_EXTENSIONS = (".oga", ".ogg", ".wav", ".flac", ".mp3")
TEST_APP = "Notification Center"
TEST_KINDS = ("normal", "critical", "buttons")
RULES_MAX_BYTES = 256 * 1024
SAMPLE_MAX_CHARS = 4096
SAMPLE_MAX_HINTS = 50
URGENCY_VALUES = {"low": 0, "normal": 1, "critical": 2}


class SettingsError(Exception):
    """A save was refused. `errors` maps the offending variable / key to a message."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()) or "invalid")
        self.errors = errors


def _fc_list() -> str:
    return subprocess.run(
        ["fc-list", ":", "family"], capture_output=True, text=True, timeout=10, check=True
    ).stdout


class SettingsService:
    def __init__(
        self,
        runtime,
        server,
        config_dir,
        *,
        sound_globs: tuple[str, ...] = SOUND_GLOBS,
        fc_list=_fc_list,
    ) -> None:
        self.runtime = runtime
        self.server = server
        self.dir = Path(config_dir)
        self._sound_globs = sound_globs
        self._fc_list = fc_list
        self._fonts: list[str] | None = None

    # ---- paths ------------------------------------------------------------------------------

    @property
    def theme_path(self) -> Path:
        return self.dir / "theme.css"

    @property
    def style_path(self) -> Path:
        return self.dir / "style.css"

    @property
    def config_path(self) -> Path:
        return self.runtime.config_path

    # ---- read -------------------------------------------------------------------------------

    def get(self) -> dict:
        return {
            "theme": self._theme_state(),
            "style_css": self._read_style(),
            "config": self._config_values(),
            "config_meta": self._config_meta(),
            "sound_themes": self._sound_themes(),
            "fonts": self._font_families(),
        }

    def _theme_state(self) -> dict:
        values = theme.read_theme(self.theme_path)
        return {
            "values": values,
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
            "presets": list(theme.PRESETS),
            "active_preset": self._active_preset(values),
        }

    @staticmethod
    def _active_preset(values: dict[str, str]) -> str | None:
        def norm(d: dict[str, str]) -> dict[str, str]:
            return {k: v for k, v in d.items() if v != theme.BY_NAME[k].default}

        mine = norm(values)
        for name, preset in theme.PRESETS.items():
            if norm(preset) == mine:
                return name
        return None

    def _read_style(self) -> str:
        try:
            return self.style_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""

    def _config_values(self) -> dict:
        cfg = load_config(self.config_path)
        out: dict[str, dict] = {s: {} for s in SECTIONS}
        for f in FIELDS:
            out[f.section][f.key] = f.external(getattr(cfg, f.attr))
        return out

    @staticmethod
    def _config_meta() -> dict:
        meta: dict[str, dict] = {s: {} for s in SECTIONS}
        for f in FIELDS:
            m: dict = {"type": f.type, "label": f.label, "help": f.help, "restart": f.restart}
            if f.choices is not None:
                m["choices"] = list(f.choices)
            if f.min is not None:
                m["min"] = f.min
            if f.max is not None:
                m["max"] = f.max
            if f.nullable:
                m["nullable"] = True
            meta[f.section][f.key] = m
        return meta

    def _sound_themes(self) -> list[str]:
        names: set[str] = set()
        for pattern in self._sound_globs:
            for d in glob.glob(os.path.expanduser(pattern)):
                try:
                    for entry in os.listdir(d):
                        stem, ext = os.path.splitext(entry)
                        if ext.lower() in SOUND_EXTENSIONS and stem:
                            names.add(stem)
                except OSError:
                    continue
        return sorted(names)

    def _font_families(self) -> list[str]:
        if self._fonts is None:
            try:
                families = {
                    fam.strip()
                    for line in self._fc_list().splitlines()
                    for fam in line.split(",")
                    if fam.strip()
                }
                self._fonts = sorted(families, key=str.lower)
            except (OSError, subprocess.SubprocessError):
                self._fonts = []
        return self._fonts

    # ---- theme ------------------------------------------------------------------------------

    def save_theme(self, values: dict) -> dict:
        if not isinstance(values, dict):
            raise SettingsError({"values": "must be an object"})
        errors: dict[str, str] = {}
        clean: dict[str, str] = {}
        for name, value in values.items():
            if value is None or (isinstance(value, str) and not value.strip()):
                if name not in theme.BY_NAME:
                    errors[str(name)] = "unknown theme variable"
                continue  # no override
            try:
                clean[name] = theme.validate(name, value)
            except ValueError as e:
                errors[str(name)] = str(e)
        if errors:
            raise SettingsError(errors)
        theme.write_theme(self.theme_path, clean)
        log.info("theme.css saved (%d overrides)", len(clean))
        return self._theme_state()

    def preset_values(self, name: str) -> dict:
        try:
            return theme.apply_preset(name)
        except KeyError:
            raise SettingsError({"preset": f"unknown preset {name!r}"}) from None

    def save_style(self, text: str) -> None:
        if not isinstance(text, str):
            raise SettingsError({"style_css": "must be text"})
        try:
            raw = text.encode("utf-8")
        except UnicodeEncodeError:
            raise SettingsError({"style_css": "not valid UTF-8"}) from None
        if len(raw) > STYLE_MAX_BYTES:
            raise SettingsError(
                {"style_css": f"too large ({len(raw)} bytes; the limit is {STYLE_MAX_BYTES})"}
            )
        atomic_write(self.style_path, text)
        log.info("style.css saved (%d bytes)", len(raw))

    # ---- config.toml ------------------------------------------------------------------------

    def save_config(self, section: str, values: dict) -> dict:
        if section not in SECTIONS:
            raise SettingsError({"section": f"unknown section {section!r}"})
        if not isinstance(values, dict):
            raise SettingsError({"values": "must be an object"})
        errors: dict[str, str] = {}
        updates: dict[str, object] = {}
        removals: list[str] = []
        restart: list[str] = []
        running = self.runtime.config
        for key, value in values.items():
            f = BY_KEY.get((section, key))
            if f is None:
                errors[str(key)] = "unknown setting"
                continue
            if value is None:
                if not f.nullable:
                    errors[key] = "a value is required"
                    continue
                new = None
                removals.append(key)
            else:
                try:
                    new = f.check(value)
                except ValueError as e:
                    errors[key] = f"{value!r} invalid ({e})"
                    continue
                updates[key] = f.external(new)
            if f.restart and new != getattr(running, f.attr):
                restart.append(key)
        if errors:
            raise SettingsError(errors)
        try:
            update_file(
                self.config_path,
                {section: updates} if updates else {},
                {section: removals} if removals else None,
            )
        except (ValueError, tomllib.TOMLDecodeError) as e:
            raise SettingsError({"config": f"config.toml could not be edited: {e}"}) from e
        log.info("config.toml [%s] saved: %s", section, sorted(values))
        return {"restart_required": restart}

    # ---- sounds and test notifications ------------------------------------------------------

    def preview_sound(self, spec: str | None, volume: int | None = None) -> dict:
        if spec is not None and not isinstance(spec, str):
            return {"ok": False, "resolved": "", "error": "sound must be text"}
        if volume is not None and (isinstance(volume, bool) or not isinstance(volume, int)):
            return {"ok": False, "resolved": "", "error": "volume must be an integer 0-100"}
        player = self.runtime.player
        player.last_error = None
        r = player.resolve(spec, volume)
        resolved = r.value if r.kind != "none" else "none"
        if player.last_error:
            return {"ok": False, "resolved": resolved, "error": player.last_error}
        player.play(spec, volume)
        return {"ok": True, "resolved": resolved, "error": player.last_error}

    def send_test(self, kind: str) -> int:
        if kind not in TEST_KINDS:
            raise SettingsError({"kind": f"one of {TEST_KINDS}"})
        if kind == "critical":
            return self.server.notify(
                TEST_APP,
                "Test notification (critical)",
                "This is a critical test from the customization panel.",
                urgency=2,
            )
        if kind == "buttons":
            return self.server.notify(
                TEST_APP,
                "Test notification (actions)",
                "Test with <b>bold</b>, <i>italic</i> and a "
                '<a href="https://example.org">link</a>.',
                actions=("default", "Open", "reply", "Reply"),
            )
        return self.server.notify(
            TEST_APP,
            "Test notification",
            "This is a test from the customization panel.",
        )

    # ---- rules.toml -------------------------------------------------------------------------

    @property
    def rules_path(self) -> Path:
        return self.dir / "rules.toml"

    def _rules_text(self) -> str:
        try:
            return self.rules_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
        except UnicodeDecodeError as e:
            raise SettingsError({"": f"rules.toml is not valid UTF-8: {e}"}) from e

    def rules_get(self) -> dict:
        text = self._rules_text()
        out: dict[str, Any] = {
            "rules": [],
            "error": None,
            "editable": True,
            "path": str(self.rules_path),
        }
        try:
            out["rules"] = rules_edit.list_rules(text)
        except RulesEditError as e:
            out["error"] = str(e)
            out["editable"] = False
            return out
        try:
            RuleSet.from_toml(text)
        except RulesError as e:
            out["error"] = str(e)
        return out

    def _rules_apply(self, fn, *args, index: int | None = None, expect: str | None = None) -> dict:
        text = self._rules_text()
        if expect is not None:
            # the page sends the name it listed: a stale list must not edit another rule
            try:
                names = [r["name"] for r in rules_edit.list_rules(text)]
            except RulesEditError:
                names = []
            if not (isinstance(index, int) and 0 <= index < len(names)) or names[index] != expect:
                raise SettingsError(
                    {"": "rules.toml changed since the list was loaded; reload the list"}
                )
        try:
            new = fn(text, *args)
            if new != text:
                rules_edit.write_file(self.rules_path, new)
        except RulesEditError as e:
            raise SettingsError(e.errors) from e
        except OSError as e:
            raise SettingsError({"": f"rules.toml could not be written: {e}"}) from e
        log.info("rules.toml edited by the panel (%s)", fn.__name__)
        return self.rules_get()

    def rules_update(self, index: int, data: dict, expect: str | None = None) -> dict:
        return self._rules_apply(rules_edit.update_rule, index, data, index=index, expect=expect)

    def rules_add(self, data: dict, position: int | None = None) -> dict:
        return self._rules_apply(rules_edit.add_rule, data, position)

    def rules_delete(self, index: int, expect: str | None = None) -> dict:
        return self._rules_apply(rules_edit.delete_rule, index, index=index, expect=expect)

    def rules_move(self, index: int, to: int, expect: str | None = None) -> dict:
        return self._rules_apply(rules_edit.move_rule, index, to, index=index, expect=expect)

    def rules_set_enabled(self, index: int, on: bool, expect: str | None = None) -> dict:
        return self._rules_apply(rules_edit.set_enabled, index, on, index=index, expect=expect)

    def rules_get_source(self) -> dict:
        return {"text": self._rules_text(), "path": str(self.rules_path)}

    def rules_save_source(self, text: str) -> dict:
        if not isinstance(text, str):
            raise SettingsError({"source": "must be text"})
        try:
            raw = text.encode("utf-8")
        except UnicodeEncodeError:
            raise SettingsError({"source": "not valid UTF-8"}) from None
        if len(raw) > RULES_MAX_BYTES:
            raise SettingsError(
                {"source": f"too large ({len(raw)} bytes; the limit is {RULES_MAX_BYTES})"}
            )
        try:
            rules_edit.write_file(self.rules_path, text)
        except RulesEditError as e:
            raise SettingsError({"source": "; ".join(e.errors.values())}) from e
        except OSError as e:
            raise SettingsError({"source": f"rules.toml could not be written: {e}"}) from e
        log.info("rules.toml saved from the raw editor (%d bytes)", len(raw))
        return self.rules_get()

    # ---- rules_test: a dry run --------------------------------------------------------------

    @staticmethod
    def _sample_notification(sample: Any, timeouts: Any = None) -> Notification:
        if not isinstance(sample, dict):
            raise SettingsError({"sample": "must be an object"})
        errors: dict[str, str] = {}

        def text(key: str, default: str = "") -> str:
            v = sample.get(key, default)
            if v is None:
                return ""
            if not isinstance(v, str) or len(v) > SAMPLE_MAX_CHARS:
                errors[f"sample.{key}"] = f"must be text of at most {SAMPLE_MAX_CHARS} characters"
                return ""
            return v

        app, summary, body = text("app"), text("summary"), text("body")
        category = text("category")
        urgency = text("urgency", "normal").lower() or "normal"
        if urgency not in URGENCY_VALUES:
            errors["sample.urgency"] = "must be low, normal or critical"
        hints_in = sample.get("hints") or {}
        hints: dict[str, Any] = {}
        if not isinstance(hints_in, dict) or len(hints_in) > SAMPLE_MAX_HINTS:
            errors["sample.hints"] = f"must be an object of at most {SAMPLE_MAX_HINTS} hints"
        else:
            for k, v in hints_in.items():
                ok = isinstance(v, bool | int | float) or (
                    isinstance(v, str) and len(v) <= SAMPLE_MAX_CHARS
                )
                if not isinstance(k, str) or not ok:
                    errors["sample.hints"] = "hint values must be text, numbers or booleans"
                    break
                hints[k] = v
        if errors:
            raise SettingsError(errors)
        hints["urgency"] = URGENCY_VALUES[urgency]
        if category:
            hints["category"] = category
        return Notification.from_notify(
            0, app, 0, "", summary, body, [], hints, -1, time.time(), timeouts
        )

    @staticmethod
    def _preview(n: Notification) -> dict:
        d = to_web(n, body_runs(n.body))
        d.update(
            {
                "state": str(n.state),
                "sound": n.sound,
                "volume": n.sound_volume,
                "dnd_bypass": n.dnd_bypass,
                "matched_rules": list(n.matched_rules),
                # what would run, never the command itself
                "runs": [
                    {"rule": r["rule"], "when": r["when"], "cooldown": r["cooldown"]}
                    for r in n.runs
                ],
            }
        )
        return d

    def rules_test(self, data: dict, sample: dict, index: int | None = None) -> dict:
        """Would this draft rule match the sample, and what would the notification become
        (the rule alone; the saved ruleset with the draft in place)? Nothing is executed."""
        n = self._sample_notification(sample, getattr(self.runtime, "timeouts", None))
        if not isinstance(data, dict):
            raise SettingsError({"": "rule data must be an object"})
        draft = {**data, "enabled": True}
        try:
            alone = RuleSet.from_toml(rules_edit.add_rule("", draft))
            name = data.get("name") if isinstance(data.get("name"), str) else ""
        except RulesEditError as e:
            raise SettingsError(e.errors) from e
        one = alone.apply(n)
        result: dict[str, Any] = {"rule": self._preview(one), "full": None, "full_error": None}
        matched = bool(one.matched_rules)
        try:
            text = self._rules_text()
            if index is None:
                combined = rules_edit.add_rule(text, draft)
            else:
                combined = rules_edit.update_rule(text, index, draft)
            after_apps = getattr(self.runtime, "app_rules", None)
            base = after_apps.apply(n) if after_apps is not None else n
            full = RuleSet.from_toml(combined).apply(base)
            result["full"] = self._preview(full)
            result["full"]["rule_matched"] = name in full.matched_rules
        except RulesEditError as e:
            result["full_error"] = "; ".join(e.errors.values())
        return {"matched": matched, "result": result}
