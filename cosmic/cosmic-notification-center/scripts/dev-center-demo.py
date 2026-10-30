#!/usr/bin/python3
# ruff: noqa: E501
"""Open the center window on the live display over a temp database of ~30 seeded notifications.

After 3 s it navigates to a details page. JS -> Python traffic is logged to stderr. Closes after
~30 s, or with --hold stays until the window is closed with Ctrl+C. Never touches the bus or the
real history DB.

Usage: /usr/bin/python3 scripts/dev-center-demo.py [--hold] [--snapshot-dir DIR]
  --snapshot-dir DIR   save list.png (2 s), details.png (6 s) and details-bottom.png (8 s), then exit
  --settings-snapshots DIR   open the Settings panel over an in-memory fake SettingsService
                       (tests/fake_settings.py; nothing is written anywhere) and save list-900.png,
                       list-menu.png, appearance-<preset>.png (default, crt-green, night-ops-hud),
                       appearance-error.png, behavior.png, sounds.png, rules-list.png,
                       rules-edit.png, rules-edit-buttons.png, rules-edit-script.png, rules-test.png,
                       rules-source-error.png, advanced.png, guard.png, then exit. The Rules
                       tab edits a copy of tests/data/rules_user.toml in the temp dir (a real
                       SettingsService for the rules_* calls; everything else is the fake).
  --collapse-snapshots DIR   drive the collapsible list and save expanded.png, collapsed.png,
                       mixed.png and app-only.png, then exit. The view state goes to a temp file.

The seed spans five calendar days (today, yesterday and three older) and six apps, so the day
sections and the app groups inside them can all be collapsed.
"""

import argparse
import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
import demo_icons  # noqa: E402
from gi.repository import GLib, Gtk  # noqa: E402

from notifyd.center import CenterWindow  # noqa: E402
from notifyd.model import CloseReason, Notification, State, TimeoutPolicy  # noqa: E402
from notifyd.rules import RuleSet  # noqa: E402
from notifyd.settings import SettingsService  # noqa: E402
from notifyd.store import Store  # noqa: E402
from tests.fake_settings import FakeSettings  # noqa: E402

HOUR = 3600.0
DETAIL_ID = 3  # Example App (after the two icon-demo rows)
DAY = 86400.0


class DemoSettings(FakeSettings):
    """The fake panel backend, except that rules_* run the real SettingsService on a temp dir."""

    def __init__(self, config_dir: Path) -> None:
        super().__init__()
        config_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / "tests" / "data" / "rules_user.toml", config_dir / "rules.toml")
        runtime = SimpleNamespace(
            config_path=config_dir / "config.toml",
            timeouts=TimeoutPolicy(),
            app_rules=RuleSet.empty(),
            player=None,
        )
        self._svc = SettingsService(runtime, None, config_dir)

    def __getattribute__(self, name: str):
        if name.startswith("rules_"):
            return getattr(object.__getattribute__(self, "_svc"), name)
        return object.__getattribute__(self, name)


