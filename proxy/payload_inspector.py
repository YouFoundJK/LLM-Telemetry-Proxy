#!/usr/bin/env python3
"""
Raw payload inspector, JSONL logging, SSE broadcasting, and proxy admin endpoints.
Part of the LLM Telemetry Proxy.
"""

import os
import sys
import json
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List, Set

from aiohttp import web

try:
    from proxy.repo_paths import resolve_repo_root, REPO_ROOT
except ImportError:
    from repo_paths import resolve_repo_root, REPO_ROOT

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from proxy.fast_json import json_loads, json_dumps
except ImportError:
    from fast_json import json_loads, json_dumps

from proxy.telemetry_db import (
    _token_budget,
    DB_PATH,
    _load_cache,
)

LOGGER_DIR = REPO_ROOT / "logger"
_env_logger_file = os.environ.get("LOGGER_FILE_PATH")
LOGGER_FILE = Path(_env_logger_file) if _env_logger_file else (LOGGER_DIR / "payloads.jsonl")

# Raw payload logging state (Default: False)
_raw_logging_enabled = False
_raw_subscribers: Set[asyncio.Queue] = set()
_raw_payload_counter = 0


def next_raw_payload_seq() -> int:
    global _raw_payload_counter
    _raw_payload_counter += 1
    return _raw_payload_counter


def format_size(bytes_val: int) -> str:
    if bytes_val < 1024:
        return f"{bytes_val} B"
    elif bytes_val < 1024 * 1024:
        return f"{bytes_val / 1024:.1f} KB"
    else:
        return f"{bytes_val / (1024 * 1024):.2f} MB"


def _sanitize_headers(req_headers: dict) -> dict:
    safe_req_headers = {}
    sensitive_keys = {"authorization", "api-key", "x-api-key", "x-auth-token", "proxy-authorization"}
    for k, v in (req_headers or {}).items():
        k_lower = str(k).lower()
        if k_lower in sensitive_keys and isinstance(v, str):
            if v.startswith("Bearer ") and len(v) > 17:
                token = v[7:]
                masked = f"Bearer {token[:4]}...{token[-4:]}"
            elif len(v) > 10:
                masked = f"{v[:4]}...{v[-4:]}"
            else:
                masked = "***"
            safe_req_headers[k] = masked
        else:
            safe_req_headers[k] = v
    return safe_req_headers


