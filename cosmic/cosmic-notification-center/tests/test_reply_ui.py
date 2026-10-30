"""Reply actions, UI side: payload cleaning, message validation on all three hosts, and the toast
hold / keyboard state machine. Nothing here needs a display."""

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from notifyd import model, reply_ui
from notifyd.center import CenterBridge, CenterWindow
from notifyd.model import CloseReason, Notification
from notifyd.reply_ui import clean_reply, clean_result
from notifyd.shade import ShadeBridge, ShadeState, ShadeWindow
from notifyd.store import Store
from notifyd.toasts import REPLY_LINGER_MS
from tests.test_toasts import WEB, FakeHost, note

HOST = "127.0.0.1:4320"
SECRETS = ("secret/path", "SECRETTOKEN", "topsecretbody")


def raw(**kw):
    """A backend reply dict that also carries keys the contract says it never has."""
    r = {
        "choices": [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}],
        "text": {"placeholder": "Say…", "max": 50},
        "host": HOST,
        "method": "POST",
        "allowed": True,
        "reason": None,
        "confirm": False,
        "answered": None,
        "error": None,
        "url": "http://127.0.0.1:4320/secret/path",
        "body": "topsecretbody",
        "token": "SECRETTOKEN",
    }
    r.update(kw)
    return r


def mk(id, **kw):
    return Notification.from_notify(
        id, "App", 0, "", "Sum", "body", [], {}, kw.pop("expire", -1), 1.0
    )


class ReplyCase(unittest.TestCase):
    """Patches `to_web` (reply_ui's and the toast host's) so notification id -> raw reply."""

    def setUp(self):
        self.replies: dict[int, dict] = {}
        real = model.to_web

        def fake(n, runs):
            d = real(n, runs)
            if n.id in self.replies:
                d["reply"] = self.replies[n.id]
            return d

        for target in ("notifyd.reply_ui.to_web", "notifyd.toasts.to_web"):
            p = mock.patch(target, fake)
            p.start()
            self.addCleanup(p.stop)


class Cleaning(unittest.TestCase):
    def test_allow_list_drops_url_body_token(self):
        out = clean_reply(raw())
        text = json.dumps(out)
        for secret in SECRETS:
            self.assertNotIn(secret, text)
        self.assertEqual(
            set(out),
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
        self.assertEqual(
            out["choices"], [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}]
        )
        self.assertEqual(out["text"], {"placeholder": "Say…", "max": 50})
        self.assertEqual((out["host"], out["method"], out["allowed"]), (HOST, "POST", True))

    def test_not_a_dict_is_none(self):
        for bad in (None, [], "x", 5):
            self.assertIsNone(clean_reply(bad))

    def test_text_max_is_capped_and_defaulted(self):
        self.assertEqual(
            clean_reply(raw(text={"placeholder": "", "max": 99999}))["text"]["max"], 4000
        )
        self.assertEqual(clean_reply(raw(text={"placeholder": ""}))["text"]["max"], 4000)
        self.assertEqual(clean_reply(raw(text={"max": -3}))["text"]["max"], 4000)
        self.assertEqual(clean_reply(raw(text={"max": True}))["text"]["max"], 4000)
        self.assertIsNone(clean_reply(raw(text=None))["text"])

    def test_allowed_must_be_exactly_true(self):
        for v in (1, "yes", None):
            self.assertFalse(clean_reply(raw(allowed=v))["allowed"], v)

    def test_answered_passes_through_with_only_known_keys(self):
        ans = {"choice": "yes", "at": 1000.5, "status": 200, "token": "SECRETTOKEN", "url": "x"}
        out = clean_reply(raw(answered=ans))["answered"]
        self.assertEqual(out, {"choice": "yes", "at": 1000.5, "status": 200})
        out = clean_reply(raw(answered={"text": "hi", "at": 5, "status": "failed"}))["answered"]
        self.assertEqual(out, {"text": "hi", "at": 5, "status": "failed"})

    def test_choices_are_validated_and_capped(self):
        many = [{"id": f"c{i}", "label": "l"} for i in range(100)]
        bad = [{"id": 5, "label": "x"}, {"label": "no id"}, "str", {"id": "", "label": "x"}]
        self.assertEqual(len(clean_reply(raw(choices=many + bad))["choices"]), reply_ui.MAX_CHOICES)
        self.assertEqual(clean_reply(raw(choices=bad))["choices"], [])
        self.assertEqual(clean_reply(raw(choices="nope"))["choices"], [])

    def test_result_cleaning(self):
        self.assertEqual(clean_result({"status": "sent"}, "choice"), {"status": "sent"})
        self.assertEqual(
            clean_result({"status": "pending_confirm", "token": "ab_-9"}, "text"),
            {"status": "pending_confirm", "token": "ab_-9"},
        )
        refused = {"status": "refused", "reason": "nope", "extra": 1}
        self.assertEqual(clean_result(refused, "text"), {"status": "refused", "reason": "nope"})
        for bad in (None, 5, "sent", {}, {"status": "weird"}):
            self.assertEqual(clean_result(bad, "choice")["status"], "refused", bad)
        for tok in (None, "", "a b", "x" * 65, "ü", 5):  # unusable confirm token
            res = clean_result({"status": "pending_confirm", "token": tok}, "choice")
            self.assertEqual(res["status"], "refused", tok)
            self.assertNotIn("token", res)
        self.assertEqual(
            clean_result({"status": "sent", "token": "leak"}, "text"), {"status": "sent"}
        )
        self.assertEqual(clean_result(None, "confirm"), {"status": "sent"})
        self.assertEqual(clean_result({"status": "refused"}, "cancel"), {"status": "cancelled"})


