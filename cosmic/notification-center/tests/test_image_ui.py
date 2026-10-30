"""Image previews, the UI side (`notifyd.image_ui`) on all three hosts: payloads, the nc-image:
URL handler's validation, the three page messages (accepted and rejected), the config key.
No display, no GTK clipboard, no real wl-copy."""

import json
import re
import tempfile
import unittest
from pathlib import Path

from notifyd import image_ui
from notifyd.center import CenterBridge
from notifyd.config import BY_KEY, Config, load_config
from notifyd.image_ui import ImageService, image_uri, parse_uri, register_scheme
from notifyd.model import Notification
from notifyd.shade import ShadeBridge, ShadeState
from notifyd.store import Store
from tests.test_images import PNG, FakeProc
from tests.test_toasts import WEB, FakeHost


def mk(id, body="", hints=None, app="App"):
    return Notification.from_notify(id, app, 0, "", "Sum", body, [], hints or {}, -1, 100.0)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name).resolve()
        self.shot = self.dir / "Screenshot_2026-10-27_09-43-40.png"
        self.shot.write_bytes(PNG)
        self.store = Store(":memory:")
        self.enabled = True
        self.copied, self.opened, self.spawned, self.gtk = [], [], [], []
        self.js: list[str] = []
        self.svc = ImageService(
            self.store.get,
            enabled=lambda: self.enabled,
            copy_text=self.copied.append,
            open_uri=self.opened.append,
            popen=self.popen,
            which=lambda n: "/usr/bin/wl-copy",
            gtk_copy=lambda p: self.gtk.append(p) or True,
        )

    def popen(self, argv, **kw):
        self.spawned.append((argv, kw))
        return FakeProc(0)

    def add(self, id=1, **kw):
        n = mk(id, **kw)
        self.store.record(n)
        return n

    def screenshot(self, id=1):
        """The COSMIC Screenshot notification, as the real one arrives."""
        return self.add(id, body=str(self.shot), hints={"transient": True}, app="COSMIC Screenshot")

    def results(self):
        out = []
        for js in self.js:
            m = re.fullmatch(r"window\.nc\.imageResult\((.*)\)", js, re.S)
            if m:
                out.append(json.loads(m[1]))
        return out


class Uris(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_uri("nc-image://img/12/1a2b-ff"), 12)
        self.assertEqual(parse_uri("nc-image://img/0/0-0"), 0)
        self.assertEqual(parse_uri(f"nc-image://img/{2**32 - 1}/a"), 2**32 - 1)

    def test_parse_rejects(self):
        for bad in (
            None,
            5,
            "",
            "file:///etc/passwd",
            "nc-image://img/1",
            "nc-image://img/1/",
            "nc-image://img/-1/a",
            "nc-image://img/a/a",
            "nc-image://img/1/zz",
            "nc-image://img/1/a/b",
            "nc-image://img/1/a?x=1",
            "nc-image://img/1/a#f",
            "nc-image://other/1/a",
            "nc-image://img/../etc/passwd",
            "nc-image://img//etc/passwd",
            "nc-image://img/1/../../x",
            "nc-image://img/%2e%2e/a",
            "nc-image://img/1/a\n",
            f"nc-image://img/{2**32}/a",
            "nc-image://img/1/" + "a" * 100,
            "NC-IMAGE://img/1/a",
            "http://img/1/a",
        ):
            self.assertIsNone(parse_uri(bad), repr(bad))


