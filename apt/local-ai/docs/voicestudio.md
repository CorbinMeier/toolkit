# VoiceStudio

A local, private ElevenLabs: text-to-speech, voice cloning from a short sample, voice design from a description, video dubbing, dictation, transcription and audiobooks, in 600+ languages. Nothing leaves your machine.

- Source: `repos/VoiceStudio` (https://github.com/debpalash/VoiceStudio, AGPL-3.0; each voice model has its own licence). Docs: `repos/VoiceStudio/docs/`
- App: `bin/voicestudio/VoiceStudio-0.5.7-linux-x64.AppImage` (checksum verified against the release's `SHA256SUMS.txt`)

## Start

```bash
voicestudio
```

The **first launch** sets up its Python runtime and asks before downloading each voice model (about 5 GB total on a CPU-only machine). The `voicestudio` launcher stores all of that in `local-ai/data/voicestudio/`, so after the first run the folder carries everything and the next machine skips the download.

## On this laptop (no NVIDIA GPU)

It runs on the CPU, slower. For good speed:
- Pick a **light engine** in the Model Manager: **KittenTTS**, **Supertonic-3** or **PocketTTS**.
- Use a **small Whisper** model for transcription and dictation.
- **OmniVoice GGUF** and **CosyVoice GGUF** engines can use the Radeon iGPU through Vulkan.

The launcher sets `OMNIVOICE_TORCH_VARIANT=cpu`. On a machine with an NVIDIA GPU, start it with `OMNIVOICE_TORCH_VARIANT=cuda voicestudio`.

## What to try first

1. **Studio**: type text, pick a voice, generate.
2. **Clone**: drop in 10 to 30 seconds of clean speech and save it as a voice profile.
3. **Design**: describe a voice ("calm female narrator, British") and save the one you like.
4. **Dub**: give it a video file or URL, choose a target language, and it transcribes, translates and re-voices it.
5. **Watch folders**: point one at a directory; text or audio dropped there is processed automatically.

## Power-user: drive it from code and agents

While the app runs, it serves an **OpenAI-compatible API** on `127.0.0.1:3900`, so any OpenAI SDK or script works:

```bash
# text to speech ("voice" = your cloned profile's name or id, or alloy/coral/...)
curl -s http://127.0.0.1:3900/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"model":"tts-1","voice":"alloy","input":"Hello from my laptop."}' -o hello.wav

# speech to text
curl -s http://127.0.0.1:3900/v1/audio/transcriptions -F file=@meeting.m4a
```

Requests from the same machine need no key. Live transcription runs over a WebSocket at `ws://127.0.0.1:3900/v1/audio/transcriptions/stream`.

**MCP**: agents (OpenCode, Claude Code, Codex) can use VoiceStudio as a tool at `http://127.0.0.1:3900/mcp/`. OpenCode setup is in docs/opencode.md. The app also exports ready-made client configs in **Settings → Model Context Protocol**.

**LLM features** (script writing, translation for dubbing): in Settings → LLM providers, choose an OpenAI-compatible provider with endpoint `http://127.0.0.1:8080/v1` to use your local Qwen (`qwen-server` running), or Ollama at `http://127.0.0.1:11434/v1`.

Deeper docs in the repo: `docs/agentic-voice.md` (full API), `docs/mcp.md`, `docs/performance.md` (speed by engine), `docs/dubbing/`.

## Where things live

| Path | Holds |
|---|---|
| `data/voicestudio/data` | projects, voice profiles, outputs (`OMNIVOICE_DATA_DIR`) |
| `data/voicestudio/models`, `engines`, `hf` | downloaded voice models and engine runtimes |

Launching the AppImage directly (not via `voicestudio`) uses `~/.config` defaults instead and will not see this data.

## Updating

Download the newer `VoiceStudio-<version>-linux-x64.AppImage` from https://github.com/debpalash/VoiceStudio/releases into `bin/voicestudio/` (the launcher picks the newest) and delete the old one. Check it with `sha256sum` against that release's `SHA256SUMS.txt`.
