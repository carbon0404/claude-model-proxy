#!/usr/bin/env python3
"""
Claude Desktop Model Proxy — Multi-Provider Router
===================================================
Sits between Claude Desktop and multiple inference providers, mapping standard
Claude model names to different backend models at different base URLs.

Routing example:
    claude-opus-4-6   → deepseek-v4-pro   @ api.deepseek.com/anthropic
    claude-haiku-*     → deepseek-v4-flash  @ api.deepseek.com/anthropic
    claude-sonnet-4-6  → kimi-k2.6          @ api.moonshot.cn/anthropic

Usage:
    python proxy.py                    # default port 5679
    python proxy.py --port 5680        # custom port
"""

import http.server
import json
import argparse
import os
import sys
import socket
import re
import uuid
import time
import traceback
import ssl
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime, timezone

# ── Logging ──────────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def log_req(method, path, body=None):
    preview = ""
    if body:
        try:
            if len(body) > 200:
                preview = body[:200].decode("utf-8", errors="replace") + "..."
            else:
                preview = body.decode("utf-8", errors="replace")
        except Exception:
            preview = f"<{len(body)} bytes>"
    log(f"{method} {path} body={preview}")

def log_resp(status, body=None):
    preview = ""
    if body:
        try:
            if len(body) > 150:
                preview = body[:150].decode("utf-8", errors="replace") + "..."
            else:
                preview = body.decode("utf-8", errors="replace")
        except Exception:
            preview = f"<{len(body)} bytes>"
    log(f"  → {status} {preview}")

def log_err(e):
    log(f"  ✗ ERROR: {type(e).__name__}: {e}")
    log(f"  {traceback.format_exc().strip().split(chr(10))[-1]}")

# ── Paths ────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
PROXY_MAP_FILE = SCRIPT_DIR / "model_map.json"
PROVIDER_CONFIG_FILE = Path.home() / ".claude-model-proxy" / "config.json"
DEFAULT_PORT = 5679

# ── Build routing table ──────────────────────────────────────────────────────

def build_routing(provider_config_path, mapping_path):
    """Combine provider config (URLs, keys, models) with proxy
    mapping (Claude name → provider model name) into a routing table."""
    if os.path.exists(mapping_path):
        with open(mapping_path, "r") as f:
            claude_to_provider_model = json.load(f)
    else:
        print(f"!! Mapping file not found: {mapping_path}")
        sys.exit(1)

    if os.path.exists(provider_config_path):
        with open(provider_config_path, "r") as f:
            provider_cfg = json.load(f)
    else:
        print(f"!! Provider config not found: {provider_config_path}")
        sys.exit(1)

    provider_index = {}
    for p in provider_cfg.get("providers", []):
        url = p["target_url"]
        key = p["api_key"]
        for m in p.get("models", []):
            provider_index[m["name"]] = {"target_url": url, "api_key": key}

    routing = {}
    for claude_name, provider_model_name in claude_to_provider_model.items():
        if provider_model_name not in provider_index:
            print(f"!! Provider model '{provider_model_name}' not found, skipping")
            continue
        entry = provider_index[provider_model_name]
        routing[claude_name] = {
            "target_url": entry["target_url"].rstrip("/"),
            "api_key": entry["api_key"],
            "target_model": provider_model_name,
        }

    return routing


# ── Low-level HTTP forward (no urllib connection reuse) ──────────────────────

