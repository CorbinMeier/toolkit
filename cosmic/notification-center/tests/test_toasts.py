import json
import tempfile
import unittest
from pathlib import Path

from notifyd.model import CloseReason, Notification, Urgency
from notifyd.toasts import (
    ExpiryTimers,
    HoverHold,
    ToastHost,
    build_payload,
    link_allowed,
    parse_anchor,
    resolve_icon,
)

WEB = Path(__file__).resolve().parent.parent / "web"


def note(id=1, body="hi", icon="", hints=None, expire=-1, actions=None):
    return Notification.from_notify(
        id, "App", 0, icon, "Sum", body, actions or [], hints or {}, expire, 1.0
    )


class FakeClock:
    def __init__(self):
        self.next = 0
        self.jobs = {}

    def schedule(self, ms, fn):
        self.next += 1
        self.jobs[self.next] = (ms, fn)
        return self.next

    def cancel(self, h):
        self.jobs.pop(h)

    def fire_all(self):
        for h in list(self.jobs):
            _, fn = self.jobs.pop(h)
            fn()


class Timers(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.t = ExpiryTimers(self.clock.schedule, self.clock.cancel)

    def test_start_fire_forgets(self):
        fired = []
        self.t.start(1, 500, fired.append)
        self.assertEqual(self.t.pending(), {1})
        self.clock.fire_all()
        self.assertEqual(fired, [1])
        self.assertEqual(self.t.pending(), set())

    def test_restart_replaces(self):
        self.t.start(1, 500, lambda i: None)
        self.t.start(1, 900, lambda i: None)
        self.assertEqual(len(self.clock.jobs), 1)
        self.assertEqual(next(iter(self.clock.jobs.values()))[0], 900)

    def test_none_is_sticky_and_cancels(self):
        self.t.start(1, 500, lambda i: None)
        self.t.start(1, None, lambda i: None)
        self.assertEqual((self.t.pending(), self.clock.jobs), (set(), {}))

    def test_cancel_unknown_is_fine(self):
        self.t.cancel(99)


class Icons(unittest.TestCase):
    def test_resolution(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "a b.png"
            f.write_bytes(b"x")
            themed = Path(d) / "themed.svg"
            themed.write_bytes(b"x")
            lookup = {"dialog-information": str(themed)}.get
            self.assertEqual(
                resolve_icon(str(f), {}, lookup), "file://" + str(f).replace(" ", "%20")
            )
            self.assertEqual(
                resolve_icon("file://" + str(f).replace(" ", "%20"), {}, lookup),
                "file://" + str(f).replace(" ", "%20"),
            )
            self.assertEqual(
                resolve_icon("dialog-information", {}, lookup), "file://" + str(themed)
            )
            # image-path hint wins over app_icon
            self.assertTrue(
                resolve_icon("dialog-information", {"image-path": str(f)}, lookup).endswith(
                    "a%20b.png"
                )
            )
            self.assertEqual(resolve_icon(str(Path(d) / "missing.png"), {}, lookup), "")
            self.assertEqual(resolve_icon("nope", {}, lookup), "")
            self.assertEqual(resolve_icon("http://x/y.png", {}, lookup), "")
            self.assertEqual(resolve_icon("", {}, lookup), "")


class Payload(unittest.TestCase):
    def test_body_sanitized_icon_resolved(self):
        n = note(body='<b>x</b><script>y</script><a href="javascript:z">l</a>', icon="/nope")
        p = build_payload(n, "file:///i.png")
        self.assertEqual(p["icon"], "file:///i.png")
        self.assertEqual(p["body"][0], {"text": "x", "b": True})
        self.assertNotIn("<", "".join(r["text"] for r in p["body"]))
        self.assertTrue(all("href" not in r for r in p["body"]))
        self.assertEqual(p["urgency"], "normal")

    def test_link_allowed(self):
        self.assertTrue(link_allowed("https://a"))
        self.assertTrue(link_allowed("mailto:a@b"))
        for bad in ["javascript:x", "file:///x", "ftp://x", "", "x"]:
            self.assertFalse(link_allowed(bad), bad)

    def test_anchor(self):
        self.assertEqual(parse_anchor("bottom-left"), ("bottom", "left"))
        self.assertEqual(parse_anchor("bogus"), ("top", "right"))


class FakeHost(ToastHost):
    """ToastHost with the GTK side replaced by recorders."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.js = []
        self.opened = []
        self.sizes = []
        self._timers = ExpiryTimers(FakeClock().schedule, lambda h: None)
        self.clock = FakeClock()
        self._timers = ExpiryTimers(self.clock.schedule, self.clock.cancel)
        self.grace_clock = FakeClock()
        self._hover._timers = ExpiryTimers(self.grace_clock.schedule, self.grace_clock.cancel)

    def _ensure_view(self):
        pass

    def _reveal(self):
        pass

    def _eval(self, script):
        self.js.append(script)

    def _open_link(self, href):
        self.opened.append(href)

    def _theme_lookup(self, name):
        return None

    def _apply_size(self, w, h, rects):
        self.sizes.append((w, h, rects))


class HostCase(unittest.TestCase):
    def setUp(self):
        self.actions, self.closed = [], []
        self.h = FakeHost(
            WEB,
            lambda i, k: self.actions.append((i, k)),
            lambda i, r: self.closed.append((i, r)),
        )


class HostLogic(HostCase):
    def test_show_pushes_json_and_arms_timer(self):
        self.h.show(note(1, expire=1000))
        self.assertTrue(self.h.js[-1].startswith("window.nc.show({"))
        self.assertEqual(self.h._timers.pending(), {1})

    def test_never_expiring_has_no_timer(self):
        self.h.show(note(1, hints={"urgency": 2}))
        self.assertEqual(self.h._timers.pending(), set())

    def test_same_id_restarts_timer_once(self):
        self.h.show(note(1, expire=1000))
        self.h.show(note(1, expire=2000))
        self.assertEqual(len(self.h.clock.jobs), 1)

    def test_expiry_fires_expired_once(self):
        self.h.show(note(1, expire=1000))
        self.h.clock.fire_all()
        self.assertEqual(self.closed, [(1, CloseReason.EXPIRED)])
        self.assertEqual(self.h.js[-1], "window.nc.close(1)")

    def test_close_is_silent(self):
        self.h.show(note(1, expire=1000))
        self.h.close(1)
        self.assertEqual((self.closed, self.actions, self.h._timers.pending()), ([], [], set()))
        self.h.close(1)  # second close: no extra JS
        self.assertEqual(self.h.js.count("window.nc.close(1)"), 1)

    def test_dismiss_message(self):
        self.h.show(note(1, expire=1000))
        self.h._on_message({"type": "closed", "id": 1})
        self.assertEqual(self.closed, [(1, CloseReason.DISMISSED)])
        self.assertEqual(self.h._timers.pending(), set())
        self.h._on_message({"type": "closed", "id": 1})  # already gone
        self.assertEqual(len(self.closed), 1)

    def test_action_fires_then_closes(self):
        self.h.show(note(1, actions=["default", "Open", "ok", "OK"]))
        self.h._on_message({"type": "action", "id": 1, "key": "default"})
        self.assertEqual(self.actions, [(1, "default")])
        self.assertEqual(self.closed, [(1, CloseReason.DISMISSED)])

    def test_untrusted_messages_ignored(self):
        self.h.show(note(1))
        for msg in [
            None,
            "x",
            [],
            {},
            {"type": "action", "id": 2, "key": "k"},
            {"type": "action", "id": True, "key": "k"},
            {"type": "action", "id": 1, "key": 5},
            {"type": "link", "href": "javascript:alert(1)"},
            {"type": "link", "href": 3},
            {"type": "height", "width": "x", "height": 1},
        ]:
            self.h._on_message(msg)
        self.assertEqual((self.actions, self.closed, self.h.opened, self.h.sizes), ([], [], [], []))

    def test_link_opens_when_allowed(self):
        self.h._on_message({"type": "link", "href": "https://example.com"})
        self.assertEqual(self.h.opened, ["https://example.com"])

    def test_height_message_clamped(self):
        self.h._on_message(
            {"type": "height", "width": 440, "height": 99999, "rects": [[0, 0, 1, 2]]}
        )
        self.assertEqual(self.h.sizes, [(440, 8000, [(0, 0, 1, 2)])])

    def test_user_css_pushed(self):
        self.h.set_user_css("a{}")
        self.assertEqual(self.h.js[-1], 'window.nc.setUserCss("a{}")')

    def test_urgency_enum_in_payload(self):
        self.assertEqual(note(1, hints={"urgency": 2}).urgency, Urgency.CRITICAL)


class HoverHoldLogic(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.released = []
        self.hh = HoverHold(self.released.append, 1000, self.clock.schedule, self.clock.cancel)

    def test_not_hovered_closes_immediately(self):
        self.assertTrue(self.hh.expired(1))

    def test_expiry_during_hover_deferred_until_leave_plus_grace(self):
        self.hh.set_inside(True)
        self.assertFalse(self.hh.expired(1))
        self.hh.set_inside(False)
        self.assertEqual(self.released, [])  # grace still running
        self.assertEqual([ms for ms, _ in self.clock.jobs.values()], [1000])
        self.clock.fire_all()
        self.assertEqual(self.released, [1])
        self.assertFalse(self.hh.held)

    def test_reenter_during_grace_holds_again(self):
        self.hh.set_inside(True)
        self.hh.expired(1)
        self.hh.set_inside(False)
        self.hh.set_inside(True)
        self.assertEqual([ms for ms, _ in self.clock.jobs.values()], [120000])  # grace cancelled
        self.hh.set_inside(False)
        self.assertEqual(self.released, [])
        self.clock.fire_all()
        self.assertEqual(self.released, [1])

    def test_expiry_during_grace_is_deferred_too(self):
        self.hh.set_inside(True)
        self.hh.set_inside(False)
        self.assertFalse(self.hh.expired(2))
        self.clock.fire_all()
        self.assertEqual(self.released, [2])

    def test_grace_zero_and_configurable(self):
        self.hh.grace_ms = 250
        self.hh.set_inside(True)
        self.hh.set_inside(False)
        self.assertEqual([ms for ms, _ in self.clock.jobs.values()], [250])

    def test_forget_and_reset(self):
        self.hh.set_inside(True)
        self.hh.expired(1)
        self.hh.forget(1)
        self.assertEqual(self.hh.deferred(), [])
        self.hh.reset()
        self.assertFalse(self.hh.held)

    def test_duplicate_inside_messages_ignored(self):
        self.hh.set_inside(True)
        self.hh.set_inside(True)
        self.hh.set_inside(False)
        self.hh.set_inside(False)
        self.assertEqual(len(self.clock.jobs), 1)


class HostHover(HostCase):
    def hover(self, inside):
        self.h._on_message({"type": "hover", "inside": inside})

    def test_expiry_while_hovered_waits_then_reports_expired(self):
        self.h.show(note(1, expire=1000))
        self.hover(True)
        self.h.clock.fire_all()
        self.assertEqual(self.closed, [])
        self.assertNotIn("window.nc.close(1)", self.h.js)
        self.hover(False)
        self.assertEqual(self.closed, [])
        self.h.grace_clock.fire_all()
        self.assertEqual(self.closed, [(1, CloseReason.EXPIRED)])
        self.assertEqual(self.h.js[-1], "window.nc.close(1)")

    def test_hover_holds_every_toast(self):
        self.h.show(note(1, expire=1000))
        self.h.show(note(2, expire=2000))
        self.hover(True)
        self.h.clock.fire_all()
        self.hover(False)
        self.h.grace_clock.fire_all()
        self.assertEqual(sorted(self.closed), [(1, CloseReason.EXPIRED), (2, CloseReason.EXPIRED)])

    def test_reenter_during_grace_holds_again(self):
        self.h.show(note(1, expire=1000))
        self.hover(True)
        self.h.clock.fire_all()
        self.hover(False)
        self.hover(True)
        self.assertEqual([ms for ms, _ in self.h.grace_clock.jobs.values()], [120000])
        self.assertEqual(self.closed, [])

    def test_dismiss_action_close_work_while_hovered(self):
        self.h.show(note(1, expire=1000))
        self.h.show(note(2, expire=1000, actions=["ok", "OK"]))
        self.h.show(note(3, expire=1000))
        self.hover(True)
        self.h._on_message({"type": "closed", "id": 1})
        self.h._on_message({"type": "action", "id": 2, "key": "ok"})
        self.h.close(3)
        self.assertEqual(sorted(i for i, _ in self.closed), [1, 2])
        self.assertEqual(self.actions, [(2, "ok")])
        self.assertEqual(self.h._active, set())

    def test_dismissed_while_deferred_not_reported_again(self):
        self.h.show(note(1, expire=1000))
        self.hover(True)
        self.h.clock.fire_all()
        self.h._on_message({"type": "closed", "id": 1})
        self.hover(False)
        self.h.grace_clock.fire_all()
        self.assertEqual(self.closed, [(1, CloseReason.DISMISSED)])

    def test_replace_while_deferred_restarts_lifetime(self):
        self.h.show(note(1, expire=1000))
        self.hover(True)
        self.h.clock.fire_all()
        self.h.show(note(1, expire=5000))
        self.hover(False)
        self.h.grace_clock.fire_all()
        self.assertEqual(self.closed, [])
        self.assertEqual(self.h._timers.pending(), {1})

    def test_stack_emptied_resets_hover(self):
        self.h.show(note(1, expire=1000))
        self.hover(True)
        self.h._on_message({"type": "closed", "id": 1})
        self.h.show(note(2, expire=1000))
        self.h.clock.fire_all()
        self.assertEqual(self.closed[-1], (2, CloseReason.EXPIRED))

    def test_bad_hover_message_ignored(self):
        self.h.show(note(1, expire=1000))
        bad_msgs = [
            {"type": "hover"},
            {"type": "hover", "inside": 1},
            {"type": "hover", "inside": "y"},
        ]
        for bad in bad_msgs:
            self.h._on_message(bad)
        self.h.clock.fire_all()
        self.assertEqual(self.closed, [(1, CloseReason.EXPIRED)])


class FakeNow:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class GtkHover(unittest.TestCase):
    """HoverHold driven by GTK-style enter/leave/motion/hide, no display."""

    def setUp(self):
        self.clock = FakeClock()
        self.now = FakeNow()
        self.released = []
        self.hh = HoverHold(
            self.released.append,
            1000,
            self.clock.schedule,
            self.clock.cancel,
            max_hold_ms=120000,
            now=self.now,
            count=lambda: 2,
        )

    def delays(self):
        return sorted(ms for ms, _ in self.clock.jobs.values())

    def test_enter_holds_leave_releases_after_grace(self):
        self.hh.gtk_crossing(True)
        self.assertTrue(self.hh.held)
        self.assertFalse(self.hh.expired(1))
        self.hh.gtk_crossing(False)
        self.assertEqual(self.released, [])
        self.assertEqual(self.delays(), [1000])  # grace only; safety cancelled
        self.clock.fire_all()
        self.assertEqual(self.released, [1])
        self.assertFalse(self.hh.held)

    def test_expire_during_hold_then_close_on_leave_grace(self):
        self.hh.gtk_crossing(True)
        self.assertFalse(self.hh.expired(7))
        self.hh.gtk_crossing(False)
        self.clock.fire_all()
        self.assertEqual(self.released, [7])

    def test_inferior_leave_ignored(self):
        self.hh.gtk_crossing(True)
        self.hh.gtk_crossing(False, inferior=True)
        self.assertTrue(self.hh.inside)
        self.assertEqual(self.delays(), [120000])  # no grace timer started
        self.hh.gtk_crossing(True, inferior=True)  # child -> window: still inside
        self.assertTrue(self.hh.inside)

    def test_reenter_in_grace_holds(self):
        self.hh.gtk_crossing(True)
        self.hh.expired(1)
        self.hh.gtk_crossing(False)
        self.hh.gtk_crossing(True)
        self.clock.fire_all()  # only safety timer left
        self.assertEqual(self.released, [])

    def test_gtk_leave_overrides_js_inside(self):
        self.hh.set_inside(True, "js")
        self.hh.expired(1)
        self.hh.gtk_crossing(False)
        self.clock.fire_all()
        self.assertEqual(self.released, [1])

    def test_safety_release_after_max_hold(self):
        self.hh.gtk_crossing(True)
        self.hh.expired(1)
        self.assertEqual(self.delays(), [120000])
        self.now.t = 120.0
        with self.assertLogs("notifyd", "WARNING"):
            self.clock.fire_all()  # safety fires: leave never came
        self.assertFalse(self.hh.inside)
        self.assertEqual(self.released, [])  # grace still pending
        self.clock.fire_all()
        self.assertEqual(self.released, [1])

    def test_motion_refresh_prevents_safety_release(self):
        self.hh.gtk_crossing(True)
        self.now.t = 100.0
        self.hh.touch()  # motion
        self.now.t = 120.0
        self.clock.fire_all()  # timer fires 20 s after the last motion: re-arms
        self.assertTrue(self.hh.inside)
        self.assertEqual(self.delays(), [100000])
        self.now.t = 220.0
        with self.assertLogs("notifyd", "WARNING"):
            self.clock.fire_all()
        self.assertFalse(self.hh.inside)

    def test_safety_disabled_with_zero(self):
        self.hh.max_hold_ms = 0
        self.hh.gtk_crossing(True)
        self.assertEqual(self.clock.jobs, {})
        self.assertTrue(self.hh.inside)

    def test_set_max_hold_applies_live(self):
        self.hh.gtk_crossing(True)
        self.hh.set_max_hold(5000)
        self.assertEqual(self.delays(), [5000])
        self.hh.set_max_hold(0)
        self.assertEqual(self.clock.jobs, {})

    def test_hide_releases_hold_immediately(self):
        self.hh.gtk_crossing(True)
        self.hh.expired(1)
        self.hh.release_now("hide")
        self.assertEqual(self.released, [1])
        self.assertFalse(self.hh.held)
        self.assertEqual(self.clock.jobs, {})

    def test_release_now_when_idle_is_noop(self):
        self.hh.release_now("hide")
        self.assertEqual(self.released, [])

    def test_logs_only_state_changes(self):
        with self.assertLogs("notifyd", "INFO") as logs:
            self.hh.gtk_crossing(True)
            self.hh.touch()
            self.hh.set_inside(True, "js")  # duplicate
            self.hh.gtk_crossing(False)
            self.hh.gtk_crossing(False)  # duplicate
        text = "\n".join(r.getMessage() for r in logs.records)
        self.assertEqual(len(logs.records), 2)
        self.assertIn("hover enter source=gtk toasts=2", text)
        self.assertIn("hover leave source=gtk toasts=2", text)


class HostGtkHover(HostCase):
    def test_gtk_hold_defers_expiry_and_hide_releases(self):
        self.h.show(note(1, expire=1000))
        self.h._gtk_crossing(True, False, (10, 10))
        self.h.clock.fire_all()
        self.assertEqual(self.closed, [])
        self.h._on_window_hide()
        self.assertEqual(self.closed, [(1, CloseReason.EXPIRED)])

    def test_motion_without_enter_holds(self):
        self.h._gtk_motion((5, 5))
        self.assertTrue(self.h._hover.inside)

    def test_region_change_excluding_pointer_releases(self):
        self.h.show(note(1, expire=1000))
        self.h._rects = [(0, 0, 100, 50)]
        self.h._gtk_crossing(True, False, (10, 10))
        self.assertTrue(self.h._pointer_in_rects())
        self.h._rects = [(0, 60, 100, 50)]
        self.assertFalse(self.h._pointer_in_rects())
        self.h._rects = []
        self.h._pointer = None
        self.assertTrue(self.h._pointer_in_rects())  # unknown: keep hold

    def test_js_leave_still_works(self):
        self.h.show(note(1, expire=1000))
        self.h._on_message({"type": "hover", "inside": True})
        self.h.clock.fire_all()
        self.h._on_message({"type": "hover", "inside": False})
        self.h.grace_clock.fire_all()
        self.assertEqual(self.closed, [(1, CloseReason.EXPIRED)])


class HostClicks(HostCase):
    def setUp(self):
        super().setUp()
        self.details = []
        self.h.on_details = self.details.append

    def test_details_message_keeps_toast_open(self):
        self.h.show(note(1, expire=1000))
        self.h._on_message({"type": "details", "id": 1})
        self.assertEqual(self.details, [1])
        self.assertEqual((self.closed, self.actions), ([], []))
        self.assertEqual(self.h._active, {1})
        self.assertEqual(self.h._timers.pending(), {1})

    def test_details_for_unknown_id_ignored(self):
        self.h._on_message({"type": "details", "id": 9})
        self.h._on_message({"type": "details", "id": True})
        self.assertEqual(self.details, [])

    def test_default_action_runs_then_closes(self):
        self.h.show(note(1, actions=["default", "Open"]))
        self.h._on_message({"type": "action", "id": 1, "key": "default"})
        self.assertEqual(self.actions, [(1, "default")])
        self.assertEqual(self.closed, [(1, CloseReason.DISMISSED)])
        self.assertEqual(self.details, [])

    def test_details_click_without_callback_dismisses(self):
        self.h.on_details = None
        self.h.show(note(1, expire=1000))
        self.h._on_message({"type": "details", "id": 1})
        self.assertEqual(self.closed, [(1, CloseReason.DISMISSED)])
        self.assertEqual(self.h._active, set())
        self.assertEqual(self.h._timers.pending(), set())

    def test_on_details_defaults_to_none(self):
        h = FakeHost(WEB, lambda i, k: None, lambda i, r: None)
        self.assertIsNone(h.on_details)

    def test_close_button_only_dismisses(self):
        self.h.show(note(1))
        self.h._on_message({"type": "closed", "id": 1})
        self.assertEqual((self.details, self.closed), ([], [(1, CloseReason.DISMISSED)]))


class WebFiles(unittest.TestCase):
    def test_no_innerhtml(self):
        js = (WEB / "toasts.js").read_text()
        for bad in ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("]:
            self.assertNotIn(bad, js)

    def test_js_click_routing_and_hover(self):
        js = (WEB / "toasts.js").read_text()
        self.assertIn('{ type: "details", id: n.id }', js)
        self.assertIn('type: "hover"', js)
        self.assertIn("mouseenter", js)
        self.assertIn("mouseleave", js)
        self.assertIn("cursor: pointer;", (WEB / "default.css").read_text())

    def test_csp_present(self):
        html = (WEB / "toasts.html").read_text()
        self.assertIn(
            "default-src 'none'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; img-src file:",
            html,
        )

    def test_default_css_is_variables(self):
        css = (WEB / "default.css").read_text()
        for v in [
            "--nc-bg",
            "--nc-urgency-critical",
            "--nc-radius",
            "--nc-shadow",
            "--nc-opacity",
            "--nc-width",
            "--nc-gap",
            "--nc-font-family",
            "--nc-enter-animation",
            "--nc-exit-animation",
            "--nc-border-width",
        ]:
            self.assertIn(v + ":", css)


BUTTONS = [
    {"id": "b0", "label": "Open log", "run": ["/bin/secret-cmd", "--token=hunter2"], "close": True},
    {"id": "b1", "label": "Later", "builtin": "snooze", "minutes": 15, "close": True},
    {"id": "b2", "label": "Copy", "builtin": "copy-body", "close": False},
]


class RuleButtons(HostCase):
    def setUp(self):
        super().setUp()
        self.pressed = []
        self.h.on_button = lambda i, b: self.pressed.append((i, b))

    def shown(self, id=1):
        n = note(id)
        n.buttons = [dict(b) for b in BUTTONS]
        self.h.show(n)
        return json.loads(self.h.js[-1][len("window.nc.show(") : -1])

    def test_payload_has_buttons_without_commands(self):
        p = self.shown()
        self.assertTrue(p["buttons_enabled"])
        self.assertEqual([b["id"] for b in p["buttons"]], ["b0", "b1", "b2"])
        self.assertEqual(p["buttons"][0]["kind"], "run")
        self.assertEqual(
            p["buttons"][1],
            {
                "id": "b1",
                "label": "Later",
                "kind": "builtin",
                "builtin": "snooze",
                "minutes": 15,
                "close": True,
            },
        )
        self.assertNotIn("secret-cmd", self.h.js[-1])
        self.assertNotIn("hunter2", self.h.js[-1])

    def test_disabled_without_on_button(self):
        self.h.on_button = None
        self.assertFalse(self.shown()["buttons_enabled"])
        self.h._on_message({"type": "button", "id": 1, "button": "b0"})
        self.assertEqual(self.pressed, [])

    def test_kwarg(self):
        h = FakeHost(WEB, lambda i, k: None, lambda i, r: None, on_button=lambda i, b: None)
        self.assertIsNotNone(h.on_button)
        self.assertIsNone(FakeHost(WEB, lambda i, k: None, lambda i, r: None).on_button)

    def test_valid_press_routes_and_keeps_the_toast(self):
        self.shown()
        self.h._on_message({"type": "button", "id": 1, "button": "b1"})
        self.assertEqual(self.pressed, [(1, "b1")])
        self.assertEqual(self.closed, [])
        self.assertIn(1, self.h._active)

    def test_rejects_bad_presses(self):
        self.shown()
        for msg in (
            {"type": "button", "id": 1, "button": "b9"},  # unknown button
            {"type": "button", "id": 1, "button": 0},  # not a string
            {"type": "button", "id": 1},
            {"type": "button", "id": "1", "button": "b0"},  # non-int id
            {"type": "button", "id": True, "button": "b0"},
            {"type": "button", "id": 2, "button": "b0"},  # not an active toast
        ):
            self.h._on_message(msg)
        self.assertEqual(self.pressed, [])

    def test_closed_toast_forgets_its_buttons(self):
        self.shown()
        self.h.close(1)
        self.h._on_message({"type": "button", "id": 1, "button": "b0"})
        self.assertEqual(self.pressed, [])


if __name__ == "__main__":
    unittest.main()
