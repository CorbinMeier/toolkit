"""Block-level, comment-preserving editor for `rules.toml` (the Rules panel's engine).

A file is split into a HEADER (everything above the first rule's leading comments) and one BLOCK
per `[[rule]]`:

    lead   the comment lines directly above the `[[rule]]` header (no blank line between)
    body   the `[[rule]]` line, its keys and its `[[rule.buttons]]` / `[rule.hint]` sub-tables,
           up to the last line that holds TOML
    tail   what follows the body until the next block's lead: blank lines and free-floating
           comments (commented-out rules included). They travel with the block they follow.

Joining header + blocks gives back the file byte for byte, so an untouched file is never
altered, and every operation rewrites only the block(s) it names.

Operations take and return TEXT (`update_rule`, `add_rule`, `delete_rule`, `move_rule`,
`set_enabled`); each validates the whole result with `RuleSet.from_toml` (the daemon's own
validator, never a copy) and raises `RulesEditError({field_path: message})` instead of
returning bad text. `write_file` is the atomic, validated file write.

How `update_rule` edits a block (decisions, also in docs/rules.md):
- A rule whose data did not change is returned byte for byte.
- Otherwise it patches the existing lines: keys whose value is unchanged keep their line and
  inline comment; a changed key keeps its position, indent and trailing comment and gets a new
  value; a removed key loses its line (comments above it stay); a new key is inserted after the
  last key that precedes it in canonical order. `[rule.hint]` and each `[[rule.buttons]]` that
  did not change are kept verbatim; a changed one is rewritten under its own header (its
  leading comment stays). New hint / buttons become sub-tables at the end of the block.
- If the block cannot be patched safely (dotted keys, anything odd) or the patched text does
  not parse back to exactly the data, the block body is re-serialized cleanly. The leading
  comment and the tail are kept either way.
- Data cleanup before writing: null, "" and empty lists/tables are dropped; a flag that is
  `false`, `enabled = true`, `match_mode = "all"`, `run_when = "arrive"`, `cooldown = 0` and
  `run_timeout = 30` are dropped unless the rule already had that key.
- Moving or deleting a block takes its comments with it. A moved block gets a blank line after
  it when it lands before another block; a block that becomes last keeps the file's original
  trailing blank-line count.
- `list_rules` needs one `[[rule]]` header per rule: `rule = [{...}]` inline arrays, other
  top-level tables, or keys above the first rule make it raise (use the raw editor then).
"""

from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from notifyd.config_edit import _parse, _Rec, atomic_write
from notifyd.rules import RulesError, RuleSet

MAX_SUMMARY = 220

# Canonical key order of a freshly written block (and where new keys are inserted).
KEY_ORDER = (
    "name",
    "enabled",
    "match_mode",
    "app",
    "app_regex",
    "summary",
    "body",
    "urgency",
    "category",
    "hint_present",
    "hint",
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
)
RANK = {k: i for i, k in enumerate(KEY_ORDER)}
BUTTON_ORDER = ("label", "run", "builtin", "minutes", "close")
FLAG_KEYS = ("block", "store_only", "dnd_bypass", "stop")
DEFAULTS = {
    "enabled": True,
    "match_mode": "all",
    "run_when": "arrive",
    "cooldown": 0,
    "run_timeout": 30,
}
SUBTABLE_KEYS = ("hint", "buttons")
_BARE = re.compile(r"[A-Za-z0-9_-]+")
_LINES = re.compile(r"[^\n]*\n|[^\n]+")
_PREFIX = re.compile(r"^rule #\d+ \(.*?\)(?:, line \d+)?: ", re.S)
_TICKED = re.compile(r"`([^`]+)`")
_UNKNOWN_IN = re.compile(r"unknown key\(s\) in `([^`]+)`: ([^\s,]+)")
_UNKNOWN = re.compile(r"unknown key\(s\): ([^\s,(]+)")


