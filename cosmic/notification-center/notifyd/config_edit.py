"""Comment-preserving edits of config.toml, for the subset this project uses.

Supported: `[table]` headers and `key = scalar` lines (strings, ints, floats, bools; "never" is
just a string). Everything else (comments, blank lines, arrays, inline tables, multi-line
strings, other tables) is left byte-for-byte alone. Anything the editor cannot place safely
raises ValueError instead of guessing:
  - the target table is an array of tables (`[[name]]`)
  - the target key currently holds an array, an inline table or a multi-line string
  - a table or key name that is not a plain bare name

`set_values` / `remove_keys` work on text; `update_file` is the atomic, validated file wrapper.
The panel's save path should call `update_file` only.
"""

from __future__ import annotations

import math
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

_BARE = re.compile(r"^[A-Za-z0-9_-]+$")
_TABLE_NAME = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$")
_HEADER = re.compile(r"^\s*(\[\[?)\s*([^\[\]]*?)\s*(\]\]?)\s*(#.*)?$")
_KEY_LINE = re.compile(r"""^(\s*)([A-Za-z0-9_-]+|"[^"]*"|'[^']*')(\s*=\s*)""")


@dataclass
class _Rec:
    kind: str  # blank | comment | header | aot | key | other
    start: int
    end: int  # inclusive; > start only for multi-line values
    table: str = ""  # the table this line belongs to ("" = root)
    name: str = ""  # header: its table name; key: its key
    indent: str = ""
    sep: str = ""
    vstart: int = 0
    vend: int = 0
    vkind: str = ""  # scalar | structure | multiline
    tail: str = ""  # text between the value and the end of line, comment included


def _fail(msg: str) -> ValueError:
    return ValueError(msg)


def _unquote_key(raw: str) -> str:
    return raw[1:-1] if raw[:1] in "\"'" else raw


def _scan_value(lines: list[str], i: int, col: int) -> tuple[int, int, int, str]:
    """Scan a value starting at lines[i][col:]. Returns (last line, value end col on that line,
    comment/tail start col, kind). Handles strings, triple-quoted strings and nested brackets
    across lines."""
    depth = 0
    kind = "scalar"
    ml: str | None = None  # inside a multi-line string, its delimiter
    j = i
    while True:
        line = lines[j].rstrip("\r\n")
        k = col if j == i else 0
        last_value_col = k
        while k < len(line):
            c = line[k]
            if ml:
                if line.startswith(ml, k) and not _escaped(line, k, ml):
                    k += 3
                    ml = None
                    last_value_col = k
                else:
                    k += 1
                continue
            if line.startswith('"""', k) or line.startswith("'''", k):
                ml = line[k : k + 3]
                kind = "multiline"
                k += 3
            elif c in "\"'":
                k = _skip_string(line, k)
                last_value_col = k
            elif c in "[{":
                depth += 1
                kind = "structure"
                k += 1
                last_value_col = k
            elif c in "]}":
                depth -= 1
                k += 1
                last_value_col = k
            elif c == "#":
                break
            else:
                if not c.isspace():
                    last_value_col = k + 1
                k += 1
        if depth <= 0 and ml is None:
            return j, last_value_col, last_value_col, kind
        j += 1
        if j >= len(lines):
            raise _fail("unterminated value in config text")


def _escaped(line: str, k: int, delim: str) -> bool:
    if delim[0] == "'":
        return False
    n = 0
    while k - 1 - n >= 0 and line[k - 1 - n] == "\\":
        n += 1
    return n % 2 == 1


def _skip_string(line: str, k: int) -> int:
    q = line[k]
    k += 1
    while k < len(line):
        if q == '"' and line[k] == "\\":
            k += 2
            continue
        if line[k] == q:
            return k + 1
        k += 1
    return k


def _parse(lines: list[str]) -> list[_Rec]:
    recs: list[_Rec] = []
    table = ""
    i = 0
    while i < len(lines):
        raw = lines[i].rstrip("\r\n")
        s = raw.strip()
        if not s:
            recs.append(_Rec("blank", i, i, table))
        elif s.startswith("#"):
            recs.append(_Rec("comment", i, i, table, indent=raw[: len(raw) - len(raw.lstrip())]))
        elif s.startswith("["):
            m = _HEADER.match(raw)
            if not m:
                raise _fail(f"line {i + 1}: cannot parse table header")
            name = re.sub(r"\s*\.\s*", ".", m.group(2))
            if m.group(1) == "[[":
                recs.append(_Rec("aot", i, i, name, name=name))
                table = name
            else:
                recs.append(_Rec("header", i, i, name, name=name))
                table = name
        else:
            m = _KEY_LINE.match(raw)
            if not m:
                recs.append(_Rec("other", i, i, table))
            else:
                last, vend, tailstart, vkind = _scan_value(lines, i, m.end())
                full_last = lines[last].rstrip("\r\n")
                recs.append(
                    _Rec(
                        "key",
                        i,
                        last,
                        table,
                        name=_unquote_key(m.group(2)),
                        indent=m.group(1),
                        sep=m.group(3),
                        vstart=m.end(),
                        vend=vend,
                        vkind=vkind,
                        tail=full_last[tailstart:],
                    )
                )
                i = last
        i += 1
    return recs


