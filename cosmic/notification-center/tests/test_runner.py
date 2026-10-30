import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from gi.repository import GLib

from notifyd import runner as runner_mod
from notifyd.model import Notification, Urgency
from notifyd.runner import Runner
from notifyd.store import Store


def mk(id=1, summary="sum", body="", **kw):
    n = Notification.from_notify(id, "App", 0, "", summary, body, [], {}, -1, 100.0)
    for k, v in kw.items():
        setattr(n, k, v)
    return n


def pump(cond, timeout=8.0):
    ctx = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            return False
        ctx.iteration(False)
        time.sleep(0.005)
    return True


def spec(cmd, **kw):
    return {"rule": "r1", "cmd": cmd, "when": "arrive", "cooldown": 0, "timeout": 30, **kw}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.store = Store(":memory:")
        self.n = mk()
        self.store.record(self.n)
        self.r = Runner(self.store, home=self.home)

    def tearDown(self):
        self.r.shutdown()
        self.store.close()
        self._tmp.cleanup()

    def finish(self, run_id):
        self.assertTrue(pump(lambda: self.r.running() == 0), "run did not finish")
        return next(x for x in self.store.runs_for(self.n.id) if x["id"] == run_id)

    def go(self, s, **kw):
        kw.setdefault("trigger", "rule")
        return self.finish(self.r.run(self.n, s, **kw))


class Forms(Base):
    def test_shell_string(self):
        row = self.go(spec("echo hi; echo err >&2; pwd"))
        self.assertEqual(row["output_tail"], f"hi\nerr\n{self.home}\n")
        self.assertEqual(row["exit_code"], 0)
        self.assertIsNone(row["signal"])
        self.assertEqual(row["trigger"], "rule")
        self.assertEqual(row["rule"], "r1")
        self.assertEqual(row["cmd"], "echo hi; echo err >&2; pwd")
        self.assertIsNotNone(row["finished_at"])
        self.assertGreaterEqual(row["duration_ms"], 0)

    def test_argv_no_shell(self):
        row = self.go(spec(["/bin/echo", "a;b", "$HOME", "*"]))
        self.assertEqual(row["output_tail"], "a;b $HOME *\n")
        self.assertEqual(row["cmd"], ["/bin/echo", "a;b", "$HOME", "*"])

    def test_tilde_only_in_argv0(self):
        script = self.home / "bin" / "tool"
        script.parent.mkdir()
        script.write_text('#!/bin/sh\necho "$1"\n')
        script.chmod(0o755)
        row = self.go(spec(["~/bin/tool", "~/x"]))
        self.assertEqual(row["output_tail"], "~/x\n")

    def test_exit_code(self):
        self.assertEqual(self.go(spec("exit 3"))["exit_code"], 3)

    def test_empty_cmd_is_none(self):
        self.assertIsNone(self.r.run(self.n, spec(""), trigger="rule"))

    def test_stdin_is_devnull(self):
        self.assertEqual(self.go(spec("cat; echo done"))["output_tail"], "done\n")


class Env(Base):
    def test_values(self):
        n = mk(
            7,
            summary="Sum é",
            body="<b>Hello</b> <i>wor</i>ld &amp; <a href='https://x.y'>link</a>",
            urgency=Urgency.CRITICAL,
            category="email.arrived",
            tags=["a", "b"],
            hints={"x-nc-url": "https://e/1", "n": 5},
        )
        n.app_name = "Mail"
        self.store.record(n)
        self.n = n
        row = self.go(
            spec(
                'printf "%s\\n" "$NC_ID" "$NC_APP" "$NC_SUMMARY" "$NC_BODY" "$NC_URGENCY" '
                '"$NC_CATEGORY" "$NC_TAGS" "$NC_HINTS_JSON" "$NC_RULE" "${NC_BUTTON-unset}"'
            )
        )
        lines = row["output_tail"].splitlines()
        self.assertEqual(lines[0:3], ["7", "Mail", "Sum é"])
        self.assertEqual(lines[3], "Hello world & link")
        self.assertEqual(lines[4:7], ["critical", "email.arrived", "a,b"])
        self.assertEqual(json.loads(lines[7]), {"x-nc-url": "https://e/1", "n": 5})
        self.assertEqual(lines[8:], ["r1", "unset"])

    def test_button_trigger(self):
        row = self.go(spec("echo $NC_BUTTON"), trigger="button", label="Do it")
        self.assertEqual(row["output_tail"], "Do it\n")
        self.assertEqual(row["trigger"], "button")
        self.assertEqual(row["button_label"], "Do it")

    def test_urgency_names(self):
        for u, name in ((Urgency.LOW, "low"), (Urgency.NORMAL, "normal")):
            self.n.urgency = u
            self.assertEqual(self.go(spec("echo $NC_URGENCY"))["output_tail"], name + "\n")


class Injection(Base):
    def _assert_inert(self, marker, **fields):
        n = mk(5, **fields)
        self.store.record(n)
        self.n = n
        for cmd in ("echo ok", ["/bin/echo", "ok"]):
            row = self.go(spec(cmd))
            self.assertEqual(row["output_tail"], "ok\n")
        self.assertFalse(marker.exists(), "notification data was executed")

    def test_body_and_summary_never_execute(self):
        marker = self.home / "pwned"
        self._assert_inert(
            marker,
            body=f"; touch {marker} #",
            summary=f"$(touch {marker}) `touch {marker}`",
        )

    def test_data_not_reinterpreted_through_env(self):
        marker = self.home / "pwned2"
        n = mk(6, body=f"$(touch {marker})", summary=f"; touch {marker}")
        self.store.record(n)
        self.n = n
        row = self.go(spec('echo "$NC_BODY|$NC_SUMMARY"'))
        self.assertIn("$(touch", row["output_tail"])
        self.assertFalse(marker.exists())