class RulesEditError(Exception):
    """An edit that was refused. `errors` maps a field path ("app_regex", "buttons[1].label",
    "hint.x-status", "name"; "" for anything general) to a message. Nothing was written."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(f"{k}: {v}" if k else v for k, v in errors.items()) or "invalid")
        self.errors = errors


class _Unpatchable(Exception):
    pass


# ---- text model ------------------------------------------------------------------------------


def _lines(text: str) -> list[str]:
    return _LINES.findall(text)


def _is_comment(line: str) -> bool:
    return line.lstrip().startswith("#")


def _is_blank(line: str) -> bool:
    return not line.strip()


def _nl(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


@dataclass
class _Block:
    lead: list[str] = field(default_factory=list)
    body: list[str] = field(default_factory=list)
    tail: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        return self.lead + self.body + self.tail

    def text(self) -> str:
        return "".join(self.lines())


def _split(text: str) -> tuple[list[str], list[_Block]]:
    """(header lines, blocks). Raises RulesEditError for layouts the editor cannot place."""
    lines = _lines(text)
    try:
        recs = _parse(lines)
    except ValueError as e:
        raise RulesEditError({"": f"rules.toml cannot be parsed for editing: {e}"}) from e
    starts: list[int] = []
    for r in recs:
        if r.kind == "aot" and r.name == "rule":
            starts.append(r.start)
        elif r.kind in ("aot", "header") and not r.name.startswith("rule."):
            raise RulesEditError({"": f"line {r.start + 1}: unsupported table [{r.name}]"})
        elif r.kind in ("key", "other") and not starts:
            raise RulesEditError({"": f"line {r.start + 1}: a key above the first [[rule]]"})
    if not starts:
        return lines, []
    content_end: list[int] = []
    for i, s in enumerate(starts):
        nxt = starts[i + 1] if i + 1 < len(starts) else len(lines)
        ends = [r.end for r in recs if s <= r.start < nxt and r.kind not in ("blank", "comment")]
        content_end.append(max(ends))
    # the comment run directly above each header is its lead
    leads: list[int] = []  # first line of each block's lead
    for i, s in enumerate(starts):
        floor = content_end[i - 1] + 1 if i else 0
        k = s
        while k > floor and _is_comment(lines[k - 1]):
            k -= 1
        leads.append(k)
    header = lines[: leads[0]]
    blocks = []
    for i, s in enumerate(starts):
        tail_end = leads[i + 1] if i + 1 < len(starts) else len(lines)
        blocks.append(
            _Block(
                lead=lines[leads[i] : s],
                body=lines[s : content_end[i] + 1],
                tail=lines[content_end[i] + 1 : tail_end],
            )
        )
    return header, blocks


def _join(header: list[str], blocks: list[_Block]) -> str:
    return "".join(header) + "".join(b.text() for b in blocks)


def _trailing_blanks(lines: list[str]) -> int:
    n = 0
    for line in reversed(lines):
        if not _is_blank(line):
            break
        n += 1
    return n


def _ensure_nl(lines: list[str], nl: str) -> None:
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += nl


# ---- value serialization ---------------------------------------------------------------------

_ESC = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}


def _quote(s: str) -> str:
    if (
        "\\" in s
        and "'" not in s
        and all(c == "\t" or (ord(c) >= 0x20 and ord(c) != 0x7F) for c in s)
    ):
        return "'" + s + "'"  # a literal string: regex backslashes stay readable
    out = []
    for ch in s:
        if ch in _ESC:
            out.append(_ESC[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        elif 0xD800 <= ord(ch) <= 0xDFFF:
            raise ValueError("lone surrogate in string")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _key(k: Any) -> str:
    if not isinstance(k, str):
        raise ValueError(f"table key {k!r} is not text")
    return k if _BARE.fullmatch(k) else _quote(k)


def _inline(v: Any) -> str:
    """Any JSON-like value as one TOML value on one line."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            raise ValueError(f"cannot store {v!r} in TOML")
        return repr(v)
    if isinstance(v, str):
        return _quote(v)
    if isinstance(v, list | tuple):
        return "[" + ", ".join(_inline(x) for x in v) + "]"
    if isinstance(v, dict):
        if not v:
            return "{}"
        return "{ " + ", ".join(f"{_key(k)} = {_inline(x)}" for k, x in v.items()) + " }"
    raise ValueError(f"cannot store {type(v).__name__} in TOML")


def _ordered(d: dict[str, Any], order: tuple[str, ...]) -> list[str]:
    known = [k for k in order if k in d]
    return known + sorted(k for k in d if k not in order)


def _is_table_list(v: Any) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v)


def _kv_lines(d: dict[str, Any], order: tuple[str, ...], nl: str) -> list[str]:
    return [f"{_key(k)} = {_inline(d[k])}{nl}" for k in _ordered(d, order)]


