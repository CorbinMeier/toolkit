"""Reply actions (x-nc-actions): hint parsing, templating, allowlist, sending, the server flow,
hot reload, the bus methods and the CLI. Headless: a local http.server on 127.0.0.1, private
buses, daemons with --no-display --no-sound --no-run. Nothing here shows a window."""

import json
import logging
import os
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from gi.repository import Gio, GLib

from notifyd.__main__ import Config, Runtime, build_daemon
from notifyd.dbus_server import CTL_IFACE, CTL_PATH, FDO_IFACE, FDO_PATH, REPLY_CONFIRM_TTL
from notifyd.http_actions import (
    MAX_SEND_ERRORS,
    ConfigError,
    HintError,
    HttpActions,
    normalize_url,
    parse_config,
    parse_hint,
)
from notifyd.model import Notification, to_web
from notifyd.sound import NullPlayer
from tests.test_integration import Caller, connect, pump_briefly, pump_until
from tests.test_wiring import FakeCenter, WiringCase

ROOT = Path(__file__).resolve().parent.parent


# ---- a local HTTP server -----------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _serve(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        self.server.seen.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            }
        )
        route = self.path.split("?")[0]
        try:
            if "/redir" in route:
                self.send_response(302)
                self.send_header("Location", "/final")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif "/slow" in route:
                time.sleep(1.5)
                self._reply(200, b"late")
            elif "/big" in route:
                self.send_response(200)
                self.send_header("Content-Length", str(1 << 20))
                self.end_headers()
                for _ in range(128):
                    self.wfile.write(b"x" * 8192)
            elif "/fail" in route:
                self._reply(500, b"boom")
            elif "/echo-auth" in route:
                self._reply(200, self.headers.get("Authorization", "").encode())
            else:
                self._reply(200, b"ok")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _reply(self, code, data):
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_PATCH = _serve


def start_http():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    srv.seen = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def toml_config(port, app="Example", prefixes=None, top="", entry=""):
    prefixes = prefixes if prefixes is not None else [f"http://127.0.0.1:{port}/"]
    plist = ", ".join(json.dumps(p) for p in prefixes)
    return f'{top}\n[[allow]]\napp = "{app}"\nprefixes = [{plist}]\n{entry}\n'


def hint_json(base, path="/endpoint/reply/thread-22/{choice}", **over):
    d = {"choices": ["yes", "no"], "request": {"method": "POST", "url": base + path}}
    d.update(over)
    return json.dumps(d)


def mk_note(hint, app="Example", id=7, **extra):
    hints = {"x-nc-actions": hint, **extra}
    return Notification.from_notify(id, app, 0, "", "S", "b", [], hints, -1, 100.0)


class HttpBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = start_http()
        cls.port = cls.srv.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.srv.seen.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def actions(self, text=None, **kw):
        if text is None:
            text = toml_config(self.port, **kw)
        (self.dir / "http-actions.toml").write_text(text)
        ha = HttpActions(self.dir)
        self.assertIsNone(ha.load())
        return ha

    def send(self, ha, n, **kw):
        out = []
        reason = ha.send(n, on_done=out.append, **kw)
        self.assertIsNone(reason, reason)
        pump_until(lambda: out, 10)
        return out[0]


# ---- hint parsing ------------------------------------------------------------------------

URL = "http://127.0.0.1:4320/endpoint/reply/thread-22/{choice}"


def raw(**over):
    d = {"choices": ["yes", "no"], "request": {"url": URL}}
    d.update(over)
    return json.dumps(d)


class SendErrorBoundTests(unittest.TestCase):
    """The in-memory per-notification send-error map stays bounded."""

    def test_set_and_clear(self):
        ha = HttpActions(None)
        ha.set_error(1, "boom")
        self.assertEqual(ha._send_errors, {1: "boom"})
        ha.set_error(1, None)
        ha.set_error(2, None)  # clearing an unknown id is fine
        self.assertEqual(ha._send_errors, {})

    def test_cap_drops_oldest_first(self):
        ha = HttpActions(None)
        for i in range(1, MAX_SEND_ERRORS + 11):
            ha.set_error(i, f"e{i}")
        self.assertEqual(MAX_SEND_ERRORS, 500)
        self.assertEqual(len(ha._send_errors), MAX_SEND_ERRORS)
        self.assertNotIn(1, ha._send_errors)
        self.assertNotIn(10, ha._send_errors)
        self.assertIn(11, ha._send_errors)
        self.assertIn(MAX_SEND_ERRORS + 10, ha._send_errors)

    def test_an_updated_entry_counts_as_newest(self):
        ha = HttpActions(None)
        for i in range(1, MAX_SEND_ERRORS + 1):
            ha.set_error(i, "old")
        ha.set_error(1, "again")  # refreshed: now the newest
        ha.set_error(MAX_SEND_ERRORS + 1, "new")  # evicts id 2, not id 1
        self.assertEqual(ha._send_errors[1], "again")
        self.assertNotIn(2, ha._send_errors)
        self.assertEqual(len(ha._send_errors), MAX_SEND_ERRORS)

    def test_forget_forget_all_and_sweep(self):
        ha = HttpActions(None)
        for i in range(1, 6):
            ha.set_error(i, "x")
        ha.forget([1, 2, 99])
        self.assertEqual(sorted(ha._send_errors), [3, 4, 5])
        ha.sweep(lambda ids: [i for i in ids if i == 4])  # only 4 still exists
        self.assertEqual(sorted(ha._send_errors), [4])
        ha.forget_all()
        self.assertEqual(ha._send_errors, {})
        ha.sweep(lambda ids: self.fail("nothing to ask about"))  # empty map: no lookup


