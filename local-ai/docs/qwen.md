# Qwen3.8 27B (local LLM)

Qwen3.8-27B is Alibaba's Qwen model from August 2026: a 27-billion-parameter dense model. It reads text and images, handles up to 256K tokens of context, and is licensed Apache-2.0, so commercial use is fine. It runs here on **llama.cpp**, a single binary with no Python and no install.

- Model file: `models/qwen3.8-27b/Qwen3.8-27B-UD-IQ4_XS.gguf`. This is a 14.3 GB, 4-bit quant from `unsloth/Qwen3.8-27B-GGUF`, picked as the best quality that fits this laptop's 23 GB of RAM.
- Vision: `models/qwen3.8-27b/mmproj-F16.gguf` (0.9 GB, image input)
- Server: `bin/llama.cpp-vulkan/` (Radeon 860M through Vulkan) and `bin/llama.cpp-cpu/` (fallback). llama.cpp build b11998.

## Start

```bash
qwen-server
```

Wait for `server is listening on http://127.0.0.1:8080`. After that you have:
- **A chat UI** at http://127.0.0.1:8080, ChatGPT-style in the browser, with file and image upload.
- **An OpenAI-compatible API** at `http://127.0.0.1:8080/v1`, which works with OpenCode, VoiceStudio, scripts and any OpenAI SDK (any API key string works).

```bash
curl -s http://127.0.0.1:8080/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.8-27b","messages":[{"role":"user","content":"One-line haiku about RAM"}]}' | jq -r '.choices[0].message.content'
```

Stop it with Ctrl+C. While it runs it holds about 15 GB of RAM, so close it when you're done.

## Knobs

You set these as environment variables before the command, for example `CTX=32768 qwen-server`.

| Variable | Default | What it does |
|---|---|---|
| `CTX` | `16384` | Context window in tokens. More = longer chats and files, more RAM. OpenCode wants ≥16K |
| `THINK` | `1` | `THINK=0` turns reasoning off: much faster replies |
| `VISION` | `0` | `VISION=1` loads the image projector (+0.9 GB) |
| `BACKEND` | `cpu` | `vulkan` = GPU build (AMD/Intel/NVIDIA). It crashes on this laptop's 860M, so only use it on other machines |
| `NGL` | auto | Number of layers on the GPU. llama.cpp fits it automatically; set `NGL=0` to keep it all on the CPU |
| `PORT` / `HOST` | `8080` / `127.0.0.1` | `HOST=0.0.0.0` shares it on your LAN (no auth, so only on a trusted network) |
| `MODEL` | the IQ4_XS file | Point at any other `.gguf` |

Anything after the command is passed to llama-server, e.g. `qwen-server --threads 8`. Full list: `bin/llama.cpp-vulkan/llama-server --help`.

**Thinking**: Qwen3.8 reasons before answering by default. That gives better answers but is slower, often by minutes on this laptop. Start `THINK=0 qwen-server` to have it answer straight away. Each mode uses the sampling settings the Qwen model card recommends for it. In the web UI the reasoning shows up as a collapsible block.

## Speed on this laptop: be realistic

Measured on the Ryzen AI 7 350 laptop (CPU build, 2026-10-29): it loads in about 16 s, reads your prompt at about 24 tokens/s, and writes about 2.7 tokens/s, which is roughly two words a second. OpenCode's own system prompt is around 10K tokens, so the first reply in a new OpenCode session takes several minutes.

The Vulkan (iGPU) build fails to load on this laptop with `radv/amdgpu: Not enough memory for command submission`, even with only 4 layers on the GPU. So the default here is `BACKEND=cpu`. On a machine where Vulkan works, try `BACKEND=vulkan qwen-server`.

A dense 27B is the biggest model this laptop can hold, and it runs slowly. The memory bandwidth limits it, not the GPU. It's good for careful one-off questions and for plan-mode coding. It's too slow for long agent loops. Ways to go faster:

| Option | Size | Speed here | How |
|---|---|---|---|
| Qwen3.8 27B (this) | 14 GB | slow, best quality | `qwen-server` |
| `qwen3.5:9b` | ~7 GB | about 3-4× faster | `ollama pull qwen3.5:9b`, then `/models` in OpenCode → Ollama |
| `qwen3:30b` (MoE, 3B active) | ~19 GB | fast, but leaves almost no RAM for anything else | Ollama |
| A cloud model | 0 | fast | OpenCode `/connect` |

To keep a smaller GGUF in this folder instead of in Ollama's store, download it into `models/` and run `MODEL=models/<file>.gguf qwen-server`.

## Ollama

Ollama is installed on this machine as a system service, but it is version 0.17.5, which is too old for Qwen3.8 (support arrived around 0.40.7). Once you upgrade it (`curl -fsSL https://ollama.com/install.sh | sh`), `ollama run qwen3.8:27b` also works. Note that Ollama would download its own 18 GB copy into `/usr/share/ollama`, which is outside this folder. llama.cpp + `models/` is the portable route.

## Updating llama.cpp

New models sometimes need a newer llama.cpp. Download `llama-<build>-bin-ubuntu-vulkan-x64.tar.gz` (and `-ubuntu-x64` for the CPU build) from https://github.com/ggml-org/llama.cpp/releases, then extract each one over its `bin/llama.cpp-*/` folder with `tar xzf <file> -C bin/llama.cpp-vulkan --strip-components=1`.