def _raw_forward(method, target_url, path, headers, body, api_key, timeout=300):
    """Forward a request using raw socket + minimal HTTP, no connection reuse."""
    parsed = urlparse(target_url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    is_https = parsed.scheme == "https"

    # Build HTTP request bytes
    req_lines = [f"{method} {path} HTTP/1.0"]
    # Build headers dict for forwarding
    fwd_headers = {}
    for key, val in headers.items():
        low = key.lower()
        if low in ("host", "authorization", "connection", "content-length",
                    "accept-encoding"):  # strip to prevent gzip response
            continue
        fwd_headers[key] = val
    fwd_headers["Host"] = host
    fwd_headers["Authorization"] = f"Bearer {api_key}"
    fwd_headers["Connection"] = "close"
    fwd_headers["Accept"] = "*/*"
    fwd_headers["Accept-Encoding"] = "identity"  # no compression

    if body:
        fwd_headers["Content-Length"] = str(len(body))

    for k, v in fwd_headers.items():
        req_lines.append(f"{k}: {v}")
    req_lines.append("")
    req_lines.append("")

    req_data = "\r\n".join(req_lines).encode("utf-8")
    if body:
        req_data += body

    # Connect
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)

    try:
        if is_https:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)

        sock.connect((host, port))
        sock.sendall(req_data)

        # Read response
        raw = b""
        while True:
            try:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                raw += chunk
                # Try to parse headers to know when body is complete
                if b"\r\n\r\n" in raw:
                    header_end = raw.index(b"\r\n\r\n")
                    hdr_section = raw[:header_end].decode("utf-8", errors="replace")
                    # Parse status line
                    lines = hdr_section.split("\r\n")
                    status_line = lines[0]
                    status_code = int(status_line.split(" ")[1])

                    # Parse Content-Length
                    clen = None
                    for line in lines[1:]:
                        if line.lower().startswith("content-length:"):
                            clen = int(line.split(":", 1)[1].strip())
                            break

                    if clen is not None:
                        body_start = header_end + 4
                        needed = body_start + clen
                        while len(raw) < needed:
                            chunk = sock.recv(65536)
                            if not chunk:
                                break
                            raw += chunk
                        break  # Full response received
                    else:
                        # No Content-Length, read until connection close
                        # (Connection: close should close the socket)
                        pass
            except socket.timeout:
                break

    finally:
        sock.close()

    # Parse response
    if b"\r\n\r\n" not in raw:
        return 502, {"Content-Type": "application/json"}, json.dumps({"error": "no response"}).encode()

    header_end = raw.index(b"\r\n\r\n")
    hdr_section = raw[:header_end].decode("utf-8", errors="replace")
    body_bytes = raw[header_end + 4:]

    lines = hdr_section.split("\r\n")
    status_code = int(lines[0].split(" ")[1])

    resp_headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            resp_headers[k.strip()] = v.strip()

    return status_code, resp_headers, body_bytes


# ── Streaming forward ────────────────────────────────────────────────────────

def _raw_stream_forward(method, target_url, path, headers, body, api_key, timeout=300):
    """Forward a streaming request, return the open socket for chunked reading."""
    parsed = urlparse(target_url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    is_https = parsed.scheme == "https"

    req_lines = [f"{method} {path} HTTP/1.0"]
    fwd_headers = {}
    for key, val in headers.items():
        low = key.lower()
        if low in ("host", "authorization", "connection", "content-length",
                    "accept-encoding"):
            continue
        fwd_headers[key] = val
    fwd_headers["Host"] = host
    fwd_headers["Authorization"] = f"Bearer {api_key}"
    fwd_headers["Connection"] = "close"
    fwd_headers["Accept"] = "*/*"
    fwd_headers["Accept-Encoding"] = "identity"
    if body:
        fwd_headers["Content-Length"] = str(len(body))

    for k, v in fwd_headers.items():
        req_lines.append(f"{k}: {v}")
    req_lines.append("")
    req_lines.append("")

    req_data = "\r\n".join(req_lines).encode("utf-8")
    if body:
        req_data += body

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        if is_https:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)
        sock.connect((host, port))
        sock.sendall(req_data)
        return sock
    except Exception:
        sock.close()
        raise


# ── Anthropic ↔ OpenAI format conversion ──────────────────────────────────

