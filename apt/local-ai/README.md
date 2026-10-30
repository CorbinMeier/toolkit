# local-ai

A self-contained kit for running AI tools offline. Copy this whole folder to another Linux x64 machine and it works there too.

| Tool | What it is | Start it | Guide |
|---|---|---|---|
| **Qwen3.8 27B** | Local LLM (Qwen's Aug 2026 model), served by llama.cpp | `qwen-server` → http://127.0.0.1:8080 | [docs/qwen.md](docs/qwen.md) |
| **OpenCode** | Terminal coding agent (like Claude Code); add a model with `/connect` | `oc` | [docs/opencode.md](docs/opencode.md) |
| **VoiceStudio** | Local ElevenLabs: TTS, voice cloning, dubbing, transcription | `voicestudio` | [docs/voicestudio.md](docs/voicestudio.md) |
| **Parakeet dictation** | Double-tap Right Alt to dictate anywhere; local speech-to-text | systemd user units (`parakeet-*`), see `parakeet/README.md` | [parakeet/README.md](parakeet/README.md) |
| **OpenSEO** | Self-hosted Semrush/Ahrefs (needs a paid DataForSEO key) | `openseo up` → http://localhost:3001 | [docs/openseo.md](docs/openseo.md) |

## First run on any machine

```bash
cd /path/to/local-ai
./bootstrap.sh                # downloads whatever is missing (verified), then links the commands
./bootstrap.sh --no-model     # same, without the 15 GB Qwen model
./bootstrap.sh --no-docker    # same, without the OpenSEO images
```

Copied the full folder over already? `scripts/link-commands.sh` on its own just adds `qwen-server`, `oc`, `voicestudio` and `openseo` to `~/.local/bin`. Version pins (llama.cpp, OpenCode, VoiceStudio, model file) are at the top of `bootstrap.sh`.

Then open a terminal and use the commands above. `~/.local/bin` must be on your PATH (it is by default on Ubuntu/Pop!_OS).

## What's in here

```
local-ai/
├── README.md            ← you are here
├── bootstrap.sh         ← rebuilds everything below from the internet (idempotent, checksummed)
├── docs/                ← one guide per tool
├── scripts/             ← the launchers (the short commands link to these)
├── config/opencode/     ← portable OpenCode config, custom commands, agents
├── bin/
│   ├── llama.cpp-vulkan ← llama.cpp b11998, GPU build (AMD/Intel/NVIDIA via Vulkan)
│   ├── llama.cpp-cpu    ← same, CPU-only fallback
│   ├── opencode/        ← OpenCode 1.19.5 standalone binary
│   └── voicestudio/     ← VoiceStudio 0.5.7 AppImage (+ SHA256SUMS.txt, verified)
├── models/qwen3.8-27b/  ← Qwen3.8-27B GGUF (IQ4_XS, 14.3 GB) + vision projector
├── images/              ← OpenSEO Docker images, saved for offline `docker load`
├── data/                ← VoiceStudio's data, engines and voice models (fills on first use)
└── repos/               ← full git clones: opencode, open-seo, VoiceStudio (source + docs)
```

## Prerequisites
OpenCode, VoiceStudio, OpenSEO, llama.cpp, the Qwen model and Parakeet's runtime are other people's projects. They are fetched from their own repos, never copied into this one. Links and the version policy: [`../../PREREQUISITES.md`](../../PREREQUISITES.md).

## Copying to another machine

```bash
rsync -a --info=progress2 ~/local-ai/ otherhost:~/local-ai/   # or copy to a USB drive
```

What the target needs (nothing else to download):

| Needs | Why | Check |
|---|---|---|
| Linux x86_64 with a desktop | All binaries are x64; VoiceStudio is a GUI app | `uname -m` |
| Vulkan driver (Mesa), optional | Only for `BACKEND=vulkan qwen-server`. The default is the CPU build | `vulkaninfo --summary` |
| Docker + Compose | OpenSEO only | `docker compose version` |
| ~16 GB free RAM | Qwen3.8 27B. Less RAM → see the smaller-model notes in docs/qwen.md | `free -g` |

Updating a repo later: `git -C repos/<name> pull`. Updating a binary: see each guide.

## Outside this repo / what git tracks
- This folder lives in `apt/` of the `toolkit` repo. `.gitignore` leaves out everything `bootstrap.sh` can rebuild: `bin/`, `models/`, `images/`, `repos/`, `data/`, and Parakeet's `venv/` and models. What is tracked is small: scripts, docs, the OpenCode config and the Parakeet source.
- `~/.local/bin/{qwen-server,oc,voicestudio,openseo}` are symlinks into `scripts/` made by `scripts/link-commands.sh`; they break if this folder moves.
- Parakeet's three systemd units live in `~/.config/systemd/user/`; `parakeet/systemd/` only holds copies.
- Parakeet's models are not fetched by `bootstrap.sh` yet, so "copy the folder to another machine" is the only way to move them.
- This is the general (non-COSMIC) tooling. COSMIC desktop tools live in `../../cosmic/`; see the root README for the folder rules.
