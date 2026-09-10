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
import uuid
from typing import Any, Optional, Dict
from datetime import datetime, timezone
from pathlib import Path

import gzip
import asyncio
import aiohttp
from aiohttp import web

DASHBOARD_DIR = Path(__file__).resolve().parent
REPO_ROOT = DASHBOARD_DIR.parent

# Ensure proxy module can be imported
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "proxy") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "proxy"))

try:
    from proxy.model_router import ModelRouter
except ImportError:
    try:
        from model_router import ModelRouter
    except ImportError:
        ModelRouter = None

try:
    from proxy_manager import ProxyManager
except ImportError:
    from dashboard.proxy_manager import ProxyManager

# ── Config & Path Resolvers ──────────────────────────────────────────────────
STATUS_API = "https://llm.ai.e-infra.cz/status/api/v1/models"
DEFAULT_PORT = 9118

# SVG Favicon icon handler
FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="6" fill="#0d1117"/><path d="M7 16h4l3-8 4 16 3-8h4" fill="none" stroke="#58a6ff" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg>"""

async def handle_favicon(request: web.Request) -> web.Response:
    """Serve embedded SVG favicon."""
    return web.Response(
        body=FAVICON_SVG.encode("utf-8"),
        content_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"}
    )

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
        REPO_ROOT / "data" / "model_costs.json",
        DASHBOARD_DIR / "data" / "model_costs.json",
        REPO_ROOT / "model_costs.json",
        DASHBOARD_DIR / "model_costs.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]

def get_routes_config_path() -> Path:
    candidates = [
        REPO_ROOT / "data" / "model_routes.json",
        DASHBOARD_DIR / "data" / "model_routes.json",
        REPO_ROOT / "model_routes.json",
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

def get_resolved_model(model_name: str, mapping: dict, timestamp: str = None) -> str:
    if not model_name or not mapping:
        return model_name
    m_lower = str(model_name).lower().strip()
    
    target = mapping.get(m_lower)
    if target is None:
        for k, v in mapping.items():
            if k.lower().strip() == m_lower:
                target = v
                break
                
    if target is None:
        return model_name
        
    if isinstance(target, str):
        return target
        
    if isinstance(target, dict):
        sorted_dates = sorted(target.keys())
        if not sorted_dates:
            return model_name
        if not timestamp:
            return target[sorted_dates[-1]]
            
        ts_date = str(timestamp)[:10]
        matched_date = sorted_dates[0]
        for d in sorted_dates:
            if d <= ts_date:
                matched_date = d
            else:
                break
        return target[matched_date]
        
    if isinstance(target, list):
        return target[0] if target else model_name
        
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

def get_db_fingerprint() -> str:
    db_path = get_db_path()
    if not db_path.exists():
        return "none"
    try:
        conn = get_db()
        try:
            cur = conn.execute("SELECT value FROM _telemetry_meta WHERE key = 'db_instance_id'")
            row = cur.fetchone()
            inst_id = row[0] if row else None
            
            cur2 = conn.execute("SELECT value FROM _telemetry_meta WHERE key = 'compaction_version'")
            row2 = cur2.fetchone()
            comp_ver = row2[0] if row2 else "1"
            
            if inst_id:
                return f"{inst_id}_v{comp_ver}"
        except Exception:
            pass
        finally:
            conn.close()
    except Exception:
        pass

    # Fallback: if _telemetry_meta is not initialized yet, seed it in a write transaction
    try:
        conn_w = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            conn_w.execute("""
                CREATE TABLE IF NOT EXISTS _telemetry_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            inst_id = uuid.uuid4().hex
            conn_w.execute("INSERT OR IGNORE INTO _telemetry_meta (key, value) VALUES ('db_instance_id', ?)", (inst_id,))
            conn_w.execute("INSERT OR IGNORE INTO _telemetry_meta (key, value) VALUES ('compaction_version', '1')")
            conn_w.commit()
            return f"{inst_id}_v1"
        finally:
            conn_w.close()
    except Exception:
        return "default_instance_v1"


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
        conn.execute("PRAGMA cache_size = -128000")  # 128MB memory cache
        conn.execute("PRAGMA mmap_size = 536870912")  # Memory map up to 512MB
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA temp_store = MEMORY")
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
                for alias_key, target in mapping.items():
                    alias_lower = alias_key.lower().strip()
                    if m_lower == alias_lower:
                        if isinstance(target, str):
                            expanded_models.append(target)
                        elif isinstance(target, dict):
                            expanded_models.extend(target.values())
                        elif isinstance(target, list):
                            expanded_models.extend(target)
                    elif isinstance(target, str) and m_lower == target.lower().strip():
                        expanded_models.append(alias_key)
                    elif isinstance(target, dict) and any(m_lower == str(v).lower().strip() for v in target.values()):
                        expanded_models.append(alias_key)
                    elif isinstance(target, list) and any(m_lower == str(v).lower().strip() for v in target):
                        expanded_models.append(alias_key)
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
            where_parts.append("((error IS NOT NULL AND error != '') OR (status_code IS NOT NULL AND (status_code < 200 OR status_code >= 300)))")

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
                       SUM(CASE WHEN (error IS NOT NULL AND error != '') OR (status_code IS NOT NULL AND (status_code < 200 OR status_code >= 300)) THEN COALESCE(calls_count, 1) ELSE 0 END) as errors
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
                
                if r["ttfb_count"]:
                    g["sum_ttfb"] += (r["sum_ttfb"] or 0.0)
                    g["ttfb_count"] += r["ttfb_count"]
                if r["max_ttfb"] and r["max_ttfb"] > g["max_ttfb"]:
                    g["max_ttfb"] = r["max_ttfb"]
                    
                if r["rtt_count"]:
                    g["sum_rtt"] += (r["sum_rtt"] or 0.0)
                    g["rtt_count"] += r["rtt_count"]
                if r["max_rtt"] and r["max_rtt"] > g["max_rtt"]:
                    g["max_rtt"] = r["max_rtt"]
                    
                if r["sum_output_for_tps"]:
                    g["sum_output_for_tps"] += r["sum_output_for_tps"]
                if r["sum_total_ms_for_tps"]:
                    g["sum_total_ms_for_tps"] += r["sum_total_ms_for_tps"]
                    
                if r["load_count"]:
                    g["sum_load"] += (r["sum_load"] or 0.0)
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
                       SUM(CASE WHEN (error IS NOT NULL AND error != '') OR (status_code IS NOT NULL AND (status_code < 200 OR status_code >= 300)) THEN COALESCE(calls_count, 1) ELSE 0 END) as errors
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
                       SUM(CASE WHEN (error IS NOT NULL AND error != '') OR (status_code IS NOT NULL AND (status_code < 200 OR status_code >= 300)) THEN COALESCE(calls_count, 1) ELSE 0 END) as errors
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
            
            http_err_map = {
                400: "HTTP 400 Bad Request",
                401: "HTTP 401 Unauthorized",
                403: "HTTP 403 Forbidden",
                404: "HTTP 404 Not Found",
                408: "HTTP 408 Request Timeout",
                429: "HTTP 429 Rate Limit Exceeded",
                500: "HTTP 500 Internal Server Error",
                502: "HTTP 502 Bad Gateway",
                503: "HTTP 503 Service Unavailable",
                504: "HTTP 504 Gateway Timeout"
            }
            calls = []
            router = ModelRouter(config_path=get_routes_config_path()) if ModelRouter else None
            for r in rows:
                d = dict(r)
                raw_model = d.get("model")
                d["model"] = get_resolved_model(raw_model, mapping, d.get("timestamp"))
                if not d.get("route_name") and router and raw_model:
                    res = router.resolve(raw_model)
                    if res:
                        d["route_name"] = res.route_name
                        d["upstream_url"] = res.upstream_url
                if not d.get("error") and d.get("status_code") and (d["status_code"] < 200 or d["status_code"] >= 300):
                    d["error"] = http_err_map.get(d["status_code"], f"HTTP {d['status_code']}")
                calls.append(d)
            result["calls"] = calls

        summary = conn.execute(f"""
            SELECT SUM(COALESCE(calls_count, 1)) as calls,
                   COALESCE(SUM(input_tokens), 0) as total_input,
                   COALESCE(SUM(output_tokens), 0) as total_output,
                   COALESCE(SUM(ttfb_ms * COALESCE(calls_count, 1)) / NULLIF(SUM(CASE WHEN ttfb_ms IS NOT NULL THEN COALESCE(calls_count, 1) ELSE 0 END), 0), 0) as avg_ttfb,
                   COALESCE(SUM(total_ms * COALESCE(calls_count, 1)) / NULLIF(SUM(CASE WHEN total_ms IS NOT NULL THEN COALESCE(calls_count, 1) ELSE 0 END), 0), 0) as avg_rtt,
                   COALESCE(SUM(CASE WHEN output_tokens > 0 AND total_ms > 0 THEN output_tokens ELSE 0 END) / NULLIF(SUM(CASE WHEN output_tokens > 0 AND total_ms > 0 THEN total_ms * COALESCE(calls_count, 1) ELSE 0 END) / 1000.0, 0), 0) as avg_tps,
                   SUM(CASE WHEN (error IS NOT NULL AND error != '') OR (status_code IS NOT NULL AND (status_code < 200 OR status_code >= 300)) THEN COALESCE(calls_count, 1) ELSE 0 END) as errors,
                   COALESCE(SUM(calls_count), 0) as total_calls_tracked
            FROM api_calls WHERE {where_clause}
        """, params).fetchone()
        result["summary"] = dict(summary) if summary else {}

        # Available models and types (cached in-memory for 30 seconds)
        now_ts = datetime.now().timestamp()
        if _metadata_cache["models"] is None or (now_ts - _metadata_cache["last_fetched"] > 30.0):
            try:
                models_avail = conn.execute(
                    "SELECT DISTINCT model FROM api_calls WHERE model IS NOT NULL AND (COALESCE(calls_count, 1) > 0 OR COALESCE(input_tokens, 0) > 0 OR COALESCE(output_tokens, 0) > 0) ORDER BY model"
                ).fetchall()
                mapped_models = set()
                for r in models_avail:
                    if r["model"]:
                        target = mapping.get(r["model"].lower().strip())
                        if isinstance(target, dict):
                            for v in target.values():
                                if v:
                                    mapped_models.add(v)
                        elif isinstance(target, str):
                            mapped_models.add(target)
                        elif isinstance(target, list):
                            for v in target:
                                if v:
                                    mapped_models.add(v)
                        else:
                            mapped_models.add(r["model"])
                _metadata_cache["models"] = sorted([m for m in mapped_models if m])

                types_avail = conn.execute(
                    "SELECT DISTINCT call_type FROM api_calls WHERE call_type IS NOT NULL ORDER BY call_type"
                ).fetchall()
                _metadata_cache["types"] = [r["call_type"] for r in types_avail if r["call_type"]]
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
                    SUM(CASE WHEN (error IS NOT NULL AND error != '') OR (status_code IS NOT NULL AND (status_code < 200 OR status_code >= 300)) THEN COALESCE(calls_count, 1) ELSE 0 END) as total_errors,
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
                           SUM(CASE WHEN (error IS NOT NULL AND error != '') OR (status_code IS NOT NULL AND (status_code < 200 OR status_code >= 300)) THEN COALESCE(calls_count, 1) ELSE 0 END) as errors,
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


