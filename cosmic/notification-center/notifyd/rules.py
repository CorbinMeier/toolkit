"""Rules engine: parse `rules.toml`, validate it, apply it to a Notification.

Pure Python, stdlib only, no GLib: the daemon wires it in (hot reload, the `process` hook).
A failed load raises RulesError, so the caller can keep its previous good RuleSet.

Decisions where the brief was silent (also in docs/rules.md):
- The key `urgency` is the MATCH field. The override action is `set_urgency`.
- `set_urgency` updates `n.urgency` only; `timeout_ms` is not recomputed (use `timeout`).
- `sound = "none"` is stored literally as "none" in `n.sound`; the sound module interprets it.
  A sound starting with `~` is expanded to an absolute path at load.
- `volume` without `sound` still sets `n.sound_volume`.
- `block` wins over `store_only` when one rule sets both; across rules the later one wins.
- `stop = true` only takes effect on a rule that matched.
- Hint values are matched as `str(value)` (so a bool is "True", a list is "['a', 'b']").
- `timeout` is a positive int (ms) or "never".
- `category` is exact and case-sensitive; `app` is exact and case-insensitive.
- Disabled rules are still validated at load and still appear in `names`.
- `run` appends one entry to `n.runs`; `buttons` append to `n.buttons`. Buttons are de-duplicated
  by (label, run, builtin, minutes), capped at MAX_BUTTONS (extras dropped with a WARNING naming
  the rules), and given ids b0, b1, ... in final order after all rules ran. A blocked or
  store_only notification still gets `runs`/`buttons`; the daemon decides what to do with them.
- Execution lives elsewhere (runner.py); this module only parses, validates and records.
"""

from __future__ import annotations

import copy
import logging
import os
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from notifyd.model import Notification, State, Urgency

log = logging.getLogger("notifyd.rules")

MAX_MATCH_CHARS = 10_000
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
URGENCIES = {"low": Urgency.LOW, "normal": Urgency.NORMAL, "critical": Urgency.CRITICAL}

MATCH_KEYS = frozenset(
    {"app", "app_regex", "summary", "body", "urgency", "category", "hint", "hint_present"}
)
ACTION_KEYS = frozenset(
    {
        "block",
        "store_only",
        "sound",
        "volume",
        "timeout",
        "set_urgency",
        "icon",
        "class",
        "highlight",
        "tags",
        "dnd_bypass",
        "stop",
        "run",
        "run_when",
        "cooldown",
        "run_timeout",
        "buttons",
    }
)
RUN_OPTION_KEYS = frozenset({"run_when", "cooldown", "run_timeout"})
RUN_WHEN = ("arrive", "shown")
BUILTINS = frozenset(
    {"details", "copy-body", "copy-summary", "open-link", "mark-read", "dismiss", "snooze"}
)
# Builtins that keep the toast open by default (the others close it).
BUILTIN_KEEP_OPEN = frozenset({"details", "copy-body", "copy-summary", "open-link"})
BUTTON_KEYS = frozenset({"label", "run", "builtin", "minutes", "close"})
MAX_BUTTONS = 6
MAX_LABEL_CHARS = 40
GENERAL_KEYS = frozenset({"name", "enabled", "match_mode"})
ALL_KEYS = MATCH_KEYS | ACTION_KEYS | GENERAL_KEYS

# Syntax Python's re accepts that the toast's JS RegExp does not (highlight patterns only).
_INLINE_FLAGS = re.compile(r"\(\?[a-zA-Z-]+[):]")
_LEADING_FLAGS = re.compile(r"^\(\?[ims]+\)")


class RulesError(Exception):
    """A rules file that cannot be loaded. `rule_index` (0-based) and `line` are set when known."""

    def __init__(self, message: str, *, rule_index: int | None = None, line: int | None = None):
        super().__init__(message)
        self.rule_index = rule_index
        self.line = line


