#!/usr/bin/env python3
"""
Telemetry Dashboard Server — standalone visualization layer.

Reads from the SQLite DB that the proxy writes to. Serves the HTML dashboard
and API endpoints for chart data. Has NOTHING to do with proxying LLM calls.

Usage:
    python3 server.py              # serve on port 9118
    python3 server.py --port 8080  # custom port
    python3 server.py --db /path/to/db
"""

import json
import sqlite3
import sys
import os
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from aiohttp import web

try:
    from proxy_manager import ProxyManager
except ImportError:
    from dashboard.proxy_manager import ProxyManager

# ── Config & Path Resolvers ──────────────────────────────────────────────────
STATUS_API = "https://llm.ai.e-infra.cz/status/api/v1/models"
DEFAULT_PORT = 9118

DASHBOARD_DIR = Path(__file__).resolve().parent
REPO_ROOT = DASHBOARD_DIR.parent

def get_dashboard_html_path() -> Path:
    candidates = [
        DASHBOARD_DIR / "dashboard.html",
        REPO_ROOT / "dashboard.html",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]

def get_inspector_html_path() -> Path:
    candidates = [
        DASHBOARD_DIR / "static" / "raw_log_inspector.html",
        DASHBOARD_DIR / "raw_log_inspector.html",
        REPO_ROOT / "raw_log_inspector.html",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]

def get_logger_file_path() -> Path:
    _env_logger = os.environ.get("LOGGER_FILE_PATH")
    if _env_logger:
        return Path(_env_logger)
    return REPO_ROOT / "logger" / "payloads.jsonl"

def get_static_dir_path() -> Path:
    candidates = [
        DASHBOARD_DIR / "static",
        REPO_ROOT / "static",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]

