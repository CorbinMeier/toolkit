#!/usr/bin/env bash
# Manual exit stress: needs a live display (WAYLAND_DISPLAY or DISPLAY). NOT part of check.sh.
# Usage: scripts/exit-stress.sh [N]      (N runs per scenario, default 20)
#   (a) N runs each of dev-center-demo.py and dev-shade-demo.py with --snapshot-dir
#       (PYTHONFAULTHANDLER=1, MALLOC_PERTURB_=165)
#   (b) N runs of bin/notifyd on a private dbus-run-session bus with the display on: send 3
#       notifications, --toggle-shade, --show-center, wait 2 s, SIGTERM, expect exit 0
# Fails on a non-zero exit or stderr matching the crash patterns below. Never touches the live
# bus, DB or config: everything lives in mktemp -d dirs removed by a trap.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
ROOT=$PWD
PY=/usr/bin/python3
BAD='corrupted|double-linked|free\(\):|Fatal Python|assertion failed|CRITICAL'

if [ "${1:-}" = "--inner" ]; then
  # runs inside dbus-run-session: $2 = temp dir, $3 = run label
  tmp=$2
  addr=$DBUS_SESSION_BUS_ADDRESS
  err=$tmp/daemon.err
  "$ROOT/bin/notifyd" --no-run --no-sound --db "$tmp/h.db" --config "$tmp/config.toml" \
    --state-dir "$tmp" --bus-address "$addr" 2>"$err" &
  pid=$!
  for _ in $(seq 100); do
    gdbus call --session -d org.freedesktop.DBus -o /org/freedesktop/DBus \
      -m org.freedesktop.DBus.NameHasOwner net.corbinmeier.Notifications1 2>/dev/null |
      grep -q true && break
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.1
  done
  for i in 1 2 3; do
    gdbus call --session -d org.freedesktop.Notifications -o /org/freedesktop/Notifications \
      -m org.freedesktop.Notifications.Notify stress 0 "" "Stress $i" "Body $i" "[]" "{}" 8000 \
      >/dev/null 2>>"$err"
  done
  "$ROOT/bin/notifyd" --toggle-shade --bus-address "$addr" 2>>"$err"
  "$ROOT/bin/notifyd" --show-center --bus-address "$addr" 2>>"$err"
  sleep 2
  kill -TERM "$pid" 2>/dev/null
  wait "$pid"
  echo "$?" >"$tmp/exit"
  exit 0
fi

N=${1:-20}
[[ $N =~ ^[0-9]+$ ]] || { echo "usage: $0 [N]" >&2; exit 2; }
[ -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ] || { echo "no display (WAYLAND_DISPLAY/DISPLAY unset)" >&2; exit 2; }
WORK=$(mktemp -d -t nc-exit-stress.XXXXXX)
trap 'rm -rf "$WORK"' EXIT
fails=0

check() { # label code errfile
  local label=$1 code=$2 errf=$3
  if [ "$code" -ne 0 ]; then
    echo "FAIL $label: exit $code"
    tail -5 "$errf" | sed 's/^/    /'
    fails=$((fails + 1))
  elif grep -Eq "$BAD" "$errf"; then
    echo "FAIL $label: stderr matched"
    grep -E "$BAD" "$errf" | head -3 | sed 's/^/    /'
    fails=$((fails + 1))
  fi
}

for demo in dev-center-demo dev-shade-demo; do
  for i in $(seq "$N"); do
    d=$WORK/$demo-$i
    mkdir -p "$d"
    PYTHONFAULTHANDLER=1 MALLOC_PERTURB_=165 "$PY" "scripts/$demo.py" --snapshot-dir "$d/snap" \
      >"$d/out" 2>"$d/err"
    check "$demo run $i" "$?" "$d/err"
  done
  echo "(a) $demo: $N runs done"
done

for i in $(seq "$N"); do
  d=$WORK/daemon-$i
  mkdir -p "$d"
  : >"$d/config.toml"
  PYTHONFAULTHANDLER=1 MALLOC_PERTURB_=165 dbus-run-session -- "$0" --inner "$d" >"$d/out" 2>"$d/runerr"
  code=$(cat "$d/exit" 2>/dev/null || echo missing)
  [ "$code" = missing ] && code=99
  cat "$d/runerr" >>"$d/daemon.err" 2>/dev/null
  check "notifyd run $i" "$code" "$d/daemon.err"
done
echo "(b) notifyd: $N runs done"

if [ "$fails" -eq 0 ]; then
  echo "EXIT-STRESS PASS ($N runs per scenario)"
else
  echo "EXIT-STRESS FAIL ($fails failing runs)"
  exit 1
fi