def serialize_value(value: object) -> str:
    """A TOML scalar: bool, int, float, str. Raises TypeError for anything else."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"cannot store {value!r} in TOML")
        return repr(value)
    if isinstance(value, str):
        return _quote(value)
    raise TypeError(f"unsupported config value type {type(value).__name__}: {value!r}")


_ESC = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}


def _quote(s: str) -> str:
    out = []
    for ch in s:
        if ch in _ESC:
            out.append(_ESC[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        elif 0xD800 <= ord(ch) <= 0xDFFF:
            raise ValueError("lone surrogate in string")
        else:
            out.append(ch)  # unicode stays as is; the file is UTF-8
    return '"' + "".join(out) + '"'


def _check_names(table: str, keys: list[str]) -> None:
    if table and not _TABLE_NAME.match(table):
        raise _fail(f"unsupported table name {table!r}")
    for k in keys:
        if not _BARE.match(k):
            raise _fail(f"unsupported key name {k!r}")


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _section(recs: list[_Rec], table: str, lines: list[str]) -> tuple[int, int] | None:
    """(first body line, last body line + 1) of a [table], or None when absent. Root = ("")."""
    for r in recs:
        if r.kind == "aot" and r.name == table:
            raise _fail(f"[[{table}]] is an array of tables; not supported")
    if table == "":
        heads = [r.start for r in recs if r.kind in ("header", "aot")]
        return 0, (heads[0] if heads else len(lines))
    for idx, r in enumerate(recs):
        if r.kind == "header" and r.name == table:
            end = len(lines)
            for r2 in recs[idx + 1 :]:
                if r2.kind in ("header", "aot"):
                    end = r2.start
                    break
            return r.start + 1, end
    return None


def _insert_after(lines: list[str], idx: int, text: str, nl: str) -> None:
    """Insert one line after lines[idx] (idx = -1: at the top)."""
    if idx >= 0 and not lines[idx].endswith("\n"):
        lines[idx] += nl
    lines.insert(idx + 1, text + nl)


def _set_one(lines: list[str], table: str, key: str, value: object, nl: str) -> None:
    rendered = serialize_value(value)
    recs = _parse(lines)
    sec = _section(recs, table, lines)
    if sec is not None:
        lo, hi = sec
        for r in recs:
            if r.kind == "key" and r.table == table and r.name == key and lo <= r.start < hi:
                if r.vkind != "scalar":
                    raise _fail(
                        f"[{table}] {key} holds an array, inline table or multi-line string; "
                        "refusing to overwrite it"
                    )
                old = lines[r.start]
                eol = old[len(old.rstrip("\r\n")) :]
                head = old.rstrip("\r\n")[: r.vstart]
                lines[r.start] = head + rendered + r.tail + eol
                return
        # a commented default: `# key = ...` goes live right under the comment
        pat = re.compile(r"^(\s*)#\s*" + re.escape(key) + r"\s*=")
        for r in recs:
            if r.kind == "comment" and lo <= r.start < hi and pat.match(lines[r.start]):
                indent = pat.match(lines[r.start]).group(1)
                _insert_after(lines, r.start, f"{indent}{key} = {rendered}", nl)
                return
        # at the end of the table, before trailing blank lines
        last = lo - 1
        for n in range(lo, hi):
            if lines[n].strip():
                last = n
        _insert_after(lines, last, f"{key} = {rendered}", nl)
        return
    # missing table: append
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += nl
    if lines and lines[-1].strip():
        lines.append(nl)
    lines.append(f"[{table}]{nl}")
    lines.append(f"{key} = {rendered}{nl}")


def _check_result(before: str, after: str) -> None:
    try:
        tomllib.loads(after)
    except tomllib.TOMLDecodeError as e:
        try:
            tomllib.loads(before)
        except tomllib.TOMLDecodeError:
            return  # the input was already broken; the caller sees that on load
        raise _fail(f"edit would produce invalid TOML: {e}") from e


def set_values(text: str, updates: dict[str, dict[str, object]]) -> str:
    """Set `updates[table][key] = value`, keeping comments, indentation and every other line."""
    nl = _newline(text)
    lines = text.splitlines(keepends=True)
    for table, kv in updates.items():
        _check_names(table, list(kv))
        for key, value in kv.items():
            _set_one(lines, table, key, value, nl)
    out = "".join(lines)
    _check_result(text, out)
    return out


def remove_keys(text: str, removals: dict[str, list[str]]) -> str:
    """Delete the real `key = ...` lines (commented defaults stay). Missing keys are fine."""
    lines = text.splitlines(keepends=True)
    for table, keys in removals.items():
        _check_names(table, list(keys))
        for key in keys:
            recs = _parse(lines)
            sec = _section(recs, table, lines)
            if sec is None:
                continue
            lo, hi = sec
            for r in recs:
                if r.kind == "key" and r.table == table and r.name == key and lo <= r.start < hi:
                    del lines[r.start : r.end + 1]
                    break
    out = "".join(lines)
    _check_result(text, out)
    return out


def update_file(
    path: str | os.PathLike,
    updates: dict[str, dict[str, object]],
    removals: dict[str, list[str]] | None = None,
) -> None:
    """Apply removals then updates to the file, validate with tomllib, write atomically."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    new = text
    if removals:
        new = remove_keys(new, removals)
    new = set_values(new, updates)
    tomllib.loads(new)  # raises TOMLDecodeError: nothing is written
    if new == text and p.exists():
        return
    atomic_write(p, new)


def atomic_write(path: Path, text: str) -> None:
    """tmp file in the same directory + fsync + rename; keeps the target's mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, path.stat().st_mode & 0o777)
        except FileNotFoundError:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    try:
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass
