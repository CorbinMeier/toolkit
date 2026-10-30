"""Dictation daemon: owns the hotkey, the mic, the model and the state.

Double-tap Right Alt (gap set in config.json) to start, double-tap again to stop and type.
The tray talks to it over a unix socket (JSON lines: status | toggle | quit).
Env DICTATE_DRYRUN=1 logs the text instead of typing it.
"""
import asyncio, json, os, subprocess, time, wave
from pathlib import Path
import numpy as np
import sherpa_onnx
import evdev
from evdev import ecodes as e
from transcribe import load

CONF = Path.home() / ".config/parakeet-dictate/config.json"
DEFAULTS = {"tap_delay_ms": 400, "precapture": True, "max_seconds": 300, "mode": "record"}
CACHE = Path.home() / ".cache/parakeet"
SOCK = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "parakeet.sock"
DRYRUN = bool(os.environ.get("DICTATE_DRYRUN"))
RATE = 16000
VAD_MODEL = Path(__file__).parent / "model/silero_vad.onnx"


def cfg():
    try:
        d = json.loads(CONF.read_text())
    except (OSError, ValueError):
        d = {}
    return {**DEFAULTS, **d}


class Recorder:
    """parec streamed into memory; start() returns before audio is flowing (a few ms to ~100 ms)."""

    def __init__(self):
        self.proc = None
        self.task = None
        self.buf = bytearray()

    @property
    def running(self):
        return self.proc is not None

    async def start(self):
        self.buf = bytearray()
        self.proc = await asyncio.create_subprocess_exec(
            "parec", "--channels=1", f"--rate={RATE}", "--format=s16le", "--latency-msec=20",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        self.task = asyncio.create_task(self._pump(self.proc))

    async def _pump(self, proc):
        while chunk := await proc.stdout.read(4096):
            self.buf += chunk

    async def stop(self):
        proc, self.proc = self.proc, None
        if proc is None:
            return b""
        if proc.returncode is None:
            proc.terminate()
        await self.task
        await proc.wait()
        return bytes(self.buf)


class Daemon:
    def __init__(self):
        self.state = "loading"  # loading | idle | listening | busy
        self.error = ""
        self.rec = Recorder()
        self.lock = asyncio.Lock()
        self.armed_at = 0.0
        self.timer = None
        self.max_timer = None
        self.watched = set()
        self.recognizer = None
        self.live_done = asyncio.Event()
        self.live_task = None

    # ---- hotkey logic -------------------------------------------------
    async def on_tap(self):
        async with self.lock:
            if self.state in ("loading", "busy"):
                return
            c = cfg()
            now = time.monotonic()
            delay = c["tap_delay_ms"] / 1000
            if self.armed_at and now - self.armed_at <= delay:
                self._cancel_timer()
                self.armed_at = 0.0
                await self._toggle(c)
                return
            self.armed_at = now
            if self.state == "idle" and c["precapture"] and not self.rec.running:
                await self.rec.start()  # speculative; discarded unless a second tap follows
            self.timer = asyncio.get_running_loop().call_later(
                delay, lambda: asyncio.create_task(self.disarm()))

    async def disarm(self):
        """Gap expired or another key was pressed: forget the first tap, drop speculative audio."""
        async with self.lock:
            self._cancel_timer()
            self.armed_at = 0.0
            if self.state == "idle" and self.rec.running:
                await self.rec.stop()

    def _cancel_timer(self):
        if self.timer:
            self.timer.cancel()
            self.timer = None

    async def _toggle(self, c):
        if self.state == "idle":
            if not self.rec.running:
                await self.rec.start()
            self.state = "listening"
            self.error = ""
            if c["mode"] == "live" and VAD_MODEL.exists():
                self.live_done.clear()
                self.live_task = asyncio.create_task(self.live_session())
            else:
                self.live_task = None
            self.max_timer = asyncio.get_running_loop().call_later(
                c["max_seconds"], lambda: asyncio.create_task(self.command_toggle()))
        elif self.state == "listening":
            if self.max_timer:
                self.max_timer.cancel()
                self.max_timer = None
            self.state = "busy"
            data = await self.rec.stop()
            if self.live_task:
                self.live_done.set()  # live_session flushes the tail, saves the wav, sets idle
            else:
                asyncio.create_task(self.transcribe(data))

    async def command_toggle(self):
        async with self.lock:
            if self.state in ("idle", "listening"):
                await self._toggle(cfg())

    # ---- transcription and typing -------------------------------------
    def _decode(self, samples):
        s = self.recognizer.create_stream()
        s.accept_waveform(RATE, samples)
        self.recognizer.decode_stream(s)
        return s.result.text.strip()

    async def transcribe(self, data):
        try:
            self._save_wav(data)
            if len(data) < RATE * 2 * 0.3:
                return
            samples = np.frombuffer(data, np.int16).astype(np.float32) / 32768
            text = await asyncio.get_running_loop().run_in_executor(None, self._decode, samples)
            if text:
                await self.type_text(text + " ")
        except Exception as ex:  # keep the daemon alive; last.wav has the audio
            self.error = f"transcribe failed: {ex}"
        finally:
            self.state = "idle"

    def _save_wav(self, data):
        CACHE.mkdir(parents=True, exist_ok=True)
        with wave.open(str(CACHE / "last.wav"), "wb") as w:  # kept so a failure can be retried
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(data)

    async def live_session(self):
        """Feed the growing mic buffer to the VAD; decode and type each phrase as soon as you pause."""
        cfgv = sherpa_onnx.VadModelConfig()
        cfgv.silero_vad.model = str(VAD_MODEL)
        cfgv.silero_vad.min_silence_duration = 0.5
        cfgv.silero_vad.min_speech_duration = 0.25
        cfgv.silero_vad.max_speech_duration = 20  # force a split during a long run-on
        cfgv.sample_rate = RATE
        vad = sherpa_onnx.VoiceActivityDetector(cfgv, buffer_size_in_seconds=60)
        loop = asyncio.get_running_loop()
        buf, pos = self.rec.buf, 0
        try:
            while True:
                done = self.live_done.is_set()
                n = (len(buf) - pos) // 2 * 2
                if n > 0:
                    # sherpa's VAD mishandles one huge call, so feed it in 512-sample windows
                    pcm = np.frombuffer(bytes(buf[pos:pos + n]), np.int16).astype(np.float32) / 32768
                    pos += n
                    for i in range(0, len(pcm), 512):
                        vad.accept_waveform(pcm[i:i + 512])
                if done:
                    vad.flush()
                while not vad.empty():
                    seg = np.array(vad.front.samples, dtype=np.float32)
                    vad.pop()
                    text = await loop.run_in_executor(None, self._decode, seg)
                    if text:
                        await self.type_text(text + " ")
                if done:
                    break
                await asyncio.sleep(0.1)
        except Exception as ex:
            self.error = f"live failed: {ex}"
        finally:
            try:
                self._save_wav(bytes(buf))
            finally:
                self.state = "idle"

    async def type_text(self, text):
        if DRYRUN:
            print("TEXT:", text, flush=True)
            return
        p = await asyncio.create_subprocess_exec("wtype", "--", text, stderr=asyncio.subprocess.DEVNULL)
        if await p.wait() != 0:
            c = await asyncio.create_subprocess_exec("wl-copy", "--", text)
            await c.wait()
            self.error = "typing failed; text copied to clipboard"

    # ---- devices ------------------------------------------------------
    async def watch(self, path):
        try:
            dev = evdev.InputDevice(path)
            async for ev in dev.async_read_loop():
                if ev.type != e.EV_KEY or ev.value != 1:
                    continue
                if ev.code == e.KEY_RIGHTALT:
                    await self.on_tap()
                elif self.armed_at:
                    await self.disarm()
        except OSError:
            pass
        finally:
            self.watched.discard(path)

    async def supervise(self):
        """Rescan every few seconds so unplugging or replugging a keyboard doesn't need a restart."""
        while True:
            for path in evdev.list_devices():
                if path in self.watched:
                    continue
                try:
                    d = evdev.InputDevice(path)
                    has = e.KEY_RIGHTALT in d.capabilities().get(e.EV_KEY, [])
                    d.close()
                except OSError:
                    continue
                if has:
                    self.watched.add(path)
                    asyncio.create_task(self.watch(path))
            await asyncio.sleep(3)

    # ---- tray socket --------------------------------------------------
    async def client(self, reader, writer):
        try:
            req = json.loads(await reader.readline() or b"{}")
            cmd = req.get("cmd")
            if cmd == "toggle":
                await self.command_toggle()
            elif cmd == "quit":
                writer.write(b'{"ok": true}\n')
                await writer.drain()
                os._exit(0)
            writer.write((json.dumps({"state": self.state, "error": self.error}) + "\n").encode())
            await writer.drain()
        except (ValueError, OSError):
            pass
        finally:
            writer.close()

    async def run(self):
        SOCK.unlink(missing_ok=True)
        await asyncio.start_unix_server(self.client, path=str(SOCK))
        asyncio.create_task(self.supervise())
        self.recognizer = await asyncio.get_running_loop().run_in_executor(None, load)
        self.state = "idle"
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(Daemon().run())