def make_raw_payload_start_record(
    req_id: str,
    path: str,
    method: str,
    call_type: str,
    model: str,
    client_ip: str,
    req_headers: dict,
    payload_obj: any,
    is_stream: bool,
    seq: int = None,
) -> dict:
    safe_req_headers = _sanitize_headers(req_headers)
    return {
        "id": req_id,
        "seq": seq,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "in_progress",
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
            "status_code": None,
            "headers": {},
            "is_stream": bool(is_stream),
            "ttfb_ms": None,
            "total_ms": None,
            "tokens_per_s": None,
            "usage": {
                "prompt_tokens": None,
                "completion_tokens": None,
                "reasoning_tokens": None,
                "total_tokens": None,
            },
            "content": {
                "text": None,
                "reasoning_content": None,
                "tool_calls": None,
            },
            "raw_json": None,
            "error": None,
        },
    }


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
    seq: int = None,
) -> dict:
    safe_req_headers = _sanitize_headers(req_headers)
    return {
        "id": req_id,
        "seq": seq,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "completed",
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


def read_recent_jsonl_lines(file_path: Path, limit: int = 50) -> tuple[list[dict], int]:
    """Efficiently read the last `limit` lines from a JSONL file without reading or parsing the whole file."""
    if not file_path or not Path(file_path).exists():
        return [], 0

    p = Path(file_path)
    file_size = p.stat().st_size
    if file_size == 0:
        return [], 0

    lines = []
    total_count = 0

    if file_size < 512 * 1024:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line_s = line.strip()
                if line_s:
                    total_count += 1
                    lines.append(line_s)
        recent_lines = lines[-limit:]
    else:
        chunk_size = 64 * 1024
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            position = f.tell()
            buffer = bytearray()
            found_lines = []

            while position > 0 and len(found_lines) <= (limit + 5):
                read_size = min(chunk_size, position)
                position -= read_size
                f.seek(position, os.SEEK_SET)
                chunk = f.read(read_size)
                buffer = chunk + buffer

                parts = buffer.split(b"\n")
                if position > 0:
                    buffer = parts[0]
                    complete_lines = parts[1:]
                else:
                    buffer = bytearray()
                    complete_lines = parts

                for part in complete_lines:
                    p_str = part.decode("utf-8", errors="replace").strip()
                    if p_str:
                        found_lines.append(p_str)

            recent_lines = found_lines[-limit:]
            total_count = max(len(found_lines), limit)

    entries = []
    for l in recent_lines:
        try:
            entries.append(json_loads(l))
        except Exception:
            pass

    return list(reversed(entries)), total_count


def append_raw_payload(record: dict):
    if not _raw_logging_enabled:
        return
    try:
        LOGGER_DIR.mkdir(parents=True, exist_ok=True)
        line = json_dumps(record, ensure_ascii=False) + "\n"
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


# ── Admin & Management Endpoints ─────────────────────────────────────────────
# Dynamic Model Router reference (injected from llm_telemetry_proxy)
_model_router = None
_default_upstream = "https://llm.ai.e-infra.cz/v1"


def set_admin_router(router, default_upstream):
    global _model_router, _default_upstream
    _model_router = router
    _default_upstream = default_upstream


async def handle_health(request: web.Request) -> web.Response:
    """Health check endpoint with token budget, router state, and queue status."""
    budget_status = _token_budget.get_status()
    limiters_summary = _model_router.get_all_limiters_stats() if _model_router else {}
    
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
    upstream_url = (_model_router.default_upstream_url if _model_router else None) or _default_upstream
    return web.json_response({
        "status": "ok",
        "upstream": upstream_url,
        "router": {
            "default_upstream": _model_router.default_upstream_url if _model_router else None,
            "active_rules_count": sum(1 for r in _model_router.rules if r.enabled) if _model_router else 0,
            "total_rules_count": len(_model_router.rules) if _model_router else 0,
        },
        "db": str(DB_PATH),
        "rate_limiter": _model_router.default_limiter.get_stats() if _model_router else {},
        "limiters_summary": limiters_summary,
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


async def handle_routes_get(request: web.Request) -> web.Response:
    """GET /v1/routes or /routes — retrieves active routing configuration."""
    if not _model_router:
        return web.json_response({"error": "Router not initialized"}, status=500)
    return web.json_response(_model_router.to_dict())


async def handle_routes_save(request: web.Request) -> web.Response:
    """POST /v1/routes or /routes — updates routing configuration and persists to disk."""
    if not _model_router:
        return web.json_response({"error": "Router not initialized"}, status=500)
    try:
        data = await request.json()
        if not isinstance(data, dict):
            return web.json_response({"error": "Invalid payload format, expected JSON object"}, status=400)
        _model_router.update_from_dict(data)
        success = _model_router.save()
        if not success:
            return web.json_response({"error": "Failed to persist routes configuration to disk"}, status=500)
        return web.json_response({
            "success": True,
            "message": "Routes updated and saved successfully",
            "config": _model_router.to_dict(),
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)


async def handle_routes_test(request: web.Request) -> web.Response:
    """POST /v1/routes/test or /routes/test — evaluates route resolution for a model name."""
    if not _model_router:
        return web.json_response({"error": "Router not initialized"}, status=500)
    try:
        data = await request.json() if request.can_read_body else {}
        model_name = data.get("model", "")
        chain = _model_router.resolve_chain(model_name)
        res = chain[0] if chain else _model_router.resolve(model_name)
        candidates_list = [
            {
                "route_id": c.route_id,
                "route_name": c.route_name,
                "resolved_upstream": c.upstream_url,
                "is_default": c.is_default,
                "pattern_matched": c.pattern_matched,
                "priority": c.priority,
                "strategy": c.strategy,
                "max_concurrent": c.max_concurrent,
                "max_rpm": c.max_rpm,
                "has_api_key": bool(c.api_key),
            }
            for c in chain
        ]
        return web.json_response({
            "model": model_name,
            "routing_strategy": _model_router.routing_strategy,
            "resolved_upstream": res.upstream_url,
            "route_name": res.route_name,
            "route_id": res.route_id,
            "is_default": res.is_default,
            "pattern_matched": res.pattern_matched,
            "priority": res.priority,
            "strategy": res.strategy,
            "max_concurrent": res.max_concurrent,
            "slot_cooldown_ms": res.slot_cooldown_ms,
            "max_rpm": res.max_rpm,
            "timeout": res.timeout,
            "fallback_upstream_url": res.fallback_upstream_url,
            "has_api_key": bool(res.api_key),
            "matched_routes_count": len(chain),
            "candidates": candidates_list,
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)


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
        "rel_path": str(LOGGER_FILE.relative_to(REPO_ROOT)) if LOGGER_FILE.is_relative_to(REPO_ROOT) else str(LOGGER_FILE),
        "file_size_bytes": file_size,
        "file_size_formatted": format_size(file_size),
        "subscribers_count": len(_raw_subscribers),
    })


async def handle_raw_log_recent(request: web.Request) -> web.Response:
    """GET /v1/raw-log/recent or /raw-log/recent?limit=50"""
    limit_str = request.query.get("limit", "50")
    limit = int(limit_str) if limit_str.isdigit() else 50
    limit = min(500, max(1, limit))

    entries, total_count = read_recent_jsonl_lines(LOGGER_FILE, limit)
    file_size = LOGGER_FILE.stat().st_size if LOGGER_FILE.exists() else 0
    return web.json_response({
        "enabled": _raw_logging_enabled,
        "total_count": total_count,
        "returned_count": len(entries),
        "file_size_bytes": file_size,
        "file_size_formatted": format_size(file_size),
        "entries": entries,
    })


async def handle_raw_log_clear(request: web.Request) -> web.Response:
    """POST /v1/raw-log/clear or /raw-log/clear"""
    try:
        if LOGGER_FILE.exists():
            with open(LOGGER_FILE, "w", encoding="utf-8") as f:
                f.truncate(0)
        return web.json_response({"success": True, "message": "Logger file cleared successfully."})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_raw_log_stream(request: web.StreamResponse) -> web.StreamResponse:
    """GET /v1/raw-log/stream or /raw-log/stream — Server-Sent Events (SSE) live feed."""
    response = web.StreamResponse(
        status=200,
        reason='OK',
        headers={
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache, no-transform',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
            'Access-Control-Allow-Origin': '*',
        }
    )
    await response.prepare(request)

    q = asyncio.Queue(maxsize=100)
    _raw_subscribers.add(q)
    try:
        init_payload = json_dumps({"type": "connected", "enabled": _raw_logging_enabled, "timestamp": datetime.now(timezone.utc).isoformat()})
        await response.write(f"data: {init_payload}\n\n".encode("utf-8"))

        while True:
            try:
                record = await asyncio.wait_for(q.get(), timeout=15.0)
                data = json_dumps(record, ensure_ascii=False)
                await response.write(f"data: {data}\n\n".encode("utf-8"))
            except asyncio.TimeoutError:
                await response.write(b": keepalive\n\n")
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        _raw_subscribers.discard(q)
    return response
