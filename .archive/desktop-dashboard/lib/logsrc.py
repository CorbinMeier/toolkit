"""Filtered, status-highlighted log tailing for the Log Highlighter panel (#28).

Source (journal vs. arbitrary file) and the highlight patterns are
config-driven -- editable from the Control Backend (#30) rather than
hardcoded, per the issue. Only lines matching a configured pattern are
returned: this is a highlight feed, not a full tail.

The journal source matches on indexed fields rather than a single unit,
because the auth failures the shipped patterns look for are emitted by
several different processes across several units (#72). See
_journalctl_argv() for why `--grep` is not used to achieve the same reach.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:+-]+")


def _tail_lines(path: Path, max_lines: int) -> list[str]:
    """Last `max_lines` lines of `path`, without reading the whole file into
    memory for a log that's grown large over time."""
    chunk = 8192
    with path.open("rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        data = b""
        pos = size
        while pos > 0 and data.count(b"\n") <= max_lines:
            step = min(chunk, pos)
            pos -= step
            fh.seek(pos)
            data = fh.read(step) + data
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[-max_lines:]


# A match must be FIELD=value. Validated rather than trusted so a typo in
# config.json can never reach journalctl's argv as a flag: a value like "-f"
# would otherwise start a follow that never returns, hanging the request
# thread. Same whitelist-not-sanitise posture as the music command route.
_MATCH_RE = re.compile(r"^[A-Z_][A-Z0-9_]*=")


def journal_matches(logs_cfg: dict) -> list[str]:
    """Validated journal field matches, OR'd together by the caller.

    Empty means "fall back to journalctl_unit", which is what keeps the
    Control Backend's phone-editable unit field meaningful (#30): setting a
    unit narrows the feed to it, clearing it returns to these matches.
    """
    raw = logs_cfg.get("journalctl_matches") or []
    if isinstance(raw, str):
        raw = [raw]
    return [m for m in raw if isinstance(m, str) and _MATCH_RE.match(m)]


def _journalctl_argv(logs_cfg: dict, max_lines: int) -> list[str] | None:
    """journalctl argv for this config, or None when no source is configured.

    Matching is on **indexed fields** (`_COMM=sudo`), never on `--grep`. That
    is a measured decision, not a stylistic one. The local auth failures this
    feed exists to surface are emitted by cosmic-greeter/greetd/sudo across
    several units -- including per-session `session-N.scope` names that cannot
    be hardcoded -- so no single `-u` catches them (#72). The obvious
    widening, a system-wide `--grep`, is unusable on the request thread: `-g`
    is a full text scan with no index, so when matches are sparse `-n 200`
    walks the whole journal hunting a 200th hit that does not exist. Measured
    here: 60s+ unbounded, still 4.3s even bounded to `--since -24h`, versus
    **126ms** for the OR'd `_COMM=` matches returning the same auth lines. A
    five-second poll can afford neither of the first two.

    `-r` is passed unconditionally so ordering is one documented behavior
    rather than a quirk: plain `-n` yields oldest-first, but `-n` combined
    with `--grep` yields newest-first, and silently depending on which would
    invert the panel the moment the source shape changes. The caller reverses
    once, so "most recent last" holds for every source.
    """
    argv = ["journalctl", "-r", "-n", str(max_lines), "--no-pager",
            "-o", "short-iso", "--no-hostname"]
    # An explicitly chosen unit wins: it is the narrowing the user just asked
    # for from their phone, so the broad default must not override it.
    unit = logs_cfg.get("journalctl_unit")
    if unit:
        return [*argv, "-u", unit]
    matches = journal_matches(logs_cfg)
    if not matches:
        return None
    for i, match in enumerate(matches):
        if i:
            argv.append("+")  # journalctl's OR between match groups
        argv.append(match)
    return argv


def _journalctl_lines(argv: list[str]) -> list[str]:
    out = subprocess.run(argv, capture_output=True, text=True,
                         timeout=10, check=True)
    # -r gave newest-first; the panel's contract is most-recent-last.
    return out.stdout.splitlines()[::-1]


def read_log_lines(cfg: dict) -> list[dict]:
    """Matched, highlighted lines for the panel: [{"ts", "text", "status",
    "label"}, ...], most recent last. Never raises -- a missing unit, an
    unreadable file, or insufficient journal permissions means an empty
    (not broken) panel; the caller decides whether to surface that.
    """
    logs_cfg = cfg.get("logs", {})
    max_lines = logs_cfg.get("max_lines", 200)
    patterns = [
        (re.compile(p["regex"]), p.get("status", "info"), p.get("label", ""))
        for p in logs_cfg.get("patterns", [])
    ]
    if not patterns:
        return []

    try:
        if logs_cfg.get("source_type") == "file":
            file_path = logs_cfg.get("file_path")
            if not file_path:
                return []
            raw_lines = _tail_lines(Path(file_path), max_lines)
        else:
            argv = _journalctl_argv(logs_cfg, max_lines)
            if argv is None:
                return []
            raw_lines = _journalctl_lines(argv)
    except (OSError, subprocess.SubprocessError):
        return []

    matched = []
    for line in raw_lines:
        for regex, status, label in patterns:
            if regex.search(line):
                ts_match = _TS_RE.match(line)
                matched.append({
                    "ts": ts_match.group(0) if ts_match else "",
                    "text": line.strip(),
                    "status": status,
                    "label": label,
                })
                break
    return matched
