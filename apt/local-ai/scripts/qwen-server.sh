#!/usr/bin/env bash
# Serve Qwen3.8-27B with llama.cpp: web chat + OpenAI-compatible API on http://127.0.0.1:8080
# Env overrides: BACKEND=cpu|vulkan  CTX=16384  NGL=<gpu layers>  VISION=1  THINK=0  PORT=8080  HOST=127.0.0.1  MODEL=<gguf>
# Extra args pass straight to llama-server (see: llama-server --help).
set -euo pipefail
ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
# cpu by default: on the Radeon 860M (RADV) Vulkan loads fail with "Not enough memory for command submission".
BACKEND="${BACKEND:-cpu}"
MODEL="${MODEL:-$ROOT/models/qwen3.8-27b/Qwen3.8-27B-UD-IQ4_XS.gguf}"
MMPROJ="$ROOT/models/qwen3.8-27b/mmproj-F16.gguf"

args=(-m "$MODEL" --jinja -c "${CTX:-16384}" --host "${HOST:-127.0.0.1}" --port "${PORT:-8080}" --alias qwen3.8-27b)
[[ ${VISION:-0} == 1 && -f $MMPROJ ]] && args+=(--mmproj "$MMPROJ")
[[ -n ${NGL:-} ]] && args+=(-ngl "$NGL")
# Sampling recommended on the Qwen3.8 model card for each mode.
if [[ ${THINK:-1} == 1 ]]; then
  args+=(--temp 1.0 --top-p 0.95 --top-k 20 --min-p 0)
else
  args+=(--reasoning off --temp 0.7 --top-p 0.8 --top-k 20 --min-p 0 --presence-penalty 1.5)
fi

exec "$ROOT/bin/llama.cpp-$BACKEND/llama-server" "${args[@]}" "$@"
