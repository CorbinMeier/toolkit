"""theme.css: the Appearance panel's output. `--nc-*` custom properties only.

`VARIABLES` is the authoritative list of every `--nc-*` variable `web/default.css`, `web/shade.css`
(its `:root`) and `web/center.css` (its `.center` rule) define (a test keeps them equal).
`render_theme_css` validates each value by type before it can reach a stylesheet, so a value can
never close the rule, open a comment, import or fetch anything.

Layers: default.css -> theme.css (this module) -> style.css (hand-written).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

APP_DIR = "net.corbinmeier.notifications"
GROUPS = (
    "Colors",
    "Urgency",
    "Typography",
    "Shape",
    "Motion",
    "Layout",
    "Image",
    "Shade",
    "Center",
)
TYPES = ("color", "length", "number", "font", "duration", "shadow", "easing", "animation")

HEADER = (
    "/* theme.css: written by the notification center's Appearance panel. Edits here are\n"
    "   overwritten the next time you change a setting there. Put your own CSS in style.css,\n"
    "   which loads after this file. */\n"
)


@dataclass(frozen=True)
class Var:
    name: str  # full custom-property name, "--nc-bg"
    type: str
    default: str  # exactly as in web/default.css
    label: str
    group: str
    lo: float | None = None  # number range
    hi: float | None = None


VARIABLES: tuple[Var, ...] = (
    Var("--nc-bg", "color", "#1f2125", "Background", "Colors"),
    Var("--nc-fg", "color", "#e8eaed", "Text", "Colors"),
    Var("--nc-fg-muted", "color", "#9aa0a6", "Muted text", "Colors"),
    Var("--nc-accent", "color", "#8ab4f8", "Accent (links, actions)", "Colors"),
    Var("--nc-border-color", "color", "#3a3d44", "Border", "Colors"),
    Var("--nc-mark-bg", "color", "rgba(138, 180, 248, 0.25)", "Highlight background", "Colors"),
    Var("--nc-mark-fg", "color", "inherit", "Highlight text", "Colors"),
    Var("--nc-button-bg", "color", "rgba(255, 255, 255, 0.08)", "Button background", "Colors"),
    Var(
        "--nc-button-bg-hover",
        "color",
        "rgba(255, 255, 255, 0.16)",
        "Button background (hover)",
        "Colors",
    ),
    Var("--nc-avatar-saturation", "length", "45%", "Letter avatar saturation", "Colors"),
    Var("--nc-avatar-lightness", "length", "42%", "Letter avatar lightness", "Colors"),
    Var("--nc-avatar-fg", "color", "#ffffff", "Letter avatar text", "Colors"),
    Var("--nc-urgency-low", "color", "#6b7280", "Low urgency stripe", "Urgency"),
    Var("--nc-urgency-normal", "color", "#8ab4f8", "Normal urgency stripe", "Urgency"),
    Var("--nc-urgency-critical", "color", "#f28b82", "Critical urgency stripe", "Urgency"),
    Var("--nc-urgency-critical-bg", "color", "#2b1f21", "Critical background", "Urgency"),
    Var(
        "--nc-font-family",
        "font",
        '"Inter", "Noto Sans", system-ui, sans-serif',
        "Font family",
        "Typography",
    ),
    Var("--nc-font-size", "length", "14px", "Font size", "Typography"),
    Var("--nc-border-width", "length", "1px", "Border width", "Shape"),
    Var("--nc-radius", "length", "12px", "Corner radius", "Shape"),
    Var("--nc-shadow", "shadow", "0 6px 20px rgba(0, 0, 0, 0.45)", "Shadow", "Shape"),
    Var("--nc-opacity", "number", "0.97", "Opacity", "Shape", 0, 1),
    Var("--nc-anim-duration", "duration", "220ms", "Animation duration", "Motion"),
    Var(
        "--nc-enter-animation",
        "animation",
        "nc-enter var(--nc-anim-duration) ease-out both",
        "Enter animation",
        "Motion",
    ),
    Var(
        "--nc-exit-animation",
        "animation",
        "nc-exit var(--nc-anim-duration) ease-in both",
        "Exit animation",
        "Motion",
    ),
    Var("--nc-width", "length", "380px", "Toast width", "Layout"),
    Var("--nc-gap", "length", "10px", "Gap between toasts", "Layout"),
    Var("--nc-padding", "length", "12px", "Padding", "Layout"),
    Var("--nc-icon-size", "length", "40px", "Icon size", "Layout"),
    Var(
        "--nc-shadow-room",
        "length",
        "20px",
        "Shadow room (transparent margin so shadows are not clipped)",
        "Layout",
    ),
    Var("--nc-image-toast-height", "length", "140px", "Toast image height (max)", "Image"),
    Var("--nc-image-shade-height", "length", "96px", "Shade image height (max)", "Image"),
    Var("--nc-image-detail-height", "length", "360px", "Center image height (max)", "Image"),
    Var("--nc-image-list-size", "length", "36px", "Center list thumbnail size", "Image"),
    Var("--nc-image-radius", "length", "6px", "Image corner radius", "Image"),
    # web/shade.css :root
    Var("--nc-shade-width", "length", "420px", "Shade width", "Shade"),
    Var("--nc-shade-gap", "length", "8px", "Gap between shade rows", "Shade"),
    Var("--nc-shade-surface", "color", "rgba(255, 255, 255, 0.05)", "Row background", "Shade"),
    Var(
        "--nc-shade-surface-hover",
        "color",
        "rgba(255, 255, 255, 0.09)",
        "Row background (hover)",
        "Shade",
    ),
    Var("--nc-shade-divider", "color", "var(--nc-border-color)", "Divider", "Shade"),
    Var("--nc-shade-unread", "color", "var(--nc-accent)", "Unread marker", "Shade"),
    Var("--nc-shade-badge-bg", "color", "var(--nc-button-bg-hover)", "Badge background", "Shade"),
    Var("--nc-shade-badge-fg", "color", "var(--nc-fg)", "Badge text", "Shade"),
    Var("--nc-shade-anim-duration", "duration", "180ms", "Shade open/close duration", "Shade"),
    Var(
        "--nc-danger",
        "color",
        "var(--nc-urgency-critical)",
        "Danger (destructive actions)",
        "Urgency",
    ),
    # web/center.css .center
    Var("--nc-center-row-hover", "color", "rgba(255, 255, 255, 0.045)", "Row (hover)", "Center"),
    Var(
        "--nc-center-row-selected",
        "color",
        "rgba(138, 180, 248, 0.14)",
        "Row (selected)",
        "Center",
    ),
    Var("--nc-center-bar-bg", "color", "rgba(255, 255, 255, 0.03)", "Toolbar background", "Center"),
    Var("--nc-center-title-bg", "color", "var(--nc-bg)", "Title bar background", "Center"),
    Var("--nc-center-danger", "color", "var(--nc-urgency-critical)", "Danger", "Center"),
    Var("--nc-center-radius", "length", "min(var(--nc-radius), 8px)", "Corner radius", "Center"),
    Var("--nc-center-font-size", "length", "13px", "Font size", "Center"),
    Var("--nc-center-anim-duration", "duration", "160ms", "Open/close duration", "Center"),
)

BY_NAME: dict[str, Var] = {v.name: v for v in VARIABLES}

# ---- validation ---------------------------------------------------------------------------

_BANNED_SUBSTRINGS = ("/*", "*/", "url(", "expression", "@import", "@", "!important", "javascript:")
_BANNED_CHARS = set(";{}<>\\\n\r\0")

_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_COLOR_FN = re.compile(r"^(?:rgb|rgba|hsl|hsla)\(([0-9.,%/+\-\s]|deg|turn|rad)+\)$", re.I)
_NUM = r"-?(?:\d+(?:\.\d+)?|\.\d+)"
_LENGTH = re.compile(rf"^(?:{_NUM}(?:px|rem|em|%)|0)$")
_NUMBER = re.compile(rf"^{_NUM}$")
_VAR_REF = re.compile(r"^var\((--nc-[a-z0-9-]+)\)$")
_MIN_VAR = re.compile(rf"^min\(\s*var\((--nc-[a-z0-9-]+)\)\s*,\s*(?:{_NUM}(?:px|rem|em|%)|0)\s*\)$")
_DURATION = re.compile(r"^(?:\d+(?:\.\d+)?|\.\d+)(?:ms|s)$")
_FONT_ENTRY = re.compile(r"""^(?:"[^"\;{}<>]+"|'[^'\;{}<>]+'|[A-Za-z_-][A-Za-z0-9_ -]*)$""")
_EASING_KW = {"linear", "ease", "ease-in", "ease-out", "ease-in-out", "step-start", "step-end"}
_BEZIER = re.compile(rf"^cubic-bezier\(\s*{_NUM}\s*,\s*{_NUM}\s*,\s*{_NUM}\s*,\s*{_NUM}\s*\)$")
_STEPS = re.compile(
    r"^steps\(\s*\d+\s*(?:,\s*(?:jump-start|jump-end|jump-none|jump-both|start|end)\s*)?\)$"
)
_FILL = {"none", "forwards", "backwards", "both"}
KEYFRAMES = ("nc-enter", "nc-exit")  # the only @keyframes default.css defines

