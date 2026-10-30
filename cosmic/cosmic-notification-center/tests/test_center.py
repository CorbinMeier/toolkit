import json
import re
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from notifyd.center import (
    MAX_STATE_APPS,
    MAX_STATE_DAYS,
    MAX_STATE_GROUPS,
    CenterBridge,
    CenterState,
    CenterWindow,
    ListQuery,
    _same_page,
    detail_item,
    link_allowed,
    list_item,
    parse_expand,
    parse_list_query,
    preview,
    run_item,
    run_status,
    runs_payload,
    valid_id,
    valid_ids,
)
from notifyd.model import CloseReason, Notification, State
from notifyd.store import Store
from tests.fake_settings import FakeSettings, SettingsError

WEB = Path(__file__).resolve().parent.parent / "web"


def mk(id, summary="s", body="", app="App", at=100.0, state=State.SHOWN, **kw):
    n = Notification.from_notify(id, app, 0, "", summary, body, kw.pop("actions", []), {}, -1, at)
    n.state = state
    for k, v in kw.items():
        setattr(n, k, v)
    return n


class Harness:
    """A bridge on a real in-memory store with every callback recorded."""

    def __init__(self, action_ok=True, settings=None):
        self.store = Store(":memory:")
        self.js: list[str] = []
        self.calls: list[tuple] = []
        self.action_ok = action_ok
        self.bridge = CenterBridge(
            self.store,
            on_invoke_action=lambda i, k: (self.calls.append(("action", i, k)), self.action_ok)[1],
            on_dismiss=lambda i: self.calls.append(("dismiss", i)),
            on_mark_read=lambda ids: self.calls.append(("mark_read", ids)),
            on_delete=lambda ids: self.calls.append(("delete", ids)),
            on_clear=lambda: self.calls.append(("clear",)),
            run_js=self.js.append,
            copy_text=lambda t: self.calls.append(("copy", t)),
            open_link=lambda h: self.calls.append(("link", h)),
            settings=settings,
        )

    def last_reply(self):
        m = re.fullmatch(r"window\.nc\.reply\((\d+), (.*)\)", self.js[-1], re.S)
        assert m, self.js[-1]
        return int(m[1]), json.loads(m[2])


class Validation(unittest.TestCase):
    def test_ids_must_be_real_ints(self):
        for bad in (True, "1", 1.0, -1, 2**32, None, [1]):
            self.assertFalse(valid_id(bad), bad)
        self.assertTrue(valid_id(0))
        self.assertTrue(valid_id(2**32 - 1))
        self.assertEqual(valid_ids([1, 2]), [1, 2])
        self.assertEqual(valid_ids([]), [])
        for bad in ("1", [1, "2"], [True], [1.5], None, [-3], list(range(10001))):
            self.assertIsNone(valid_ids(bad), bad)

    def test_list_query_defaults_and_clamps(self):
        self.assertEqual(parse_list_query({}), ListQuery())
        q = parse_list_query(
            {"q": "x", "offset": -5, "limit": 99999, "app": "A", "state": "dnd", "tag": "t"}
        )
        self.assertEqual((q.offset, q.limit, q.app, q.state, q.tag), (0, 200, "A", "dnd", "t"))
        self.assertEqual(parse_list_query({"limit": 0}).limit, 1)
        self.assertEqual(parse_list_query({"app": None}).app, "")

    def test_list_query_rejects_bad_types(self):
        for bad in (
            {"q": 5},
            {"offset": "0"},
            {"limit": True},
            {"state": "bogus"},
            {"unread_only": "yes"},
            {"tag": ["a"]},
            {"q": "x" * 501},
        ):
            self.assertIsNone(parse_list_query(bad), bad)

    def test_links_allow_list(self):
        for ok in ("http://a.b", "https://a.b/c?d=1", "mailto:x@y.z"):
            self.assertEqual(link_allowed(ok), ok)
        for bad in (
            "javascript:alert(1)",
            "java\tscript:alert(1)",
            "file:///etc/passwd",
            "data:text/html,x",
            "ftp://a",
            "/relative",
            "",
            None,
            5,
            "https://a/" + "x" * 5000,
        ):
            self.assertIsNone(link_allowed(bad), bad)

    def test_same_page_ignores_fragment_only(self):
        page = "file:///w/center.html"
        self.assertTrue(_same_page(page, page))
        self.assertTrue(_same_page(page + "#/n/3", page))
        self.assertFalse(_same_page("file:///w/other.html", page))
        self.assertFalse(_same_page("https://evil.example/", page))


class Payloads(unittest.TestCase):
    def test_preview_first_nonempty_line_capped(self):
        self.assertEqual(preview([{"text": "\n  \nfirst\nsecond"}]), "first")
        self.assertEqual(preview([]), "")
        self.assertEqual(len(preview([{"text": "x" * 1000}])), 240)

    def test_list_item_has_preview_not_body(self):
        n = mk(1, "Sum", "<b>Bold</b> line\nmore", tags=["ci"], classes=["c"])
        item = list_item(n, "file:///i.png")
        self.assertEqual(item["preview"], "Bold line")
        self.assertNotIn("body", item)
        self.assertEqual(
            (item["unread"], item["open"], item["state"], item["urgency"]),
            (True, True, "shown", "normal"),
        )
        self.assertEqual(
            (item["tags"], item["classes"], item["icon"]), (["ci"], ["c"], "file:///i.png")
        )
        json.dumps(item)

    def test_unread_and_open_follow_state(self):
        self.assertFalse(list_item(mk(1, state=State.BLOCKED), "")["unread"])
        self.assertTrue(list_item(mk(1, state=State.DND), "")["unread"])
        self.assertFalse(list_item(mk(1, state=State.DND), "")["open"])
        self.assertFalse(list_item(mk(1, read_at=5.0), "")["unread"])
        self.assertFalse(list_item(mk(1, closed_at=5.0), "")["open"])

    def test_detail_body_is_sanitized_runs_with_links(self):
        body = (
            '<script>alert(1)</script><b>hi</b> <a href="https://x.test/a">x</a> '
            '<a href="javascript:evil()">bad</a> <a href="https://x.test/a">again</a><img src="x">'
        )
        n = mk(
            7,
            body=body,
            actions=["default", "Open", "ack", "Ack"],
            matched_rules=["r1"],
            sound="/s.ogg",
            closed_at=9.0,
            close_reason=CloseReason.EXPIRED,
        )
        n.hints = {"urgency": 1, "x-nc-url": "https://u", "nested": {"a": [1, 2]}}
        d = detail_item(n, "")
        text = "".join(r["text"] for r in d["body"])
        self.assertIn("alert(1)", text)  # kept as inert text
        self.assertNotIn("<script", text)
        self.assertFalse(any("javascript" in r.get("href", "") for r in d["body"]))
        self.assertEqual(d["links"], [{"href": "https://x.test/a", "text": "x"}])
        self.assertEqual(
            d["actions"], [{"key": "default", "label": "Open"}, {"key": "ack", "label": "Ack"}]
        )
        self.assertEqual(d["hints"]["nested"], {"a": [1, 2]})
        self.assertEqual((d["matched_rules"], d["sound"]), (["r1"], "/s.ogg"))
        self.assertEqual((d["closed_at"], d["close_reason"], d["open"]), (9.0, "expired", False))
        self.assertIsNone(d["read_at"])
        json.dumps(d)


