"""Shared by the dev demos: icon resolution reads a temp applications dir, never the real
.desktop files (the XDG env is left alone so GTK keeps its mime database and pixbuf loaders).

`setup(tmp_dir)` swaps the default desktop index (call it before the first notification
resolves), writes the fake "Calendar Pro" .desktop (Icon= a temp SVG) and a second
SVG for an explicit-icon app, and returns (desktop_app_icon_path, explicit_icon_path)."""

from pathlib import Path

from notifyd import icons

SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">'
    '<rect x="4" y="4" width="56" height="56" rx="12" fill="{c}"/>'
    '<circle cx="32" cy="32" r="14" fill="white"/></svg>'
)


def setup(tmp: str | Path) -> tuple[str, str]:
    root = Path(tmp)
    apps = root / "xdg" / "applications"
    apps.mkdir(parents=True)
    icons._DEFAULT_INDEX = icons.DesktopIndex(lambda: [apps])
    cal = root / "calendar.svg"
    cal.write_text(SVG.format(c="#2e8b57"))
    explicit = root / "photo.svg"
    explicit.write_text(SVG.format(c="#c0392b"))
    (apps / "calendar-pro.desktop").write_text(
        f"[Desktop Entry]\nType=Application\nName=Calendar Pro\nIcon={cal}\n"
    )
    return str(cal), str(explicit)