class Service(Base):
    def test_info_for_the_cosmic_screenshot(self):
        n = self.screenshot()
        info = self.svc.info(n)
        self.assertEqual(set(info), {"uri", "mime", "name"})
        self.assertEqual(info["mime"], "image/png")
        self.assertEqual(info["name"], self.shot.name)
        self.assertEqual(parse_uri(info["uri"]), 1)
        self.assertNotIn(str(self.dir), json.dumps(info))  # the page never sees the path

    def test_no_image_no_info(self):
        self.assertIsNone(self.svc.info(self.add(2, body="hello")))
        self.assertIsNone(self.svc.info(self.add(3, body=str(self.dir / "missing.png"))))
        self.assertIsNone(self.svc.info(None))

    def test_the_switch_turns_everything_off(self):
        n = self.screenshot()
        self.enabled = False
        self.assertIsNone(self.svc.info(n))
        self.assertIsNone(self.svc.serve(image_uri(1, images_ref(n))))
        self.svc.handle({"type": "image_copy", "id": 1}, self.js.append)
        self.assertEqual(
            (self.spawned, self.results()), ([], [{"id": 1, "kind": "image_copy", "ok": False}])
        )

    def test_serve_validates_at_request_time(self):
        n = self.screenshot()
        uri = self.svc.info(n)["uri"]
        self.assertEqual(self.svc.serve(uri), (PNG, "image/png"))
        # a different version token is the same image (it only busts the cache)
        self.assertEqual(self.svc.serve("nc-image://img/1/0-0"), (PNG, "image/png"))
        self.shot.write_bytes(b"now it is text")  # changed after the payload went out
        self.assertIsNone(self.svc.serve(uri))
        self.shot.unlink()
        self.assertIsNone(self.svc.serve(uri))

    def test_serve_only_what_that_notification_resolves(self):
        self.screenshot(1)
        self.add(2, body="no image here")
        self.assertIsNone(self.svc.serve("nc-image://img/2/0-0"))  # a notification without one
        self.assertIsNone(self.svc.serve("nc-image://img/99/0-0"))  # unknown id
        for bad in ("file://" + str(self.shot), str(self.shot), "nc-image://img/1/../x"):
            self.assertIsNone(self.svc.serve(bad), bad)

    def test_serve_never_serves_a_non_image_a_hint_points_at(self):
        secret = self.dir / "secret.png"
        secret.write_text("password")
        self.add(5, hints={"image-path": str(secret)})
        (self.dir / "link.png").symlink_to(self.dir / "secret.png")
        self.add(6, body=str(self.dir / "link.png"))
        self.assertIsNone(self.svc.serve("nc-image://img/5/0-0"))
        self.assertIsNone(self.svc.serve("nc-image://img/6/0-0"))

    def test_copy_image_spawns_wl_copy_with_the_mime(self):
        self.screenshot()
        self.svc.handle({"type": "image_copy", "id": 1}, self.js.append)
        ((argv, kw),) = self.spawned
        self.assertEqual(argv, ["/usr/bin/wl-copy", "--type", "image/png"])
        self.assertFalse(kw.get("shell", False))
        self.assertEqual(self.results(), [{"id": 1, "kind": "image_copy", "ok": True}])

    def test_copy_image_falls_back_to_gtk_without_wl_copy(self):
        self.screenshot()
        self.svc.which = lambda n: None
        self.svc.handle({"type": "image_copy", "id": 1}, self.js.append)
        self.assertEqual((self.spawned, self.gtk), ([], [str(self.shot)]))
        self.assertTrue(self.results()[0]["ok"])

    def test_copy_path_uses_the_text_clipboard_with_the_senders_path(self):
        link = self.dir / "link.png"
        link.symlink_to(self.shot)
        self.add(4, body=str(link))
        self.svc.handle({"type": "image_copy_path", "id": 4}, self.js.append)
        self.assertEqual(self.copied, [str(link)])
        self.assertTrue(self.results()[0]["ok"])

    def test_open_launches_the_file_uri(self):
        self.screenshot()
        self.svc.handle({"type": "image_open", "id": 1}, self.js.append)
        self.assertEqual(self.opened, ["file://" + str(self.shot)])

    def test_a_failing_action_answers_not_ok(self):
        self.screenshot()

        def boom(_t):
            raise RuntimeError("x")

        self.svc.copy_text = boom
        with self.assertLogs("notifyd.images", "ERROR"):
            self.svc.handle({"type": "image_copy_path", "id": 1}, self.js.append)
        self.assertFalse(self.results()[0]["ok"])

    def test_junk_is_dropped_without_an_answer(self):
        self.screenshot()
        for bad in (
            {"type": "image_copy"},
            {"type": "image_copy", "id": True},
            {"type": "image_copy", "id": "1"},
            {"type": "image_copy", "id": -1},
            {"type": "image_copy", "id": 2**32},
            {"type": "image_copy", "id": [1]},
            {"type": "image_delete", "id": 1},
            {"type": None, "id": 1},
            {"id": 1},
        ):
            self.svc.handle(bad, self.js.append)
        self.assertEqual((self.js, self.spawned, self.copied, self.opened), ([], [], [], []))

    def test_a_valid_id_without_an_image_answers_not_ok(self):
        self.add(3, body="hi")
        for kind in image_ui.KINDS:
            self.svc.handle({"type": kind, "id": 3}, self.js.append)
        self.assertEqual([r["ok"] for r in self.results()], [False] * 3)
        self.assertEqual((self.spawned, self.copied, self.opened), ([], [], []))