def _full_body(data: dict[str, Any], nl: str) -> list[str]:
    """A clean `[[rule]]` block body: keys in canonical order, then [rule.hint], then buttons."""
    subs = {k for k in SUBTABLE_KEYS if k in data}
    if "hint" in subs and not isinstance(data["hint"], dict):
        subs.discard("hint")
    if "buttons" in subs and not _is_table_list(data["buttons"]):
        subs.discard("buttons")
    top = {k: v for k, v in data.items() if k not in subs}
    out = [f"[[rule]]{nl}"] + _kv_lines(top, KEY_ORDER, nl)
    if "hint" in subs:
        out.append(f"[rule.hint]{nl}")
        out += [f"{_key(k)} = {_inline(v)}{nl}" for k, v in data["hint"].items()]
    if "buttons" in subs:
        for b in data["buttons"]:
            out.append(f"[[rule.buttons]]{nl}")
            out += _kv_lines(b, BUTTON_ORDER, nl)
    return out


# ---- data helpers ----------------------------------------------------------------------------


def _same(a: Any, b: Any) -> bool:
    """Equality that tells True from 1 and 1 from 1.0, and ignores table key order."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    return a == b


def _empty(v: Any) -> bool:
    return v is None or v == "" or (isinstance(v, list | dict) and not v)


def clean_data(data: Any, old: dict[str, Any] | None = None) -> dict[str, Any]:
    """The rule table to write: see the module docstring ("Data cleanup")."""
    if not isinstance(data, dict):
        raise RulesEditError({"": "rule data must be an object"})
    old = old or {}
    out: dict[str, Any] = {}
    for k, v in data.items():
        if not isinstance(k, str):
            raise RulesEditError({"": f"key {k!r} is not text"})
        if k != "name" and _empty(v):
            continue
        if k not in old and (
            (k in FLAG_KEYS and v is False) or (k in DEFAULTS and _same(v, DEFAULTS[k]))
        ):
            continue
        if k == "buttons" and isinstance(v, list):
            v = [_clean_button(b) for b in v]
        out[k] = v
    return out


def _clean_button(b: Any) -> Any:
    if not isinstance(b, dict):
        return b
    return {k: v for k, v in b.items() if not (k != "label" and _empty(v))}


# ---- validation ------------------------------------------------------------------------------


def _map_error(e: RulesError, target: int | None) -> dict[str, str]:
    """RuleSet's message -> {field path: message}. A problem in some other rule, or a TOML
    error, is general ("")."""
    msg = str(e)
    idx = e.rule_index
    if idx is None:
        return {"": msg}
    rest = _PREFIX.sub("", msg, count=1)
    if "duplicate name" in rest:
        return {"name" if target is not None else "": rest if target is not None else msg}
    if target is None or idx != target:
        return {"": msg}
    m = _UNKNOWN_IN.search(rest)
    if m:
        base = m.group(1)
        return {base if base == "highlight" else f"{base}.{m.group(2)}": rest}
    m = _UNKNOWN.search(rest)
    if m:
        return {m.group(1): rest}
    if "missing required `name`" in rest:
        return {"name": rest}
    m = _TICKED.search(rest)
    return {m.group(1) if m else "": rest}


def validate(text: str, target: int | None = None) -> RuleSet:
    try:
        return RuleSet.from_toml(text)
    except RulesError as e:
        raise RulesEditError(_map_error(e, target)) from e


# ---- reading ---------------------------------------------------------------------------------


def _parse_rules(text: str) -> tuple[list[str], list[_Block], list[dict[str, Any]]]:
    header, blocks = _split(text)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise RulesEditError({"": f"rules file is not valid TOML: {e}"}) from e
    rules = data.get("rule", [])
    if not isinstance(rules, list) or len(rules) != len(blocks):
        raise RulesEditError(
            {"": "rules.toml uses a layout the editor cannot list (use Edit as text)"}
        )
    return header, blocks, rules


def _quoted(s: str) -> str:
    return f"“{s}”" if len(s) <= 40 else f"“{s[:39]}…”"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def summarize(d: dict[str, Any]) -> str:
    """One short human line: what the rule matches, then what it does."""
    parts: list[str] = []
    if isinstance(d.get("app"), str):
        parts.append(f"app = {d['app']}")
    if isinstance(d.get("app_regex"), str):
        parts.append(f"app ~ {_quoted(d['app_regex'])}")
    for k in ("summary", "body"):
        if isinstance(d.get(k), str):
            parts.append(f"{k} ~ {_quoted(d[k])}")
    u = d.get("urgency")
    if isinstance(u, str | list):
        parts.append("urgency = " + (u if isinstance(u, str) else "/".join(map(str, u))))
    c = d.get("category")
    if isinstance(c, str | list):
        parts.append("category = " + (c if isinstance(c, str) else "/".join(map(str, c))))
    if isinstance(d.get("hint"), dict) and d["hint"]:
        parts.append("hint " + ", ".join(map(str, d["hint"])))
    if isinstance(d.get("hint_present"), list) and d["hint_present"]:
        parts.append("has hint " + ", ".join(map(str, d["hint_present"])))
    sep = " or " if d.get("match_mode") == "any" else " and "
    match = sep.join(parts) if parts else "every notification"

    acts: list[str] = []
    if d.get("block") is True:
        acts.append("block")
    if d.get("store_only") is True:
        acts.append("store only")
    if isinstance(d.get("sound"), str):
        acts.append("sound" if d["sound"] != "none" else "silent")
    elif "volume" in d:
        acts.append(f"volume {d['volume']}")
    if "timeout" in d:
        acts.append("stays" if d["timeout"] == "never" else f"timeout {d['timeout']} ms")
    if "set_urgency" in d:
        acts.append(f"urgency {d['set_urgency']}")
    if "icon" in d:
        acts.append("icon")
    cls = d.get("class")
    if cls:
        acts.append("class " + (cls if isinstance(cls, str) else "/".join(map(str, cls))))
    if d.get("highlight"):
        acts.append("highlight")
    if isinstance(d.get("tags"), list) and d["tags"]:
        acts.append("tag " + "/".join(map(str, d["tags"])))
    if d.get("dnd_bypass") is True:
        acts.append("bypass DND")
    if "run" in d:
        acts.append("runs a command")
    if isinstance(d.get("buttons"), list) and d["buttons"]:
        acts.append(_plural(len(d["buttons"]), "button"))
    if d.get("stop") is True:
        acts.append("stop")
    line = f"{match} → {', '.join(acts) if acts else 'no action'}"
    return line if len(line) <= MAX_SUMMARY else line[: MAX_SUMMARY - 1] + "…"


def list_rules(text: str) -> list[dict[str, Any]]:
    """[{index, name, enabled, data, summary, source}] in file order."""
    _, blocks, rules = _parse_rules(text)
    out = []
    for i, (b, d) in enumerate(zip(blocks, rules, strict=True)):
        name = d.get("name")
        src = b.lead + b.body
        out.append(
            {
                "index": i,
                "name": name if isinstance(name, str) else "",
                "enabled": d.get("enabled", True) is not False,
                "data": d,
                "summary": summarize(d),
                "source": "".join(src),
            }
        )
    return out


# ---- block rewriting -------------------------------------------------------------------------


def _check_index(index: Any, n: int, what: str = "index") -> int:
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < n:
        raise RulesEditError({"": f"no rule at {what} {index!r}"})
    return index


def _patch_body(body: list[str], old: dict[str, Any], data: dict[str, Any], nl: str) -> list[str]:
    """Edit `body` in place of a rewrite: keep everything unchanged (see the module docstring).
    Raises _Unpatchable when a line cannot be placed."""
    body = list(body)
    _ensure_nl(body, nl)
    recs = _parse(body)
    if not recs or recs[0].kind != "aot":
        raise _Unpatchable
    # sub-table sections: header record index -> (lead start, header line, end exclusive)
    heads = [r for r in recs if r.kind in ("aot", "header") and r.start > 0]
    sections: list[dict[str, Any]] = []
    for j, h in enumerate(heads):
        parts = h.name.split(".")
        if len(parts) != 2 or parts[0] != "rule":
            raise _Unpatchable
        lead = h.start
        floor = heads[j - 1].start + 1 if j else 1
        while lead > floor and _is_comment(body[lead - 1]):
            lead -= 1
        sections.append({"key": parts[1], "aot": h.kind == "aot", "lead": lead, "head": h.start})
    for j, s in enumerate(sections):
        s["end"] = sections[j + 1]["lead"] if j + 1 < len(sections) else len(body)
    top_end = sections[0]["lead"] if sections else len(body)
    top: dict[str, _Rec] = {}
    for r in recs:
        if r.start == 0 or r.start >= top_end:
            continue
        if r.kind == "other":
            raise _Unpatchable
        if r.kind == "key":
            top[r.name] = r
    edits: list[tuple[int, int, int, list[str]]] = []  # (start, end, seq, replacement)
    seq = 0

    def edit(start: int, end: int, repl: list[str]) -> None:
        nonlocal seq
        seq += 1
        edits.append((start, end, seq, repl))

    by_key: dict[str, list[dict[str, Any]]] = {}
    for s in sections:
        by_key.setdefault(s["key"], []).append(s)
    appended: list[str] = []
    for k in _ordered({**old, **data}, KEY_ORDER):
        in_old, in_new = k in old, k in data
        if in_old and in_new and _same(old[k], data[k]):
            continue
        if in_old and k in top:  # an inline key line
            r = top[k]
            if not in_new:
                edit(r.start, r.end + 1, [])
                continue
            first = body[r.start].rstrip("\r\n")
            text = first[: r.vstart] + _inline(data[k]) + r.tail
            edit(r.start, r.end + 1, [text + _eol(body[r.end], nl)])
        elif in_old:  # a sub-table form
            secs = by_key.get(k, [])
            if not secs:
                raise _Unpatchable
            if not in_new:
                for s in secs:
                    edit(s["lead"], s["end"], [])
                continue
            new = data[k]
            if secs[0]["aot"]:
                if not _is_table_list(new) or not isinstance(old[k], list):
                    raise _Unpatchable
                for n, item in enumerate(new):
                    if n < len(secs):
                        if not _same(item, old[k][n]):
                            _rewrite_section(body, secs[n], item, BUTTON_ORDER, nl, edit)
                    else:
                        appended += [f"[[rule.{k}]]{nl}"] + _kv_lines(item, BUTTON_ORDER, nl)
                for s in secs[len(new) :]:
                    edit(s["lead"], s["end"], [])
            else:
                if not isinstance(new, dict) or len(secs) != 1:
                    raise _Unpatchable
                _rewrite_section(body, secs[0], new, (), nl, edit, keep_order=True)
        elif in_new:  # a new key
            v = data[k]
            if k == "hint" and isinstance(v, dict):
                appended += [f"[rule.hint]{nl}"] + [
                    f"{_key(x)} = {_inline(y)}{nl}" for x, y in v.items()
                ]
            elif k == "buttons" and _is_table_list(v):
                for item in v:
                    appended += [f"[[rule.buttons]]{nl}"] + _kv_lines(item, BUTTON_ORDER, nl)
            else:
                rank = RANK.get(k, len(KEY_ORDER))
                before = [r for n, r in top.items() if RANK.get(n, len(KEY_ORDER)) <= rank]
                at = max((r.end for r in before), default=0) + 1
                edit(at, at, [f"{_key(k)} = {_inline(v)}{nl}"])
    if appended:
        # after the last content line, so a trailing comment run stays above nothing
        last = max((r.end for r in recs if r.kind not in ("blank", "comment")), default=0) + 1
        edit(last, last, appended)
    # bottom up; at one line a replacement goes before an insertion, inserts keep their order
    for start, end, _seq, repl in sorted(
        edits, key=lambda e: (-e[0], 0 if e[1] > e[0] else 1, -e[2])
    ):
        body[start:end] = repl
    return body


def _eol(line: str, nl: str) -> str:
    return line[len(line.rstrip("\r\n")) :] or nl


def _rewrite_section(
    body: list[str],
    sec: dict[str, Any],
    new: dict[str, Any],
    order,
    nl: str,
    edit,
    keep_order=False,
) -> None:
    """Replace a sub-table's key lines, keeping its leading comment and header line."""
    keys = list(new) if keep_order else _ordered(new, order)
    repl = [f"{_key(k)} = {_inline(new[k])}{nl}" for k in keys]
    edit(sec["head"] + 1, sec["end"], repl)


