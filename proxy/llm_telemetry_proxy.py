#!/usr/bin/env python3
"""
LLM Telemetry Proxy — passive instrumentation of real API calls.

Sits between Hermes and e-INFRA. Every request is forwarded transparently,
but timed (TTFB + total RTT), correlated with server load, and logged to SQLite.

Also tracks total API call counts (proxy_calls table) for cross-checking
against logged calls — so you can verify nothing was lost.

Usage:
    python3 proxy/llm_telemetry_proxy.py > proxy.log 2>&1 &
"""

import sys
import os
import time
import uuid
import signal
import atexit
import argparse
import asyncio
from pathlib import Path
from types import ModuleType
from datetime import datetime
from typing import Optional, Dict, Any, Tuple, List, Union

try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    _HAS_UVLOOP = True
except ImportError:
    _HAS_UVLOOP = False

import aiohttp
from aiohttp import web

try:
    from proxy.repo_paths import resolve_repo_root, REPO_ROOT
except ImportError:
    from repo_paths import resolve_repo_root, REPO_ROOT

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from proxy.model_router import (
        ModelRouter,
        build_upstream_url,
        UpstreamConcurrencyLimiter,
        _SlotContextManager,
        RouteResolutionResult,
        normalize_timeout,
    )
except ImportError:
    from model_router import (
        ModelRouter,
        build_upstream_url,
        UpstreamConcurrencyLimiter,
        _SlotContextManager,
        RouteResolutionResult,
        normalize_timeout,
    )

import proxy.telemetry_db as telemetry_db
from proxy.telemetry_db import (
    format_time_remaining,
    RollingTokenBudget,
    _token_budget,
    get_db,
    init_db,
    load_model_mapping,
    resolve_canonical_model,
    classify_endpoint,
    log_call,
    log_proxy_call,
    fetch_server_load,
    _load_cache,
    _load_cache_lock,
    DAILY_TOKEN_LIMIT,
)

import proxy.payload_inspector as payload_inspector
from proxy.payload_inspector import (
    next_raw_payload_seq,
    format_size,
    make_raw_payload_start_record,
    make_raw_payload_record,
    read_recent_jsonl_lines,
    append_raw_payload,
    broadcast_raw_payload,
    handle_health,
    handle_routes_get,
    handle_routes_save,
    handle_routes_test,
    handle_raw_log_status,
    handle_raw_log_toggle,
    handle_raw_log_recent,
    handle_raw_log_clear,
    handle_raw_log_stream,
)

import proxy.proxy_stream as proxy_stream
from proxy.proxy_stream import (
    parse_retry_after,
    evaluate_retry_condition,
    handle_streaming_upstream,
)

import proxy.proxy_forwarder as proxy_forwarder
from proxy.proxy_forwarder import (
    handle_proxy,
    _simple_forward,
)

# ── Configuration Defaults ──────────────────────────────────────────────────
DEFAULT_UPSTREAM = "https://llm.ai.e-infra.cz/v1"
STATUS_API = telemetry_db.STATUS_API
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 9090

PID_FILE = REPO_ROOT / "data" / ".proxy.pid"
TOKEN_BUDGET_FILE = telemetry_db.TOKEN_BUDGET_FILE
ROUTES_CONFIG_FILE = REPO_ROOT / "data" / "model_routes.json"

# Dynamic Model Router instance
_model_router_inst = ModelRouter(config_path=ROUTES_CONFIG_FILE)
if not ROUTES_CONFIG_FILE.exists():
    _model_router_inst.default_upstream_url = DEFAULT_UPSTREAM

MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", 3))
SLOT_COOLDOWN_MS = int(os.environ.get("CONCURRENCY_SLOT_COOLDOWN_MS", 50))
RETRY_429_MAX = int(os.environ.get("RETRY_429_MAX", 3))
UPSTREAM_SESSION_KEY = web.AppKey("upstream_session", aiohttp.ClientSession) if hasattr(web, "AppKey") else "upstream_session"

_concurrency_limiter = _model_router_inst.default_limiter
_concurrency_limiter.max_concurrent = MAX_CONCURRENT
_concurrency_limiter.slot_cooldown_seconds = SLOT_COOLDOWN_MS / 1000.0
_upstream_semaphore = _concurrency_limiter


def _tlog(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


# Initialize submodules with shared references
payload_inspector.set_admin_router(_model_router_inst, DEFAULT_UPSTREAM)
proxy_forwarder.set_forwarder_globals(_model_router_inst, DEFAULT_UPSTREAM, UPSTREAM_SESSION_KEY, RETRY_429_MAX, _tlog)


# ── App Factory & Lifecycle ──────────────────────────────────────────────────
@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        return web.Response(headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS, PUT, DELETE",
            "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Requested-With",
        })
    try:
        response = await handler(request)
        if response is None:
            response = web.json_response(
                {"error": {"message": "Internal handler error: no response generated", "type": "proxy_internal_error"}},
                status=500,
            )
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response
    except web.HTTPException as ex:
        ex.headers["Access-Control-Allow-Origin"] = "*"
        raise ex
    except Exception as ex:
        err_response = web.json_response({"error": {"message": str(ex), "type": "proxy_internal_error"}}, status=500)
        err_response.headers["Access-Control-Allow-Origin"] = "*"
        return err_response


