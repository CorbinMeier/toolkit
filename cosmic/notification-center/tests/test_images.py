"""Image previews, the pure part (`notifyd.images`): finding the image, validating it, the copy
command. Real temp files, no GTK, no display; wl-copy is never run (the spawn is stubbed)."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from notifyd import images

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 24
GIF = b"GIF89a" + b"\x00" * 24
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 16
BMP = b"BM" + b"\x00" * 24


class Files(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name).resolve()

    def write(self, name, data=PNG):
        p = self.dir / name
        p.write_bytes(data)
        return str(p)


class Sniff(unittest.TestCase):
    def test_each_format(self):
        for data, mime in (
            (PNG, "image/png"),
            (JPEG, "image/jpeg"),
            (GIF, "image/gif"),
            (WEBP, "image/webp"),
            (BMP, "image/bmp"),
        ):
            self.assertEqual(images.sniff(data[: images.HEAD]), mime)

    def test_not_images(self):
        for data in (b"", b"<svg xmlns=", b"hello world", b"RIFF\x00\x00\x00\x00WAVE", b"%PDF-1.4"):
            self.assertIsNone(images.sniff(data), data)


class ToPath(unittest.TestCase):
    def test_accepts_absolute_path_and_file_uri(self):
        self.assertEqual(images.to_path("/a/b.png"), "/a/b.png")
        self.assertEqual(images.to_path("  /a/b.png \n"), "/a/b.png")
        self.assertEqual(images.to_path("/a/My Shots/b.png"), "/a/My Shots/b.png")
        self.assertEqual(images.to_path("file:///a/b%20c.png"), "/a/b c.png")
        self.assertEqual(images.to_path("file://localhost/a/b.png"), "/a/b.png")

    def test_rejects_everything_else(self):
        for bad in (
            None,
            5,
            "",
            "   ",
            "b.png",
            "~/b.png",
            "./b.png",
            "http://x/b.png",
            "file://host/a/b.png",
            "file:///a/b.png?x=1",
            "file:///a/b.png#f",
            "file:///a/%00b.png",
            "file:///a/%0Ab.png",
            "/a/b.png\n/a/c.png",
            "/a/b.png\x00",
            "Screenshot saved to: /a/b.png",
            "/" + "a" * images.MAX_PATH,
        ):
            self.assertIsNone(images.to_path(bad), repr(bad))

    def test_candidates_order_and_sources(self):
        hints = {"image_path": "/h2.png", "image-path": "/h1.png"}
        self.assertEqual(images.candidates(hints, " /b.png "), ["/h1.png", "/h2.png", "/b.png"])
        self.assertEqual(images.candidates({"image-path": "dialog-info"}, "text"), [])
        self.assertEqual(images.candidates({"image-path": b"/x.png"}, ""), [])


class Resolve(Files):
    def test_hint_plain_path(self):
        p = self.write("a.png")
        ref = images.resolve({"image-path": p}, "")
        self.assertEqual((ref.path, ref.shown, ref.mime, ref.size), (p, p, "image/png", len(PNG)))
        self.assertEqual(ref.name, "a.png")

    def test_hint_alt_spelling_and_file_uri(self):
        p = self.write("a b.png")
        ref = images.resolve({"image_path": "file://" + p.replace(" ", "%20")}, "x")
        self.assertEqual(ref.path, p)

    def test_body_that_is_exactly_one_path(self):
        p = self.write("Screenshot_2026-10-27_09-43-40.png")
        for body in (p, f"  {p}\n", "file://" + p):
            self.assertEqual(images.resolve({"transient": True}, body).path, p, body)

    def test_body_with_other_text_is_no_image(self):
        p = self.write("a.png")
        self.assertIsNone(images.resolve({}, f"Saved to {p}"))
        self.assertIsNone(images.resolve({}, f"{p}\n{p}"))

    def test_hint_wins_then_body_is_the_fallback(self):
        a, b = self.write("a.png"), self.write("b.jpg", JPEG)
        self.assertEqual(images.resolve({"image-path": a}, b).path, a)
        self.assertEqual(images.resolve({"image-path": "/nope/x.png"}, b).path, b)
        self.assertEqual(images.resolve({"image-path": "bell"}, b).mime, "image/jpeg")

    def test_every_format_by_extension(self):
        for name, data, mime in (
            ("a.png", PNG, "image/png"),
            ("a.JPG", JPEG, "image/jpeg"),
            ("a.jpeg", JPEG, "image/jpeg"),
            ("a.gif", GIF, "image/gif"),
            ("a.webp", WEBP, "image/webp"),
            ("a.bmp", BMP, "image/bmp"),
        ):
            self.assertEqual(images.resolve({}, self.write(name, data)).mime, mime, name)

    def test_non_image_extension(self):
        for name in ("a.txt", "a.svg", "a", "a.png.txt", "a.pdf"):
            self.assertIsNone(images.resolve({}, self.write(name)), name)

    def test_missing_and_deleted(self):
        self.assertIsNone(images.resolve({}, str(self.dir / "missing.png")))
        p = self.write("gone.png")
        self.assertIsNotNone(images.resolve({}, p))
        os.unlink(p)
        self.assertIsNone(images.resolve({}, p))

    def test_directory_named_like_an_image(self):
        d = self.dir / "dir.png"
        d.mkdir()
        self.assertIsNone(images.resolve({}, str(d)))

    def test_fifo_is_not_opened(self):
        f = self.dir / "pipe.png"
        os.mkfifo(f)
        self.assertIsNone(images.resolve({}, str(f)))

    def test_wrong_magic(self):
        self.assertIsNone(images.resolve({}, self.write("fake.png", b"not an image at all")))
        self.assertIsNone(images.resolve({}, self.write("svg.png", b"<svg onload=x></svg>")))
        self.assertIsNone(images.resolve({}, self.write("mismatch.png", JPEG)))
        self.assertIsNone(images.resolve({}, self.write("empty.png", b"")))

    def test_symlink_to_a_non_image(self):
        secret = self.write("secret.txt", b"password")
        link = self.dir / "shot.png"
        link.symlink_to(secret)
        self.assertIsNone(images.resolve({}, str(link)))
        self.assertIsNone(images.resolve({"image-path": str(link)}, ""))

    def test_symlink_to_a_real_image_resolves_to_its_target(self):
        real = self.write("real.png")
        link = self.dir / "link.png"
        link.symlink_to(real)
        ref = images.resolve({}, str(link))
        self.assertEqual((ref.path, ref.shown), (real, str(link)))

    def test_oversize(self):
        p = self.dir / "big.png"
        with open(p, "wb") as f:
            f.write(PNG)
            f.truncate(images.MAX_BYTES + 1)
        self.assertIsNone(images.resolve({}, str(p)))
        with open(p, "r+b") as f:
            f.truncate(images.MAX_BYTES)
        self.assertEqual(images.resolve({}, str(p)).size, images.MAX_BYTES)

    def test_none_hints_and_body_are_fine(self):
        self.assertIsNone(images.resolve(None, None))


class Read(Files):
    def test_reads_the_verified_bytes(self):
        ref = images.resolve({}, self.write("a.png"))
        self.assertEqual(images.read_image(ref), PNG)

    def test_file_changed_after_resolve(self):
        p = self.write("a.png")
        ref = images.resolve({}, p)
        Path(p).write_bytes(b"now some text")
        self.assertIsNone(images.read_image(ref))
        Path(p).write_bytes(JPEG)
        self.assertIsNone(images.read_image(ref))
        os.unlink(p)
        self.assertIsNone(images.read_image(ref))

    def test_grown_past_the_limit(self):
        p = self.write("a.png")
        ref = images.resolve({}, p)
        with open(p, "r+b") as f:
            f.truncate(images.MAX_BYTES + 5)
        self.assertIsNone(images.read_image(ref))

    def test_replaced_by_a_symlink_is_not_followed(self):
        p = self.write("a.png")
        ref = images.resolve({}, p)
        other = self.write("other.png")
        os.unlink(p)
        os.symlink(other, p)
        self.assertIsNone(images.read_image(ref))


class FakeProc:
    def __init__(self, code=0, timeout=False):
        self.code, self.timeout, self.killed = code, timeout, False

    def wait(self, timeout=None):
        if self.timeout and not self.killed:
            raise subprocess.TimeoutExpired("wl-copy", timeout)
        return self.code

    def kill(self):
        self.killed = True


class CopyImage(Files):
    def setUp(self):
        super().setUp()
        self.ref = images.resolve({}, self.write("a.png"))
        self.calls = []

    def popen(self, code=0, timeout=False):
        def fake(argv, **kw):
            kw["stdin_data"] = kw["stdin"].read()  # the file really is on stdin
            self.calls.append((argv, kw))
            return FakeProc(code, timeout)

        return fake

    def test_argv_has_the_type_and_never_the_path(self):
        which = {"wl-copy": "/usr/bin/wl-copy"}.get
        self.assertEqual(
            images.copy_argv("image/png", which), ["/usr/bin/wl-copy", "--type", "image/png"]
        )
        self.assertIsNone(images.copy_argv("image/png", lambda n: None))

    def test_spawns_wl_copy_without_a_shell_and_feeds_the_file(self):
        ok = images.copy_image(
            self.ref, popen=self.popen(), which=lambda n: "/usr/bin/wl-copy", fallback=None
        )
        self.assertTrue(ok)
        ((argv, kw),) = self.calls
        self.assertEqual(argv, ["/usr/bin/wl-copy", "--type", "image/png"])
        self.assertIsInstance(argv, list)
        self.assertFalse(kw.get("shell", False))
        self.assertEqual(kw["stdin_data"], PNG)
        self.assertEqual(kw["stdout"], subprocess.DEVNULL)
        self.assertNotIn(self.ref.path, argv)

    def test_missing_wl_copy_uses_the_fallback(self):
        got = []
        ok = images.copy_image(
            self.ref,
            popen=self.popen(),
            which=lambda n: None,
            fallback=lambda p: got.append(p) or True,
        )
        self.assertTrue(ok)
        self.assertEqual((self.calls, got), ([], [self.ref.path]))

    def test_nothing_available_is_a_failure(self):
        self.assertFalse(images.copy_image(self.ref, popen=self.popen(), which=lambda n: None))

    def test_wl_copy_failing_or_hanging_falls_back(self):
        which = lambda n: "/usr/bin/wl-copy"  # noqa: E731
        got = []
        fb = lambda p: got.append(p) or True  # noqa: E731
        self.assertTrue(images.copy_image(self.ref, popen=self.popen(1), which=which, fallback=fb))
        hang = FakeProc(timeout=True)
        self.assertTrue(
            images.copy_image(self.ref, popen=lambda argv, **kw: hang, which=which, fallback=fb)
        )
        self.assertTrue(hang.killed)
        self.assertEqual(len(got), 2)

    def test_file_gone_is_a_failure_and_spawns_nothing(self):
        os.unlink(self.ref.path)
        ok = images.copy_image(self.ref, popen=self.popen(), which=lambda n: "/usr/bin/wl-copy")
        self.assertFalse(ok)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
