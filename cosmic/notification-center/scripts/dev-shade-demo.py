#!/usr/bin/python3
# ruff: noqa: E501
"""Open the dashboard shade on the live display over a temp database of ~25 seeded notifications.

All callbacks are fakes that log to stdout; dismiss / mark-read act on the temp DB so the page
reacts. Never touches the bus, the real history DB or the real shade state file.

Usage: /usr/bin/python3 scripts/dev-shade-demo.py [--hold] [--snapshot-dir DIR] [--check-catcher]
  --hold               stay open until Ctrl+C (default: close after ~60 s)
  --snapshot-dir DIR   save collapsed.png, expanded.png and settings.png (WebKit snapshots), exit
  --check-catcher      emit a button press on the catcher surface and verify both surfaces hide
                       (exit status 1 when either stays up)
"""

import argparse
import json
import logging
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gi  # noqa: E402

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
import demo_icons  # noqa: E402
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from notifyd.model import CloseReason, Notification, State  # noqa: E402
from notifyd.shade import ShadeWindow  # noqa: E402
from notifyd.store import Store  # noqa: E402

DEMO_BUTTONS = [
    {"id": "b0", "label": "Open log", "run": ["/bin/true"], "close": False},
    {"id": "b1", "label": "Snooze 15 min", "builtin": "snooze", "minutes": 15, "close": True},
    {"id": "b2", "label": "Copy body", "builtin": "copy-body", "close": False},
]

HOUR = 3600.0
MIN = 60.0
BOLD_BODY = 'Disk is <b>96% full</b> on prod-1.\nSee <a href="https://app.example.com/issues/412">issue 412</a>.'
LONG = (
    "The nightly build finished with 3 warnings and 0 errors. "
    "Artifacts were uploaded to the usual place; the full log is attached to the run. "
    "Next scheduled run is tomorrow at 02:00."
)

# (app, icon, summary, body, urgency, actions(flat), state, tags, age_min, read, expired)
SPECS = [
    (
        "Example App",
        "",
        "URGENT: prod-1 disk 96% full",
        BOLD_BODY,
        2,
        ["default", "Open", "ack", "Acknowledge"],
        State.SHOWN,
        ["example", "urgent"],
        2,
        False,
        False,
    ),
    (
        "Example App",
        "",
        "Issue #408 assigned to you",
        "Fix the flaky booking test.",
        1,
        ["open", "Open issue"],
        State.SHOWN,
        ["example"],
        14,
        False,
        False,
    ),
    (
        "Example App",
        "",
        "Deploy finished: apex-hvac",
        "Production deploy completed in 41 s.",
        1,
        [],
        State.SHOWN,
        ["example"],
        55,
        True,
        True,
    ),
    (
        "Example App",
        "",
        "Time entry reminder",
        "You have 2 h unlogged today.",
        0,
        [],
        State.DND,
        ["example"],
        130,
        False,
        False,
    ),
    (
        "Example App",
        "",
        "Backup warning: replica lag 38 s",
        "",
        1,
        [],
        State.SHOWN,
        ["example"],
        300,
        True,
        True,
    ),
    (
        "Thunderbird",
        "mail-unread",
        "Justin Hale: Re: June schedule",
        "Looks good to me. Can we move the Thursday call to 3pm? I have a conflict in the morning.",
        1,
        ["reply", "Reply", "archive", "Archive"],
        State.SHOWN,
        ["mail"],
        6,
        False,
        False,
    ),
    (
        "Thunderbird",
        "mail-unread",
        "Newsletter: Cloudflare weekly",
        "Workers, D1 and a very long line that keeps going so that the two line clamp has something to cut off at the end of the second line, honestly.",
        0,
        [],
        State.SHOWN,
        ["mail"],
        90,
        True,
        False,
    ),
    (
        "Thunderbird",
        "mail-unread",
        "Invoice 2041 paid",
        "Payment received, thank you.",
        1,
        [],
        State.DND,
        ["mail"],
        200,
        False,
        False,
    ),
    (
        "Thunderbird",
        "mail-unread",
        "Meeting invite: Roof inspection",
        "Tuesday 10:00.",
        1,
        [],
        State.SHOWN,
        ["mail"],
        600,
        True,
        True,
    ),
    (
        "Thunderbird",
        "mail-unread",
        "Alex: photos from the site",
        "Three attachments.",
        1,
        [],
        State.SHOWN,
        ["mail"],
        1500,
        True,
        False,
    ),
    (
        "Signal",
        "chat",
        "Sam",
        "are you around for a quick call?",
        1,
        ["reply", "Reply"],
        State.SHOWN,
        [],
        4,
        False,
        False,
    ),
    (
        "Signal",
        "chat",
        "Family group",
        "Mom: dinner is at 6 on Sunday",
        1,
        [],
        State.SHOWN,
        [],
        25,
        False,
        False,
    ),
    (
        "Signal",
        "chat",
        "Sam",
        "ok never mind, got it sorted",
        1,
        [],
        State.SHOWN,
        [],
        22,
        True,
        True,
    ),
    ("Signal", "chat", "Riley", "sent a photo", 1, [], State.DND, [], 400, False, False),
    (
        "Calendar",
        "x-office-calendar",
        "Standup in 10 minutes",
        "Daily team standup, room 2.",
        1,
        ["snooze", "Snooze"],
        State.SHOWN,
        ["cal"],
        9,
        False,
        False,
    ),
    (
        "Calendar",
        "x-office-calendar",
        "Dentist tomorrow 09:00",
        "",
        1,
        [],
        State.SHOWN,
        ["cal"],
        800,
        True,
        True,
    ),
    (
        "Calendar",
        "x-office-calendar",
        "Quarterly review",
        "Prep slides before Friday.",
        0,
        [],
        State.SHOWN,
        ["cal"],
        2000,
        True,
        False,
    ),
    (
        "Build Server",
        "applications-engineering",
        "Nightly build passed",
        LONG,
        0,
        ["log", "Open log"],
        State.SHOWN,
        ["ci"],
        35,
        False,
        False,
    ),
    (
        "Build Server",
        "applications-engineering",
        "Build #1288 FAILED",
        "tests/test_booking.py::test_overlap failed",
        2,
        ["log", "Open log", "rerun", "Re-run"],
        State.SHOWN,
        ["ci", "urgent"],
        70,
        False,
        True,
    ),
    (
        "Build Server",
        "applications-engineering",
        "Build #1287 passed",
        "",
        0,
        [],
        State.SHOWN,
        ["ci"],
        180,
        True,
        False,
    ),
    (
        "Build Server",
        "applications-engineering",
        "Dependency update available",
        "ruff 0.15.2",
        0,
        [],
        State.DND,
        ["ci"],
        500,
        False,
        False,
    ),
    (
        "Build Server",
        "applications-engineering",
        "Cache cleaned",
        "Freed 3.1 GB.",
        0,
        [],
        State.SHOWN,
        ["ci"],
        1200,
        True,
        False,
    ),
    ("Signal", "chat", "Riley", "typing... never mind", 1, [], State.SHOWN, [], 1100, True, False),
    (
        "Calendar",
        "x-office-calendar",
        "Lunch with Dana",
        "12:30, the usual place.",
        1,
        [],
        State.SHOWN,
        ["cal"],
        3000,
        True,
        False,
    ),
    (
        "Thunderbird",
        "mail-unread",
        "Security alert",
        "New sign-in from Linux.",
        1,
        [],
        State.SHOWN,
        ["mail"],
        4000,
        False,
        False,
    ),
]


