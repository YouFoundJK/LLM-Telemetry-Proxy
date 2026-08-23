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
from typing import Optional, Dict, Any, Tuple, List, Union
import asyncio
import json
import random
import sqlite3
import sys
import time
import argparse
import atexit
import os
import signal
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

import aiohttp
from aiohttp import web

try:
    from proxy.model_router import ModelRouter, build_upstream_url
except ImportError:
    from model_router import ModelRouter, build_upstream_url

# ── Config ──────────────────────────────────────────────────────────────────
# UPSTREAM = "https://llm-dev.ai.e-infra.cz/v1"
# STATUS_API = "https://llm-dev.ai.e-infra.cz/status/api/v1/models"
DEFAULT_UPSTREAM = "https://llm.ai.e-infra.cz/v1"
UPSTREAM = DEFAULT_UPSTREAM
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
TOKEN_BUDGET_FILE = REPO_ROOT / "data" / "token_budget.json"
ROUTES_CONFIG_FILE = REPO_ROOT / "data" / "model_routes.json"

# Dynamic Model Router instance
_model_router = ModelRouter(config_path=ROUTES_CONFIG_FILE)
if not ROUTES_CONFIG_FILE.exists():
    _model_router.default_upstream_url = UPSTREAM

# ── Rate Limiting & Concurrency Control ──────────────────────────────────────
# e-INFRA enforces max 4 parallel requests per API key.
# When concurrency is maxed out and queued requests are waiting, a slot cooldown
# delay (default 50ms) is strictly enforced upon slot release before dispatching the
# slot to the next queued request. When active concurrency is below max_concurrent,
# incoming requests are admitted immediately without delay.
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", 3))
SLOT_COOLDOWN_MS = int(os.environ.get("CONCURRENCY_SLOT_COOLDOWN_MS", 50))
RETRY_429_MAX = int(os.environ.get("RETRY_429_MAX", 3))
UPSTREAM_SESSION_KEY = web.AppKey("upstream_session", aiohttp.ClientSession) if hasattr(web, "AppKey") else "upstream_session"


class UpstreamConcurrencyLimiter:
    """
    Asynchronous concurrency limiter with strict FIFO queuing, slot cooldown gaps,
    and cancellation safety.
    """

    def __init__(self, max_concurrent: int = 4, slot_cooldown_seconds: float = 0.05):
        self.max_concurrent = max(1, int(max_concurrent))
        self.slot_cooldown_seconds = max(0.0, float(slot_cooldown_seconds))
        self._active_count = 0
        self._waiters = deque()  # deque of asyncio.Future
        self._lock = asyncio.Lock()
        self._last_release_time = 0.0
        self._total_admitted = 0
        self._total_queued = 0
        self._peak_active = 0
        self._total_retries_429 = 0
        self._total_retries_attempted = 0
        self._total_retries_absorbed = 0
        self._total_retries_failed = 0

    @property
    def active(self) -> int:
        return self._active_count

    @property
    def queued(self) -> int:
        return len(self._waiters)

    def record_429_retry(self):
        self._total_retries_429 += 1
        self._total_retries_attempted += 1

    def record_retry_attempt(self):
        self._total_retries_429 += 1
        self._total_retries_attempted += 1

    def record_retry_absorbed(self):
        self._total_retries_absorbed += 1

    def record_retry_failed(self):
        self._total_retries_failed += 1

    def get_stats(self) -> dict:
        return {
            "max_concurrent": self.max_concurrent,
            "active": self._active_count,
            "queued": len(self._waiters),
            "slot_cooldown_ms": int(round(self.slot_cooldown_seconds * 1000)),
            "total_admitted": self._total_admitted,
            "total_queued": self._total_queued,
            "total_retries_429": self._total_retries_429,
            "total_retries_attempted": self._total_retries_attempted,
            "total_retries_absorbed": self._total_retries_absorbed,
            "total_retries_failed": self._total_retries_failed,
            "peak_active": self._peak_active,
        }

    def slot(self):
        """Returns an async context manager for acquiring and releasing a concurrency slot."""
        return _SlotContextManager(self)

    async def acquire(self):
        """Acquire a concurrency slot, waiting in FIFO order if max_concurrent is reached."""
        async with self._lock:
            # If we have capacity and no waiters are queued, admit immediately without delay!
            if self._active_count < self.max_concurrent and not self._waiters:
                self._active_count += 1
                self._total_admitted += 1
                self._peak_active = max(self._peak_active, self._active_count)
                return

            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            self._waiters.append(fut)
            self._total_queued += 1

        try:
            await fut
        except asyncio.CancelledError:
            async with self._lock:
                if fut in self._waiters:
                    self._waiters.remove(fut)
                elif fut.done() and not fut.cancelled():
                    # The future was resolved with a slot just before/during cancellation.
                    # Release the slot so capacity is not leaked.
                    self._schedule_handover_or_decrement()
            raise

    async def release(self):
        """Release a concurrency slot."""
        async with self._lock:
            self._schedule_handover_or_decrement()

    def _schedule_handover_or_decrement(self):
        """Must be called while holding self._lock."""
        self._last_release_time = time.monotonic()
        if not self._waiters:
            self._active_count = max(0, self._active_count - 1)
            return

        # Queue has waiters (it was maxed out). Spawn cooldown handover task.
        asyncio.create_task(self._cooldown_and_dispatch())

    async def _cooldown_and_dispatch(self):
        if self.slot_cooldown_seconds > 0:
            await asyncio.sleep(self.slot_cooldown_seconds)

        async with self._lock:
            while self._waiters:
                fut = self._waiters.popleft()
                if not fut.done() and not fut.cancelled():
                    self._total_admitted += 1
                    fut.set_result(None)
                    return
            # If all waiters were cancelled during cooldown
            self._active_count = max(0, self._active_count - 1)


class _SlotContextManager:
    def __init__(self, limiter: UpstreamConcurrencyLimiter):
        self.limiter = limiter

    async def __aenter__(self):
        await self.limiter.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.limiter.release()


