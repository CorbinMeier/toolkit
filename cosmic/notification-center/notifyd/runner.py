"""Script runner for rule `run` and button `run` (spec: "Workflow: scripts, buttons, details page").

Decisions where docs/spec.md was silent:
- Notification data reaches the child only through `NC_*` environment variables; the command is
  never templated. A str command is `/bin/sh -c <str>`, a list is an argv (no shell).
- One worker thread per child reads the merged stdout/stderr, enforces the timeout and the
  TERM -> KILL escalation, and then hands the result to the GLib main thread with
  `GLib.idle_add`. Every store write happens on the main thread (the store is single-threaded):
  `add_run` inside `run()`, `finish_run` in the idle callback (or in `shutdown()`).
- `run()` must be called from the main thread. Cooldown starts when a run is spawned and is
  keyed by `spec["rule"]`; only `trigger="rule"` is subject to it. Busy/cooldown skips are
  recorded with `skipped_reason` and returned as their run id.
- The rule name comes from `spec["rule"]` (rule runs carry it); button specs without it get "".
- A run killed by the timeout gets `[timed out after Ns]` appended to its output tail.
- A grandchild that keeps the pipe open after the child exits is not waited for: the worker
  drains what is readable and finishes.
- `on_finished(run_id, notification_id)` (optional kwarg/attribute) is called on the main thread
  once a run's row is final: after `finish_run`, and for skipped and failed-to-start runs as
  soon as `run()` has recorded them. Errors in it are logged.
- A spawn that fails (missing program, no permission) is a finished run with exit code 127 and
  the error text as output; `run()` never raises.
"""

from __future__ import annotations

import json
import logging
import os
import select
import signal as _signal
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any

from gi.repository import GLib

from notifyd.model import Notification
from notifyd.sanitize import body_runs
from notifyd.store import Store

log = logging.getLogger("notifyd.runner")

TAIL_BYTES = 4096
DEFAULT_TIMEOUT = 30
KILL_GRACE = 3.0  # seconds between TERM and KILL on a timeout
SHUTDOWN_GRACE = 1.5  # seconds between TERM and KILL in shutdown()
_POLL = 0.05


def _urgency_name(n: Notification) -> str:
    try:
        return {0: "low", 1: "normal", 2: "critical"}[int(n.urgency)]
    except (KeyError, TypeError, ValueError):
        return "normal"


def build_env(n: Notification, rule: str, label: str | None) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        NC_ID=str(n.id),
        NC_APP=n.app_name or "",
        NC_SUMMARY=n.summary or "",
        NC_BODY="".join(r.get("text", "") for r in body_runs(n.body or "")),
        NC_URGENCY=_urgency_name(n),
        NC_CATEGORY=n.category or "",
        NC_TAGS=",".join(n.tags or []),
        NC_HINTS_JSON=json.dumps(n.hints or {}, ensure_ascii=False, default=str),
        NC_RULE=rule,
    )
    if label is not None:
        env["NC_BUTTON"] = label
    return env


class _Job:
    def __init__(
        self, run_id: int, proc: Any, started: float, timeout: float, notification_id: int = 0
    ) -> None:
        self.run_id = run_id
        self.notification_id = notification_id
        self.proc = proc
        self.started = started  # monotonic
        self.timeout = timeout
        self.tail = b""
        self.timed_out = False
        self.stop = threading.Event()  # set by shutdown(): TERM now, KILL after the grace
        self.done = threading.Event()  # worker finished (result fields are final)
        self.finalized = False  # main thread only
        self.thread: threading.Thread | None = None
        self.duration_ms = 0