def anthropic_to_openai(request_body):
    """Convert Anthropic Messages API request to OpenAI Chat Completions format."""
    messages = []

    # Anthropic has a top-level "system" field → OpenAI uses role="system" message
    system = request_body.get("system")
    if system:
        if isinstance(system, str):
            messages.append({"role": "system", "content": system})
        elif isinstance(system, list):
            # system can be a list of content blocks
            text_parts = []
            for block in system:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            if text_parts:
                messages.append({"role": "system", "content": "\n".join(text_parts)})

    # Copy conversation messages
    for msg in request_body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # Anthropic content can be a string or a list of content blocks
        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif isinstance(block, dict) and block.get("type") == "image":
                    # Pass through image blocks unchanged (OpenAI also supports them)
                    pass
            if text_parts:
                content = "\n".join(text_parts)

        messages.append({"role": role, "content": content})

    openai_req = {"messages": messages}

    # Copy common fields
    for field in ("max_tokens", "temperature", "top_p", "stream",
                  "stop", "frequency_penalty", "presence_penalty"):
        if field in request_body:
            openai_req[field] = request_body[field]

    # model is set by the caller (already rewritten)
    openai_req["model"] = request_body.get("model", "")

    return openai_req


def openai_to_anthropic(openai_resp, original_anthropic_model):
    """Convert OpenAI Chat Completions response to Anthropic Messages format."""
    choice = openai_resp.get("choices", [{}])[0]
    message = choice.get("message", {})
    content_text = message.get("content", "")
    finish_reason = choice.get("finish_reason", "stop")

    # Map finish_reason
    stop_reason_map = {
        "stop": "end_turn",
        "length": "max_tokens",
        "content_filter": "stop_sequence",
    }
    stop_reason = stop_reason_map.get(finish_reason, "end_turn")

    anthropic_resp = {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": content_text}],
        "model": original_anthropic_model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": openai_resp.get("usage", {}).get("prompt_tokens", 0),
            "output_tokens": openai_resp.get("usage", {}).get("completion_tokens", 0),
        },
    }
    return anthropic_resp


def openai_stream_to_anthropic_sse(line, model_name, message_id):
    """Convert an OpenAI SSE chunk line to an Anthropic SSE chunk line.
    Returns a list of SSE lines (may be empty, 1, or multiple events).
    """
    line = line.strip()
    if not line:
        return []
    if not line.startswith("data: "):
        return [line]

    data_str = line[6:]
    if data_str == "[DONE]":
        return ["data: [DONE]"]

    try:
        chunk = json.loads(data_str)
    except json.JSONDecodeError:
        return [line]

    choice = chunk.get("choices", [{}])[0]
    delta = choice.get("delta", {})
    delta_content = delta.get("content", "")
    finish_reason = choice.get("finish_reason")

    results = []

    # First chunk: message_start + content_block_start
    if delta.get("role") == "assistant" and not delta_content:
        results.append(f"event: message_start")
        results.append(f'data: {{"type":"message_start","message":{{"id":"{message_id}","type":"message","role":"assistant","model":"{model_name}","content":[],"usage":{{"input_tokens":0,"output_tokens":0}}}}}}')
        results.append(f"event: content_block_start")
        results.append(f'data: {{"type":"content_block_start","index":0,"content_block":{{"type":"text","text":""}}}}')
        results.append(f"event: ping")
        results.append(f'data: {{"type":"ping"}}')
        return results

    # Content delta
    if delta_content:
        results.append(f"event: content_block_delta")
        results.append(f'data: {{"type":"content_block_delta","index":0,"delta":{{"type":"text_delta","text":{json.dumps(delta_content)}}}}}')
        return results

    # Final chunk: content_block_stop + message_delta + message_stop
    if finish_reason:
        stop_map = {"stop": "end_turn", "length": "max_tokens", "content_filter": "stop_sequence"}
        stop_reason = stop_map.get(finish_reason, "end_turn")
        results.append(f"event: content_block_stop")
        results.append(f'data: {{"type":"content_block_stop","index":0}}')
        results.append(f"event: message_delta")
        results.append(f'data: {{"type":"message_delta","delta":{{"stop_reason":"{stop_reason}","stop_sequence":null}},"usage":{{"output_tokens":0}}}}')
        results.append(f"event: message_stop")
        results.append(f'data: {{"type":"message_stop"}}')
        return results

    # Usage info chunk
    if "usage" in chunk and not delta_content and not finish_reason:
        usage = chunk["usage"]
        results.append(f"event: message_delta")
        results.append(f'data: {{"type":"message_delta","delta":{{"stop_reason":"end_turn","stop_sequence":null}},"usage":{{"output_tokens":{usage.get("completion_tokens", 0)}}}}}')
        results.append(f"event: message_stop")
        results.append(f'data: {{"type":"message_stop"}}')
        return results

    return results


