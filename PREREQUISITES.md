# Prerequisites: upstream projects

This repo **does not contain or copy** any of the projects below. No vendoring, no git submodules. They are things you install or clone yourself, from their own repos, and the goal is always **their most up-to-date code**. Credit and licences are theirs.

## local-ai

| Project | What for | Source |
|---|---|---|
| llama.cpp | Runs the local Qwen model | https://github.com/ggml-org/llama.cpp |
| Qwen3.8-27B (GGUF quant by Unsloth) | The local language model | https://huggingface.co/unsloth/Qwen3.8-27B-GGUF |
| OpenCode | Terminal coding agent | https://github.com/anomalyco/opencode |
| VoiceStudio (AGPL-3.0) | Local text-to-speech, voice cloning, dubbing, transcription | https://github.com/debpalash/VoiceStudio |
| OpenSEO | Self-hosted SEO tool (needs a paid DataForSEO key) | https://github.com/every-app/open-seo |
| sherpa-onnx | Runs Parakeet speech-to-text locally | https://github.com/k2-fsa/sherpa-onnx |
| NVIDIA Parakeet TDT (v2 English, v3 multilingual) | Dictation model; int8 ONNX builds from the sherpa-onnx project. The exact download source is not recorded yet. | https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3 |
| wtype | Types dictated text on Wayland | https://github.com/atx/wtype |
| python-evdev | Reads the dictation hotkey | https://github.com/gvalkov/python-evdev |

## cosmic

| Project | What for | Source |
|---|---|---|
| COSMIC desktop (Pop!_OS 24.04) | The desktop these tools extend | https://github.com/pop-os/cosmic-epoch |
| libcosmic | Toolkit for the three panel applets | https://github.com/pop-os/libcosmic |
| cosmic-applets | Reference for applet layout; its `Cargo.lock` decides the libcosmic revision | https://github.com/pop-os/cosmic-applets |
| cosmic-notifications | The stock notification service that `cosmic-notification-center` replaces | https://github.com/pop-os/cosmic-notifications |
| Claude Code | `statusLine` feed for the usage applet and `vitals` | https://github.com/anthropics/claude-code |
| Rust toolchain | Builds the applets and `vitals` | https://rustup.rs |

## Where this policy bends
- **libcosmic is pinned** by `rev` in each applet's `Cargo.toml` to match the installed COSMIC release (1.11.0). Latest libcosmic will not necessarily run against an older desktop. Re-pin after a COSMIC upgrade; steps are in `cosmic/cosmic-applet-timer/CLAUDE.md`.
- **`local-ai/bootstrap.sh` pins versions** of llama.cpp, OpenCode and VoiceStudio (with checksums) for reproducible installs. To follow upstream's latest instead, bump the pins at the top of the script, or change it to resolve the newest release.