class ParseHintTests(unittest.TestCase):
    def test_valid_shapes(self):
        h = parse_hint(raw())
        self.assertEqual(h.choices, (("yes", "yes"), ("no", "no")))
        self.assertEqual(
            (h.method, h.content_type, h.host), ("POST", "application/json", "127.0.0.1:4320")
        )
        h = parse_hint(raw(choices=[{"id": "yes", "label": "Yes"}, {"id": "n.o-_1"}]))
        self.assertEqual(h.choices, (("yes", "Yes"), ("n.o-_1", "n.o-_1")))
        self.assertIsNone(h.text)
        h = parse_hint(
            json.dumps(
                {
                    "text": {"placeholder": "Say", "max": 50},
                    "request": {"method": "PUT", "url": "https://Example.com/x?t={text}"},
                }
            )
        )
        self.assertEqual(h.text, {"placeholder": "Say", "max": 50})
        self.assertEqual(h.host, "example.com")
        h = parse_hint(raw(text=True, request={"url": URL + "?t={text}"}))
        self.assertEqual(h.text["max"], 2000)
        # literal braces in a JSON body are not placeholders
        h = parse_hint(raw(request={"url": URL, "body": '{"answer": "{choice}", "n": {"a": 1}}'}))
        self.assertIn("{choice}", h.body)
        # six choices is the limit
        parse_hint(raw(choices=[f"c{i}" for i in range(6)]))

    def test_invalid(self):
        many = [f"c{i}" for i in range(7)]
        big = raw(request={"url": URL, "body": "x" * 9000})
        cases = {
            "not a string": 42,
            "bad json": "{nope",
            "array": "[1]",
            "oversize": big,
            "unknown key": raw(extra=1),
            "no request": json.dumps({"choices": ["a"]}),
            "request not object": raw(request="x"),
            "nothing to do": json.dumps({"request": {"url": URL}}),
            "too many choices": raw(choices=many),
            "choices not list": raw(choices="yes"),
            "empty id": raw(choices=[""]),
            "slash in id": raw(choices=["a/b"]),
            "long id": raw(choices=["a" * 41]),
            "dot id": raw(choices=[".."]),
            "single dot id": raw(choices=["."]),
            "space in id": raw(choices=["a b"]),
            "duplicate id": raw(choices=["a", "a"]),
            "long label": raw(choices=[{"id": "a", "label": "x" * 41}]),
            "control in label": raw(choices=[{"id": "a", "label": "x\ny"}]),
            "choice extra key": raw(choices=[{"id": "a", "url": "x"}]),
            "choice number": raw(choices=[1]),
            "bad method": raw(request={"method": "DELETE", "url": URL}),
            "ftp": raw(request={"url": "ftp://127.0.0.1/{choice}"}),
            "relative": raw(request={"url": "/x/{choice}"}),
            "userinfo": raw(request={"url": "http://a@127.0.0.1/{choice}"}),
            "fragment": raw(request={"url": URL + "#x"}),
            "get with body": raw(request={"method": "GET", "url": URL, "body": "x"}),
            "bad content type": raw(request={"url": URL, "content_type": "text/html"}),
            "unknown placeholder": raw(request={"url": URL + "/{foo}"}),
            "unknown in body": raw(request={"url": URL, "body": "{who}"}),
            "choice w/o choices": json.dumps({"text": True, "request": {"url": URL}}),
            "text w/o text": raw(request={"url": URL + "?q={text}"}),
            "choices unused": raw(request={"url": "http://127.0.0.1:4320/x"}),
            "text unused": raw(text=True),
            "placeholder in host": raw(request={"url": "http://{choice}.evil.com/x"}),
            "placeholder in port": raw(request={"url": "http://127.0.0.1:{choice}/x"}),
            "text max 0": raw(text={"max": 0}),
            "text max big": raw(text={"max": 2001}),
            "text max bool": raw(text={"max": True}),
            "text extra": raw(text={"x": 1}),
            "text string": raw(text="yes"),
            "request extra": raw(request={"url": URL, "proxy": "x"}),
        }
        for name, value in cases.items():
            if name in ("text unused",):
                value = raw(text=True)  # url has {choice} but no {text}
            with self.subTest(name):
                with self.assertRaises(HintError):
                    parse_hint(value)

    def test_text_only_with_text_placeholder(self):
        h = parse_hint(
            json.dumps({"text": True, "request": {"url": "http://127.0.0.1:1/r?t={text}"}})
        )
        self.assertEqual(h.choices, ())


class NormalizeTests(unittest.TestCase):
    def test_normalize(self):
        n = normalize_url
        self.assertEqual(n("HTTP://LocalHost:80/a/b"), "http://localhost/a/b")
        self.assertEqual(n("https://x.org:443"), "https://x.org/")
        self.assertEqual(n("http://x.org:8080/a/../b/./c/"), "http://x.org:8080/b/c/")
        self.assertEqual(n("http://x.org/a/%2e%2e/b"), "http://x.org/b")
        self.assertEqual(n("http://x.org/../../a"), "http://x.org/a")
        self.assertEqual(n("http://x.org/a?x=1&y=2"), "http://x.org/a?x=1&y=2")
        self.assertEqual(n("http://[::1]:99/a"), "http://[::1]:99/a")
        for bad in (
            "ftp://x.org/",
            "file:///etc/passwd",
            "http://u@x.org/",
            "http://u:p@x.org/",
            "http://x.org:99999/",
            "http://x.org/a b",
            "http://x.org/a\r\nHost: evil",
            "http://x.org/a\\b",
            "http://x.org/#frag",
            "http:///nohost",
            "//x.org/",
            "",
        ):
            with self.subTest(bad):
                self.assertIsNone(n(bad))


class ConfigParseTests(unittest.TestCase):
    def test_defaults_and_errors(self):
        c = parse_config("")
        self.assertEqual((c.enabled, c.text_reply, c.timeout_s, c.entries), (True, True, 10.0, ()))
        bad = [
            "bogus = 1",
            "enabled = 'yes'",
            "timeout_s = 0",
            "timeout_s = 500",
            "max_response_bytes = 0",
            "allow = 3",
            "[[allow]]\napp = 'A'",
            "[[allow]]\nprefixes = ['http://x/']",
            "[[allow]]\napp = 'A'\nprefixes = ['ftp://x/']",
            "[[allow]]\napp = 'A'\nprefixes = ['http://u@x/']",
            "[[allow]]\napp = 'A'\nprefixes = ['http://x/?q=1']",
            "[[allow]]\napp = 'A'\nprefixes = ['http://x/']\nweird = 1",
            "[[allow]]\napp = 'A'\nprefixes = ['http://x/']\nheaders = {'Host' = 'x'}",
            "[[allow]]\napp = 'A'\nprefixes = ['http://x/']\nheaders = {'X' = \"a\\nb\"}",
            "[[allow]]\napp = 'A'\nprefixes = ['http://x/']\nconfirm = 'yes'",
            "not toml = =",
        ]
        for text in bad:
            with self.subTest(text):
                with self.assertRaises(ConfigError):
                    parse_config(text)


# ---- allowlist ---------------------------------------------------------------------------