def images_ref(n):
    from notifyd import images

    return images.resolve(n.hints, n.body)


class FakeContext:
    def __init__(self):
        self.calls = []

    def register_uri_scheme(self, scheme, cb, data):
        self.calls.append((scheme, cb, data))


class Scheme(Base):
    def test_registered_once_per_context_as_nc_image(self):
        ctx = FakeContext()
        self.assertTrue(register_scheme(self.svc, ctx))
        self.assertFalse(register_scheme(self.svc, ctx))
        ((scheme, cb, data),) = ctx.calls
        self.assertEqual((scheme, data), ("nc-image", self.svc))
        self.assertIs(cb, image_ui._on_request)

    def test_request_handler_answers_with_bytes_or_an_error(self):
        self.screenshot()

        class Req:
            def __init__(self, uri):
                self.uri, self.out = uri, None

            def get_uri(self):
                return self.uri

            def finish(self, stream, length, mime):
                self.out = ("ok", stream.read_bytes(length, None).get_data(), length, mime)

            def finish_error(self, err):
                self.out = ("error", err.message)

        ok = Req(self.svc.info(self.store.get(1))["uri"])
        image_ui._on_request(ok, self.svc)
        self.assertEqual(ok.out, ("ok", PNG, len(PNG), "image/png"))
        for uri in ("nc-image://img/9/0-0", "file:///etc/passwd"):
            bad = Req(uri)
            image_ui._on_request(bad, self.svc)
            self.assertEqual(bad.out[0], "error", uri)

    def test_views_are_built_on_the_default_context_the_scheme_goes_on(self):
        for f in ("toasts.py", "center.py", "shade.py"):
            src = (Path(__file__).resolve().parent.parent / "notifyd" / f).read_text()
            self.assertIn("WebKit2.WebView.new_with_user_content_manager(ucm)", src, f)
            self.assertLess(
                src.index("register_scheme("), src.index("new_with_user_content_manager")
            )

    def test_csp_allows_only_the_one_new_scheme(self):
        for page in ("toasts.html", "shade.html", "center.html"):
            html = (WEB / page).read_text()
            self.assertIn("img-src file: nc-image:", html, page)
            self.assertIn("default-src 'none'", html)
            self.assertIn('<script src="image.js"></script>', html, page)


class CenterSide(Base):
    def setUp(self):
        super().setUp()
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
        )

    def test_off_without_a_service(self):
        self.screenshot()
        self.assertIsNone(self.bridge.detail_payload(1)["notification"]["image"])
        self.bridge.handle({"type": "image_copy", "id": 1})
        self.assertEqual(self.js, [])

    def test_list_added_and_detail_carry_the_image(self):
        self.bridge.images = self.svc
        n = self.screenshot()
        self.add(2, body="plain")
        from notifyd.center import ListQuery

        items = {i["id"]: i for i in self.bridge.list_payload(ListQuery())["items"]}
        self.assertEqual(items[1]["image"]["mime"], "image/png")
        self.assertIsNone(items[2]["image"])
        self.assertEqual(self.bridge.added_item(n)["image"], items[1]["image"])
        self.assertEqual(self.bridge.detail_payload(1)["notification"]["image"], items[1]["image"])

    def test_messages(self):
        self.bridge.images = self.svc
        self.screenshot()
        for kind in image_ui.KINDS:
            self.bridge.handle({"type": kind, "id": 1})
        self.assertEqual([r["ok"] for r in self.results()], [True] * 3)
        self.assertEqual(
            (len(self.spawned), self.copied, len(self.opened)), (1, [str(self.shot)], 1)
        )
        n = len(self.js)
        for bad in ({"type": "image_copy", "id": "1"}, {"type": "image_copy"}, {"type": "image_x"}):
            self.bridge.handle(bad)
        self.bridge.handle("junk")
        self.assertEqual(len(self.js), n)