def seed(store: Store, explicit_icon: str = "") -> None:
    now = time.time()
    # an app matching the temp Calendar Pro .desktop (no icon given) and one with an explicit icon
    extra = [
        (
            "Calendar Pro",
            "",
            "Meeting at 10:00",
            "Standup, room 4.",
            1,
            [],
            State.SHOWN,
            [],
            3,
            False,
            False,
        ),
        (
            "Photo Sync",
            explicit_icon,
            "Upload done",
            "128 photos.",
            1,
            [],
            State.SHOWN,
            [],
            5,
            False,
            False,
        ),
    ]
    for i, (
        app,
        icon,
        summary,
        body,
        urg,
        actions,
        state,
        tags,
        age_min,
        read,
        expired,
    ) in enumerate([*SPECS, *extra], 1):
        at = now - age_min * MIN
        n = Notification.from_notify(
            i, app, 0, icon, summary, body, actions, {"urgency": urg}, -1, at
        )
        n.state, n.tags = state, list(tags)
        if "urgent" in tags:
            n.classes = ["urgent-row"]
            n.highlights = [{"pattern": r"(?i)\b(URGENT|FAILED)\b", "class": "urgent"}]
        if i in (1, 2):  # the newest items of the first group: peek and expanded rows
            n.buttons = [dict(b) for b in DEMO_BUTTONS]
        if read:
            n.read_at = at + 30
        if expired and state == State.SHOWN:
            n.closed_at, n.close_reason = at + 10, CloseReason.EXPIRED
        store.record(n)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hold", action="store_true")
    ap.add_argument("--snapshot-dir", type=Path)
    ap.add_argument("--check-catcher", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("gi").setLevel(logging.WARNING)

    tmp = tempfile.TemporaryDirectory(prefix="nc-shade-demo-")
    store = Store(Path(tmp.name) / "demo.db")
    _, explicit = demo_icons.setup(Path(tmp.name) / "icons")
    seed(store, explicit)
    prefs: dict[str, dict] = {
        "Signal": {"muted": False, "silent": True, "sound": "/tmp/ping.ogg", "volume": 60}
    }
    dnd = {"on": False}
    code = {"status": 0}

    def dismiss(ids: list[int]) -> None:
        print(f"on_dismiss {ids}", flush=True)
        for i in ids:
            n = store.get(i)
            if n is None:
                continue
            if n.closed_at is None:  # open: the wiring goes through server.dismiss
                store.mark_closed(i, CloseReason.DISMISSED, time.time())
            else:  # already expired: quiet
                store.mark_dismissed_quiet([i], time.time())

    def mark_read(ids: list[int]) -> None:
        print(f"on_mark_read {ids}", flush=True)
        store.mark_read(ids, time.time())

    def open_details(i: int) -> None:
        print(f"on_open_details {i}", flush=True)

    def open_center() -> None:
        print("on_open_center", flush=True)

    def invoke(i: int, key: str) -> bool:
        print(f"on_invoke_action {i} {key}", flush=True)
        return True

    def set_dnd(on: bool) -> None:
        print(f"on_set_dnd {on}", flush=True)
        dnd["on"] = on

    def get_prefs(app: str) -> dict:
        print(f"get_app_prefs {app!r}", flush=True)
        return dict(
            prefs.get(
                app,
                {
                    "muted": False,
                    "silent": False,
                    "sticky": False,
                    "blocked": False,
                    "dnd_bypass": False,
                    "sound": None,
                    "volume": None,
                },
            )
        )

    def set_prefs(app: str, p: dict) -> None:
        print(f"on_set_app_prefs {app!r} {p}", flush=True)
        vol = p.get("volume")
        if vol is not None and not (isinstance(vol, int) and 0 <= vol <= 100):
            raise ValueError(f"`volume` must be an integer from 0 to 100, got {vol!r}")
        if p.get("sound") == "bad":
            raise ValueError("sound file not found: bad")
        prefs[app] = p

    win = ShadeWindow(
        store,
        ROOT / "web",
        on_dismiss=dismiss,
        on_mark_read=mark_read,
        on_open_details=open_details,
        on_open_center=open_center,
        on_invoke_action=invoke,
        get_dnd=lambda: dnd["on"],
        on_set_dnd=set_dnd,
        get_app_prefs=get_prefs,
        on_set_app_prefs=set_prefs,
        state_path=Path(tmp.name) / "shade.json",
        on_button=lambda i, b: print(f"on_button {i} {b}", flush=True),
    )
    win.set_user_css(
        ".shade-item.tag-urgent .shade-item-summary { color: var(--nc-urgency-critical); }"
    )
    win.show()

    def js(script: str) -> bool:
        win._eval(script)
        return False

    def at(sec: float, fn) -> None:
        GLib.timeout_add(int(sec * 1000), lambda: (fn(), False)[1])

    def snap(name: str, then=None) -> None:
        out = args.snapshot_dir / name
        win.save_snapshot(
            out, lambda ok: (print(f"snapshot {out} ok={ok}", flush=True), then and then())[1]
        )

    def check_catcher() -> None:
        catcher, shade = win._catcher, win._window
        before = (bool(catcher.get_visible()), bool(shade.get_visible()), win.is_visible())
        ev = Gdk.Event.new(Gdk.EventType.BUTTON_PRESS)
        ev.button.button = 1
        ev.button.window = catcher.get_window()
        catcher.emit("button-press-event", ev)
        after = (bool(catcher.get_visible()), bool(shade.get_visible()), win.is_visible())
        ok = before == (True, True, True) and after == (False, False, False)
        print(
            f"catcher check: before(catcher, shade, is_visible)={before} after={after} -> {'OK' if ok else 'FAIL'}",
            flush=True,
        )
        code["status"] = 0 if ok else 1

    if args.snapshot_dir:
        args.snapshot_dir.mkdir(parents=True, exist_ok=True)
        at(2.5, lambda: snap("collapsed.png"))
        at(
            3.5,
            lambda: js(
                "document.querySelectorAll('.shade-group-head')[0].click(); document.querySelectorAll('.shade-group-head')[1].click()"
            ),
        )
        at(5, lambda: snap("expanded.png"))
        at(5.5, lambda: js("document.querySelectorAll('.shade-gear')[2].click()"))
        at(
            6.5,
            lambda: js(
                "var s=document.querySelector('.shade-settings input.shade-input'); s.value='bad'; s.dispatchEvent(new Event('input')); document.querySelector('.shade-settings .btn-primary').click()"
            ),
        )
        at(7.5, lambda: snap("settings.png"))
        icon_js = (
            "var i=document.querySelector('.shade-icon-field input'); i.value=%s; "
            "i.dispatchEvent(new Event('input'))"
        )
        at(8, lambda: js(icon_js % json.dumps(explicit)))
        at(9, lambda: snap("settings-icon.png"))
        at(9.5, lambda: js(icon_js % json.dumps("no-such-icon-name")))
        at(10.5, lambda: snap("settings-icon-bad.png", Gtk.main_quit))
    elif args.check_catcher:
        at(3, check_catcher)
        at(3.5, Gtk.main_quit)
    elif not args.hold:
        at(60, Gtk.main_quit)
    try:
        Gtk.main()
    except KeyboardInterrupt:
        pass
    win.teardown()
    deadline = time.monotonic() + 1.0
    while Gtk.events_pending() and time.monotonic() < deadline:
        Gtk.main_iteration_do(False)
    tmp.cleanup()
    return code["status"]


if __name__ == "__main__":
    sys.exit(main())
