import json
import logging
import re
import tempfile
import unittest
from pathlib import Path

from notifyd.model import CloseReason, Notification, State
from notifyd.shade import (
    ShadeBridge,
    ShadeState,
    ShadeSurfaces,
    ShadeWindow,
    build_groups,
    clean_prefs,
    parse_size,
)
from notifyd.store import Store

WEB = Path(__file__).resolve().parent.parent / "web"


def mk(id, summary="s", body="", app="App", at=100.0, state=State.SHOWN, **kw):
    n = Notification.from_notify(id, app, 0, "", summary, body, kw.pop("actions", []), {}, -1, at)
    n.state = state
    for k, v in kw.items():
        setattr(n, k, v)
    return n


class Harness:
    def __init__(self, tmp, prefs_error=None):
        self.store = Store(":memory:")
        self.state = ShadeState(Path(tmp) / "shade.json")
        self.js: list[str] = []
        self.calls: list[tuple] = []
        self.dnd = False
        self.prefs = {"muted": True, "volume": 40}
        self.prefs_error = prefs_error
        self.bridge = ShadeBridge(
            self.store,
            self.state,
            on_dismiss=lambda ids: self.calls.append(("dismiss", ids)),
            on_mark_read=lambda ids: self.calls.append(("mark_read", ids)),
            on_open_details=lambda i: self.calls.append(("details", i)),
            on_open_center=lambda: self.calls.append(("center",)),
            on_invoke_action=lambda i, k: (self.calls.append(("action", i, k)), True)[1],
            get_dnd=lambda: self.dnd,
            on_set_dnd=lambda on: self.calls.append(("dnd", on)),
            get_app_prefs=lambda app: dict(self.prefs, evil=object()),
            on_set_app_prefs=self.set_prefs,
            run_js=self.js.append,
            open_link=lambda h: self.calls.append(("link", h)),
            close=lambda: self.calls.append(("close",)),
            on_size=lambda s: self.calls.append(("size", s)),
        )

    def set_prefs(self, app, prefs):
        if self.prefs_error:
            raise ValueError(self.prefs_error)
        self.calls.append(("prefs", app, prefs))

    def last_reply(self):
        m = re.fullmatch(r"window\.nc\.reply\((\d+), (.*)\)", self.js[-1], re.S)
        assert m, self.js[-1]
        return int(m[1]), json.loads(m[2])

    def ask(self, msg):
        self.bridge.handle(dict(msg, req=7))
        req, data = self.last_reply()
        assert req == 7
        return data


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.h = Harness(self._tmp.name)
        s = self.h.store
        s.record(mk(1, "one", app="A", at=10.0, actions=["ok", "OK"]))
        s.record(mk(2, "two", app="B", at=20.0, body="<b>bold</b> text"))
        s.record(mk(3, "three", app="A", at=30.0, closed_at=31.0, close_reason=CloseReason.EXPIRED))
        s.record(
            mk(4, "gone", app="C", at=40.0, closed_at=41.0, close_reason=CloseReason.DISMISSED)
        )


class Groups(Base):
    def test_order_counts_and_hooks(self):
        data = self.h.ask({"type": "state"})
        self.assertEqual([g["app"] for g in data["groups"]], ["A", "B"])  # A's newest is 30
        a = data["groups"][0]
        self.assertEqual((a["count"], a["unread"], a["newest_summary"]), (2, 2, "three"))
        self.assertEqual([i["id"] for i in a["items"]], [3, 1])
        self.assertTrue(a["items"][0]["expired"])
        self.assertFalse(a["items"][1]["expired"])
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["unread"], 3)
        self.assertFalse(data["dnd"])
        b = data["groups"][1]["items"][0]
        self.assertEqual(b["body"], [{"text": "bold", "b": True}, {"text": " text"}])

    def test_unread_excludes_read_and_dnd_flag(self):
        self.h.store.mark_read([1], 50.0)
        self.h.dnd = True
        data = self.h.ask({"type": "state"})
        self.assertEqual((data["unread"], data["dnd"]), (2, True))

    def test_build_groups_orders_by_newest_item_and_ties(self):
        items = [mk(1, app="X", at=5.0), mk(2, app="Y", at=9.0), mk(3, app="X", at=9.0)]
        groups = build_groups(items, lambda n: "", lambda app: app == "Y")
        self.assertEqual([g["app"] for g in groups], ["X", "Y"])  # tie: higher id first
        self.assertEqual([i["id"] for i in groups[0]["items"]], [3, 1])
        self.assertEqual([g["expanded"] for g in groups], [False, True])

    def test_empty(self):
        self.assertEqual(build_groups([], lambda n: ""), [])

    def test_expanded_comes_from_state(self):
        self.h.state.set_expanded("B", True)
        data = self.h.ask({"type": "state"})
        self.assertEqual([g["expanded"] for g in data["groups"]], [False, True])

    def test_dnd_state_item_is_listed(self):
        self.h.store.record(mk(5, app="D", at=50.0, state=State.DND))
        data = self.h.ask({"type": "state"})
        self.assertEqual(data["groups"][0]["app"], "D")
        self.assertEqual(data["groups"][0]["items"][0]["state"], "dnd")