class Limits(Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(runner_mod, "KILL_GRACE", 0.6)
        p.start()
        self.addCleanup(p.stop)

    def test_timeout_term(self):
        t0 = time.monotonic()
        row = self.go(spec("sleep 30", timeout=1))
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(row["signal"], 15)
        self.assertIsNone(row["exit_code"])
        self.assertIn("timed out after 1s", row["output_tail"])

    def test_timeout_kill_for_term_ignorer(self):
        t0 = time.monotonic()
        row = self.go(spec("trap '' TERM; sleep 30", timeout=1))
        took = time.monotonic() - t0
        self.assertGreaterEqual(took, 1.5)  # timeout + KILL_GRACE
        self.assertLess(took, 6)
        self.assertEqual(row["signal"], 9)

    def test_output_tail_truncation(self):
        row = self.go(spec("yes 0123456789 | head -c 20000; echo; echo END"))
        tail = row["output_tail"]
        self.assertEqual(len(tail.encode()), 4096)
        self.assertTrue(tail.endswith("END\n"))

    def test_invalid_utf8_replaced(self):
        row = self.go(spec("printf 'a\\377b'"))
        self.assertEqual(row["output_tail"], "a�b")

    def test_cooldown_skip(self):
        now = [1000.0]
        r = Runner(self.store, home=self.home, clock=lambda: now[0])
        s = spec("echo x", cooldown=60)
        first = r.run(self.n, s, trigger="rule")
        now[0] += 10
        second = r.run(self.n, s, trigger="rule")
        other = r.run(self.n, spec("echo x", rule="r2", cooldown=60), trigger="rule")
        btn = r.run(self.n, s, trigger="button", label="b")
        self.assertTrue(pump(lambda: r.running() == 0))
        now[0] += 60
        third = r.run(self.n, s, trigger="rule")
        self.assertTrue(pump(lambda: r.running() == 0))
        rows = {x["id"]: x for x in self.store.runs_for(self.n.id)}
        self.assertEqual(rows[second]["skipped_reason"], "cooldown")
        self.assertIsNone(rows[second]["exit_code"])
        for i in (first, other, btn, third):
            self.assertIsNone(rows[i]["skipped_reason"])
            self.assertEqual(rows[i]["exit_code"], 0)
        r.shutdown()

    def test_busy_skip(self):
        r = Runner(self.store, home=self.home, max_concurrent=2)
        a = r.run(self.n, spec("sleep 5"), trigger="rule")
        b = r.run(self.n, spec("sleep 5"), trigger="rule")
        c = r.run(self.n, spec("echo x"), trigger="rule")
        self.assertEqual(r.running(), 2)
        rows = {x["id"]: x for x in self.store.runs_for(self.n.id)}
        self.assertEqual(rows[c]["skipped_reason"], "busy")
        self.assertIsNone(rows[a]["skipped_reason"])
        self.assertIsNone(rows[b]["skipped_reason"])
        r.shutdown()
        self.assertEqual(r.running(), 0)

    def test_missing_program(self):
        rid = self.r.run(self.n, spec(["/nonexistent/prog", "x"]), trigger="rule")
        self.assertEqual(self.r.running(), 0)
        row = self.store.runs_for(self.n.id)[0]
        self.assertEqual(row["id"], rid)
        self.assertEqual(row["exit_code"], 127)
        self.assertIn("/nonexistent/prog", row["output_tail"])
        self.assertIsNotNone(row["finished_at"])

    def test_shutdown_term_then_kill(self):
        # Each child prints "ready" only after it is set up (the trap is installed), so TERM can
        # never reach the TERM-ignoring child before it ignores TERM.
        a = self.r.run(self.n, spec("echo ready; sleep 30"), trigger="rule")
        b = self.r.run(self.n, spec("trap '' TERM; echo ready; sleep 30"), trigger="rule")
        jobs = list(self.r._jobs.values())
        self.assertTrue(
            pump(lambda: all(b"ready" in j.tail for j in jobs), timeout=15), "children not ready"
        )
        pids = [j.proc.pid for j in jobs]
        t0 = time.monotonic()
        self.r.shutdown()
        self.assertLess(time.monotonic() - t0, 6)
        self.assertEqual(self.r.running(), 0)
        rows = {x["id"]: x for x in self.store.runs_for(self.n.id)}
        self.assertEqual(rows[a]["signal"], 15)
        self.assertEqual(rows[b]["signal"], 9)

        def gone(pid):
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                return True
            return False

        # The group leader is reaped, but its `sleep` may still be a zombie awaiting init's wait()
        # for a moment under load, so poll for the group to vanish instead of checking once.
        self.assertTrue(pump(lambda: all(gone(p) for p in pids), timeout=10), "groups still alive")

    def test_records_bookkeeping(self):
        now = [500.0]
        r = Runner(self.store, home=self.home, clock=lambda: now[0])
        rid = r.run(self.n, spec("sleep 0.2; echo done"), trigger="rule")
        row = self.store.runs_for(self.n.id)[0]
        self.assertEqual((row["started_at"], row["finished_at"]), (500.0, None))
        self.assertEqual(row["notification_id"], self.n.id)
        now[0] = 501.0
        self.assertTrue(pump(lambda: r.running() == 0))
        row = self.store.runs_for(self.n.id)[0]
        self.assertEqual((row["id"], row["finished_at"]), (rid, 501.0))
        self.assertGreaterEqual(row["duration_ms"], 150)


if __name__ == "__main__":
    unittest.main()
