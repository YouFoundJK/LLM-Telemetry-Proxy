#!/usr/bin/env python3
"""
LLM Telemetry Proxy — passive instrumentation of real API calls.

Sits between Hermes and e-INFRA. Every request is forwarded transparently,
but timed (TTFB + total RTT), correlated with server load, and logged to SQLite.

Also tracks total API call counts (proxy_calls table) for cross-checking
against logged calls — so you can verify nothing was lost.

No probe text. No dummy requests. Only wraps the calls Hermes is already making.

The dashboard is a SEPARATE server — see ~/telemetry-dashboard/dashboard.sh
This proxy only proxies + logs. No dashboard endpoints here.

Usage:
    source ~/server/.venv/bin/activate
    nohup python3 proxy/llm_telemetry_proxy.py > proxy.log 2>&1 &
    # Then: hermes config set model.base_url http://localhost:9090/v1

    # Kill it via: pkill -f llm_telemetry_proxy.py
"""

from collections import deque
import asyncio
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from aiohttp import web

# ── Config ──────────────────────────────────────────────────────────────────
# UPSTREAM = "https://llm-dev.ai.e-infra.cz/v1"
# STATUS_API = "https://llm-dev.ai.e-infra.cz/status/api/v1/models"
UPSTREAM = "https://llm.ai.e-infra.cz/v1"
STATUS_API = "https://llm.ai.e-infra.cz/status/api/v1/models"
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 9090
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "llm_telemetry.db"

# Models to track server load for
WATCHED_MODELS = ["Deepseek-v4", "Glm-5.2", "Qwen3.5-int4", "Kimi-K2.7"]

# ── Rate Limiting ────────────────────────────────────────────────────────────
# e-INFRA enforces max 4 parallel requests per API key.
# Instead of letting requests hit 429s, we gate them here with a semaphore.
# Requests beyond 4 wait in an async queue until a slot frees up.
MAX_CONCURRENT = 4
_upstream_semaphore = asyncio.Semaphore(MAX_CONCURRENT)

# ── Token Budget Enforcement ─────────────────────────────────────────────────
# Hard cap: 240M tokens per day. When exceeded, proxy rejects with clear error.
DAILY_TOKEN_LIMIT = 480_000_000  # 240 million tokens

class RollingTokenBudget:
    """Rolling 24-hour token budget with hard enforcement."""
    
    def __init__(self, daily_limit: int):
        self.daily_limit = daily_limit
        self._usage = deque()  # (timestamp, token_count)
    
    def record_and_check(self, input_tokens: int, output_tokens: int) -> tuple[bool, dict]:
        """
        Record usage and check if within budget.
        Returns (allowed: bool, status: dict).
        Status includes: total_used, remaining, percentage_used, daily_limit.
        """
        now = time.time()
        total = input_tokens + output_tokens
        self._usage.append((now, total))
        
        # Purge entries older than 24 hours
        cutoff = now - 86400
        while self._usage and self._usage[0][0] < cutoff:
            self._usage.popleft()
        
        current_usage = sum(count for _, count in self._usage)
        remaining = max(0, self.daily_limit - current_usage)
        percentage_used = (current_usage / self.daily_limit) * 100
        
        status = {
            "total_used": current_usage,
            "remaining": remaining,
            "percentage_used": round(percentage_used, 2),
            "daily_limit": self.daily_limit,
        }
        
        if current_usage >= self.daily_limit:
            return False, status
        return True, status
    
    def get_status(self) -> dict:
        """Get current budget status without recording usage."""
        now = time.time()
        cutoff = now - 86400
        while self._usage and self._usage[0][0] < cutoff:
            self._usage.popleft()
        
        current_usage = sum(count for _, count in self._usage)
        remaining = max(0, self.daily_limit - current_usage)
        percentage_used = (current_usage / self.daily_limit) * 100
        
        return {
            "total_used": current_usage,
            "remaining": remaining,
            "percentage_used": round(percentage_used, 2),
            "daily_limit": self.daily_limit,
        }

_token_budget = RollingTokenBudget(DAILY_TOKEN_LIMIT)