def create_app():
    app = web.Application(client_max_size=10 * 1024 * 1024, middlewares=[cors_middleware])

    async def on_startup(application):
        connector = aiohttp.TCPConnector(
            limit=100,
            limit_per_host=20,
            keepalive_timeout=60.0,
            enable_cleanup_closed=True,
            force_close=False,
        )
        timeout = aiohttp.ClientTimeout(total=600, connect=10, sock_read=300)
        application[UPSTREAM_SESSION_KEY] = aiohttp.ClientSession(connector=connector, timeout=timeout)

    async def on_cleanup(application):
        session = application.get(UPSTREAM_SESSION_KEY)
        if session and not session.closed:
            await session.close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    # Model router management routes
    app.router.add_get("/v1/routes", handle_routes_get)
    app.router.add_get("/routes", handle_routes_get)
    app.router.add_post("/v1/routes", handle_routes_save)
    app.router.add_post("/routes", handle_routes_save)
    app.router.add_post("/v1/routes/test", handle_routes_test)
    app.router.add_post("/routes/test", handle_routes_test)

    # Raw payload management routes
    app.router.add_get("/v1/raw-log/status", handle_raw_log_status)
    app.router.add_get("/raw-log/status", handle_raw_log_status)
    app.router.add_post("/v1/raw-log/toggle", handle_raw_log_toggle)
    app.router.add_post("/raw-log/toggle", handle_raw_log_toggle)
    app.router.add_get("/v1/raw-log/recent", handle_raw_log_recent)
    app.router.add_get("/raw-log/recent", handle_raw_log_recent)
    app.router.add_post("/v1/raw-log/clear", handle_raw_log_clear)
    app.router.add_get("/v1/raw-log/stream", handle_raw_log_stream)
    app.router.add_get("/raw-log/stream", handle_raw_log_stream)

    app.router.add_route("*", "/v1/{tail:.*}", handle_proxy)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/", handle_health)
    return app


# ── Main Entrypoint ──────────────────────────────────────────────────────────
def main():
    global LISTEN_HOST, LISTEN_PORT, PID_FILE, DAILY_TOKEN_LIMIT
    global MAX_CONCURRENT, SLOT_COOLDOWN_MS, RETRY_429_MAX, _concurrency_limiter

    parser = argparse.ArgumentParser(description="LLM Telemetry Proxy")
    parser.add_argument("--port", type=int, default=LISTEN_PORT, help="Listen port (default 9090)")
    parser.add_argument("--host", type=str, default=LISTEN_HOST, help="Listen host (default 0.0.0.0)")
    parser.add_argument("--upstream", type=str, default=proxy_forwarder.UPSTREAM, help="Upstream API base URL")
    parser.add_argument("--token-limit", type=int, default=DAILY_TOKEN_LIMIT, help="Daily token budget cap (default 480000000)")
    parser.add_argument("--max-concurrent", type=int, default=MAX_CONCURRENT, help="Maximum concurrent upstream requests (default 4)")
    parser.add_argument("--slot-cooldown-ms", type=int, default=SLOT_COOLDOWN_MS, help="Cooldown gap in ms before dispatching next queued request when max concurrency is hit (default 50)")
    parser.add_argument("--retry-429-max", type=int, default=RETRY_429_MAX, help="Max retry attempts on upstream rate limits and transient hiccups (default 3, 0 disables)")
    parser.add_argument("--db", type=str, default=str(telemetry_db.DB_PATH), help="SQLite database file path")
    parser.add_argument("--pid-file", type=str, default=str(PID_FILE), help="PID file path")
    args = parser.parse_args()

    LISTEN_PORT = args.port
    LISTEN_HOST = args.host
    DAILY_TOKEN_LIMIT = args.token_limit
    MAX_CONCURRENT = args.max_concurrent
    SLOT_COOLDOWN_MS = args.slot_cooldown_ms
    RETRY_429_MAX = args.retry_429_max
    PID_FILE = Path(args.pid_file)

    p_db = Path(args.db)
    telemetry_db.DB_PATH = p_db
    payload_inspector.DB_PATH = p_db

    curr_router = proxy_forwarder._model_router or _model_router_inst
    if args.upstream and args.upstream != DEFAULT_UPSTREAM:
        curr_router.default_upstream_url = args.upstream
        proxy_forwarder.UPSTREAM = args.upstream
    elif curr_router.default_upstream_url:
        proxy_forwarder.UPSTREAM = curr_router.default_upstream_url

    _concurrency_limiter.max_concurrent = MAX_CONCURRENT
    _concurrency_limiter.slot_cooldown_seconds = max(0.0, SLOT_COOLDOWN_MS / 1000.0)

    _token_budget.daily_limit = DAILY_TOKEN_LIMIT
    _token_budget.db_path = p_db
    _token_budget._load_state()

    payload_inspector.set_admin_router(curr_router, proxy_forwarder.UPSTREAM)
    proxy_forwarder.set_forwarder_globals(curr_router, proxy_forwarder.UPSTREAM, UPSTREAM_SESSION_KEY, RETRY_429_MAX, _tlog)

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

    # Freezing Python GC permanent generation for static objects (routes, mappings, configs)
    import gc
    if hasattr(gc, "freeze"):
        try:
            gc.freeze()
        except Exception:
            pass

    # Detect high-performance accelerators for status banner
    try:
        from proxy.fast_json import HAS_ORJSON
    except ImportError:
        try:
            from fast_json import HAS_ORJSON
        except ImportError:
            HAS_ORJSON = False

    accel = []
    if _HAS_UVLOOP:
        accel.append("uvloop")
    if HAS_ORJSON:
        accel.append("orjson")
    accel_str = f" [Accelerators: {' + '.join(accel)}]" if accel else ""

    print(f"[telemetry] Proxy starting on {LISTEN_HOST}:{LISTEN_PORT}{accel_str}", file=sys.stderr)
    print(f"[telemetry] Upstream: {proxy_forwarder.UPSTREAM}", file=sys.stderr)
    print(f"[telemetry] Max Concurrent: {MAX_CONCURRENT} (Slot Cooldown: {SLOT_COOLDOWN_MS}ms, 429 Retries: {RETRY_429_MAX})", file=sys.stderr)
    print(f"[telemetry] DB: {telemetry_db.DB_PATH}", file=sys.stderr)
    print(f"[telemetry] PID: {os.getpid()}", file=sys.stderr)
    print(f"[telemetry] Dashboard: Control via Dashboard UI (http://localhost:9118)", file=sys.stderr)
    web.run_app(create_app(), host=LISTEN_HOST, port=LISTEN_PORT, access_log=None)


