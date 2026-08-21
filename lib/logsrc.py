"""Filtered, status-highlighted log tailing for the Log Highlighter panel (#28).

Source (journalctl unit vs. arbitrary file) and the highlight patterns are
config-driven -- editable from the Control Backend (#30) rather than
hardcoded, per the issue. Only lines matching a configured pattern are
returned: this is a highlight feed, not a full tail.
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


def _journalctl_lines(unit: str, max_lines: int) -> list[str]:
    out = subprocess.run(
        ["journalctl", "-u", unit, "-n", str(max_lines), "--no-pager",
         "-o", "short-iso", "--no-hostname"],
        capture_output=True, text=True, timeout=10, check=True)
    return out.stdout.splitlines()


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
            unit = logs_cfg.get("journalctl_unit")
            if not unit:
                return []
            raw_lines = _journalctl_lines(unit, max_lines)
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
