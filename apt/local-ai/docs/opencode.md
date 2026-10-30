# OpenCode

An open-source terminal coding agent: you describe a change, it reads your code, edits files and runs commands. Same idea as Claude Code, but it works with any model provider (Anthropic, OpenAI, OpenRouter, Ollama...).

- Source: `repos/opencode` (https://github.com/anomalyco/opencode, MIT). Full docs: `repos/opencode/packages/web/src/content/docs/*.mdx` or https://opencode.ai/docs
- Binary: `bin/opencode/opencode` (v1.19.5). Run it as **`oc`**. That wrapper loads `config/opencode/` (your commands, agents, MCP servers), so your setup travels with the folder.

> You also have an older OpenCode (1.2.18) at `~/.opencode/bin/opencode`, called as `opencode`. Use `oc` for this kit.

## Start

```bash
cd ~/some/project
oc
```

First time on a machine: type `/connect`, pick a provider (Anthropic, OpenAI, OpenRouter, OpenCode Zen...), paste a key, then `/models` to choose a model. Logins are stored per machine in `~/.local/share/opencode/`, so do `/connect` again after copying the folder.

## The 10 things to learn first

| Do this | How |
|---|---|
| Switch agent: **build** (edits files) ↔ **plan** (read-only, thinks first) | `Tab` |
| Point at a file | type `@` then a filename (fuzzy search) |
| Run a shell command, output goes to the model | start the message with `!` e.g. `!npm test` |
| Command palette (everything is in here) | `Ctrl+P` |
| Switch model | `/models` |
| Undo the last change (files too) / redo | `/undo` / `/redo` |
| New session / list old ones | `Ctrl+X N` / `Ctrl+X L` |
| Shrink a long conversation | `/compact` |
| Stop the model mid-answer | `Esc` |
| Write a long prompt in your editor | `Ctrl+X E` |

`Ctrl+X` is the **leader** key: press it, release, then the letter. Other useful ones: `Ctrl+X B` sidebar, `Ctrl+X T` themes, `Ctrl+X X` export session, `Ctrl+X S` status.

Start every new project with `/init`: it writes an `AGENTS.md` describing the repo, which the agent reads every session (like CLAUDE.md).

## Power-user moves

**Scripting / one-shots** (no TUI):
```bash
oc run "explain what src/index.ts does"
oc run -c "now add tests for it"              # continue the last session
oc run -m anthropic/claude-sonnet-5-5 "quick question"   # pick a model per run (`oc models` lists them)
oc run --format json "..." | jq                # machine-readable events
git diff | oc run "write a commit message for this"
```

**Keep a server warm** so runs start instantly (MCP servers stay booted):
```bash
oc serve --port 4096 &
oc run --attach http://localhost:4096 "..."
```
`oc web` opens the same thing in a browser.

**Custom slash commands**: markdown files in `config/opencode/commands/`. The file name is the command. `$ARGUMENTS` is what you type after it; `` !`cmd` `` injects shell output. One is included: `/review` reviews your uncommitted diff.

```markdown
---
description: Write tests for a file
agent: build
---
Write thorough unit tests for $ARGUMENTS. Run them and fix failures.
```

**Custom agents**: markdown files in `config/opencode/agents/` (e.g. `docs-writer.md`) with a system prompt in the body and `mode: primary` (Tab cycles to it) or `mode: subagent` (call with `@docs-writer`). Built-in subagent: `@general`.

**MCP servers** (give the agent new tools) go in `config/opencode/opencode.json`:
```json
"mcp": {
  "voicestudio": { "type": "remote", "url": "http://127.0.0.1:3900/mcp/" },
  "everything":  { "type": "local", "command": ["npx", "-y", "@modelcontextprotocol/server-everything"] }
}
```
The VoiceStudio one lets OpenCode speak or transcribe audio while VoiceStudio is running. `oc mcp list` shows what's connected.

**Per-project config**: an `opencode.json` in a repo root overrides the global one (different model, permissions, MCP for that project).

## Config files

| File | Holds |
|---|---|
| `config/opencode/opencode.json` | models/providers, MCP, permissions (portable, used by `oc`) |
| `config/opencode/commands/`, `agents/` | your slash commands and agents |
| `~/.config/opencode/` | the global config the old `opencode` uses; it is merged in as well |
| `~/.local/share/opencode/` | sessions and logins (stay on each machine) |


## Updating

Download the newest `opencode-linux-x64.tar.gz` from https://github.com/anomalyco/opencode/releases and extract it over `bin/opencode/`. Auto-update is off in `oc` on purpose so the copy you carry stays the version you tested.