# ── Transparent Delegation Wrapper for 100% Backward Compatibility ──────────
class _ProxyModule(ModuleType):
    """Dynamically delegates attribute reads/writes to submodules for seamless test monkeypatching."""

    @property
    def _raw_logging_enabled(self):
        return payload_inspector._raw_logging_enabled

    @_raw_logging_enabled.setter
    def _raw_logging_enabled(self, val):
        payload_inspector._raw_logging_enabled = bool(val)

    @property
    def UPSTREAM(self):
        return proxy_forwarder.UPSTREAM

    @UPSTREAM.setter
    def UPSTREAM(self, val):
        proxy_forwarder.UPSTREAM = val
        payload_inspector._default_upstream = val

    @property
    def DB_PATH(self):
        return telemetry_db.DB_PATH

    @DB_PATH.setter
    def DB_PATH(self, val):
        p = Path(val) if val is not None else None
        telemetry_db.DB_PATH = p
        if hasattr(telemetry_db, "_token_budget") and telemetry_db._token_budget:
            telemetry_db._token_budget.db_path = p
        payload_inspector.DB_PATH = p

    @property
    def _model_router(self):
        return proxy_forwarder._model_router

    @_model_router.setter
    def _model_router(self, val):
        proxy_forwarder._model_router = val
        payload_inspector.set_admin_router(val, getattr(proxy_forwarder, "UPSTREAM", DEFAULT_UPSTREAM))

    @property
    def RETRY_429_MAX(self):
        return proxy_forwarder._retry_429_max

    @RETRY_429_MAX.setter
    def RETRY_429_MAX(self, val):
        proxy_forwarder._retry_429_max = int(val)

    @property
    def SLOT_COOLDOWN_MS(self):
        if hasattr(self, "_concurrency_limiter") and self._concurrency_limiter:
            return int(self._concurrency_limiter.slot_cooldown_seconds * 1000)
        return 50

    @SLOT_COOLDOWN_MS.setter
    def SLOT_COOLDOWN_MS(self, val):
        if hasattr(self, "_concurrency_limiter") and self._concurrency_limiter:
            self._concurrency_limiter.slot_cooldown_seconds = float(val) / 1000.0

    @property
    def LOGGER_FILE(self):
        return payload_inspector.LOGGER_FILE

    @LOGGER_FILE.setter
    def LOGGER_FILE(self, val):
        payload_inspector.LOGGER_FILE = Path(val) if val is not None else None

    @property
    def LOGGER_DIR(self):
        return payload_inspector.LOGGER_DIR

    @LOGGER_DIR.setter
    def LOGGER_DIR(self, val):
        payload_inspector.LOGGER_DIR = Path(val) if val is not None else None

    @property
    def _raw_subscribers(self):
        return payload_inspector._raw_subscribers

    @_raw_subscribers.setter
    def _raw_subscribers(self, val):
        payload_inspector._raw_subscribers = val

    @property
    def _load_cache(self):
        return telemetry_db._load_cache

    @_load_cache.setter
    def _load_cache(self, val):
        telemetry_db._load_cache = val


try:
    sys.modules[__name__].__class__ = _ProxyModule
except Exception:
    pass

if __name__ == "__main__":
    main()