@dataclass
class _Rule:
    name: str
    enabled: bool = True
    match_any: bool = False
    # Each matcher takes the notification and returns bool.
    matchers: list[Callable[[Notification], bool]] = field(default_factory=list)
    block: bool = False
    store_only: bool = False
    sound: str | None = None
    volume: int | None = None
    timeout: tuple[int | None] | None = None  # 1-tuple so None ("never") is distinguishable
    set_urgency: Urgency | None = None
    icon: str | None = None
    classes: list[str] = field(default_factory=list)
    highlights: list[dict[str, str]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    dnd_bypass: bool = False
    stop: bool = False
    run: dict[str, Any] | None = None  # {"cmd", "when", "cooldown", "timeout"}
    buttons: list[dict[str, Any]] = field(default_factory=list)  # without ids

    def matches(self, n: Notification) -> bool:
        if not self.matchers:
            return True
        results = (m(n) for m in self.matchers)
        return any(results) if self.match_any else all(results)


def _extend_unique(target: list, items: list) -> None:
    for item in items:
        if item not in target:
            target.append(item)


def _clip(text: Any) -> str:
    return str(text)[:MAX_MATCH_CHARS]


class _Ctx:
    """Error-message helper bound to one rule."""

    def __init__(self, index: int, name: str, line: int | None):
        self.index = index
        self.name = name
        self.line = line

    def where(self) -> str:
        where = f"rule #{self.index + 1} ({self.name!r})"
        if self.line:
            where += f", line {self.line}"
        return where

    def err(self, message: str) -> RulesError:
        return RulesError(f"{self.where()}: {message}", rule_index=self.index, line=self.line)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _str(ctx: _Ctx, key: str, v: Any) -> str:
    if not isinstance(v, str):
        raise ctx.err(f"`{key}` must be a string, got {type(v).__name__}")
    return v


def _bool(ctx: _Ctx, key: str, v: Any) -> bool:
    if not isinstance(v, bool):
        raise ctx.err(f"`{key}` must be true or false, got {type(v).__name__}")
    return v


def _str_list(ctx: _Ctx, key: str, v: Any, *, allow_scalar: bool) -> list[str]:
    if isinstance(v, str) and allow_scalar:
        return [v]
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        what = "a string or a list of strings" if allow_scalar else "a list of strings"
        raise ctx.err(f"`{key}` must be {what}")
    return list(v)


def _command(ctx: _Ctx, key: str, v: Any) -> str | list[str]:
    """A shell string (run via sh -c) or an argv list (run directly). Never empty."""
    if isinstance(v, str):
        if not v.strip():
            raise ctx.err(f"`{key}` must not be empty")
        return v
    if isinstance(v, list) and v and all(isinstance(x, str) and x for x in v):
        return list(v)
    raise ctx.err(f"`{key}` must be a non-empty string or a non-empty list of non-empty strings")


def _int_range(ctx: _Ctx, key: str, v: Any, lo: int, hi: int | None) -> int:
    if not _is_int(v) or v < lo or (hi is not None and v > hi):
        rng = f"{lo} to {hi}" if hi is not None else f"{lo} or more"
        raise ctx.err(f"`{key}` must be an integer from {rng}, got {v!r}")
    return v


def _buttons(ctx: _Ctx, v: Any) -> list[dict[str, Any]]:
    if not isinstance(v, list) or not v:
        raise ctx.err("`buttons` must be a non-empty list of tables: [[rule.buttons]]")
    out = []
    for i, item in enumerate(v):
        where = f"buttons[{i}]"
        if not isinstance(item, dict):
            raise ctx.err(f"`{where}` must be a table")
        unknown = sorted(set(item) - BUTTON_KEYS)
        if unknown:
            raise ctx.err(f"unknown key(s) in `{where}`: {', '.join(unknown)}")
        if "label" not in item:
            raise ctx.err(f"`{where}` is missing `label`")
        label = _str(ctx, f"{where}.label", item["label"])
        if not label.strip():
            raise ctx.err(f"`{where}.label` must not be empty")
        if len(label) > MAX_LABEL_CHARS:
            raise ctx.err(f"`{where}.label` is longer than {MAX_LABEL_CHARS} characters")
        if ("run" in item) == ("builtin" in item):
            raise ctx.err(f"`{where}` needs exactly one of `run` or `builtin`")
        run = builtin = minutes = None
        if "run" in item:
            run = _command(ctx, f"{where}.run", item["run"])
            if "minutes" in item:
                raise ctx.err(f'`{where}.minutes` is only valid with builtin = "snooze"')
        else:
            builtin = _str(ctx, f"{where}.builtin", item["builtin"])
            if builtin not in BUILTINS:
                raise ctx.err(
                    f"`{where}.builtin` must be one of {', '.join(sorted(BUILTINS))}, "
                    f"got {builtin!r}"
                )
            if builtin == "snooze":
                if "minutes" not in item:
                    raise ctx.err(f"`{where}` (snooze) needs `minutes`")
                minutes = _int_range(ctx, f"{where}.minutes", item["minutes"], 1, 10080)
            elif "minutes" in item:
                raise ctx.err(f'`{where}.minutes` is only valid with builtin = "snooze"')
        close = builtin not in BUILTIN_KEEP_OPEN
        if "close" in item:
            close = _bool(ctx, f"{where}.close", item["close"])
        out.append(
            {"label": label, "run": run, "builtin": builtin, "minutes": minutes, "close": close}
        )
    return out


def _regex(ctx: _Ctx, key: str, v: Any) -> re.Pattern[str]:
    pattern = _str(ctx, key, v)
    try:
        return re.compile(pattern)
    except re.error as e:
        raise ctx.err(f"`{key}` is not a valid regex ({e}): {pattern!r}") from e


def _ident(ctx: _Ctx, key: str, v: str) -> str:
    if not IDENT_RE.fullmatch(v):
        raise ctx.err(
            f"`{key}` value {v!r} is not a CSS-safe identifier "
            "(letters, digits, '_' and '-', not starting with a digit or '-')"
        )
    return v


def js_incompatibilities(pattern: str) -> list[str]:
    """Python-only regex syntax that a JS RegExp (the toast highlighter) will not understand."""
    found = []
    if "(?P<" in pattern or "(?P=" in pattern or "(?P>" in pattern:
        found.append("named group (?P<...>)")
    if "\\A" in pattern or "\\Z" in pattern:
        found.append("\\A or \\Z anchor")
    for m in _INLINE_FLAGS.finditer(pattern):
        if m.start() == 0 and _LEADING_FLAGS.match(pattern):
            continue
        found.append(f"inline flags {m.group(0)!r}")
    return found


def _highlight(ctx: _Ctx, v: Any) -> list[dict[str, str]]:
    items = v if isinstance(v, list) else [v]
    out = []
    for item in items:
        if not isinstance(item, dict):
            raise ctx.err("`highlight` must be a table {pattern, class} or a list of them")
        unknown = set(item) - {"pattern", "class"}
        if unknown:
            raise ctx.err(f"unknown key(s) in `highlight`: {', '.join(sorted(unknown))}")
        if "pattern" not in item or "class" not in item:
            raise ctx.err("`highlight` needs both `pattern` and `class`")
        pattern = _str(ctx, "highlight.pattern", item["pattern"])
        _regex(ctx, "highlight.pattern", pattern)
        cls = _ident(ctx, "highlight.class", _str(ctx, "highlight.class", item["class"]))
        for problem in js_incompatibilities(pattern):
            log.warning(
                "%s: highlight pattern %r uses Python-only syntax (%s); "
                "the toast highlighter is JavaScript and may ignore it",
                ctx.where(),
                pattern,
                problem,
            )
        out.append({"pattern": pattern, "class": cls})
    return out


def _parse_urgency(ctx: _Ctx, key: str, v: Any) -> list[Urgency]:
    names = _str_list(ctx, key, v, allow_scalar=True)
    if not names:
        raise ctx.err(f"`{key}` must not be empty")
    out = []
    for name in names:
        if name.lower() not in URGENCIES:
            raise ctx.err(f"`{key}` value {name!r} must be one of low, normal, critical")
        out.append(URGENCIES[name.lower()])
    return out


def _build_matchers(ctx: _Ctx, raw: dict[str, Any]) -> list[Callable[[Notification], bool]]:
    ms: list[Callable[[Notification], bool]] = []
    if "app" in raw:
        app = _str(ctx, "app", raw["app"]).casefold()
        ms.append(lambda n: n.app_name.casefold() == app)
    if "app_regex" in raw:
        rx = _regex(ctx, "app_regex", raw["app_regex"])
        ms.append(lambda n: rx.search(_clip(n.app_name)) is not None)
    for key in ("summary", "body"):
        if key in raw:
            rx = _regex(ctx, key, raw[key])
            attr = key
            ms.append(lambda n, rx=rx, attr=attr: rx.search(_clip(getattr(n, attr))) is not None)
    if "urgency" in raw:
        levels = _parse_urgency(ctx, "urgency", raw["urgency"])
        ms.append(lambda n: n.urgency in levels)
    if "category" in raw:
        cats = _str_list(ctx, "category", raw["category"], allow_scalar=True)
        ms.append(lambda n: n.category in cats)
    if "hint" in raw:
        table = raw["hint"]
        if not isinstance(table, dict):
            raise ctx.err('`hint` must be a table of key = "regex"')
        pairs = []
        for k, v in table.items():
            pairs.append((k, _regex(ctx, f"hint.{k}", v)))

        def hint_match(n: Notification, pairs=pairs) -> bool:
            for k, rx in pairs:
                if k not in n.hints or rx.search(_clip(n.hints[k])) is None:
                    return False
            return True

        ms.append(hint_match)
    if "hint_present" in raw:
        keys = _str_list(ctx, "hint_present", raw["hint_present"], allow_scalar=False)
        ms.append(lambda n: all(k in n.hints for k in keys))
    return ms


def _build_rule(index: int, raw: Any, line: int | None) -> _Rule:
    if not isinstance(raw, dict):
        raise RulesError(f"rule #{index + 1}: must be a table", rule_index=index, line=line)
    name = raw.get("name")
    label = name if isinstance(name, str) and name else "<unnamed>"
    ctx = _Ctx(index, label, line)
    if "name" not in raw:
        raise ctx.err("missing required `name`")
    if not isinstance(name, str) or not name.strip():
        raise ctx.err("`name` must be a non-empty string")
    unknown = sorted(set(raw) - ALL_KEYS)
    if unknown:
        raise ctx.err(f"unknown key(s): {', '.join(unknown)} (check for a typo; see docs/rules.md)")

    rule = _Rule(name=name)
    if "enabled" in raw:
        rule.enabled = _bool(ctx, "enabled", raw["enabled"])
    if "match_mode" in raw:
        mode = _str(ctx, "match_mode", raw["match_mode"])
        if mode not in ("all", "any"):
            raise ctx.err(f'`match_mode` must be "all" or "any", got {mode!r}')
        rule.match_any = mode == "any"
    rule.matchers = _build_matchers(ctx, raw)

    if "block" in raw:
        rule.block = _bool(ctx, "block", raw["block"])
    if "store_only" in raw:
        rule.store_only = _bool(ctx, "store_only", raw["store_only"])
    if "sound" in raw:
        sound = _str(ctx, "sound", raw["sound"])
        if not sound.strip():
            raise ctx.err('`sound` must not be empty (use "none" for silence)')
        rule.sound = os.path.expanduser(sound) if sound.startswith("~") else sound
    if "volume" in raw:
        vol = raw["volume"]
        if not _is_int(vol) or not 0 <= vol <= 100:
            raise ctx.err(f"`volume` must be an integer from 0 to 100, got {vol!r}")
        rule.volume = vol
    if "timeout" in raw:
        t = raw["timeout"]
        if t == "never":
            rule.timeout = (None,)
        elif _is_int(t) and t > 0:
            rule.timeout = (t,)
        else:
            raise ctx.err(f'`timeout` must be a positive integer (ms) or "never", got {t!r}')
    if "set_urgency" in raw:
        rule.set_urgency = _parse_urgency(ctx, "set_urgency", raw["set_urgency"])[0]
        if isinstance(raw["set_urgency"], list):
            raise ctx.err("`set_urgency` must be a single level, not a list")
    if "icon" in raw:
        rule.icon = _str(ctx, "icon", raw["icon"])
    if "class" in raw:
        rule.classes = [
            _ident(ctx, "class", c)
            for c in _str_list(ctx, "class", raw["class"], allow_scalar=True)
        ]
    if "highlight" in raw:
        rule.highlights = _highlight(ctx, raw["highlight"])
    if "tags" in raw:
        rule.tags = [
            _ident(ctx, "tags", t) for t in _str_list(ctx, "tags", raw["tags"], allow_scalar=False)
        ]
    if "dnd_bypass" in raw:
        rule.dnd_bypass = _bool(ctx, "dnd_bypass", raw["dnd_bypass"])
    if "stop" in raw:
        rule.stop = _bool(ctx, "stop", raw["stop"])
    if "run" in raw:
        rule.run = {
            "cmd": _command(ctx, "run", raw["run"]),
            "when": "arrive",
            "cooldown": 0,
            "timeout": 30,
        }
        if "run_when" in raw:
            when = _str(ctx, "run_when", raw["run_when"])
            if when not in RUN_WHEN:
                raise ctx.err(f'`run_when` must be "arrive" or "shown", got {when!r}')
            rule.run["when"] = when
        if "cooldown" in raw:
            rule.run["cooldown"] = _int_range(ctx, "cooldown", raw["cooldown"], 0, None)
        if "run_timeout" in raw:
            rule.run["timeout"] = _int_range(ctx, "run_timeout", raw["run_timeout"], 1, 3600)
    else:
        stray = sorted(RUN_OPTION_KEYS & set(raw))
        if stray:
            raise ctx.err(f"`{', '.join(stray)}` only valid together with `run`")
    if "buttons" in raw:
        rule.buttons = _buttons(ctx, raw["buttons"])
    return rule


def _rule_lines(text: str) -> list[int]:
    """1-based line of each `[[rule]]` header, in order (best effort; ignores string contents)."""
    header = re.compile(r"^\s*\[\[\s*rule\s*\]\]")
    return [i for i, line in enumerate(text.splitlines(), 1) if header.match(line)]


class RuleSet:
    """An ordered, validated list of rules. Immutable after construction."""

    def __init__(self, rules: list[_Rule] | None = None):
        self._rules = list(rules or [])
        self.names: list[str] = [r.name for r in self._rules]

    @classmethod
    def empty(cls) -> RuleSet:
        return cls([])

    @classmethod
    def from_toml(cls, text: str) -> RuleSet:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise RulesError(f"rules file is not valid TOML: {e}") from e
        extra = sorted(set(data) - {"rule"})
        if extra:
            raise RulesError(
                f"unknown top-level key(s): {', '.join(extra)} (rules go under [[rule]])"
            )
        raw_rules = data.get("rule", [])
        if not isinstance(raw_rules, list):
            raise RulesError("`rule` must be an array of tables: write [[rule]], not [rule]")
        lines = _rule_lines(text)
        rules: list[_Rule] = []
        seen: dict[str, int] = {}
        for i, raw in enumerate(raw_rules):
            line = lines[i] if len(lines) == len(raw_rules) else None
            rule = _build_rule(i, raw, line)
            if rule.name in seen:
                raise RulesError(
                    f"rule #{i + 1} ({rule.name!r})"
                    + (f", line {line}" if line else "")
                    + f": duplicate name (same as rule #{seen[rule.name] + 1})",
                    rule_index=i,
                    line=line,
                )
            seen[rule.name] = i
            rules.append(rule)
        return cls(rules)

    @classmethod
    def load(cls, path: os.PathLike | str) -> RuleSet:
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except FileNotFoundError:
            return cls.empty()
        except OSError as e:
            raise RulesError(f"cannot read rules file {os.fspath(path)}: {e}") from e
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise RulesError(f"rules file {os.fspath(path)} is not valid UTF-8: {e}") from e
        return cls.from_toml(text)

    def apply(self, n: Notification) -> Notification:
        """Return a modified copy of `n`; the input is never mutated."""
        out = copy.deepcopy(n)
        sources: list[str] = ["" for _ in out.buttons]  # rule name per button, for the cap warning
        for rule in self._rules:
            if not rule.enabled or not rule.matches(out):
                continue
            self._apply_rule(rule, out, sources)
            if rule.stop:
                break
        self._finish_buttons(out, sources)
        return out

    @staticmethod
    def _finish_buttons(n: Notification, sources: list[str]) -> None:
        """De-duplicate (first wins), cap at MAX_BUTTONS, assign ids b0, b1, ... in order."""
        seen: set[tuple] = set()
        kept: list[dict[str, Any]] = []
        dropped: list[str] = []
        for b, src in zip(n.buttons, sources, strict=True):
            key = (
                b["label"],
                tuple(b["run"]) if isinstance(b["run"], list) else b["run"],
                b["builtin"],
                b["minutes"],
            )
            if key in seen:
                continue
            seen.add(key)
            if len(kept) >= MAX_BUTTONS:
                dropped.append(src or "?")
                continue
            kept.append({**b, "id": f"b{len(kept)}"})
        if dropped:
            log.warning(
                "notification %s: more than %d buttons; dropped %d from rule(s): %s",
                n.id,
                MAX_BUTTONS,
                len(dropped),
                ", ".join(dict.fromkeys(dropped)),
            )
        n.buttons = [
            {k: b[k] for k in ("id", "label", "run", "builtin", "minutes", "close")} for b in kept
        ]

    @staticmethod
    def _apply_rule(rule: _Rule, n: Notification, sources: list[str]) -> None:
        n.matched_rules.append(rule.name)
        if rule.block:
            n.state = State.BLOCKED
        elif rule.store_only:
            n.state = State.STORE_ONLY
        if rule.sound is not None:
            n.sound = rule.sound
        if rule.volume is not None:
            n.sound_volume = rule.volume
        if rule.timeout is not None:
            n.timeout_ms = rule.timeout[0]
        if rule.set_urgency is not None:
            n.urgency = rule.set_urgency
        if rule.icon is not None:
            n.app_icon = rule.icon
            # the rule replaces the app's icon: an `image-path` hint would otherwise outrank it
            n.hints.pop("image-path", None)
            n.hints.pop("image_path", None)
        if rule.dnd_bypass:
            n.dnd_bypass = True
        _extend_unique(n.classes, rule.classes)
        _extend_unique(n.tags, rule.tags)
        _extend_unique(n.highlights, rule.highlights)
        if rule.run is not None:
            n.runs.append({"rule": rule.name, **copy.deepcopy(rule.run)})
        for b in rule.buttons:
            n.buttons.append(copy.deepcopy(b))
            sources.append(rule.name)

    def __len__(self) -> int:
        return len(self._rules)