def seed(store: Store, explicit_icon: str = "") -> None:
    now = time.time()
    specs = [
        (
            "Calendar Pro",
            "",
            "Meeting at 10:00",
            "Standup, room 4.",
            {"urgency": 1},
            [],
            State.SHOWN,
            [],
            0.2,
            False,
        ),
        (
            "Photo Sync",
            explicit_icon,
            "Upload done",
            "128 photos.",
            {"urgency": 1},
            [],
            State.SHOWN,
            [],
            0.3,
            False,
        ),
        # (app, icon, summary, body, hints, actions, state, tags, age_hours, read)
        (
            "Example App",
            "",
            "URGENT: prod-1 disk 96% full",
            'Disk is <b>96% full</b> on prod-1.\nSee <a href="https://app.example.com/issues/412">issue 412</a>'
            ' or <a href="mailto:ops@example.com">mail ops</a>.\n<a href="javascript:alert(1)">not a link</a>'
            "<script>alert('x')</script>",
            {
                "urgency": 2,
                "category": "device",
                "x-nc-url": "https://app.example.com/issues/412",
                "sound-name": "dialog-warning",
                "nested": {"host": "prod-1", "pct": 96.2, "mounts": ["/", "/var"]},
            },
            ["default", "Open", "ack", "Acknowledge", "snooze", "Snooze 1h"],
            State.SHOWN,
            ["example", "urgent"],
            0.1,
            False,
        ),
        (
            "Example App",
            "dialog-information",
            "Issue 409 closed",
            "Merged into <i>main</i>.",
            {"urgency": 1},
            [],
            State.SHOWN,
            ["example"],
            0.5,
            False,
        ),
        (
            "Example App",
            "",
            "Build failed: web-dev",
            "Step <b>lint</b> failed.\n<u>3 errors</u>, 1 warning.\nhttps://ci.example.com/run/88",
            {"urgency": 2, "category": "build.failed", "x-nc-url": "https://ci.example.com/run/88"},
            ["default", "Open", "retry", "Retry"],
            State.SHOWN,
            ["example", "ci"],
            1.2,
            False,
        ),
        (
            "Terminal",
            "utilities-terminal",
            "Command finished",
            "make test exited with <b>0</b> in 41s.",
            {"urgency": 0},
            [],
            State.SHOWN,
            [],
            1.5,
            True,
        ),
        (
            "Terminal",
            "utilities-terminal",
            "Command finished",
            "cargo build exited with 101 in 12s.",
            {"urgency": 1},
            [],
            State.SHOWN,
            ["ci"],
            2.0,
            True,
        ),
        (
            "Mail",
            "mail-unread",
            "Ada Lovelace: the spec",
            'Can you look at <a href="https://example.com/spec">the spec</a> before Friday?\n\nThanks,\nAda',
            {"urgency": 1, "category": "email.arrived", "desktop-entry": "org.gnome.Evolution"},
            ["default", "Open"],
            State.SHOWN,
            [],
            2.5,
            False,
        ),
        (
            "Mail",
            "mail-unread",
            "Newsletter: 10 tips",
            "Unsubscribe anytime.",
            {"urgency": 0},
            [],
            State.BLOCKED,
            ["noise"],
            3.0,
            False,
        ),
        (
            "Mail",
            "mail-unread",
            "Grace Hopper: lunch?",
            "Noon works. <b>Bring the notes.</b>",
            {"urgency": 1},
            [],
            State.SHOWN,
            [],
            3.4,
            True,
        ),
        (
            "Calendar",
            "x-office-calendar",
            "Standup in 10 minutes",
            "Room 4, video link in the invite.",
            {"urgency": 1, "category": "reminder"},
            ["default", "Join"],
            State.SHOWN,
            [],
            4.0,
            True,
        ),
        (
            "Calendar",
            "x-office-calendar",
            "Dentist tomorrow 09:00",
            "",
            {"urgency": 1},
            [],
            State.DND,
            [],
            5.0,
            False,
        ),
        (
            "Firefox",
            "firefox",
            "Download complete",
            "ubuntu-24.04.iso (5.8 GB)",
            {"urgency": 0},
            ["default", "Show"],
            State.SHOWN,
            [],
            6.0,
            True,
        ),
        (
            "Firefox",
            "firefox",
            "example.com wants to send notifications",
            "",
            {"urgency": 1},
            [],
            State.STORE_ONLY,
            ["noise"],
            6.5,
            False,
        ),
        (
            "System Updates",
            "software-update-available",
            "12 updates available",
            "Includes <b>linux 7.1.6</b> and 3 security fixes.",
            {"urgency": 1, "image-path": "software-update-available"},
            ["default", "Review"],
            State.SHOWN,
            [],
            8.0,
            False,
        ),
        (
            "Backup",
            "drive-harddisk",
            "Nightly backup complete",
            "412 files, 2.1 GB, no errors.",
            {"urgency": 0},
            [],
            State.SHOWN,
            ["backup"],
            10.0,
            True,
        ),
        (
            "Backup",
            "drive-harddisk",
            "Backup warning",
            "Skipped <i>3 unreadable files</i>.",
            {"urgency": 1},
            [],
            State.SHOWN,
            ["backup"],
            10.2,
            False,
        ),
        # yesterday
        (
            "Example App",
            "dialog-information",
            "Issue 401 assigned",
            "Assigned to you by <b>Corbin</b>.",
            {"urgency": 1},
            [],
            State.SHOWN,
            ["example"],
            26.0,
            True,
        ),
        (
            "Example App",
            "",
            "URGENT: certificate expires in 3 days",
            'Renew <a href="https://certs.example.com">certs.example.com</a>.',
            {"urgency": 2},
            [],
            State.SHOWN,
            ["example", "urgent"],
            27.0,
            True,
        ),
        (
            "Terminal",
            "utilities-terminal",
            "Command finished",
            "npm install exited with 0 in 8s.",
            {"urgency": 0},
            [],
            State.SHOWN,
            [],
            28.0,
            True,
        ),
        (
            "Mail",
            "mail-unread",
            "Linus: patch review",
            "Series v3 is up.\nPlease review the first <b>two</b> patches.",
            {"urgency": 1},
            [],
            State.SHOWN,
            [],
            29.0,
            True,
        ),
        (
            "Slack",
            "",
            "#general: lunch order",
            "pizza or tacos?",
            {"urgency": 1},
            [],
            State.DND,
            [],
            30.0,
            False,
        ),
        (
            "Slack",
            "",
            "#ops: deploy done",
            "web-dev rolled out at 14:02",
            {"urgency": 0},
            [],
            State.DND,
            ["ci"],
            31.0,
            True,
        ),
        (
            "Calendar",
            "x-office-calendar",
            "Retro in 5 minutes",
            "",
            {"urgency": 1},
            [],
            State.SHOWN,
            [],
            33.0,
            True,
        ),
        (
            "System Updates",
            "software-update-available",
            "Firmware update available",
            "Dell XPS BIOS 1.30",
            {"urgency": 1},
            [],
            State.SHOWN,
            [],
            34.0,
            True,
        ),
        # older
        (
            "Backup",
            "drive-harddisk",
            "Nightly backup complete",
            "398 files, 1.9 GB.",
            {"urgency": 0},
            [],
            State.SHOWN,
            ["backup"],
            50.0,
            True,
        ),
        (
            "Example App",
            "dialog-information",
            "Weekly summary ready",
            "14 closed, 6 opened.",
            {"urgency": 1},
            [],
            State.SHOWN,
            ["example"],
            52.0,
            True,
        ),
        (
            "Terminal",
            "utilities-terminal",
            "Command finished",
            "pytest exited with 1 in 3s.",
            {"urgency": 1},
            [],
            State.SHOWN,
            [],
            53.0,
            True,
        ),
        (
            "Mail",
            "mail-unread",
            "Spam: you won",
            'Click <a href="http://spam.example">here</a>.',
            {"urgency": 0},
            [],
            State.BLOCKED,
            ["noise"],
            55.0,
            False,
        ),
        (
            "Firefox",
            "firefox",
            "Update installed",
            "Restart to apply.",
            {"urgency": 0},
            [],
            State.SHOWN,
            [],
            72.0,
            True,
        ),
        (
            "Calendar",
            "x-office-calendar",
            "Team offsite",
            "Friday, all day. <b>Bring a laptop.</b>",
            {"urgency": 1},
            [],
            State.SHOWN,
            [],
            74.0,
            True,
        ),
        (
            "Backup",
            "drive-harddisk",
            "Disk almost full",
            "The backup volume is at 91%.",
            {"urgency": 2},
            [],
            State.SHOWN,
            ["backup", "urgent"],
            76.0,
            True,
        ),
    ]
    for i, (app, icon, summary, body, hints, actions, state, tags, age, read) in enumerate(
        specs, 1
    ):
        at = now - age * HOUR
        n = Notification.from_notify(i, app, 0, icon, summary, body, actions, hints, -1, at)
        n.state, n.tags = state, tags
        n.matched_rules = ["example sound"] if app == "Example App" else []
        n.sound = "/home/me/Music/example.mp3" if app == "Example App" else None
        if "urgent" in tags:
            n.classes = ["urgent-row"]
            n.highlights = [{"pattern": r"(?i)\bURGENT\b", "class": "urgent"}]
        if read:
            n.read_at = at + 60
        if age > 1 and state == State.SHOWN:
            n.closed_at, n.close_reason = at + 10, CloseReason.EXPIRED
        store.record(n)
    seed_days(store, now, len(specs))