class ListAndGet(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        for i in range(1, 8):
            self.h.store.record(
                mk(i, f"sum {i}", f"body {i}", app="A" if i % 2 else "B", at=float(i))
            )

    def ask(self, **msg):
        self.h.bridge.handle(msg)
        return self.h.last_reply()

    def test_list_pages_and_has_more(self):
        req, res = self.ask(type="list", req=4, limit=3)
        self.assertEqual(req, 4)
        self.assertEqual([i["id"] for i in res["items"]], [7, 6, 5])
        self.assertTrue(res["has_more"])
        _, res = self.ask(type="list", req=5, limit=3, offset=6)
        self.assertEqual([i["id"] for i in res["items"]], [1])
        self.assertFalse(res["has_more"])

    def test_list_filters_and_search(self):
        _, res = self.ask(type="list", req=1, app="A", q="body")
        self.assertEqual([i["id"] for i in res["items"]], [7, 5, 3, 1])
        _, res = self.ask(type="list", req=2, q="sum 6")
        self.assertEqual([i["id"] for i in res["items"]], [6])
        self.h.store.mark_read([7, 5], 50.0)
        _, res = self.ask(type="list", req=3, unread_only=True, app="A")
        self.assertEqual([i["id"] for i in res["items"]], [3, 1])

    def test_bad_list_query_answers_error(self):
        _, res = self.ask(type="list", req=1, state="nope")
        self.assertEqual(res, {"error": "bad request"})

    def test_get_found_and_missing(self):
        _, res = self.ask(type="get", req=1, id=3)
        self.assertEqual(res["notification"]["body"], [{"text": "body 3"}])
        _, res = self.ask(type="get", req=2, id=999)
        self.assertEqual(res, {"notification": None})
        _, res = self.ask(type="get", req=3, id="3")
        self.assertEqual(res, {"error": "bad request"})

    def test_facets(self):
        self.h.store.record(mk(9, tags=["red"]))
        _, res = self.ask(type="facets", req=1)
        self.assertEqual(res, {"apps": ["A", "App", "B"], "tags": ["red"]})

    def test_request_without_int_req_is_dropped(self):
        for req in (None, "1", True, -1):
            self.h.bridge.handle({"type": "facets", "req": req})
        self.assertEqual(self.h.js, [])

    def test_store_failure_becomes_error_reply(self):
        self.h.store.close()
        _, res = self.ask(type="facets", req=1)
        self.assertIn("error", res)


class Routing(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.h.store.record(mk(1, "Hello", "<b>Body</b> text", actions=["ack", "Ack"]))
        self.h.store.record(mk(2, "Read", read_at=3.0))
        self.h.store.record(mk(3, "Blocked", state=State.BLOCKED))

    def send(self, **msg):
        self.h.bridge.handle(msg)

    def test_action_ok_and_unavailable(self):
        self.send(type="action", req=1, id=1, key="ack")
        self.assertEqual(self.h.calls, [("action", 1, "ack")])
        self.assertEqual(self.h.last_reply(), (1, {"ok": True}))
        self.h.action_ok = False
        self.send(type="action", req=2, id=1, key="ack")
        self.assertEqual(self.h.last_reply(), (2, {"ok": False}))

    def test_action_rejects_unknown_key_id_or_types(self):
        n = len(self.h.calls)
        self.send(type="action", req=1, id=1, key="nope")
        self.assertEqual(self.h.last_reply()[1], {"ok": False})
        self.send(type="action", req=2, id=99, key="ack")
        self.assertEqual(self.h.last_reply()[1], {"ok": False})
        self.send(type="action", req=3, id=1, key=5)
        self.assertEqual(self.h.last_reply()[1], {"ok": False})
        self.send(type="action", req=4, id="1", key="ack")
        self.assertEqual(self.h.last_reply()[1], {"error": "bad request"})
        self.assertEqual(len(self.h.calls), n)

    def test_dismiss(self):
        self.send(type="dismiss", req=1, id=1)
        self.assertEqual(self.h.calls, [("dismiss", 1)])
        self.assertEqual(self.h.last_reply(), (1, {"ok": True}))
        self.send(type="dismiss", req=2, id=True)
        self.assertEqual(self.h.calls, [("dismiss", 1)])

    def test_mark_read_and_delete_validate_ids(self):
        self.send(type="mark_read", ids=[1, 2])
        self.send(type="delete", ids=[3])
        self.send(type="mark_read", ids=["1"])
        self.send(type="delete", ids=[True])
        self.send(type="delete", ids=[])
        self.send(type="delete", ids=7)
        self.send(type="delete")
        self.assertEqual(self.h.calls, [("mark_read", [1, 2]), ("delete", [3])])

    def test_mark_all_read_uses_unread_ids_only(self):
        self.send(type="mark_all_read")
        self.assertEqual(self.h.calls, [("mark_read", [1])])  # 2 is read, 3 is blocked
        self.h.store.mark_read([1], 1.0)
        self.send(type="mark_all_read")
        self.assertEqual(len(self.h.calls), 1)

    def test_clear(self):
        self.send(type="clear")
        self.assertEqual(self.h.calls, [("clear",)])

    def test_copy_uses_store_text_not_page_text(self):
        self.send(type="copy", id=1, what="body")
        self.send(type="copy", id=1, what="summary", text="injected")
        self.send(type="copy", id=1, what="other")
        self.send(type="copy", id=99, what="body")
        self.send(type="copy", id="1", what="body")
        self.assertEqual(self.h.calls, [("copy", "Body text"), ("copy", "Hello")])

    def test_links_are_allow_listed(self):
        self.send(type="link", href="https://example.com/x")
        self.send(type="link", href="mailto:a@b.c")
        self.send(type="link", href="javascript:alert(1)")
        self.send(type="link", href="file:///etc/passwd")
        self.send(type="link", href=None)
        self.send(type="link")
        self.assertEqual(
            self.h.calls, [("link", "https://example.com/x"), ("link", "mailto:a@b.c")]
        )

    def test_unknown_and_malformed_messages_are_ignored(self):
        for msg in (
            None,
            [],
            "x",
            5,
            {},
            {"type": None},
            {"type": "eval", "code": "1"},
            {"type": ["list"]},
        ):
            self.h.bridge.handle(msg)
        self.assertEqual((self.h.calls, self.h.js), ([], []))

    def test_callback_exception_does_not_escape(self):
        def boom(ids):
            raise RuntimeError("x")

        self.h.bridge.on_delete = boom
        with self.assertLogs("notifyd.center", "ERROR"):
            self.send(type="delete", ids=[1])


class WindowWithoutDisplay(unittest.TestCase):
    def make(self):
        self.calls = []
        return CenterWindow(
            Store(":memory:"),
            WEB,
            on_invoke_action=lambda i, k: True,
            on_dismiss=lambda i: None,
            on_mark_read=lambda ids: None,
            on_delete=lambda ids: None,
            on_clear=lambda: None,
            state_path=Path(tempfile.mkdtemp()) / "center.json",
        )

    def test_lazy_nothing_built_before_show(self):
        w = self.make()
        self.assertFalse(w.visible)
        w.notify_added(mk(1))  # never opened: ignored
        w.refresh()
        w.hide()
        self.assertIsNone(w._window)
        self.assertEqual(w._pending_js, [])

    def test_user_css_is_queued_until_the_page_loads(self):
        w = self.make()
        w.set_user_css("a{b:c}")
        w.set_user_css("d{e:f}")
        self.assertEqual(w._user_css, "d{e:f}")
        self.assertEqual(len(w._pending_js), 2)
        self.assertTrue(all("setUserCss" in s for s in w._pending_js))

    def test_bridge_reply_is_queued_as_js(self):
        w = self.make()
        w.bridge.handle({"type": "facets", "req": 3})
        self.assertEqual(len(w._pending_js), 1)
        self.assertTrue(w._pending_js[0].startswith("window.nc.reply(3, "))


class StaticPage(unittest.TestCase):
    def test_html_loads_default_css_first_with_strict_csp(self):
        html = (WEB / "center.html").read_text()
        self.assertLess(html.index("default.css"), html.index("center.css"))
        self.assertIn("default-src 'none'", html)
        self.assertIn("script-src 'self'", html)
        self.assertIn("img-src file:", html)
        self.assertIn('class="center"', html)
        for slot in ('id="d-buttons"', 'id="d-runlog"', 'id="d-runlog-refresh"'):
            self.assertIn(slot, html)  # the Buttons and Run log sections

    def test_js_never_uses_html_string_setters_or_confirm(self):
        js = (WEB / "center.js").read_text()
        for banned in (
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "document.write",
            "eval(",
            "confirm(",
        ):
            self.assertNotIn(banned, js)
        self.assertIn("window.nc", js)

    def test_css_documents_hooks_and_uses_shared_variables(self):
        css = (WEB / "center.css").read_text()
        for hook in (".center", ".row", ".detail", "data-urgency", "--nc-accent", "--nc-bg"):
            self.assertIn(hook, css)
        shared = set(re.findall(r"--nc-[a-z-]+", (WEB / "default.css").read_text()))
        used = set(re.findall(r"var\((--nc-[a-z-]+)", css))
        local = {v for v in used if v.startswith("--nc-center")}
        self.assertEqual(used - shared - local, set())


BUTTONS = [
    {"id": "b0", "label": "Open log", "run": ["/bin/secret-cmd", "--token=hunter2"], "close": True},
    {"id": "b1", "label": "Later", "builtin": "snooze", "minutes": 15, "close": True},
    {"id": "b2", "label": "Copy", "builtin": "copy-body", "close": False},
]

RUNS = [  # oldest first, as Store.runs_for returns them
    {
        "id": 1,
        "notification_id": 5,
        "rule": "log example",
        "trigger": "rule",
        "button_label": None,
        "cmd": ["/bin/secret-cmd", "--token=hunter2"],
        "started_at": 10.0,
        "finished_at": 10.2,
        "exit_code": 0,
        "signal": None,
        "duration_ms": 200,
        "output_tail": "ok\n",
        "skipped_reason": None,
    },
    {
        "id": 2,
        "notification_id": 5,
        "rule": "log example",
        "trigger": "button",
        "button_label": "Open log",
        "cmd": "echo $TOKEN",
        "started_at": 20.0,
        "finished_at": 20.1,
        "exit_code": 3,
        "signal": None,
        "duration_ms": 100,
        "output_tail": "boom",
        "skipped_reason": None,
    },
    {
        "id": 3,
        "notification_id": 5,
        "rule": "slow",
        "trigger": "rule",
        "button_label": None,
        "cmd": "sleep 99",
        "started_at": 30.0,
        "finished_at": 60.0,
        "exit_code": None,
        "signal": 15,
        "duration_ms": 30000,
        "output_tail": "\n[timed out after 30s]",
        "skipped_reason": None,
    },
    {
        "id": 4,
        "notification_id": 5,
        "rule": "slow",
        "trigger": "rule",
        "button_label": None,
        "cmd": "sleep 99",
        "started_at": 40.0,
        "finished_at": 40.0,
        "exit_code": None,
        "signal": None,
        "duration_ms": None,
        "output_tail": None,
        "skipped_reason": "cooldown",
    },
    {
        "id": 5,
        "notification_id": 5,
        "rule": "x",
        "trigger": "rule",
        "button_label": None,
        "cmd": "x",
        "started_at": 50.0,
        "finished_at": 51.0,
        "exit_code": None,
        "signal": 9,
        "duration_ms": 1000,
        "output_tail": "",
        "skipped_reason": None,
    },
]


class RunStatus(unittest.TestCase):
    def test_statuses(self):
        got = [run_status(r) for r in RUNS]
        self.assertEqual(
            got,
            [
                ("exit 0 \u2713", "ok"),
                ("exit 3", "failed"),
                ("timed out", "timeout"),
                ("skipped: cooldown", "skipped"),
                ("signal SIGKILL", "signal"),
            ],
        )

    def test_running_and_odd_signal(self):
        self.assertEqual(run_status({"started_at": 1.0}), ("running", "running"))
        self.assertEqual(run_status({"signal": 999}), ("signal 999", "signal"))

    def test_titles(self):
        items = {r["id"]: run_item(r) for r in RUNS}
        self.assertEqual(items[1]["title"], "log example")
        self.assertEqual(items[2]["title"], "button: Open log")

    def test_payload_is_newest_first_and_never_has_cmd(self):
        out = runs_payload(RUNS)
        self.assertEqual([r["id"] for r in out], [5, 4, 3, 2, 1])
        text = json.dumps(out)
        for secret in ("cmd", "secret-cmd", "hunter2", "$TOKEN", "sleep 99"):
            self.assertNotIn(secret, text)
        self.assertNotIn("notification_id", out[0])

    def test_tail_is_capped(self):
        r = dict(RUNS[0], output_tail="x" * 100000)
        self.assertEqual(len(run_item(r)["output_tail"]), 8192)


class RuleButtons(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.h.store.record(mk(5, "n", buttons=[dict(b) for b in BUTTONS]))
        self.h.store.record(mk(6, "plain"))
        self.h.bridge.on_button = lambda i, b: self.h.calls.append(("button", i, b))
        self.h.bridge.get_runs = lambda i: list(RUNS) if i == 5 else []

    def ask(self, msg):
        self.h.bridge.handle(dict(msg, req=3))
        return self.h.last_reply()[1]

    def test_details_payload_with_buttons_and_runs(self):
        res = self.ask({"type": "get", "id": 5})
        self.assertTrue(res["buttons_enabled"])
        n = res["notification"]
        self.assertEqual([b["label"] for b in n["buttons"]], ["Open log", "Later", "Copy"])
        self.assertEqual(n["buttons"][1]["minutes"], 15)
        self.assertEqual([r["id"] for r in res["runs"]], [5, 4, 3, 2, 1])
        text = json.dumps(res)
        for secret in ("secret-cmd", "hunter2", "$TOKEN", "sleep 99", '"cmd"'):
            self.assertNotIn(secret, text)

    def test_payload_without_buttons_or_hooks(self):
        self.assertEqual(self.ask({"type": "get", "id": 6})["notification"]["buttons"], [])
        self.h.bridge.on_button = None
        self.h.bridge.get_runs = None
        res = self.ask({"type": "get", "id": 5})
        self.assertFalse(res["buttons_enabled"])
        self.assertEqual(res["runs"], [])

    def test_runs_request(self):
        res = self.ask({"type": "runs", "id": 5})
        self.assertEqual([r["id"] for r in res["runs"]], [5, 4, 3, 2, 1])
        self.assertEqual(self.ask({"type": "runs", "id": 6}), {"runs": []})
        self.assertEqual(self.ask({"type": "runs", "id": "5"}), {"error": "bad request"})
        self.assertNotIn("secret-cmd", json.dumps(res))

    def test_get_runs_failure_is_an_empty_log(self):
        def boom(i):
            raise RuntimeError("db gone")

        self.h.bridge.get_runs = boom
        with self.assertLogs("notifyd.center", "ERROR"):
            self.assertEqual(self.ask({"type": "runs", "id": 5}), {"runs": []})

    def test_press_routes(self):
        self.h.bridge.handle({"type": "button", "id": 5, "button": "b1"})
        self.assertEqual(self.h.calls, [("button", 5, "b1")])

    def test_rejects_bad_presses(self):
        for msg in (
            {"type": "button", "id": 5, "button": "b9"},
            {"type": "button", "id": 5, "button": ["b0"]},
            {"type": "button", "id": "5", "button": "b0"},
            {"type": "button", "id": True, "button": "b0"},
            {"type": "button", "id": 6, "button": "b0"},
            {"type": "button", "id": 99, "button": "b0"},
            {"type": "button", "id": 5},
        ):
            self.h.bridge.handle(msg)
        self.assertEqual(self.h.calls, [])

    def test_missing_on_button_ignores_presses(self):
        self.h.bridge.on_button = None
        self.h.bridge.handle({"type": "button", "id": 5, "button": "b0"})
        self.assertEqual(self.h.calls, [])

    def test_window_attributes_and_kwargs(self):
        def make(**kw):
            return CenterWindow(
                Store(":memory:"),
                WEB,
                on_invoke_action=lambda i, k: True,
                on_dismiss=lambda i: None,
                on_mark_read=lambda ids: None,
                on_delete=lambda ids: None,
                on_clear=lambda: None,
                state_path=Path(tempfile.mkdtemp()) / "center.json",
                **kw,
            )

        w = make()
        self.assertIsNone(w.on_button)
        self.assertIsNone(w.get_runs)
        f = lambda *a: None  # noqa: E731
        w.on_button, w.get_runs = f, f
        self.assertIs(w.bridge.on_button, f)
        self.assertIs(w.bridge.get_runs, f)
        w2 = make(on_button=f, get_runs=f)
        self.assertIs(w2.on_button, f)
        self.assertIs(w2.get_runs, f)

    def test_page_builds_with_text_only(self):
        js = (WEB / "center.js").read_text()
        self.assertIn("nc-runlog", js)
        self.assertNotIn("innerHTML", js)


DAY = "2026-10-26"


class SavedState(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "sub" / "center.json"

    def test_defaults_without_a_file(self):
        s = CenterState(self.path)
        self.assertEqual(
            s.payload(), {"group_by": "day", "days": {}, "groups": {}, "apps": {}}
        )  # no overrides: the page applies today open, older days closed, app groups open
        self.assertFalse(self.path.exists())

    def test_default_path_is_the_state_dir(self):
        from notifyd.center import DEFAULT_STATE_PATH

        self.assertEqual(DEFAULT_STATE_PATH.name, "center.json")
        self.assertEqual(DEFAULT_STATE_PATH.parent.name, "net.corbinmeier.notifications")

    def test_round_trip_is_atomic(self):
        s = CenterState(self.path)
        exp = {"days": {DAY: False}, "groups": {DAY: {"A": False}}, "apps": {"B": False}}
        s.set_expand(exp)
        s.set_group_by("app")
        again = CenterState(self.path)
        self.assertEqual(again.group_by, "app")
        self.assertEqual(again.expand, exp)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["center.json"])

    def test_corrupt_or_wrong_files_fall_back(self):
        self.path.parent.mkdir(parents=True)
        for text in (
            "",
            "{not json",
            "[]",
            "null",
            '{"group_by": "nope", "days": [1]}',
            '{"days": {"not-a-day": true}}',
            f'{{"days": {{"{DAY}": "yes"}}}}',
            f'{{"groups": {{"{DAY}": {{"A": 1}}}}}}',
        ):
            self.path.write_text(text)
            s = CenterState(self.path)
            self.assertEqual(
                s.payload(), {"group_by": "day", "days": {}, "groups": {}, "apps": {}}, text
            )
        self.path.write_bytes(b"\xff\xfe")
        self.assertEqual(CenterState(self.path).group_by, "day")

    def test_a_valid_group_by_survives_bad_overrides(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"group_by": "app", "days": {"x": true}}')
        s = CenterState(self.path)
        self.assertEqual((s.group_by, s.expand["days"]), ("app", {}))

    def test_unwritable_location_is_logged_not_raised(self):
        blocker = Path(self._tmp.name) / "file"
        blocker.write_text("")
        s = CenterState(blocker / "center.json")  # the parent is a file
        with self.assertLogs("notifyd.center", "WARNING"):
            s.set_group_by("app")
        self.assertEqual(s.group_by, "app")

    def test_in_memory_state_writes_nothing(self):
        s = CenterState(None)
        s.set_group_by("app")
        self.assertEqual(s.group_by, "app")

    def test_parse_expand_validates_keys_and_sizes(self):
        good = {"days": {DAY: True}, "groups": {DAY: {"A": False}}, "apps": {"x" * 500: False}}
        self.assertEqual(parse_expand(good), good)
        self.assertEqual(parse_expand({}), {"days": {}, "groups": {}, "apps": {}})
        bad = [
            {"days": {"yesterday": True}},
            {"days": {DAY: 1}},
            {"days": {DAY: None}},
            {"days": [DAY]},
            {"apps": {"x" * 501: True}},
            {"apps": {"A": "no"}},
            {"groups": {"nope": {"A": True}}},
            {"groups": {DAY: {"A": 1}}},
            {"groups": {DAY: ["A"]}},
            {"groups": [DAY]},
            {"days": "x"},
        ]
        for msg in bad:
            self.assertIsNone(parse_expand(msg), msg)

    def test_parse_expand_caps(self):
        start = date(2000, 1, 1)
        days = {(start + timedelta(i)).isoformat(): True for i in range(MAX_STATE_DAYS + 1)}
        self.assertGreater(len(days), MAX_STATE_DAYS)
        self.assertIsNone(parse_expand({"days": days}))
        self.assertIsNone(parse_expand({"apps": {str(i): True for i in range(MAX_STATE_APPS + 1)}}))
        per_day = MAX_STATE_GROUPS // 2 + 1
        two = {
            "2026-10-25": {str(i): True for i in range(per_day)},
            DAY: {str(i): True for i in range(per_day)},
        }
        self.assertIsNone(parse_expand({"groups": two}))  # over the total across days


class BridgeState(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "center.json"
        self.h.bridge.state = CenterState(self.path)

    def test_query_returns_the_defaults(self):
        self.h.bridge.handle({"type": "center_state", "req": 3})
        self.assertEqual(
            self.h.last_reply(),
            (3, {"group_by": "day", "days": {}, "groups": {}, "apps": {}}),
        )

    def test_set_expand_is_saved_and_queried_back(self):
        exp = {"days": {DAY: False}, "groups": {DAY: {"A": False}}, "apps": {"A": False}}
        self.h.bridge.handle({"type": "set_expand", **exp})
        self.assertEqual(CenterState(self.path).expand, exp)
        self.h.bridge.handle({"type": "center_state", "req": 1})
        self.assertEqual(self.h.last_reply()[1]["days"], {DAY: False})

    def test_set_group_by_is_saved(self):
        self.h.bridge.handle({"type": "set_group_by", "mode": "app"})
        self.assertEqual(CenterState(self.path).group_by, "app")
        self.h.bridge.handle({"type": "center_state", "req": 1})
        self.assertEqual(self.h.last_reply()[1]["group_by"], "app")

    def test_bad_messages_change_nothing(self):
        self.h.bridge.handle({"type": "set_expand", "days": {DAY: False}})
        before = self.path.read_text()
        for msg in (
            {"type": "set_expand", "days": {"x": True}},
            {"type": "set_expand", "apps": {"x" * 501: True}},
            {"type": "set_expand", "groups": {DAY: {"A": "yes"}}},
            {"type": "set_group_by", "mode": "week"},
            {"type": "set_group_by", "mode": None},
            {"type": "set_group_by"},
        ):
            with self.assertNoLogs("notifyd.center", "ERROR"):
                self.h.bridge.handle(msg)
        self.assertEqual(self.path.read_text(), before)
        self.assertEqual(self.h.bridge.state.group_by, "day")

    def test_collapse_is_a_pure_view_state(self):
        for i in range(5):
            self.h.store.record(mk(i + 1, app="A", at=100.0 + i))
        self.h.bridge.handle({"type": "list", "req": 1, "limit": 2})
        first = self.h.last_reply()[1]
        self.h.bridge.handle({"type": "set_expand", "days": {DAY: False}})
        self.h.bridge.handle({"type": "set_group_by", "mode": "app"})
        self.h.bridge.handle({"type": "list", "req": 2, "limit": 2})
        self.assertEqual(self.h.last_reply()[1], first)
        self.h.bridge.handle({"type": "list", "req": 3, "offset": 2, "limit": 2})
        self.assertEqual([n["id"] for n in self.h.last_reply()[1]["items"]], [3, 2])

    def test_page_markup_has_the_hooks(self):
        html = (WEB / "center.html").read_text()
        for needle in ('id="btn-toggle-all"', 'id="f-group"', "center-toggle-all"):
            self.assertIn(needle, html)
        css = (WEB / "center.css").read_text()
        for needle in (".center-day", ".center-group", ".collapsed", "prefers-reduced-motion"):
            self.assertIn(needle, css)
        self.assertIn("Content-Security-Policy", html)
        self.assertIn("script-src 'self'", html)


class SettingsBridge(unittest.TestCase):
    """The settings_* requests: validation, routing to the service, error passthrough."""

    def setUp(self):
        self.fake = FakeSettings()
        self.h = Harness(settings=self.fake)
        self.req = 0

    def ask(self, **msg):
        self.req += 1
        self.h.bridge.handle({"req": self.req, **msg})
        req, data = self.h.last_reply()
        self.assertEqual(req, self.req)
        return data

    def test_get_returns_the_service_payload(self):
        res = self.ask(type="settings_get")
        self.assertTrue(res["ok"])
        s = res["settings"]
        self.assertEqual(s["theme"]["active_preset"], "default")
        self.assertIn("toasts", s["config_meta"])
        self.assertTrue(s["sound_themes"] and s["fonts"])

    def test_unavailable_when_settings_is_none(self):
        h = Harness()
        self.assertIsNone(h.bridge.settings)
        for i, msg in enumerate(
            (
                {"type": "settings_get"},
                {"type": "settings_save_theme", "values": {}},
                {"type": "settings_preset", "name": "default"},
                {"type": "settings_save_style", "text": ""},
                {"type": "settings_save_config", "section": "toasts", "values": {}},
                {"type": "settings_preview_sound", "spec": "", "volume": 50},
                {"type": "settings_send_test", "kind": "normal"},
            ),
            1,
        ):
            h.bridge.handle({"req": i, **msg})
            req, res = h.last_reply()
            self.assertEqual(req, i)
            self.assertFalse(res["ok"], msg)
            self.assertTrue(res["unavailable"], msg)
            self.assertIn("unavailable", res["errors"][""].lower())

    def test_window_exposes_the_settings_attribute(self):
        win = CenterWindow(
            Store(":memory:"),
            WEB,
            on_invoke_action=lambda *a: True,
            on_dismiss=lambda i: None,
            on_mark_read=lambda ids: None,
            on_delete=lambda ids: None,
            on_clear=lambda: None,
        )
        self.assertIsNone(win.settings)
        win.settings = self.fake
        self.assertIs(win.bridge.settings, self.fake)

    def test_save_theme_routes_and_validates_shape(self):
        res = self.ask(type="settings_save_theme", values={"--nc-radius": "4px"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["theme"]["values"], {"--nc-radius": "4px"})
        self.assertIn(("save_theme", {"--nc-radius": "4px"}), self.fake.calls)
        n = len(self.fake.calls)
        for bad in (
            None,
            [],
            "x",
            {"--nc-radius": 4},
            {"--nc-radius": None},
            {"--nc-radius": "x" * 513},
            {"--nc-radius": "a\0b"},
            {"bad key!": "1px"},
            {1: "1px"},
            {f"--nc-v{i}": "1px" for i in range(201)},
        ):
            res = self.ask(type="settings_save_theme", values=bad)
            self.assertFalse(res["ok"], bad)
        self.assertEqual(len(self.fake.calls), n)

    def test_theme_errors_pass_through_per_field(self):
        res = self.ask(type="settings_save_theme", values={"--nc-radius": "huge"})
        self.assertFalse(res["ok"])
        self.assertEqual(list(res["errors"]), ["--nc-radius"])
        self.assertIn("not a valid", res["errors"]["--nc-radius"])

    def test_preset_is_a_preview_only(self):
        res = self.ask(type="settings_preset", name="crt-green")
        self.assertTrue(res["ok"])
        self.assertEqual(res["values"]["--nc-bg"], "#020a05")
        self.assertEqual(self.fake.theme_values, {})
        self.assertFalse(self.ask(type="settings_preset", name="nope")["ok"])
        for bad in (None, 3, "x" * 65, "../x", ""):
            res = self.ask(type="settings_preset", name=bad)
            self.assertEqual(res["errors"], {"": "bad request"}, bad)

    def test_style_size_cap_is_in_bytes(self):
        self.assertTrue(self.ask(type="settings_save_style", text="a{}")["ok"])
        self.assertEqual(self.fake.style_css, "a{}")
        self.assertTrue(self.ask(type="settings_save_style", text="x" * (256 * 1024))["ok"])
        n = len(self.fake.calls)
        for text in ("x" * (256 * 1024 + 1), "\u00e9" * (128 * 1024 + 1)):
            res = self.ask(type="settings_save_style", text=text)
            self.assertFalse(res["ok"])
            self.assertIn("style", res["errors"])
        for bad in (None, 5, ["a"]):
            self.assertFalse(self.ask(type="settings_save_style", text=bad)["ok"])
        self.assertEqual(len(self.fake.calls), n)
        res = self.ask(type="settings_save_style", text="@import 'x';")
        self.assertEqual(res["errors"], {"style": "@import is not allowed"})

    def test_save_config_validates_section_and_values(self):
        res = self.ask(
            type="settings_save_config",
            section="toasts",
            values={"anchor": "top-left", "margin": 4},
        )
        self.assertEqual(
            res, {"ok": True, "restart_required": ["anchor", "margin"], "restart_command": mock.ANY}
        )
        res = self.ask(
            type="settings_save_config",
            section="toasts",
            values={"max_visible": 3, "respect_app_timeout": False, "timeout_low": None},
        )
        self.assertEqual(res, {"ok": True, "restart_required": [], "restart_command": mock.ANY})
        n = len(self.fake.calls)
        for section, values in (
            ("rules", {"a": 1}),
            ("../x", {}),
            (None, {}),
            (["toasts"], {}),
            ("toasts", None),
            ("toasts", [1]),
            ("toasts", {"margin": [1]}),
            ("toasts", {"margin": {"a": 1}}),
            ("toasts", {"anchor": "x" * 513}),
            ("toasts", {"bad key": 1}),
            ("toasts", {"margin": float("nan")}),
            ("toasts", {f"k{i}": 1 for i in range(201)}),
        ):
            res = self.ask(type="settings_save_config", section=section, values=values)
            self.assertFalse(res["ok"], (section, values))
        self.assertEqual(len(self.fake.calls), n)

    def test_config_errors_pass_through(self):
        res = self.ask(type="settings_save_config", section="history", values={"retention_days": 0})
        self.assertFalse(res["ok"])
        self.assertEqual(list(res["errors"]), ["retention_days"])

    def test_config_known_sections(self):
        for section in ("toasts", "shade", "history", "sound", "windows"):
            self.assertTrue(
                self.ask(type="settings_save_config", section=section, values={})["ok"], section
            )

    def test_preview_sound(self):
        res = self.ask(type="settings_preview_sound", spec="bell", volume=60)
        self.assertEqual(res, {"ok": True, "resolved": "bell", "error": ""})
        self.assertEqual(self.fake.calls[-1], ("preview_sound", "bell", 60))
        res = self.ask(type="settings_preview_sound", spec="/nope/x.wav", volume=100)
        self.assertFalse(res["ok"])
        self.assertIn("no such file", res["error"])
        res = self.ask(type="settings_preview_sound", spec="")
        self.assertTrue(res["ok"])
        n = len(self.fake.calls)
        for spec, vol in (
            ("x" * 513, 50),
            (None, 50),
            (5, 50),
            ("a", 101),
            ("a", -1),
            ("a", True),
            ("a", 1.5),
            ("a", "9"),
        ):
            res = self.ask(type="settings_preview_sound", spec=spec, volume=vol)
            self.assertFalse(res["ok"], (spec, vol))
        self.assertEqual(len(self.fake.calls), n)

    def test_send_test_kinds(self):
        for kind in ("normal", "critical", "buttons"):
            self.assertEqual(
                self.ask(type="settings_send_test", kind=kind), {"ok": True, "id": 4242}
            )
        n = len(self.fake.calls)
        for bad in ("urgent", "", None, 1, ["normal"]):
            self.assertFalse(self.ask(type="settings_send_test", kind=bad)["ok"], bad)
        self.assertEqual(len(self.fake.calls), n)

    def test_service_errors_pass_through_and_plain_ones_are_general(self):
        self.fake.fail_with = SettingsError({"--nc-bg": "bad", "": "disk full"})
        res = self.ask(type="settings_save_theme", values={"--nc-bg": "#fff"})
        self.assertEqual(res, {"ok": False, "errors": {"--nc-bg": "bad", "": "disk full"}})
        self.fake.fail_with = OSError("read-only file system")
        with self.assertLogs("notifyd.center", "ERROR"):
            res = self.ask(type="settings_save_style", text="a{}")
        self.assertEqual(res, {"ok": False, "errors": {"": "read-only file system"}})

    def test_a_request_without_a_valid_req_is_dropped(self):
        before = len(self.h.js)
        for req in (None, "1", -1, True):
            self.h.bridge.handle({"type": "settings_get", "req": req})
        self.h.bridge.handle({"type": "settings_get"})
        self.assertEqual(len(self.h.js), before)
        self.assertEqual(self.fake.calls, [])

    def test_the_page_wires_settings(self):
        html = (WEB / "center.html").read_text()
        for needle in (
            'id="btn-settings"',
            'id="view-settings"',
            'id="btn-more-menu"',
            "settings.js",
            "settings.css",
        ):
            self.assertIn(needle, html)
        js = (WEB / "settings.js").read_text()
        for kind in (
            "settings_get",
            "settings_save_theme",
            "settings_preset",
            "settings_save_style",
            "settings_save_config",
            "settings_preview_sound",
            "settings_send_test",
        ):
            self.assertIn(kind, js)
        for banned in (
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "document.write",
            "confirm(",
            'setAttribute("style"',
        ):
            self.assertNotIn(banned, js)


class RulesBridge(unittest.TestCase):
    """The rules_* requests: validation, caps, routing, error passthrough."""

    def setUp(self):
        self.fake = FakeSettings()
        self.h = Harness(settings=self.fake)
        self.req = 0

    def ask(self, **msg):
        self.req += 1
        self.h.bridge.handle({"req": self.req, **msg})
        req, data = self.h.last_reply()
        self.assertEqual(req, self.req)
        return data

    def calls(self):
        return [c for c in self.fake.calls if c[0].startswith("rules") and c[0] != "rules_get"]

    def test_get_and_source(self):
        res = self.ask(type="rules_get")
        self.assertTrue(res["ok"])
        self.assertEqual(res["rules"][0]["name"], "example sound")
        self.assertTrue(res["editable"])
        res = self.ask(type="rules_get_source")
        self.assertEqual(res["ok"], True)
        self.assertIn("[[rule]]", res["text"])

    def test_unavailable_when_settings_is_none(self):
        h = Harness()
        for i, msg in enumerate(
            (
                {"type": "rules_get"},
                {"type": "rules_update", "index": 0, "data": {}},
                {"type": "rules_add", "data": {}},
                {"type": "rules_delete", "index": 0},
                {"type": "rules_move", "index": 0, "to": 1},
                {"type": "rules_set_enabled", "index": 0, "on": True},
                {"type": "rules_test", "data": {}, "sample": {}},
                {"type": "rules_get_source"},
                {"type": "rules_save_source", "text": ""},
            ),
            1,
        ):
            h.bridge.handle({"req": i, **msg})
            _, res = h.last_reply()
            self.assertFalse(res["ok"], msg)
            self.assertTrue(res["unavailable"], msg)

    def test_routing_and_arguments(self):
        data = {"name": "a", "app": "X"}
        self.assertTrue(self.ask(type="rules_update", index=2, data=data, expect="a")["ok"])
        self.assertTrue(self.ask(type="rules_add", data=data)["ok"])
        self.assertTrue(self.ask(type="rules_add", data=data, position=3)["ok"])
        self.assertTrue(self.ask(type="rules_delete", index=1)["ok"])
        self.assertTrue(self.ask(type="rules_move", index=1, to=0)["ok"])
        self.assertTrue(self.ask(type="rules_set_enabled", index=1, on=False)["ok"])
        self.assertTrue(self.ask(type="rules_save_source", text="[[rule]]\n")["ok"])
        self.assertEqual(
            self.calls(),
            [
                ("rules_update", 2, data, "a"),
                ("rules_add", data, None),
                ("rules_add", data, 3),
                ("rules_delete", 1, None),
                ("rules_move", 1, 0, None),
                ("rules_set_enabled", 1, False, None),
                ("rules_save_source", "[[rule]]\n"),
            ],
        )

    def test_test_request(self):
        sample = {"app": "A", "summary": "s", "body": "b", "urgency": "low", "hints": {"k": 1}}
        res = self.ask(type="rules_test", data={"name": "t"}, sample=sample, index=0)
        self.assertEqual((res["ok"], res["matched"]), (True, True))
        self.assertIn("rule", res["result"])
        call = self.calls()[-1]
        self.assertEqual(call[0], "rules_test")
        self.assertEqual(call[3], 0)
        self.assertEqual(call[2]["hints"], {"k": 1})

    def test_indexes_must_be_real_ints(self):
        before = len(self.calls())
        for bad in (True, "1", 1.0, -1, 10000, None, [1]):
            self.assertFalse(self.ask(type="rules_delete", index=bad)["ok"], bad)
            self.assertFalse(self.ask(type="rules_update", index=bad, data={})["ok"], bad)
            self.assertFalse(self.ask(type="rules_move", index=bad, to=0)["ok"], bad)
            self.assertFalse(self.ask(type="rules_move", index=0, to=bad)["ok"], bad)
            self.assertFalse(self.ask(type="rules_set_enabled", index=bad, on=True)["ok"], bad)
        for bad in (True, "1", 1.5, -1, 10000):
            self.assertFalse(self.ask(type="rules_add", data={}, position=bad)["ok"], bad)
            self.assertFalse(self.ask(type="rules_test", data={}, sample={}, index=bad)["ok"], bad)
        for bad in (1, "yes", None):
            self.assertFalse(self.ask(type="rules_set_enabled", index=0, on=bad)["ok"], bad)
        self.assertFalse(self.ask(type="rules_update", index=0, data={}, expect=5)["ok"])
        self.assertFalse(self.ask(type="rules_delete", index=0, expect="x" * 513)["ok"])
        self.assertEqual(len(self.calls()), before)

    def test_rule_data_caps_and_shape(self):
        before = len(self.calls())
        for bad in ([], "x", None, 5, {"name": float("nan")}):
            self.assertFalse(self.ask(type="rules_add", data=bad)["ok"], bad)
        big = {"name": "a", "run": "x" * (64 * 1024)}
        self.assertFalse(self.ask(type="rules_add", data=big)["ok"])
        self.assertFalse(self.ask(type="rules_update", index=0, data=big)["ok"])
        self.assertEqual(len(self.calls()), before)
        edge = {"name": "a", "run": "x" * (64 * 1024 - 40)}
        self.assertTrue(self.ask(type="rules_add", data=edge)["ok"])

    def test_sample_caps(self):
        ok = {"app": "a" * 4096, "summary": "", "body": "", "urgency": "normal", "hints": {}}
        self.assertTrue(self.ask(type="rules_test", data={"name": "t"}, sample=ok)["ok"])
        before = len(self.calls())
        for bad in (
            {**ok, "app": "a" * 4097},
            {**ok, "body": "x" * 4097},
            {**ok, "summary": 5},
            {**ok, "urgency": ["low"]},
            {**ok, "hints": {"k": ["a"]}},
            {**ok, "hints": {"k": {"a": 1}}},
            {**ok, "hints": {"k": "v" * 4097}},
            {**ok, "hints": {str(i): "v" for i in range(51)}},
            {**ok, "hints": []},
            {**ok, "body": "a\0b"},
            [],
            None,
        ):
            self.assertFalse(
                self.ask(type="rules_test", data={"name": "t"}, sample=bad)["ok"], str(bad)[:60]
            )
        self.assertFalse(self.ask(type="rules_test", data="x", sample=ok)["ok"])
        self.assertEqual(len(self.calls()), before)

    def test_source_cap(self):
        self.assertTrue(
            self.ask(type="rules_save_source", text="#" * (256 * 1024))["ok"],
        )
        for text in ("#" * (256 * 1024 + 1), 5, None, ["x"]):
            res = self.ask(type="rules_save_source", text=text)
            self.assertFalse(res["ok"])

    def test_service_errors_pass_through_with_field_paths(self):
        res = self.ask(type="rules_update", index=0, data={"name": "bad"})
        self.assertEqual(res, {"ok": False, "errors": {"app_regex": "not a valid regex"}})
        res = self.ask(type="rules_save_source", text="bad")
        self.assertEqual(res, {"ok": False, "errors": {"source": "line 1: not valid"}})
        self.fake.fail_with = OSError("read-only file system")
        with self.assertLogs("notifyd.center", "ERROR"):
            res = self.ask(type="rules_get")
        self.assertEqual(res, {"ok": False, "errors": {"": "read-only file system"}})

    def test_a_request_without_a_valid_req_is_dropped(self):
        before = len(self.h.js)
        for req in (None, "1", -1, True):
            self.h.bridge.handle({"type": "rules_get", "req": req})
        self.assertEqual(len(self.h.js), before)
        self.assertEqual(self.calls(), [])

    def test_the_page_wires_the_rules_tab(self):
        html = (WEB / "center.html").read_text()
        self.assertIn("rules.js", html)
        js = (WEB / "rules.js").read_text()
        for kind in (
            "rules_get",
            "rules_update",
            "rules_add",
            "rules_delete",
            "rules_move",
            "rules_set_enabled",
            "rules_test",
            "rules_get_source",
            "rules_save_source",
            "settings_preview_sound",
        ):
            self.assertIn(kind, js)
        for banned in (
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "document.write",
            "confirm(",
            "eval(",
            'setAttribute("style"',
        ):
            self.assertNotIn(banned, js)
        self.assertIn("rules", (WEB / "center.js").read_text())
        self.assertIn('["rules", "Rules"]', (WEB / "settings.js").read_text())


if __name__ == "__main__":
    unittest.main()
