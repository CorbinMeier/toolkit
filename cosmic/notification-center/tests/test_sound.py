import os
import tempfile
import unittest
from pathlib import Path

from notifyd.model import Notification
from notifyd.sound import Resolved, SoundPlayer, choose


def make(hints=None, sound=None, volume=None):
    n = Notification.from_notify(1, "app", 0, "", "s", "b", [], hints or {}, -1, 0.0)
    n.sound = sound
    if volume is not None:
        n.sound_volume = volume
    return n


class FakeProc:
    def __init__(self, cmd):
        self.cmd = cmd
        self.terminated = False
        self.done = False

    def poll(self):
        return 0 if (self.done or self.terminated) else None

    def terminate(self):
        self.terminated = True

    def wait(self):
        return 0


class Spawner:
    def __init__(self, fail=()):
        self.procs = []
        self.fail = set(fail)

    def __call__(self, cmd, **kw):
        if cmd[0] in self.fail:
            raise FileNotFoundError(cmd[0])
        p = FakeProc(cmd)
        self.procs.append(p)
        return p


ALL = {"pw-play", "paplay", "canberra-gtk-play"}


def which_of(names):
    return lambda name: f"/usr/bin/{name}" if name in names else None


class ChooseTests(unittest.TestCase):
    def test_default(self):
        self.assertEqual(choose(make()), (None, None))

    def test_rule_sound_wins(self):
        n = make({"suppress-sound": True, "sound-name": "x"}, sound="/a.wav", volume=40)
        self.assertEqual(choose(n), ("/a.wav", 40))

    def test_rule_none(self):
        self.assertEqual(choose(make({"sound-name": "x"}, sound="none"))[0], "none")

    def test_suppress_beats_hints(self):
        n = make({"suppress-sound": True, "sound-file": "/a.wav", "sound-name": "x"})
        self.assertEqual(choose(n)[0], "none")

    def test_suppress_false(self):
        self.assertEqual(choose(make({"suppress-sound": False, "sound-name": "x"}))[0], "x")

    def test_file_beats_name(self):
        n = make({"sound-file": "file:///a.wav", "sound-name": "x"})
        self.assertEqual(choose(n)[0], "file:///a.wav")

    def test_name(self):
        self.assertEqual(choose(make({"sound-name": "bell"}))[0], "bell")

    def test_default_volume_is_none(self):
        n = make()
        self.assertIsNone(n.sound_volume)
        self.assertIsNone(choose(n)[1])

    def test_set_volume_is_returned(self):
        n = make()
        n.sound_volume = 35
        self.assertEqual(choose(n), (None, 35))


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wav = Path(self.tmp.name) / "a b.wav"
        self.wav.write_bytes(b"x")

    def test_default_theme(self):
        r = SoundPlayer().resolve(None)
        self.assertEqual(r, Resolved("theme", "message-new-instant", 100))

    def test_none(self):
        self.assertEqual(SoundPlayer().resolve("none", 50), Resolved("none", "", 50))

    def test_theme_name(self):
        self.assertEqual(SoundPlayer().resolve("bell").kind, "theme")

    def test_file(self):
        p = SoundPlayer()
        self.assertEqual(p.resolve(str(self.wav)), Resolved("file", str(self.wav), 100))
        self.assertIsNone(p.last_error)

    def test_extension_only_is_path(self):
        p = SoundPlayer()
        r = p.resolve("nonexistent.mp3")
        self.assertEqual(r.kind, "theme")
        self.assertIn("nonexistent.mp3", p.last_error)

    def test_file_uri_with_escape(self):
        uri = "file://" + str(self.wav).replace(" ", "%20")
        self.assertEqual(SoundPlayer().resolve(uri).value, str(self.wav))

    def test_tilde(self):
        old = os.environ["HOME"]
        os.environ["HOME"] = self.tmp.name
        self.addCleanup(os.environ.__setitem__, "HOME", old)
        self.assertEqual(SoundPlayer().resolve("~/a b.wav").value, str(self.wav))

    def test_missing_falls_back_to_default(self):
        p = SoundPlayer(default="bell")
        r = p.resolve("/no/such.wav")
        self.assertEqual(r, Resolved("theme", "bell", 100))
        self.assertIn("/no/such.wav", p.last_error)

    def test_missing_falls_back_to_default_file(self):
        p = SoundPlayer(default=str(self.wav))
        self.assertEqual(p.resolve("/no/such.wav").value, str(self.wav))

    def test_unreadable(self):
        self.wav.chmod(0)
        self.addCleanup(self.wav.chmod, 0o644)
        if os.access(self.wav, os.R_OK):
            self.skipTest("running as root")
        p = SoundPlayer()
        self.assertEqual(p.resolve(str(self.wav)).kind, "theme")
        self.assertIsNotNone(p.last_error)

    def test_directory_is_not_a_file(self):
        p = SoundPlayer()
        self.assertEqual(p.resolve(self.tmp.name).kind, "theme")
        self.assertIsNotNone(p.last_error)

    def test_broken_default_goes_to_theme(self):
        p = SoundPlayer(default="/no/default.ogg")
        self.assertEqual(p.resolve(None), Resolved("theme", "message-new-instant", 100))
        self.assertIn("default.ogg", p.last_error)

    def test_last_error_kept_after_success(self):
        p = SoundPlayer()
        p.resolve("/no/such.wav")
        p.resolve(str(self.wav))
        self.assertIn("such.wav", p.last_error)

    def test_default_none(self):
        self.assertEqual(SoundPlayer(default="none").resolve(None).kind, "none")

    def test_volume_clamp_and_master(self):
        p = SoundPlayer()
        self.assertEqual(p.resolve("bell", 250).volume, 100)
        self.assertEqual(p.resolve("bell", -5).volume, 0)
        p.set_master_volume(50)
        self.assertEqual(p.resolve("bell", 80).volume, 40)
        self.assertEqual(p.resolve("bell").volume, 50)
        p.set_master_volume(500)
        self.assertEqual(p.resolve("bell", 70).volume, 70)
        p.set_master_volume(-1)
        self.assertEqual(p.resolve("bell").volume, 0)

    def test_set_default(self):
        p = SoundPlayer()
        p.set_default("bell")
        self.assertEqual(p.resolve(None).value, "bell")


class PlayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wav = Path(self.tmp.name) / "a.wav"
        self.wav.write_bytes(b"x")

    def player(self, names=ALL, fail=()):
        self.sp = Spawner(fail)
        return SoundPlayer(spawn=self.sp, which=which_of(names))

    def test_pw_play_file(self):
        p = self.player()
        r = p.play(str(self.wav), 50)
        self.assertEqual(r.kind, "file")
        self.assertEqual(self.sp.procs[0].cmd, ["pw-play", "--volume=0.50", str(self.wav)])

    def test_paplay_fallback_when_missing(self):
        p = self.player({"paplay"})
        p.play(str(self.wav), 50)
        self.assertEqual(self.sp.procs[0].cmd, ["paplay", "--volume=32768", str(self.wav)])

    def test_paplay_fallback_when_spawn_fails(self):
        p = self.player(fail={"pw-play"})
        p.play(str(self.wav))
        self.assertEqual(self.sp.procs[0].cmd[0], "paplay")
        self.assertEqual(self.sp.procs[0].cmd[1], "--volume=65536")

    def test_theme_default_volume(self):
        p = self.player()
        p.play(None)
        self.assertEqual(
            self.sp.procs[0].cmd, ["canberra-gtk-play", "-i", "message-new-instant", "-V", "0.0"]
        )

    def test_theme_db(self):
        p = self.player()
        p.play("bell", 50)
        self.assertEqual(self.sp.procs[0].cmd[-1], "-6.0")

    def test_zero_volume_skips(self):
        p = self.player()
        self.assertEqual(p.play("bell", 0).volume, 0)
        self.assertEqual(self.sp.procs, [])

    def test_none_skips(self):
        p = self.player()
        self.assertEqual(p.play("none").kind, "none")
        self.assertEqual(self.sp.procs, [])

    def test_missing_file_plays_default(self):
        p = self.player()
        p.play("/no/such.wav")
        self.assertEqual(self.sp.procs[0].cmd[0], "canberra-gtk-play")
        self.assertIsNotNone(p.last_error)

    def test_no_player_found_logs_once(self):
        p = self.player(set())
        with self.assertLogs("notifyd.sound", "ERROR") as cm:
            r1 = p.play(str(self.wav))
            r2 = p.play("bell")
        self.assertEqual(r1.kind, "file")
        self.assertEqual(r2.kind, "theme")
        self.assertEqual(len(cm.records), 1)
        self.assertEqual(self.sp.procs, [])

    def test_concurrency_cap(self):
        p = self.player()
        for _ in range(5):
            p.play("bell")
        procs = self.sp.procs
        self.assertEqual([x.terminated for x in procs], [True, True, False, False, False])

    def test_finished_not_terminated(self):
        p = self.player()
        for _ in range(3):
            p.play("bell")
        self.sp.procs[0].done = True
        p.play("bell")
        self.assertFalse(any(x.terminated for x in self.sp.procs))

    def test_children_reaped(self):
        waited = []

        class P(FakeProc):
            def wait(self):
                waited.append(self)
                return 0

        p = SoundPlayer(spawn=lambda cmd, **kw: P(cmd), which=which_of(ALL))
        p.play("bell")
        import time

        deadline = time.time() + 2
        while not waited and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(waited), 1)

    def test_never_raises(self):
        def boom(*a, **k):
            raise RuntimeError("boom")

        p = SoundPlayer(spawn=boom, which=which_of(ALL))
        self.assertEqual(p.play("bell").kind, "theme")
        p2 = SoundPlayer(spawn=Spawner(), which=boom)
        self.assertEqual(p2.play("bell").kind, "theme")
        self.assertEqual(p2.play(123).kind, "theme")  # type: ignore[arg-type]
        p3 = SoundPlayer(master_volume="x")  # type: ignore[arg-type]
        self.assertEqual(p3.resolve("bell").volume, 100)


if __name__ == "__main__":
    unittest.main()