def results(js):
    out = []
    for s in js:
        m = re.fullmatch(r"window\.nc\.replyResult\((.*)\)", s, re.S)
        if m:
            out.append(json.loads(m[1]))
    return out


class Side:
    """One host behind a common face: add(n), send(msg), results(), payload leak check."""

    def __init__(self, **callbacks):
        self.calls: list[tuple] = []
        self.cb = {
            "on_reply_choice": lambda i, c: (
                self.calls.append(("choice", i, c)),
                {"status": "sent"},
            )[1],
            "on_reply_text": lambda i, t: (self.calls.append(("text", i, t)), {"status": "sent"})[
                1
            ],
            "on_reply_confirm": lambda i, t: self.calls.append(("confirm", i, t)),
            "on_reply_cancel": lambda i, t: self.calls.append(("cancel", i, t)),
        }
        self.cb.update(callbacks)


class ShadeSide(Side):
    def __init__(self, tc, **callbacks):
        super().__init__(**callbacks)
        tmp = tempfile.TemporaryDirectory()
        tc.addCleanup(tmp.cleanup)
        self.store = Store(":memory:")
        self.js: list[str] = []
        self.bridge = ShadeBridge(
            self.store,
            ShadeState(Path(tmp.name) / "s.json"),
            on_dismiss=lambda ids: None,
            on_mark_read=lambda ids: None,
            on_open_details=lambda i: None,
            on_open_center=lambda: None,
            on_invoke_action=lambda i, k: True,
            get_dnd=lambda: False,
            on_set_dnd=lambda on: None,
            get_app_prefs=lambda app: {},
            on_set_app_prefs=lambda app, p: None,
            run_js=self.js.append,
            open_link=lambda h: None,
            close=lambda: None,
            **self.cb,
        )

    def add(self, n):
        self.store.record(n)

    def send(self, msg):
        self.bridge.handle(msg)

    def results(self):
        return results(self.js)

    def page_payload(self, id):
        data = self.bridge.payload()
        items = [i for g in data["groups"] for i in g["items"] if i["id"] == id]
        return json.dumps(data), items[0]["reply"]