class Routing(Base):
    def test_dismiss_and_mark_read_only_listed_ids(self):
        self.h.bridge.handle({"type": "dismiss", "ids": [1, 4, 99, 1]})  # 4 is gone, 99 unknown
        self.h.bridge.handle({"type": "mark_read", "ids": [2, 3]})
        self.assertEqual(self.h.calls, [("dismiss", [1]), ("mark_read", [2, 3])])
        self.assertEqual(self.h.js.count("window.nc.refresh()"), 2)

    def test_nothing_listed_means_no_call(self):
        self.h.bridge.handle({"type": "dismiss", "ids": [4, 99]})
        self.assertEqual(self.h.calls, [])
        self.assertEqual(self.h.js, [])

    def test_bad_ids_dropped(self):
        for bad in ([True], ["1"], [-1], [2**40], "1", None, [1.5]):
            self.h.bridge.handle({"type": "dismiss", "ids": bad})
        self.assertEqual(self.h.calls, [])

    def test_group_commands(self):
        self.h.bridge.handle({"type": "dismiss_app", "app": "A"})
        self.h.bridge.handle({"type": "mark_read_app", "app": "B"})
        self.h.bridge.handle({"type": "dismiss_app", "app": "nope"})
        self.h.bridge.handle({"type": "dismiss_app", "app": 5})
        self.assertEqual(self.h.calls, [("dismiss", [3, 1]), ("mark_read", [2])])

    def test_clear_all_covers_everything_in_the_shade(self):
        self.h.bridge.handle({"type": "clear_all"})
        self.assertEqual(self.h.calls, [("dismiss", [3, 2, 1])])

    def test_details_closes_then_opens(self):
        self.h.bridge.handle({"type": "details", "id": 2})
        self.h.bridge.handle({"type": "details", "id": "2"})
        self.assertEqual(self.h.calls, [("close",), ("details", 2)])

    def test_open_center_and_close(self):
        self.h.bridge.handle({"type": "open_center"})
        self.h.bridge.handle({"type": "close"})
        self.assertEqual(self.h.calls, [("close",), ("center",), ("close",)])

    def test_action_validated_against_the_notification(self):
        self.assertEqual(self.h.ask({"type": "action", "id": 1, "key": "ok"}), {"ok": True})
        self.assertEqual(self.h.ask({"type": "action", "id": 1, "key": "nope"}), {"ok": False})
        self.assertEqual(self.h.ask({"type": "action", "id": 99, "key": "ok"}), {"ok": False})
        self.assertEqual(self.h.ask({"type": "action", "id": 1, "key": 5}), {"ok": False})
        self.assertEqual(self.h.calls, [("action", 1, "ok")])

    def test_dnd(self):
        self.h.bridge.handle({"type": "dnd", "on": True})
        self.h.bridge.handle({"type": "dnd", "on": "yes"})
        self.h.bridge.handle({"type": "dnd"})
        self.assertEqual(self.h.calls, [("dnd", True)])

    def test_links_allow_listed(self):
        for href in (
            "https://example.com/x",
            "mailto:a@b.c",
            "javascript:alert(1)",
            "file:///etc",
            5,
        ):
            self.h.bridge.handle({"type": "link", "href": href})
        self.assertEqual(
            self.h.calls, [("link", "https://example.com/x"), ("link", "mailto:a@b.c")]
        )

    def test_expand_persists(self):
        self.h.bridge.handle({"type": "expand", "app": "A", "expanded": True})
        self.h.bridge.handle({"type": "expand", "app": "B", "expanded": "yes"})
        self.h.bridge.handle({"type": "expand", "app": "", "expanded": True})
        self.assertTrue(ShadeState(self.h.state.path).is_expanded("A"))
        self.assertFalse(ShadeState(self.h.state.path).is_expanded("B"))
        self.h.bridge.handle({"type": "expand", "app": "A", "expanded": False})
        self.assertFalse(ShadeState(self.h.state.path).is_expanded("A"))

    def test_junk_messages(self):
        for junk in (None, 5, [], "x", {}, {"type": 3}, {"type": "boom"}, {"type": "state"}):
            self.h.bridge.handle(junk)  # {"type": "state"} has no req: dropped
        self.assertEqual(self.h.calls, [])
        self.assertEqual(self.h.js, [])

    def test_failing_callback_does_not_escape(self):
        def boom(ids):
            raise RuntimeError("x")

        self.h.bridge.on_dismiss = boom
        with self.assertLogs("notifyd.shade", "ERROR"):
            self.h.bridge.handle({"type": "dismiss", "ids": [1]})

    def test_request_error_is_answered(self):
        def boom():
            raise RuntimeError("db gone")

        self.h.bridge.get_dnd = boom
        with self.assertLogs("notifyd.shade", "ERROR"):
            self.assertEqual(self.h.ask({"type": "state"}), {"error": "db gone"})

    def test_size(self):
        msg = {"type": "size", "width": 460, "height": 300, "room": 20, "rect": [20, 20, 420, 260]}
        self.h.bridge.handle(msg)
        self.h.bridge.handle(dict(msg, width=-1))
        self.h.bridge.handle(dict(msg, rect=[1, 2, 3]))
        self.h.bridge.handle(dict(msg, room="x"))
        self.assertEqual(len(self.h.calls), 1)
        self.assertEqual(self.h.calls[0][1]["rect"], [20, 20, 420, 260])
        self.assertIsNone(parse_size(dict(msg, height=True)))