def seed_days(store: Store, now: float, last_id: int) -> None:
    """More rows pinned to calendar days (local time), so the list spans 3+ days and 4+ apps
    whatever time of day the demo runs."""
    midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    # (app, icon, summary, body, days_ago, hour, read); today's rows land just before `now`
    extra = [
        ("Chat", "internet-chat", "Ana: lunch?", "Thai place at noon.", 0, 0.2, False),
        ("Chat", "internet-chat", "Ben: PR approved", "Merge when green.", 0, 0.7, False),
        ("Backup", "drive-harddisk", "Backup finished", "412 files, 1.2 GB.", 0, 1.1, True),
        ("Calendar", "x-office-calendar", "Standup in 10 min", "Room 4.", 0, 1.6, True),
        ("Mail", "mail-unread", "Invoice #881", "From the accountant.", 1, 16.5, False),
        ("Mail", "mail-unread", "Re: offsite", "Fri works for me.", 1, 11.2, False),
        ("Chat", "internet-chat", "Cy: build is red", "main failed on lint.", 1, 15.0, True),
        ("Firefox", "firefox", "Download complete", "report.pdf", 1, 9.4, True),
        ("Calendar", "x-office-calendar", "Dentist", "Thursday 09:00.", 2, 8.0, True),
        ("Chat", "internet-chat", "Dee: ship it", "Release notes are in.", 2, 14.3, True),
        ("Backup", "drive-harddisk", "Backup finished", "380 files, 1.1 GB.", 2, 3.0, True),
        ("Mail", "mail-unread", "Newsletter", "Ten tips for tidy inboxes.", 3, 10.0, True),
        ("Firefox", "firefox", "Update available", "Version 999.", 3, 12.5, True),
        (
            "Example App",
            "dialog-information",
            "Issue 409 closed",
            "Fixed in main.",
            3,
            17.0,
            True,
        ),
        ("Example App", "dialog-warning", "Queue is long", "14 open items.", 4, 9.0, True),
        ("Calendar", "x-office-calendar", "Retro", "Friday 15:00.", 4, 13.0, True),
    ]
    for i, (app, icon, summary, body, days_ago, hour, read) in enumerate(extra, last_id + 1):
        at = now - hour * HOUR if days_ago == 0 else midnight - days_ago * DAY + hour * HOUR
        n = Notification.from_notify(i, app, 0, icon, summary, body, [], {"urgency": 1}, -1, at)
        if read:
            n.read_at = at + 60
        if at < now - HOUR:
            n.closed_at, n.close_reason = at + 10, CloseReason.EXPIRED
        store.record(n)


