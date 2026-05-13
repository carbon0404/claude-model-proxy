#!/usr/bin/env python3
"""Integration test: multi-provider routing + Anthropic↔OpenAI format conversion."""
import http.server, json, os, sys, time, threading
from socketserver import ThreadingMixIn

SCRIPT = os.path.dirname(os.path.abspath(__file__))

# Clean
for p in [5671, 5672, 5679]:
    os.system(f"lsof -ti tcp:{p} 2>/dev/null | xargs kill -9 2>/dev/null")
time.sleep(0.4)

class ThreadedServer(ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

def http_req(host, port, method, path, body=None, headers=None):
    import http.client
    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, dict(resp.headers), resp.read()
    finally:
        conn.close()

# ── Mock providers ──────────────────────────────────────────────────────
def make_mock(label):
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def do_GET(s):
            s._ok({"provider": label, "ok": True})
        def do_POST(s):
            l = int(s.headers.get("Content-Length", 0))
            raw = s.rfile.read(l)
            body = json.loads(raw) if raw else {}
            # Record what was received (for verification)
            received_model = body.get("model", "?")
            messages = body.get("messages", [])
            has_system = any(m.get("role") == "system" for m in messages)
            # Return OpenAI-format response
            resp = {
                "id": f"chatcmpl-{label.lower()}",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant",
                              "content": f"Response from {label}"}, "finish_reason": "stop"}],
                "model": received_model,
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            }
            b = json.dumps(resp).encode()
            s.send_response(200)
            s.send_header("Content-Type", "application/json")
            s.send_header("Content-Length", str(len(b)))
            s.send_header("Connection", "close")
            s.end_headers()
            s.wfile.write(b)
        def _ok(s, d):
            b = json.dumps(d).encode()
            s.send_response(200)
            s.send_header("Content-Type", "application/json")
            s.send_header("Content-Length", str(len(b)))
            s.send_header("Connection", "close")
            s.end_headers()
            s.wfile.write(b)
        def log_message(s, *a): pass
    return H

# ── Start mocks ─────────────────────────────────────────────────────────
print("[1/3] Starting mocks...")
ds = ThreadedServer(("127.0.0.1", 5671), make_mock("DeepSeek"))
ms = ThreadedServer(("127.0.0.1", 5672), make_mock("Moonshot"))
threading.Thread(target=ds.serve_forever, daemon=True).start()
threading.Thread(target=ms.serve_forever, daemon=True).start()
time.sleep(0.2)

status, _, _ = http_req("127.0.0.1", 5671, "GET", "/ping")
assert status == 200
status, _, _ = http_req("127.0.0.1", 5672, "GET", "/ping")
assert status == 200
print("  Mocks: OK")

# ── Provider config ─────────────────────────────────────────────────────
cfg_path = os.path.join(SCRIPT, ".test_providers.json")
with open(cfg_path, "w") as f:
    json.dump({"providers": [
        {"target_url": "http://127.0.0.1:5671/anthropic", "api_key": "sk-ds",
         "models": [{"name": "deepseek-v4-pro", "to_1m": "auto"},
                    {"name": "deepseek-v4-flash", "to_1m": "auto"}]},
        {"target_url": "http://127.0.0.1:5672/anthropic", "api_key": "sk-ms",
         "models": [{"name": "kimi-k2.6", "to_1m": "auto"}]},
    ]}, f)

# ── Start proxy ────────────────────────────────────────────────────────
print("[2/3] Starting proxy...")
sys.path.insert(0, SCRIPT)
from proxy import build_routing, ModelProxyHandler
routing = build_routing(cfg_path, os.path.join(SCRIPT, "model_map.json"))
ModelProxyHandler.routing = routing
px = ThreadedServer(("127.0.0.1", 5679), ModelProxyHandler)
threading.Thread(target=px.serve_forever, daemon=True).start()
time.sleep(0.2)
print("  Routing:")
for c, r in routing.items():
    print(f"    {c} → {r['target_model']}")

# ── Tests ───────────────────────────────────────────────────────────────
print("[3/3] Running tests...\n")
passed = 0
failed = 0

# Test 1: Anthropic format → DeepSeek (opus → deepseek-v4-pro)
print("--- Test 1: Anthropic /v1/messages (Opus → DeepSeek) ---")
body = json.dumps({
    "model": "claude-opus-4-6",
    "max_tokens": 100,
    "messages": [{"role": "user", "content": "hi"}],
    "system": "be helpful",
}).encode()
status, headers, raw = http_req("127.0.0.1", 5679, "POST", "/v1/messages",
    body=body, headers={"Content-Type": "application/json", "Connection": "close"})
resp = json.loads(raw)
checks = [
    ("status 200", status == 200),
    ("Anthropic id", resp.get("id", "").startswith("msg_")),
    ("type=message", resp.get("type") == "message"),
    ("role=assistant", resp.get("role") == "assistant"),
    ("model restored", resp.get("model") == "claude-opus-4-6"),
    ("content[text]", resp.get("content", [{}])[0].get("text") == "Response from DeepSeek"),
    ("stop_reason", resp.get("stop_reason") == "end_turn"),
]
for label, ok in checks:
    if ok: passed += 1
    else: failed += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")

# Test 2: Anthropic format → Moonshot (sonnet → kimi)
print("\n--- Test 2: Anthropic /v1/messages (Sonnet → Moonshot) ---")
body = json.dumps({
    "model": "claude-sonnet-4-6",
    "max_tokens": 50,
    "messages": [{"role": "user", "content": "test"}],
}).encode()
status, headers, raw = http_req("127.0.0.1", 5679, "POST", "/v1/messages",
    body=body, headers={"Content-Type": "application/json", "Connection": "close"})
resp = json.loads(raw)
checks2 = [
    ("status 200", status == 200),
    ("Moonshot response", resp.get("content", [{}])[0].get("text") == "Response from Moonshot"),
    ("model restored", resp.get("model") == "claude-sonnet-4-6"),
]
for label, ok in checks2:
    if ok: passed += 1
    else: failed += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")

# Test 3: /v1/models
print("\n--- Test 3: /v1/models ---")
status, _, raw = http_req("127.0.0.1", 5679, "GET", "/v1/models")
data = json.loads(raw)
ids = [m["id"] for m in data.get("data", [])]
exp = ["claude-opus-4-6", "claude-haiku-4-5-20251001", "claude-sonnet-4-6"]
if set(ids) == set(exp):
    passed += 1; print(f"  [PASS] {ids}")
else:
    failed += 1; print(f"  [FAIL] {ids} ≠ {exp}")

# Summary
print(f"\n{'='*50}")
total = passed + failed
print(f"  {passed}/{total} passed")
if failed == 0:
    print("  ALL TESTS PASSED")
else:
    print(f"  {failed} FAILED")

px.shutdown(); ds.shutdown(); ms.shutdown()
os.unlink(cfg_path)
sys.exit(0 if failed == 0 else 1)
