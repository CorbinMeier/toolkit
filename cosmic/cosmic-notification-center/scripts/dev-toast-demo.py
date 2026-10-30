#!/usr/bin/python3
"""Show three sample toasts on the live display for ~15 s, then exit. Never touches the bus.

Usage: /usr/bin/python3 scripts/dev-toast-demo.py [seconds]
       /usr/bin/python3 scripts/dev-toast-demo.py --snapshot-dir DIR   (toast.png, then exit)
       /usr/bin/python3 scripts/dev-toast-demo.py --hover-check

--hover-check shows one 2 s toast and drives the page's own mouseenter/mouseleave listeners with
synthetic events (the real pointer is left alone), logging every page message with a timestamp.
Expected: hover in at 0.5 s, no close at 2 s, hover out at 4 s, "closed 1 EXPIRED" at ~5 s.
"""

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
import demo_icons  # noqa: E402
from gi.repository import GLib, Gtk  # noqa: E402

from notifyd.model import Notification  # noqa: E402
from notifyd.toasts import ToastHost  # noqa: E402

# Rule buttons as the store holds them (the commands never reach the page).
BUTTONS = [
    {"id": "b0", "label": "Open log", "run": ["/bin/true"], "close": False},
    {"id": "b1", "label": "Snooze 15 min", "builtin": "snooze", "minutes": 15, "close": True},
    {"id": "b2", "label": "Copy body", "builtin": "copy-body", "close": False},
]


def make(id, app, icon, summary, body, hints=None, actions=None, expire=-1, buttons=None):
    n = Notification.from_notify(
        id, app, 0, icon, summary, body, actions or [], hints or {}, expire, time.time()
    )
    n.buttons = [dict(b) for b in buttons or []]
    return n


def snapshot(host, out: Path) -> None:
    from gi.repository import WebKit2

    def finish(view, result):
        try:
            view.get_snapshot_finish(result).write_to_png(str(out))
            print(f"snapshot {out} ok=True", flush=True)
        except Exception as exc:
            print(f"snapshot failed: {exc}", flush=True)
        Gtk.main_quit()

    host._view.get_snapshot(
        WebKit2.SnapshotRegion.FULL_DOCUMENT, WebKit2.SnapshotOptions.NONE, None, finish
    )


def hover_check() -> int:
    t0 = time.monotonic()

    def log(msg):
        print(f"[{time.monotonic() - t0:5.2f}s] {msg}", flush=True)

    host = ToastHost(
        ROOT / "web",
        lambda i, k: log(f"action {i} {k}"),
        lambda i, r: log(f"closed {i} {r.name}"),
        on_details=lambda i: log(f"details {i}"),
    )
    real = host._on_message

    def spy(msg):
        if isinstance(msg, dict) and msg.get("type") in ("hover", "details"):
            log(f"page message {msg}")
        real(msg)

    host._on_message = spy
    host.show(make(1, "Demo", "", "Hover check", "expires after 2 s", expire=2000))

    def fire(name):
        log(f"dispatch {name}")
        host._eval(f'document.getElementById("stack").dispatchEvent(new MouseEvent("{name}"))')
        return False

    GLib.timeout_add(500, fire, "mouseenter")
    GLib.timeout_add(4000, fire, "mouseleave")
    GLib.timeout_add(6500, Gtk.main_quit)
    Gtk.main()
    host.teardown()
    deadline = time.monotonic() + 1.0
    while Gtk.events_pending() and time.monotonic() < deadline:
        Gtk.main_iteration_do(False)
    return 0


def main() -> int:
    if "--hover-check" in sys.argv:
        return hover_check()
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 15
    _, explicit = demo_icons.setup(tempfile.mkdtemp(prefix="nc-toast-demo-"))
    host = ToastHost(
        ROOT / "web",
        lambda i, k: print(f"action {i} {k}", flush=True),
        lambda i, r: print(f"closed {i} {r.name}", flush=True),
        on_button=lambda i, b: print(f"button {i} {b}", flush=True),
    )
    host.show(
        make(
            1,
            "Terminal",
            "utilities-terminal",
            "Build finished",
            "All 42 tests passed.",
            expire=12000,
            buttons=BUTTONS,
        )
    )
    crit = make(
        2,
        "Example App",
        "",
        "URGENT: server down",
        "Disk is <b>96% full</b> on prod-1.",
        {"urgency": 2},
        ["default", "Open", "ack", "Acknowledge"],
        buttons=BUTTONS[:2],
    )
    crit.highlights = [{"pattern": r"(?i)\bURGENT\b", "class": "urgent"}]
    host.show(crit)
    host.show(
        make(
            3,
            "Mail",
            "mail-unread",
            "New message from Ada",
            'See <a href="https://example.com/spec">the spec</a> &amp; <i>reply</i> '
            '<u>soon</u>. <a href="javascript:alert(1)">not a link</a>',
            expire=14000,
        )
    )
    # matches the temp Calendar Pro .desktop (no icon given), and an explicit icon path
    host.show(make(4, "Calendar Pro", "", "Meeting at 10:00", "Standup, room 4.", expire=14000))
    host.show(make(5, "Photo Sync", explicit, "Upload done", "128 photos.", expire=14000))
    if "--snapshot-dir" in sys.argv:
        out = Path(sys.argv[sys.argv.index("--snapshot-dir") + 1])
        out.mkdir(parents=True, exist_ok=True)
        GLib.timeout_add_seconds(3, lambda: (snapshot(host, out / "toast.png"), False)[1])
        GLib.timeout_add_seconds(10, Gtk.main_quit)
    else:
        GLib.timeout_add_seconds(seconds, Gtk.main_quit)
    Gtk.main()
    host.teardown()
    deadline = time.monotonic() + 1.0
    while Gtk.events_pending() and time.monotonic() < deadline:
        Gtk.main_iteration_do(False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
