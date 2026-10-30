# Public

My workspace tooling for a Linux desktop (Pop!_OS 24.04 with COSMIC, Wayland). Two halves, each with its own README:

| Directory | What it is |
|---|---|
| [`cosmic/`](cosmic/README.md) | COSMIC desktop tools: a replacement notification service and panel applet, a timer applet, a Claude plan-usage applet, and `vitals`, a terminal system dashboard. Rust and Python. |
| [`local-ai/`](local-ai/README.md) | Offline AI kit: a local Qwen model, OpenCode, VoiceStudio, OpenSEO, and Parakeet push-to-dictate. Shell and Python. |

Each tool's README has an **Outside this repo** section. Read it before installing: these tools write to `~/.local`, `~/.config` and systemd user units, and some replace or shadow a running system service. All of it is reversible with the tool's own `scripts/uninstall.sh`.

Upstream projects (llama.cpp, OpenCode, libcosmic and others) are **prerequisites you get from their own repos**, not copies: see [`PREREQUISITES.md`](PREREQUISITES.md).

## Honest caveats
- **Built for one machine.** Paths, the COSMIC 1.11.0 libcosmic pin, the 150% display scale and the `input` group are all this laptop. Expect to adjust.
- **`CLAUDE.md` files are agent instructions**, not user docs. They describe how I work with Claude Code on each project.
- **Large downloads are not here.** `local-ai/bootstrap.sh` fetches binaries and models; Parakeet's models have no recorded source yet.
- **Licence: MIT** (see `LICENSE`). Copy it freely; keep the copyright notice, which is the credit.