COLOR_NAMES = frozenset(
    """aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue
    blueviolet brown burlywood cadetblue chartreuse chocolate coral cornflowerblue cornsilk
    crimson cyan darkblue darkcyan darkgoldenrod darkgray darkgreen darkgrey darkkhaki
    darkmagenta darkolivegreen darkorange darkorchid darkred darksalmon darkseagreen
    darkslateblue darkslategray darkslategrey darkturquoise darkviolet deeppink deepskyblue
    dimgray dimgrey dodgerblue firebrick floralwhite forestgreen fuchsia gainsboro ghostwhite
    gold goldenrod gray green greenyellow grey honeydew hotpink indianred indigo ivory khaki
    lavender lavenderblush lawngreen lemonchiffon lightblue lightcoral lightcyan
    lightgoldenrodyellow lightgray lightgreen lightgrey lightpink lightsalmon lightseagreen
    lightskyblue lightslategray lightslategrey lightsteelblue lightyellow lime limegreen linen
    magenta maroon mediumaquamarine mediumblue mediumorchid mediumpurple mediumseagreen
    mediumslateblue mediumspringgreen mediumturquoise mediumvioletred midnightblue mintcream
    mistyrose moccasin navajowhite navy oldlace olive olivedrab orange orangered orchid
    palegoldenrod palegreen paleturquoise palevioletred papayawhip peachpuff peru pink plum
    powderblue purple rebeccapurple red rosybrown royalblue saddlebrown salmon sandybrown
    seagreen seashell sienna silver skyblue slateblue slategray slategrey snow springgreen
    steelblue tan teal thistle tomato turquoise violet wheat white whitesmoke yellow
    yellowgreen transparent currentcolor inherit""".split()
)