# ── SQLite ──────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_calls (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp     TEXT    NOT NULL,
            model         TEXT,
            endpoint      TEXT,
            input_tokens  INTEGER,
            output_tokens INTEGER,
            ttfb_ms       REAL,
            total_ms      REAL,
            tokens_per_s  REAL,
            server_running REAL,
            server_tok_s  REAL,
            server_model  TEXT,
            status_code   INTEGER,
            error         TEXT,
            call_type     TEXT DEFAULT 'chat',
            calls_count   INTEGER DEFAULT 1
        )
    """)
    # Migrations for existing DBs
    try:
        conn.execute("ALTER TABLE api_calls ADD COLUMN call_type TEXT DEFAULT 'chat'")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE api_calls ADD COLUMN calls_count INTEGER DEFAULT 1")
    except sqlite3.OperationalError:
        pass

    # Call counter table — tracks EVERY request through the proxy,
    # even ones that fail before logging to api_calls
    conn.execute("""
        CREATE TABLE IF NOT EXISTS proxy_calls (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp     TEXT    NOT NULL,
            endpoint      TEXT,
            method        TEXT,
            call_type     TEXT,
            model         TEXT,
            status_code   INTEGER,
            error         TEXT,
            logged        INTEGER DEFAULT 0,
            ttfb_ms       REAL,
            total_ms      REAL,
            calls_count   INTEGER DEFAULT 1
        )
    """)
    try:
        conn.execute("ALTER TABLE proxy_calls ADD COLUMN calls_count INTEGER DEFAULT 1")
    except sqlite3.OperationalError:
        pass

    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON api_calls(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_model ON api_calls(model)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_type ON api_calls(call_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_proxy_ts ON proxy_calls(timestamp)")
    conn.commit()
    conn.close()


def log_call(model, endpoint, input_tokens, output_tokens,
             ttfb_ms, total_ms, tokens_per_s,
             server_running, server_tok_s, server_model,
             status_code, error, call_type='chat'):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        INSERT INTO api_calls
            (timestamp, model, endpoint, input_tokens, output_tokens,
             ttfb_ms, total_ms, tokens_per_s,
             server_running, server_tok_s, server_model,
             status_code, error, call_type, calls_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
    """, (
        datetime.now(timezone.utc).isoformat(),
        model, endpoint, input_tokens, output_tokens,
        ttfb_ms, total_ms, tokens_per_s,
        server_running, server_tok_s, server_model,
        status_code, error, call_type,
    ))
    conn.commit()
    conn.close()


def log_proxy_call(endpoint, method, call_type, model, status_code, error, logged, ttfb_ms, total_ms):
    """Log EVERY request through the proxy — even ones that fail before logging to api_calls."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        INSERT INTO proxy_calls
            (timestamp, endpoint, method, call_type, model, status_code, error, logged, ttfb_ms, total_ms, calls_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
    """, (
        datetime.now(timezone.utc).isoformat(),
        endpoint, method, call_type, model, status_code, error, logged, ttfb_ms, total_ms,
    ))
    conn.commit()
    conn.close()


def classify_endpoint(path):
    """Classify API call type from endpoint path."""
    if "/chat/completions" in path or "/completions" in path:
        return "chat"
    if "/embeddings" in path:
        return "embedding"
    if path.endswith("/models") and "/models/" not in path:
        return "model_list"
    if "/models/" in path:
        return "model_info"
    if "/props" in path:
        return "props"
    if "/rerank" in path:
        return "rerank"
    return "other"


# ── Server Load Cache ───────────────────────────────────────────────────────
_load_cache = {"data": None, "ts": 0}
_load_cache_lock = asyncio.Lock()