class CenterSide(Side):
    def __init__(self, tc, **callbacks):
        super().__init__(**callbacks)
        self.store = Store(":memory:")
        self.js: list[str] = []
        self.bridge = CenterBridge(
            self.store,
            on_invoke_action=lambda i, k: True,
            on_dismiss=lambda i: None,
            on_mark_read=lambda ids: None,
            on_delete=lambda ids: None,
            on_clear=lambda: None,
            run_js=self.js.append,
            copy_text=lambda t: None,
            open_link=lambda h: None,
            **self.cb,
        )

    def add(self, n):
        self.store.record(n)

    def send(self, msg):
        self.bridge.handle(msg)

    def results(self):
        return results(self.js)

    def page_payload(self, id):
        data = self.bridge.detail_payload(id)
        return json.dumps(data), data["notification"]["reply"]


class ToastSide(Side):
    def __init__(self, tc, **callbacks):
        super().__init__(**callbacks)
        self.host = FakeHost(WEB, lambda i, k: None, lambda i, r: None, **self.cb)

    def add(self, n):
        self.host.show(n)

    def send(self, msg):
        self.host._on_message(msg)

    def results(self):
        return results(self.host.js)

    def page_payload(self, id):
        script = next(s for s in self.host.js if s.startswith("window.nc.show("))
        text = script[len("window.nc.show(") : -1]
        return text, json.loads(text)["reply"]


