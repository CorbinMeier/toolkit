"""Sound playback for notifications. Wave-1 module; the daemon wiring comes later.

Decisions where docs/spec.md was silent:
- `choose` reads `sound_volume` with getattr (the rules engine adds it). A rule-set `n.sound`
  wins; an empty string counts as unset. `suppress-sound` is true for bool True or 1.
- A `sound-file` hint that is not a string or is empty is ignored (falls through to `sound-name`).
- Volume 0 (after master scaling) plays nothing, but is still returned as Resolved.
- Master scaling: final = round(base * master / 100), base defaulting to 100.
- Theme volume to canberra: dB = 20 * log10(volume / 100), so 100 -> 0 dB.
- Relative file paths are made absolute against the current directory.
- `last_error` is kept until a newer error replaces it; a later success does not clear it.
- A spawn that raises OSError moves on to the next player; all failing is logged, not raised.
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import unquote, urlparse

from notifyd.model import Notification

log = logging.getLogger("notifyd.sound")

DEFAULT_THEME_SOUND = "message-new-instant"
AUDIO_EXTENSIONS = (".mp3", ".wav", ".ogg", ".flac")
MAX_CONCURRENT = 3


@dataclass(frozen=True)
class Resolved:
    kind: Literal["none", "file", "theme"]
    value: str  # absolute path for file; event id for theme; "" for none
    volume: int  # 0-100 after master volume


def choose(n: Notification) -> tuple[str | None, int | None]:
    """Pick the sound spec by precedence; the volume is the rule-set `sound_volume`."""
    volume = getattr(n, "sound_volume", None)
    if n.sound:
        return n.sound, volume
    hints = n.hints or {}
    if hints.get("suppress-sound") in (True, 1):
        return "none", volume
    sound_file = hints.get("sound-file")
    if isinstance(sound_file, str) and sound_file:
        return sound_file, volume
    sound_name = hints.get("sound-name")
    if isinstance(sound_name, str) and sound_name:
        return sound_name, volume
    return None, volume


def _clamp(v: Any) -> int:
    try:
        return max(0, min(100, int(round(float(v)))))
    except (TypeError, ValueError, OverflowError):
        return 100


def _is_path_like(spec: str) -> bool:
    return (
        "/" in spec
        or spec.startswith("~")
        or spec.startswith("file://")
        or spec.lower().endswith(AUDIO_EXTENSIONS)
    )


def _to_path(spec: str) -> str:
    if spec.startswith("file://"):
        spec = unquote(urlparse(spec).path)
    return os.path.abspath(os.path.expanduser(spec))


class SoundPlayer:
    def __init__(
        self,
        default: str | None = None,
        master_volume: int = 100,
        *,
        spawn=subprocess.Popen,
        which=shutil.which,
    ) -> None:
        self._default = default
        self._master = _clamp(master_volume)
        self._spawn = spawn
        self._which = which
        self._lock = threading.Lock()
        self._procs: deque = deque()
        self._warned_no_player = False
        self.last_error: str | None = None

    def set_default(self, spec: str | None) -> None:
        self._default = spec

    def set_master_volume(self, v: int) -> None:
        self._master = _clamp(v)

    def _error(self, msg: str) -> None:
        self.last_error = msg
        log.error("%s", msg)

    def _check_file(self, spec: str) -> str | None:
        """Return the absolute path if it is a readable regular file, else None (error set)."""
        path = _to_path(spec)
        if os.path.isfile(path) and os.access(path, os.R_OK):
            return path
        self._error(f"sound file missing or unreadable: {path}")
        return None

    def resolve(self, spec: str | None, volume: int | None = None) -> Resolved:
        vol = round(_clamp(100 if volume is None else volume) * self._master / 100)
        if isinstance(spec, str) and spec.strip().lower() == "none":
            return Resolved("none", "", vol)
        if isinstance(spec, str) and spec.strip():
            spec = spec.strip()
            if _is_path_like(spec):
                path = self._check_file(spec)
                if path:
                    return Resolved("file", path, vol)
            else:
                return Resolved("theme", spec, vol)
        return self._resolve_default(vol)

    def _resolve_default(self, vol: int) -> Resolved:
        d = self._default
        if isinstance(d, str) and d.strip():
            d = d.strip()
            if d.lower() == "none":
                return Resolved("none", "", vol)
            if _is_path_like(d):
                path = self._check_file(d)
                if path:
                    return Resolved("file", path, vol)
            else:
                return Resolved("theme", d, vol)
        return Resolved("theme", DEFAULT_THEME_SOUND, vol)

    def _commands(self, r: Resolved) -> list[list[str]]:
        cmds: list[list[str]] = []
        if r.kind == "file":
            if self._which("pw-play"):
                cmds.append(["pw-play", f"--volume={r.volume / 100:.2f}", r.value])
            if self._which("paplay"):
                cmds.append(["paplay", f"--volume={round(r.volume / 100 * 65536)}", r.value])
        elif r.kind == "theme" and self._which("canberra-gtk-play"):
            db = 20 * math.log10(r.volume / 100)
            cmds.append(["canberra-gtk-play", "-i", r.value, "-V", f"{db:.1f}"])
        return cmds

    def play(self, spec: str | None, volume: int | None = None) -> Resolved:
        try:
            r = self.resolve(spec, volume)
        except Exception:  # never raise
            log.exception("sound resolve failed")
            return Resolved("none", "", 0)
        try:
            if r.kind == "none" or r.volume <= 0:
                return r
            cmds = self._commands(r)
            if not cmds:
                if not self._warned_no_player:
                    self._warned_no_player = True
                    log.error("no sound player found for %s sound %r", r.kind, r.value)
                return r
            for cmd in cmds:
                if self._launch(cmd):
                    break
        except Exception:
            log.exception("sound play failed")
        return r

    def _launch(self, cmd: list[str]) -> bool:
        with self._lock:
            self._procs = deque(p for p in self._procs if p.poll() is None)
            while len(self._procs) >= MAX_CONCURRENT:
                old = self._procs.popleft()
                try:
                    old.terminate()
                except Exception:
                    log.exception("terminating old sound failed")
            try:
                proc = self._spawn(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as e:
                log.error("could not start %s: %s", cmd[0], e)
                return False
            self._procs.append(proc)
        threading.Thread(target=self._reap, args=(proc,), daemon=True).start()
        return True

    @staticmethod
    def _reap(proc) -> None:
        try:
            proc.wait()
        except Exception:
            pass


class NullPlayer:
    """SoundPlayer stand-in for --no-sound / --no-display: same API, never spawns anything."""

    last_error: str | None = None

    def set_default(self, spec: str | None) -> None:
        pass

    def set_master_volume(self, v: int) -> None:
        pass

    def resolve(self, spec: str | None, volume: int | None = None) -> Resolved:
        return Resolved("none", "", 0)

    def play(self, spec: str | None, volume: int | None = None) -> Resolved:
        return Resolved("none", "", 0)
