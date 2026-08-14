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
import argparse
import atexit
import os
import signal
import uuid
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

REPO_ROOT = Path(__file__).resolve().parent.parent
LOGGER_DIR = REPO_ROOT / "logger"
_env_logger_file = os.environ.get("LOGGER_FILE_PATH")
LOGGER_FILE = Path(_env_logger_file) if _env_logger_file else (LOGGER_DIR / "payloads.jsonl")

# Raw payload logging state (Default: False)
_raw_logging_enabled = False
_raw_subscribers = set()

_env_db_path = os.environ.get("TELEMETRY_DB_PATH")
DB_PATH = Path(_env_db_path) if _env_db_path else (REPO_ROOT / "data" / "llm_telemetry.db")
PID_FILE = REPO_ROOT / "data" / ".proxy.pid"

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


# ── Real-Time Model Mapping Resolution (Held in Server RAM) ──────────────────
_model_mapping_cache = {}
_model_mapping_mtime = 0

def load_model_mapping():
    global _model_mapping_cache, _model_mapping_mtime
    candidates = [
        REPO_ROOT / "data" / "model_mapping.json",
        REPO_ROOT / "model_mapping.json",
    ]
    for p in candidates:
        if p.exists():
            try:
                mtime = p.stat().st_mtime
                if mtime != _model_mapping_mtime or not _model_mapping_cache:
                    with open(p, "r", encoding="utf-8") as f:
                        _model_mapping_cache = json.load(f)
                    _model_mapping_mtime = mtime
                return _model_mapping_cache
            except Exception:
                pass
    return _model_mapping_cache

def resolve_canonical_model(model_name: str) -> str:
    if not model_name:
        return model_name
    mapping = load_model_mapping()
    if not mapping:
        return model_name
    m_lower = str(model_name).lower().strip()
    for canonical, aliases in mapping.items():
        if m_lower == canonical.lower().strip():
            return canonical
        for alias in aliases:
            if m_lower == str(alias).lower().strip():
                return canonical
    return model_name


def log_call(model, endpoint, input_tokens, output_tokens,
             ttfb_ms, total_ms, tokens_per_s,
             server_running, server_tok_s, server_model,
             status_code, error, call_type='chat'):
    try:
        model = resolve_canonical_model(model)
        conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
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
    except Exception as e:
        print(f"[telemetry] log_call error: {e}", file=sys.stderr)