class Prefs(Base):
    def test_get_filters_unknown_and_unsafe_values(self):
        self.assertEqual(self.h.ask({"type": "prefs_get", "app": "A"}), {"prefs": self.h.prefs})

    def test_set_passes_known_keys_only(self):
        prefs = {"muted": True, "sound": "/x.ogg", "volume": 30, "sneaky": 1, "sticky": False}
        self.assertEqual(
            self.h.ask({"type": "prefs_set", "app": "A", "prefs": prefs}), {"ok": True}
        )
        want = {"muted": True, "sound": "/x.ogg", "volume": 30, "sticky": False}
        self.assertEqual(self.h.calls, [("prefs", "A", want)])

    def test_validation_error_is_shown_inline(self):
        h = Harness(self._tmp.name, prefs_error="`volume` must be an integer from 0 to 100")
        res = h.ask({"type": "prefs_set", "app": "A", "prefs": {"volume": 400}})
        self.assertEqual(res, {"ok": False, "error": "`volume` must be an integer from 0 to 100"})

    def test_bad_requests(self):
        self.assertEqual(self.h.ask({"type": "prefs_get"}), {"error": "bad request"})
        self.assertEqual(self.h.ask({"type": "prefs_get", "app": ""}), {"error": "bad request"})
        res = self.h.ask({"type": "prefs_set", "app": "A", "prefs": "x"})
        self.assertEqual(res, {"ok": False, "error": "bad request"})
        res = self.h.ask({"type": "prefs_set", "app": "A", "prefs": {"sound": ["a"]}})
        self.assertEqual(res, {"ok": False, "error": "bad request"})
        self.assertEqual(self.h.calls, [])

    def test_clean_prefs(self):
        self.assertIsNone(clean_prefs([]))
        self.assertEqual(clean_prefs({"volume": None, "x": 1}), {"volume": None})
        self.assertIsNone(clean_prefs({"sound": "x" * 5000}))


