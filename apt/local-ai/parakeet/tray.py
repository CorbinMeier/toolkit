"""Tray front end for the dictation daemon (daemon.py).

Icon: red = ready, green = listening, amber = transcribing/loading, grey = daemon not running.
Menu: status, Start/Stop, mode, Settings…, Quit. Run with the system python3 (needs gi).
"""
import json, os, socket, subprocess
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("AppIndicator3", "0.1")
from pathlib import Path
from gi.repository import Gtk, GLib, AppIndicator3 as AI

HERE = Path(__file__).parent
CONF = Path.home() / ".config/parakeet-dictate/config.json"
SOCK = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "parakeet.sock"
DEFAULTS = {"tap_delay_ms": 400, "precapture": True, "max_seconds": 300, "mode": "record"}
LIVE_READY = True


def read_cfg():
    try:
        d = json.loads(CONF.read_text())
    except (OSError, ValueError):
        d = {}
    return {**DEFAULTS, **d}


def write_cfg(**changes):
    CONF.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONF.with_suffix(".tmp")
    tmp.write_text(json.dumps({**read_cfg(), **changes}, indent=2))
    tmp.replace(CONF)


def ask(cmd):
    """One request to the daemon; None if it isn't running."""
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(0.5)
    try:
        s.connect(str(SOCK))
        s.sendall((json.dumps({"cmd": cmd}) + "\n").encode())
        return json.loads(s.makefile().readline())
    except (OSError, ValueError):
        return None
    finally:
        s.close()


# ---- settings window -------------------------------------------------
settings_win = None


def open_settings(_=None):
    global settings_win
    if settings_win:
        settings_win.present()
        return
    c = read_cfg()
    win = Gtk.Window(title="Dictation settings")
    win.set_default_size(420, -1)
    win.set_border_width(16)
    grid = Gtk.Grid(column_spacing=16, row_spacing=14)
    win.add(grid)

    def row(i, label, widget, hint):
        l = Gtk.Label(label=label, xalign=0)
        h = Gtk.Label(xalign=0)
        h.set_markup(f"<small>{hint}</small>")
        h.set_line_wrap(True)
        h.set_max_width_chars(44)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.pack_start(widget, False, False, 0)
        box.pack_start(h, False, False, 0)
        grid.attach(l, 0, i, 1, 1)
        grid.attach(box, 1, i, 1, 1)
        l.set_valign(Gtk.Align.START)

    delay = Gtk.SpinButton.new_with_range(100, 1000, 25)
    delay.set_value(c["tap_delay_ms"])
    delay.connect("value-changed", lambda w: write_cfg(tap_delay_ms=int(w.get_value())))
    row(0, "Double-tap gap (ms)", delay,
        "Longest pause between the two Right Alt taps. Lower = fewer accidental triggers, higher = easier to hit.")

    pre = Gtk.Switch(halign=Gtk.Align.START)
    pre.set_active(bool(c["precapture"]))
    pre.connect("notify::active", lambda w, _p: write_cfg(precapture=w.get_active()))
    row(1, "Start mic on first tap", pre,
        "Records from the first tap (dropped if no second tap) so the start of your speech isn't clipped. "
        "Off = the mic opens only after the double-tap.")

    mx = Gtk.SpinButton.new_with_range(10, 900, 10)
    mx.set_value(c["max_seconds"])
    mx.connect("value-changed", lambda w: write_cfg(max_seconds=int(w.get_value())))
    row(2, "Max recording (s)", mx, "Recording stops and types automatically after this long.")

    note = Gtk.Label(xalign=0)
    note.set_markup("<small>Changes apply immediately.</small>")
    grid.attach(note, 0, 3, 2, 1)

    def closed(*_):
        global settings_win
        settings_win = None

    win.connect("destroy", closed)
    settings_win = win
    win.show_all()


# ---- indicator and menu ----------------------------------------------
ind = AI.Indicator.new_with_path("parakeet-dictate", "parakeet-off",
                                 AI.IndicatorCategory.APPLICATION_STATUS, str(HERE / "icons"))
ind.set_status(AI.IndicatorStatus.ACTIVE)
ind.set_title("Dictation")

menu = Gtk.Menu()
status_item = Gtk.MenuItem(label="Dictation service not running")
status_item.set_sensitive(False)
menu.append(status_item)

toggle_item = Gtk.MenuItem(label="Start dictation")
toggle_item.connect("activate", lambda _: ask("toggle"))
menu.append(toggle_item)

menu.append(Gtk.SeparatorMenuItem())


def set_mode(item, name):
    if item.get_active():
        write_cfg(mode=name)


rec_item = Gtk.RadioMenuItem(label="Mode: Record, then type")
live_item = Gtk.RadioMenuItem.new_with_label_from_widget(rec_item, "Mode: Live (types as you pause)")
live_item.set_sensitive(LIVE_READY)
(live_item if read_cfg()["mode"] == "live" and LIVE_READY else rec_item).set_active(True)
rec_item.connect("toggled", set_mode, "record")
live_item.connect("toggled", set_mode, "live")
menu.append(rec_item)
menu.append(live_item)

menu.append(Gtk.SeparatorMenuItem())

settings_item = Gtk.MenuItem(label="Settings…")
settings_item.connect("activate", open_settings)
menu.append(settings_item)


def quit_all(_):
    ask("quit")
    Gtk.main_quit()


quit_item = Gtk.MenuItem(label="Quit (stops dictation)")
quit_item.connect("activate", quit_all)
menu.append(quit_item)

menu.show_all()
ind.set_menu(menu)

VIEW = {  # state -> (icon, status text, toggle label, toggle enabled)
    "offline": ("parakeet-off", "Dictation service not running", "Start dictation", False),
    "loading": ("parakeet-busy", "Loading model…", "Start dictation", False),
    "idle": ("parakeet-idle", "Ready — double-tap Right Alt", "Start dictation", True),
    "listening": ("parakeet-listening", "Listening…", "Stop dictation", True),
    "busy": ("parakeet-busy", "Transcribing…", "Start dictation", False),
}
current = None


def poll():
    global current
    r = ask("status")
    s = r["state"] if r and r.get("state") in VIEW else "offline"
    err = r.get("error", "") if r else ""
    key = (s, err)
    if key != current:
        current = key
        icon, status, action, enabled = VIEW[s]
        ind.set_icon_full(icon, s)
        status_item.set_label(f"{status} ({err})" if err else status)
        toggle_item.set_label(action)
        toggle_item.set_sensitive(enabled)
    return True


GLib.timeout_add(200, poll)
Gtk.main()