def log_proxy_call(endpoint, method, call_type, model, status_code, error, logged, ttfb_ms, total_ms):
    """Log EVERY request through the proxy — even ones that fail before logging to api_calls."""
    try:
        model = resolve_canonical_model(model)
        conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
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
    except Exception as e:
        print(f"[telemetry] log_proxy_call error: {e}", file=sys.stderr)


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
                        if not isinstance(m, dict):
                            continue
                        name = m.get("model_name") or m.get("container", "?")
                        latest = m.get("latest")
                        if not isinstance(latest, dict):
                            latest = {}
                        if isinstance(latest.get("num_requests_running"), dict):
                            running, tok_s = 0, 0.0
                        else:
                            running = latest.get("num_requests_running", 0) or 0
                            tok_s = latest.get("generation_tokens_rate", 0.0) or 0.0
                        data[name] = {
                            "status": m.get("status", "unknown"),
                            "running": running,
                            "tok_s": tok_s,
                            "kv_cache": latest.get("kv_cache_usage_perc", 0) or 0,
                            "waiting": latest.get("num_requests_waiting", 0) or 0,
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


# ── Raw Payload Logging Helpers ─────────────────────────────────────────────
def format_size(bytes_val: int) -> str:
    if bytes_val < 1024:
        return f"{bytes_val} B"
    elif bytes_val < 1024 * 1024:
        return f"{bytes_val / 1024:.1f} KB"
    else:
        return f"{bytes_val / (1024 * 1024):.2f} MB"


def make_raw_payload_record(
    req_id: str,
    path: str,
    method: str,
    call_type: str,
    model: str,
    client_ip: str,
    req_headers: dict,
    payload_obj: any,
    status_code: int,
    resp_headers: dict,
    is_stream: bool,
    ttfb_ms: float,
    total_ms: float,
    tokens_per_s: float,
    input_tokens: int,
    output_tokens: int,
    reasoning_tokens: int,
    content_text: str,
    reasoning_text: str,
    tool_calls: any,
    raw_resp_json: any,
    error: str,
) -> dict:
    safe_req_headers = {}
    for k, v in (req_headers or {}).items():
        if k.lower() == "authorization" and isinstance(v, str) and v.startswith("Bearer "):
            token = v[7:]
            if len(token) > 10:
                masked = f"Bearer {token[:4]}...{token[-4:]}"
            else:
                masked = "Bearer ***"
            safe_req_headers[k] = masked
        else:
            safe_req_headers[k] = v

    return {
        "id": req_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "endpoint": path,
        "method": method,
        "call_type": call_type,
        "model": model,
        "client": {
            "ip": client_ip,
            "headers": safe_req_headers,
            "user_agent": safe_req_headers.get("User-Agent") or safe_req_headers.get("user-agent", ""),
        },
        "request": {
            "headers": safe_req_headers,
            "payload": payload_obj,
            "messages": payload_obj.get("messages") if isinstance(payload_obj, dict) else None,
            "prompt": payload_obj.get("prompt") if isinstance(payload_obj, dict) else None,
            "input": payload_obj.get("input") if isinstance(payload_obj, dict) else None,
            "parameters": {
                k: v for k, v in payload_obj.items()
                if k not in ("messages", "prompt", "input")
            } if isinstance(payload_obj, dict) else {},
        },
        "response": {
            "status_code": status_code,
            "headers": dict(resp_headers) if resp_headers else {},
            "is_stream": bool(is_stream),
            "ttfb_ms": round(ttfb_ms, 2) if ttfb_ms is not None else None,
            "total_ms": round(total_ms, 2) if total_ms is not None else None,
            "tokens_per_s": round(tokens_per_s, 2) if tokens_per_s is not None else None,
            "usage": {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "reasoning_tokens": reasoning_tokens,
                "total_tokens": ((input_tokens or 0) + (output_tokens or 0)) if (input_tokens is not None or output_tokens is not None) else None,
            },
            "content": {
                "text": content_text,
                "reasoning_content": reasoning_text,
                "tool_calls": tool_calls,
            },
            "raw_json": raw_resp_json,
            "error": error,
        },
    }


def append_raw_payload(record: dict):
    if not _raw_logging_enabled:
        return
    try:
        LOGGER_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with open(LOGGER_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        print(f"[telemetry] Failed to append raw payload to {LOGGER_FILE}: {e}", file=sys.stderr)


async def broadcast_raw_payload(record: dict):
    if not _raw_subscribers:
        return
    dead = set()
    for q in list(_raw_subscribers):
        try:
            q.put_nowait(record)
        except asyncio.QueueFull:
            pass
        except Exception:
            dead.add(q)
    _raw_subscribers.difference_update(dead)


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
    payload = None
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

    req_id = f"req_{uuid.uuid4().hex[:12]}"
    t_start = time.monotonic()
    ttfb_ms = None
    status_code = None
    error = None
    output_tokens = None
    reasoning_tokens = None
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
                        collected_content = ""
                        collected_reasoning = ""
                        collected_tool_calls = []

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
                                            if isinstance(chunk_data, dict):
                                                if chunk_data.get("usage") and isinstance(chunk_data["usage"], dict):
                                                    collected_usage = chunk_data["usage"]
                                                choices = chunk_data.get("choices")
                                                if isinstance(choices, list):
                                                    for choice in choices:
                                                        if isinstance(choice, dict):
                                                            delta = choice.get("delta")
                                                            if isinstance(delta, dict):
                                                                if delta.get("content"):
                                                                    content_chars += len(delta["content"])
                                                                    collected_content += delta["content"]
                                                                if delta.get("reasoning_content"):
                                                                    content_chars += len(delta["reasoning_content"])
                                                                    collected_reasoning += delta["reasoning_content"]
                                                if chunk_data.get("error"):
                                                    err_obj = chunk_data["error"]
                                                    if isinstance(err_obj, dict):
                                                        error = err_obj.get("message") or err_obj.get("type") or str(err_obj)
                                                    else:
                                                        error = str(err_obj)
                                        except json.JSONDecodeError:
                                            pass
                            except Exception:
                                pass

                        await response.write_eof()

                        # Telemetry post-processing (isolated: errors here will never affect the client)
                        try:
                            if status_code and (status_code < 200 or status_code >= 300) and not error:
                                error = f"HTTP {status_code}"

                            if collected_usage and isinstance(collected_usage, dict):
                                input_tokens = collected_usage.get("prompt_tokens", input_tokens)
                                output_tokens = collected_usage.get("completion_tokens")
                                details = collected_usage.get("completion_tokens_details")
                                if isinstance(details, dict):
                                    reasoning_tokens = details.get("reasoning_tokens")
                                else:
                                    reasoning_tokens = collected_usage.get("reasoning_tokens")
                                if not output_tokens or output_tokens == 0:
                                    output_tokens = max(1, content_chars // 4)

                            # Record token budget
                            try:
                                _token_budget.record_and_check(input_tokens or 0, output_tokens or 0)
                            except Exception as b_err:
                                print(f"[telemetry] token budget record error: {b_err}", file=sys.stderr)

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

                            raw_record = make_raw_payload_record(
                                req_id=req_id,
                                path=path,
                                method=method,
                                call_type=call_type,
                                model=model,
                                client_ip=request.remote,
                                req_headers=dict(request.headers),
                                payload_obj=payload,
                                status_code=status_code,
                                resp_headers=dict(upstream_resp.headers),
                                is_stream=True,
                                ttfb_ms=ttfb_ms,
                                total_ms=t_total,
                                tokens_per_s=tokens_per_s,
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                reasoning_tokens=reasoning_tokens,
                                content_text=collected_content,
                                reasoning_text=collected_reasoning,
                                tool_calls=collected_tool_calls if collected_tool_calls else None,
                                raw_resp_json=None,
                                error=error,
                            )
                            if _raw_logging_enabled:
                                append_raw_payload(raw_record)
                            if _raw_subscribers:
                                asyncio.create_task(broadcast_raw_payload(raw_record))
                        except Exception as tel_err:
                            print(f"[telemetry] streaming telemetry error: {tel_err}", file=sys.stderr)

                        return response

                    else:
                        resp_body = await upstream_resp.read()
                        t_first_byte = time.monotonic()
                        ttfb_ms = (t_first_byte - t_start) * 1000
                        t_total = (time.monotonic() - t_start) * 1000

                        resp_headers = {}
                        for k, v in upstream_resp.headers.items():
                            if k.lower() not in ("content-length", "content-encoding", "transfer-encoding"):
                                resp_headers[k] = v

                        # Telemetry post-processing (isolated: errors here will never affect the client response)
                        budget_headers = {}
                        allowed = True
                        try:
                            resp_data = None
                            resp_text = None
                            resp_reasoning = None
                            resp_tool_calls = None
                            try:
                                resp_data = json.loads(resp_body)
                                if isinstance(resp_data, dict):
                                    u = resp_data.get("usage")
                                    if isinstance(u, dict):
                                        input_tokens = u.get("prompt_tokens", input_tokens)
                                        output_tokens = u.get("completion_tokens")
                                        details = u.get("completion_tokens_details")
                                        if isinstance(details, dict):
                                            reasoning_tokens = details.get("reasoning_tokens")
                                        else:
                                            reasoning_tokens = u.get("reasoning_tokens")
                                    choices = resp_data.get("choices")
                                    if isinstance(choices, list) and len(choices) > 0 and isinstance(choices[0], dict):
                                        msg = choices[0].get("message")
                                        if isinstance(msg, dict):
                                            resp_text = msg.get("content")
                                            resp_reasoning = msg.get("reasoning_content")
                                            resp_tool_calls = msg.get("tool_calls")
                                        if not resp_text and "text" in choices[0]:
                                            resp_text = choices[0].get("text")
                                    if resp_data.get("error"):
                                        err_obj = resp_data["error"]
                                        if isinstance(err_obj, dict):
                                            error = err_obj.get("message") or err_obj.get("type") or str(err_obj)
                                        else:
                                            error = str(err_obj)
                                    elif resp_data.get("message") and status_code and (status_code < 200 or status_code >= 300):
                                        error = str(resp_data["message"])
                                    elif resp_data.get("detail") and status_code and (status_code < 200 or status_code >= 300):
                                        error = str(resp_data["detail"])
                            except Exception:
                                if status_code and (status_code < 200 or status_code >= 300):
                                    error = resp_body.decode("utf-8", errors="replace")[:200].strip()

                            if not error and status_code and (status_code < 200 or status_code >= 300):
                                error = f"HTTP {status_code}"

                            # Check token budget
                            try:
                                allowed, budget_status = _token_budget.record_and_check(
                                    input_tokens or 0, output_tokens or 0
                                )
                                budget_headers = {
                                    "X-Token-Budget-Used": str(budget_status["total_used"]),
                                    "X-Token-Budget-Remaining": str(budget_status["remaining"]),
                                    "X-Token-Budget-Percentage": str(budget_status["percentage_used"]),
                                    "X-Token-Budget-Limit": str(budget_status["daily_limit"]),
                                }
                                resp_headers.update(budget_headers)
                            except Exception as b_err:
                                print(f"[telemetry] token budget check error: {b_err}", file=sys.stderr)

                            if output_tokens and t_total > 0:
                                tokens_per_s = output_tokens / (t_total / 1000)

                            if not allowed:
                                error = "token_budget_exceeded"

                            log_call(model, path, input_tokens, output_tokens,
                                     ttfb_ms, t_total, tokens_per_s,
                                     server_running, server_tok_s, server_model,
                                     status_code, error, call_type)
                            logged = True
                            log_proxy_call(path, method, call_type, model, status_code, error, 1, ttfb_ms, t_total)

                            raw_record = make_raw_payload_record(
                                req_id=req_id,
                                path=path,
                                method=method,
                                call_type=call_type,
                                model=model,
                                client_ip=request.remote,
                                req_headers=dict(request.headers),
                                payload_obj=payload,
                                status_code=status_code,
                                resp_headers=dict(upstream_resp.headers),
                                is_stream=False,
                                ttfb_ms=ttfb_ms,
                                total_ms=t_total,
                                tokens_per_s=tokens_per_s,
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                reasoning_tokens=reasoning_tokens,
                                content_text=resp_text,
                                reasoning_text=resp_reasoning,
                                tool_calls=resp_tool_calls,
                                raw_resp_json=resp_data,
                                error=error,
                            )
                            if _raw_logging_enabled:
                                append_raw_payload(raw_record)
                            if _raw_subscribers:
                                asyncio.create_task(broadcast_raw_payload(raw_record))
                        except Exception as tel_err:
                            print(f"[telemetry] batch telemetry error: {tel_err}", file=sys.stderr)

                        if not allowed:
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

                        return web.Response(
                            status=upstream_resp.status,
                            body=resp_body,
                            headers=resp_headers,
                            content_type=upstream_resp.content_type,
                        )

    except asyncio.TimeoutError:
        error = "timeout"
        t_total = (time.monotonic() - t_start) * 1000
        try:
            log_call(model, path, input_tokens, output_tokens,
                     ttfb_ms, t_total, None,
                     server_running, server_tok_s, server_model,
                     None, error, call_type)
            log_proxy_call(path, method, call_type, model, None, error, 1 if logged else 0, ttfb_ms, t_total)
        except Exception:
            pass

        return web.json_response({"error": {"message": "upstream timeout"}}, status=504)

    except Exception as e:
        error = str(e)[:200]
        t_total = (time.monotonic() - t_start) * 1000
        try:
            log_call(model, path, input_tokens, output_tokens,
                     ttfb_ms, t_total, None,
                     server_running, server_tok_s, server_model,
                     status_code, error, call_type)
            log_proxy_call(path, method, call_type, model, status_code, error, 1 if logged else 0, ttfb_ms, t_total)
        except Exception:
            pass

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
                    if status_code and (status_code < 200 or status_code >= 300):
                        error = f"HTTP {status_code}"
                    
                    try:
                        log_proxy_call(path, method, classify_endpoint(path), None, status_code, error, 0, None, t_total)
                    except Exception as tel_err:
                        print(f"[telemetry] _simple_forward log error: {tel_err}", file=sys.stderr)

                    return web.Response(
                        status=upstream_resp.status,
                        body=body,
                        content_type=upstream_resp.content_type,
                    )
    except asyncio.TimeoutError:
        try:
            log_proxy_call(path, method, classify_endpoint(path), None, 504, "timeout", 0, None, (time.monotonic() - t_start) * 1000)
        except Exception:
            pass
        return web.json_response({"error": {"message": "upstream timeout"}}, status=504)
    except Exception as e:
        error = str(e)[:200]
        t_total = (time.monotonic() - t_start) * 1000
        try:
            log_proxy_call(path, method, classify_endpoint(path), None, None, error, 0, None, t_total)
        except Exception:
            pass
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
    
    file_size = LOGGER_FILE.stat().st_size if LOGGER_FILE.exists() else 0
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
        "raw_logging": {
            "enabled": _raw_logging_enabled,
            "file_path": str(LOGGER_FILE),
            "file_size_bytes": file_size,
            "file_size_formatted": format_size(file_size),
            "subscribers_count": len(_raw_subscribers),
        }
    })


# ── Raw Payload Management Endpoints ─────────────────────────────────────────
async def handle_raw_log_status(request: web.Request) -> web.Response:
    """GET /v1/raw-log/status or /raw-log/status"""
    file_size = LOGGER_FILE.stat().st_size if LOGGER_FILE.exists() else 0
    return web.json_response({
        "enabled": _raw_logging_enabled,
        "file_path": str(LOGGER_FILE),
        "rel_path": str(LOGGER_FILE.relative_to(REPO_ROOT)) if LOGGER_FILE.is_relative_to(REPO_ROOT) else str(LOGGER_FILE),
        "file_size_bytes": file_size,
        "file_size_formatted": format_size(file_size),
        "subscribers_count": len(_raw_subscribers),
    })


async def handle_raw_log_toggle(request: web.Request) -> web.Response:
    """POST /v1/raw-log/toggle or /raw-log/toggle"""
    global _raw_logging_enabled
    try:
        data = await request.json() if request.can_read_body else {}
    except Exception:
        data = {}
    
    if "enabled" in data:
        _raw_logging_enabled = bool(data["enabled"])
    else:
        _raw_logging_enabled = not _raw_logging_enabled

    file_size = LOGGER_FILE.stat().st_size if LOGGER_FILE.exists() else 0
    return web.json_response({
        "success": True,
        "enabled": _raw_logging_enabled,
        "file_path": str(LOGGER_FILE),
        "file_size_formatted": format_size(file_size),
        "message": f"Raw payload logging is now {'ENABLED' if _raw_logging_enabled else 'DISABLED'}"
    })


async def handle_raw_log_recent(request: web.Request) -> web.Response:
    """GET /v1/raw-log/recent or /raw-log/recent — retrieve the last N lines from the logger file."""
    limit_str = request.query.get("limit", "50")
    try:
        limit = max(1, min(500, int(limit_str)))
    except ValueError:
        limit = 50

    if not LOGGER_FILE.exists():
        return web.json_response({"entries": [], "total_count": 0})

    try:
        entries = []
        with open(LOGGER_FILE, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        pass
        total_count = len(entries)
        recent_entries = entries[-limit:]
        return web.json_response({"entries": list(reversed(recent_entries)), "total_count": total_count})
    except Exception as e:
        return web.json_response({"error": str(e), "entries": []}, status=500)


async def handle_raw_log_clear(request: web.Request) -> web.Response:
    """POST /v1/raw-log/clear or /raw-log/clear — truncate the logger file."""
    try:
        if LOGGER_FILE.exists():
            with open(LOGGER_FILE, "w", encoding="utf-8") as f:
                f.truncate(0)
        return web.json_response({"success": True, "message": "Logger file cleared successfully."})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_raw_log_stream(request: web.Request) -> web.StreamResponse:
    """GET /v1/raw-log/stream or /raw-log/stream — Server-Sent Events (SSE) live feed."""
    response = web.StreamResponse(
        status=200,
        reason='OK',
        headers={
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'Access-Control-Allow-Origin': '*',
        }
    )
    await response.prepare(request)

    q = asyncio.Queue(maxsize=100)
    _raw_subscribers.add(q)
    try:
        # Initial connect ping
        init_payload = json.dumps({"type": "connected", "enabled": _raw_logging_enabled, "timestamp": datetime.now(timezone.utc).isoformat()})
        await response.write(f"data: {init_payload}\n\n".encode("utf-8"))
        await response.drain()

        while True:
            try:
                record = await asyncio.wait_for(q.get(), timeout=15.0)
                data = json.dumps(record, ensure_ascii=False)
                await response.write(f"data: {data}\n\n".encode("utf-8"))
                await response.drain()
            except asyncio.TimeoutError:
                # Keep-alive heartbeat comment
                await response.write(b": keepalive\n\n")
                await response.drain()
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        _raw_subscribers.discard(q)
    return response


# ── App ──────────────────────────────────────────────────────────────────────
@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        return web.Response(headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS, PUT, DELETE",
            "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Requested-With",
        })
    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


def create_app():
    app = web.Application(client_max_size=10 * 1024 * 1024, middlewares=[cors_middleware])
    
    # Raw payload management routes (must be before wildcard /v1/{tail:.*})
    app.router.add_get("/v1/raw-log/status", handle_raw_log_status)
    app.router.add_get("/raw-log/status", handle_raw_log_status)
    app.router.add_post("/v1/raw-log/toggle", handle_raw_log_toggle)
    app.router.add_post("/raw-log/toggle", handle_raw_log_toggle)
    app.router.add_get("/v1/raw-log/recent", handle_raw_log_recent)
    app.router.add_get("/raw-log/recent", handle_raw_log_recent)
    app.router.add_post("/v1/raw-log/clear", handle_raw_log_clear)
    app.router.add_post("/raw-log/clear", handle_raw_log_clear)
    app.router.add_get("/v1/raw-log/stream", handle_raw_log_stream)
    app.router.add_get("/raw-log/stream", handle_raw_log_stream)

    app.router.add_route("*", "/v1/{tail:.*}", handle_proxy)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/", handle_health)
    return app


def main():
    global LISTEN_HOST, LISTEN_PORT, UPSTREAM, DB_PATH, PID_FILE

    parser = argparse.ArgumentParser(description="LLM Telemetry Proxy")
    parser.add_argument("--port", type=int, default=LISTEN_PORT, help="Listen port (default 9090)")
    parser.add_argument("--host", type=str, default=LISTEN_HOST, help="Listen host (default 0.0.0.0)")
    parser.add_argument("--upstream", type=str, default=UPSTREAM, help="Upstream API base URL")
    parser.add_argument("--db", type=str, default=str(DB_PATH), help="SQLite database file path")
    parser.add_argument("--pid-file", type=str, default=str(PID_FILE), help="PID file path")
    args = parser.parse_args()

    LISTEN_PORT = args.port
    LISTEN_HOST = args.host
    UPSTREAM = args.upstream
    DB_PATH = Path(args.db)
    PID_FILE = Path(args.pid_file)

    # Write PID file
    try:
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception as e:
        print(f"[telemetry] Warning: could not write PID file: {e}", file=sys.stderr)

    def cleanup_pid():
        try:
            if PID_FILE.exists():
                content = PID_FILE.read_text(encoding="utf-8").strip()
                if content == str(os.getpid()):
                    PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    atexit.register(cleanup_pid)

    def handle_signal(sig, frame):
        cleanup_pid()
        sys.exit(0)

    try:
        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
    except Exception:
        pass

    init_db()
    print(f"[telemetry] Proxy starting on {LISTEN_HOST}:{LISTEN_PORT}", file=sys.stderr)
    print(f"[telemetry] Upstream: {UPSTREAM}", file=sys.stderr)
    print(f"[telemetry] DB: {DB_PATH}", file=sys.stderr)
    print(f"[telemetry] PID: {os.getpid()}", file=sys.stderr)
    print(f"[telemetry] Dashboard: Control via Dashboard UI (http://localhost:9118)", file=sys.stderr)
    web.run_app(create_app(), host=LISTEN_HOST, port=LISTEN_PORT, access_log=None)


if __name__ == "__main__":
    main()