def evaluate_retry_condition(
    status_code: Optional[int],
    headers: Optional[Any],
    body_text_or_json: Optional[Any],
    attempt: int,
    retry_policy: Optional[dict],
) -> tuple:
    """
    Evaluates whether an upstream response qualifies as a transient rate-limit / capacity hiccup
    and calculates the retry delay (or 0.0s for immediate zero-delay retry).

    Returns:
        (should_retry: bool, retry_delay_seconds: float, reason: Optional[str])
    """
    if not retry_policy or not retry_policy.get("enabled", True):
        return False, 0.0, None

    max_retries = int(retry_policy.get("max_retries", 0))
    if attempt >= max_retries or max_retries <= 0:
        return False, 0.0, None

    retry_on_status = set(retry_policy.get("retry_on_status", [429, 502, 503, 504, 529]))
    retry_patterns = [p.lower() for p in retry_policy.get("retry_on_body_patterns", [])]
    retry_on_empty = bool(retry_policy.get("retry_on_empty", True))
    max_retry_after = float(retry_policy.get("max_retry_after_seconds", 5.0))
    mode = str(retry_policy.get("mode", "immediate")).lower()

    is_retryable = False
    reason = None

    if status_code in retry_on_status:
        is_retryable = True
        reason = f"HTTP {status_code}"

    # Check for empty response body (0 bytes / whitespace)
    if not is_retryable and retry_on_empty:
        if body_text_or_json is None:
            if status_code and status_code >= 400:
                is_retryable = True
                reason = f"HTTP {status_code} with no body"
        elif isinstance(body_text_or_json, (bytes, str)) and not body_text_or_json.strip():
            is_retryable = True
            reason = "Empty response returned from upstream (0 bytes)"

    # Check body content, error objects, and choices
    if not is_retryable and body_text_or_json:
        body_str = ""
        if isinstance(body_text_or_json, dict):
            err_val = body_text_or_json.get("error", "")
            detail_val = body_text_or_json.get("detail", "")
            msg_val = body_text_or_json.get("message", "")
            choices_val = ""
            choices = body_text_or_json.get("choices")
            if isinstance(choices, list):
                if len(choices) == 0 and retry_on_empty:
                    is_retryable = True
                    reason = "Empty choices list returned from upstream"
                elif len(choices) > 0 and isinstance(choices[0], dict):
                    msg = choices[0].get("message")
                    delta = choices[0].get("delta")
                    txt = choices[0].get("text")
                    c_text = None
                    tc_val = None
                    if isinstance(msg, dict):
                        c_text = msg.get("content")
                        tc_val = msg.get("tool_calls")
                    elif isinstance(delta, dict):
                        c_text = delta.get("content")
                        tc_val = delta.get("tool_calls")
                    elif isinstance(txt, str):
                        c_text = txt
                    
                    if c_text is not None:
                        choices_val = str(c_text)
                    
                    if retry_on_empty and (c_text is None or (isinstance(c_text, str) and not c_text.strip())) and not tc_val:
                        is_retryable = True
                        reason = "Empty content/no response returned in upstream choice"

            body_str = f"{err_val} {detail_val} {msg_val} {choices_val}".lower()
        elif isinstance(body_text_or_json, str):
            body_str = body_text_or_json.lower()
        elif isinstance(body_text_or_json, bytes):
            try:
                body_str = body_text_or_json.decode("utf-8", errors="ignore").lower()
            except Exception:
                pass

        if not is_retryable:
            for pat in retry_patterns:
                if pat in body_str:
                    is_retryable = True
                    reason = f"Body pattern match: '{pat}'"
                    break

    if not is_retryable:
        return False, 0.0, None

    # Evaluate delay
    delay = 0.0
    retry_after_hdr = None
    if headers:
        for k, v in (headers.items() if hasattr(headers, "items") else []):
            if str(k).lower() in ("retry-after", "x-ratelimit-reset-requests", "ratelimit-reset-requests"):
                retry_after_hdr = v
                break

    if retry_after_hdr:
        try:
            parsed_after = float(retry_after_hdr)
            if parsed_after > max_retry_after:
                # Explicit cooldown exceeds acceptable threshold; fail fast
                return False, 0.0, None
            delay = max(0.0, parsed_after)
        except (ValueError, TypeError):
            pass

    if delay == 0.0:
        if mode == "immediate":
            delay = 0.0  # Instantaneous re-dispatch
        else:
            delay = 0.5 * (2 ** attempt) + random.uniform(0.05, 0.2)

    return True, delay, reason


_concurrency_limiter = _model_router.default_limiter
_concurrency_limiter.max_concurrent = MAX_CONCURRENT
_concurrency_limiter.slot_cooldown_seconds = SLOT_COOLDOWN_MS / 1000.0
# Alias for backwards-compatibility if referenced elsewhere
_upstream_semaphore = _concurrency_limiter