class StateFile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "sub" / "shade.json"

    def test_missing_means_collapsed(self):
        self.assertFalse(ShadeState(self.path).is_expanded("A"))

    def test_roundtrip_creates_dirs_and_leaves_no_temp(self):
        s = ShadeState(self.path)
        s.set_expanded("A", True)
        s.set_expanded("B", True)
        s.set_expanded("B", False)
        again = ShadeState(self.path)
        self.assertTrue(again.is_expanded("A"))
        self.assertFalse(again.is_expanded("B"))
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["shade.json"])
        self.assertEqual(json.loads(self.path.read_text()), {"expanded": {"A": True}})

    def test_corrupt_means_collapsed_and_recovers(self):
        self.path.parent.mkdir(parents=True)
        for text in (
            "{not json",
            "[]",
            "null",
            '{"expanded": 5}',
            '{"expanded": {"A": "yes"}}',
            "",
        ):
            self.path.write_text(text)
            self.assertFalse(ShadeState(self.path).is_expanded("A"), text)
        self.path.write_bytes(b"\xff\xfe\x00")
        s = ShadeState(self.path)
        self.assertFalse(s.is_expanded("A"))
        s.set_expanded("A", True)
        self.assertTrue(ShadeState(self.path).is_expanded("A"))

    def test_unwritable_location_does_not_raise(self):
        blocker = Path(self._tmp.name) / "file"
        blocker.write_text("x")
        s = ShadeState(blocker / "shade.json")  # parent is a file
        with self.assertLogs("notifyd.shade", "WARNING"):
            s.set_expanded("A", True)
        self.assertTrue(s.is_expanded("A"))  # still remembered in memory


class Surfaces(unittest.TestCase):
    """No path may leave the catcher up without the shade."""

    def setUp(self):
        logging.disable(logging.CRITICAL)  # the failure paths log tracebacks on purpose
        self.addCleanup(logging.disable, logging.NOTSET)

    def make(self, fail_catcher=False, fail_shade=False, fail_hide_shade=False):
        log: list[str] = []
        up = {"catcher": False, "shade": False}
        box: dict = {}

        def show_catcher():
            if fail_catcher:
                raise RuntimeError("catcher")
            up["catcher"] = True
            log.append("+catcher")

        def hide_catcher():
            up["catcher"] = False
            log.append("-catcher")

        def show_shade():
            up["shade"] = True  # mapped, then the failure happens
            if fail_shade:
                raise RuntimeError("shade")
            log.append("+shade")

        def hide_shade():
            up["shade"] = False
            log.append("-shade")
            box["s"].on_shade_hidden()  # the real window's "hide" signal fires re-entrantly
            if fail_hide_shade:
                raise RuntimeError("hide")

        s = ShadeSurfaces(show_catcher, hide_catcher, show_shade, hide_shade)
        box["s"] = s
        return s, up, log

    def assertLockstep(self, up):
        self.assertEqual(up["catcher"], up["shade"], up)

    def test_show_hide_toggle(self):
        s, up, log = self.make()
        self.assertTrue(s.show())
        self.assertEqual(log, ["+catcher", "+shade"])  # catcher first: shade sits above it
        self.assertEqual((s.visible, up), (True, {"catcher": True, "shade": True}))
        self.assertTrue(s.show())  # idempotent
        self.assertEqual(log, ["+catcher", "+shade"])
        s.toggle()
        self.assertEqual((s.visible, up), (False, {"catcher": False, "shade": False}))
        self.assertEqual(log[-2:], ["-shade", "-catcher"])  # re-entrancy did not double up
        s.toggle()
        self.assertTrue(s.visible)

    def test_catcher_press_closes_both(self):
        s, up, _ = self.make()
        s.show()
        s.on_catcher_press()
        self.assertEqual((s.visible, up), (False, {"catcher": False, "shade": False}))

    def test_shade_hidden_externally_takes_the_catcher_down(self):
        s, up, _ = self.make()
        s.show()
        up["shade"] = False
        s.on_shade_hidden()
        self.assertFalse(up["catcher"])
        self.assertFalse(s.visible)

    def test_catcher_hidden_externally_takes_the_shade_down(self):
        s, up, _ = self.make()
        s.show()
        up["catcher"] = False
        s.on_catcher_hidden()
        self.assertEqual(up, {"catcher": False, "shade": False})

    def test_failing_catcher_leaves_nothing_up(self):
        s, up, _ = self.make(fail_catcher=True)
        self.assertFalse(s.show())
        self.assertEqual((s.visible, up), (False, {"catcher": False, "shade": False}))

    def test_failing_shade_takes_the_catcher_down(self):
        s, up, _ = self.make(fail_shade=True)
        self.assertFalse(s.show())
        self.assertEqual((s.visible, up), (False, {"catcher": False, "shade": False}))

    def test_failing_hide_still_hides_the_catcher(self):
        s, up, _ = self.make(fail_hide_shade=True)
        s.show()
        s.hide()
        self.assertEqual((s.visible, up), (False, {"catcher": False, "shade": False}))

    def test_hide_when_never_shown_is_safe(self):
        s, up, _ = self.make()
        s.hide()
        s.on_catcher_press()
        self.assertEqual((s.visible, up), (False, {"catcher": False, "shade": False}))

    def test_any_event_sequence_keeps_lockstep(self):
        import itertools

        events = ["show", "hide", "toggle", "on_catcher_press", "on_shade_hidden"]
        for seq in itertools.product(events, repeat=4):
            s, up, _ = self.make()
            for ev in seq:
                getattr(s, ev)()
                if ev in ("hide", "on_catcher_press", "on_shade_hidden"):
                    self.assertEqual(up, {"catcher": False, "shade": False}, seq)
                self.assertEqual(s.visible, up["shade"], seq)
                self.assertLockstep(up)