DEMO_BUTTONS = [
    {"id": "b0", "label": "Open log", "run": ["/bin/true"], "close": False},
    {"id": "b1", "label": "Snooze 15 min", "builtin": "snooze", "minutes": 15, "close": True},
    {"id": "b2", "label": "Copy body", "builtin": "copy-body", "close": False},
]


def seed_buttons_and_runs(store: Store, id: int) -> None:
    """Rule buttons plus a fake run log (exit 0, exit 1, timed out, skipped) on one notification.
    The cmd values are decoys: they must never appear on the page."""
    now = time.time()
    n = store.get(id)
    if n is None:
        return
    n.buttons = [dict(b) for b in DEMO_BUTTONS]
    store.record(n)
    base = dict(cmd=["/usr/bin/secret-cmd", "--token=hunter2"])
    store.add_run(
        id,
        rule="log example",
        trigger="rule",
        started_at=now - 300,
        finished_at=now - 299.8,
        exit_code=0,
        duration_ms=200,
        output_tail="logged 1 line\n",
        **base,
    )
    store.add_run(
        id,
        rule="log example",
        trigger="button",
        button_label="Open log",
        started_at=now - 120,
        finished_at=now - 119.4,
        exit_code=1,
        duration_ms=640,
        output_tail="open-log: no such file: /var/log/app.log\nusage: open-log FILE\n",
        **base,
    )
    store.add_run(
        id,
        rule="slow sync",
        trigger="rule",
        started_at=now - 90,
        finished_at=now - 60,
        signal=15,
        duration_ms=30000,
        output_tail="syncing...\n[timed out after 30s]",
        **base,
    )
    store.add_run(
        id,
        rule="slow sync",
        trigger="rule",
        started_at=now - 30,
        finished_at=now - 30,
        skipped_reason="cooldown",
        **base,
    )


