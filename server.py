#!/usr/bin/env python3
"""
llama-webui: Standalone server for the llama.cpp Web UI frontend.

Serves the pre-built Svelte UI and proxies /v1/* requests to any
OpenAI-compatible backend (mlx-lm, llama.cpp, vLLM, Ollama, etc.).

Usage:
    python server.py --backend http://localhost:8080
    python server.py --backend http://localhost:8080 --port 3000 --host 0.0.0.0
"""

import argparse
import io
import json
import logging
import mimetypes
import os
import posixpath
import shutil
import socket
import sys
import time
import urllib.parse
from functools import partial
from http import HTTPStatus
from http.client import HTTPConnection, HTTPResponse
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Optional

logger = logging.getLogger("llama-webui")

HERE = Path(__file__).parent.resolve()
PUBLIC_DIR = HERE / "public"

DEFAULT_PROPS = {
    "default_generation_settings": {
        "n_ctx": 8192,
        "n_predict": -1,
        "seed": -1,
        "params": {
            "temperature": 0.7,
            "dynatemp_range": 0.0,
            "dynatemp_exponent": 1.0,
            "top_k": 40,
            "top_p": 0.95,
            "min_p": 0.05,
            "xtc_probability": 0.0,
            "xtc_threshold": 0.1,
            "typ_p": 1.0,
            "repeat_last_n": 64,
            "repeat_penalty": 1.0,
            "presence_penalty": 0.0,
            "frequency_penalty": 0.0,
            "dry_multiplier": 0.0,
            "dry_base": 1.75,
            "dry_allowed_length": 2,
            "dry_penalty_last_n": -1,
            "mirostat": 0,
            "mirostat_tau": 5.0,
            "mirostat_eta": 0.1,
            "stop": [],
            "max_tokens": -1,
            "n_keep": 0,
            "n_discard": -1,
            "ignore_eos": False,
            "stream": True,
            "logit_bias": [],
            "n_probs": 0,
            "min_keep": 0,
            "grammar": "",
            "chat_format": "chatml",
            "samplers": [
                "dry", "top_k", "top_p", "min_p", "xtc", "typ_p",
                "temperature", "repeat_penalty", "mirostat"
            ],
            "backend_sampling": True,
            "timings_per_token": False,
            "post_sampling_probs": False,
            "lora": [],
        },
    },
    "total_slots": 1,
    "model_path": "",
    "role": "model",
    "modalities": {
        "vision": False,
        "audio": False,
    },
    "chat_template": "",
    "bos_token": "<|begin_of_text|>",
    "eos_token": "<|eot_id|>",
    "build_info": "llama-webui (standalone)",
}


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Thread-per-request HTTP server."""
    allow_reuse_address = True
    daemon_threads = True


def _build_upstream_request(handler: BaseHTTPRequestHandler, backend_url: str) -> tuple:
    """Build an upstream request from the incoming handler's request.

    Returns (method, path, headers, body).
    """
    parsed = urllib.parse.urlparse(backend_url)
    path = handler.path

    body = None
    content_length = handler.headers.get("Content-Length")
    if content_length:
        try:
            body = handler.rfile.read(int(content_length))
        except Exception:
            body = None

    # Forward relevant headers, skip hop-by-hop
    skip_headers = {
        "Host", "Connection", "Keep-Alive", "Proxy-Authenticate",
        "Proxy-Authorization", "TE", "Trailer", "Transfer-Encoding",
        "Upgrade", "Content-Length",
    }
    headers = {}
    for key, value in handler.headers.items():
        if key not in skip_headers:
            headers[key] = value

    return handler.command, path, headers, body


def proxy_stream(handler: BaseHTTPRequestHandler, backend_url: str) -> None:
    """Proxy a request to the backend, streaming the response back."""
    backend_parsed = urllib.parse.urlparse(backend_url)
    is_secure = backend_parsed.scheme == "https"
    port = backend_parsed.port or (443 if is_secure else 80)
    host = backend_parsed.hostname or "localhost"

    method, path, headers, body = _build_upstream_request(handler, backend_url)

    try:
        conn = HTTPConnection(host, port, timeout=60)
        conn.connect()

        # Send the request
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", f"{host}:{port}" if port != (443 if is_secure else 80) else host)
        for key, value in headers.items():
            conn.putheader(key, value)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)

        response = conn.getresponse()

        # Read response headers and send to client
        handler.send_response(response.status)
        skip_response_headers = {
            "Transfer-Encoding", "Connection", "Keep-Alive",
        }
        for key, value in response.getheaders():
            if key not in skip_response_headers:
                handler.send_header(key, value)
        handler.end_headers()

        # Stream the response body
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            try:
                handler.wfile.write(chunk)
                handler.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break

        response.close()
        conn.close()

    except Exception as e:
        logger.error("Proxy error for %s %s: %s", method, path, e)
        try:
            handler.send_error(502, f"Bad Gateway: {e}")
        except Exception:
            pass


def read_static_file(path: str) -> tuple[Optional[bytes], Optional[str]]:
    """Read a static file from the public directory.

    Returns (content, mime_type) or (None, None) if not found.
    """
    # Normalize path, prevent directory traversal
    rel_path = posixpath.normpath(path.lstrip("/"))
    abs_path = (PUBLIC_DIR / rel_path).resolve()

    # Must be within PUBLIC_DIR
    if not str(abs_path).startswith(str(PUBLIC_DIR.resolve())):
        return None, None

    if not abs_path.is_file():
        return None, None

    mime_type, _ = mimetypes.guess_type(str(abs_path))
    if mime_type is None:
        mime_type = "application/octet-stream"

    return abs_path.read_bytes(), mime_type


def handle_static_or_404(handler: BaseHTTPRequestHandler, path: str) -> bool:
    """Try to serve a static file. Returns True if handled."""
    content, mime_type = read_static_file(path)
    if content is None:
        return False

    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", mime_type)
    handler.send_header("Content-Length", str(len(content)))
    handler.send_header("Cache-Control", "public, max-age=3600")
    handler.end_headers()
    handler.wfile.write(content)
    return True


class LlamaWebUIHandler(BaseHTTPRequestHandler):
    """HTTP request handler for llama-webui."""

    # Set by factory
    backend_url = "http://localhost:8080"
    props_overrides = {}
    start_time = time.time()

    # Silence default logging (we use our own)
    def log_message(self, format, *args):
        logger.info("%s - %s", self.client_address[0], format % args)

    # ---- CORS ----

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Max-Age", "86400")

    # ---- Routes ----

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path == "/health":
            self._json_response({"status": "ok", "uptime": time.time() - self.start_time})
        elif path == "/props":
            self._handle_props()
        elif path == "/slots":
            self._handle_slots()
        elif path.startswith("/v1/"):
            proxy_stream(self, self.backend_url)
        elif path == "/" or path == "":
            if not handle_static_or_404(self, "/index.html"):
                self.send_error(404, "Not Found")
        else:
            if not handle_static_or_404(self, path):
                self.send_error(404, "Not Found")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path

        if path.startswith("/v1/"):
            proxy_stream(self, self.backend_url)
        else:
            self.send_error(404, "Not Found")

    def do_PUT(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/v1/"):
            proxy_stream(self, self.backend_url)
        else:
            self.send_error(404, "Not Found")

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/v1/"):
            proxy_stream(self, self.backend_url)
        else:
            self.send_error(404, "Not Found")

    # ---- Endpoint handlers ----

    def _handle_props(self):
        props = dict(DEFAULT_PROPS)
        props.update(self.props_overrides)

        # Check query params
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "autoload" in query and query["autoload"][0].lower() == "false":
            pass  # props is same regardless, we just don't auto-load

        self._json_response(props)

    def _handle_slots(self):
        self._json_response([
            {
                "id": 0,
                "is_processing": False,
            }
        ])

    # ---- Helpers ----

    def _json_response(self, data, status=HTTPStatus.OK):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def create_handler(backend_url: str, **props_overrides):
    """Factory to create a handler class with configured backend_url."""
    cls = type(
        "ConfiguredHandler",
        (LlamaWebUIHandler,),
        {
            "backend_url": backend_url,
            "props_overrides": props_overrides,
        },
    )
    return cls


def main():
    parser = argparse.ArgumentParser(
        description="llama-webui: Standalone frontend for any OpenAI-compatible LLM backend"
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("LLAMA_WEBUI_HOST", "127.0.0.1"),
        help="Host to bind to (default: 127.0.0.1, env: LLAMA_WEBUI_HOST)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("LLAMA_WEBUI_PORT", "3000")),
        help="Port to listen on (default: 3000, env: LLAMA_WEBUI_PORT)",
    )
    parser.add_argument(
        "--backend",
        default=os.environ.get("LLAMA_WEBUI_BACKEND", "http://localhost:8080"),
        help="Backend API URL (default: http://localhost:8080, env: LLAMA_WEBUI_BACKEND)",
    )
    parser.add_argument(
        "--vision",
        action="store_true",
        default=os.environ.get("LLAMA_WEBUI_VISION", "").lower() in ("1", "true", "yes"),
        help="Enable vision modality support",
    )
    parser.add_argument(
        "--audio",
        action="store_true",
        default=os.environ.get("LLAMA_WEBUI_AUDIO", "").lower() in ("1", "true", "yes"),
        help="Enable audio modality support",
    )
    parser.add_argument(
        "--ctx-size",
        type=int,
        default=int(os.environ.get("LLAMA_WEBUI_CTX_SIZE", "8192")),
        help="Default context size (default: 8192, env: LLAMA_WEBUI_CTX_SIZE)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=os.environ.get("LLAMA_WEBUI_VERBOSE", "").lower() in ("1", "true", "yes"),
        help="Verbose logging",
    )

    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Build props overrides from CLI args
    props_overrides = {}
    modalities = {"vision": args.vision, "audio": args.audio}
    default_params = dict(DEFAULT_PROPS["default_generation_settings"]["params"])
    props_overrides["modalities"] = modalities
    props_overrides["default_generation_settings"] = dict(
        DEFAULT_PROPS["default_generation_settings"]
    )
    props_overrides["default_generation_settings"]["n_ctx"] = args.ctx_size

    handler_class = create_handler(
        backend_url=args.backend.rstrip("/"),
        **props_overrides,
    )

    server = ThreadingHTTPServer((args.host, args.port), handler_class)

    logger.info("llama-webui starting on http://%s:%d", args.host, args.port)
    logger.info("Proxying /v1/* to backend: %s", args.backend)
    logger.info("Modalities: vision=%s, audio=%s", args.vision, args.audio)
    logger.info("Context size: %d", args.ctx_size)
    logger.info("Open http://%s:%d in your browser", args.host, args.port)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