class WindowNoDisplay(unittest.TestCase):
    def test_constructs_and_tracks_staleness_without_gtk(self):
        with tempfile.TemporaryDirectory() as tmp:
            noop = lambda *a: None  # noqa: E731
            w = ShadeWindow(
                Store(":memory:"),
                WEB,
                on_dismiss=noop,
                on_mark_read=noop,
                on_open_details=noop,
                on_open_center=noop,
                on_invoke_action=lambda i, k: True,
                get_dnd=lambda: False,
                on_set_dnd=noop,
                get_app_prefs=lambda app: {},
                on_set_app_prefs=noop,
                state_path=Path(tmp) / "shade.json",
            )
            self.assertFalse(w.is_visible())
            w.notify_added(mk(1))
            self.assertTrue(w._stale)
            w.hide()  # never shown: safe
            w.set_dnd(True)
            w.set_user_css("a{}")
            self.assertEqual(len(w._pending_js), 2)
            w.configure(anchor="bottom-left", margin=3, width=300)
            self.assertEqual((w.anchor, w.margin, w.width), ("bottom-left", 3, 300))


class Assets(unittest.TestCase):
    def test_page_links_and_csp(self):
        html = (WEB / "shade.html").read_text()
        self.assertLess(html.index("default.css"), html.index("shade.css"))
        self.assertIn("default-src 'none'", html)
        self.assertIn("script-src 'self'", html)
        self.assertIn("No notifications", html)
        self.assertIn('src="shade.js"', html)

    def test_js_is_dom_only_and_has_no_confirm(self):
        js = (WEB / "shade.js").read_text()
        for banned in (
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "document.write",
            "confirm(",
            "eval(",
        ):
            self.assertNotIn(banned, js)

    def test_css_documents_hooks(self):
        css = (WEB / "shade.css").read_text()
        head = css[: css.index("*/")]
        for hook in (
            ".shade-group[data-app]",
            ".shade-item[data-urgency][data-state]",
            ".shade-settings",
            ".tag-<name>",
        ):
            self.assertIn(hook, head)
        self.assertIn("prefers-reduced-motion", css)