def _split_top(s: str, sep: str) -> list[str]:
    """Split on `sep` (a single char, or whitespace when sep == " ") outside parentheses and
    quotes."""
    parts, cur, depth, quote = [], [], 0, ""
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            cur.append(ch)
        elif ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            cur.append(ch)
        elif depth == 0 and (ch.isspace() if sep == " " else ch == sep):
            if cur or sep != " ":
                parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur or (sep != " " and parts):
        parts.append("".join(cur))
    if depth != 0 or quote:
        raise ValueError("unbalanced parentheses or quotes")
    parts = [p.strip() for p in parts]
    if sep != " " and not all(parts):
        raise ValueError("empty list entry")
    return parts


def _is_color(v: str) -> bool:
    return bool(_HEX.match(v) or _COLOR_FN.match(v) or v.lower() in COLOR_NAMES)


def _is_length(v: str, signed: bool = False) -> bool:
    return bool(_LENGTH.match(v)) and (signed or not v.startswith("-"))


def _is_easing(v: str) -> bool:
    return v in _EASING_KW or bool(_BEZIER.match(v) or _STEPS.match(v))


def _check_shadow(v: str) -> bool:
    if v == "none":
        return True
    for layer in _split_top(v, ","):
        toks = _split_top(layer, " ")
        inset = toks.count("inset")
        if inset > 1:
            return False
        toks = [t for t in toks if t != "inset"]
        lens = [t for t in toks if _is_length(t, signed=True)]
        colors = [t for t in toks if t not in lens]
        if not 2 <= len(lens) <= 4 or len(colors) > 1:
            return False
        if len(lens) >= 3 and lens[2].startswith("-"):
            return False  # negative blur
        if colors and not _is_color(colors[0]):
            return False
        # lengths must come together, not interleaved with the color
        idx = [i for i, t in enumerate(toks) if t in lens]
        if idx != list(range(idx[0], idx[0] + len(idx))):
            return False
    return True