def collapse_snapshots(win: CenterWindow, out: Path) -> None:
    """expanded.png, collapsed.png, mixed.png, app-only.png: clicks are made in the page."""
    out.mkdir(parents=True, exist_ok=True)
    toggle = "document.getElementById('btn-toggle-all').click()"
    day2 = "document.querySelectorAll('#groups .day-title')[1].click()"
    # the second day's first app group collapses (its header is the 2nd .center-head)
    group = (
        "document.querySelectorAll('#groups .center-day')[1].querySelector('.app-title').click()"
    )
    app_only = (
        "var s = document.getElementById('f-group'); s.value = 'app';"
        " s.dispatchEvent(new Event('change'))"
    )

    def at(sec: float, fn) -> None:
        GLib.timeout_add(int(sec * 1000), lambda: (fn(), False)[1])

    def js(script: str):
        return lambda: win._eval(script)

    def snap(name: str, last: bool = False):
        return lambda: win.save_snapshot(out / name, (lambda ok: Gtk.main_quit()) if last else None)

    at(4.0, js(toggle))  # default has today open: this collapses all ...
    at(4.3, js(toggle))  # ... and this expands all
    at(4.9, snap("expanded.png"))
    at(5.5, js(toggle))
    at(6.2, snap("collapsed.png"))
    at(6.8, js(day2))
    at(7.1, js(group))
    at(7.8, snap("mixed.png"))
    at(8.4, js(app_only))
    at(9.1, snap("app-only.png", last=True))


