"""Per-app settings: `app-settings.toml`, a UI-managed file applied BEFORE the hand-written rules.

Pure Python, stdlib only. `AppSettings.to_ruleset()` turns the prefs into a `RuleSet` (one rule
per app) so the daemon needs no second engine; the user's `rules.toml` runs after it and wins.

Decisions where the brief was silent (also in docs/rules.md):
- App keys are case-insensitive (casefold); the first spelling stored is the display name.
- `icon` is only type-checked on load (a later-deleted file must not break the whole file);
  existence is checked when the UI saves it (`notifyd.icons.icon_error`). Blank -> None.
- A blank `sound` string is normalized to None (unset).
- `silent` wins over `sound`; `volume` still applies when only `volume` is set.
- Unknown keys, a duplicate app (case-insensitively) or an empty name are load errors.
- The rules are built from dicts through the rules engine's own validation (no TOML round trip),
  so app names and paths never pass through a regex or a TOML string.
"""

from __future__ import annotations

import copy
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, fields
from typing import Any

from notifyd.rules import RulesError, RuleSet, _build_rule

HEADER = (
    "# Managed by the notification center UI (per-app settings).\n"
    "# Hand edits are allowed and applied live, but comments are not preserved when the UI\n"
    "# saves. Applied before rules.toml, so your own rules always win.\n"
)

_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}
_PREF_KEYS = ("muted", "silent", "sound", "volume", "sticky", "blocked", "dnd_bypass", "icon")


class AppSettingsError(Exception):
    """An app-settings file that cannot be loaded; the message names the line when known."""


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


@dataclass
class AppPrefs:
    muted: bool = False  # -> store_only
    silent: bool = False  # -> sound "none"
    sound: str | None = None  # file path or theme name (ignored when silent)
    volume: int | None = None  # 0-100
    sticky: bool = False  # -> timeout "never"
    blocked: bool = False  # -> block
    dnd_bypass: bool = False
    icon: str | None = None  # path, ~ path or theme icon name (-> the `icon` rule action)

    def is_default(self) -> bool:
        return self == AppPrefs()

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, d: Any) -> AppPrefs:
        if not isinstance(d, dict):
            raise ValueError(f"prefs must be an object, got {type(d).__name__}")
        unknown = sorted(set(d) - set(_PREF_KEYS))
        if unknown:
            raise ValueError(f"unknown pref(s): {', '.join(map(str, unknown))}")
        out = cls()
        for key in ("muted", "silent", "sticky", "blocked", "dnd_bypass"):
            if key in d:
                if not isinstance(d[key], bool):
                    raise ValueError(f"`{key}` must be true or false, got {type(d[key]).__name__}")
                setattr(out, key, d[key])
        if d.get("sound") is not None:
            if not isinstance(d["sound"], str):
                raise ValueError(f"`sound` must be a string, got {type(d['sound']).__name__}")
            out.sound = d["sound"] if d["sound"].strip() else None
        if d.get("icon") is not None:
            if not isinstance(d["icon"], str):
                raise ValueError(f"`icon` must be a string, got {type(d['icon']).__name__}")
            out.icon = d["icon"].strip() or None
        if d.get("volume") is not None:
            vol = d["volume"]
            if not _is_int(vol) or not 0 <= vol <= 100:
                raise ValueError(f"`volume` must be an integer from 0 to 100, got {vol!r}")
            out.volume = vol
        return out


def _key(app: str) -> str:
    return app.casefold()


def _check_app(app: Any) -> str:
    if not isinstance(app, str) or not app.strip():
        raise ValueError("app name must be a non-empty string")
    return app


def toml_string(s: str) -> str:
    """A TOML basic string: quotes, backslashes and control characters escaped, unicode raw."""
    out = []
    for ch in s:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