class AllowlistTests(HttpBase):
    def plan(self, n, **kw):
        return self.actions(**kw).plan(n)

    def note(self, **kw):
        return mk_note(hint_json(self.base), **kw)

    def test_allowed_and_plan_shape(self):
        p = self.plan(self.note())
        self.assertTrue(p.allowed)
        self.assertIsNone(p.reason)
        web = p.to_web()
        self.assertEqual(
            set(web),
            {
                "choices",
                "text",
                "host",
                "method",
                "allowed",
                "reason",
                "confirm",
                "answered",
                "error",
            },
        )
        self.assertEqual(web["host"], f"127.0.0.1:{self.port}")
        self.assertEqual(web["choices"][0], {"id": "yes", "label": "yes"})
        self.assertNotIn("thread-22", json.dumps(web))

    def test_no_hint_is_none(self):
        n = Notification.from_notify(1, "Example", 0, "", "S", "b", [], {}, -1, 1.0)
        self.assertIsNone(self.actions().plan(n))

    def test_app_match_is_case_insensitive_and_exact(self):
        self.assertTrue(
            self.plan(
                self.note(app="example") if False else mk_note(hint_json(self.base), app="EXAMPLE")
            ).allowed
        )
        p = self.plan(mk_note(hint_json(self.base), app="Example2"))
        self.assertFalse(p.allowed)
        self.assertIn("app", p.reason)
        self.assertFalse(self.plan(mk_note(hint_json(self.base), app="Hu")).allowed)

    def test_empty_missing_and_disabled(self):
        ha = HttpActions(self.dir)  # no file: empty allowlist
        self.assertIsNone(ha.load())
        p = ha.plan(self.note())
        self.assertFalse(p.allowed)
        self.assertEqual(p.reason, "no allowlist configured")
        self.assertEqual(ha.check(self.note(), "yes"), "no allowlist configured")
        self.assertFalse(
            self.plan(self.note(), text="enabled = false\n" + toml_config(self.port)).allowed
        )
        self.assertFalse(self.plan(self.note(), entry="enabled = false").allowed)
        self.assertEqual(self.plan(self.note(), top="").reason, None)

    def test_lookalike_hosts_userinfo_schemes_ports(self):
        cases = {
            "lookalike subdomain": f"http://127.0.0.1.evil.com:{self.port}/endpoint/x/{{choice}}",
            "suffix host": f"http://127.0.0.10:{self.port}/endpoint/{{choice}}",
            "other port": f"http://127.0.0.1:{self.port + 1}/endpoint/{{choice}}",
            "https": f"https://127.0.0.1:{self.port}/endpoint/{{choice}}",
            "other path": f"http://127.0.0.1:{self.port}/other/{{choice}}",
            "dotdot escape": f"http://127.0.0.1:{self.port}/endpoint/../admin/{{choice}}",
            "encoded dotdot": f"http://127.0.0.1:{self.port}/endpoint/%2e%2e/admin/{{choice}}",
            "default port": "http://127.0.0.1/endpoint/{choice}",
        }
        ha = self.actions(prefixes=[f"http://127.0.0.1:{self.port}/endpoint/"])
        for name, url in cases.items():
            with self.subTest(name):
                p = ha.plan(mk_note(json.dumps({"choices": ["a"], "request": {"url": url}})))
                self.assertFalse(p.allowed, url)
                self.assertIsNone(p.error)
        for name, url in {
            "userinfo": f"http://127.0.0.1:{self.port}@evil.com/endpoint/{{choice}}",
            "userinfo2": f"http://evil@127.0.0.1:{self.port}/endpoint/{{choice}}",
            "ftp": f"ftp://127.0.0.1:{self.port}/endpoint/{{choice}}",
        }.items():
            with self.subTest(name):
                p = ha.plan(mk_note(json.dumps({"choices": ["a"], "request": {"url": url}})))
                self.assertFalse(p.allowed)
                self.assertIsNotNone(p.error)

    def test_normalization_helps_the_legit_sender(self):
        ha = self.actions(prefixes=[f"HTTP://127.0.0.1:{self.port}/endpoint"])
        url = f"HTTP://127.0.0.1:{self.port}/x/../endpoint/./reply/{{choice}}"
        self.assertTrue(
            ha.plan(mk_note(json.dumps({"choices": ["a"], "request": {"url": url}}))).allowed
        )
        # path-segment boundary: /endpoint-evil is not under /endpoint
        url = f"http://127.0.0.1:{self.port}/endpoint-evil/{{choice}}"
        self.assertFalse(
            ha.plan(mk_note(json.dumps({"choices": ["a"], "request": {"url": url}}))).allowed
        )
        # default port equals the explicit one
        ha = self.actions(prefixes=["http://127.0.0.1/"])
        url = "http://127.0.0.1:80/r/{choice}"
        self.assertTrue(
            ha.plan(mk_note(json.dumps({"choices": ["a"], "request": {"url": url}}))).allowed
        )

    def test_text_reply_false(self):
        both = hint_json(self.base, text=True, request={"url": self.base + "/r/{choice}?t={text}"})
        ha = self.actions(top="text_reply = false")
        p = ha.plan(mk_note(both))
        self.assertTrue(p.allowed)
        self.assertIsNone(p.text)
        self.assertEqual(ha.check(mk_note(both), None, "hello"), "free-text replies are disabled")
        self.assertIsNone(ha.check(mk_note(both), "yes"))
        text_only = json.dumps({"text": True, "request": {"url": self.base + "/r?t={text}"}})
        p = ha.plan(mk_note(text_only))
        self.assertFalse(p.allowed)
        self.assertEqual(p.reason, "free-text replies are disabled")
        self.assertTrue(self.actions().plan(mk_note(text_only)).allowed)

    def test_invalid_hint_plan(self):
        p = self.actions().plan(mk_note("{nope"))
        self.assertFalse(p.allowed)
        self.assertIn("JSON", p.error)
        self.assertEqual((p.choices, p.text, p.host), ([], None, ""))

    def test_on_arrival_logs_warning_and_records(self):
        ha = self.actions()
        n = mk_note(hint_json(self.base, choices=["a/b"]))
        with self.assertLogs("notifyd.http", "WARNING") as cm:
            ha.on_arrival(n)
        self.assertIn("ignored", cm.output[0])
        self.assertIn("choice id", n.reply_error)
        ok = self.note()
        ha.on_arrival(ok)
        self.assertIsNone(ok.reply_error)

    def test_bad_file_fails_closed_and_recovers(self):
        ha = self.actions()
        self.assertTrue(ha.plan(self.note()).allowed)
        (self.dir / "http-actions.toml").write_text("enabled = [")
        with self.assertLogs("notifyd.http", "ERROR"):
            err = ha.load()
        self.assertIn("http-actions.toml", err)
        self.assertEqual(ha.error, err)
        self.assertFalse(ha.plan(self.note()).allowed)
        (self.dir / "http-actions.toml").write_text(toml_config(self.port))
        self.assertIsNone(ha.load())
        self.assertIsNone(ha.error)
        self.assertTrue(ha.plan(self.note()).allowed)
        (self.dir / "http-actions.toml").unlink()
        self.assertIsNone(ha.load())
        self.assertFalse(ha.plan(self.note()).allowed)


# ---- templating and injection ------------------------------------------------------------