async def fetch_server_load(model_hint=None):
    """Fetch server load from status API. Returns (running, tok_s, model_name) or (None, None, None)."""
    now = time.time()
    if _load_cache["data"] and (now - _load_cache["ts"]) < 10:
        data = _load_cache["data"]
    else:
        async with _load_cache_lock:
            if _load_cache["data"] and (now - _load_cache["ts"]) < 10:
                data = _load_cache["data"]
            else:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(STATUS_API, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                            raw = await resp.json()
                    data = {}
                    for m in raw:
                        name = m.get("model_name") or m.get("container", "?")
                        latest = m.get("latest", {})
                        if isinstance(latest.get("num_requests_running"), dict):
                            running, tok_s = 0, 0.0
                        else:
                            running = latest.get("num_requests_running", 0)
                            tok_s = latest.get("generation_tokens_rate", 0.0)
                        data[name] = {
                            "status": m.get("status", "unknown"),
                            "running": running,
                            "tok_s": tok_s,
                            "kv_cache": latest.get("kv_cache_usage_perc", 0),
                            "waiting": latest.get("num_requests_waiting", 0),
                        }
                    _load_cache["data"] = data
                    _load_cache["ts"] = time.time()
                except Exception as e:
                    print(f"[telemetry] status API fetch failed: {e}", file=sys.stderr)
                    return None, None, None

    if model_hint and data:
        hint_lower = model_hint.lower()
        name_map = {
            "deepseek": "Deepseek-v4",
            "glm": "Glm-5.2",
            "qwen": "Qwen3.5-int4",
            "kimi": "Kimi-K3",
            "gpt-oss": "Gpt-oss-120b",
            "gemma": "Gemma4",
        }
        matched_name = None
        for key, api_name in name_map.items():
            if key in hint_lower:
                matched_name = api_name
                break
        if matched_name and matched_name in data:
            d = data[matched_name]
            return d["running"], d["tok_s"], matched_name
        for name in WATCHED_MODELS:
            if name in data:
                d = data[name]
                return d["running"], d["tok_s"], name

    return None, None, None


# ── Proxy Handler ───────────────────────────────────────────────────────────
async def handle_proxy(request: web.Request) -> web.StreamResponse:
    path = request.path
    method = request.method
    call_type = classify_endpoint(path)

    # Skip logging for non-inference calls
    if call_type in ("model_list", "model_info", "props", "other"):
        return await _simple_forward(request, path, method)

    body = await request.read()
    model = None
    input_tokens = None
    try:
        if body:
            payload = json.loads(body)
            model = payload.get("model")
            if payload.get("stream") and not payload.get("stream_options"):
                payload["stream_options"] = {"include_usage": True}
                body = json.dumps(payload).encode("utf-8")
    except (json.JSONDecodeError, KeyError):
        pass

    server_running, server_tok_s, server_model = await fetch_server_load(model)

    upstream_url = f"{UPSTREAM}{path.replace('/v1', '', 1)}" if path.startswith("/v1") else f"{UPSTREAM}{path}"

    headers = dict(request.headers)
    headers.pop("Host", None)
    headers.pop("host", None)
    if body and "Content-Length" in headers:
        headers["Content-Length"] = str(len(body))

    t_start = time.monotonic()
    ttfb_ms = None
    status_code = None
    error = None
    output_tokens = None
    tokens_per_s = None
    logged = False

    try:
        timeout = aiohttp.ClientTimeout(total=300)
        # Gate: never exceed MAX_CONCURRENT parallel upstream requests
        async with _upstream_semaphore:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(
                    method, upstream_url,
                    headers=headers,
                    data=body if body else None,
                    params=request.query,
                ) as upstream_resp:
                    status_code = upstream_resp.status
                    content_type = upstream_resp.headers.get("Content-Type", "")
                    is_stream = "text/event-stream" in content_type

                    if is_stream:
                        response = web.StreamResponse(
                            status=upstream_resp.status,
                            headers={
                                "Content-Type": content_type,
                                "Cache-Control": "no-cache",
                                "Connection": "keep-alive",
                            },
                        )
                        await response.prepare(request)

                        t_first_byte = None
                        collected_usage = None
                        content_chars = 0

                        async for chunk in upstream_resp.content:
                            if t_first_byte is None:
                                t_first_byte = time.monotonic()
                                ttfb_ms = (t_first_byte - t_start) * 1000
                            await response.write(chunk)
                            try:
                                text = chunk.decode("utf-8", errors="replace")
                                for line in text.split("\n"):
                                    if line.startswith("data: ") and line.strip() != "data: [DONE]":
                                        try:
                                            chunk_data = json.loads(line[6:])
                                            if chunk_data.get("usage"):
                                                collected_usage = chunk_data["usage"]
                                            for choice in chunk_data.get("choices", []):
                                                delta = choice.get("delta", {})
                                                if delta.get("content"):
                                                    content_chars += len(delta["content"])
                                                if delta.get("reasoning_content"):
                                                    content_chars += len(delta["reasoning_content"])
                                        except json.JSONDecodeError:
                                            pass
                            except Exception:
                                pass

                        await response.write_eof()

                        if collected_usage:
                            input_tokens = collected_usage.get("prompt_tokens", input_tokens)
                            output_tokens = collected_usage.get("completion_tokens")
                            if not output_tokens or output_tokens == 0:
                                output_tokens = max(1, content_chars // 4)
                        
                        # Check token budget after getting usage data
                        allowed, budget_status = _token_budget.record_and_check(
                            input_tokens or 0, output_tokens or 0
                        )
                        
                        # Add budget status to response headers for all responses
                        budget_headers = {
                            "X-Token-Budget-Used": str(budget_status["total_used"]),
                            "X-Token-Budget-Remaining": str(budget_status["remaining"]),
                            "X-Token-Budget-Percentage": str(budget_status["percentage_used"]),
                            "X-Token-Budget-Limit": str(budget_status["daily_limit"]),
                        }
                        
                        if not allowed:
                            # Budget exceeded - send error response
                            error_msg = (
                                f"🚫 DAILY TOKEN BUDGET EXCEEDED\\n\\n"
                                f"Used: {budget_status['total_used']:,} tokens "
                                f"({budget_status['percentage_used']:.1f}% of daily limit)\\n"
                                f"Limit: {budget_status['daily_limit']:,} tokens/day\\n"
                                f"Remaining: {budget_status['remaining']:,} tokens\\n\\n"
                                f"Token cap enforced by proxy. Requests blocked until 24h window rolls."
                            )
                            return web.json_response(
                                {"error": {"message": error_msg, "type": "token_budget_exceeded"}},
                                status=429,
                                headers=budget_headers
                            )

                        t_total = (time.monotonic() - t_start) * 1000
                        if ttfb_ms is None:
                            ttfb_ms = t_total
                        if output_tokens and t_total > 0:
                            tokens_per_s = output_tokens / (t_total / 1000)

                        log_call(model, path, input_tokens, output_tokens,
                                 ttfb_ms, t_total, tokens_per_s,
                                 server_running, server_tok_s, server_model,
                                 status_code, error, call_type)
                        logged = True
                        log_proxy_call(path, method, call_type, model, status_code, error, 1, ttfb_ms, t_total)
                        
                        # Add budget headers to successful response
                        for key, value in budget_headers.items():
                            response.headers[key] = value
                        return response

                    else:
                        resp_body = await upstream_resp.read()
                        t_first_byte = time.monotonic()
                        ttfb_ms = (t_first_byte - t_start) * 1000

                        try:
                            resp_data = json.loads(resp_body)
                            if resp_data.get("usage"):
                                u = resp_data["usage"]
                                input_tokens = u.get("prompt_tokens", input_tokens)
                                output_tokens = u.get("completion_tokens")
                        except (json.JSONDecodeError, KeyError):
                            pass

                        # Check token budget after getting usage data
                        allowed, budget_status = _token_budget.record_and_check(
                            input_tokens or 0, output_tokens or 0
                        )
                        
                        # Add budget status to response headers for all responses
                        budget_headers = {
                            "X-Token-Budget-Used": str(budget_status["total_used"]),
                            "X-Token-Budget-Remaining": str(budget_status["remaining"]),
                            "X-Token-Budget-Percentage": str(budget_status["percentage_used"]),
                            "X-Token-Budget-Limit": str(budget_status["daily_limit"]),
                        }
                        
                        if not allowed:
                            # Budget exceeded - send error response
                            error_msg = (
                                f"🚫 DAILY TOKEN BUDGET EXCEEDED\n\n"
                                f"Used: {budget_status['total_used']:,} tokens "
                                f"({budget_status['percentage_used']:.1f}% of daily limit)\n"
                                f"Limit: {budget_status['daily_limit']:,} tokens/day\n"
                                f"Remaining: {budget_status['remaining']:,} tokens\n\n"
                                f"Token cap enforced by proxy. Requests blocked until 24h window rolls."
                            )
                            return web.json_response(
                                {"error": {"message": error_msg, "type": "token_budget_exceeded"}},
                                status=429,
                                headers=budget_headers
                            )

                        t_total = (time.monotonic() - t_start) * 1000
                        if ttfb_ms is None:
                            ttfb_ms = t_total
                        if output_tokens and t_total > 0:
                            tokens_per_s = output_tokens / (t_total / 1000)

                        log_call(model, path, input_tokens, output_tokens,
                                 ttfb_ms, t_total, tokens_per_s,
                                 server_running, server_tok_s, server_model,
                                 status_code, error, call_type)
                        logged = True
                        log_proxy_call(path, method, call_type, model, status_code, error, 1, ttfb_ms, t_total)

                        return web.Response(
                            status=upstream_resp.status,
                            body=resp_body,
                            content_type=upstream_resp.content_type,
                        )

    except asyncio.TimeoutError:
        error = "timeout"
        t_total = (time.monotonic() - t_start) * 1000
        log_call(model, path, input_tokens, output_tokens,
                 ttfb_ms, t_total, None,
                 server_running, server_tok_s, server_model,
                 None, error, call_type)
        log_proxy_call(path, method, call_type, model, None, error, 1 if logged else 0, ttfb_ms, t_total)
        return web.json_response({"error": {"message": "upstream timeout"}}, status=504)

    except Exception as e:
        error = str(e)[:200]
        t_total = (time.monotonic() - t_start) * 1000
        log_call(model, path, input_tokens, output_tokens,
                 ttfb_ms, t_total, None,
                 server_running, server_tok_s, server_model,
                 status_code, error, call_type)
        log_proxy_call(path, method, call_type, model, status_code, error, 1 if logged else 0, ttfb_ms, t_total)
        return web.json_response({"error": {"message": str(e)}}, status=502)


async def _simple_forward(request, path, method):
    """Forward non-inference calls (model list, props, etc.) without logging to api_calls."""
    upstream_url = f"{UPSTREAM}{path.replace('/v1', '', 1)}" if path.startswith("/v1") else f"{UPSTREAM}{path}"
    headers = dict(request.headers)
    headers.pop("Host", None)
    headers.pop("host", None)

    t_start = time.monotonic()
    status_code = None
    error = None

    try:
        async with _upstream_semaphore:
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    method, upstream_url,
                    headers=headers,
                    params=request.query,
                ) as upstream_resp:
                    body = await upstream_resp.read()
                    status_code = upstream_resp.status
                    t_total = (time.monotonic() - t_start) * 1000
                    # Still count these in proxy_calls for cross-checking
                    log_proxy_call(path, method, classify_endpoint(path), None, status_code, None, 0, None, t_total)
                    return web.Response(
                        status=upstream_resp.status,
                        body=body,
                        content_type=upstream_resp.content_type,
                    )
    except Exception as e:
        error = str(e)[:200]
        t_total = (time.monotonic() - t_start) * 1000
        log_proxy_call(path, method, classify_endpoint(path), None, None, error, 0, None, t_total)
        return web.json_response({"error": {"message": str(e)}}, status=502)


async def handle_health(request: web.Request) -> web.Response:
    """Health check endpoint with token budget and queue status."""
    budget_status = _token_budget.get_status()
    
    # Fetch per-model queue stats from cached load data
    model_queue = {}
    load_data = _load_cache.get("data")
    if load_data:
        for model_name in ["Deepseek-v4", "Glm-5.2"]:
            d = load_data.get(model_name)
            if d:
                model_queue[model_name] = {
                    "running": d["running"],
                    "waiting": d["waiting"],
                }
    
    return web.json_response({
        "status": "ok",
        "upstream": UPSTREAM,
        "db": str(DB_PATH),
        "rate_limiter": {
            "max_concurrent": MAX_CONCURRENT,
            "active": MAX_CONCURRENT - _upstream_semaphore._value,
        },
        "token_budget": budget_status,
        "model_queue": model_queue,
    })


# ── App ──────────────────────────────────────────────────────────────────────
def create_app():
    app = web.Application(client_max_size=10 * 1024 * 1024)
    app.router.add_route("*", "/v1/{tail:.*}", handle_proxy)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/", handle_health)
    return app


def main():
    init_db()
    print(f"[telemetry] Proxy starting on {LISTEN_HOST}:{LISTEN_PORT}", file=sys.stderr)
    print(f"[telemetry] Upstream: {UPSTREAM}", file=sys.stderr)
    print(f"[telemetry] DB: {DB_PATH}", file=sys.stderr)
    print(f"[telemetry] Dashboard: ~/telemetry-dashboard/dashboard.sh start", file=sys.stderr)
    web.run_app(create_app(), host=LISTEN_HOST, port=LISTEN_PORT, access_log=None)


if __name__ == "__main__":
    main()