# ── Proxy handler ───────────────────────────────────────────────────────────

class ModelProxyHandler(http.server.BaseHTTPRequestHandler):
    routing = {}

    def log_message(self, fmt, *args):
        msg = args[0] if args else fmt
        log(f"{self.command} {self.path} → {msg}")

    # ── Route lookup ────────────────────────────────────────────────────

    def _lookup_route(self, model_name):
        route = self.routing.get(model_name)
        if route:
            return route["target_url"], route["api_key"], route["target_model"]
        return None, None, None

    def _rewrite_and_route(self, body_bytes):
        try:
            data = json.loads(body_bytes)
        except json.JSONDecodeError:
            return body_bytes, None, None

        model = data.get("model", "")
        url, key, target_model = self._lookup_route(model)
        if url:
            log(f"  ↳ model: {model} → {target_model}  url: {url}")
            data["model"] = target_model
            return json.dumps(data).encode("utf-8"), url, key
        else:
            log(f"  ↳ model: {model} → NO ROUTE (502)")
            return None, None, None

    # ── Request proxy ───────────────────────────────────────────────────

    def _proxy_to_provider(self, rewrite_fn=None):
        t0 = time.time()
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else None
        log_req(self.command, self.path, body)
        target_url = None
        api_key = None

        if body and rewrite_fn:
            body, target_url, api_key = rewrite_fn(body)

        if body is None and rewrite_fn:
            log("  → 502 (no route)")
            self.send_error(502, "Model not configured in proxy routing table")
            return

        if target_url is None and body:
            try:
                data = json.loads(body)
                url, key, _ = self._lookup_route(data.get("model", ""))
                target_url, api_key = url, key
            except Exception:
                pass

        fwd_t0 = time.time()
        status, resp_headers, resp_body = _raw_forward(
            self.command, target_url or "", self.path,
            self.headers, body, api_key or ""
        )
        fwd_ms = int((time.time() - fwd_t0) * 1000)
        log(f"  ← forward {status} in {fwd_ms}ms")

        # Check if streaming
        ct = resp_headers.get("Content-Type", resp_headers.get("content-type", ""))
        te = resp_headers.get("Transfer-Encoding", resp_headers.get("transfer-encoding", ""))
        is_stream = ct.startswith("text/event-stream") or te.lower() == "chunked"

        if is_stream:
            self._stream_to_client(target_url, self.path, body, api_key, status, resp_headers)
            return

        self.send_response(status)
        for k, v in resp_headers.items():
            if k.lower() in ("transfer-encoding", "content-encoding"):
                continue
            self.send_header(k, v)
        self.end_headers()
        if resp_body:
            self.wfile.write(resp_body)

    def _stream_to_client(self, target_url, path, body, api_key, status, resp_headers):
        try:
            sock = _raw_stream_forward(
                self.command, target_url, path,
                self.headers, body, api_key
            )
        except Exception as e:
            self.send_error(502, str(e))
            return

        # Read response headers
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = sock.recv(4096)
            if not chunk:
                break
            raw += chunk

        header_end = raw.index(b"\r\n\r\n")
        hdr_section = raw[:header_end].decode("utf-8", errors="replace")
        remainder = raw[header_end + 4:]

        lines = hdr_section.split("\r\n")
        resp_status = int(lines[0].split(" ")[1])
        out_headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                out_headers[k.strip()] = v.strip()

        self.send_response(resp_status)
        for k, v in out_headers.items():
            if k.lower() in ("transfer-encoding", "content-encoding"):
                continue
            self.send_header(k, v)
        self.end_headers()

        if remainder:
            self.wfile.write(remainder)
            self.wfile.flush()

        while True:
            try:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
            except Exception:
                break
        sock.close()

    # ── HTTP dispatch ───────────────────────────────────────────────────

    def _path(self):
        """Return the path part without query string."""
        return urlparse(self.path).path

    def do_GET(self):
        path = self._path()
        log(f">>> GET {self.path}")
        if path == "/v1/models":
            self._handle_models()
        elif path.startswith("/v1/models/"):
            self._handle_single_model(path[len("/v1/models/"):])
        else:
            log(f"  → 404 (unknown path)")
            self.send_error(404, f"Not found: {self.path}")

    def do_POST(self):
        path = self._path()
        log(f">>> POST {self.path}")
        if path == "/v1/messages":
            self._handle_messages()
        elif path == "/v1/chat/completions":
            self._proxy_to_provider(rewrite_fn=self._rewrite_and_route)
        else:
            log(f"  → 404 (unknown path: {path})")
            self.send_error(404, f"Not found: {self.path}")

    def _handle_messages(self):
        """Handle Anthropic Messages API: translate → OpenAI format, forward,
        translate response back to Anthropic format."""
        t0 = time.time()
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else None
        log_req("POST", self.path, body)

        if not body:
            log("  → 400 Empty body")
            self.send_error(400, "Empty body")
            return

        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            log(f"  → 400 Invalid JSON: {e}")
            self.send_error(400, "Invalid JSON")
            return

        model = data.get("model", "")
        is_stream = data.get("stream", False)
        max_tokens = data.get("max_tokens", "?")
        msg_count = len(data.get("messages", []))

        # Look up route
        url, key, target_model = self._lookup_route(model)
        if not url:
            log(f"  → 502 Model '{model}' not configured")
            self.send_error(502, f"Model '{model}' not configured")
            return

        log(f"  ↳ Anthropic→OpenAI  {model}→{target_model}  msgs={msg_count} max_tok={max_tokens} stream={is_stream}")
        data["model"] = target_model

        # Convert Anthropic → OpenAI
        openai_req = anthropic_to_openai(data)
        openai_body = json.dumps(openai_req).encode("utf-8")

        # Forward to provider
        fwd_t0 = time.time()
        status, resp_headers, resp_body = _raw_forward(
            "POST", url, "/v1/chat/completions",
            self.headers, openai_body, key
        )
        fwd_ms = int((time.time() - fwd_t0) * 1000)
        log(f"  ← provider {status} in {fwd_ms}ms")

        if is_stream:
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self._convert_and_stream(url, key, openai_body, model, data.get("id", f"msg_{uuid.uuid4().hex[:24]}"))
            log(f"  ✓ streaming done in {int((time.time()-t0)*1000)}ms")
            return

        # Convert response OpenAI → Anthropic
        try:
            openai_resp = json.loads(resp_body)
        except Exception:
            log_resp(status, resp_body)
            self.send_response(status)
            for k, v in resp_headers.items():
                if k.lower() in ("transfer-encoding", "content-encoding", "content-length"):
                    continue
                self.send_header(k, v)
            self.end_headers()
            if resp_body:
                self.wfile.write(resp_body)
            return

        anthropic_resp = openai_to_anthropic(openai_resp, model)
        anthropic_body = json.dumps(anthropic_resp).encode("utf-8")
        log_resp(status, anthropic_body)

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(anthropic_body)))
        self.send_header("anthropic-version", "2023-06-01")
        self.end_headers()
        self.wfile.write(anthropic_body)
        log(f"  ✓ done in {int((time.time()-t0)*1000)}ms")

    def _convert_and_stream(self, target_url, api_key, openai_body, original_model, message_id):
        """Stream response from provider, converting OpenAI SSE → Anthropic SSE."""
        sock = _raw_stream_forward("POST", target_url, "/v1/chat/completions",
                                    self.headers, openai_body, api_key)
        try:
            buf = b""
            while True:
                try:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                    # Process complete lines
                    while b"\n" in buf:
                        line_end = buf.index(b"\n")
                        line = buf[:line_end].decode("utf-8", errors="replace")
                        buf = buf[line_end + 1:]
                        converted = openai_stream_to_anthropic_sse(
                            line, original_model, message_id
                        )
                        for cl in converted:
                            self.wfile.write((cl + "\n").encode("utf-8"))
                            self.wfile.flush()
                except socket.timeout:
                    break
            # Flush remaining buffer
            if buf:
                line = buf.decode("utf-8", errors="replace").strip()
                if line:
                    for cl in openai_stream_to_anthropic_sse(line, original_model, message_id):
                        self.wfile.write((cl + "\n").encode("utf-8"))
                        self.wfile.flush()
        finally:
            sock.close()

    def do_HEAD(self):
        path = self._path()
        log(f">>> HEAD {self.path}")
        if path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def _handle_models(self):
        models_data = []
        for claude_name, route in self.routing.items():
            models_data.append({
                "id": claude_name,
                "display_name": route["target_model"],
            })
        resp = json.dumps({"data": models_data}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(resp)

    def _handle_single_model(self, model_id):
        """Return info for a single model by ID."""
        route = self.routing.get(model_id)
        if route:
            resp = json.dumps({
                "id": model_id,
                "display_name": route["target_model"],
                "type": "model",
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp)
        else:
            self.send_error(404, f"Model '{model_id}' not found")


# ── Entry point ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Claude Desktop Model Proxy — multi-provider model router"
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--mapping", type=str, default=str(PROXY_MAP_FILE))
    parser.add_argument("--providers", type=str, default=str(PROVIDER_CONFIG_FILE))
    args = parser.parse_args()

    routing = build_routing(args.providers, args.mapping)
    if not routing:
        print("ERROR: No routes configured.")
        sys.exit(1)

    ModelProxyHandler.routing = routing

    # Multi-threaded server
    class ThreadedServer(http.server.ThreadingHTTPServer):
        daemon_threads = True

    print()
    print("┌──────────────────────────────────────────────────────────────┐")
    print("│      Claude Desktop Model Proxy — Multi-Provider Router       │")
    print("├──────────────────────────────────────────────────────────────┤")
    print(f"│  Listening  : http://127.0.0.1:{args.port}                     ")
    print(f"│  Mapping    : {args.mapping}")
    print(f"│  Providers  : {args.providers}")
    print("├──────────────────────────────────────────────────────────────┤")
    print("│  Route table:                                                  │")
    for claude, route in routing.items():
        url_short = route["target_url"].replace("https://", "")
        if len(url_short) > 36:
            url_short = url_short[:33] + "..."
        print(f"│    {claude:36s} → {route['target_model']:20s} @ {url_short}")
    print("├──────────────────────────────────────────────────────────────┤")
    print("│  Claude Desktop config:                                        │")
    print(f'│    "inferenceGatewayBaseUrl": "http://127.0.0.1:{args.port}"  ')
    print("│    inferenceModels: use standard Claude model names above      │")
    print("└──────────────────────────────────────────────────────────────┘")
    print()
    print("Press Ctrl+C to stop.")
    print()

    server = ThreadedServer(("127.0.0.1", args.port), ModelProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
