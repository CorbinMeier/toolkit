#!/bin/bash
# Toggle dictation: first call starts recording, second stops, transcribes (Parakeet) and types the text.
# The tray icon (tray.py) follows the state written to $STATE.
D="$(cd "$(dirname "$0")" && pwd)"
PID=/tmp/parakeet-dictate.pid
WAV=/tmp/parakeet-dictate.wav
STATE=/tmp/parakeet-state
if [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; then
  kill "$(cat "$PID")"; rm -f "$PID"; sleep 0.2
  echo busy > "$STATE"
  text=$("$D/venv/bin/python" "$D/transcribe.py" "$WAV" 2>/dev/null)
  [ -n "$text" ] && wtype -- "$text "
  rm -f "$WAV"
  echo idle > "$STATE"
else
  parecord --channels=1 --rate=16000 --format=s16le --file-format=wav "$WAV" &
  echo $! > "$PID"
  echo listening > "$STATE"
fi