def _killpg(proc: Any, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass
    except OSError:
        log.exception("signalling process group %s failed", proc.pid)


class Runner:
    def __init__(
        self,
        store: Store,
        *,
        max_concurrent: int = 4,
        clock=time.time,
        spawn=subprocess.Popen,
        home: str | os.PathLike | None = None,
        on_finished: Callable[[int, int], None] | None = None,
    ) -> None:
        self.on_finished = on_finished
        self._store = store
        self._max = max_concurrent
        self._clock = clock
        self._spawn = spawn
        self._home = os.fspath(home) if home is not None else os.environ.get("HOME", "/")
        self._jobs: dict[int, _Job] = {}  # by run id, running only (main thread)
        self._last_start: dict[str, float] = {}  # rule -> clock() of the last spawn

    def _finished(self, run_id: int, notification_id: int) -> None:
        if self.on_finished is None:
            return
        try:
            self.on_finished(run_id, notification_id)
        except Exception:
            log.exception("on_finished callback failed run=%d", run_id)

    def running(self) -> int:
        return len(self._jobs)

    # --- starting a run ---

    def _argv(self, cmd: str | list[str]) -> list[str]:
        if isinstance(cmd, str):
            return ["/bin/sh", "-c", cmd]
        argv = [str(a) for a in cmd]
        if argv and (argv[0] == "~" or argv[0].startswith("~/")):
            argv[0] = os.path.join(self._home, argv[0][2:]) if len(argv[0]) > 1 else self._home
        elif argv and argv[0].startswith("~"):
            argv[0] = os.path.expanduser(argv[0])
        return argv

    def run(
        self, n: Notification, spec: dict, *, trigger: str, label: str | None = None
    ) -> int | None:
        """Start the command of `spec` for `n`. Returns the run id (also for skipped and failed
        runs), or None if there was nothing to run."""
        cmd = spec.get("cmd", spec.get("run"))
        if not cmd:
            return None
        rule = spec.get("rule") or ""
        now = self._clock()
        base = dict(
            rule=rule,
            trigger=trigger,
            cmd=cmd,
            started_at=now,
            button_label=label,
        )
        cooldown = spec.get("cooldown") or 0
        if trigger == "rule" and cooldown > 0:
            last = self._last_start.get(rule)
            if last is not None and now - last < cooldown:
                return self._skipped(n, base, "cooldown")
        if len(self._jobs) >= self._max:
            return self._skipped(n, base, "busy")

        argv = self._argv(cmd)
        timeout = spec.get("timeout") or DEFAULT_TIMEOUT
        try:
            env = build_env(n, rule, label if trigger == "button" else None)
            proc = self._spawn(
                argv,
                cwd=self._home,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception as e:  # missing program, permission, bad cwd: a failed run, not a raise
            run_id = self._store.add_run(
                n.id,
                **base,
                finished_at=now,
                exit_code=127,
                duration_ms=0,
                output_tail=f"{type(e).__name__}: {e}",
            )
            log.info("run %d rule=%r failed to start: %s (exit 127)", run_id, rule, e)
            self._finished(run_id, n.id)
            return run_id

        if rule:
            self._last_start[rule] = now
        run_id = self._store.add_run(n.id, **base)
        job = _Job(run_id, proc, time.monotonic(), float(timeout), n.id)
        self._jobs[run_id] = job
        log.info("run %d start rule=%r trigger=%s argv=%r", run_id, rule, trigger, argv)
        job.thread = threading.Thread(target=self._work, args=(job,), daemon=True)
        job.thread.start()
        return run_id

    def _skipped(self, n: Notification, base: dict, reason: str) -> int:
        run_id = self._store.add_run(
            n.id, **base, finished_at=base["started_at"], skipped_reason=reason
        )
        log.info("run %d rule=%r skipped (%s)", run_id, base["rule"], reason)
        self._finished(run_id, n.id)
        return run_id

    # --- worker thread: no store access here ---

    def _work(self, job: _Job) -> None:
        proc = job.proc
        fd = proc.stdout.fileno()
        deadline = job.started + job.timeout
        term_at: float | None = None
        killed = False
        eof = False
        try:
            while not eof:
                now = time.monotonic()
                if term_at is None and (now >= deadline or job.stop.is_set()):
                    job.timed_out = not job.stop.is_set()
                    term_at = now
                    _killpg(proc, _signal.SIGTERM)
                elif term_at is not None and not killed:
                    grace = SHUTDOWN_GRACE if job.stop.is_set() else KILL_GRACE
                    if now - term_at >= grace:
                        killed = True
                        _killpg(proc, _signal.SIGKILL)
                ready, _, _ = select.select([fd], [], [], _POLL)
                if ready:
                    data = os.read(fd, 65536)
                    if not data:
                        eof = True
                    else:
                        job.tail = (job.tail + data)[-TAIL_BYTES:]
                elif proc.poll() is not None:
                    break  # exited, nothing left to read (a grandchild may still hold the pipe)
            while True:  # EOF or exit: reap, escalating if the group is still alive
                try:
                    proc.wait(timeout=_POLL)
                    break
                except subprocess.TimeoutExpired:
                    if not killed and time.monotonic() - (term_at or deadline) >= KILL_GRACE:
                        killed = True
                        _killpg(proc, _signal.SIGKILL)
        except Exception:
            log.exception("run %d worker failed", job.run_id)
            _killpg(proc, _signal.SIGKILL)
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
            job.duration_ms = int((time.monotonic() - job.started) * 1000)
            job.done.set()
            GLib.idle_add(self._finalize_idle, job)

    # --- main thread ---

    def _finalize_idle(self, job: _Job) -> bool:
        self._finalize(job)
        return False

    def _finalize(self, job: _Job) -> None:
        if job.finalized:
            return
        job.finalized = True
        self._jobs.pop(job.run_id, None)
        rc = job.proc.returncode
        exit_code = rc if rc is not None and rc >= 0 else None
        sig = -rc if rc is not None and rc < 0 else None
        text = job.tail.decode("utf-8", errors="replace")
        if job.timed_out:
            text += f"\n[timed out after {job.timeout:g}s]"
        try:
            self._store.finish_run(
                job.run_id,
                finished_at=self._clock(),
                exit_code=exit_code,
                signal=sig,
                duration_ms=job.duration_ms,
                output_tail=text,
            )
        except Exception:
            log.exception("recording run %d failed", job.run_id)
        log.info(
            "run %d finish exit=%s signal=%s duration=%dms",
            job.run_id,
            exit_code,
            sig,
            job.duration_ms,
        )
        self._finished(job.run_id, job.notification_id)

    def shutdown(self) -> None:
        """TERM every running group, wait briefly, KILL stragglers, record the results."""
        jobs = list(self._jobs.values())
        for job in jobs:
            job.stop.set()
        for job in jobs:
            if job.thread is not None:
                job.thread.join(SHUTDOWN_GRACE + 3)
            if not job.done.is_set():
                _killpg(job.proc, _signal.SIGKILL)
                job.done.wait(2)
            if job.done.is_set():
                self._finalize(job)
        self._jobs.clear()
