"""Run dictate.sh when Right Alt is double-tapped (no other key in between)."""
import asyncio, subprocess, time
from pathlib import Path
import evdev
from evdev import ecodes as e

GAP = 0.4  # max seconds between the two taps
DICTATE = str(Path(__file__).parent / "dictate.sh")
last = 0.0

async def watch(dev):
    global last
    async for ev in dev.async_read_loop():
        if ev.type != e.EV_KEY or ev.value != 1:
            continue
        if ev.code != e.KEY_RIGHTALT:
            last = 0.0
            continue
        now = time.monotonic()
        if now - last <= GAP:
            last = 0.0
            subprocess.Popen([DICTATE])
        else:
            last = now

async def main():
    devs = [d for d in map(evdev.InputDevice, evdev.list_devices())
            if e.KEY_RIGHTALT in d.capabilities().get(e.EV_KEY, [])]
    await asyncio.gather(*(watch(d) for d in devs))

asyncio.run(main())
