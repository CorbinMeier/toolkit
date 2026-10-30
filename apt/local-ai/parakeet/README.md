# parakeet: local dictation

Double-tap Right Alt to start dictating, double-tap again to stop; the text is typed at the cursor. Speech to text is NVIDIA Parakeet TDT run locally through sherpa-onnx (no network). The tray icon shows idle, listening, busy or off.

| File | Job |
|---|---|
| `daemon.py` | Owns the hotkey (evdev), the mic, the model and the state; tray talks to it over `$XDG_RUNTIME_DIR/parakeet.sock` |
| `transcribe.py` | Loads the model and transcribes a 16 kHz mono wav |
| `hotkey.py`, `dictate.sh` | Older toggle path, superseded by `daemon.py`. `parakeet-hotkey.service` is currently inactive: do not enable it alongside the daemon or both react to the same keypress |
| `tray.py` | Tray icon (GTK, system `python3`), icons in `icons/` |
| `systemd/` | Copies of the three user units |

## Outside this repo
- **Not tracked, large, must be re-fetched on a new machine:** `venv/` (128 MB), `model-v2/` (English-only, used by `transcribe.py`, 631 MB) and `model/` (multilingual v3, 641 MB). `bootstrap.sh` does not fetch them yet; there is no recorded download source, so write one down before relying on a rebuild.
- **The live units are in `~/.config/systemd/user/`**, not here. `systemd/` holds copies; after editing either, copy to the other and run `systemctl --user daemon-reload`. Units: `parakeet-daemon`, `parakeet-hotkey`, `parakeet-tray`. They hardcode `%h/local-ai/parakeet`, so the folder must live at `~/local-ai`.
- **Needs `/dev/input` access**: your user must be in the `input` group (it is on this machine). Without it the hotkey silently never fires.
- **Needs these system tools:** `wtype` (types the text), `parecord` (mic), and `python3-gi` with the AppIndicator3 typelib for the tray (`GI_TYPELIB_PATH` is set in `parakeet-tray.service`).
- Config: `~/.config/parakeet-dictate/config.json` (double-tap gap and the like), not tracked. `DICTATE_DRYRUN=1` logs the text instead of typing it.
- Scratch files in `/tmp`: `parakeet-dictate.{pid,wav}`, `parakeet-state` (the old `dictate.sh` path).
- Built on 2026-10-30; not yet tested after a reboot or a COSMIC upgrade.