class ShadeSide(Base):
    def setUp(self):
        super().setUp()
        self.bridge = ShadeBridge(
            self.store,
            ShadeState(self.dir / "shade.json"),
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
        )

    def items(self):
        return {i["id"]: i for g in self.bridge.payload()["groups"] for i in g["items"]}

    def test_rows_carry_the_image_only_with_a_service(self):
        self.screenshot()
        self.add(2, body="plain")
        self.assertIsNone(self.items()[1]["image"])
        self.bridge.images = self.svc
        items = self.items()
        self.assertEqual(items[1]["image"]["mime"], "image/png")
        self.assertIsNone(items[2]["image"])

    def test_messages_and_junk(self):
        self.bridge.images = self.svc
        self.screenshot()
        self.bridge.handle({"type": "image_copy_path", "id": 1})
        self.assertEqual(self.copied, [str(self.shot)])
        n = len(self.js)
        for bad in ({"type": "image_copy_path", "id": None}, {"type": "image_copy", "id": 1.5}):
            self.bridge.handle(bad)
        self.assertEqual(len(self.js), n)


class ToastSide(Base):
    def setUp(self):
        super().setUp()
        self.closed, self.actions, self.details = [], [], []
        self.host = FakeHost(
            WEB,
            lambda i, k: self.actions.append((i, k)),
            lambda i, r: self.closed.append((i, r)),
            on_details=self.details.append,
        )
        self.host.images = self.svc

    def shown(self):
        script = next(s for s in self.host.js if s.startswith("window.nc.show("))
        return json.loads(script[len("window.nc.show(") : -1])

    def test_payload_has_the_image_or_null(self):
        self.host.show(self.screenshot())
        self.assertEqual(self.shown()["image"]["mime"], "image/png")
        self.host.js.clear()
        self.host.images = None
        self.host.show(self.screenshot())
        self.assertIsNone(self.shown()["image"])

    def test_buttons_do_not_dismiss_or_act(self):
        self.host.show(self.screenshot())
        before = self.host._hover.inside
        for kind in image_ui.KINDS:
            self.host._on_message({"type": kind, "id": 1})
        self.assertEqual((self.closed, self.actions, self.details), ([], [], []))
        self.assertIn(1, self.host._active)
        self.assertEqual(self.host._hover.inside, before)
        self.assertEqual([r["ok"] for r in self.results_of(self.host.js)], [True] * 3)
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.copied, [str(self.shot)])

    def results_of(self, js):
        self.js = js
        return self.results()

    def test_only_toasts_on_screen_and_valid_ids(self):
        self.screenshot(7)  # in the store, but never shown as a toast
        self.host.show(self.screenshot(1))
        n = len(self.host.js)
        for bad in (
            {"type": "image_copy", "id": 7},
            {"type": "image_copy", "id": "1"},
            {"type": "image_copy"},
            {"type": "image_copy", "id": True},
        ):
            self.host._on_message(bad)
        self.assertEqual((len(self.host.js), self.spawned), (n, []))

    def test_off_without_a_service(self):
        self.host.show(self.screenshot())
        self.host.images = None
        n = len(self.host.js)
        self.host._on_message({"type": "image_copy", "id": 1})
        self.assertEqual(len(self.host.js), n)


class ConfigKey(unittest.TestCase):
    def test_default_and_validation(self):
        self.assertTrue(Config().images_preview)
        f = BY_KEY[("images", "preview")]
        self.assertFalse(f.restart)
        self.assertEqual((f.check(False), f.type), (False, "bool"))
        for bad in ("false", 0, 1, None):
            with self.assertRaises(ValueError):
                f.check(bad)

    def test_loader(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            p.write_text("[images]\npreview = false\n")
            self.assertFalse(load_config(p).images_preview)
            p.write_text('[images]\npreview = "no"\n')
            with self.assertLogs("notifyd", "ERROR"):
                self.assertTrue(load_config(p).images_preview)


if __name__ == "__main__":
    unittest.main()
