#!/usr/bin/env bash
# Spec §8 budget check: CPU, steady RSS, first-frame time and binary size of the release build.
# Runs vitals for 60 s at 200x50 in a pty (`script`), with no display variables, after one uncounted
# `--once` run that fills the token index so the backfill is not measured. Figures come from the program
# itself (/proc/self/stat and status, hidden --stats flag). Last line: BENCH PASS or BENCH FAIL.
set -euo pipefail
cd "$(dirname "$0")/.."
unset WAYLAND_DISPLAY DISPLAY

SECONDS_TO_RUN=${BENCH_SECONDS:-60}
MAX_CPU_PERCENT=0.5      # of one core
MAX_RSS_KB=8192          # 8 MB
MAX_FIRST_FRAME_MS=30
MAX_BINARY_BYTES=$((3250 * 1000))

cargo build --release --quiet
bin=target/release/vitals
stats=$(mktemp)
trap 'rm -f "$stats"' EXIT

echo "warming the token index (uncounted)..." >&2
"$bin" --once --size 200x50 >/dev/null

echo "running ${SECONDS_TO_RUN}s in a pty..." >&2
script -qfc "stty cols 200 rows 50; exec $bin --exit-after $SECONDS_TO_RUN --stats $stats" /dev/null >/dev/null

get() { sed -n "s/^$1=//p" "$stats"; }
hz=$(get clk_tck)
total=$(get cpu_ticks_total)
after5=$(get cpu_ticks_after_5s)
elapsed=$(get elapsed_s)
rss=$(get rss_kb)
hwm=$(get hwm_kb)
first=$(get first_frame_ms)
size=$(stat -c %s "$bin")

# CPU over the time after the first 5 s, as a percent of one core.
cpu=$(awk -v t="$total" -v a="$after5" -v hz="$hz" -v e="$elapsed" 'BEGIN { printf "%.3f", (t - a) / hz / (e - 5) * 100 }')

printf 'cpu            %s %% of one core   (budget <= %s)\n' "$cpu" "$MAX_CPU_PERCENT"
printf 'rss (steady)   %s kB               (budget <= %s; peak %s kB)\n' "$rss" "$MAX_RSS_KB" "$hwm"
printf 'first frame    %s ms               (budget <= %s)\n' "$first" "$MAX_FIRST_FRAME_MS"
printf 'binary         %s bytes            (budget <= %s)\n' "$size" "$MAX_BINARY_BYTES"

fail=0
awk -v v="$cpu" -v m="$MAX_CPU_PERCENT" 'BEGIN { exit !(v > m) }' && { echo "CPU over budget" >&2; fail=1; }
awk -v v="$rss" -v m="$MAX_RSS_KB" 'BEGIN { exit !(v > m) }' && { echo "RSS over budget" >&2; fail=1; }
awk -v v="$first" -v m="$MAX_FIRST_FRAME_MS" 'BEGIN { exit !(v > m) }' && { echo "first frame over budget" >&2; fail=1; }
[ "$size" -gt "$MAX_BINARY_BYTES" ] && { echo "binary over budget" >&2; fail=1; }
if [ "$fail" -eq 0 ]; then echo "BENCH PASS"; else echo "BENCH FAIL"; exit 1; fi