class AppSettings:
    def __init__(self) -> None:
        # casefolded app -> (display name, prefs); dict order is file order
        self._apps: dict[str, tuple[str, AppPrefs]] = {}

    def copy(self) -> AppSettings:
        new = AppSettings()
        new._apps = {k: (name, copy.copy(p)) for k, (name, p) in self._apps.items()}
        return new

    def get(self, app: str) -> AppPrefs:
        entry = self._apps.get(_key(app))
        return copy.copy(entry[1]) if entry else AppPrefs()

    def set(self, app: str, prefs: AppPrefs) -> None:
        _check_app(app)
        key = _key(app)
        if prefs.is_default():
            self._apps.pop(key, None)
        else:
            name = self._apps[key][0] if key in self._apps else app
            self._apps[key] = (name, copy.copy(prefs))

    def apps(self) -> list[str]:
        return [name for name, _ in self._apps.values()]

    def __len__(self) -> int:
        return len(self._apps)

    # --- text / file ---------------------------------------------------------------------

    def dumps(self) -> str:
        parts = [HEADER]
        for name, p in self._apps.values():
            lines = ["[[app]]", f"name = {toml_string(name)}"]
            d = p.to_dict()
            for key in _PREF_KEYS:
                v = d[key]
                if v is None or v is False:
                    continue
                if isinstance(v, str):
                    lines.append(f"{key} = {toml_string(v)}")
                elif v is True:
                    lines.append(f"{key} = true")
                else:
                    lines.append(f"{key} = {v}")
            parts.append("\n".join(lines) + "\n")
        return "\n".join(parts)

    @classmethod
    def loads(cls, text: str) -> AppSettings:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise AppSettingsError(f"not valid TOML: {e}") from e
        extra = sorted(set(data) - {"app"})
        if extra:
            raise AppSettingsError(
                f"unknown top-level key(s): {', '.join(extra)} (apps go under [[app]])"
            )
        raw_apps = data.get("app", [])
        if not isinstance(raw_apps, list):
            raise AppSettingsError("`app` must be an array of tables: write [[app]], not [app]")
        header = re.compile(r"^\s*\[\[\s*app\s*\]\]")
        lines = [i for i, ln in enumerate(text.splitlines(), 1) if header.match(ln)]
        out = cls()
        for i, raw in enumerate(raw_apps):
            where = f"app #{i + 1}"
            if len(lines) == len(raw_apps):
                where += f", line {lines[i]}"
            if not isinstance(raw, dict):
                raise AppSettingsError(f"{where}: must be a table")
            name = raw.get("name")
            if not isinstance(name, str) or not name.strip():
                raise AppSettingsError(f"{where}: `name` must be a non-empty string")
            where += f" ({name!r})"
            try:
                prefs = AppPrefs.from_dict({k: v for k, v in raw.items() if k != "name"})
            except ValueError as e:
                raise AppSettingsError(f"{where}: {e}") from e
            if _key(name) in out._apps:
                raise AppSettingsError(f"{where}: duplicate app (names are case-insensitive)")
            if not prefs.is_default():
                out._apps[_key(name)] = (name, prefs)
        return out

    @classmethod
    def load(cls, path: os.PathLike | str) -> AppSettings:
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except FileNotFoundError:
            return cls()
        except OSError as e:
            raise AppSettingsError(f"cannot read {os.fspath(path)}: {e}") from e
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise AppSettingsError(f"{os.fspath(path)} is not valid UTF-8: {e}") from e
        return cls.loads(text)

    def save(self, path: os.PathLike | str) -> None:
        """Atomic: temp file in the same directory, fsync, rename over the target."""
        path = os.fspath(path)
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".app-settings.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(self.dumps().encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    # --- rules ---------------------------------------------------------------------------

    def to_ruleset(self) -> RuleSet:
        rules = []
        for i, (name, p) in enumerate(self._apps.values()):
            raw: dict[str, Any] = {"name": f"app: {name}", "app": name}
            if p.muted:
                raw["store_only"] = True
            if p.silent:
                raw["sound"] = "none"
            elif p.sound is not None:
                raw["sound"] = p.sound
            if p.volume is not None:
                raw["volume"] = p.volume
            if p.sticky:
                raw["timeout"] = "never"
            if p.blocked:
                raw["block"] = True
            if p.dnd_bypass:
                raw["dnd_bypass"] = True
            if p.icon is not None:
                raw["icon"] = p.icon
            try:
                rules.append(_build_rule(i, raw, None))
            except RulesError as e:
                raise AppSettingsError(str(e)) from e
        return RuleSet(rules)
