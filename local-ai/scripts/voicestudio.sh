#!/usr/bin/env bash
# Launch the VoiceStudio AppImage with its data, engines and model weights kept inside local-ai/data,
# so they travel with the folder. Local API + MCP: http://127.0.0.1:3900
set -euo pipefail
ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
DATA="$ROOT/data/voicestudio"
mkdir -p "$DATA"/{data,models,engines,hf}
export OMNIVOICE_DATA_DIR="$DATA/data"
export OMNIVOICE_CACHE_DIR="$DATA/models"
export OMNIVOICE_ENGINES_DIR="$DATA/engines"
export HF_HOME="$DATA/hf"
# No NVIDIA driver on this machine: skip the 3 GB CUDA download. Set cuda on an NVIDIA box.
export OMNIVOICE_TORCH_VARIANT="${OMNIVOICE_TORCH_VARIANT:-cpu}"
APP="$(ls "$ROOT"/bin/voicestudio/VoiceStudio-*-linux-x64.AppImage | sort -V | tail -1)"
exec "$APP" "$@"