def _check_font(v: str) -> bool:
    if len(v) > 300:
        return False
    entries = _split_top(v, ",")
    return bool(entries) and all(_FONT_ENTRY.match(e) for e in entries)


def _check_animation(v: str) -> bool:
    if v == "none":
        return True
    names = durations = easings = 0
    for t in _split_top(v, " "):
        if t in KEYFRAMES:
            names += 1
        elif _DURATION.match(t) or t == "var(--nc-anim-duration)":
            durations += 1
        elif _is_easing(t):
            easings += 1
        elif t not in _FILL:
            return False
    return names == 1 and durations <= 1 and easings <= 1


def validate(name: str, value: object) -> str:
    """The normalised value for `name`, or ValueError. Name is the full `--nc-*` name."""
    var = BY_NAME.get(name)
    if var is None:
        raise ValueError(f"unknown theme variable {name!r}")
    if not isinstance(value, str):
        raise ValueError(f"{name}: value must be a string")
    v = value.strip()
    if not v or len(v) > 400:
        raise ValueError(f"{name}: empty or too long")
    low = v.lower()
    if any(c in _BANNED_CHARS for c in v) or any(b in low for b in _BANNED_SUBSTRINGS):
        raise ValueError(f"{name}: forbidden characters in {value!r}")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in v):
        raise ValueError(f"{name}: control characters in value")
    t = var.type
    ref = _VAR_REF.match(v) or (_MIN_VAR.match(v) if t == "length" else None)
    if ref and t in ("color", "length", "duration"):
        # A reference to another known variable (the shipped defaults use these).
        if ref.group(1) in BY_NAME and ref.group(1) != name:
            return v
        raise ValueError(f"{name}: {value!r} refers to an unknown or the same variable")
    try:
        if t == "color":
            ok = _is_color(v)
        elif t == "length":
            ok = _is_length(v)
        elif t == "number":
            ok = bool(_NUMBER.match(v))
            if ok and var.lo is not None:
                ok = var.lo <= float(v) <= (var.hi if var.hi is not None else float("inf"))
        elif t == "duration":
            ok = bool(_DURATION.match(v))
        elif t == "font":
            ok = _check_font(v)
        elif t == "shadow":
            ok = _check_shadow(v)
        elif t == "easing":
            ok = _is_easing(v)
        else:
            ok = _check_animation(v)
    except ValueError:
        ok = False
    if not ok:
        raise ValueError(f"{name}: {value!r} is not a valid {t}")
    return v


# ---- rendering and files ------------------------------------------------------------------


def render_theme_css(values: dict[str, str]) -> str:
    """`:root { --nc-x: v; }` for exactly the given overrides (in VARIABLES order). Empty in,
    empty out. Raises ValueError on an unknown variable or an invalid value."""
    checked = {name: validate(name, v) for name, v in values.items()}
    if not checked:
        return ""
    # `--nc-center-*` are declared on `.center` in center.css, which would shadow a :root value,
    # so their overrides go on `.center` (theme.css loads after center.css).
    out = ""
    for selector, is_center in ((":root", False), (".center", True)):
        body = "".join(
            f"  {var.name}: {checked[var.name]};\n"
            for var in VARIABLES
            if var.name in checked and var.name.startswith("--nc-center-") == is_center
        )
        if body:
            out += f"{selector} {{\n{body}}}\n"
    return out


def theme_path() -> Path:
    """The live theme.css location. Callers pass explicit paths in tests."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_DIR / "theme.css"


def read_theme(path: str | os.PathLike) -> dict[str, str]:
    """Overrides in a theme.css. Missing or unreadable means none; a declaration that is not a
    known variable with a valid value is skipped (a hand-edited file never breaks the panel)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return {}
    return read_theme_text(text)