def get_model_mapping_path() -> Path:
    candidates = [
        REPO_ROOT / "data" / "model_mapping.json",
        DASHBOARD_DIR / "data" / "model_mapping.json",
        REPO_ROOT / "model_mapping.json",
        DASHBOARD_DIR / "model_mapping.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]

def get_model_costs_path() -> Path:
    candidates = [
        DASHBOARD_DIR / "model_costs.json",
        REPO_ROOT / "model_costs.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]

# ── Model Aliases Mapping ───────────────────────────────────────────────────
def load_model_mapping() -> dict:
    mapping_path = get_model_mapping_path()
    if mapping_path.exists():
        try:
            with open(mapping_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[server] Error loading model_mapping.json from {mapping_path}: {e}", file=sys.stderr)
    return {}

def get_resolved_model(model_name: str, mapping: dict) -> str:
    if not model_name:
        return model_name
    model_lower = model_name.lower().strip()
    for main_model, aliases in mapping.items():
        if model_lower == main_model.lower().strip():
            return main_model
        for alias in aliases:
            if model_lower == alias.lower().strip():
                return main_model
    return model_name

# ── DB Path Auto-Detection ───────────────────────────────────────────────────
def get_db_path() -> Path:
    # 1. Command line override: --db <path>
    if "--db" in sys.argv:
        try:
            idx = sys.argv.index("--db")
            if idx + 1 < len(sys.argv):
                return Path(sys.argv[idx + 1])
        except ValueError:
            pass

    # 2. Environment variable override
    env_path = os.environ.get("TELEMETRY_DB_PATH")
    if env_path:
        return Path(env_path)

    # 3. Default relative repository paths
    candidates = [
        REPO_ROOT / "data" / "llm_telemetry.db",
        DASHBOARD_DIR / "data" / "llm_telemetry.db",
    ]

    for candidate in candidates:
        try:
            if candidate.exists():
                return candidate
        except Exception:
            pass

    # Default fallback
    return candidates[0]


# ── DB Connection & Performance ──────────────────────────────────────────────
_metadata_cache = {
    "models": None,
    "types": None,
    "last_fetched": 0.0
}

def get_db():
    db_path = get_db_path()
    path_str = str(db_path)
    clean_path = path_str.replace("\\", "/")

    if path_str.startswith("\\\\") or path_str.startswith("//"):
        # It's a Windows UNC path (e.g., \\wsl.localhost\Ubuntu\...)
        stripped = clean_path.lstrip("/")
        uri = f"file:////{stripped}?mode=ro&nolock=1"
    else:
        # Standard local path
        abs_path = db_path.absolute().as_posix()
        if not abs_path.startswith("/"):
            abs_path = "/" + abs_path
        encoded_path = urllib.parse.quote(abs_path)
        uri = f"file:{encoded_path}?mode=ro&nolock=1"

    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA cache_size = -64000")  # 64MB memory cache
        conn.execute("PRAGMA mmap_size = 268435456")  # Memory map up to 256MB
        conn.execute("PRAGMA query_only = ON")
    except Exception:
        pass
    return conn


# ── CORS Middleware ─────────────────────────────────────────────────────────
@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        response = web.Response(status=200)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With"
        return response

    try:
        response = await handler(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response
    except web.HTTPException as ex:
        ex.headers["Access-Control-Allow-Origin"] = "*"
        raise ex
    except Exception as ex:
        err_response = web.json_response({"error": str(ex)}, status=500)
        err_response.headers["Access-Control-Allow-Origin"] = "*"
        return err_response


# ── API Endpoints ───────────────────────────────────────────────────────────

async def handle_query(request: web.Request) -> web.Response:
    """GET /api/query — flexible query with filters."""
    try:
        conn = get_db()
    except Exception as e:
        return web.json_response({
            "error": "Database connection failed",
            "details": str(e)
        }, status=503)

    mapping = load_model_mapping()

    try:
        where_parts = []
        params = []

        models = request.query.getall("model", None)
        if models:
            expanded_models = []
            for m in models:
                expanded_models.append(m)
                m_lower = m.lower().strip()
                for main_model, aliases in mapping.items():
                    if m_lower == main_model.lower().strip() or any(m_lower == a.lower().strip() for a in aliases):
                        expanded_models.append(main_model)
                        expanded_models.extend(aliases)
            expanded_models = list(set(expanded_models))
            
            placeholders = ",".join("?" for _ in expanded_models)
            where_parts.append(f"model IN ({placeholders})")
            params.extend(expanded_models)

        call_types = request.query.getall("call_type", None)
        if call_types:
            placeholders = ",".join("?" * len(call_types))
            where_parts.append(f"call_type IN ({placeholders})")
            params.extend(call_types)

        from_ts = request.query.get("from")
        if from_ts:
            where_parts.append("timestamp >= ?")
            params.append(from_ts)

        to_ts = request.query.get("to")
        if to_ts:
            where_parts.append("timestamp <= ?")
            params.append(to_ts)

        errors_only = request.query.get("errors_only")
        if errors_only and errors_only.lower() in ("1", "true", "yes"):
            where_parts.append("error IS NOT NULL")

        where_clause = " AND ".join(where_parts) if where_parts else "1=1"
        group_by = request.query.get("group_by")
        limit = int(request.query.get("limit", 1000))

        result = {}

        if group_by == "model":
            rows = conn.execute(f"""
                SELECT model, SUM(COALESCE(calls_count, 1)) as calls,
                       SUM(input_tokens) as total_input,
                       SUM(output_tokens) as total_output,
                       SUM(ttfb_ms * COALESCE(calls_count, 1)) as sum_ttfb,
                       SUM(CASE WHEN ttfb_ms IS NOT NULL THEN COALESCE(calls_count, 1) ELSE 0 END) as ttfb_count,
                       MAX(ttfb_ms) as max_ttfb,
                       SUM(total_ms * COALESCE(calls_count, 1)) as sum_rtt,
                       SUM(CASE WHEN total_ms IS NOT NULL THEN COALESCE(calls_count, 1) ELSE 0 END) as rtt_count,
                       MAX(total_ms) as max_rtt,
                       SUM(CASE WHEN output_tokens > 0 AND total_ms > 0 THEN output_tokens ELSE 0 END) as sum_output_for_tps,
                       SUM(CASE WHEN output_tokens > 0 AND total_ms > 0 THEN total_ms * COALESCE(calls_count, 1) ELSE 0 END) as sum_total_ms_for_tps,
                       SUM(server_running * COALESCE(calls_count, 1)) as sum_load,
                       SUM(CASE WHEN server_running IS NOT NULL THEN COALESCE(calls_count, 1) ELSE 0 END) as load_count,
                       SUM(CASE WHEN error IS NOT NULL THEN COALESCE(calls_count, 1) ELSE 0 END) as errors
                FROM api_calls WHERE {where_clause}
                GROUP BY model ORDER BY calls DESC
            """, params).fetchall()
            
            grouped_res = {}
            for r in rows:
                resolved = get_resolved_model(r["model"], mapping) or "?"
                if resolved not in grouped_res:
                    grouped_res[resolved] = {
                        "model": resolved,
                        "calls": 0,
                        "total_input": 0,
                        "total_output": 0,
                        "sum_ttfb": 0.0,
                        "ttfb_count": 0,
                        "max_ttfb": 0.0,
                        "sum_rtt": 0.0,
                        "rtt_count": 0,
                        "max_rtt": 0.0,
                        "sum_output_for_tps": 0,
                        "sum_total_ms_for_tps": 0.0,
                        "sum_load": 0.0,
                        "load_count": 0,
                        "errors": 0
                    }
                g = grouped_res[resolved]
                g["calls"] += r["calls"]
                g["total_input"] += r["total_input"] if r["total_input"] else 0
                g["total_output"] += r["total_output"] if r["total_output"] else 0
                
                if r["sum_ttfb"]:
                    g["sum_ttfb"] += r["sum_ttfb"]
                    g["ttfb_count"] += r["ttfb_count"]
                if r["max_ttfb"] and r["max_ttfb"] > g["max_ttfb"]:
                    g["max_ttfb"] = r["max_ttfb"]
                    
                if r["sum_rtt"]:
                    g["sum_rtt"] += r["sum_rtt"]
                    g["rtt_count"] += r["rtt_count"]
                if r["max_rtt"] and r["max_rtt"] > g["max_rtt"]:
                    g["max_rtt"] = r["max_rtt"]
                    
                if r["sum_output_for_tps"]:
                    g["sum_output_for_tps"] += r["sum_output_for_tps"]
                if r["sum_total_ms_for_tps"]:
                    g["sum_total_ms_for_tps"] += r["sum_total_ms_for_tps"]
                    
                if r["sum_load"]:
                    g["sum_load"] += r["sum_load"]
                    g["load_count"] += r["load_count"]
                    
                g["errors"] += r["errors"] if r["errors"] else 0
                
            groups = []
            for resolved, g in grouped_res.items():
                groups.append({
                    "model": resolved,
                    "calls": g["calls"],
                    "total_input": g["total_input"],
                    "total_output": g["total_output"],
                    "avg_ttfb": round(g["sum_ttfb"] / g["ttfb_count"], 2) if g["ttfb_count"] > 0 else 0,
                    "max_ttfb": g["max_ttfb"],
                    "avg_rtt": round(g["sum_rtt"] / g["rtt_count"], 2) if g["rtt_count"] > 0 else 0,
                    "max_rtt": g["max_rtt"],
                    "avg_tps": round(g["sum_output_for_tps"] / (g["sum_total_ms_for_tps"] / 1000.0), 2) if g["sum_total_ms_for_tps"] > 0 else None,
                    "avg_load": round(g["sum_load"] / g["load_count"], 2) if g["load_count"] > 0 else None,
                    "errors": g["errors"]
                })
            groups.sort(key=lambda x: x["total_input"], reverse=True)
            result["groups"] = groups

        elif group_by == "hour":
            rows = conn.execute(f"""
                SELECT strftime('%Y-%m-%dT%H:00:00', timestamp) as hour,
                       SUM(COALESCE(calls_count, 1)) as calls,
                       SUM(input_tokens) as total_input,
                       SUM(output_tokens) as total_output,
                       SUM(ttfb_ms * COALESCE(calls_count, 1)) / NULLIF(SUM(CASE WHEN ttfb_ms IS NOT NULL THEN COALESCE(calls_count, 1) ELSE 0 END), 0) as avg_ttfb, 
                       SUM(total_ms * COALESCE(calls_count, 1)) / NULLIF(SUM(CASE WHEN total_ms IS NOT NULL THEN COALESCE(calls_count, 1) ELSE 0 END), 0) as avg_rtt,
                       SUM(CASE WHEN output_tokens > 0 AND total_ms > 0 THEN output_tokens ELSE 0 END) / NULLIF(SUM(CASE WHEN output_tokens > 0 AND total_ms > 0 THEN total_ms * COALESCE(calls_count, 1) ELSE 0 END) / 1000.0, 0) as avg_tps,
                       SUM(server_running * COALESCE(calls_count, 1)) / NULLIF(SUM(CASE WHEN server_running IS NOT NULL THEN COALESCE(calls_count, 1) ELSE 0 END), 0) as avg_load,
                       SUM(CASE WHEN error IS NOT NULL THEN COALESCE(calls_count, 1) ELSE 0 END) as errors
                FROM api_calls WHERE {where_clause}
                GROUP BY hour ORDER BY hour
            """, params).fetchall()
            result["groups"] = [dict(r) for r in rows]

        elif group_by == "day":
            rows = conn.execute(f"""
                SELECT strftime('%Y-%m-%d', timestamp) as day,
                       SUM(COALESCE(calls_count, 1)) as calls,
                       SUM(input_tokens) as total_input,
                       SUM(output_tokens) as total_output,
                       SUM(ttfb_ms * COALESCE(calls_count, 1)) / NULLIF(SUM(CASE WHEN ttfb_ms IS NOT NULL THEN COALESCE(calls_count, 1) ELSE 0 END), 0) as avg_ttfb, 
                       SUM(total_ms * COALESCE(calls_count, 1)) / NULLIF(SUM(CASE WHEN total_ms IS NOT NULL THEN COALESCE(calls_count, 1) ELSE 0 END), 0) as avg_rtt,
                       SUM(CASE WHEN output_tokens > 0 AND total_ms > 0 THEN output_tokens ELSE 0 END) / NULLIF(SUM(CASE WHEN output_tokens > 0 AND total_ms > 0 THEN total_ms * COALESCE(calls_count, 1) ELSE 0 END) / 1000.0, 0) as avg_tps,
                       SUM(server_running * COALESCE(calls_count, 1)) / NULLIF(SUM(CASE WHEN server_running IS NOT NULL THEN COALESCE(calls_count, 1) ELSE 0 END), 0) as avg_load,
                       SUM(CASE WHEN error IS NOT NULL THEN COALESCE(calls_count, 1) ELSE 0 END) as errors
                FROM api_calls WHERE {where_clause}
                GROUP BY day ORDER BY day
            """, params).fetchall()
            result["groups"] = [dict(r) for r in rows]

        elif group_by == "call_type":
            rows = conn.execute(f"""
                SELECT call_type, SUM(COALESCE(calls_count, 1)) as calls,
                       SUM(input_tokens) as total_input,
                       SUM(output_tokens) as total_output,
                       SUM(total_ms * COALESCE(calls_count, 1)) / NULLIF(SUM(CASE WHEN total_ms IS NOT NULL THEN COALESCE(calls_count, 1) ELSE 0 END), 0) as avg_rtt
                FROM api_calls WHERE {where_clause}
                GROUP BY call_type ORDER BY calls DESC
            """, params).fetchall()
            result["groups"] = [dict(r) for r in rows]

        else:
            rows = conn.execute(f"""
                SELECT * FROM api_calls WHERE {where_clause}
                ORDER BY id DESC LIMIT ?
            """, params + [limit]).fetchall()
            
            calls = []
            for r in rows:
                d = dict(r)
                d["model"] = get_resolved_model(d["model"], mapping)
                calls.append(d)
            result["calls"] = calls

        summary = conn.execute(f"""
            SELECT SUM(COALESCE(calls_count, 1)) as calls,
                   COALESCE(SUM(input_tokens), 0) as total_input,
                   COALESCE(SUM(output_tokens), 0) as total_output,
                   COALESCE(SUM(ttfb_ms * COALESCE(calls_count, 1)) / NULLIF(SUM(CASE WHEN ttfb_ms IS NOT NULL THEN COALESCE(calls_count, 1) ELSE 0 END), 0), 0) as avg_ttfb,
                   COALESCE(SUM(total_ms * COALESCE(calls_count, 1)) / NULLIF(SUM(CASE WHEN total_ms IS NOT NULL THEN COALESCE(calls_count, 1) ELSE 0 END), 0), 0) as avg_rtt,
                   COALESCE(SUM(CASE WHEN output_tokens > 0 AND total_ms > 0 THEN output_tokens ELSE 0 END) / NULLIF(SUM(CASE WHEN output_tokens > 0 AND total_ms > 0 THEN total_ms * COALESCE(calls_count, 1) ELSE 0 END) / 1000.0, 0), 0) as avg_tps,
                   SUM(CASE WHEN error IS NOT NULL THEN COALESCE(calls_count, 1) ELSE 0 END) as errors,
                   MIN(timestamp) as first_call,
                   MAX(timestamp) as last_call
            FROM api_calls WHERE {where_clause}
        """, params).fetchone()
        result["summary"] = dict(summary)

        # Available models and types (cached in-memory for 30 seconds)
        now_ts = datetime.now().timestamp()
        if _metadata_cache["models"] is None or (now_ts - _metadata_cache["last_fetched"] > 30.0):
            try:
                models_avail = conn.execute(
                    "SELECT DISTINCT model FROM api_calls WHERE model IS NOT NULL ORDER BY model"
                ).fetchall()
                mapped_models = [get_resolved_model(r["model"], mapping) for r in models_avail]
                _metadata_cache["models"] = sorted(list(set(mapped_models)))

                types_avail = conn.execute(
                    "SELECT DISTINCT call_type FROM api_calls ORDER BY call_type"
                ).fetchall()
                _metadata_cache["types"] = [r["call_type"] for r in types_avail]
                _metadata_cache["last_fetched"] = now_ts
            except Exception:
                pass

        result["available_models"] = _metadata_cache["models"] or []
        result["available_types"] = _metadata_cache["types"] or []

        # Proxy stats for cross-checking
        try:
            proxy_stats = conn.execute(f"""
                SELECT
                    SUM(COALESCE(calls_count, 1)) as total_calls,
                    SUM(CASE WHEN error IS NOT NULL THEN COALESCE(calls_count, 1) ELSE 0 END) as total_errors,
                    SUM(CASE WHEN logged = 1 THEN COALESCE(calls_count, 1) ELSE 0 END) as logged_calls,
                    SUM(CASE WHEN logged = 0 THEN COALESCE(calls_count, 1) ELSE 0 END) as unlogged_calls,
                    MIN(timestamp) as started_at,
                    MAX(timestamp) as last_call
                FROM proxy_calls WHERE {where_clause}
            """, params).fetchone()
            result["proxy_stats"] = dict(proxy_stats) if proxy_stats else None

            try:
                type_breakdown = conn.execute(f"""
                    SELECT call_type,
                           SUM(COALESCE(calls_count, 1)) as calls,
                           SUM(CASE WHEN error IS NOT NULL THEN COALESCE(calls_count, 1) ELSE 0 END) as errors,
                           SUM(CASE WHEN logged = 1 THEN COALESCE(calls_count, 1) ELSE 0 END) as logged
                    FROM proxy_calls WHERE {where_clause} GROUP BY call_type ORDER BY calls DESC
                """, params).fetchall()
                result["proxy_breakdown"] = [dict(r) for r in type_breakdown]
            except sqlite3.OperationalError:
                result["proxy_breakdown"] = []

        except sqlite3.OperationalError:
            result["proxy_stats"] = None
            result["proxy_breakdown"] = []

        return web.json_response(result)
    except Exception as e:
        return web.json_response({
            "error": "Query execution failed",
            "details": str(e)
        }, status=500)
    finally:
        conn.close()


async def handle_server_status(request: web.Request) -> web.Response:
    """GET /api/server-status — live e-INFRA server status."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(STATUS_API, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                raw = await resp.json()

        models = []
        for m in raw:
            if m.get("status") not in ("online",):
                continue
            name = m.get("model_name") or m.get("container", "?")
            latest = m.get("latest", {})
            if isinstance(latest.get("num_requests_running"), dict):
                running, tok_s = 0, 0.0
            else:
                running = latest.get("num_requests_running", 0)
                tok_s = latest.get("generation_tokens_rate", 0.0)
            models.append({
                "name": name,
                "status": m.get("status", "unknown"),
                "running": running,
                "tokens_per_s": tok_s,
                "kv_cache": latest.get("kv_cache_usage_perc", 0),
                "waiting": latest.get("num_requests_waiting", 0),
                "first_seen": m.get("first_seen", "?"),
                "last_seen": m.get("last_seen", "?"),
            })

        return web.json_response({"models": models, "fetched_at": datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=502)


async def handle_health(request: web.Request) -> web.Response:
    db_path = get_db_path()
    db_exists = db_path.exists()
    dashboard_html = get_dashboard_html_path()
    try:
        db_size = db_path.stat().st_size if db_exists else 0
    except Exception:
        db_size = 0
    return web.json_response({
        "status": "ok",
        "db_path": str(db_path),
        "db_exists": db_exists,
        "db_size_mb": round(db_size / 1024 / 1024, 1) if db_exists else 0,
        "dashboard_html": dashboard_html.exists(),
    })


async def handle_costs(request: web.Request) -> web.Response:
    """GET /api/costs — retrieves the model costs configuration."""
    costs_path = get_model_costs_path()
    if costs_path.exists():
        try:
            with open(costs_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return web.json_response(data)
        except Exception as e:
            return web.json_response({"error": f"Failed to parse model_costs.json: {str(e)}"}, status=500)
    else:
        fallback = {
            "deepseek": {"input_cost_per_million": 0.14, "output_cost_per_million": 0.28, "provider_source": "DeepSeek API (Official)", "last_updated": "2026-07-04"},
            "gemma-4": {"input_cost_per_million": 0.07, "output_cost_per_million": 0.27, "provider_source": "Google AI Studio", "last_updated": "2026-07-04"},
            "glm-5.2": {"input_cost_per_million": 1.40, "output_cost_per_million": 4.40, "provider_source": "Zhipu AI Developer Platform", "last_updated": "2026-07-04"},
            "gpt-oss-120b": {"input_cost_per_million": 0.60, "output_cost_per_million": 0.60, "provider_source": "Together AI (Hosted)", "last_updated": "2026-07-04"},
            "qwen3-embedding-4b": {"input_cost_per_million": 0.01, "output_cost_per_million": 0.00, "provider_source": "Alibaba Cloud Model Studio", "last_updated": "2026-07-04"},
            "qwen3.5-int4": {"input_cost_per_million": 0.05, "output_cost_per_million": 0.10, "provider_source": "Alibaba Cloud / self-hosted", "last_updated": "2026-07-04"}
        }
        return web.json_response(fallback)


async def handle_dashboard(request: web.Request) -> web.Response:
    dashboard_html = get_dashboard_html_path()
    if dashboard_html.exists():
        return web.FileResponse(dashboard_html)
    return web.Response(text="Dashboard file not found at " + str(dashboard_html), status=404)


# ── Proxy Management Endpoints ───────────────────────────────────────────────
async def handle_proxy_status(request: web.Request) -> web.Response:
    """GET /api/proxy/status — get proxy running state, health, port, upstream, etc."""
    port_str = request.query.get("port")
    port = int(port_str) if port_str and port_str.isdigit() else None
    status = await ProxyManager.get_status(port=port)
    return web.json_response(status)


async def handle_proxy_start(request: web.Request) -> web.Response:
    """POST /api/proxy/start — start the proxy background process."""
    try:
        data = await request.json() if request.can_read_body else {}
    except Exception:
        data = {}
    port = int(data.get("port", 9090))
    host = data.get("host", "0.0.0.0")
    upstream = data.get("upstream", "https://llm.ai.e-infra.cz/v1")
    db_path = get_db_path()
    res = await ProxyManager.start(port=port, host=host, upstream=upstream, db_path=db_path)
    status_code = 200 if res.get("success") else 500
    return web.json_response(res, status=status_code)


async def handle_proxy_stop(request: web.Request) -> web.Response:
    """POST /api/proxy/stop — stop / kill the proxy process."""
    try:
        data = await request.json() if request.can_read_body else {}
    except Exception:
        data = {}
    force = bool(data.get("force", False))
    res = await ProxyManager.stop(force=force)
    return web.json_response(res)


async def handle_proxy_restart(request: web.Request) -> web.Response:
    """POST /api/proxy/restart — restart the proxy process."""
    try:
        data = await request.json() if request.can_read_body else {}
    except Exception:
        data = {}
    port = int(data.get("port", 9090))
    host = data.get("host", "0.0.0.0")
    upstream = data.get("upstream", "https://llm.ai.e-infra.cz/v1")
    db_path = get_db_path()
    res = await ProxyManager.restart(port=port, host=host, upstream=upstream, db_path=db_path)
    status_code = 200 if res.get("success") else 500
    return web.json_response(res, status=status_code)


async def handle_proxy_logs(request: web.Request) -> web.Response:
    """GET /api/proxy/logs — view recent proxy log output."""
    lines_str = request.query.get("lines", "150")
    lines = int(lines_str) if lines_str.isdigit() else 150
    logs_data = ProxyManager.get_logs(lines=lines)
    return web.json_response(logs_data)


async def handle_proxy_clear_logs(request: web.Request) -> web.Response:
    """POST /api/proxy/clear-logs — clear proxy log output file."""
    res = ProxyManager.clear_logs()
    return web.json_response(res)


async def handle_db_compress(request: web.Request) -> web.Response:
    """POST /api/db/compress — run db_compress.py maintenance script."""
    res = await ProxyManager.run_db_compress()
    status_code = 200 if res.get("success") else 500
    return web.json_response(res, status=status_code)


# ── Raw Payload Log & Inspector Endpoints ────────────────────────────────────
async def handle_inspector(request: web.Request) -> web.Response:
    """GET /inspector and /raw-logs — serve the standalone inspector UI."""
    inspector_html = get_inspector_html_path()
    if inspector_html.exists():
        return web.FileResponse(inspector_html)
    return web.Response(text="Inspector HTML not found at " + str(inspector_html), status=404)


async def handle_raw_log_status(request: web.Request) -> web.Response:
    """GET /api/raw-log/status — get raw payload logging state, file size, proxy connection."""
    logger_file = get_logger_file_path()
    file_exists = logger_file.exists()
    size = logger_file.stat().st_size if file_exists else 0

    proxy_status = None
    proxy_port = 9090
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1.0)) as session:
            async with session.get(f"http://127.0.0.1:{proxy_port}/v1/raw-log/status") as resp:
                if resp.status == 200:
                    proxy_status = await resp.json()
    except Exception:
        pass

    enabled = proxy_status.get("enabled", False) if proxy_status else False

    def fmt_sz(b):
        if b < 1024:
            return f"{b} B"
        elif b < 1024 * 1024:
            return f"{b / 1024:.1f} KB"
        return f"{b / (1024 * 1024):.2f} MB"

    return web.json_response({
        "enabled": enabled,
        "proxy_alive": proxy_status is not None,
        "file_path": str(logger_file),
        "rel_path": str(logger_file.relative_to(REPO_ROOT)) if logger_file.is_relative_to(REPO_ROOT) else str(logger_file),
        "file_size_bytes": size,
        "file_size_formatted": fmt_sz(size),
        "proxy_details": proxy_status,
    })


async def handle_raw_log_toggle(request: web.Request) -> web.Response:
    """POST /api/raw-log/toggle — toggle raw logging ON / OFF via proxy."""
    try:
        data = await request.json() if request.can_read_body else {}
    except Exception:
        data = {}

    proxy_port = 9090
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2.0)) as session:
            async with session.post(f"http://127.0.0.1:{proxy_port}/v1/raw-log/toggle", json=data) as resp:
                if resp.status == 200:
                    res_data = await resp.json()
                    return web.json_response(res_data)
                return web.json_response({"success": False, "error": f"Proxy returned status {resp.status}"}, status=502)
    except Exception as e:
        return web.json_response({"success": False, "error": f"Proxy unreachable: {str(e)}"}, status=503)


async def handle_raw_log_recent(request: web.Request) -> web.Response:
    """GET /api/raw-log/recent — retrieve the last N lines from the logger file."""
    limit_str = request.query.get("limit", "50")
    try:
        limit = max(1, min(500, int(limit_str)))
    except ValueError:
        limit = 50

    logger_file = get_logger_file_path()
    if not logger_file.exists():
        return web.json_response({"entries": [], "total_count": 0})

    try:
        entries = []
        with open(logger_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line_str = line.strip()
                if line_str:
                    try:
                        entries.append(json.loads(line_str))
                    except Exception:
                        pass
        total_count = len(entries)
        recent_entries = entries[-limit:]
        return web.json_response({"entries": list(reversed(recent_entries)), "total_count": total_count})
    except Exception as e:
        return web.json_response({"error": str(e), "entries": []}, status=500)


async def handle_raw_log_clear(request: web.Request) -> web.Response:
    """POST /api/raw-log/clear — truncate the logger file."""
    logger_file = get_logger_file_path()
    try:
        if logger_file.exists():
            with open(logger_file, "w", encoding="utf-8") as f:
                f.truncate(0)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1.0)) as session:
                await session.post("http://127.0.0.1:9090/v1/raw-log/clear")
        except Exception:
            pass
        return web.json_response({"success": True, "message": "Logger file cleared successfully."})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_raw_log_stream(request: web.Request) -> web.StreamResponse:
    """GET /api/raw-log/stream — Server-Sent Events (SSE) bridge to proxy stream."""
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

    proxy_url = "http://127.0.0.1:9090/v1/raw-log/stream"
    try:
        timeout = aiohttp.ClientTimeout(total=None)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(proxy_url) as proxy_resp:
                if proxy_resp.status != 200:
                    await response.write(b"event: error\ndata: {\"error\": \"Proxy SSE unavailable\"}\n\n")
                    return response
                async for chunk in proxy_resp.content:
                    await response.write(chunk)
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    except Exception as e:
        try:
            err_json = json.dumps({"error": str(e)})
            await response.write(f"event: error\ndata: {err_json}\n\n".encode("utf-8"))
        except Exception:
            pass
    return response


# ── App ──────────────────────────────────────────────────────────────────────
def create_app():
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/api/query", handle_query)
    app.router.add_get("/api/server-status", handle_server_status)
    app.router.add_get("/api/stats", handle_query)  # alias
    app.router.add_get("/api/costs", handle_costs)
    app.router.add_get("/health", handle_health)
    
    # Proxy lifecycle routes
    app.router.add_get("/api/proxy/status", handle_proxy_status)
    app.router.add_post("/api/proxy/start", handle_proxy_start)
    app.router.add_post("/api/proxy/stop", handle_proxy_stop)
    app.router.add_post("/api/proxy/restart", handle_proxy_restart)
    app.router.add_get("/api/proxy/logs", handle_proxy_logs)
    app.router.add_post("/api/proxy/clear-logs", handle_proxy_clear_logs)
    app.router.add_post("/api/db/compress", handle_db_compress)

    # Raw Payload Log & Inspector routes
    app.router.add_get("/api/raw-log/status", handle_raw_log_status)
    app.router.add_post("/api/raw-log/toggle", handle_raw_log_toggle)
    app.router.add_get("/api/raw-log/recent", handle_raw_log_recent)
    app.router.add_post("/api/raw-log/clear", handle_raw_log_clear)
    app.router.add_get("/api/raw-log/stream", handle_raw_log_stream)

    app.router.add_get("/", handle_dashboard)
    app.router.add_get("/dashboard", handle_dashboard)
    app.router.add_get("/inspector", handle_inspector)
    app.router.add_get("/raw-logs", handle_inspector)

    static_dir = get_static_dir_path()
    static_dir.mkdir(exist_ok=True)
    app.router.add_static("/static/", path=static_dir, name="static")

    return app


def main():
    port = DEFAULT_PORT
    if "--port" in sys.argv:
        try:
            idx = sys.argv.index("--port")
            if idx + 1 < len(sys.argv):
                port = int(sys.argv[idx + 1])
        except ValueError:
            pass

    db_path = get_db_path()
    dashboard_html = get_dashboard_html_path()

    print(f"[dashboard] Server starting on http://localhost:{port}", file=sys.stderr)
    print(f"[dashboard] DB: {db_path}", file=sys.stderr)
    print(f"[dashboard] HTML: {dashboard_html}", file=sys.stderr)
    web.run_app(create_app(), host="127.0.0.1", port=port, access_log=None)


if __name__ == "__main__":
    main()