def _rewrite_block(block: _Block, old: dict[str, Any], data: dict[str, Any], nl: str) -> _Block:
    """The block with `data` as its rule. Lead and tail are always kept."""
    if _same(old, data):
        return block
    body: list[str] | None = None
    try:
        patched = _patch_body(block.body, old, data, nl)
        while len(patched) > 1 and _is_blank(patched[-1]):
            patched.pop()  # a removed last sub-table leaves its blank separator behind
        got = tomllib.loads("".join(patched)).get("rule", [])
        if len(got) == 1 and _same(got[0], data):
            body = patched
    except (_Unpatchable, ValueError, tomllib.TOMLDecodeError, KeyError, IndexError):
        body = None
    if body is None:
        body = _full_body(data, nl)
    return _Block(block.lead, body, block.tail)


# ---- the operations --------------------------------------------------------------------------


def _serialize_error(e: Exception) -> RulesEditError:
    return RulesEditError({"": f"cannot write this rule: {e}"})


def update_rule(text: str, index: int, data: dict[str, Any]) -> str:
    header, blocks, rules = _parse_rules(text)
    _check_index(index, len(blocks))
    new = clean_data(data, rules[index])
    try:
        blocks[index] = _rewrite_block(blocks[index], rules[index], new, _nl(text))
    except (ValueError, TypeError) as e:
        raise _serialize_error(e) from e
    out = _join(header, blocks)
    if out != text:
        validate(out, index)
    return out