def read_theme_text(text: str) -> dict[str, str]:
    """`read_theme` on text already in memory."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    out: dict[str, str] = {}
    for m in re.finditer(r"(--nc-[a-z0-9-]+)\s*:\s*([^;{}]*);", text):
        try:
            out[m.group(1)] = validate(m.group(1), m.group(2).strip())
        except ValueError:
            continue
    return out


def write_theme(path: str | os.PathLike, values: dict[str, str]) -> None:
    """Validate, then write theme.css atomically with the UI-managed header."""
    from notifyd.config_edit import atomic_write

    atomic_write(Path(path), HEADER + render_theme_css(values))


# ---- presets --------------------------------------------------------------------------------

PRESET_LABELS = {
    "default": "Default",
    "crt-green": "CRT Green",
    "night-ops-hud": "Night Ops HUD",
}

PRESETS: dict[str, dict[str, str]] = {
    "default": {},
    # Green phosphor on black, after the lock screen (cosmic-crt-lock): VT323, glow, square corners.
    "crt-green": {
        "--nc-bg": "#020a05",
        "--nc-fg": "#72ffa6",
        "--nc-fg-muted": "#3ccd72",
        "--nc-accent": "#b6ffd0",
        "--nc-border-color": "#1f8a4b",
        "--nc-mark-bg": "rgba(61, 255, 122, 0.28)",
        "--nc-button-bg": "rgba(61, 255, 122, 0.08)",
        "--nc-button-bg-hover": "rgba(61, 255, 122, 0.2)",
        "--nc-urgency-low": "#1f8a4b",
        "--nc-urgency-normal": "#3dff7a",
        "--nc-urgency-critical": "#ff6a3d",
        "--nc-urgency-critical-bg": "#1a0a05",
        "--nc-font-family": '"VT323", "Fira Mono", ui-monospace, monospace',
        "--nc-font-size": "19px",
        "--nc-border-width": "1px",
        "--nc-radius": "0px",
        "--nc-shadow": "0 0 14px rgba(61, 255, 122, 0.35), inset 0 0 8px rgba(61, 255, 122, 0.12)",
        "--nc-opacity": "0.98",
        "--nc-anim-duration": "140ms",
    },
    # The desktop dashboard's "Night Ops HUD" (DESIGN.md): deep console panels, teal signal,
    # ember gold highlights, Fira Mono, no rounded corners (the notch itself needs style.css).
    "night-ops-hud": {
        "--nc-bg": "#171b23",
        "--nc-fg": "#eef2f8",
        "--nc-fg-muted": "#8e9bb0",
        "--nc-accent": "#2be4ea",
        "--nc-border-color": "#1f6a6e",
        "--nc-mark-bg": "rgba(254, 211, 63, 0.22)",
        "--nc-mark-fg": "#fed33f",
        "--nc-button-bg": "rgba(43, 228, 234, 0.08)",
        "--nc-button-bg-hover": "rgba(43, 228, 234, 0.2)",
        "--nc-urgency-low": "#5b6577",
        "--nc-urgency-normal": "#2be4ea",
        "--nc-urgency-critical": "#e8615a",
        "--nc-urgency-critical-bg": "#2a1618",
        "--nc-font-family": '"Fira Mono", ui-monospace, monospace',
        "--nc-font-size": "13px",
        "--nc-border-width": "2px",
        "--nc-radius": "0px",
        "--nc-shadow": "0 0 12px rgba(43, 228, 234, 0.16)",
        "--nc-opacity": "0.96",
        "--nc-anim-duration": "160ms",
    },
}


def apply_preset(name: str) -> dict[str, str]:
    """A copy of the preset's values (validated). Unknown name raises KeyError."""
    values = dict(PRESETS[name])
    render_theme_css(values)  # a broken preset fails here, not in the panel
    return values


def preset_css(name: str) -> str:
    """The text of web/presets/<name>.css."""
    css = render_theme_css(PRESETS[name])
    return (
        f"/* {PRESET_LABELS[name]} preset: copy into theme.css, or pick it in the panel. */\n" + css
    )


def variables_table() -> str:
    """Markdown table of VARIABLES, for docs/theming.md."""
    rows = ["| Variable | Group | Type | Default | Controls |", "|---|---|---|---|---|"]
    for v in VARIABLES:
        default = v.default.replace("|", "\\|")
        rows.append(f"| `{v.name}` | {v.group} | {v.type} | `{default}` | {v.label} |")
    return "\n".join(rows)
