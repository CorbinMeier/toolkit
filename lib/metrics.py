"""SQLite-backed historical metrics store.

Samples are written on a slower, independent interval from the 5s state
poll (see dashd-serve's `maybe_sample_metrics`), so a compact trend window
doesn't require keeping days of 5s-resolution rows. Retention is bounded --
old rows are pruned on every insert, since the volume here (one row per
sample interval, not per state poll) makes that cheap.

Every function takes an explicit db_path rather than reading a module
global, so tests can point at a tempdir the same way test_dashd.py swaps
dashd.DATA for the weather cache.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

# Columns are nullable: a given sample may be missing e.g. cpu_temp_c on a
# machine with no readable thermal sensor (see dashd-serve's system_stats).
COLUMNS = ("cpu_pct", "cpu_temp_c", "cpu_freq_mhz", "mem_pct", "net_down", "net_up")


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=5)
    # WAL lets the sampler's writes and a request handler's reads proceed
    # concurrently without blocking each other.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS samples (ts REAL NOT NULL, "
        + ", ".join(f"{c} REAL" for c in COLUMNS) + ")")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts)")
    return conn


def insert_sample(db_path: Path, sample: dict, *, retain_seconds: float) -> None:
    """Insert one row stamped with the current time, then prune anything
    older than `retain_seconds`. `sample` may omit any of COLUMNS; missing
    keys are stored NULL.
    """
    now = time.time()
    conn = _connect(db_path)
    try:
        cols = ["ts", *COLUMNS]
        vals = [now, *(sample.get(c) for c in COLUMNS)]
        conn.execute(
            f"INSERT INTO samples ({', '.join(cols)}) VALUES "
            f"({', '.join('?' * len(cols))})", vals)
        conn.execute("DELETE FROM samples WHERE ts < ?", (now - retain_seconds,))
        conn.commit()
    finally:
        conn.close()


def query_history(db_path: Path, *, since_seconds: float) -> list[dict]:
    """Return samples newer than `since_seconds` ago, oldest first.

    Returns [] for a DB that doesn't exist yet rather than creating one --
    a read shouldn't have the side effect of initializing storage.
    """
    if not Path(db_path).exists():
        return []
    conn = _connect(db_path)
    try:
        cutoff = time.time() - since_seconds
        cols = ["ts", *COLUMNS]
        cur = conn.execute(
            f"SELECT {', '.join(cols)} FROM samples WHERE ts >= ? "
            f"ORDER BY ts ASC", (cutoff,))
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
    finally:
        conn.close()
