#!/usr/bin/env python3
"""
Telemetry database, model mapping, token budget, and server load monitoring.
Part of the LLM Telemetry Proxy.
"""

import os
import sys
import time
import json
import uuid
import sqlite3
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import deque
from typing import Optional, Dict, Any, Tuple, List, Union

import aiohttp

def resolve_repo_root(start_file: Optional[Path] = None) -> Path:
    """Accurately locate the repository root under Python and Nuitka standalone binary execution."""
    for env_key in ("LLM_PROXY_REPO_ROOT", "REPO_ROOT"):
        val = os.environ.get(env_key)
        if val and Path(val).is_dir():
            return Path(val).resolve()

    start = (start_file or Path(__file__)).resolve()
    for p in [start.parent] + list(start.parents):
        if (p / "proxy" / "llm_telemetry_proxy.py").is_file():
            return p
        if (p / "proxy").is_dir() and ((p / "dashboard").is_dir() or (p / "data").is_dir()):
            return p

    try:
        cwd = Path.cwd().resolve()
        for p in [cwd] + list(cwd.parents):
            if (p / "proxy" / "llm_telemetry_proxy.py").is_file():
                return p
            if (p / "proxy").is_dir() and ((p / "dashboard").is_dir() or (p / "data").is_dir()):
                return p
    except Exception:
        pass

    p = start.parent
    if p.name.endswith(".dist"):
        return p.parent.parent
    if p.name == "dist":
        return p.parent
    return p.parent


REPO_ROOT = resolve_repo_root(Path(__file__))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_env_db_path = os.environ.get("TELEMETRY_DB_PATH")
DB_PATH = Path(_env_db_path) if _env_db_path else (REPO_ROOT / "data" / "llm_telemetry.db")
TOKEN_BUDGET_FILE = REPO_ROOT / "data" / "token_budget.json"
STATUS_API = "https://llm.ai.e-infra.cz/status/api/v1/models"
DAILY_TOKEN_LIMIT = 480_000_000  # 480 million tokens


def format_time_remaining(seconds: int) -> str:
    if seconds <= 0:
        return "0m"
    hrs = seconds // 3600
    mins = (seconds % 3600) // 60
    secs = seconds % 60
    if hrs > 0:
        return f"{hrs}h {mins}m" if mins > 0 else f"{hrs}h"
    if mins > 0:
        return f"{mins}m"
    if secs > 0:
        return "< 1m"
    return "0m"