class TemplatingTests(HttpBase):
    def prep(self, hint, choice=None, text=None, **kw):
        ha = self.actions(**kw)
        return ha.prepare(mk_note(hint), choice, text)

    def test_choice_in_path(self):
        p = self.prep(hint_json(self.base), "yes")
        self.assertEqual(p.url, f"{self.base}/endpoint/reply/thread-22/yes")
        self.assertEqual((p.method, p.body, p.label), ("POST", None, "yes"))

    def test_text_is_one_path_segment_or_query_value(self):
        h = json.dumps(
            {"text": True, "request": {"method": "GET", "url": self.base + "/endpoint/t/{text}"}}
        )
        attacks = {
            "../": "..%2F",
            "../../admin": "..%2F..%2Fadmin",
            "%2f": "%252f",
            "%2e%2e": "%252e%252e",
            "a/b": "a%2Fb",
            "x?y=1": "x%3Fy%3D1",
            "x#frag": "x%23frag",
            "a b": "a%20b",
            "a\r\nHost: evil": "a%0D%0AHost%3A%20evil",
            "\\..\\": "%5C..%5C",
            "ü": "%C3%BC",
        }
        for text, enc in attacks.items():
            with self.subTest(text):
                p = self.prep(h, None, text, prefixes=[self.base + "/endpoint/t/"])
                self.assertEqual(p.url, f"{self.base}/endpoint/t/{enc}")
                path = urlsplit(p.url).path
                self.assertEqual(path.count("/"), 3)
                self.assertEqual(urlsplit(p.url).query, "")
                self.assertNotIn("#", p.url)
                self.assertNotIn("\r", p.url)

    def test_text_in_query(self):
        h = json.dumps(
            {"text": True, "request": {"method": "GET", "url": self.base + "/r?t={text}&k=1"}}
        )
        p = self.prep(h, None, "a&k=2#x/y", prefixes=[self.base + "/r"])
        self.assertEqual(parse_qs(urlsplit(p.url).query), {"t": ["a&k=2#x/y"], "k": ["1"]})

    def test_dot_segments_refused(self):
        h = json.dumps({"text": True, "request": {"url": self.base + "/endpoint/t/{text}"}})
        ha = self.actions(prefixes=[self.base + "/endpoint/"])
        for t in (".", ".."):
            with self.assertRaises(Exception) as cm:
                ha.prepare(mk_note(h), None, t)
            self.assertIn("unsafe", str(cm.exception))
        self.assertIn("unsafe", ha.check(mk_note(h), None, ".."))

    def test_body_json_escaping_and_breakout(self):
        body = '{"answer": "{choice}", "text": "{text}"}'
        h = hint_json(
            self.base,
            text=True,
            request={"url": self.base + "/r/{choice}", "body": body},
        )
        evil = 'x", "admin": true, "y": "\\" \r\n\t' + chr(0) + "{choice}"
        p = self.prep(h, "yes", evil, prefixes=[self.base + "/r/"])
        data = json.loads(p.body)
        self.assertEqual(set(data), {"answer", "text"})
        self.assertEqual(data["text"], evil)  # also: "{choice}" inside the text is not expanded
        self.assertEqual(data["answer"], "yes")
        self.assertEqual(p.headers["Content-Type"], "application/json")
        self.assertNotIn(b"\r", p.body)

    def test_body_form_and_text(self):
        h = hint_json(
            self.base,
            text=True,
            request={
                "url": self.base + "/r/{choice}",
                "body": "a={choice}&t={text}",
                "content_type": "application/x-www-form-urlencoded",
            },
        )
        p = self.prep(h, "no", "x=1&a=evil\r\n", prefixes=[self.base + "/r/"])
        self.assertEqual(parse_qs(p.body.decode()), {"a": ["no"], "t": ["x=1&a=evil\r\n"]})
        h = hint_json(
            self.base,
            text=True,
            request={
                "url": self.base + "/r/{choice}",
                "body": "{choice}: {text}",
                "content_type": "text/plain",
            },
        )
        p = self.prep(h, "no", "ünï\ncode", prefixes=[self.base + "/r/"])
        self.assertEqual(p.body.decode(), "no: ünï\ncode")
        self.assertEqual(p.headers["Content-Type"], "text/plain")

    def test_unused_placeholder_is_empty(self):
        h = hint_json(
            self.base,
            text=True,
            request={"url": self.base + "/r/{choice}", "body": '{"c": "{choice}", "t": "{text}"}'},
        )
        p = self.prep(h, "yes", None, prefixes=[self.base + "/r/"])
        self.assertEqual(json.loads(p.body), {"c": "yes", "t": ""})

    def test_refusals(self):
        ha = self.actions()
        n = mk_note(hint_json(self.base))
        self.assertEqual(ha.check(n, "maybe"), "unknown choice")
        self.assertEqual(ha.check(n, None, None), "nothing to send")
        self.assertIn("free-text", ha.check(n, None, "hi"))
        self.assertIsNone(ha.check(n, "yes"))
        t = mk_note(
            json.dumps({"text": {"max": 5}, "request": {"url": self.base + "/endpoint/{text}"}})
        )
        self.assertIn("longer than 5", ha.check(t, None, "123456"))
        self.assertIsNone(ha.check(t, None, "12345"))
        self.assertEqual(ha.check(t, None, "   "), "the reply is empty")
        self.assertEqual(ha.check(mk_note("{bad"), "yes"), "the reply hint is invalid")
        no_hint = Notification.from_notify(1, "Example", 0, "", "S", "b", [], {}, -1, 1.0)
        self.assertEqual(ha.check(no_hint, "yes"), "this notification has no reply actions")

    def test_final_url_rechecked_against_allowlist(self):
        # The template passes (sample "x"), but the real value must still stay in the prefix:
        # a choice id cannot change the path structure, so the final URL stays allowed.
        p = self.prep(hint_json(self.base), "no", prefixes=[self.base + "/endpoint/reply/"])
        self.assertTrue(p.url.startswith(self.base + "/endpoint/reply/"))


# ---- sending -----------------------------------------------------------------------------