# ── Token Budget Enforcement ─────────────────────────────────────────────────
# Hard cap: 480M tokens per day. When exceeded, proxy rejects with clear error.
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

    def __init__(self, daily_limit: int, db_path: Path = DB_PATH, state_file: Path = TOKEN_BUDGET_FILE):
        self.daily_limit = daily_limit
        self.db_path = db_path
        self.state_file = state_file
        self._usage = deque()  # (timestamp_float, token_count)
        self._load_state()

    def _load_state(self):
        """Restore token budget from SQLite DB and state file for the current UTC day."""
        now_utc = datetime.now(timezone.utc)
        start_of_today = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff = start_of_today.timestamp()
        cutoff_iso = start_of_today.isoformat()
        temp_usage = []

        # 1. First attempt: Query SQLite DB for calls within the current UTC day
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
                        ts_float = dt.timestamp()
                        if ts_float >= cutoff:
                            temp_usage.append((ts_float, total_tok))
                    except Exception:
                        pass
        except Exception as e:
            print(f"[telemetry] Error loading token budget from DB: {e}", file=sys.stderr)

        # 2. Fallback: Read from state_file if DB had no records
        if not temp_usage and self.state_file and Path(self.state_file).exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    records = data.get("recent_usage", [])
                    for r in records:
                        ts = r.get("ts", 0)
                        cnt = r.get("tokens", 0)
                        if ts >= cutoff and cnt > 0:
                            temp_usage.append((ts, cnt))
            except Exception as e:
                print(f"[telemetry] Error reading {self.state_file}: {e}", file=sys.stderr)

        if temp_usage:
            self._usage = deque(sorted(temp_usage, key=lambda x: x[0]))

        self._save_state()

    def _save_state(self):
        """Save current token budget summary & recent history to data/token_budget.json."""
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
        """
        Record usage and check if within budget.
        Returns (allowed: bool, status: dict).
        Status includes: total_used, remaining, percentage_used, daily_limit.
        """
        now = time.time()
        total = input_tokens + output_tokens
        self._usage.append((now, total))

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

        if current_usage >= self.daily_limit:
            return False, status
        return True, status

    def get_status(self) -> dict:
        """Get current budget status without recording usage."""
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
            calls_count   INTEGER DEFAULT 1,
            route_name    TEXT,
            upstream_url  TEXT
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
    try:
        conn.execute("ALTER TABLE api_calls ADD COLUMN route_name TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE api_calls ADD COLUMN upstream_url TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE api_calls ADD COLUMN retries_attempted INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE api_calls ADD COLUMN absorbed_429 INTEGER DEFAULT 0")
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
            calls_count   INTEGER DEFAULT 1,
            route_name    TEXT,
            upstream_url  TEXT,
            retries_attempted INTEGER DEFAULT 0,
            absorbed_429  INTEGER DEFAULT 0
        )
    """)
    try:
        conn.execute("ALTER TABLE proxy_calls ADD COLUMN calls_count INTEGER DEFAULT 1")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE proxy_calls ADD COLUMN route_name TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE proxy_calls ADD COLUMN upstream_url TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE proxy_calls ADD COLUMN retries_attempted INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE proxy_calls ADD COLUMN absorbed_429 INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON api_calls(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_model ON api_calls(model)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_type ON api_calls(call_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_api_calls_ts_id ON api_calls(timestamp DESC, id DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_api_calls_model_ts ON api_calls(model, timestamp DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_proxy_ts ON proxy_calls(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_proxy_calls_ts_id ON proxy_calls(timestamp DESC, id DESC)")

    # Database Metadata Table for stable fingerprinting and compaction tracking
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _telemetry_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
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
    candidates = [
        REPO_ROOT / "data" / "model_mapping.json",
        REPO_ROOT / "model_mapping.json",
        REPO_ROOT / "data" / "model_mappings.json",
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


def log_call(model, endpoint, input_tokens, output_tokens,
             ttfb_ms, total_ms, tokens_per_s,
             server_running, server_tok_s, server_model,
             status_code, error, call_type='chat',
             route_name=None, upstream_url=None,
             retries_attempted=0, absorbed_429=0):
    try:
        model = resolve_canonical_model(model)
        conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
        try:
            conn.execute("""
                INSERT INTO api_calls
                    (timestamp, model, endpoint, input_tokens, output_tokens,
                     ttfb_ms, total_ms, tokens_per_s,
                     server_running, server_tok_s, server_model,
                     status_code, error, call_type, calls_count,
                     route_name, upstream_url, retries_attempted, absorbed_429)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            """, (
                datetime.now(timezone.utc).isoformat(),
                model, endpoint, input_tokens, output_tokens,
                ttfb_ms, total_ms, tokens_per_s,
                server_running, server_tok_s, server_model,
                status_code, error, call_type,
                route_name, upstream_url,
                int(retries_attempted or 0), int(absorbed_429 or 0)
            ))
        except sqlite3.OperationalError as op_err:
            err_msg = str(op_err)
            if "retries_attempted" in err_msg or "absorbed_429" in err_msg or "route_name" in err_msg:
                try:
                    conn.execute("ALTER TABLE api_calls ADD COLUMN route_name TEXT")
                except Exception:
                    pass
                try:
                    conn.execute("ALTER TABLE api_calls ADD COLUMN upstream_url TEXT")
                except Exception:
                    pass
                try:
                    conn.execute("ALTER TABLE api_calls ADD COLUMN retries_attempted INTEGER DEFAULT 0")
                except Exception:
                    pass
                try:
                    conn.execute("ALTER TABLE api_calls ADD COLUMN absorbed_429 INTEGER DEFAULT 0")
                except Exception:
                    pass
                try:
                    conn.execute("""
                        INSERT INTO api_calls
                            (timestamp, model, endpoint, input_tokens, output_tokens,
                             ttfb_ms, total_ms, tokens_per_s,
                             server_running, server_tok_s, server_model,
                             status_code, error, call_type, calls_count,
                             route_name, upstream_url, retries_attempted, absorbed_429)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                    """, (
                        datetime.now(timezone.utc).isoformat(),
                        model, endpoint, input_tokens, output_tokens,
                        ttfb_ms, total_ms, tokens_per_s,
                        server_running, server_tok_s, server_model,
                        status_code, error, call_type,
                        route_name, upstream_url,
                        int(retries_attempted or 0), int(absorbed_429 or 0)
                    ))
                except Exception:
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
                        status_code, error, call_type
                    ))
            else:
                raise
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[telemetry] log_call error: {e}", file=sys.stderr)


def log_proxy_call(endpoint, method, call_type, model, status_code, error, logged, ttfb_ms, total_ms,
                   route_name=None, upstream_url=None,
                   retries_attempted=0, absorbed_429=0):
    """Log EVERY request through the proxy — even ones that fail before logging to api_calls."""
    try:
        model = resolve_canonical_model(model)
        conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
        try:
            conn.execute("""
                INSERT INTO proxy_calls
                    (timestamp, endpoint, method, call_type, model, status_code, error, logged, ttfb_ms, total_ms, calls_count,
                     route_name, upstream_url, retries_attempted, absorbed_429)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            """, (
                datetime.now(timezone.utc).isoformat(),
                endpoint, method, call_type, model, status_code, error, logged, ttfb_ms, total_ms,
                route_name, upstream_url,
                int(retries_attempted or 0), int(absorbed_429 or 0)
            ))
        except sqlite3.OperationalError as op_err:
            err_msg = str(op_err)
            if "retries_attempted" in err_msg or "absorbed_429" in err_msg or "route_name" in err_msg:
                try:
                    conn.execute("ALTER TABLE proxy_calls ADD COLUMN route_name TEXT")
                except Exception:
                    pass
                try:
                    conn.execute("ALTER TABLE proxy_calls ADD COLUMN upstream_url TEXT")
                except Exception:
                    pass
                try:
                    conn.execute("ALTER TABLE proxy_calls ADD COLUMN retries_attempted INTEGER DEFAULT 0")
                except Exception:
                    pass
                try:
                    conn.execute("ALTER TABLE proxy_calls ADD COLUMN absorbed_429 INTEGER DEFAULT 0")
                except Exception:
                    pass
                try:
                    conn.execute("""
                        INSERT INTO proxy_calls
                            (timestamp, endpoint, method, call_type, model, status_code, error, logged, ttfb_ms, total_ms, calls_count,
                             route_name, upstream_url, retries_attempted, absorbed_429)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                    """, (
                        datetime.now(timezone.utc).isoformat(),
                        endpoint, method, call_type, model, status_code, error, logged, ttfb_ms, total_ms,
                        route_name, upstream_url,
                        int(retries_attempted or 0), int(absorbed_429 or 0)
                    ))
                except Exception:
                    conn.execute("""
                        INSERT INTO proxy_calls
                            (timestamp, endpoint, method, call_type, model, status_code, error, logged, ttfb_ms, total_ms, calls_count)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """, (
                        datetime.now(timezone.utc).isoformat(),
                        endpoint, method, call_type, model, status_code, error, logged, ttfb_ms, total_ms
                    ))
            else:
                raise
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
    """Fetch server load from status API for e-INFRA models. Returns (running, tok_s, model_name) or (None, None, None)."""
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
                        if not isinstance(m, dict):
                            continue
                        # Rigorous filtering: ONLY active online nodes are valid for live telemetry
                        if m.get("status") not in ("online",):
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

    # Normalized lookup map: lowercase_name -> original_name in data
    norm_data = {k.lower().strip(): k for k in data.keys()}

    m_str = str(model_hint).strip().lower()
    canon_str = str(resolve_canonical_model(model_hint)).strip().lower()

    # 1. Exact direct match
    if m_str in norm_data:
        target_name = norm_data[m_str]
        d = data[target_name]
        return d["running"], d["tok_s"], target_name

    # 2. Canonical alias match (e.g. 'deepseek' -> 'deepseek-v4-flash', 'kimi' -> 'kimi-k3')
    if canon_str in norm_data:
        target_name = norm_data[canon_str]
        d = data[target_name]
        return d["running"], d["tok_s"], target_name

    # 3. Strip thinking suffix (e.g. 'deepseek-v4-flash-thinking' -> 'deepseek-v4-flash')
    m_no_think = m_str.replace("-thinking", "").replace("_thinking", "")
    if m_no_think in norm_data:
        target_name = norm_data[m_no_think]
        d = data[target_name]
        return d["running"], d["tok_s"], target_name

    canon_no_think = canon_str.replace("-thinking", "").replace("_thinking", "")
    if canon_no_think in norm_data:
        target_name = norm_data[canon_no_think]
        d = data[target_name]
        return d["running"], d["tok_s"], target_name

    # 4. Check if any online model equals raw or canon model
    for norm_name, original_name in norm_data.items():
        if norm_name == m_str or norm_name == canon_str:
            d = data[original_name]
            return d["running"], d["tok_s"], original_name

    # Model is unmonitored, external, or not hosted on e-INFRA -> return None cleanly
    return None, None, None


# ── Raw Payload Logging Helpers ─────────────────────────────────────────────
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
            entries.append(json.loads(l))
        except Exception:
            pass

    return list(reversed(entries)), total_count


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
            
            # Approximate prompt input tokens from payload if possible
            if isinstance(payload, dict):
                prompt_text = ""
                if "messages" in payload and isinstance(payload["messages"], list):
                    for msg in payload["messages"]:
                        if isinstance(msg, dict):
                            content = msg.get("content", "")
                            if isinstance(content, str):
                                prompt_text += content + " "
                            elif isinstance(content, list):
                                for part in content:
                                    if isinstance(part, dict) and part.get("text"):
                                        prompt_text += str(part["text"]) + " "
                elif "prompt" in payload:
                    p = payload["prompt"]
                    if isinstance(p, str):
                        prompt_text = p
                    elif isinstance(p, list):
                        prompt_text = " ".join(str(x) for x in p)
                elif "input" in payload:
                    inp = payload["input"]
                    if isinstance(inp, str):
                        prompt_text = inp
                    elif isinstance(inp, list):
                        prompt_text = " ".join(str(x) for x in inp)
                if prompt_text:
                    input_tokens = max(1, len(prompt_text) // 4)
    except (json.JSONDecodeError, KeyError):
        pass

    # Dynamic model route resolution
    route_res = _model_router.resolve(model)
    route_name = route_res.route_name
    resolved_base = UPSTREAM if (route_res.is_default and UPSTREAM != DEFAULT_UPSTREAM) else route_res.upstream_url
    upstream_url = build_upstream_url(resolved_base, path)

    # Server load telemetry (strictly applicable for e-INFRA cluster upstreams)
    is_einfra = ("e-infra.cz" in resolved_base.lower()) or (resolved_base.strip() == DEFAULT_UPSTREAM.strip())
    if is_einfra:
        server_running, server_tok_s, server_model = await fetch_server_load(model)
    else:
        server_running, server_tok_s, server_model = None, None, None

    headers = dict(request.headers)
    headers.pop("Host", None)
    headers.pop("host", None)
    if body and "Content-Length" in headers:
        headers["Content-Length"] = str(len(body))

    req_id = f"req_{uuid.uuid4().hex[:12]}"
    req_seq = next_raw_payload_seq()
    t_start = time.monotonic()
    ttfb_ms = None
    status_code = None
    error = None
    output_tokens = None
    reasoning_tokens = None
    tokens_per_s = None
    logged = False

    is_stream_req = bool(payload.get("stream") if isinstance(payload, dict) else False)
    if _raw_logging_enabled and _raw_subscribers:
        start_record = make_raw_payload_start_record(
            req_id=req_id,
            path=path,
            method=method,
            call_type=call_type,
            model=model,
            client_ip=request.remote,
            req_headers=dict(request.headers),
            payload_obj=payload,
            is_stream=is_stream_req,
            seq=req_seq,
        )
        asyncio.create_task(broadcast_raw_payload(start_record))

    try:
        timeout = aiohttp.ClientTimeout(total=300)
        target_limiter = _model_router.get_limiter(route_res.route_id)
        retry_policy = dict(route_res.retry_policy or _model_router.default_retry_policy)
        if route_res.is_default:
            if RETRY_429_MAX == 0:
                retry_policy["enabled"] = False
                retry_policy["max_retries"] = 0
            else:
                retry_policy["max_retries"] = RETRY_429_MAX

        max_retries = int(retry_policy.get("max_retries", 0)) if retry_policy.get("enabled", True) else 0

        attempt = 0
        while attempt <= max_retries:
            retry_needed = False
            retry_delay = 0.0
            retry_reason = None

            # Gate: never exceed route-specific max_concurrent parallel upstream requests
            async with target_limiter.slot():
                req_session = request.app.get(UPSTREAM_SESSION_KEY) if hasattr(request, "app") and UPSTREAM_SESSION_KEY in request.app else None
                owns_session = False
                if req_session is None or req_session.closed:
                    req_session = aiohttp.ClientSession(timeout=timeout)
                    owns_session = True

                try:
                    async with req_session.request(
                        method, upstream_url,
                        headers=headers,
                        data=body if body else None,
                        params=request.query,
                    ) as upstream_resp:
                        status_code = upstream_resp.status
                        content_type = upstream_resp.headers.get("Content-Type", "")
                        is_stream_candidate = "text/event-stream" in content_type and status_code == 200

                        # Evaluate if HTTP status triggers a retry
                        sniff_needed, sniff_delay, sniff_reason = evaluate_retry_condition(
                            status_code=status_code,
                            headers=upstream_resp.headers,
                            body_text_or_json=None,
                            attempt=attempt,
                            retry_policy=retry_policy,
                        )

                        if sniff_needed and attempt < max_retries:
                            target_limiter.record_retry_attempt()
                            print(
                                f"[telemetry] Upstream rate limit on {path} ({sniff_reason}, attempt {attempt+1}/{max_retries+1}). "
                                f"Re-dispatching with delay={sniff_delay:.2f}s...",
                                file=sys.stderr,
                            )
                            retry_needed = True
                            retry_delay = sniff_delay
                            retry_reason = sniff_reason

                        elif is_stream_candidate:
                            # ── Streaming Handler with Deferred Client Handshake ───────────
                            buffered_chunks = []
                            stream_retry_needed = False
                            stream_delay = 0.0
                            stream_reason = None

                            async for chunk in upstream_resp.content:
                                buffered_chunks.append(chunk)
                                try:
                                    text = chunk.decode("utf-8", errors="replace")
                                    for line in text.split("\n"):
                                        if line.startswith("data: ") and line.strip() != "data: [DONE]":
                                            chunk_data = json.loads(line[6:])
                                            if isinstance(chunk_data, dict):
                                                s_needed, s_delay, s_reason = evaluate_retry_condition(
                                                    status_code=None,
                                                    headers=upstream_resp.headers,
                                                    body_text_or_json=chunk_data,
                                                    attempt=attempt,
                                                    retry_policy=retry_policy,
                                                )
                                                if s_needed:
                                                    stream_retry_needed = True
                                                    stream_delay = s_delay
                                                    stream_reason = s_reason
                                                    break
                                except Exception:
                                    pass
                                if stream_retry_needed or buffered_chunks:
                                    break

                            # If upstream immediately closed stream with 0 chunks or only [DONE] without content
                            if not stream_retry_needed and retry_policy.get("retry_on_empty", True):
                                if not buffered_chunks:
                                    stream_retry_needed = True
                                    stream_delay = 0.0
                                    stream_reason = "Upstream closed SSE stream with 0 chunks"
                                else:
                                    all_done = True
                                    for b_chk in buffered_chunks:
                                        t_chk = b_chk.decode("utf-8", errors="ignore").strip()
                                        if t_chk and t_chk != "data: [DONE]":
                                            all_done = False
                                            break
                                    if all_done:
                                        stream_retry_needed = True
                                        stream_delay = 0.0
                                        stream_reason = "Upstream sent empty stream (0 tokens before [DONE])"

                            if stream_retry_needed and attempt < max_retries:
                                target_limiter.record_retry_attempt()
                                print(
                                    f"[telemetry] Upstream SSE rate-limit chunk intercepted on {path} ({stream_reason}, attempt {attempt+1}/{max_retries+1}). "
                                    f"Re-dispatching stream with delay={stream_delay:.2f}s...",
                                    file=sys.stderr,
                                )
                                retry_needed = True
                                retry_delay = stream_delay
                                retry_reason = stream_reason
                            else:
                                if attempt > 0:
                                    target_limiter.record_retry_absorbed()

                                stream_headers = {
                                    "Content-Type": content_type,
                                    "Cache-Control": "no-cache",
                                    "Connection": "keep-alive",
                                    "X-Proxy-Retries-Attempted": str(attempt),
                                    "X-Proxy-Rate-Limit-Absorbed": "1" if attempt > 0 else "0",
                                }
                                response = web.StreamResponse(
                                    status=upstream_resp.status,
                                    headers=stream_headers,
                                )
                                await response.prepare(request)

                                t_first_byte = None
                                collected_usage = None
                                content_chars = 0
                                collected_content = ""
                                collected_reasoning = ""
                                collected_tool_calls = []

                                def _parse_sse_chunk(raw_bytes: bytes):
                                    nonlocal t_first_byte, collected_usage, content_chars, collected_content, collected_reasoning, error
                                    if t_first_byte is None:
                                        t_first_byte = time.monotonic()
                                    try:
                                        text = raw_bytes.decode("utf-8", errors="replace")
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

                                for b_chunk in buffered_chunks:
                                    await response.write(b_chunk)
                                    _parse_sse_chunk(b_chunk)

                                async for chunk in upstream_resp.content:
                                    await response.write(chunk)
                                    _parse_sse_chunk(chunk)

                                await response.write_eof()

                                # Telemetry post-processing
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
                                    elif content_chars > 0 and not output_tokens:
                                        output_tokens = max(1, content_chars // 4)

                                    # Record token budget
                                    try:
                                        _token_budget.record_and_check(input_tokens or 0, output_tokens or 0)
                                    except Exception as b_err:
                                        print(f"[telemetry] token budget record error: {b_err}", file=sys.stderr)

                                    t_total = (time.monotonic() - t_start) * 1000
                                    if t_first_byte is None:
                                        t_first_byte = time.monotonic()
                                    ttfb_ms = (t_first_byte - t_start) * 1000
                                    if output_tokens and t_total > 0:
                                        tokens_per_s = output_tokens / (t_total / 1000)

                                    log_call(model, path, input_tokens, output_tokens,
                                             ttfb_ms, t_total, tokens_per_s,
                                             server_running, server_tok_s, server_model,
                                             status_code, error, call_type,
                                             route_name=route_name, upstream_url=upstream_url,
                                             retries_attempted=attempt, absorbed_429=1 if attempt > 0 else 0)
                                    logged = True
                                    log_proxy_call(path, method, call_type, model, status_code, error, 1, ttfb_ms, t_total,
                                                   route_name=route_name, upstream_url=upstream_url,
                                                   retries_attempted=attempt, absorbed_429=1 if attempt > 0 else 0)

                                    if _raw_logging_enabled:
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
                                            seq=req_seq,
                                        )
                                        append_raw_payload(raw_record)
                                        if _raw_subscribers:
                                            asyncio.create_task(broadcast_raw_payload(raw_record))
                                except Exception as tel_err:
                                    print(f"[telemetry] streaming telemetry error: {tel_err}", file=sys.stderr)

                                return response

                        else:
                            # ── Non-Streaming Handler ──────────────────────────────────────
                            resp_body = await upstream_resp.read()
                            t_first_byte = time.monotonic()
                            ttfb_ms = (t_first_byte - t_start) * 1000
                            t_total = (time.monotonic() - t_start) * 1000

                            resp_data = None
                            try:
                                resp_data = json.loads(resp_body)
                            except Exception:
                                pass

                            # Evaluate body content for rate limits
                            b_needed, b_delay, b_reason = evaluate_retry_condition(
                                status_code=status_code,
                                headers=upstream_resp.headers,
                                body_text_or_json=resp_data if resp_data else resp_body,
                                attempt=attempt,
                                retry_policy=retry_policy,
                            )

                            if b_needed and attempt < max_retries:
                                target_limiter.record_retry_attempt()
                                print(
                                    f"[telemetry] Upstream body rate-limit intercepted on {path} ({b_reason}, attempt {attempt+1}/{max_retries+1}). "
                                    f"Re-dispatching with delay={b_delay:.2f}s...",
                                    file=sys.stderr,
                                )
                                retry_needed = True
                                retry_delay = b_delay
                                retry_reason = b_reason
                            else:
                                if attempt > 0:
                                    if status_code and 200 <= status_code < 300:
                                        target_limiter.record_retry_absorbed()
                                    else:
                                        target_limiter.record_retry_failed()

                                resp_headers = {}
                                for k, v in upstream_resp.headers.items():
                                    if k.lower() not in ("content-length", "content-encoding", "transfer-encoding"):
                                        resp_headers[k] = v
                                resp_headers["X-Proxy-Retries-Attempted"] = str(attempt)
                                resp_headers["X-Proxy-Rate-Limit-Absorbed"] = "1" if (attempt > 0 and status_code and 200 <= status_code < 300) else "0"

                                # Telemetry post-processing (isolated)
                                budget_headers = {}
                                allowed = True
                                try:
                                    resp_text = None
                                    resp_reasoning = None
                                    resp_tool_calls = None
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
                                        if not output_tokens and resp_text:
                                            output_tokens = max(1, len(resp_text) // 4)
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
                                    elif status_code and (status_code < 200 or status_code >= 300):
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
                                             status_code, error, call_type,
                                             route_name=route_name, upstream_url=upstream_url,
                                             retries_attempted=attempt,
                                             absorbed_429=1 if (attempt > 0 and status_code and 200 <= status_code < 300) else 0)
                                    logged = True
                                    log_proxy_call(path, method, call_type, model, status_code, error, 1, ttfb_ms, t_total,
                                                   route_name=route_name, upstream_url=upstream_url,
                                                   retries_attempted=attempt,
                                                   absorbed_429=1 if (attempt > 0 and status_code and 200 <= status_code < 300) else 0)

                                    if _raw_logging_enabled:
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
                                            seq=req_seq,
                                        )
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

                                try:
                                    return web.Response(
                                        status=upstream_resp.status,
                                        body=resp_body,
                                        headers=resp_headers,
                                    )
                                except Exception:
                                    return web.Response(
                                        status=upstream_resp.status,
                                        body=resp_body,
                                    )
                except (aiohttp.ClientError, ConnectionResetError, ConnectionRefusedError, BrokenPipeError,
                        asyncio.IncompleteReadError, asyncio.TimeoutError) as net_err:
                    if attempt < max_retries and retry_policy.get("retry_on_disconnect", True):
                        target_limiter.record_retry_attempt()
                        retry_needed = True
                        retry_delay = 0.0 if str(retry_policy.get("mode", "immediate")).lower() == "immediate" else (0.5 * (2 ** attempt) + random.uniform(0.05, 0.2))
                        retry_reason = f"Upstream disconnect/no response: {type(net_err).__name__} ({net_err})"
                        print(
                            f"[telemetry] Upstream connection dropped on {path} ({retry_reason}, attempt {attempt+1}/{max_retries+1}). "
                            f"Re-dispatching with delay={retry_delay:.2f}s...",
                            file=sys.stderr,
                        )
                    else:
                        raise
                finally:
                    if owns_session and not req_session.closed:
                        await req_session.close()

            if retry_needed:
                attempt += 1
                if retry_delay > 0:
                    await asyncio.sleep(retry_delay)
                continue
            else:
                break

    except asyncio.TimeoutError:
        error = "timeout"
        t_total = (time.monotonic() - t_start) * 1000
        try:
            log_call(model, path, input_tokens, output_tokens,
                     ttfb_ms, t_total, None,
                     server_running, server_tok_s, server_model,
                     504, error, call_type,
                     route_name=route_name, upstream_url=upstream_url)
            log_proxy_call(path, method, call_type, model, 504, error, 1 if logged else 0, ttfb_ms, t_total,
                           route_name=route_name, upstream_url=upstream_url)
        except Exception:
            pass

        if _raw_logging_enabled:
            err_record = make_raw_payload_record(
                req_id=req_id,
                path=path,
                method=method,
                call_type=call_type,
                model=model,
                client_ip=request.remote,
                req_headers=dict(request.headers),
                payload_obj=payload,
                status_code=504,
                resp_headers={},
                is_stream=is_stream_req,
                ttfb_ms=ttfb_ms,
                total_ms=t_total,
                tokens_per_s=None,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                reasoning_tokens=reasoning_tokens,
                content_text="",
                reasoning_text="",
                tool_calls=None,
                raw_resp_json=None,
                error="upstream timeout",
                seq=req_seq,
            )
            append_raw_payload(err_record)
            if _raw_subscribers:
                asyncio.create_task(broadcast_raw_payload(err_record))

        return web.json_response({"error": {"message": "upstream timeout"}}, status=504)

    except asyncio.CancelledError:
        error = "client_cancelled"
        t_total = (time.monotonic() - t_start) * 1000
        try:
            log_call(model, path, input_tokens, output_tokens,
                     ttfb_ms, t_total, None,
                     server_running, server_tok_s, server_model,
                     499, error, call_type,
                     route_name=route_name, upstream_url=upstream_url)
            log_proxy_call(path, method, call_type, model, 499, error, 1 if logged else 0, ttfb_ms, t_total,
                           route_name=route_name, upstream_url=upstream_url)
        except Exception:
            pass

        if _raw_logging_enabled:
            err_record = make_raw_payload_record(
                req_id=req_id,
                path=path,
                method=method,
                call_type=call_type,
                model=model,
                client_ip=request.remote,
                req_headers=dict(request.headers),
                payload_obj=payload,
                status_code=499,
                resp_headers={},
                is_stream=is_stream_req,
                ttfb_ms=ttfb_ms,
                total_ms=t_total,
                tokens_per_s=None,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                reasoning_tokens=reasoning_tokens,
                content_text="",
                reasoning_text="",
                tool_calls=None,
                raw_resp_json=None,
                error=error,
                seq=req_seq,
            )
            append_raw_payload(err_record)
            if _raw_subscribers:
                asyncio.create_task(broadcast_raw_payload(err_record))
        raise

    except Exception as e:
        error = str(e)[:200]
        t_total = (time.monotonic() - t_start) * 1000
        try:
            log_call(model, path, input_tokens, output_tokens,
                     ttfb_ms, t_total, None,
                     server_running, server_tok_s, server_model,
                     status_code, error, call_type,
                     route_name=route_name, upstream_url=upstream_url)
            log_proxy_call(path, method, call_type, model, status_code, error, 1 if logged else 0, ttfb_ms, t_total,
                           route_name=route_name, upstream_url=upstream_url)
        except Exception:
            pass

        if _raw_logging_enabled:
            err_record = make_raw_payload_record(
                req_id=req_id,
                path=path,
                method=method,
                call_type=call_type,
                model=model,
                client_ip=request.remote,
                req_headers=dict(request.headers),
                payload_obj=payload,
                status_code=status_code or 502,
                resp_headers={},
                is_stream=is_stream_req,
                ttfb_ms=ttfb_ms,
                total_ms=t_total,
                tokens_per_s=None,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                reasoning_tokens=reasoning_tokens,
                content_text="",
                reasoning_text="",
                tool_calls=None,
                raw_resp_json=None,
                error=error,
                seq=req_seq,
            )
            append_raw_payload(err_record)
            if _raw_subscribers:
                asyncio.create_task(broadcast_raw_payload(err_record))

        return web.json_response({"error": {"message": str(e)}}, status=502)


async def _simple_forward(request, path, method):
    """Forward non-inference calls (model list, props, etc.) without logging to api_calls."""
    route_res = _model_router.resolve(None)
    resolved_base = UPSTREAM if (route_res.is_default and UPSTREAM != DEFAULT_UPSTREAM) else route_res.upstream_url
    upstream_url = build_upstream_url(resolved_base, path)
    headers = dict(request.headers)
    headers.pop("Host", None)
    headers.pop("host", None)

    req_body = await request.read() if request.can_read_body else None
    if req_body and "Content-Length" in headers:
        headers["Content-Length"] = str(len(req_body))

    t_start = time.monotonic()
    status_code = None
    error = None

    try:
        timeout = aiohttp.ClientTimeout(total=300)
        target_limiter = _model_router.get_limiter(route_res.route_id)
        retry_policy = dict(route_res.retry_policy or _model_router.default_retry_policy)
        if route_res.is_default:
            if RETRY_429_MAX == 0:
                retry_policy["enabled"] = False
                retry_policy["max_retries"] = 0
            else:
                retry_policy["max_retries"] = RETRY_429_MAX
        max_retries = int(retry_policy.get("max_retries", 0)) if retry_policy.get("enabled", True) else 0

        attempt = 0
        while attempt <= max_retries:
            retry_needed = False
            retry_delay = 0.0
            retry_reason = None

            async with target_limiter.slot():
                req_session = request.app.get(UPSTREAM_SESSION_KEY) if hasattr(request, "app") and UPSTREAM_SESSION_KEY in request.app else None
                owns_session = False
                if req_session is None or req_session.closed:
                    req_session = aiohttp.ClientSession(timeout=timeout)
                    owns_session = True

                try:
                    async with req_session.request(
                        method, upstream_url,
                        headers=headers,
                        data=req_body if req_body else None,
                        params=request.query,
                    ) as upstream_resp:
                        status_code = upstream_resp.status
                        body = await upstream_resp.read()

                        sniff_needed, sniff_delay, sniff_reason = evaluate_retry_condition(
                            status_code=status_code,
                            headers=upstream_resp.headers,
                            body_text_or_json=body,
                            attempt=attempt,
                            retry_policy=retry_policy,
                        )

                        if sniff_needed and attempt < max_retries:
                            target_limiter.record_retry_attempt()
                            print(
                                f"[telemetry] Upstream rate limit in _simple_forward on {path} ({sniff_reason}, attempt {attempt+1}/{max_retries+1}). "
                                f"Re-dispatching with delay={sniff_delay:.2f}s...",
                                file=sys.stderr,
                            )
                            retry_needed = True
                            retry_delay = sniff_delay
                            retry_reason = sniff_reason
                        else:
                            if attempt > 0:
                                if status_code and 200 <= status_code < 300:
                                    target_limiter.record_retry_absorbed()
                                else:
                                    target_limiter.record_retry_failed()

                            t_total = (time.monotonic() - t_start) * 1000
                            if status_code and (status_code < 200 or status_code >= 300):
                                error = f"HTTP {status_code}"

                            try:
                                log_proxy_call(path, method, classify_endpoint(path), None, status_code, error, 0, None, t_total,
                                               route_name=_model_router.default_name, upstream_url=upstream_url,
                                               retries_attempted=attempt,
                                               absorbed_429=1 if (attempt > 0 and status_code and 200 <= status_code < 300) else 0)
                            except Exception as tel_err:
                                print(f"[telemetry] _simple_forward log error: {tel_err}", file=sys.stderr)

                            simple_headers = {}
                            for k, v in upstream_resp.headers.items():
                                if k.lower() not in (
                                    "content-length", "content-encoding", "transfer-encoding",
                                    "connection", "keep-alive", "upgrade"
                                ):
                                    simple_headers[k] = v
                            simple_headers["X-Proxy-Retries-Attempted"] = str(attempt)
                            simple_headers["X-Proxy-Rate-Limit-Absorbed"] = "1" if (attempt > 0 and status_code and 200 <= status_code < 300) else "0"

                            try:
                                return web.Response(
                                    status=upstream_resp.status,
                                    body=body,
                                    headers=simple_headers,
                                )
                            except Exception:
                                return web.Response(
                                    status=upstream_resp.status,
                                    body=body,
                                )
                except (aiohttp.ClientError, ConnectionResetError, ConnectionRefusedError, BrokenPipeError,
                        asyncio.IncompleteReadError, asyncio.TimeoutError) as net_err:
                    if attempt < max_retries and retry_policy.get("retry_on_disconnect", True):
                        target_limiter.record_retry_attempt()
                        retry_needed = True
                        retry_delay = 0.0 if str(retry_policy.get("mode", "immediate")).lower() == "immediate" else (0.5 * (2 ** attempt) + random.uniform(0.05, 0.2))
                        retry_reason = f"Upstream connection failure: {type(net_err).__name__}"
                        print(
                            f"[telemetry] Upstream connection dropped in _simple_forward on {path} ({retry_reason}, attempt {attempt+1}/{max_retries+1}). "
                            f"Re-dispatching with delay={retry_delay:.2f}s...",
                            file=sys.stderr,
                        )
                    else:
                        raise
                finally:
                    if owns_session and not req_session.closed:
                        await req_session.close()

            if retry_needed:
                attempt += 1
                if retry_delay > 0:
                    await asyncio.sleep(retry_delay)
                continue
            else:
                break

    except asyncio.TimeoutError:
        try:
            log_proxy_call(path, method, classify_endpoint(path), None, 504, "timeout", 0, None, (time.monotonic() - t_start) * 1000,
                           route_name=_model_router.default_name, upstream_url=upstream_url)
        except Exception:
            pass
        return web.json_response({"error": {"message": "upstream timeout"}}, status=504)
    except asyncio.CancelledError:
        try:
            log_proxy_call(path, method, classify_endpoint(path), None, 499, "client_cancelled", 0, None, (time.monotonic() - t_start) * 1000,
                           route_name=_model_router.default_name, upstream_url=upstream_url)
        except Exception:
            pass
        raise
    except Exception as e:
        error = str(e)[:200]
        t_total = (time.monotonic() - t_start) * 1000
        try:
            log_proxy_call(path, method, classify_endpoint(path), None, None, error, 0, None, t_total,
                           route_name=_model_router.default_name, upstream_url=upstream_url)
        except Exception:
            pass
        return web.json_response({"error": {"message": str(e)}}, status=502)


async def handle_health(request: web.Request) -> web.Response:
    """Health check endpoint with token budget, router state, and queue status."""
    budget_status = _token_budget.get_status()
    limiters_summary = _model_router.get_all_limiters_stats()
    
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
        "upstream": _model_router.default_upstream_url or UPSTREAM,
        "router": {
            "default_upstream": _model_router.default_upstream_url,
            "active_rules_count": sum(1 for r in _model_router.rules if r.enabled),
            "total_rules_count": len(_model_router.rules),
        },
        "db": str(DB_PATH),
        "rate_limiter": _model_router.default_limiter.get_stats(),
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


# ── Model Router Management Endpoints ────────────────────────────────────────
async def handle_routes_get(request: web.Request) -> web.Response:
    """GET /v1/routes or /routes — retrieves active routing configuration."""
    return web.json_response(_model_router.to_dict())


async def handle_routes_save(request: web.Request) -> web.Response:
    """POST /v1/routes or /routes — updates routing configuration and persists to disk."""
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
    try:
        data = await request.json() if request.can_read_body else {}
        model_name = data.get("model", "")
        res = _model_router.resolve(model_name)
        return web.json_response({
            "model": model_name,
            "resolved_upstream": res.upstream_url,
            "route_name": res.route_name,
            "route_id": res.route_id,
            "is_default": res.is_default,
            "pattern_matched": res.pattern_matched,
            "max_concurrent": res.max_concurrent,
            "slot_cooldown_ms": res.slot_cooldown_ms,
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)


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
        # Initial connect ping with active state
        init_payload = json.dumps({"type": "connected", "enabled": _raw_logging_enabled, "timestamp": datetime.now(timezone.utc).isoformat()})
        await response.write(f"data: {init_payload}\n\n".encode("utf-8"))

        while True:
            try:
                record = await asyncio.wait_for(q.get(), timeout=15.0)
                data = json.dumps(record, ensure_ascii=False)
                await response.write(f"data: {data}\n\n".encode("utf-8"))
            except asyncio.TimeoutError:
                # Keep-alive heartbeat comment
                await response.write(b": keepalive\n\n")
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

    async def on_startup(application):
        connector = aiohttp.TCPConnector(
            limit=100,
            limit_per_host=20,
            keepalive_timeout=60.0,
            enable_cleanup_closed=True,
            force_close=False,
        )
        timeout = aiohttp.ClientTimeout(total=300, connect=10, sock_read=300)
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

    # Raw payload management routes (must be before wildcard /v1/{tail:.*})
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


def main():
    global LISTEN_HOST, LISTEN_PORT, UPSTREAM, DB_PATH, PID_FILE, DAILY_TOKEN_LIMIT
    global MAX_CONCURRENT, SLOT_COOLDOWN_MS, RETRY_429_MAX, _concurrency_limiter

    parser = argparse.ArgumentParser(description="LLM Telemetry Proxy")
    parser.add_argument("--port", type=int, default=LISTEN_PORT, help="Listen port (default 9090)")
    parser.add_argument("--host", type=str, default=LISTEN_HOST, help="Listen host (default 0.0.0.0)")
    parser.add_argument("--upstream", type=str, default=UPSTREAM, help="Upstream API base URL")
    parser.add_argument("--token-limit", type=int, default=DAILY_TOKEN_LIMIT, help="Daily token budget cap (default 480000000)")
    parser.add_argument("--max-concurrent", type=int, default=MAX_CONCURRENT, help="Maximum concurrent upstream requests (default 4)")
    parser.add_argument("--slot-cooldown-ms", type=int, default=SLOT_COOLDOWN_MS, help="Cooldown gap in ms before dispatching next queued request when max concurrency is hit (default 50)")
    parser.add_argument("--retry-429-max", type=int, default=RETRY_429_MAX, help="Max retry attempts on upstream rate limits and transient hiccups (default 3, 0 disables)")
    parser.add_argument("--db", type=str, default=str(DB_PATH), help="SQLite database file path")
    parser.add_argument("--pid-file", type=str, default=str(PID_FILE), help="PID file path")
    args = parser.parse_args()

    LISTEN_PORT = args.port
    LISTEN_HOST = args.host
    UPSTREAM = args.upstream
    DAILY_TOKEN_LIMIT = args.token_limit
    MAX_CONCURRENT = args.max_concurrent
    SLOT_COOLDOWN_MS = args.slot_cooldown_ms
    RETRY_429_MAX = args.retry_429_max
    DB_PATH = Path(args.db)
    PID_FILE = Path(args.pid_file)

    if args.upstream and args.upstream != DEFAULT_UPSTREAM:
        _model_router.default_upstream_url = args.upstream
        UPSTREAM = args.upstream
    elif _model_router.default_upstream_url:
        UPSTREAM = _model_router.default_upstream_url

    _concurrency_limiter.max_concurrent = MAX_CONCURRENT
    _concurrency_limiter.slot_cooldown_seconds = max(0.0, SLOT_COOLDOWN_MS / 1000.0)

    _token_budget.daily_limit = DAILY_TOKEN_LIMIT
    _token_budget.db_path = DB_PATH
    _token_budget._load_state()

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
    print(f"[telemetry] Max Concurrent: {MAX_CONCURRENT} (Slot Cooldown: {SLOT_COOLDOWN_MS}ms, 429 Retries: {RETRY_429_MAX})", file=sys.stderr)
    print(f"[telemetry] DB: {DB_PATH}", file=sys.stderr)
    print(f"[telemetry] PID: {os.getpid()}", file=sys.stderr)
    print(f"[telemetry] Dashboard: Control via Dashboard UI (http://localhost:9118)", file=sys.stderr)
    web.run_app(create_app(), host=LISTEN_HOST, port=LISTEN_PORT, access_log=None)


if __name__ == "__main__":
    main()

