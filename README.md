# llama-webui

Standalone server for [llama.cpp's Web UI](https://github.com/ggml-org/llama.cpp/tree/master/tools/server/webui) — a modern Svelte 5 chat interface that works with any OpenAI-compatible backend.

No build step, no Node.js, no npm. Just Python 3.10+ from stdlib.

![screenshot placeholder](https://img.shields.io/badge/status-working-green)

## Quick Start

```bash
# Point at any OpenAI-compatible LLM server
python server.py --backend http://localhost:8080

# Open http://localhost:3000 in your browser
```

The UI auto-connects to the backend. If your backend supports `/v1/chat/completions` and `/v1/models`, it works.

## Usage

```
python server.py [OPTIONS]
```

| Option | Default | Env Var | Description |
|---|---|---|---|
| `--host` | `127.0.0.1` | `LLAMA_WEBUI_HOST` | Bind address |
| `--port` | `3000` | `LLAMA_WEBUI_PORT` | Listen port |
| `--backend` | `http://localhost:8080` | `LLAMA_WEBUI_BACKEND` | Backend API URL |
| `--vision` | off | `LLAMA_WEBUI_VISION=1` | Enable vision (image upload) |
| `--audio` | off | `LLAMA_WEBUI_AUDIO=1` | Enable audio (voice input) |
| `--ctx-size` | 8192 | `LLAMA_WEBUI_CTX_SIZE` | Default context window |
| `--verbose` / `-v` | off | `LLAMA_WEBUI_VERBOSE=1` | Debug logging |

### Examples

```bash
# With mlx-lm on default port
python server.py --backend http://localhost:8080

# With vLLM
python server.py --backend http://localhost:8000 --port 4000

# With vision support (for LLaVA, etc.)
python server.py --backend http://localhost:8080 --vision

# Public-facing (behind auth proxy)
python server.py --host 0.0.0.0 --port 3000
```

## How It Works

```
Browser ──> llama-webui (port 3000) ──proxy /v1/*──> Backend (port 8080)
              │
              └── serves static UI files from public/
              └── stubs /props and /slots endpoints
```

- **Static files**: The pre-built Svelte UI (`index.html`, `bundle.js`, `bundle.css`)
- **API proxy**: All `/v1/*` requests are forwarded to the backend (mlx-lm, llama.cpp, etc.)
- **Stubs**: `/props` and `/slots` endpoints return minimal responses the UI expects
- **Streaming**: SSE chunks are streamed through without buffering

## Features That Just Work

- Chat with streaming responses
- Reasoning/thinking content display
- Conversation branching and history (stored in browser IndexedDB)
- Markdown with syntax highlighting
- KaTeX math rendering
- Dark/light theme
- Model selection from backend's `/v1/models`

## Requirements

- Python 3.10+
- An OpenAI-compatible LLM server (mlx-lm, llama.cpp, vLLM, Ollama, etc.)

Zero pip dependencies. Python stdlib only.

## Files

```
llama-webui/
├── server.py        # Python HTTP server + reverse proxy
├── public/
│   ├── index.html   # UI entry point
│   ├── bundle.js    # Svelte 5 app bundle (6.2MB)
│   ├── bundle.css   # TailwindCSS styles (489KB)
│   └── loading.html  # Loading placeholder
└── README.md
```

## Updating the UI Bundle

To update to a newer llama.cpp Web UI build:

```bash
# Build from source (requires Node.js + npm)
cd /path/to/llama.cpp/tools/server/webui
npm install
npm run build

# Or copy pre-built from llama.cpp repo
cp /path/to/llama.cpp/tools/server/public/* ~/llama-webui/public/
```

## License

The UI bundle is from [llama.cpp](https://github.com/ggml-org/llama.cpp) (MIT).
The server wrapper is MIT.