def settings_snapshots(win: CenterWindow, out: Path) -> None:
    """The list toolbar at the default 900px width, then every Settings tab."""
    out.mkdir(parents=True, exist_ok=True)

    def at(sec: float, fn) -> None:
        GLib.timeout_add(int(sec * 1000), lambda: (fn(), False)[1])

    def js(script: str):
        return lambda: win._eval(script)

    def snap(name: str, last: bool = False):
        return lambda: win.save_snapshot(out / name, (lambda ok: Gtk.main_quit()) if last else None)

    def go(tab: str):
        return js(f"window.nc.navigate('#/settings/{tab}')")

    def click(selector: str):
        return js(f"document.querySelector({selector!r}).click()")

    # the window opens at 900x640: the toolbar must fit on one line
    at(3.0, snap("list-900.png"))
    at(3.4, click("#btn-more-menu"))
    at(3.9, snap("list-menu.png"))
    at(4.2, js("document.body.click()"))
    t = 4.6
    at(t, click("#btn-settings"))
    for name in ("default", "crt-green", "night-ops-hud"):
        at(t + 0.8, click(f".sp-preset[data-preset='{name}']"))
        at(t + 1.6, snap(f"appearance-{name}.png"))
        t += 1.8
    # an invalid value: the error shows under the field, nothing is saved
    at(t + 0.4, click(".sp-preset[data-preset='default']"))
    at(
        t + 1.0,
        js(
            "var i = document.querySelector('[data-var=\"--nc-radius\"] .sp-num');"
            " i.value = ''; var f = document.querySelector('[data-var=\"--nc-opacity\"] .sp-num');"
            " f.value = '0.5'; f.dispatchEvent(new Event('input'));"
            " var t = document.querySelector('[data-var=\"--nc-shadow\"] .sp-text');"
            " t.value = '0 0 oops'; t.dispatchEvent(new Event('input'));"
            " document.querySelector('.sp-savebar .btn-primary').click()"
        ),
    )
    at(t + 1.9, snap("appearance-error.png"))  # the failed save scrolls the first error into view
    at(t + 2.2, js("document.querySelector('.sp-savebar .btn:nth-child(2)').click()"))
    at(t + 2.6, go("behavior"))
    at(t + 3.4, snap("behavior.png"))
    at(t + 3.7, go("sounds"))
    at(t + 4.2, click(".sp-play"))
    at(t + 4.8, snap("sounds.png"))
    # the Rules tab: list, an editor with buttons, the test panel, then the raw editor with an error
    r = t + 5.1
    at(r, go("rules"))
    at(r + 0.8, snap("rules-list.png"))
    at(r + 1.0, click(".rl-row[data-index='1'] .rl-actions .btn"))
    at(r + 1.6, snap("rules-edit.png"))
    at(r + 1.8, js("document.querySelector('.rl-section[data-section=buttons]').scrollIntoView()"))
    at(r + 2.2, snap("rules-edit-buttons.png"))
    at(r + 2.3, js("document.querySelector('.rl-section[data-section=script]').scrollIntoView()"))
    at(r + 2.35, snap("rules-edit-script.png"))
    # a sample that matches this rule: fill the form, add two hints, run the test
    at(
        r + 2.4,
        js(
            "var s = document.querySelector('.rl-section[data-section=test]');"
            " function put(e, v) { e.value = v; e.dispatchEvent(new Event('input')); }"
            " var tx = s.querySelectorAll('.rl-sample input.sp-text');"
            " put(tx[0], 'CI Runner'); put(tx[1], 'Build failed on main'); put(tx[2], 'build');"
            " var u = s.querySelector('.rl-sample select'); u.value = 'critical';"
            " u.dispatchEvent(new Event('change'));"
            " s.querySelector('.rl-sample .btn-small').click()"
        ),
    )
    at(r + 2.5, js("document.querySelector('.rl-sample .btn-small').click()"))
    at(
        r + 2.6,
        js(
            "var i = document.querySelectorAll('.rl-sample .rl-rowline input');"
            " function put(e, v) { e.value = v; e.dispatchEvent(new Event('input')); }"
            " put(i[0], 'x-status'); put(i[1], 'failed'); put(i[2], 'x-job'); put(i[3], 'nightly');"
            " document.querySelector('.rl-section[data-section=test] .btn-primary').click()"
        ),
    )
    at(r + 3.0, js("document.querySelector('.rl-result').scrollIntoView()"))
    at(r + 3.4, snap("rules-test.png"))
    at(r + 3.6, click(".rl-editbar .btn:last-child"))
    at(
        r + 4.2,
        js(
            "var a = document.getElementById('rl-source'); a.value = a.value.replace('volume = 70', 'volume = 700');"
            " a.dispatchEvent(new Event('input')); document.querySelector('.rl-editbar .btn-primary').click()"
        ),
    )
    at(r + 4.8, snap("rules-source-error.png"))
    at(r + 5.0, click(".rl-editbar .btn"))
    at(r + 5.3, click(".rl-discard .btn-danger"))
    t = r + 0.5  # the steps below keep their old offsets from t
    at(t + 5.1, go("advanced"))
    at(t + 5.4, click(".sp-testbar [data-test='critical']"))
    at(t + 5.9, snap("advanced.png"))
    # unsaved edit, then leave: the in-page confirm bar asks first
    at(
        t + 6.2,
        js(
            "var a = document.getElementById('sp-css'); a.value += '\\n.toast { }';"
            " a.dispatchEvent(new Event('input')); window.nc.navigate('#/')"
        ),
    )
    at(t + 6.9, snap("guard.png", last=True))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hold", action="store_true", help="stay open until Ctrl+C")
    ap.add_argument("--snapshot-dir", type=Path)
    ap.add_argument("--collapse-snapshots", type=Path)
    ap.add_argument("--settings-snapshots", type=Path)
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("gi").setLevel(logging.WARNING)

    tmp = tempfile.TemporaryDirectory(prefix="nc-center-demo-")
    store = Store(Path(tmp.name) / "demo.db")
    _, explicit = demo_icons.setup(Path(tmp.name) / "icons")
    seed(store, explicit)
    seed_buttons_and_runs(store, DETAIL_ID)

    def invoke(id: int, key: str) -> bool:
        n = store.get(id)
        print(f"on_invoke_action {id} {key}", flush=True)
        return n is not None and n.closed_at is None

    def mark_read(ids: list[int]) -> None:
        print(f"on_mark_read {ids}", flush=True)
        store.mark_read(ids, time.time())

    def delete(ids: list[int]) -> None:
        print(f"on_delete {ids}", flush=True)
        store.delete(ids)

    def clear() -> None:
        print("on_clear", flush=True)
        store.clear()

    def dismiss(id: int) -> None:
        print(f"on_dismiss {id}", flush=True)
        store.mark_closed(id, CloseReason.DISMISSED, time.time())

    def on_button(id: int, button: str) -> None:
        print(f"on_button {id} {button}", flush=True)

    win = CenterWindow(
        store,
        ROOT / "web",
        on_button=on_button,
        get_runs=store.runs_for,
        on_invoke_action=invoke,
        on_dismiss=dismiss,
        on_mark_read=mark_read,
        on_delete=delete,
        on_clear=clear,
        state_path=Path(tmp.name) / "center.json",  # never the real ~/.local/state file
        settings=DemoSettings(Path(tmp.name) / "config") if args.settings_snapshots else None,
    )
    win.set_user_css(".row.tag-urgent .row-summary { color: var(--nc-urgency-critical); }")
    win.show()
    if not (args.collapse_snapshots or args.settings_snapshots):
        GLib.timeout_add_seconds(3, lambda: (win.show_notification(DETAIL_ID), False)[1])
    # a live append, to see it land in the list
    live = Notification.from_notify(
        99,
        "Example App",
        0,
        "dialog-information",
        "Live append",
        "Arrived while open.",
        [],
        {"urgency": 1},
        -1,
        time.time(),
    )
    live.id = 99

    def append_live() -> bool:
        store.record(live)
        win.notify_added(live)
        return False

    if args.snapshot_dir:
        out = args.snapshot_dir
        out.mkdir(parents=True, exist_ok=True)
        GLib.timeout_add_seconds(2, lambda: (win.save_snapshot(out / "list.png"), False)[1])
        GLib.timeout_add_seconds(6, lambda: (win.save_snapshot(out / "details.png"), False)[1])
        GLib.timeout_add_seconds(
            7,
            lambda: (
                win._eval("document.querySelector('#view-detail .center-scroll').scrollTop = 9999"),
                False,
            )[1],
        )
        GLib.timeout_add_seconds(
            8,
            lambda: (
                win.save_snapshot(out / "details-bottom.png", lambda ok: Gtk.main_quit()),
                False,
            )[1],
        )
    elif args.collapse_snapshots:
        collapse_snapshots(win, args.collapse_snapshots)
    elif args.settings_snapshots:
        settings_snapshots(win, args.settings_snapshots)
    elif not args.hold:
        GLib.timeout_add_seconds(30, Gtk.main_quit)
        GLib.timeout_add_seconds(20, append_live)
    try:
        Gtk.main()
    except KeyboardInterrupt:
        pass
    win.teardown()
    deadline = time.monotonic() + 1.0
    while Gtk.events_pending() and time.monotonic() < deadline:
        Gtk.main_iteration_do(False)
    tmp.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