async def handle_query_bulk(request: web.Request) -> web.Response:
    """GET /api/query/bulk — high-throughput columnar sync endpoint for client-side Data Lake."""
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

        since_id = request.query.get("since_id")
        if since_id:
            try:
                where_parts.append("id > ?")
                params.append(int(since_id))
            except ValueError:
                pass

        since_ts = request.query.get("since_ts")
        if since_ts and not since_id:
            where_parts.append("timestamp > ?")
            params.append(since_ts)

        from_ts = request.query.get("from")
        if from_ts:
            where_parts.append("timestamp >= ?")
            params.append(from_ts)

        to_ts = request.query.get("to")
        if to_ts:
            where_parts.append("timestamp <= ?")
            params.append(to_ts)

        models = request.query.getall("model", None)
        if models:
            expanded_models = []
            for m in models:
                expanded_models.append(m)
                m_lower = m.lower().strip()
                for alias_key, target in mapping.items():
                    alias_lower = alias_key.lower().strip()
                    if m_lower == alias_lower:
                        if isinstance(target, str):
                            expanded_models.append(target)
                        elif isinstance(target, dict):
                            expanded_models.extend(target.values())
                        elif isinstance(target, list):
                            expanded_models.extend(target)
                    elif isinstance(target, str) and m_lower == target.lower().strip():
                        expanded_models.append(alias_key)
                    elif isinstance(target, dict) and any(m_lower == str(v).lower().strip() for v in target.values()):
                        expanded_models.append(alias_key)
                    elif isinstance(target, list) and any(m_lower == str(v).lower().strip() for v in target):
                        expanded_models.append(alias_key)
            expanded_models = list(set(expanded_models))
            placeholders = ",".join("?" for _ in expanded_models)
            where_parts.append(f"model IN ({placeholders})")
            params.extend(expanded_models)

        call_types = request.query.getall("call_type", None)
        if call_types:
            placeholders = ",".join("?" * len(call_types))
            where_parts.append(f"call_type IN ({placeholders})")
            params.extend(call_types)

        errors_only = request.query.get("errors_only")
        if errors_only and errors_only.lower() in ("1", "true", "yes"):
            where_parts.append("((error IS NOT NULL AND error != '') OR (status_code IS NOT NULL AND (status_code < 200 OR status_code >= 300)))")

        where_clause = " AND ".join(where_parts) if where_parts else "1=1"
        limit = int(request.query.get("limit", 500000))

        # Check table columns
        col_info = conn.execute("PRAGMA table_info(api_calls)").fetchall()
        avail_cols = {c[1] for c in col_info}
        has_route = "route_name" in avail_cols

        cols = [
            "id", "timestamp", "model", "endpoint",
            "input_tokens", "output_tokens", "ttfb_ms", "total_ms",
            "tokens_per_s", "server_running", "status_code",
            "error", "call_type", "calls_count",
            "route_name", "upstream_url"
        ]

        if has_route:
            select_cols_sql = ", ".join(cols)
        else:
            base_cols_sql = ", ".join(cols[:-2])
            select_cols_sql = f"{base_cols_sql}, NULL as route_name, NULL as upstream_url"

        sql = f"""
            SELECT {select_cols_sql}
            FROM api_calls
            WHERE {where_clause}
            ORDER BY timestamp ASC, id ASC
            LIMIT ?
        """
        rows = conn.execute(sql, params + [limit]).fetchall()

        http_err_map = {
            400: "HTTP 400 Bad Request",
            401: "HTTP 401 Unauthorized",
            403: "HTTP 403 Forbidden",
            404: "HTTP 404 Not Found",
            408: "HTTP 408 Request Timeout",
            429: "HTTP 429 Rate Limit Exceeded",
            500: "HTTP 500 Internal Server Error",
            502: "HTTP 502 Bad Gateway",
            503: "HTTP 503 Service Unavailable",
            504: "HTTP 504 Gateway Timeout"
        }

        router = ModelRouter(config_path=get_routes_config_path()) if ModelRouter else None
        matrix = []
        for r in rows:
            r_list = list(r)
            raw_model = r_list[2]
            ts = r_list[1]
            r_list[2] = get_resolved_model(raw_model, mapping, ts)
            
            status_code = r_list[10]
            err = r_list[11]
            if not err and status_code and (status_code < 200 or status_code >= 300):
                r_list[11] = http_err_map.get(status_code, f"HTTP {status_code}")

            if (not r_list[14] or not r_list[15]) and router and raw_model:
                res = router.resolve(raw_model)
                if res:
                    if not r_list[14]:
                        r_list[14] = res.route_name
                    if not r_list[15]:
                        r_list[15] = res.upstream_url

            matrix.append(r_list)

        now_ts = datetime.now().timestamp()
        if _metadata_cache["models"] is None or (now_ts - _metadata_cache["last_fetched"] > 30.0):
            try:
                models_avail = conn.execute(
                    "SELECT DISTINCT model FROM api_calls WHERE model IS NOT NULL AND (COALESCE(calls_count, 1) > 0 OR COALESCE(input_tokens, 0) > 0 OR COALESCE(output_tokens, 0) > 0) ORDER BY model"
                ).fetchall()
                mapped_models = set()
                for r in models_avail:
                    if r["model"]:
                        target = mapping.get(r["model"].lower().strip())
                        if isinstance(target, dict):
                            for v in target.values():
                                if v:
                                    mapped_models.add(v)
                        elif isinstance(target, str):
                            mapped_models.add(target)
                        elif isinstance(target, list):
                            for v in target:
                                if v:
                                    mapped_models.add(v)
                        else:
                            mapped_models.add(r["model"])
                _metadata_cache["models"] = sorted([m for m in mapped_models if m])
                types_avail = conn.execute("SELECT DISTINCT call_type FROM api_calls WHERE call_type IS NOT NULL ORDER BY call_type").fetchall()
                _metadata_cache["types"] = [r["call_type"] for r in types_avail if r["call_type"]]
                _metadata_cache["last_fetched"] = now_ts
            except Exception:
                pass

        result_payload = {
            "columns": cols,
            "rows": matrix,
            "count": len(matrix),
            "db_fingerprint": get_db_fingerprint(),
            "available_models": _metadata_cache["models"] or [],
            "available_types": _metadata_cache["types"] or []
        }

        json_text = json.dumps(result_payload)
        json_bytes = json_text.encode("utf-8")

        accept_enc = request.headers.get("Accept-Encoding", "")
        if "gzip" in accept_enc and len(json_bytes) > 1024:
            gz_body = gzip.compress(json_bytes, compresslevel=3)
            return web.Response(
                body=gz_body,
                content_type="application/json",
                headers={
                    "Content-Encoding": "gzip",
                    "Vary": "Accept-Encoding",
                    "Cache-Control": "no-cache"
                }
            )

        return web.Response(
            body=json_bytes,
            content_type="application/json",
            headers={"Cache-Control": "no-cache"}
        )
    except Exception as e:
        return web.json_response({
            "error": "Query bulk execution failed",
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
        "db_fingerprint": get_db_fingerprint(),
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
            "deepseek": [{"effective_date": "2026-07-04", "input_cost_per_million": 0.14, "output_cost_per_million": 0.28, "provider_source": "DeepSeek API (Official)"}],
            "gemma-4": [{"effective_date": "2026-07-04", "input_cost_per_million": 0.07, "output_cost_per_million": 0.27, "provider_source": "Google AI Studio"}],
            "glm-5.2": [{"effective_date": "2026-07-04", "input_cost_per_million": 1.40, "output_cost_per_million": 4.40, "provider_source": "Zhipu AI Developer Platform"}],
            "gpt-oss-120b": [{"effective_date": "2026-07-04", "input_cost_per_million": 0.60, "output_cost_per_million": 0.60, "provider_source": "Together AI (Hosted)"}],
            "qwen3-embedding-4b": [{"effective_date": "2026-07-04", "input_cost_per_million": 0.01, "output_cost_per_million": 0.00, "provider_source": "Alibaba Cloud Model Studio"}],
            "qwen3.5-int4": [{"effective_date": "2026-07-04", "input_cost_per_million": 0.05, "output_cost_per_million": 0.10, "provider_source": "Alibaba Cloud / self-hosted"}]
        }
        return web.json_response(fallback)


async def handle_costs_sync(request: web.Request) -> web.Response:
    """POST /api/costs/sync — automatically sync latest prices from LiteLLM and update model_costs.json."""
    try:
        try:
            from update_model_costs import sync_model_costs
        except ImportError:
            from dashboard.update_model_costs import sync_model_costs
        loop = asyncio.get_event_loop()
        report = await loop.run_in_executor(None, sync_model_costs)
        return web.json_response(report)
    except Exception as e:
        return web.json_response({"error": f"Failed to sync model costs: {str(e)}"}, status=500)


async def handle_model_mapping(request: web.Request) -> web.Response:
    """GET /api/model-mapping — retrieves alias to canonical model mapping."""
    mapping = load_model_mapping()
    return web.json_response(mapping)


async def handle_dashboard(request: web.Request) -> web.Response:
    dashboard_html = get_dashboard_html_path()
    if dashboard_html.exists():
        return web.FileResponse(dashboard_html)
    return web.Response(text="Dashboard file not found at " + str(dashboard_html), status=404)


def parse_token_limit(val: Any, default: int = 480_000_000) -> int:
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return max(0, int(val))
    val_str = str(val).strip().lower().replace(",", "")
    if val_str.endswith("m"):
        try:
            return max(0, int(float(val_str[:-1]) * 1_000_000))
        except ValueError:
            pass
    elif val_str.endswith("k"):
        try:
            return max(0, int(float(val_str[:-1]) * 1_000))
        except ValueError:
            pass
    elif val_str.endswith("b"):
        try:
            return max(0, int(float(val_str[:-1]) * 1_000_000_000))
        except ValueError:
            pass
    try:
        return max(0, int(val_str))
    except ValueError:
        return default


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
    upstream = data.get("upstream")
    if not upstream or upstream == "https://llm.ai.e-infra.cz/v1":
        cfg_path = get_routes_config_path()
        if cfg_path.exists():
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    r_cfg = json.load(f)
                    def_url = r_cfg.get("default_route", {}).get("upstream_url")
                    if def_url:
                        upstream = def_url
            except Exception:
                pass
    if not upstream:
        upstream = "https://llm.ai.e-infra.cz/v1"

    token_limit = parse_token_limit(data.get("token_limit", 480_000_000))
    max_concurrent = int(data.get("max_concurrent")) if data.get("max_concurrent") is not None else None
    slot_cooldown_ms = int(data.get("slot_cooldown_ms")) if data.get("slot_cooldown_ms") is not None else None
    retry_429_max = int(data.get("retry_429_max")) if data.get("retry_429_max") is not None else None
    db_path = get_db_path()
    res = await ProxyManager.start(
        port=port,
        host=host,
        upstream=upstream,
        token_limit=token_limit,
        db_path=db_path,
        max_concurrent=max_concurrent,
        slot_cooldown_ms=slot_cooldown_ms,
        retry_429_max=retry_429_max,
    )
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
    upstream = data.get("upstream")
    if not upstream or upstream == "https://llm.ai.e-infra.cz/v1":
        cfg_path = get_routes_config_path()
        if cfg_path.exists():
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    r_cfg = json.load(f)
                    def_url = r_cfg.get("default_route", {}).get("upstream_url")
                    if def_url:
                        upstream = def_url
            except Exception:
                pass
    if not upstream:
        upstream = "https://llm.ai.e-infra.cz/v1"

    token_limit = parse_token_limit(data.get("token_limit", 480_000_000))
    max_concurrent = int(data.get("max_concurrent")) if data.get("max_concurrent") is not None else None
    slot_cooldown_ms = int(data.get("slot_cooldown_ms")) if data.get("slot_cooldown_ms") is not None else None
    retry_429_max = int(data.get("retry_429_max")) if data.get("retry_429_max") is not None else None
    db_path = get_db_path()
    res = await ProxyManager.restart(
        port=port,
        host=host,
        upstream=upstream,
        token_limit=token_limit,
        db_path=db_path,
        max_concurrent=max_concurrent,
        slot_cooldown_ms=slot_cooldown_ms,
        retry_429_max=retry_429_max,
    )
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


async def handle_proxy_routes_get(request: web.Request) -> web.Response:
    """GET /api/proxy/routes — get model routing config."""
    config_path = get_routes_config_path()
    router = ModelRouter(config_path=config_path) if ModelRouter else None
    if not router:
        return web.json_response({"error": "ModelRouter not initialized"}, status=500)
    return web.json_response(router.to_dict())


async def handle_proxy_routes_save(request: web.Request) -> web.Response:
    """POST /api/proxy/routes — update model routing config and sync with live proxy."""
    try:
        data = await request.json() if request.can_read_body else {}
        if not isinstance(data, dict):
            return web.json_response({"error": "Invalid payload format, expected JSON object"}, status=400)
        config_path = get_routes_config_path()
        router = ModelRouter(config_path=config_path) if ModelRouter else None
        if not router:
            return web.json_response({"error": "ModelRouter not initialized"}, status=500)
        router.update_from_dict(data)
        success = router.save()
        if not success:
            return web.json_response({"error": "Failed to save routes configuration to disk"}, status=500)

        # If proxy is currently running, hot-reload routes dynamically
        proxy_status = await ProxyManager.get_status()
        proxy_synced = False
        if proxy_status.get("running") or proxy_status.get("health_ok"):
            proxy_port = proxy_status.get("port", 9090)
            try:
                timeout = aiohttp.ClientTimeout(total=2.0)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(f"http://127.0.0.1:{proxy_port}/v1/routes", json=data) as resp:
                        if resp.status == 200:
                            proxy_synced = True
            except Exception as sync_err:
                print(f"[dashboard] Warning: hot-syncing routes to running proxy failed: {sync_err}", file=sys.stderr)

        return web.json_response({
            "success": True,
            "message": "Model routing configuration saved successfully",
            "proxy_synced": proxy_synced,
            "config": router.to_dict(),
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)


async def handle_proxy_routes_test(request: web.Request) -> web.Response:
    """POST /api/proxy/routes/test — evaluate model route resolution."""
    try:
        data = await request.json() if request.can_read_body else {}
        model_name = data.get("model", "")
        config_path = get_routes_config_path()
        router = ModelRouter(config_path=config_path) if ModelRouter else None
        if not router:
            return web.json_response({"error": "ModelRouter not initialized"}, status=500)
        res = router.resolve(model_name)
        return web.json_response({
            "model": model_name,
            "resolved_upstream": res.upstream_url,
            "route_name": res.route_name,
            "route_id": res.route_id,
            "is_default": res.is_default,
            "pattern_matched": res.pattern_matched,
            "max_concurrent": res.max_concurrent,
            "slot_cooldown_ms": res.slot_cooldown_ms,
            "max_rpm": res.max_rpm,
            "timeout": res.timeout,
            "fallback_upstream_url": res.fallback_upstream_url,
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)


async def handle_control_panel_bundle(request: web.Request) -> web.Response:
    """GET /api/control-panel/bundle — returns unified proxy status, health, routes, logs, and raw-log status in ONE request."""
    port_str = request.query.get("port")
    port = int(port_str) if port_str and port_str.isdigit() else None
    lines_str = request.query.get("lines", "150")
    lines = int(lines_str) if lines_str.isdigit() else 150
    include_logs = request.query.get("include_logs", "1") != "0"

    # 1. Proxy status
    proxy_status = await ProxyManager.get_status(port=port)

    # 2. Database Health
    db_path = get_db_path()
    db_exists = db_path.exists()
    dashboard_html = get_dashboard_html_path()
    try:
        db_size = db_path.stat().st_size if db_exists else 0
    except Exception:
        db_size = 0
    health_data = {
        "status": "ok",
        "db_path": str(db_path),
        "db_exists": db_exists,
        "db_size_mb": round(db_size / 1024 / 1024, 1) if db_exists else 0,
        "db_fingerprint": get_db_fingerprint(),
        "dashboard_html": dashboard_html.exists(),
    }

    # 3. Routes config
    config_path = get_routes_config_path()
    router = ModelRouter(config_path=config_path) if ModelRouter else None
    routes_data = router.to_dict() if router else {}

    # 4. Raw log status
    logger_file = get_logger_file_path()
    size = logger_file.stat().st_size if logger_file.exists() else 0
    def fmt_sz(b):
        if b < 1024:
            return f"{b} B"
        elif b < 1024 * 1024:
            return f"{b / 1024:.1f} KB"
        return f"{b / (1024 * 1024):.2f} MB"

    raw_log_enabled = proxy_status.get("health", {}).get("raw_logging", False) if proxy_status.get("health") else False
    raw_log_data = {
        "enabled": raw_log_enabled,
        "proxy_alive": proxy_status.get("running", False),
        "file_path": str(logger_file),
        "rel_path": str(logger_file.relative_to(REPO_ROOT)) if logger_file.is_relative_to(REPO_ROOT) else str(logger_file),
        "file_size_bytes": size,
        "file_size_formatted": fmt_sz(size),
    }

    # 5. Proxy logs
    proxy_logs_data = ProxyManager.get_logs(lines=lines) if include_logs else None

    return web.json_response({
        "proxy_status": proxy_status,
        "health": health_data,
        "routes": routes_data,
        "raw_log_status": raw_log_data,
        "proxy_logs": proxy_logs_data,
    })


async def handle_dashboard_bundle(request: web.Request) -> web.Response:
    """GET /api/dashboard/bundle — returns model mapping, costs, health, proxy status, and routes in ONE request."""
    mapping = load_model_mapping()
    costs_path = get_model_costs_path()
    costs_data = {}
    if costs_path.exists():
        try:
            with open(costs_path, "r", encoding="utf-8") as f:
                costs_data = json.load(f)
        except Exception:
            pass

    proxy_status = await ProxyManager.get_status()
    db_path = get_db_path()
    db_exists = db_path.exists()
    try:
        db_size = db_path.stat().st_size if db_exists else 0
    except Exception:
        db_size = 0

    config_path = get_routes_config_path()
    router = ModelRouter(config_path=config_path) if ModelRouter else None
    routes_data = router.to_dict() if router else {}

    return web.json_response({
        "model_mapping": mapping,
        "model_costs": costs_data,
        "health": {
            "status": "ok",
            "db_path": str(db_path),
            "db_exists": db_exists,
            "db_size_mb": round(db_size / 1024 / 1024, 1) if db_exists else 0,
            "db_fingerprint": get_db_fingerprint(),
            "dashboard_html": get_dashboard_html_path().exists(),
        },
        "proxy_status": proxy_status,
        "routes": routes_data,
    })


def get_active_proxy_port() -> int:
    return getattr(ProxyManager, '_last_known_port', 9090)


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
        entries = []
        for l in reversed(recent_lines):
            try:
                entries.append(json.loads(l))
            except Exception:
                pass
        return entries, total_count
    else:
        # Backward chunk reader for large files
        chunk_size = 64 * 1024
        collected_lines: list[str] = []
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            position = f.tell()
            remainder = b""

            while position > 0 and len(collected_lines) < limit:
                read_size = min(chunk_size, position)
                position -= read_size
                f.seek(position, os.SEEK_SET)
                chunk = f.read(read_size) + remainder

                parts = chunk.split(b"\n")
                if position > 0:
                    remainder = parts[0]
                    complete_parts = parts[1:]
                else:
                    remainder = b""
                    complete_parts = parts

                for part in reversed(complete_parts):
                    p_str = part.decode("utf-8", errors="replace").strip()
                    if p_str:
                        collected_lines.append(p_str)
                        if len(collected_lines) >= limit:
                            break

            total_count = max(len(collected_lines), limit)

        entries = []
        for l in collected_lines:
            try:
                entries.append(json.loads(l))
            except Exception:
                pass

        return entries, total_count


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
    proxy_port = get_active_proxy_port()
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

    proxy_port = get_active_proxy_port()
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
        entries, total_count = await asyncio.to_thread(read_recent_jsonl_lines, logger_file, limit)
        return web.json_response({"entries": entries, "total_count": total_count})
    except Exception as e:
        return web.json_response({"error": str(e), "entries": []}, status=500)


async def handle_raw_log_clear(request: web.Request) -> web.Response:
    """POST /api/raw-log/clear — truncate the logger file."""
    logger_file = get_logger_file_path()
    proxy_port = get_active_proxy_port()
    try:
        if logger_file.exists():
            with open(logger_file, "w", encoding="utf-8") as f:
                f.truncate(0)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1.0)) as session:
                await session.post(f"http://127.0.0.1:{proxy_port}/v1/raw-log/clear")
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
            'Cache-Control': 'no-cache, no-transform',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
            'Access-Control-Allow-Origin': '*',
        }
    )
    await response.prepare(request)

    proxy_port = get_active_proxy_port()
    proxy_url = f"http://127.0.0.1:{proxy_port}/v1/raw-log/stream"
    try:
        timeout = aiohttp.ClientTimeout(total=None)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(proxy_url) as proxy_resp:
                if proxy_resp.status != 200:
                    await response.write(b"event: error\ndata: {\"error\": \"Proxy SSE unavailable\"}\n\n")
                    return response
                async for chunk in proxy_resp.content.iter_any():
                    if chunk:
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


