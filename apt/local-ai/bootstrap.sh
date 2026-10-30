#!/usr/bin/env bash
# Rebuild this local-ai kit from the internet: clones, binaries, model, Docker images, command links.
# Idempotent: anything already present (and verified) is skipped, so rerunning it only fills gaps.
# Usage: ./bootstrap.sh            everything
#        ./bootstrap.sh --no-model skip the 15 GB Qwen download
#        ./bootstrap.sh --no-docker skip the OpenSEO images
# Bump a version by editing the pins below, then rerun.
set -euo pipefail
ROOT="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$ROOT"

LLAMA_BUILD=b11998
OPENCODE_VERSION=1.19.5
VOICESTUDIO_VERSION=0.5.7
QWEN_REPO=unsloth/Qwen3.8-27B-GGUF
QWEN_FILE=Qwen3.8-27B-UD-IQ4_XS.gguf
QWEN_MMPROJ=mmproj-F16.gguf

WANT_MODEL=1; WANT_DOCKER=1
for a in "$@"; do
  case $a in
    --no-model) WANT_MODEL=0 ;;
    --no-docker) WANT_DOCKER=0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
need() { command -v "$1" >/dev/null || { echo "missing: $1 ($2)" >&2; exit 1; }; }
fetch() { curl -fL --retry 5 -C - --progress-bar -o "$1" "$2"; }

need git "sudo apt install git"
need curl "sudo apt install curl"
need sha256sum "coreutils"
need python3 "sudo apt install python3"
mkdir -p repos bin models/qwen3.8-27b images data

say "Repos"
for r in anomalyco/opencode every-app/open-seo debpalash/VoiceStudio; do
  d="repos/${r#*/}"
  if [[ -d $d/.git ]]; then git -C "$d" pull --ff-only --quiet && echo "updated $d"
  else git clone --quiet "https://github.com/$r.git" "$d" && echo "cloned $d"; fi
done

say "llama.cpp $LLAMA_BUILD (cpu + vulkan)"
for v in cpu:ubuntu-x64 vulkan:ubuntu-vulkan-x64; do
  dir="bin/llama.cpp-${v%%:*}"
  if [[ -x $dir/llama-server && $(cat "$dir/.build" 2>/dev/null) == "$LLAMA_BUILD" ]]; then echo "ok $dir"; continue; fi
  tmp=$(mktemp -d)
  fetch "$tmp/l.tgz" "https://github.com/ggml-org/llama.cpp/releases/download/$LLAMA_BUILD/llama-$LLAMA_BUILD-bin-${v#*:}.tar.gz"
  rm -rf "$dir"; mkdir -p "$dir"
  tar xzf "$tmp/l.tgz" -C "$dir" --strip-components=1 && echo "$LLAMA_BUILD" > "$dir/.build"
  rm -rf "$tmp"
done

say "OpenCode $OPENCODE_VERSION"
if [[ -x bin/opencode/opencode && $(cat bin/opencode/.version 2>/dev/null) == "$OPENCODE_VERSION" ]]; then echo "ok bin/opencode"
else
  tmp=$(mktemp -d)
  fetch "$tmp/o.tgz" "https://github.com/anomalyco/opencode/releases/download/v$OPENCODE_VERSION/opencode-linux-x64.tar.gz"
  mkdir -p bin/opencode && tar xzf "$tmp/o.tgz" -C bin/opencode && echo "$OPENCODE_VERSION" > bin/opencode/.version
  rm -rf "$tmp"
fi

say "VoiceStudio $VOICESTUDIO_VERSION"
vs=bin/voicestudio; app="VoiceStudio-$VOICESTUDIO_VERSION-linux-x64.AppImage"
mkdir -p "$vs"
base="https://github.com/debpalash/VoiceStudio/releases/download/v$VOICESTUDIO_VERSION"
curl -fsSL -o "$vs/SHA256SUMS.txt" "$base/SHA256SUMS.txt"
if ! (cd "$vs" && grep " $app\$" SHA256SUMS.txt | sha256sum -c --quiet - 2>/dev/null); then
  rm -f "$vs/$app"; fetch "$vs/$app" "$base/$app"
  (cd "$vs" && grep " $app\$" SHA256SUMS.txt | sha256sum -c -)
else echo "ok $vs/$app (checksum)"; fi
chmod +x "$vs/$app"

if [[ $WANT_MODEL == 1 ]]; then
  say "Qwen3.8 27B ($QWEN_FILE + $QWEN_MMPROJ, ~15 GB)"
  tree=$(curl -fsSL "https://huggingface.co/api/models/$QWEN_REPO/tree/main")
  for f in "$QWEN_FILE" "$QWEN_MMPROJ"; do
    dst="models/qwen3.8-27b/$f"
    want=$(python3 -c "import json,sys;print(next(x['lfs']['oid'] for x in json.loads(sys.argv[1]) if x['path']==sys.argv[2]))" "$tree" "$f")
    if [[ -f $dst && $(cat "$dst.sha256" 2>/dev/null) == "$want" ]]; then echo "ok $dst"; continue; fi
    fetch "$dst" "https://huggingface.co/$QWEN_REPO/resolve/main/$f"
    echo "verifying $f ..."
    [[ $(sha256sum "$dst" | cut -d' ' -f1) == "$want" ]] || { echo "CHECKSUM MISMATCH: $dst (delete it and rerun)" >&2; exit 1; }
    echo "$want" > "$dst.sha256"
  done
fi

if [[ $WANT_DOCKER == 1 ]]; then
  say "OpenSEO Docker images"
  if ! command -v docker >/dev/null || ! docker info >/dev/null 2>&1; then
    echo "skipped: Docker not installed or not usable without sudo"
  else
    COMPOSE_FILE=repos/open-seo/deploy/standalone/compose.yaml \
      docker compose --env-file repos/open-seo/deploy/standalone/.env.example pull -q
    "$ROOT/scripts/openseo.sh" save-images
    ls -lh images/open-seo-images.tar
  fi
fi

say "Commands"
"$ROOT/scripts/link-commands.sh"

say "Done"
du -sh repos bin models images data 2>/dev/null
echo "Start here: $ROOT/README.md"