class Validation:
    """Mixed into one TestCase per host. `make(**callbacks)` builds the Side."""

    side = None

    def make(self, **cb):
        s = self.side(self, **cb)
        self.replies[1] = raw()
        s.add(mk(1))
        return s

    def test_payload_has_the_cleaned_reply_and_no_secrets(self):
        s = self.make()
        text, reply = s.page_payload(1)
        for secret in SECRETS:
            self.assertNotIn(secret, text)
        self.assertEqual(reply, clean_reply(raw()))
        self.assertEqual(reply["host"], HOST)

    def test_answered_and_refusal_data_reach_the_page(self):
        self.replies[1] = raw(answered={"choice": "yes", "at": 7, "status": 200}, allowed=False)
        self.replies[1]["reason"] = "host not allowed"
        s = self.side(self)
        s.add(mk(1))
        _, reply = s.page_payload(1)
        self.assertEqual(reply["answered"], {"choice": "yes", "at": 7, "status": 200})
        self.assertEqual((reply["allowed"], reply["reason"]), (False, "host not allowed"))

    def test_nothing_wired_means_no_reply_block(self):
        none = dict.fromkeys(reply_ui.REPLY_CALLBACKS)
        s = self.make(**none)
        self.assertIsNone(s.page_payload(1)[1])
        s.send({"type": "reply_choice", "id": 1, "choice": "yes"})
        self.assertEqual((s.calls, s.results()), ([], []))

    def test_unwired_part_is_stripped(self):
        s = self.make(on_reply_text=None)
        reply = s.page_payload(1)[1]
        self.assertIsNone(reply["text"])
        self.assertEqual(len(reply["choices"]), 2)
        s.send({"type": "reply_text", "id": 1, "text": "hi"})
        self.assertEqual(s.calls, [])
        s = self.make(on_reply_choice=None)
        self.assertEqual(s.page_payload(1)[1]["choices"], [])
        self.assertIsNotNone(s.page_payload(1)[1]["text"])

    def test_choice_routes_and_answers_the_page(self):
        s = self.make()
        s.send({"type": "reply_choice", "id": 1, "choice": "yes"})
        self.assertEqual(s.calls, [("choice", 1, "yes")])
        self.assertEqual(s.results(), [{"id": 1, "kind": "choice", "status": "sent"}])

    def test_choice_validation(self):
        s = self.make()
        for bad in ("maybe", "", None, 5, ["yes"], {"id": "yes"}, "YES", "yes "):
            s.send({"type": "reply_choice", "id": 1, "choice": bad})
        s.send({"type": "reply_choice", "id": 1})
        self.assertEqual((s.calls, s.results()), ([], []))

    def test_ids_must_be_known_real_ints(self):
        s = self.make()
        for bad in (2, 0, -1, True, "1", 1.0, None, [1], 2**32, 10**30):
            s.send({"type": "reply_choice", "id": bad, "choice": "yes"})
            s.send({"type": "reply_text", "id": bad, "text": "hi"})
            s.send({"type": "reply_confirm", "id": bad, "token": "abc"})
            s.send({"type": "reply_cancel", "id": bad, "token": "abc"})
        self.assertEqual((s.calls, s.results()), ([], []))

    def test_text_routes(self):
        s = self.make()
        s.send({"type": "reply_text", "id": 1, "text": "multi\nline ✓"})
        self.assertEqual(s.calls, [("text", 1, "multi\nline ✓")])
        self.assertEqual(s.results()[0]["status"], "sent")

    def test_text_limit_is_text_max(self):
        s = self.make()  # max 50
        s.send({"type": "reply_text", "id": 1, "text": "x" * 50})
        s.send({"type": "reply_text", "id": 1, "text": "x" * 51})
        self.assertEqual(len(s.calls), 1)

    def test_text_hard_cap_4000_beats_a_larger_max(self):
        self.replies[1] = raw(text={"placeholder": "", "max": 100000})
        s = self.side(self)
        s.add(mk(1))
        s.send({"type": "reply_text", "id": 1, "text": "x" * 4000})
        s.send({"type": "reply_text", "id": 1, "text": "x" * 4001})
        self.assertEqual(len(s.calls), 1)

    def test_text_validation(self):
        s = self.make()
        for bad in ("a\x00b", "\x00", None, 5, ["hi"], {"t": 1}):
            s.send({"type": "reply_text", "id": 1, "text": bad})
        s.send({"type": "reply_text", "id": 1})
        self.assertEqual((s.calls, s.results()), ([], []))

    def test_text_refused_when_the_reply_has_no_text(self):
        self.replies[1] = raw(text=None)
        s = self.side(self)
        s.add(mk(1))
        s.send({"type": "reply_text", "id": 1, "text": "hi"})
        self.assertEqual((s.calls, s.results()), ([], []))

    def test_confirm_and_cancel_tokens(self):
        s = self.make()
        for good in ("abc", "A_b-9", "x" * 64):
            s.send({"type": "reply_confirm", "id": 1, "token": good})
            s.send({"type": "reply_cancel", "id": 1, "token": good})
        self.assertEqual(len(s.calls), 6)
        s.calls.clear()
        for bad in ("", "x" * 65, "a b", "a/b", "a\n", "é", None, 5, ["a"], {"a": 1}):
            s.send({"type": "reply_confirm", "id": 1, "token": bad})
            s.send({"type": "reply_cancel", "id": 1, "token": bad})
        s.send({"type": "reply_confirm", "id": 1})
        self.assertEqual(s.calls, [])

    def test_confirm_flow_returns_status_to_the_page(self):
        s = self.make(
            on_reply_text=lambda i, t: {"status": "pending_confirm", "token": "tok_1"},
        )
        s.send({"type": "reply_text", "id": 1, "text": "hello"})
        res = s.results()[-1]
        self.assertEqual(
            res, {"id": 1, "kind": "text", "status": "pending_confirm", "token": "tok_1"}
        )
        s.send({"type": "reply_confirm", "id": 1, "token": "tok_1"})
        self.assertEqual(s.calls, [("confirm", 1, "tok_1")])
        self.assertEqual(s.results()[-1]["status"], "sent")
        s.send({"type": "reply_cancel", "id": 1, "token": "tok_1"})
        self.assertEqual(s.results()[-1], {"id": 1, "kind": "cancel", "status": "cancelled"})

    def test_refused_reason_reaches_the_page(self):
        s = self.make(on_reply_choice=lambda i, c: {"status": "refused", "reason": "rate limited"})
        s.send({"type": "reply_choice", "id": 1, "choice": "no"})
        self.assertEqual(
            s.results(),
            [{"id": 1, "kind": "choice", "status": "refused", "reason": "rate limited"}],
        )

    def test_not_allowed_is_refused_without_asking_the_backend(self):
        self.replies[1] = raw(allowed=False, reason="host not allowed")
        s = self.side(self)
        s.add(mk(1))
        s.send({"type": "reply_choice", "id": 1, "choice": "yes"})
        s.send({"type": "reply_text", "id": 1, "text": "hi"})
        self.assertEqual(s.calls, [])
        self.assertEqual(
            [(r["status"], r["reason"]) for r in s.results()], [("refused", "host not allowed")] * 2
        )

    def test_a_raising_or_garbage_callback_is_a_refusal(self):
        def boom(i, c):
            raise RuntimeError("x")

        s = self.make(on_reply_choice=boom, on_reply_text=lambda i, t: "sent!")
        s.send({"type": "reply_choice", "id": 1, "choice": "yes"})
        s.send({"type": "reply_text", "id": 1, "text": "hi"})
        self.assertEqual([r["status"] for r in s.results()], ["refused", "refused"])

    def test_unwired_confirm_callback_is_refused(self):
        s = self.make(on_reply_confirm=None)
        s.send({"type": "reply_confirm", "id": 1, "token": "abc"})
        self.assertEqual(s.results()[-1]["status"], "refused")

    def test_a_notification_without_reply_ignores_the_messages(self):
        s = self.make()
        s.add(mk(2))
        s.send({"type": "reply_choice", "id": 2, "choice": "yes"})
        self.assertEqual(s.calls, [])