BUTTONS = [
    {"id": "b0", "label": "Open log", "run": ["/bin/secret-cmd", "--token=hunter2"], "close": True},
    {"id": "b1", "label": "Later", "builtin": "snooze", "minutes": 15, "close": True},
    {"id": "b2", "label": "Copy", "builtin": "copy-body", "close": False},
]


class RuleButtons(Base):
    def setUp(self):
        super().setUp()
        self.h.store.record(
            mk(9, "with buttons", app="Z", at=50.0, buttons=[dict(b) for b in BUTTONS])
        )
        self.h.bridge.on_button = lambda i, b: self.h.calls.append(("button", i, b))

    def item(self):
        data = self.h.ask({"type": "state"})
        group = next(g for g in data["groups"] if g["app"] == "Z")
        return data, group["items"][0]

    def test_items_carry_buttons_but_never_commands(self):
        data, item = self.item()
        self.assertTrue(data["buttons_enabled"])
        self.assertEqual([b["id"] for b in item["buttons"]], ["b0", "b1", "b2"])
        self.assertEqual(item["buttons"][1]["minutes"], 15)
        self.assertNotIn("secret-cmd", json.dumps(data))
        self.assertNotIn("hunter2", json.dumps(data))
        groups = self.h.ask({"type": "state"})["groups"]
        self.assertEqual(next(g for g in groups if g["app"] == "B")["items"][0]["buttons"], [])

    def test_disabled_without_on_button(self):
        self.h.bridge.on_button = None
        self.assertFalse(self.h.ask({"type": "state"})["buttons_enabled"])
        self.h.bridge.handle({"type": "button", "id": 9, "button": "b0"})
        self.assertEqual([c for c in self.h.calls if c[0] == "button"], [])

    def test_press_routes(self):
        self.h.bridge.handle({"type": "button", "id": 9, "button": "b2"})
        self.assertEqual(self.h.calls, [("button", 9, "b2")])

    def test_rejects_bad_presses(self):
        for msg in (
            {"type": "button", "id": 9, "button": "b7"},
            {"type": "button", "id": 9, "button": 1},
            {"type": "button", "id": "9", "button": "b0"},
            {"type": "button", "id": False, "button": "b0"},
            {"type": "button", "id": 1, "button": "b0"},  # known id, no such button
            {"type": "button", "id": 404, "button": "b0"},  # unknown id
            {"type": "button", "button": "b0"},
        ):
            self.h.bridge.handle(msg)
        self.assertEqual(self.h.calls, [])

    def test_window_forwards_on_button(self):
        with tempfile.TemporaryDirectory() as tmp:
            noop = lambda *a: None  # noqa: E731
            kw = dict(
                on_dismiss=noop,
                on_mark_read=noop,
                on_open_details=noop,
                on_open_center=noop,
                on_invoke_action=lambda i, k: True,
                get_dnd=lambda: False,
                on_set_dnd=noop,
                get_app_prefs=lambda app: {},
                on_set_app_prefs=noop,
                state_path=Path(tmp) / "s.json",
            )
            w = ShadeWindow(Store(":memory:"), WEB, **kw)
            self.assertIsNone(w.on_button)
            w.on_button = noop
            self.assertIs(w.bridge.on_button, noop)
            self.assertIs(ShadeWindow(Store(":memory:"), WEB, on_button=noop, **kw).on_button, noop)


class IconPreview(unittest.TestCase):
    def test_preview_resolves_or_explains(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            f = Path(tmp) / "i.png"
            f.write_bytes(b"x")
            self.assertEqual(
                h.ask({"type": "icon_preview", "value": str(f)})["uri"], "file://" + str(f)
            )
            self.assertEqual(h.ask({"type": "icon_preview", "value": " "}), {"uri": ""})
            bad = h.ask({"type": "icon_preview", "value": "/no/such.png"})
            self.assertEqual(bad["uri"], "")
            self.assertIn("not found", bad["error"])
            self.assertIn("error", h.ask({"type": "icon_preview", "value": 5}))
            self.assertEqual(
                h.ask({"type": "prefs_set", "app": "A", "prefs": {"icon": "x"}}), {"ok": True}
            )
            self.assertEqual(h.calls[-1], ("prefs", "A", {"icon": "x"}))


if __name__ == "__main__":
    unittest.main()
