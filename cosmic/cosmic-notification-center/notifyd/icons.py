"""App icon resolution, in one place. Used by the toast host, the shade and the center.

Chain (first hit wins; a hit must be an existing file):
  1. the `image-path` hint
  2. `app_icon` (also what the `icon` rule action and the per-app icon setting write)
  3. the `desktop-entry` hint -> that .desktop file's `Icon=`
  4. a .desktop file whose `Name=`, `StartupWMClass=` or file id equals `app_name`
     (case-insensitive), scanned from the XDG data dirs' `applications/`
  5. nothing -> None (the pages then draw a letter avatar)

An icon value is a file:// URI, an absolute path, a `~` path or a themed name; themed names go
through `theme_lookup(name) -> filename | None` (`gtk_theme_lookup` in the daemon).

Decisions where the brief was silent:
- .desktop files are parsed here (small stdlib parser), not through Gio.DesktopAppInfo: Gio
  reads XDG_DATA_DIRS once per process and would make the temp-dir tests flaky. Gtk.IconTheme is
  still used for themed names.
- Only the `[Desktop Entry]` group is read; `Hidden=true` / `NoDisplay` entries still count (an
  app's notification icon does not depend on being listed in a launcher).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

REFRESH_SECONDS = 60.0
SIZE_TOAST = 48
SIZE_SHADE = 32
SIZE_CENTER = 32
SIZE_CENTER_DETAIL = 64

ThemeLookup = Callable[[str], "str | None"]


@dataclass(frozen=True)
class DesktopEntry:
    file_id: str  # "org.foo.Bar" (the file name without .desktop)
    name: str
    wm_class: str
    icon: str
    path: str


def xdg_data_dirs(env: dict[str, str] | None = None) -> list[Path]:
    e = os.environ if env is None else env
    home = e.get("XDG_DATA_HOME") or str(Path(e.get("HOME", "~")).expanduser() / ".local/share")
    dirs = [home, *(e.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")]
    return [Path(d) / "applications" for d in dirs if d]


def parse_desktop(text: str) -> dict[str, str]:
    """Keys of the `[Desktop Entry]` group: unlocalized, last wins. No escapes beyond `\\\\`."""
    out: dict[str, str] = {}
    inside = False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            inside = line == "[Desktop Entry]"
            continue
        if inside and "=" in line:
            k, _, v = line.partition("=")
            k = k.strip()
            if "[" not in k:
                out[k] = v.strip().replace("\\\\", "\\")
    return out


def _entry(path: Path, file_id: str) -> DesktopEntry | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            d = parse_desktop(f.read(1 << 16))
    except OSError:
        return None
    return DesktopEntry(
        file_id, d.get("Name", ""), d.get("StartupWMClass", ""), d.get("Icon", ""), str(path)
    )


class DesktopIndex:
    """All .desktop entries of the data dirs, rescanned at most every `refresh` seconds."""

    def __init__(
        self,
        dirs: Callable[[], Iterable[Path]] = xdg_data_dirs,
        clock: Callable[[], float] = time.monotonic,
        refresh: float = REFRESH_SECONDS,
    ) -> None:
        self._dirs = dirs
        self._clock = clock
        self._refresh = refresh
        self._at: float | None = None
        self._entries: list[DesktopEntry] = []
        self._by_id: dict[str, DesktopEntry] = {}

    def _scan(self) -> None:
        entries: list[DesktopEntry] = []
        seen: set[str] = set()
        for d in self._dirs():  # earlier dirs win, as in the XDG spec
            try:
                files = sorted(d.rglob("*.desktop"))
            except OSError:
                continue
            for p in files:
                fid = p.relative_to(d).as_posix()[: -len(".desktop")].replace("/", "-")
                if fid in seen:
                    continue
                e = _entry(p, fid)
                if e is not None:
                    seen.add(fid)
                    entries.append(e)
        self._entries = entries
        self._by_id = {e.file_id.casefold(): e for e in entries}
        self._at = self._clock()

    def _fresh(self) -> None:
        if self._at is None or self._clock() - self._at >= self._refresh:
            self._scan()

    def by_id(self, desktop_id: str) -> DesktopEntry | None:
        self._fresh()
        key = desktop_id.strip().casefold()
        if key.endswith(".desktop"):
            key = key[: -len(".desktop")]
        return self._by_id.get(key)

    def by_app_name(self, app_name: str) -> DesktopEntry | None:
        self._fresh()
        key = app_name.strip().casefold()
        if not key:
            return None
        for field in ("name", "wm_class", "file_id"):
            for e in self._entries:
                if e.icon and getattr(e, field).casefold() == key:
                    return e
        return None


_DEFAULT_INDEX = DesktopIndex()


def file_uri(path: str) -> str:
    from urllib.parse import quote

    return "file://" + quote(path, safe="/")


def icon_path(raw: str, theme_lookup: ThemeLookup) -> str | None:
    """One icon value -> an existing file path, or None."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    raw = raw.strip()
    path: str | None
    if raw.startswith("file://"):
        path = unquote(urlsplit(raw).path)
    elif raw.startswith("~"):
        path = os.path.expanduser(raw)
    elif raw.startswith("/"):
        path = raw
    elif "://" not in raw:
        path = theme_lookup(raw)
    else:
        path = None
    return path if path and Path(path).is_file() else None


def resolve_path(
    app_name: str,
    app_icon: str,
    hints: dict[str, Any],
    theme_lookup: ThemeLookup,
    index: DesktopIndex | None = None,
) -> str | None:
    index = _DEFAULT_INDEX if index is None else index
    for raw in (hints.get("image-path"), hints.get("image_path"), app_icon):
        p = icon_path(raw, theme_lookup) if isinstance(raw, str) else None
        if p:
            return p
    entry_id = hints.get("desktop-entry") or hints.get("desktop_entry")
    if isinstance(entry_id, str) and entry_id.strip():
        e = index.by_id(entry_id)
        p = icon_path(e.icon, theme_lookup) if e else None
        if p:
            return p
    if app_name:
        e = index.by_app_name(app_name)
        p = icon_path(e.icon, theme_lookup) if e else None
        if p:
            return p
    return None


def resolve(
    app_name: str,
    app_icon: str,
    hints: dict[str, Any],
    theme_lookup: ThemeLookup,
    index: DesktopIndex | None = None,
) -> str | None:
    """The icon as a file:// URI, or None."""
    p = resolve_path(app_name, app_icon, hints, theme_lookup, index)
    return file_uri(p) if p else None


def gtk_theme_lookup(size: int) -> ThemeLookup:
    """A lookup through the default Gtk icon theme at `size` px (None when Gtk is unavailable)."""

    def lookup(name: str) -> str | None:
        try:
            from gi.repository import Gtk

            info = Gtk.IconTheme.get_default().lookup_icon(name, size, 0)
            return info.get_filename() if info else None
        except Exception:
            return None

    return lookup


def icon_error(value: str, theme_lookup: ThemeLookup) -> str | None:
    """Why `value` (a per-app icon setting) is unusable, or None when it resolves."""
    if icon_path(value, theme_lookup):
        return None
    v = value.strip()
    if v.startswith(("/", "~", "file://")):
        return f"icon file not found: {v}"
    return f"no theme icon named {v!r}"
