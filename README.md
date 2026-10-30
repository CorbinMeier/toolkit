# toolkit

My software toolkit for a Linux desktop (Pop!_OS 24.04 with COSMIC, Wayland): desktop tools, a terminal dashboard, and an offline AI kit.

## The folder tells you what a tool needs
Every tool lives in the folder named for the most specific thing it relies on. **Folder position is the dependency rule.**

| Folder | A tool here needs | May depend on |
|---|---|---|
| [`libs/`](libs/README.md) | Only a language toolchain (Rust). No desktop, no distro. | nothing |
| [`apt/`](apt/README.md) | A Linux machine with **apt** (Debian, Ubuntu, Pop!_OS). On pacman, dnf or similar, a sibling `pacman/` or `dnf/` folder would hold the equivalents. | `libs/` |
| [`cosmic/`](cosmic/README.md) | The **COSMIC** desktop (COSMIC 1.11.0 on Pop!_OS 24.04). | `libs/` |
| [`archive/`](archive/README.md) | Nothing: retired, unmaintained, kept for reference. | nothing depends on it |

- Dependencies point **down the table only**: `apt/` and `cosmic/` may use `libs/`; `libs/` uses nothing; nothing uses `archive/`. A tool never reaches sideways into another tool's folder except through `libs/`.
- If a tool's needs change, it moves folders. A general Linux script that needs apt packages goes in `apt/`; one that needs COSMIC goes in `cosmic/`.

## What is in it
| Tool | Folder | What it is |
|---|---|---|
| [`notification-center`](cosmic/notification-center/README.md) | `cosmic/` | Replaces COSMIC's notification service: toasts, rules, sounds, history, a shade, a panel applet. |
| [`applet-timer`](cosmic/applet-timer/README.md) | `cosmic/` | Panel applet: one countdown timer. |
| [`applet-claude-usage`](cosmic/applet-claude-usage/README.md) | `cosmic/` | Panel applet: Claude plan usage as two bars with a pace marker. |
| [`claude-usage-core`](libs/claude-usage-core/README.md) | `libs/` | Rust library behind the usage applet and `vitals`: usage types, pace math, status-line feed. |
| [`vitals`](apt/vitals/README.md) | `apt/` | btop-style terminal dashboard (CPU, memory, disk, network, Claude usage) with a web mode. |
| [`local-ai`](apt/local-ai/README.md) | `apt/` | Offline AI kit: local Qwen model, OpenCode, VoiceStudio, OpenSEO, Parakeet push-to-dictate. |
| [`desktop-dashboard`](archive/desktop-dashboard/README.md) | `archive/` | Retired: a live HTML page rendered as the COSMIC desktop background. |

Each tool's README has an **Outside this repo** section. Read it before installing: these tools write to `~/.local`, `~/.config` and systemd user units, and some replace or shadow a running system service. All of it is reversible with the tool's own `scripts/uninstall.sh`.

Upstream projects (llama.cpp, OpenCode, libcosmic and others) are **prerequisites you get from their own repos**, not copies: see [`PREREQUISITES.md`](PREREQUISITES.md).

## This repo is public: never commit secrets
Everything here is published, and git history is permanent. A secret pushed once must be treated as leaked, even if deleted in the next commit.
- **Never commit:** API keys, tokens, passwords, bearer-token files, TLS keys (`*.key`, `ca.key`), `.env*` files, and personal config such as `~/.config/*` copies. Tools in this repo read secrets from files in your home folder (for example the notification center's `actions.token`, or `vitals`' web `key`); those files stay out of the repo.
- **Config files in the repo are examples** with placeholders only (`*.example`, `examples/`). Put real values in `~/.config` or an untracked file.
- **Before every push**, scan what you are about to publish, for example `git diff --cached` plus `git grep -n -i -E 'key|token|secret|password'`.
- **If a secret does get committed:** revoke or rotate it first, then remove it from history. Deleting the file in a later commit does not help.

## Licence: MIT
This repo is released under the [MIT licence](LICENSE), © 2026 Corbin Meier.
- **You may** use, copy, modify, merge, publish, distribute, sublicense and sell it, including in commercial work.
- **You must** keep the copyright notice and the licence text in every copy or substantial portion. That notice is the credit.
- **No warranty.** It is provided "as is"; the author is not liable for damage. Several tools replace or shadow system services, so read each tool's **Outside this repo** section first.
- **It covers only this repo's own code.** The upstream projects in [`PREREQUISITES.md`](PREREQUISITES.md) keep their own licences (VoiceStudio is AGPL-3.0, for example), and `archive/desktop-dashboard` includes a vendored Tailwind build under its own MIT licence. Model weights have their own terms.
- Source files carry `SPDX-License-Identifier: MIT` headers.

## Honest caveats
- **Built for one machine.** Paths, the COSMIC 1.11.0 libcosmic pin, the 150% display scale and the `input` group are all this laptop. Expect to adjust.
- **`CLAUDE.md` files are agent instructions**, not user docs. They describe how I work with Claude Code on each project.
- **Large downloads are not here.** `apt/local-ai/bootstrap.sh` fetches binaries and models; Parakeet's models have no recorded source yet.