# ── Asset Handlers ───────────────────────────────────────────────────────────

async def handle_js_asset(request: web.Request) -> web.Response:
    """Handler for JS assets."""
    filename = request.match_info.get("filename", "")
    candidates = [
        DASHBOARD_DIR / "static" / "js" / filename,
        DASHBOARD_DIR / "static" / filename,
        REPO_ROOT / "dashboard" / "static" / "js" / filename,
    ]
    for p in candidates:
        if p.exists() and p.is_file():
            return web.FileResponse(p, headers={"Content-Type": "application/javascript; charset=utf-8"})
    return web.Response(text=f"JavaScript file {filename} not found", status=404)


async def handle_css_asset(request: web.Request) -> web.Response:
    """Handler for CSS assets."""
    filename = request.match_info.get("filename", "")
    candidates = [
        DASHBOARD_DIR / "static" / "css" / filename,
        DASHBOARD_DIR / "static" / filename,
        REPO_ROOT / "dashboard" / "static" / "css" / filename,
    ]
    for p in candidates:
        if p.exists() and p.is_file():
            return web.FileResponse(p, headers={"Content-Type": "text/css; charset=utf-8"})
    return web.Response(text=f"CSS file {filename} not found", status=404)


# ── App ──────────────────────────────────────────────────────────────────────
def create_app():
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/api/query", handle_query)
    app.router.add_get("/api/query/bulk", handle_query_bulk)
    app.router.add_get("/api/server-status", handle_server_status)
    app.router.add_get("/api/stats", handle_query)  # alias
    app.router.add_get("/api/costs", handle_costs)
    app.router.add_post("/api/costs/sync", handle_costs_sync)
    app.router.add_get("/api/model-mapping", handle_model_mapping)
    app.router.add_get("/favicon.ico", handle_favicon)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/health", handle_health)
    
    # Consolidated Bundled Query Routes (reduces multi-endpoint polling down to 1 request)
    app.router.add_get("/api/control-panel/bundle", handle_control_panel_bundle)
    app.router.add_get("/api/dashboard/bundle", handle_dashboard_bundle)

    # Proxy lifecycle routes
    app.router.add_get("/api/proxy/status", handle_proxy_status)
    app.router.add_post("/api/proxy/start", handle_proxy_start)
    app.router.add_post("/api/proxy/stop", handle_proxy_stop)
    app.router.add_post("/api/proxy/restart", handle_proxy_restart)
    app.router.add_get("/api/proxy/logs", handle_proxy_logs)
    app.router.add_post("/api/proxy/clear-logs", handle_proxy_clear_logs)
    app.router.add_get("/api/proxy/routes", handle_proxy_routes_get)
    app.router.add_post("/api/proxy/routes", handle_proxy_routes_save)
    app.router.add_post("/api/proxy/routes/test", handle_proxy_routes_test)
    app.router.add_post("/api/db/compress", handle_db_compress)

    # Raw Payload Log & Inspector routes
    app.router.add_get("/api/raw-log/status", handle_raw_log_status)
    app.router.add_post("/api/raw-log/toggle", handle_raw_log_toggle)
    app.router.add_get("/api/raw-log/recent", handle_raw_log_recent)
    app.router.add_post("/api/raw-log/clear", handle_raw_log_clear)
    app.router.add_get("/api/raw-log/stream", handle_raw_log_stream)

    # Inspector UI routes
    app.router.add_get("/", handle_dashboard)
    app.router.add_get("/dashboard", handle_dashboard)
    app.router.add_get("/dashboard/", handle_dashboard)
    app.router.add_get("/inspector", handle_inspector)
    app.router.add_get("/inspector/", handle_inspector)
    app.router.add_get("/raw-logs", handle_inspector)
    app.router.add_get("/raw-logs/", handle_inspector)
    app.router.add_get("/raw_log_inspector.html", handle_inspector)

    # Direct Asset Fallback Handlers (guarantees 100% 200 OK regardless of path structure)
    app.router.add_get("/static/js/{filename}", handle_js_asset)
    app.router.add_get("/static/css/{filename}", handle_css_asset)
    app.router.add_get("/js/{filename}", handle_js_asset)
    app.router.add_get("/css/{filename}", handle_css_asset)
    app.router.add_get("/inspector/static/js/{filename}", handle_js_asset)
    app.router.add_get("/inspector/static/css/{filename}", handle_css_asset)
    app.router.add_get("/inspector/js/{filename}", handle_js_asset)
    app.router.add_get("/inspector/css/{filename}", handle_css_asset)

    static_dir = get_static_dir_path()
    static_dir.mkdir(exist_ok=True)
    app.router.add_static("/static", path=static_dir, name="static")
    app.router.add_static("/static/", path=static_dir)

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
    web.run_app(create_app(), host="127.0.0.1", port=port, access_log=None, reuse_address=True)


if __name__ == "__main__":
    main()