def add_rule(text: str, data: dict[str, Any], position: int | None = None) -> str:
    header, blocks, _rules = _parse_rules(text)
    n = len(blocks)
    if position is None:
        position = n
    if isinstance(position, bool) or not isinstance(position, int) or not 0 <= position <= n:
        raise RulesEditError({"": f"cannot insert at position {position!r}"})
    nl = _nl(text)
    try:
        block = _Block(body=_full_body(clean_data(data), nl))
    except (ValueError, TypeError) as e:
        raise _serialize_error(e) from e
    orig_last = _trailing_blanks(blocks[-1].tail) if blocks else 0
    header = list(header)
    if not blocks:
        _ensure_nl(header, nl)
        if header and not _is_blank(header[-1]):
            header.append(nl)  # keep file comments from becoming the new rule's lead
    blocks.insert(position, block)
    _seams(blocks, [block] + blocks[max(position - 1, 0) : position], orig_last, nl)
    out = _join(header, blocks)
    validate(out, position)
    return out


def delete_rule(text: str, index: int) -> str:
    header, blocks, _rules = _parse_rules(text)
    _check_index(index, len(blocks))
    orig_last = _trailing_blanks(blocks[-1].tail)
    del blocks[index]
    _seams(blocks, [blocks[-1]] if blocks and index == len(blocks) else [], orig_last, _nl(text))
    out = _join(header, blocks)
    validate(out)
    return out


