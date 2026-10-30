"""Open-latency trace: one INFO line per open of the center or the shade, phases in ms.

    center open: built=no present=3ms tail=1ms painted=212ms
    shade open: built=yes present=41ms tail=2ms rows=95ms painted=240ms resized=310ms

The clock starts at the show request. `built` says whether the view had to be created for this
open (a prewarmed view counts as built=no). `present` is when map/present() returned, `tail`
when the open handlers that run right after it (toast clear, seen watermark) returned, `rows`
when the page rendered fresh rows, `painted` when it reported a frame after becoming visible
(two animation frames), `resized` the last size change after the rows (shade only). A phase
that never arrived prints `-`. Only numbers and the built flag are ever logged: no
notification text.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

log = logging.getLogger("notifyd")

# What the page may report through the `perf` message; anything else is dropped.
PAGE_PHASES = ("painted", "rows")
SETTLE_MS = 400  # the shade waits this long after painted+rows for a late resize


class OpenTrace:
    def __init__(
        self,
        name: str,
        required: tuple[str, ...],
        *,
        settle_ms: int = 0,
        clock: Callable[[], float] = time.monotonic,
        emit: Callable[[str], None] | None = None,
        schedule: Callable[[int, Callable[[], None]], None] | None = None,
    ) -> None:
        self.name = name
        self.required = required  # phases the line waits for (the rest print only if seen)
        self.settle_ms = settle_ms
        self._clock = clock
        self._emit = emit or (lambda line: log.info("%s", line))
        self._schedule = schedule
        self._active = False
        self._gen = 0
        self._t0 = 0.0
        self._built = False
        self._marks: dict[str, int] = {}
        self._scheduled = False

    def begin(self, built: bool) -> None:
        """A show request: flush a line still pending from the previous open, restart."""
        self.flush()
        self._gen += 1
        self._active = True
        self._scheduled = False
        self._t0 = self._clock()
        self._built = built
        self._marks = {}

    def mark(self, phase: str, *, last: bool = False, after: str | None = None) -> None:
        """Record `phase` once per open (`last`: keep overwriting, for a repeated event;
        `after`: only once that phase has been recorded)."""
        if not self._active or (phase in self._marks and not last):
            return
        if after is not None and after not in self._marks:
            return
        self._marks[phase] = max(0, round((self._clock() - self._t0) * 1000))
        if self._scheduled or any(p not in self._marks for p in self.required):
            return
        if self.settle_ms and self._schedule is not None:
            self._scheduled = True
            gen = self._gen
            self._schedule(self.settle_ms, lambda: self._flush_gen(gen))
        else:
            self.flush()

    def _flush_gen(self, gen: int) -> None:
        if gen == self._gen:
            self.flush()

    def flush(self) -> None:
        if not self._active:
            return
        self._active = False
        self._emit(self.line())

    def line(self) -> str:
        parts = [f"built={'yes' if self._built else 'no'}"]
        for phase in ("present", "tail", "rows", "painted", "resized"):
            if phase in self._marks:
                parts.append(f"{phase}={self._marks[phase]}ms")
            elif phase in self.required or phase == "present":
                parts.append(f"{phase}=-")
        return f"{self.name} open: " + " ".join(parts)


def glib_schedule(ms: int, fn: Callable[[], None]) -> None:
    """Run `fn` once on the GLib main loop after `ms` (imported lazily: tests need no display)."""
    from gi.repository import GLib

    def run() -> bool:
        fn()
        return False

    GLib.timeout_add(ms, run)