class ShadeValidation(Validation, ReplyCase):
    side = ShadeSide


class CenterValidation(Validation, ReplyCase):
    side = CenterSide


class ToastValidation(Validation, ReplyCase):
    side = ToastSide


class WindowHosts(ReplyCase):
    """The window hosts expose the four callbacks as attributes and constructor kwargs."""

    def test_attributes_forward_to_the_bridge(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(":memory:")
        fns = {n: (lambda *a, n=n: {"status": "sent"}) for n in reply_ui.REPLY_CALLBACKS}
        center = CenterWindow(
            store,
            WEB,
            on_invoke_action=lambda i, k: True,
            on_dismiss=lambda i: None,
            on_mark_read=lambda ids: None,
            on_delete=lambda ids: None,
            on_clear=lambda: None,
            state_path=None,
            **fns,
        )
        shade = ShadeWindow(
            store,
            WEB,
            on_dismiss=lambda ids: None,
            on_mark_read=lambda ids: None,
            on_open_details=lambda i: None,
            on_open_center=lambda: None,
            on_invoke_action=lambda i, k: True,
            get_dnd=lambda: False,
            on_set_dnd=lambda on: None,
            get_app_prefs=lambda a: {},
            on_set_app_prefs=lambda a, p: None,
            state_path=Path(tmp.name) / "s.json",
            **fns,
        )
        for host in (center, shade):
            for name in reply_ui.REPLY_CALLBACKS:
                self.assertIs(getattr(host, name), fns[name], name)
                setattr(host, name, None)  # build_daemon assigns like on_button
                self.assertIsNone(getattr(host.bridge, name), name)
                fn = lambda *a: None  # noqa: E731
                setattr(host, name, fn)
                self.assertIs(getattr(host.bridge, name), fn)


class Recording(FakeHost):
    """FakeHost that records the keyboard mode instead of touching GTK."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.kbd: list[bool] = []

    def _apply_keyboard(self, on):
        self.kbd.append(on)


class ToastReplyState(ReplyCase):
    def setUp(self):
        super().setUp()
        self.closed = []
        self.result = {"status": "sent"}
        self.h = Recording(
            WEB,
            lambda i, k: None,
            lambda i, r: self.closed.append((i, r)),
            on_reply_choice=lambda i, c: self.result,
            on_reply_text=lambda i, t: self.result,
            on_reply_confirm=lambda i, t: {"status": "sent"},
            on_reply_cancel=lambda i, t: None,
        )

    def add(self, id=1, expire=1000, **kw):
        self.replies[id] = raw(**kw)
        self.h.show(mk(id, expire=expire))

    def focus(self, focused, id=1):
        self.h._on_message({"type": "reply_focus", "id": id, "focused": focused})

    def fire(self):
        self.h.clock.fire_all()

    # keyboard mode ---------------------------------------------------------------------

    def test_toasts_never_take_the_keyboard_by_default(self):
        self.assertFalse(self.h._keyboard)
        self.h.show(note(1, expire=1000))  # no reply at all
        self.add(2, choices=[], text=None, answered=None, error="x")  # nothing to type into
        self.add(3, text=None)  # choices only
        self.assertEqual(self.h.kbd, [])
        self.assertFalse(self.h._keyboard)

    def test_text_box_turns_on_demand_on_and_off_again(self):
        self.add(1)
        self.assertEqual((self.h._keyboard, self.h.kbd), (True, [True]))
        self.add(2)
        self.assertEqual(self.h.kbd, [True])  # already on: no flapping
        self.h.close(1)
        self.assertTrue(self.h._keyboard)  # toast 2 still has a box
        self.h.close(2)
        self.assertEqual((self.h._keyboard, self.h.kbd), (False, [True, False]))

    def test_expiry_and_dismiss_also_release_the_keyboard(self):
        self.add(1)
        self.fire()
        self.assertEqual(self.h.kbd, [True, False])
        self.add(2, expire=-1)
        self.h._on_message({"type": "closed", "id": 2})
        self.assertEqual(self.h.kbd, [True, False, True, False])

    def test_not_for_a_disabled_answered_or_unwired_box(self):
        self.add(1, allowed=False, reason="no")
        self.add(2, answered={"text": "x", "at": 1, "status": 200})
        self.assertEqual(self.h.kbd, [])
        h = Recording(WEB, lambda i, k: None, lambda i, r: None, on_reply_choice=lambda i, c: {})
        self.replies[3] = raw()
        h.show(mk(3))
        self.assertEqual(h.kbd, [])  # no text callback: the box is not even sent

    def test_replace_that_loses_the_box_releases_it(self):
        self.add(1)
        self.add(1, text=None)
        self.assertEqual(self.h.kbd, [True, False])

    def test_teardown_resets(self):
        self.add(1)
        self.h.teardown()
        self.assertFalse(self.h._keyboard)

    # hold ------------------------------------------------------------------------------

    def test_focus_holds_the_toast_open_past_expiry(self):
        self.add(1)
        self.focus(True)
        self.fire()
        self.assertEqual(self.closed, [])
        self.assertIn(1, self.h._active)
        self.assertEqual(self.h._reply_deferred, {1})

    def test_blur_after_expiry_closes_after_a_short_linger(self):
        self.add(1)
        self.focus(True)
        self.fire()
        self.focus(False)
        self.assertEqual(self.closed, [])
        self.assertEqual([ms for ms, _ in self.h.clock.jobs.values()], [REPLY_LINGER_MS])
        self.fire()
        self.assertEqual(self.closed, [(1, CloseReason.EXPIRED)])
        self.assertEqual(self.h.kbd, [True, False])

    def test_blur_before_expiry_changes_nothing(self):
        self.add(1)
        self.focus(True)
        self.focus(False)
        self.assertEqual([ms for ms, _ in self.h.clock.jobs.values()], [1000])  # original timer
        self.fire()
        self.assertEqual(self.closed, [(1, CloseReason.EXPIRED)])

    def test_pending_confirm_holds_until_confirm_or_cancel(self):
        self.add(1)
        self.result = {"status": "pending_confirm", "token": "t1"}
        self.h._on_message({"type": "reply_text", "id": 1, "text": "hi"})
        self.assertEqual(self.h._reply_confirm, {1})
        self.fire()
        self.assertEqual(self.closed, [])
        self.h._on_message({"type": "reply_cancel", "id": 1, "token": "t1"})
        self.assertEqual(self.h._reply_confirm, set())
        self.fire()  # the linger timer
        self.assertEqual(self.closed, [(1, CloseReason.EXPIRED)])

    def test_confirm_releases_too(self):
        self.add(1)
        self.result = {"status": "pending_confirm", "token": "t1"}
        self.h._on_message({"type": "reply_choice", "id": 1, "choice": "yes"})
        self.h._on_message({"type": "reply_confirm", "id": 1, "token": "t1"})
        self.assertEqual(self.h._reply_confirm, set())

    def test_refused_result_releases_a_pending_confirm(self):
        self.add(1)
        self.result = {"status": "pending_confirm", "token": "t1"}
        self.h._on_message({"type": "reply_choice", "id": 1, "choice": "yes"})
        self.result = {"status": "refused", "reason": "expired"}
        self.h._on_message({"type": "reply_choice", "id": 1, "choice": "yes"})
        self.assertEqual(self.h._reply_confirm, set())

    def test_hover_release_respects_the_reply_hold(self):
        self.add(1)
        self.h._hover.set_inside(True)
        self.fire()  # expiry deferred by the hover
        self.focus(True)
        self.h._hover.set_inside(False)
        self.h.grace_clock.fire_all()  # grace over: hover releases the toast ...
        self.assertEqual(self.closed, [])  # ... but the reply still holds it
        self.focus(False)
        self.fire()
        self.assertEqual(self.closed, [(1, CloseReason.EXPIRED)])

    def test_dismiss_and_action_still_work_while_held(self):
        self.add(1)
        self.focus(True)
        self.h._on_message({"type": "closed", "id": 1})
        self.assertEqual(self.closed, [(1, CloseReason.DISMISSED)])
        self.assertEqual((self.h._reply_focus, self.h._reply_deferred), (set(), set()))
        self.assertFalse(self.h._keyboard)

    def test_replace_drops_the_old_hold(self):
        self.add(1)
        self.focus(True)
        self.add(1)  # the page rebuilt the block
        self.assertEqual(self.h._reply_focus, set())
        self.fire()
        self.assertEqual(self.closed, [(1, CloseReason.EXPIRED)])

    def test_focus_messages_are_validated(self):
        self.add(1, expire=-1)
        self.add(2, text=None)  # no text box
        for msg in (
            {"type": "reply_focus", "id": 1, "focused": 1},
            {"type": "reply_focus", "id": 1, "focused": "yes"},
            {"type": "reply_focus", "id": 99, "focused": True},
            {"type": "reply_focus", "id": True, "focused": True},
            {"type": "reply_focus", "id": "1", "focused": True},
            {"type": "reply_focus", "id": 2, "focused": True},
            {"type": "reply_focus", "focused": True},
        ):
            self.h._on_message(msg)
            self.assertEqual(self.h._reply_focus, set(), msg)

    def test_sticky_toast_needs_no_hold_logic(self):
        self.add(1, expire=-1)  # timeout from the server default is None only for urgency 2
        self.focus(True)
        self.focus(False)
        self.assertEqual(self.closed, [])

    def test_reply_result_is_pushed_to_the_page(self):
        self.add(1)
        self.h._on_message({"type": "reply_choice", "id": 1, "choice": "yes"})
        self.assertEqual(
            self.h.js[-1], 'window.nc.replyResult({"id": 1, "kind": "choice", "status": "sent"})'
        )

    def test_payload_flags(self):
        self.add(1)
        script = next(s for s in self.h.js if s.startswith("window.nc.show("))
        payload = json.loads(script[len("window.nc.show(") : -1])
        self.assertEqual(payload["reply"]["host"], HOST)
        for secret in SECRETS:
            self.assertNotIn(secret, script)


if __name__ == "__main__":
    unittest.main()