def move_rule(text: str, index: int, new_index: int) -> str:
    """Move a block (its comments with it) so it ends up at `new_index` in the new order."""
    header, blocks, _rules = _parse_rules(text)
    _check_index(index, len(blocks))
    _check_index(new_index, len(blocks), "position")
    if index == new_index:
        return text
    orig_last = _trailing_blanks(blocks[-1].tail)
    block = blocks.pop(index)
    blocks.insert(new_index, block)
    touched = [block, blocks[-1]] + blocks[max(new_index - 1, 0) : new_index]
    _seams(blocks, touched, orig_last, _nl(text))
    out = _join(header, blocks)
    validate(out)
    return out


def set_enabled(text: str, index: int, on: bool) -> str:
    if not isinstance(on, bool):
        raise RulesEditError({"": "enabled must be true or false"})
    _, _, rules = _parse_rules(text)
    _check_index(index, len(rules))
    data = dict(rules[index])
    if on:
        if "enabled" not in data:
            return text
        data["enabled"] = True
    else:
        data["enabled"] = False
    return update_rule(text, index, data)


def _seams(blocks: list[_Block], touched: list[_Block], orig_last: int, nl: str) -> None:
    """Fix blank lines where blocks changed neighbours: every touched block that is followed
    by another one ends in a blank line; the last block keeps the file's original count."""
    last = len(blocks) - 1
    for i, b in enumerate(blocks):
        if not any(b is t for t in touched):
            continue
        if i < last:
            _ensure_nl(b.tail or b.body, nl)
            if _trailing_blanks(b.lines()) == 0:
                b.tail = b.tail + [nl]
        else:
            while _trailing_blanks(b.tail) > orig_last:
                b.tail = b.tail[:-1]
    if last >= 0:
        for b in blocks[:-1]:
            _ensure_nl(b.tail or b.body, nl)


def write_file(path: str | Path, text: str) -> None:
    """Validate `text` as a whole rules file, then write it atomically (a missing file is
    created). Raises RulesEditError and writes nothing when it is invalid."""
    validate(text)
    atomic_write(Path(path), text)


__all__ = [
    "RulesEditError",
    "add_rule",
    "clean_data",
    "delete_rule",
    "list_rules",
    "move_rule",
    "set_enabled",
    "summarize",
    "update_rule",
    "validate",
    "write_file",
]