class SendTests(HttpBase):
    def test_post_json(self):
        ha = self.actions()
        h = hint_json(
            self.base,
            text=True,
            request={
                "url": self.base + "/endpoint/reply/thread-22/{choice}",
                "body": '{"answer": "{choice}", "text": "{text}"}',
            },
        )
        res = self.send(ha, mk_note(h), choice="yes", text='hi "there"')
        self.assertTrue(res.ok)
        self.assertEqual((res.status, res.output, res.label), (200, "ok", "yes"))
        (seen,) = self.srv.seen
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["path"], "/endpoint/reply/thread-22/yes")
        self.assertEqual(json.loads(seen["body"]), {"answer": "yes", "text": 'hi "there"'})
        self.assertEqual(seen["headers"]["content-type"], "application/json")
        self.assertNotIn("authorization", seen["headers"])
        self.assertEqual(seen["headers"]["user-agent"], "cosmic-notification-center")

    def test_get_with_query_and_methods(self):
        ha = self.actions()
        for method in ("GET", "PUT", "PATCH"):
            h = json.dumps(
                {
                    "text": True,
                    "request": {"method": method, "url": self.base + "/endpoint/q?t={text}"},
                }
            )
            res = self.send(ha, mk_note(h), text="a b")
            self.assertTrue(res.ok, method)
            self.assertEqual(self.srv.seen[-1]["method"], method)
            self.assertEqual(self.srv.seen[-1]["path"], "/endpoint/q?t=a%20b")

    def test_token_header_never_logged_and_redacted(self):
        tok = self.dir / "actions.token"
        tok.write_text("s3cret-token-value\n")
        ha = self.actions(entry=f'token_file = "{tok}"')
        h = hint_json(self.base, path="/endpoint/echo-auth/{choice}")
        with self.assertLogs("notifyd", "DEBUG") as cm:
            res = self.send(ha, mk_note(h), choice="yes")
            logging.getLogger("notifyd").debug("marker")
        self.assertEqual(self.srv.seen[-1]["headers"]["authorization"], "Bearer s3cret-token-value")
        self.assertNotIn("s3cret", "\n".join(cm.output))
        self.assertNotIn(
            "s3cret", res.output
        )  # the server echoed it; the stored output is redacted
        self.assertIn("[redacted]", res.output)
        self.assertNotIn("s3cret", json.dumps(ha.plan(mk_note(h)).to_web()))
        self.assertNotIn("s3cret", res.url)

    def test_missing_token_file_refuses_without_sending(self):
        ha = self.actions(entry=f'token_file = "{self.dir}/nope.token"')
        reason = ha.send(mk_note(hint_json(self.base)), choice="yes")
        self.assertEqual(reason, "the token file cannot be read")
        pump_briefly(100)
        self.assertEqual(self.srv.seen, [])
        (self.dir / "empty.token").write_text("\n")
        ha = self.actions(entry=f'token_file = "{self.dir}/empty.token"')
        self.assertIn("empty", ha.send(mk_note(hint_json(self.base)), choice="yes"))

    def test_sender_headers_ignored_and_static_headers_sent(self):
        ha = self.actions(entry='headers = {"X-Source" = "notifyd"}')
        h = hint_json(
            self.base,
            request={
                "url": self.base + "/endpoint/{choice}",
                "headers": {"Authorization": "Bearer evil", "X-Evil": "1", "Host": "evil.com"},
            },
        )
        res = self.send(ha, mk_note(h), choice="yes")
        self.assertTrue(res.ok)
        headers = self.srv.seen[-1]["headers"]
        self.assertEqual(headers["x-source"], "notifyd")
        for k in ("authorization", "x-evil"):
            self.assertNotIn(k, headers)
        self.assertEqual(headers["host"], f"127.0.0.1:{self.port}")

    def test_redirect_not_followed(self):
        ha = self.actions()
        res = self.send(
            ha, mk_note(hint_json(self.base, path="/endpoint/redir/{choice}")), choice="yes"
        )
        self.assertEqual(res.status, 302)
        self.assertFalse(res.ok)
        self.assertEqual([s["path"] for s in self.srv.seen], ["/endpoint/redir/yes"])

    def test_non_2xx(self):
        ha = self.actions()
        res = self.send(
            ha, mk_note(hint_json(self.base, path="/endpoint/fail/{choice}")), choice="yes"
        )
        self.assertEqual((res.status, res.ok, res.output), (500, False, "boom"))

    def test_timeout(self):
        ha = self.actions(top="timeout_s = 0.3")
        t0 = time.monotonic()
        res = self.send(
            ha, mk_note(hint_json(self.base, path="/endpoint/slow/{choice}")), choice="yes"
        )
        self.assertLess(time.monotonic() - t0, 1.4)
        self.assertIsNone(res.status)
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "timed out")
        self.assertEqual(res.output, "timed out")

    def test_connection_refused(self):
        dead = start_http()
        port = dead.server_address[1]
        dead.shutdown()
        dead.server_close()
        ha = self.actions(toml_config(port))
        h = hint_json(f"http://127.0.0.1:{port}")
        res = self.send(ha, mk_note(h), choice="yes")
        self.assertIsNone(res.status)
        self.assertIsNotNone(res.error)

    def test_response_cap_and_tail(self):
        ha = self.actions(top="max_response_bytes = 1000")
        res = self.send(
            ha, mk_note(hint_json(self.base, path="/endpoint/big/{choice}")), choice="yes"
        )
        self.assertEqual(res.status, 200)
        self.assertEqual(res.bytes_read, 1000)
        self.assertEqual(len(res.output), 1000)
        ha = self.actions()
        res = self.send(
            ha, mk_note(hint_json(self.base, path="/endpoint/big/{choice}")), choice="yes"
        )
        self.assertEqual(res.bytes_read, 65536)
        self.assertEqual(len(res.output), 4096)  # the first 4 KB are kept

    def test_send_never_raises(self):
        ha = self.actions()
        self.assertEqual(ha.send(None, choice="yes"), "internal error")
        self.assertIsNotNone(ha.send(mk_note(hint_json(self.base)), choice="nope"))

        def boom(res):
            raise RuntimeError("callback bug")

        with self.assertLogs("notifyd.http", "ERROR"):
            self.assertIsNone(ha.send(mk_note(hint_json(self.base)), choice="yes", on_done=boom))
            pump_briefly(500)


# ---- the server flow ---------------------------------------------------------------------


class ReplyCenter(FakeCenter):
    def __init__(self, server):
        super().__init__(server)
        self.refreshed = 0

    def refresh(self):
        self.refreshed += 1


class ServerCase(WiringCase, HttpBase):
    @classmethod
    def setUpClass(cls):
        HttpBase.setUpClass()
        WiringCase.setUpClass()

    @classmethod
    def tearDownClass(cls):
        WiringCase.tearDownClass()
        HttpBase.tearDownClass()

    def setUp(self):
        WiringCase.setUp(self)
        self.srv.seen.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.now = [5000.0]

    def tearDown(self):
        WiringCase.tearDown(self)
        self.tmp.cleanup()

    def build_reply(self, config=None, **entry):
        text = config if config is not None else toml_config(self.port, **entry)
        (self.dir / "http-actions.toml").write_text(text)
        self.ha = HttpActions(self.dir, clock=lambda: self.now[0])
        self.ha.load()
        self.server = build_daemon(
            self.server_conn,
            self.store,
            self.display,
            ReplyCenter,
            http_actions=self.ha,
            clock=lambda: self.now[0],
        )
        self.center = self.server.center
        self.changed = []
        self.server.add_changed_observer(lambda: self.changed.append(1))
        return self.server

    def post(self, hint, app="Example", extra_hints=None):
        hints = {"x-nc-actions": GLib.Variant("s", hint)}
        for k, v in (extra_hints or {}).items():
            hints[k] = GLib.Variant("s", v)
        args = GLib.Variant("(susssasa{sv}i)", (app, 0, "", "Q", "b", [], hints, -1))
        return self.c.call(FDO_PATH, FDO_IFACE, "Notify", args, "(u)").unpack()[0]

    def settle(self, nid, runs=1):
        pump_until(
            lambda: len(self.store.runs_for(nid)) >= runs and not self.server._reply_busy, 10
        )

    def reply_web(self, nid):
        return next(h for h in self.c_history() if h["id"] == nid)["reply"]

    def c_history(self):
        args = GLib.Variant("(suu)", ("", 0, 50))
        return json.loads(self.c.call(CTL_PATH, CTL_IFACE, "GetHistory", args, "(s)").unpack()[0])