class RollingTokenBudget:
    """Rolling 24-hour token budget with hard enforcement and persistence across restarts."""

    def __init__(self, daily_limit: int, db_path: Path = None, state_file: Path = None):
        self.daily_limit = daily_limit
        self.db_path = db_path if db_path is not None else DB_PATH
        self.state_file = state_file if state_file is not None else TOKEN_BUDGET_FILE
        self._usage = deque()
        self._load_state()

    def _load_state(self):
        now_utc = datetime.now(timezone.utc)
        start_of_today = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff = start_of_today.timestamp()
        cutoff_iso = start_of_today.isoformat()
        temp_usage = []

        try:
            if self.db_path and Path(self.db_path).exists():
                conn = sqlite3.connect(str(self.db_path), timeout=5.0)
                cur = conn.cursor()
                cur.execute("""
                    SELECT timestamp, input_tokens, output_tokens, calls_count
                    FROM api_calls
                    WHERE timestamp >= ?
                    ORDER BY timestamp ASC
                """, (cutoff_iso,))
                rows = cur.fetchall()
                conn.close()

                for row in rows:
                    ts_str, in_tok, out_tok, calls_cnt = row
                    cnt = calls_cnt if calls_cnt else 1
                    total_tok = ((in_tok or 0) + (out_tok or 0)) * cnt
                    if total_tok <= 0:
                        continue
                    try:
                        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if dt.timestamp() >= cutoff:
                            temp_usage.append((dt.timestamp(), total_tok))
                    except Exception:
                        pass
        except Exception as e:
            print(f"[telemetry] Error loading token budget from DB: {e}", file=sys.stderr)

        if not temp_usage and self.state_file and Path(self.state_file).exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for r in data.get("recent_usage", []):
                        ts, cnt = r.get("ts", 0), r.get("tokens", 0)
                        if ts >= cutoff and cnt > 0:
                            temp_usage.append((ts, cnt))
            except Exception as e:
                print(f"[telemetry] Error reading {self.state_file}: {e}", file=sys.stderr)

        if temp_usage:
            self._usage = deque(sorted(temp_usage, key=lambda x: x[0]))

        self._save_state()

    def _save_state(self):
        if not self.state_file:
            return
        try:
            now_utc = datetime.now(timezone.utc)
            start_of_today = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
            cutoff = start_of_today.timestamp()
            while self._usage and self._usage[0][0] < cutoff:
                self._usage.popleft()

            current_usage = sum(count for _, count in self._usage)
            remaining = max(0, self.daily_limit - current_usage)
            percentage_used = (current_usage / self.daily_limit) * 100 if self.daily_limit > 0 else 0

            now = time.time()
            oldest_ts = self._usage[0][0] if self._usage else None
            newest_ts = self._usage[-1][0] if self._usage else None
            next_reset_seconds = max(0, int((oldest_ts + 86400) - now)) if oldest_ts else 0
            full_reset_seconds = max(0, int((newest_ts + 86400) - now)) if newest_ts else 0

            tomorrow_midnight = (now_utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            daily_reset_seconds = max(0, int((tomorrow_midnight - now_utc).total_seconds()))
            daily_reset_formatted = format_time_remaining(daily_reset_seconds)

            recent_list = [{"ts": round(ts, 2), "tokens": cnt} for ts, cnt in self._usage]

            state_data = {
                "daily_limit": self.daily_limit,
                "total_used": current_usage,
                "remaining": remaining,
                "percentage_used": round(percentage_used, 2),
                "daily_reset_seconds": daily_reset_seconds,
                "daily_reset_formatted": daily_reset_formatted,
                "next_reset_seconds": next_reset_seconds,
                "full_reset_seconds": full_reset_seconds,
                "next_reset_formatted": format_time_remaining(next_reset_seconds) if oldest_ts else None,
                "full_reset_formatted": format_time_remaining(full_reset_seconds) if newest_ts else None,
                "server_time": now_utc.isoformat(),
                "reset_time_utc": tomorrow_midnight.isoformat(),
                "updated_at": now_utc.isoformat(),
                "recent_usage": recent_list[-5000:],
            }

            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            temp_file = self.state_file.with_suffix(".tmp")
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(state_data, f, indent=2)
            temp_file.replace(self.state_file)
        except Exception:
            pass

    def record_and_check(self, input_tokens: int, output_tokens: int) -> tuple[bool, dict]:
        now = time.time()
        self._usage.append((now, input_tokens + output_tokens))

        now_utc = datetime.now(timezone.utc)
        start_of_today = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff = start_of_today.timestamp()
        while self._usage and self._usage[0][0] < cutoff:
            self._usage.popleft()

        current_usage = sum(count for _, count in self._usage)
        remaining = max(0, self.daily_limit - current_usage)
        percentage_used = (current_usage / self.daily_limit) * 100 if self.daily_limit > 0 else 0

        tomorrow_midnight = (now_utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        daily_reset_seconds = max(0, int((tomorrow_midnight - now_utc).total_seconds()))
        daily_reset_formatted = format_time_remaining(daily_reset_seconds)

        oldest_ts = self._usage[0][0] if self._usage else None
        newest_ts = self._usage[-1][0] if self._usage else None
        next_reset_seconds = max(0, int((oldest_ts + 86400) - now)) if oldest_ts else 0
        full_reset_seconds = max(0, int((newest_ts + 86400) - now)) if newest_ts else 0

        status = {
            "total_used": current_usage,
            "remaining": remaining,
            "percentage_used": round(percentage_used, 2),
            "daily_limit": self.daily_limit,
            "daily_reset_seconds": daily_reset_seconds,
            "daily_reset_formatted": daily_reset_formatted,
            "next_reset_seconds": next_reset_seconds,
            "full_reset_seconds": full_reset_seconds,
            "next_reset_formatted": format_time_remaining(next_reset_seconds) if oldest_ts else None,
            "full_reset_formatted": format_time_remaining(full_reset_seconds) if newest_ts else None,
            "server_time": now_utc.isoformat(),
            "reset_time_utc": tomorrow_midnight.isoformat(),
        }

        self._save_state()
        return (current_usage < self.daily_limit), status

    def get_status(self) -> dict:
        now = time.time()
        now_utc = datetime.now(timezone.utc)
        start_of_today = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff = start_of_today.timestamp()
        while self._usage and self._usage[0][0] < cutoff:
            self._usage.popleft()

        current_usage = sum(count for _, count in self._usage)
        remaining = max(0, self.daily_limit - current_usage)
        percentage_used = (current_usage / self.daily_limit) * 100 if self.daily_limit > 0 else 0

        tomorrow_midnight = (now_utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        daily_reset_seconds = max(0, int((tomorrow_midnight - now_utc).total_seconds()))
        daily_reset_formatted = format_time_remaining(daily_reset_seconds)

        oldest_ts = self._usage[0][0] if self._usage else None
        newest_ts = self._usage[-1][0] if self._usage else None
        next_reset_seconds = max(0, int((oldest_ts + 86400) - now)) if oldest_ts else 0
        full_reset_seconds = max(0, int((newest_ts + 86400) - now)) if newest_ts else 0

        return {
            "total_used": current_usage,
            "remaining": remaining,
            "percentage_used": round(percentage_used, 2),
            "daily_limit": self.daily_limit,
            "daily_reset_seconds": daily_reset_seconds,
            "daily_reset_formatted": daily_reset_formatted,
            "next_reset_seconds": next_reset_seconds,
            "full_reset_seconds": full_reset_seconds,
            "next_reset_formatted": format_time_remaining(next_reset_seconds) if oldest_ts else None,
            "full_reset_formatted": format_time_remaining(full_reset_seconds) if newest_ts else None,
            "server_time": now_utc.isoformat(),
            "reset_time_utc": tomorrow_midnight.isoformat(),
            "oldest_token_ts": oldest_ts,
            "newest_token_ts": newest_ts,
        }


_token_budget = RollingTokenBudget(DAILY_TOKEN_LIMIT, db_path=DB_PATH, state_file=TOKEN_BUDGET_FILE)


# ── SQLite ──────────────────────────────────────────────────────────────────
def _configure_db_pragmas(conn: sqlite3.Connection):
    """Apply high-performance SQLite PRAGMAs for concurrent WAL mode and memory tuning."""
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA temp_store = MEMORY;")
        conn.execute("PRAGMA cache_size = -32000;")
        conn.execute("PRAGMA busy_timeout = 5000;")
    except Exception:
        pass


def get_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    conn.row_factory = sqlite3.Row
    _configure_db_pragmas(conn)
    return conn


def _ensure_optional_columns(conn, table: str):
    """Resiliently ensure columns exist for older database files."""
    for col, col_type, default_val in [
        ("call_type", "TEXT", "'chat'"),
        ("calls_count", "INTEGER", "1"),
        ("route_name", "TEXT", None),
        ("upstream_url", "TEXT", None),
        ("retries_attempted", "INTEGER", "0"),
        ("absorbed_429", "INTEGER", "0"),
    ]:
        try:
            sql = f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"
            if default_val is not None:
                sql += f" DEFAULT {default_val}"
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    _configure_db_pragmas(conn)
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
            calls_count   INTEGER DEFAULT 1,
            route_name    TEXT,
            upstream_url  TEXT,
            retries_attempted INTEGER DEFAULT 0,
            absorbed_429  INTEGER DEFAULT 0
        )
    """)
    _ensure_optional_columns(conn, "api_calls")

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
            calls_count   INTEGER DEFAULT 1,
            route_name    TEXT,
            upstream_url  TEXT,
            retries_attempted INTEGER DEFAULT 0,
            absorbed_429  INTEGER DEFAULT 0
        )
    """)
    _ensure_optional_columns(conn, "proxy_calls")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON api_calls(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_model ON api_calls(model)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_type ON api_calls(call_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_api_calls_ts_id ON api_calls(timestamp DESC, id DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_api_calls_model_ts ON api_calls(model, timestamp DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_proxy_ts ON proxy_calls(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_proxy_calls_ts_id ON proxy_calls(timestamp DESC, id DESC)")

    conn.execute("CREATE TABLE IF NOT EXISTS _telemetry_meta (key TEXT PRIMARY KEY, value TEXT)")
    cur = conn.execute("SELECT value FROM _telemetry_meta WHERE key = 'db_instance_id'")
    if not cur.fetchone():
        conn.execute("INSERT OR IGNORE INTO _telemetry_meta (key, value) VALUES ('db_instance_id', ?)", (uuid.uuid4().hex,))
    cur = conn.execute("SELECT value FROM _telemetry_meta WHERE key = 'compaction_version'")
    if not cur.fetchone():
        conn.execute("INSERT OR IGNORE INTO _telemetry_meta (key, value) VALUES ('compaction_version', '1')")

    conn.commit()
    conn.close()


# ── Real-Time Model Mapping Resolution (Held in Server RAM) ──────────────────
_model_mapping_cache = {}
_model_mapping_mtime = 0


def load_model_mapping():
    global _model_mapping_cache, _model_mapping_mtime
    for p in [
        REPO_ROOT / "data" / "model_mapping.json",
        REPO_ROOT / "model_mapping.json",
        REPO_ROOT / "data" / "model_mappings.json",
    ]:
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


def resolve_canonical_model(model_name: str, timestamp: str = None) -> str:
    if not model_name:
        return "Unknown"
    mapping = load_model_mapping()
    if not mapping:
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


def _insert_with_fallback(table, full_cols, full_vals, legacy_cols, legacy_vals):
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
        _configure_db_pragmas(conn)
        try:
            placeholders = ", ".join(["?"] * len(full_vals))
            conn.execute(f"INSERT INTO {table} ({', '.join(full_cols)}) VALUES ({placeholders})", full_vals)
        except sqlite3.OperationalError:
            _ensure_optional_columns(conn, table)
            try:
                placeholders = ", ".join(["?"] * len(full_vals))
                conn.execute(f"INSERT INTO {table} ({', '.join(full_cols)}) VALUES ({placeholders})", full_vals)
            except Exception:
                fb_placeholders = ", ".join(["?"] * len(legacy_vals))
                conn.execute(f"INSERT INTO {table} ({', '.join(legacy_cols)}) VALUES ({fb_placeholders})", legacy_vals)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[telemetry] {table} log error: {e}", file=sys.stderr)


def log_call(model, endpoint, input_tokens, output_tokens,
             ttfb_ms, total_ms, tokens_per_s,
             server_running, server_tok_s, server_model,
             status_code, error, call_type='chat',
             route_name=None, upstream_url=None,
             retries_attempted=0, absorbed_429=0):
    model = resolve_canonical_model(model)
    full_cols = [
        "timestamp", "model", "endpoint", "input_tokens", "output_tokens",
        "ttfb_ms", "total_ms", "tokens_per_s", "server_running", "server_tok_s",
        "server_model", "status_code", "error", "call_type", "calls_count",
        "route_name", "upstream_url", "retries_attempted", "absorbed_429"
    ]
    full_vals = (
        datetime.now(timezone.utc).isoformat(), model, endpoint, input_tokens, output_tokens,
        ttfb_ms, total_ms, tokens_per_s, server_running, server_tok_s,
        server_model, status_code, error, call_type, 1,
        route_name, upstream_url, int(retries_attempted or 0), int(absorbed_429 or 0)
    )
    legacy_cols = [
        "timestamp", "model", "endpoint", "input_tokens", "output_tokens",
        "ttfb_ms", "total_ms", "tokens_per_s", "server_running", "server_tok_s",
        "server_model", "status_code", "error", "call_type", "calls_count"
    ]
    legacy_vals = (
        datetime.now(timezone.utc).isoformat(), model, endpoint, input_tokens, output_tokens,
        ttfb_ms, total_ms, tokens_per_s, server_running, server_tok_s,
        server_model, status_code, error, call_type, 1
    )
    _insert_with_fallback("api_calls", full_cols, full_vals, legacy_cols, legacy_vals)


def log_proxy_call(endpoint, method, call_type, model, status_code, error, logged, ttfb_ms, total_ms,
                   route_name=None, upstream_url=None,
                   retries_attempted=0, absorbed_429=0):
    model = resolve_canonical_model(model)
    full_cols = [
        "timestamp", "endpoint", "method", "call_type", "model", "status_code",
        "error", "logged", "ttfb_ms", "total_ms", "calls_count",
        "route_name", "upstream_url", "retries_attempted", "absorbed_429"
    ]
    full_vals = (
        datetime.now(timezone.utc).isoformat(), endpoint, method, call_type, model, status_code,
        error, logged, ttfb_ms, total_ms, 1,
        route_name, upstream_url, int(retries_attempted or 0), int(absorbed_429 or 0)
    )
    legacy_cols = [
        "timestamp", "endpoint", "method", "call_type", "model", "status_code",
        "error", "logged", "ttfb_ms", "total_ms", "calls_count"
    ]
    legacy_vals = (
        datetime.now(timezone.utc).isoformat(), endpoint, method, call_type, model, status_code,
        error, logged, ttfb_ms, total_ms, 1
    )
    _insert_with_fallback("proxy_calls", full_cols, full_vals, legacy_cols, legacy_vals)


def classify_endpoint(path):
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
    if not model_hint:
        return None, None, None

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
                        if not isinstance(m, dict) or m.get("status") not in ("online",):
                            continue
                        name = m.get("model_name") or m.get("container", "?")
                        latest = m.get("latest") or {}
                        running = 0 if isinstance(latest.get("num_requests_running"), dict) else (latest.get("num_requests_running", 0) or 0)
                        tok_s = 0.0 if isinstance(latest.get("num_requests_running"), dict) else (latest.get("generation_tokens_rate", 0.0) or 0.0)
                        data[name] = {
                            "status": m.get("status", "online"),
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

    if not data:
        return None, None, None

    norm_data = {k.lower().strip(): k for k in data.keys()}
    m_str = str(model_hint).strip().lower()
    canon_str = str(resolve_canonical_model(model_hint)).strip().lower()

    if m_str in norm_data:
        target_name = norm_data[m_str]
        return data[target_name]["running"], data[target_name]["tok_s"], target_name

    if canon_str in norm_data:
        target_name = norm_data[canon_str]
        return data[target_name]["running"], data[target_name]["tok_s"], target_name

    mapping = load_model_mapping() or {}
    raw_mapping_val = mapping.get(m_str)
    if isinstance(raw_mapping_val, dict):
        for mapped_target in raw_mapping_val.values():
            if str(mapped_target).strip().lower() in norm_data:
                target_name = norm_data[str(mapped_target).strip().lower()]
                return data[target_name]["running"], data[target_name]["tok_s"], target_name

    m_no_think = m_str.replace("-thinking", "").replace("_thinking", "")
    if m_no_think in norm_data:
        target_name = norm_data[m_no_think]
        return data[target_name]["running"], data[target_name]["tok_s"], target_name

    canon_no_think = canon_str.replace("-thinking", "").replace("_thinking", "")
    if canon_no_think in norm_data:
        target_name = norm_data[canon_no_think]
        return data[target_name]["running"], data[target_name]["tok_s"], target_name

    for norm_name, original_name in norm_data.items():
        if norm_name == m_str or norm_name == canon_str:
            return data[original_name]["running"], data[original_name]["tok_s"], original_name

    return None, None, None
