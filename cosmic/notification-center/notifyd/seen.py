"""The "seen" watermark: when the user last opened the shade or center.

`UnseenCount` counts notifications received after it. Persisted atomically as
`seen.json` (`{"seen_at": <epoch seconds>}`) in the state dir. A missing or unreadable file
means "now" at first start, so the badge does not jump to the whole history.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger("notifyd.seen")

DEFAULT_STATE_DIR = "~/.local/state/net.corbinmeier.notifications"


class SeenMark:
    """`at` is the watermark. `state_dir=None` keeps it in memory only (tests, default)."""

    def __init__(self, state_dir: Path | None, now: float) -> None:
        self._path = None if state_dir is None else Path(state_dir) / "seen.json"
        self.at = now
        if self._path is None:
            return
        loaded = None
        try:
            value = json.loads(self._path.read_text())["seen_at"]
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                loaded = float(value)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError):
            log.warning("seen state %s unreadable; starting from now", self._path)
        if loaded is None:
            self._save()
        else:
            self.at = loaded

    def set(self, at: float) -> None:
        self.at = at
        self._save()

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".seen-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump({"seen_at": self.at}, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self._path)
            except BaseException:
                os.unlink(tmp)
                raise
        except OSError:
            log.exception("could not save %s", self._path)