class ReplyServerTests(ServerCase):
    def test_choice_flow_records_answers_closes(self):
        self.build_reply()
        nid = self.post(hint_json(self.base))
        r = self.server.reply_choice(nid, "yes")
        self.assertEqual(r, {"status": "sent"})
        self.settle(nid)
        (seen,) = self.srv.seen
        self.assertEqual(seen["path"], "/endpoint/reply/thread-22/yes")
        (run,) = self.store.runs_for(nid)
        self.assertEqual(run["trigger"], "button")
        self.assertEqual(run["button_label"], "yes")
        self.assertEqual(run["exit_code"], 200)
        self.assertEqual(run["output_tail"], "ok")
        self.assertEqual(
            run["cmd"],
            {"http": {"method": "POST", "url": f"{self.base}/endpoint/reply/thread-22/yes"}},
        )
        self.assertIsNotNone(run["finished_at"])
        n = self.store.get(nid)
        self.assertEqual(n.hints["x-nc-answered"], {"choice": "yes", "at": 5000.0, "status": 200})
        self.assertIn("x-nc-actions", n.hints)
        self.assertIsNotNone(n.closed_at)
        self.assertEqual(int(n.close_reason), 2)  # DISMISSED
        self.assertIn(nid, self.display.closed)
        self.assertTrue(self.changed)
        self.assertGreaterEqual(self.center.refreshed, 1)
        web = self.reply_web(nid)
        self.assertEqual(web["answered"]["choice"], "yes")
        self.assertTrue(web["allowed"])
        # answered: no second reply
        self.assertEqual(
            self.server.reply_choice(nid, "no"), {"status": "refused", "reason": "already answered"}
        )
        self.assertEqual(len(self.srv.seen), 1)

    def test_close_false_keeps_open(self):
        self.build_reply(entry="close = false")
        nid = self.post(hint_json(self.base))
        self.server.reply_choice(nid, "no")
        self.settle(nid)
        n = self.store.get(nid)
        self.assertIsNone(n.closed_at)
        self.assertEqual(n.hints["x-nc-answered"]["choice"], "no")

    def test_failure_is_recorded_not_answered_and_retryable(self):
        self.build_reply()
        nid = self.post(hint_json(self.base, path="/endpoint/fail/{choice}"))
        self.server.reply_choice(nid, "yes")
        self.settle(nid)
        (run,) = self.store.runs_for(nid)
        self.assertEqual(run["exit_code"], 500)
        n = self.store.get(nid)
        self.assertNotIn("x-nc-answered", n.hints)
        self.assertIsNone(n.closed_at)
        self.assertIn("500", self.reply_web(nid)["error"])
        self.assertEqual(self.server.reply_choice(nid, "yes"), {"status": "sent"})  # may retry
        self.settle(nid, 2)
        self.assertEqual(len(self.store.runs_for(nid)), 2)

    # ---- the send-error map is evicted -----------------------------------------------------

    def fail_one(self, build=True, **kw):
        if build:
            self.build_reply(**kw)
        nid = self.post(hint_json(self.base, path="/endpoint/{choice}", choices=["fail", "ok"]))
        self.server.reply_choice(nid, "fail")
        self.settle(nid)
        self.assertIn(nid, self.ha._send_errors)
        return nid

    def test_error_dropped_when_answered_successfully(self):
        nid = self.fail_one(entry="close = false")
        self.assertEqual(self.server.reply_choice(nid, "ok"), {"status": "sent"})
        self.settle(nid, 2)
        self.assertNotIn(nid, self.ha._send_errors)
        self.assertIsNone(self.reply_web(nid)["error"])

    def test_error_dropped_when_deleted(self):
        nid = self.fail_one()
        keep = self.fail_one(build=False)
        self.server.delete([nid])
        self.assertNotIn(nid, self.ha._send_errors)
        self.assertIn(keep, self.ha._send_errors)

    def test_error_dropped_when_history_cleared(self):
        self.fail_one()
        self.server.clear_history()
        self.assertEqual(self.ha._send_errors, {})

    def test_error_dropped_when_replaced(self):
        nid = self.fail_one()
        args = GLib.Variant(
            "(susssasa{sv}i)",
            ("Example", nid, "", "Q2", "b", [], {}, -1),
        )
        self.assertEqual(self.c.call(FDO_PATH, FDO_IFACE, "Notify", args, "(u)").unpack()[0], nid)
        self.assertNotIn(nid, self.ha._send_errors)

    def test_error_dropped_when_pruned(self):
        nid = self.fail_one()
        self.assertEqual(self.store.prune(1, self.now[0] + 10 * 86400), 1)
        self.ha.sweep(self.store.existing_ids)
        self.assertEqual(self.ha._send_errors, {})
        self.assertEqual(self.store.existing_ids([nid, 12345]), set())

    def test_late_failure_for_a_deleted_notification_is_not_remembered(self):
        self.build_reply()
        nid = self.post(hint_json(self.base, path="/endpoint/fail/{choice}"))
        self.server.reply_choice(nid, "yes")
        self.server.delete([nid])  # the send is still in flight
        pump_until(lambda: not self.server._reply_busy, 10)
        self.assertNotIn(nid, self.ha._send_errors)

    def test_network_error_run_has_no_exit_code(self):
        self.build_reply(top="timeout_s = 0.3")
        nid = self.post(hint_json(self.base, path="/endpoint/slow/{choice}"))
        self.server.reply_choice(nid, "yes")
        self.settle(nid)
        (run,) = self.store.runs_for(nid)
        self.assertIsNone(run["exit_code"])
        self.assertEqual(run["output_tail"], "timed out")

    def test_refusals(self):
        self.build_reply()
        nid = self.post(hint_json(self.base))
        self.assertEqual(self.server.reply_choice(nid, "maybe")["status"], "refused")
        self.assertEqual(self.server.reply_choice(9999, "yes")["status"], "refused")
        self.assertEqual(self.server.reply_text(nid, "hi")["status"], "refused")
        other = self.post(hint_json(self.base), app="Other")
        r = self.server.reply_choice(other, "yes")
        self.assertEqual(r["status"], "refused")
        self.assertIn("app", r["reason"])
        plain = self.notify("plain")
        self.assertEqual(self.server.reply_choice(plain, "yes")["status"], "refused")
        self.assertEqual(self.srv.seen, [])

    def test_empty_allowlist_is_inert(self):
        self.build_reply(config="")
        nid = self.post(hint_json(self.base))
        r = self.server.reply_choice(nid, "yes")
        self.assertEqual(r, {"status": "refused", "reason": "no allowlist configured"})
        web = self.reply_web(nid)
        self.assertFalse(web["allowed"])
        self.assertEqual(web["reason"], "no allowlist configured")
        self.assertEqual(self.srv.seen, [])

    def test_busy_refuses_second_send(self):
        self.build_reply()
        nid = self.post(hint_json(self.base, path="/endpoint/slow/{choice}"))
        self.assertEqual(self.server.reply_choice(nid, "yes")["status"], "sent")
        r = self.server.reply_choice(nid, "no")
        self.assertEqual(r["status"], "refused")
        self.assertIn("already being sent", r["reason"])
        self.settle(nid)

    def test_text_reply_and_answered_truncation(self):
        self.build_reply()
        h = json.dumps(
            {
                "text": {"placeholder": "Reply…", "max": 500},
                "request": {
                    "url": self.base + "/endpoint/text",
                    "body": '{"text": "{text}"}',
                },
            }
        )
        nid = self.post(h)
        self.assertEqual(self.server.reply_text(nid, "x" * 501)["status"], "refused")
        self.assertEqual(self.server.reply_text(nid, "  ")["status"], "refused")
        text = "y" * 450
        self.assertEqual(self.server.reply_text(nid, text), {"status": "sent"})
        self.settle(nid)
        self.assertEqual(json.loads(self.srv.seen[-1]["body"]), {"text": text})
        ans = self.store.get(nid).hints["x-nc-answered"]
        self.assertEqual(ans["text"], "y" * 200)
        self.assertNotIn("choice", ans)
        run = self.store.runs_for(nid)[0]
        self.assertEqual(run["button_label"], "reply")
        self.assertNotIn("yyyy", json.dumps(run["cmd"]))  # the body is not recorded

    def test_text_reply_disabled(self):
        self.build_reply(top="text_reply = false")
        h = hint_json(
            self.base, text=True, request={"url": self.base + "/endpoint/r/{choice}?t={text}"}
        )
        nid = self.post(h)
        self.assertIsNone(self.reply_web(nid)["text"])
        r = self.server.reply_text(nid, "hello")
        self.assertEqual(r["status"], "refused")
        self.assertEqual(self.server.reply_choice(nid, "yes")["status"], "sent")
        self.settle(nid)

    def test_forged_answered_hint_is_dropped(self):
        self.build_reply()
        nid = self.post(hint_json(self.base), extra_hints={"x-nc-answered": "{}"})
        self.assertNotIn("x-nc-answered", self.store.get(nid).hints)
        self.assertIsNone(self.reply_web(nid)["answered"])
        self.assertEqual(self.server.reply_choice(nid, "yes")["status"], "sent")
        self.settle(nid)

    def test_invalid_hint_on_arrival(self):
        self.build_reply()
        added = []
        self.server.add_added_observer(added.append)
        with self.assertLogs("notifyd.http", "WARNING"):
            nid = self.post("{not json")
        self.assertIn("JSON", added[0].reply_error)
        web = self.reply_web(nid)
        self.assertFalse(web["allowed"])
        self.assertIn("JSON", web["error"])
        self.assertEqual(self.server.reply_choice(nid, "x")["status"], "refused")

    def test_web_payload_never_leaks_path_body_or_token(self):
        tok = self.dir / "actions.token"
        tok.write_text("topsecret-token")
        self.build_reply(entry=f'token_file = "{tok}"')
        h = hint_json(
            self.base,
            request={
                "url": self.base + "/endpoint/reply/thread-22/{choice}",
                "body": "BODYTEMPLATE{choice}",
            },
        )
        nid = self.post(h)
        blob = json.dumps(self.c_history())
        for leak in ("thread-22", "BODYTEMPLATE", "topsecret", "/endpoint"):
            self.assertNotIn(leak, blob)
        self.assertEqual(self.reply_web(nid)["host"], f"127.0.0.1:{self.port}")
        n = self.store.get(nid)
        self.assertEqual(to_web(n, [])["reply"], self.reply_web(nid))

    def test_hosts_get_reply_attributes(self):
        self.build_reply()
        for host in (self.display, self.server.center):
            self.assertEqual(host.on_reply_choice, self.server.reply_choice)
            self.assertEqual(host.on_reply_text, self.server.reply_text)
            self.assertEqual(host.on_reply_confirm, self.server.confirm_reply)
            self.assertEqual(host.on_reply_cancel, self.server.cancel_reply)

    def test_bus_methods(self):
        self.build_reply()
        nid = self.post(hint_json(self.base))
        r = self.c.call(
            CTL_PATH, CTL_IFACE, "ReplyChoice", GLib.Variant("(us)", (nid, "yes")), "(s)"
        )
        self.assertEqual(json.loads(r.unpack()[0]), {"status": "sent"})
        self.settle(nid)
        r = self.c.call(
            CTL_PATH, CTL_IFACE, "ReplyChoice", GLib.Variant("(us)", (nid, "yes")), "(s)"
        )
        self.assertEqual(json.loads(r.unpack()[0])["reason"], "already answered")
        t = self.post(
            json.dumps({"text": True, "request": {"url": self.base + "/endpoint/t?q={text}"}})
        )
        r = self.c.call(
            CTL_PATH, CTL_IFACE, "ReplyText", GLib.Variant("(us)", (t, "hello there")), "(s)"
        )
        self.assertEqual(json.loads(r.unpack()[0]), {"status": "sent"})
        self.settle(t)
        self.assertEqual(self.srv.seen[-1]["path"], "/endpoint/t?q=hello%20there")


class ConfirmTests(ServerCase):
    def setUp(self):
        super().setUp()
        self.build_reply(entry="confirm = true")
        self.nid = self.post(hint_json(self.base))

    def test_confirm_flow(self):
        r = self.server.reply_choice(self.nid, "yes")
        self.assertEqual(r["status"], "pending_confirm")
        self.assertEqual(r["expires_in"], REPLY_CONFIRM_TTL)
        tok = r["token"]
        self.assertTrue(self.reply_web(self.nid)["confirm"])
        pump_briefly(100)
        self.assertEqual(self.srv.seen, [])  # nothing sent before the confirmation
        for bad_id, bad_tok in ((self.nid + 1, tok), (self.nid, "nope")):
            self.assertEqual(self.server.confirm_reply(bad_id, bad_tok)["status"], "refused")
        self.assertEqual(self.server.confirm_reply(self.nid, tok), {"status": "sent"})
        self.settle(self.nid)
        self.assertEqual(len(self.srv.seen), 1)
        # single use
        self.assertEqual(self.server.confirm_reply(self.nid, tok)["status"], "refused")
        self.assertEqual(len(self.srv.seen), 1)

    def test_cancel(self):
        tok = self.server.reply_choice(self.nid, "yes")["token"]
        self.assertEqual(self.server.cancel_reply(self.nid + 1, tok)["status"], "refused")
        self.assertEqual(self.server.cancel_reply(self.nid, tok), {"status": "cancelled"})
        self.assertEqual(self.server.confirm_reply(self.nid, tok)["status"], "refused")
        pump_briefly(100)
        self.assertEqual(self.srv.seen, [])

    def test_expiry(self):
        tok = self.server.reply_choice(self.nid, "yes")["token"]
        self.now[0] += REPLY_CONFIRM_TTL - 1
        tok2 = self.server.reply_choice(self.nid, "no")["token"]  # replaces the first
        self.assertNotEqual(tok, tok2)
        self.assertEqual(self.server.confirm_reply(self.nid, tok)["status"], "refused")
        self.now[0] += REPLY_CONFIRM_TTL + 1
        r = self.server.confirm_reply(self.nid, tok2)
        self.assertEqual(r["status"], "refused")
        self.assertIn("expired", r["reason"])
        pump_briefly(100)
        self.assertEqual(self.srv.seen, [])

    def test_confirm_rechecks_the_allowlist(self):
        tok = self.server.reply_choice(self.nid, "yes")["token"]
        (self.dir / "http-actions.toml").write_text("")
        self.ha.load()  # the user tightened the allowlist meanwhile
        r = self.server.confirm_reply(self.nid, tok)
        self.assertEqual(r["status"], "refused")
        self.assertEqual(self.srv.seen, [])


# ---- hot reload --------------------------------------------------------------------------


class HotReloadTests(HttpBase):
    def test_watch_reloads_http_actions(self):
        from notifyd.__main__ import NullDisplay

        rt = Runtime(self.dir / "config.toml", NullDisplay(), Config(), player=NullPlayer())
        reloaded, signals = [], []
        rt.on_http_reloaded = lambda: reloaded.append(1)
        rt.on_rules_reloaded = lambda ok, msg: signals.append((ok, msg))
        n = mk_note(hint_json(self.base))
        self.assertFalse(rt.http_actions.plan(n).allowed)  # missing file: empty
        monitor = rt.watch()
        try:
            (self.dir / "http-actions.toml").write_text(toml_config(self.port))
            pump_until(lambda: rt.http_actions.plan(n).allowed, 10)
            self.assertTrue(reloaded)
            self.assertEqual(signals[-1], (True, ""))
            (self.dir / "http-actions.toml").write_text("this is = = bad")
            pump_until(lambda: not rt.http_actions.plan(n).allowed, 10)  # fails closed
            pump_until(lambda: signals[-1][0] is False, 10)
            self.assertIn("http-actions.toml", signals[-1][1])
            (self.dir / "http-actions.toml").write_text(
                toml_config(self.port, entry="enabled = false")
            )
            pump_until(lambda: rt.http_actions.error is None, 10)
            self.assertFalse(rt.http_actions.plan(n).allowed)
            (self.dir / "http-actions.toml").write_text(toml_config(self.port))
            pump_until(lambda: rt.http_actions.plan(n).allowed, 10)
        finally:
            monitor.cancel()


# ---- bus + CLI against a real daemon -------------------------------------------------------


class DaemonCliTests(HttpBase):
    def start(self, addr):
        return subprocess.Popen(
            [
                str(ROOT / "bin" / "notifyd"),
                "--bus-address", addr,
                "--db", str(self.dir / "t.db"),
                "--config", str(self.dir / "config.toml"),
                "--no-display", "--no-sound", "--no-run",
            ],
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "HOME": str(self.dir)},
            # --no-display also avoids any window; the display variables are removed as well.
        )  # fmt: skip

    def cli(self, addr, *args):
        env = {k: v for k, v in os.environ.items() if k not in ("WAYLAND_DISPLAY", "DISPLAY")}
        return subprocess.run(
            [str(ROOT / "bin" / "notifyd"), "--bus-address", addr, *args],
            capture_output=True, text=True, timeout=20, env=env,
        )  # fmt: skip

    def test_bus_and_cli(self):
        bus = Gio.TestDBus()
        bus.up()
        try:
            addr = bus.get_bus_address()
            (self.dir / "config.toml").write_text("")
            (self.dir / "http-actions.toml").write_text(toml_config(self.port))
            daemon = self.start(addr)
            client = connect(addr)
            try:
                dbus = Caller(client, "org.freedesktop.DBus")

                def has(name):
                    r = dbus.call(
                        "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
                        GLib.Variant("(s)", (name,)), "(b)",
                    )  # fmt: skip
                    return r.unpack()[0]

                pump_until(lambda: has(CTL_IFACE) and has(FDO_IFACE), 10)
                fdo = Caller(client, FDO_IFACE)

                def post(hint, app="Example"):
                    args = GLib.Variant(
                        "(susssasa{sv}i)",
                        (app, 0, "", "Q", "b", [], {"x-nc-actions": GLib.Variant("s", hint)}, -1),
                    )
                    return fdo.call(FDO_PATH, FDO_IFACE, "Notify", args, "(u)").unpack()[0]

                ctl = Caller(client, CTL_IFACE)

                def history():
                    return {h["id"]: h for h in ctl.history()}

                # --reply-choice
                nid = post(hint_json(self.base))
                self.assertTrue(history()[nid]["reply"]["allowed"])
                r = self.cli(addr, "--reply-choice", str(nid), "yes")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(json.loads(r.stdout), {"status": "sent"})
                pump_until(lambda: history()[nid]["reply"]["answered"] is not None, 10)
                self.assertEqual(self.srv.seen[-1]["path"], "/endpoint/reply/thread-22/yes")
                self.assertEqual(history()[nid]["close_reason"], 2)
                # a second reply is refused: exit 1, the reason on stdout
                r = self.cli(addr, "--reply-choice", str(nid), "no")
                self.assertEqual(r.returncode, 1)
                self.assertEqual(json.loads(r.stdout)["reason"], "already answered")
                # --reply-text
                t = post(
                    json.dumps({"text": True, "request": {"url": self.base + "/endpoint/t/{text}"}})
                )
                r = self.cli(addr, "--reply-text", str(t), "hi there")
                self.assertEqual(r.returncode, 0, r.stderr)
                pump_until(lambda: history()[t]["reply"]["answered"] is not None, 10)
                self.assertEqual(self.srv.seen[-1]["path"], "/endpoint/t/hi%20there")
                # not allowed (other app) and unknown id
                o = post(hint_json(self.base), app="Other")
                r = self.cli(addr, "--reply-choice", str(o), "yes")
                self.assertEqual(r.returncode, 1)
                self.assertEqual(json.loads(r.stdout)["status"], "refused")
                r = self.cli(addr, "--reply-choice", "424242", "yes")
                self.assertEqual(r.returncode, 1)
                r = self.cli(addr, "--reply-choice", "nope", "yes")
                self.assertEqual(r.returncode, 1)
                self.assertIn("out of range", r.stderr)
                # the raw bus method
                p = post(hint_json(self.base))
                out = ctl.call(
                    CTL_PATH, CTL_IFACE, "ReplyChoice", GLib.Variant("(us)", (p, "no")), "(s)"
                )
                self.assertEqual(json.loads(out.unpack()[0]), {"status": "sent"})
                pump_until(lambda: history()[p]["reply"]["answered"] is not None, 10)
                # hot reload in the live daemon: tighten the allowlist, then a reply is refused
                (self.dir / "http-actions.toml").write_text("")
                q = post(hint_json(self.base))

                def refused():
                    return (
                        json.loads(
                            ctl.call(
                                CTL_PATH,
                                CTL_IFACE,
                                "ReplyChoice",
                                GLib.Variant("(us)", (q, "yes")),
                                "(s)",
                            ).unpack()[0]
                        )["status"]
                        == "refused"
                    )

                pump_until(refused, 10)
                client.close_sync(None)
            finally:
                daemon.terminate()
                daemon.wait(timeout=10)
                daemon.stderr.close()
        finally:
            bus.down()


if __name__ == "__main__":
    unittest.main()
