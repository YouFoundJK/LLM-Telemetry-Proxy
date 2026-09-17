# -*- coding: utf-8 -*-
"""
Model Router & Upstream Concurrency Manager

High-performance in-memory regex matcher and persistent router configuration.
Routes incoming inference calls dynamically based on model name patterns
and enforces per-route maximum API concurrency limits.
Client API keys pass through directly to the target upstream.
"""

import asyncio
import json
import os
import re
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_UPSTREAM_URL = "https://llm.ai.e-infra.cz/v1"

DEFAULT_ROUTE_TIMEOUT: Dict[str, float] = {
    "connect": 10.0,
    "first_byte": 25.0,
    "sock_read": 300.0,
    "total": 600.0,
}


def normalize_timeout(timeout: Optional[Dict[str, Any]]) -> Dict[str, float]:
    """Ensure timeout dict has valid schema, proper types, and sensible defaults."""
    merged = dict(DEFAULT_ROUTE_TIMEOUT)
    if not isinstance(timeout, dict):
        return merged
    for k in ("connect", "first_byte", "sock_read", "total"):
        if k in timeout and timeout[k] is not None:
            try:
                merged[k] = max(0.5, float(timeout[k]))
            except (ValueError, TypeError):
                pass
    return merged


DEFAULT_RETRY_POLICY: Dict[str, Any] = {
    "enabled": True,
    "max_retries": 3,
    "mode": "immediate",  # "immediate" (zero-delay) or "exponential" (backoff)
    "retry_on_status": [429, 502, 503, 504, 529],
    "retry_on_body_patterns": [
        "rate limit", "rate_limit", "rate_limit_exceeded",
        "try again", "overloaded", "capacity", "too many requests",
        "resource exhausted", "quota exceeded", "temporarily unavailable",
        "no response was returned", "no response returned", "sorry, no response",
        "server disconnected", "connection closed", "empty response"
    ],
    "retry_on_empty": True,
    "retry_on_disconnect": True,
    "retry_on_timeout": False,
    "max_retry_after_seconds": 10.0,
}


def normalize_retry_policy(policy: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Ensure retry policy dict has valid schema, proper types, and sensible defaults."""
    merged = dict(DEFAULT_RETRY_POLICY)
    if not isinstance(policy, dict):
        return merged
    for k, v in policy.items():
        merged[k] = v

    try:
        merged["max_retries"] = max(0, int(merged.get("max_retries", 3)))
    except (ValueError, TypeError):
        merged["max_retries"] = 3

    merged["enabled"] = bool(merged.get("enabled", True)) and (merged["max_retries"] > 0)
    merged["mode"] = "exponential" if str(merged.get("mode", "")).lower() == "exponential" else "immediate"
    merged["retry_on_empty"] = bool(merged.get("retry_on_empty", True))
    merged["retry_on_disconnect"] = bool(merged.get("retry_on_disconnect", True))
    merged["retry_on_timeout"] = bool(merged.get("retry_on_timeout", False))

    if not isinstance(merged.get("retry_on_status"), (list, set, tuple)):
        merged["retry_on_status"] = [429, 502, 503, 504, 529]
    else:
        try:
            merged["retry_on_status"] = [int(s) for s in merged["retry_on_status"]]
        except (ValueError, TypeError):
            merged["retry_on_status"] = [429, 502, 503, 504, 529]

    if not isinstance(merged.get("retry_on_body_patterns"), list):
        merged["retry_on_body_patterns"] = DEFAULT_RETRY_POLICY["retry_on_body_patterns"]
    else:
        merged["retry_on_body_patterns"] = [str(p) for p in merged["retry_on_body_patterns"] if str(p).strip()]

    try:
        merged["max_retry_after_seconds"] = max(0.0, float(merged.get("max_retry_after_seconds", 10.0)))
    except (ValueError, TypeError):
        merged["max_retry_after_seconds"] = 10.0

    return merged


DEFAULT_CIRCUIT_BREAKER: Dict[str, Any] = {
    "enabled": True,
    "consecutive_failures_threshold": 3,
    "quota_cooldown_seconds": 1800.0,   # 30 minutes
    "outage_cooldown_seconds": 300.0,    # 5 minutes
    "backoff_multiplier": 1.5,
    "max_cooldown_seconds": 7200.0,      # 2 hours max
}


def normalize_circuit_breaker(cb: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Ensure circuit breaker configuration has valid schema, proper types, and sensible defaults."""
    merged = dict(DEFAULT_CIRCUIT_BREAKER)
    if not isinstance(cb, dict):
        return merged
    for k, v in cb.items():
        merged[k] = v

    merged["enabled"] = bool(merged.get("enabled", True))
    try:
        merged["consecutive_failures_threshold"] = max(1, int(merged.get("consecutive_failures_threshold", 3)))
    except (ValueError, TypeError):
        merged["consecutive_failures_threshold"] = 3

    for sec_field in ("quota_cooldown_seconds", "outage_cooldown_seconds", "max_cooldown_seconds"):
        try:
            merged[sec_field] = max(1.0, float(merged.get(sec_field, DEFAULT_CIRCUIT_BREAKER[sec_field])))
        except (ValueError, TypeError):
            merged[sec_field] = DEFAULT_CIRCUIT_BREAKER[sec_field]

    try:
        merged["backoff_multiplier"] = max(1.0, float(merged.get("backoff_multiplier", 1.5)))
    except (ValueError, TypeError):
        merged["backoff_multiplier"] = 1.5

    return merged


QUOTA_ERROR_PATTERNS = (
    "quota",
    "free-models-per-day",
    "insufficient_quota",
    "insufficient quota",
    "exceeded your current quota",
    "billing",
    "credit balance",
    "out of credits",
    "zero balance",
    "insufficient funds",
    "monthly quota",
    "daily quota",
    "rate limit exceeded: free-models-per-day",
)


def is_quota_exhaustion(
    status_code: Optional[int] = None,
    body_text_or_json: Optional[Any] = None,
    reason: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    Determine if an HTTP response, error message, or body indicates hard quota or free-tier exhaustion.
    Returns (is_exhausted, detected_reason).
    """
    if status_code == 402:
        return True, "HTTP 402 Payment Required: Quota or credits exhausted"

    text_to_check = []
    if reason:
        text_to_check.append(str(reason))

    if body_text_or_json:
        if isinstance(body_text_or_json, dict):
            err = body_text_or_json.get("error")
            if isinstance(err, dict):
                text_to_check.append(str(err.get("message", "")))
                text_to_check.append(str(err.get("type", "")))
                text_to_check.append(str(err.get("code", "")))
            elif err:
                text_to_check.append(str(err))
            for k in ("message", "detail", "error_description"):
                if k in body_text_or_json and body_text_or_json[k]:
                    text_to_check.append(str(body_text_or_json[k]))
        elif isinstance(body_text_or_json, (str, bytes)):
            raw = body_text_or_json.decode("utf-8", errors="ignore") if isinstance(body_text_or_json, bytes) else body_text_or_json
            text_to_check.append(raw)

    combined = " ".join(text_to_check).lower()
    for pattern in QUOTA_ERROR_PATTERNS:
        if pattern in combined:
            return True, f"Quota exhaustion detected ({pattern})"

    return False, ""


def build_upstream_url(upstream_base: str, path: str) -> str:
    """
    Assemble target upstream URL cleanly, avoiding duplicate '/v1' path segments.
    """
    base = upstream_base.rstrip("/")
    req_path = path if path.startswith("/") else f"/{path}"

    if base.endswith("/v1") and req_path.startswith("/v1/"):
        return f"{base}{req_path[3:]}"
    if base.endswith("/v1") and req_path == "/v1":
        return base
    return f"{base}{req_path}"


def mask_api_key(key: Optional[str]) -> Optional[str]:
    """Return a masked representation of an API key for safe display and logging."""
    if not key:
        return None
    key_str = str(key).strip()
    if not key_str:
        return None
    if len(key_str) > 10:
        return f"{key_str[:4]}...{key_str[-4:]}"
    return "***"


def apply_upstream_api_key(headers: Dict[str, Any], api_key: Optional[str]) -> Dict[str, Any]:
    """
    If an upstream api_key is configured, replace incoming client Authorization/API-Key headers.
    Returns the modified headers dictionary.
    """
    if not api_key:
        return headers
    key_str = str(api_key).strip()
    if not key_str:
        return headers

    bearer_val = key_str if key_str.lower().startswith("bearer ") else f"Bearer {key_str}"
    for k in list(headers.keys()):
        if str(k).lower() == "authorization":
            del headers[k]
    headers["Authorization"] = bearer_val

    raw_key = key_str[7:].strip() if key_str.lower().startswith("bearer ") else key_str
    for k in list(headers.keys()):
        if str(k).lower() in ("api-key", "x-api-key"):
            headers[k] = raw_key

    return headers


# ── Concurrency & RPM Limiter ────────────────────────────────────────────────
class _SlotContextManager:
    """Async context manager helper for limiter.slot()."""
    def __init__(self, limiter: 'UpstreamConcurrencyLimiter'):
        self.limiter = limiter

    async def __aenter__(self):
        await self.limiter.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.limiter.release()


class AcquiredSlot:
    """
    Async context manager wrapper for a slot that was either try_acquired or needs normal acquire.
    If already_acquired=True, __aenter__ is a no-op and __aexit__ safely releases the slot.
    """
    def __init__(self, limiter: 'UpstreamConcurrencyLimiter', already_acquired: bool = False):
        self.limiter = limiter
        self.already_acquired = already_acquired

    async def __aenter__(self):
        if not self.already_acquired:
            await self.limiter.acquire()
            self.already_acquired = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.already_acquired:
            await self.limiter.release()
            self.already_acquired = False


class UpstreamConcurrencyLimiter:
    """
    Asynchronous concurrency and RPM limiter with strict FIFO queuing, slot cooldown gaps,
    rolling 60-second RPM pacing, and cancellation safety.
    """

    def __init__(
        self,
        max_concurrent: int = 4,
        slot_cooldown_seconds: float = 0.05,
        max_rpm: int = -1,
        slot_cooldown_ms: Optional[int] = None,
    ):
        if slot_cooldown_ms is not None:
            slot_cooldown_seconds = slot_cooldown_ms / 1000.0
        self.max_concurrent = max(1, int(max_concurrent))
        self.slot_cooldown_seconds = max(0.0, float(slot_cooldown_seconds))
        self.max_rpm = int(max_rpm) if max_rpm is not None else -1
        self._active_count = 0
        self._waiters = deque()  # deque of asyncio.Future
        self._lock = asyncio.Lock()
        self._last_release_time = 0.0
        self._rpm_history = deque()  # deque of float monotonic timestamps
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

    @property
    def queue_depth(self) -> int:
        return len(self._waiters)

    def has_immediate_capacity(self) -> bool:
        """Inspect without waiting whether a slot can be admitted immediately."""
        now = time.monotonic()
        while self._rpm_history and (now - self._rpm_history[0]) >= 60.0:
            self._rpm_history.popleft()

        can_admit_conc = (self._active_count < self.max_concurrent)
        can_admit_rpm = (self.max_rpm <= 0) or (len(self._rpm_history) < self.max_rpm)
        return bool(not self._waiters and can_admit_conc and can_admit_rpm)

    async def try_acquire(self) -> bool:
        """
        Atomically acquire a concurrency slot if immediately available without queuing.
        Returns True if acquired immediately, False otherwise.
        """
        async with self._lock:
            now = time.monotonic()
            while self._rpm_history and (now - self._rpm_history[0]) >= 60.0:
                self._rpm_history.popleft()

            can_admit_conc = (self._active_count < self.max_concurrent)
            can_admit_rpm = (self.max_rpm <= 0) or (len(self._rpm_history) < self.max_rpm)

            if not self._waiters and can_admit_conc and can_admit_rpm:
                self._active_count += 1
                self._total_admitted += 1
                self._peak_active = max(self._peak_active, self._active_count)
                if self.max_rpm > 0:
                    self._rpm_history.append(now)
                return True
            return False

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
        now = time.monotonic()
        recent_rpm = sum(1 for t in self._rpm_history if (now - t) < 60.0)
        return {
            "max_concurrent": self.max_concurrent,
            "active": self._active_count,
            "queued": len(self._waiters),
            "slot_cooldown_ms": int(round(self.slot_cooldown_seconds * 1000)),
            "max_rpm": self.max_rpm,
            "current_rpm": recent_rpm,
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

    def _schedule_rpm_check(self, delay: float):
        """Schedule an asynchronous check once RPM window capacity reopens."""
        async def _tick():
            await asyncio.sleep(delay)
            async with self._lock:
                self._dispatch_next()
        asyncio.create_task(_tick())

    def _dispatch_next(self):
        """Must be called while holding self._lock."""
        now = time.monotonic()
        while self._rpm_history and (now - self._rpm_history[0]) >= 60.0:
            self._rpm_history.popleft()

        while self._waiters:
            if self._active_count >= self.max_concurrent:
                return

            if self.max_rpm > 0 and len(self._rpm_history) >= self.max_rpm:
                wait_sec = max(0.01, (self._rpm_history[0] + 60.0) - now)
                self._schedule_rpm_check(wait_sec)
                return

            fut = self._waiters.popleft()
            if not fut.done() and not fut.cancelled():
                self._active_count += 1
                self._total_admitted += 1
                self._peak_active = max(self._peak_active, self._active_count)
                if self.max_rpm > 0:
                    self._rpm_history.append(now)
                fut.set_result(None)
                return

    async def acquire(self):
        """Acquire a concurrency slot, waiting in FIFO order if max_concurrent or max_rpm is reached."""
        async with self._lock:
            now = time.monotonic()
            while self._rpm_history and (now - self._rpm_history[0]) >= 60.0:
                self._rpm_history.popleft()

            can_admit_conc = (self._active_count < self.max_concurrent)
            can_admit_rpm = (self.max_rpm <= 0) or (len(self._rpm_history) < self.max_rpm)

            # If we have capacity and no waiters are queued, admit immediately without delay!
            if not self._waiters and can_admit_conc and can_admit_rpm:
                self._active_count += 1
                self._total_admitted += 1
                self._peak_active = max(self._peak_active, self._active_count)
                if self.max_rpm > 0:
                    self._rpm_history.append(now)
                return

            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            self._waiters.append(fut)
            self._total_queued += 1

            # If concurrency capacity is free but RPM is exhausted, schedule a wakeup
            if can_admit_conc and not can_admit_rpm and self._rpm_history:
                wait_sec = max(0.01, (self._rpm_history[0] + 60.0) - now)
                self._schedule_rpm_check(wait_sec)

        try:
            await fut
        except asyncio.CancelledError:
            async with self._lock:
                if fut in self._waiters:
                    self._waiters.remove(fut)
                elif fut.done() and not fut.cancelled():
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

        asyncio.create_task(self._cooldown_and_dispatch())

    async def _cooldown_and_dispatch(self):
        if self.slot_cooldown_seconds > 0:
            await asyncio.sleep(self.slot_cooldown_seconds)

        async with self._lock:
            now = time.monotonic()
            while self._rpm_history and (now - self._rpm_history[0]) >= 60.0:
                self._rpm_history.popleft()

            while self._waiters:
                fut = self._waiters.popleft()
                if not fut.done() and not fut.cancelled():
                    # If RPM limit is exceeded, we must decrement active count and wait for RPM window
                    if self.max_rpm > 0 and len(self._rpm_history) >= self.max_rpm:
                        self._active_count = max(0, self._active_count - 1)
                        # Re-insert waiter at head of line
                        self._waiters.appendleft(fut)
                        wait_sec = max(0.01, (self._rpm_history[0] + 60.0) - now)
                        self._schedule_rpm_check(wait_sec)
                        return

                    self._total_admitted += 1
                    if self.max_rpm > 0:
                        self._rpm_history.append(now)
                    fut.set_result(None)
                    return
            self._active_count = max(0, self._active_count - 1)


@dataclass
class RouteResolutionResult:
    route_id: Optional[str]
    route_name: str
    upstream_url: str
    is_default: bool = False
    pattern_matched: Optional[str] = None
    max_concurrent: int = 4
    slot_cooldown_ms: int = 50
    max_rpm: int = -1
    timeout: Optional[Dict[str, float]] = None
    fallback_upstream_url: Optional[str] = None
    retry_policy: Optional[Dict[str, Any]] = None
    circuit_breaker: Optional[Dict[str, Any]] = None
    api_key: Optional[str] = None
    priority: int = 10
    strategy: str = "inherit"
    exclude_pattern: Optional[str] = None
    is_cooling_down: bool = False
    cooldown_remaining: float = 0.0
    circuit_state: str = "CLOSED"
    canary_probe: bool = False


class ModelRouteRule:
    def __init__(
        self,
        id: str,
        name: str,
        pattern: str,
        upstream_url: str,
        api_key: Optional[str] = None,
        enabled: bool = True,
        priority: int = 10,
        max_concurrent: int = 4,
        slot_cooldown_ms: int = 50,
        max_rpm: int = -1,
        timeout: Optional[Dict[str, Any]] = None,
        fallback_upstream_url: Optional[str] = None,
        retry_policy: Optional[Dict[str, Any]] = None,
        circuit_breaker: Optional[Dict[str, Any]] = None,
        limiter: Optional[UpstreamConcurrencyLimiter] = None,
        strategy: str = "inherit",
        exclude_pattern: Optional[str] = None,
        is_default: bool = False,
    ):
        self.id = str(id) if id else f"route_{uuid.uuid4().hex[:8]}"
        self.name = str(name).strip() if name else "Custom Route"
        self.pattern = str(pattern).strip() if pattern is not None else ".*"
        self.upstream_url = str(upstream_url).strip().rstrip("/")
        self.api_key = str(api_key).strip() if api_key and str(api_key).strip() else None
        self.enabled = bool(enabled)
        self.priority = int(priority)
        self.strategy = str(strategy).strip().lower() if strategy else "inherit"
        self.max_concurrent = max(1, int(max_concurrent))
        self.slot_cooldown_ms = max(0, int(slot_cooldown_ms))
        self.max_rpm = int(max_rpm) if max_rpm is not None else -1
        self.timeout = normalize_timeout(timeout)
        self.fallback_upstream_url = str(fallback_upstream_url).strip().rstrip("/") if fallback_upstream_url else None
        self.retry_policy = normalize_retry_policy(retry_policy)
        self.circuit_breaker = normalize_circuit_breaker(circuit_breaker)
        self.exclude_pattern = str(exclude_pattern).strip() if exclude_pattern and str(exclude_pattern).strip() else None
        self.is_default = bool(is_default) or (self.id == "default")
        
        self.limiter = limiter or UpstreamConcurrencyLimiter(
            max_concurrent=self.max_concurrent,
            slot_cooldown_seconds=self.slot_cooldown_ms / 1000.0,
            max_rpm=self.max_rpm,
        )
        self.limiter.max_concurrent = self.max_concurrent
        self.limiter.slot_cooldown_seconds = self.slot_cooldown_ms / 1000.0
        self.limiter.max_rpm = self.max_rpm

        self._circuit_state: str = "CLOSED"
        self._cooldown_until: float = 0.0
        self._current_cooldown_duration: float = 0.0
        self._cooldown_reason: Optional[str] = None
        self._consecutive_failures: int = 0
        self._canary_in_flight: bool = False

        self._compiled: Optional[re.Pattern] = None
        self._compiled_exclude: Optional[re.Pattern] = None
        self._compile_regex()

    def get_circuit_state(self) -> str:
        """Evaluate circuit breaker state with automatic transition from OPEN to HALF_OPEN upon cooldown expiration."""
        if not self.circuit_breaker.get("enabled", True):
            return "CLOSED"
        if self._circuit_state == "OPEN":
            if time.monotonic() >= self._cooldown_until:
                self._circuit_state = "HALF_OPEN"
                self._canary_in_flight = False
        return self._circuit_state

    def is_available_for_admission(self) -> bool:
        """Check if route is eligible to receive requests (CLOSED, or HALF_OPEN with no canary in flight)."""
        state = self.get_circuit_state()
        if state == "CLOSED":
            return True
        if state == "HALF_OPEN":
            return not self._canary_in_flight
        return False

    def is_cooling_down(self) -> bool:
        return self.get_circuit_state() == "OPEN"

    def cooldown_remaining(self) -> float:
        if self.get_circuit_state() == "OPEN":
            return max(0.0, self._cooldown_until - time.monotonic())
        return 0.0

    def mark_success(self):
        """Reset failures and restore circuit to healthy CLOSED state."""
        self._consecutive_failures = 0
        self._circuit_state = "CLOSED"
        self._cooldown_until = 0.0
        self._current_cooldown_duration = 0.0
        self._cooldown_reason = None
        self._canary_in_flight = False

    def mark_quota_exhaustion(self, reason: str = "Quota Exceeded"):
        """Instant circuit breaker trip for hard quota limit (default 30m / 1800s)."""
        if not self.circuit_breaker.get("enabled", True):
            return
        cooldown = float(self.circuit_breaker.get("quota_cooldown_seconds", 1800.0))
        self._circuit_state = "OPEN"
        self._current_cooldown_duration = cooldown
        self._cooldown_until = time.monotonic() + cooldown
        self._cooldown_reason = reason or "Hard quota limit exceeded"
        self._canary_in_flight = False

    def mark_outage_failure(self, reason: str = "Upstream failure"):
        """Record outage failure (502/503/timeout/disconnect), tripping after threshold (default 3) or backing off if canary fails."""
        if not self.circuit_breaker.get("enabled", True):
            return
        self._consecutive_failures += 1
        state = self.get_circuit_state()

        if state == "HALF_OPEN":
            base_cd = self._current_cooldown_duration or float(self.circuit_breaker.get("outage_cooldown_seconds", 300.0))
            mult = float(self.circuit_breaker.get("backoff_multiplier", 1.5))
            max_cd = float(self.circuit_breaker.get("max_cooldown_seconds", 7200.0))
            cooldown = min(max_cd, base_cd * mult)
            self._circuit_state = "OPEN"
            self._current_cooldown_duration = cooldown
            self._cooldown_until = time.monotonic() + cooldown
            self._cooldown_reason = f"Canary probe failed: {reason}"
            self._canary_in_flight = False
        else:
            threshold = int(self.circuit_breaker.get("consecutive_failures_threshold", 3))
            if self._consecutive_failures >= threshold:
                cooldown = float(self.circuit_breaker.get("outage_cooldown_seconds", 300.0))
                self._circuit_state = "OPEN"
                self._current_cooldown_duration = cooldown
                self._cooldown_until = time.monotonic() + cooldown
                self._cooldown_reason = reason or f"{self._consecutive_failures} consecutive outage failures"
                self._canary_in_flight = False

    def mark_failure(self, reason: str = "", cooldown_seconds: float = 60.0):
        self.mark_outage_failure(reason=reason)

    def mark_cooldown(self, seconds: float = 60.0, reason: str = ""):
        self._circuit_state = "OPEN"
        self._current_cooldown_duration = max(0.001, float(seconds))
        self._cooldown_until = time.monotonic() + self._current_cooldown_duration
        self._cooldown_reason = str(reason).strip() if reason else "Manual cooldown"
        self._canary_in_flight = False

    def reset_circuit(self):
        self.mark_success()

    def release_canary(self):
        """Release canary in-flight flag without changing circuit state."""
        self._canary_in_flight = False

    def _compile_regex(self):
        if not self.pattern or self.pattern == ".*":
            self._compiled = None
        else:
            try:
                self._compiled = re.compile(self.pattern, re.IGNORECASE)
            except re.error as e:
                print(f"[ModelRouter] Invalid regex pattern '{self.pattern}' for rule '{self.name}': {e}", file=sys.stderr)
                self._compiled = None

        if not self.exclude_pattern:
            self._compiled_exclude = None
        else:
            try:
                self._compiled_exclude = re.compile(self.exclude_pattern, re.IGNORECASE)
            except re.error as e:
                print(f"[ModelRouter] Invalid exclude regex pattern '{self.exclude_pattern}' for rule '{self.name}': {e}", file=sys.stderr)
                self._compiled_exclude = None

    def matches(self, model_name: str) -> bool:
        if not self.enabled or not model_name:
            return False
        clean_name = str(model_name).strip()
        # 1. Negative / Exclude regex check: if matched, this route is immediately disqualified
        if self._compiled_exclude and self._compiled_exclude.search(clean_name):
            return False
        # 2. Positive pattern check (catch-all if empty or '.*')
        if not self.pattern or self.pattern == ".*":
            return True
        if not self._compiled:
            return False
        return bool(self._compiled.search(clean_name))

    def to_config_dict(self) -> Dict[str, Any]:
        """Clean dictionary suitable for persisting to JSON configuration file."""
        return {
            "id": self.id,
            "name": self.name,
            "pattern": self.pattern,
            "upstream_url": self.upstream_url,
            "api_key": self.api_key,
            "enabled": self.enabled,
            "priority": self.priority,
            "strategy": self.strategy,
            "max_concurrent": self.max_concurrent,
            "slot_cooldown_ms": self.slot_cooldown_ms,
            "max_rpm": self.max_rpm,
            "timeout": self.timeout,
            "fallback_upstream_url": self.fallback_upstream_url,
            "retry_policy": self.retry_policy,
            "circuit_breaker": self.circuit_breaker,
            "exclude_pattern": self.exclude_pattern,
            "is_default": self.is_default,
        }

    def to_dict(self, mask_keys: bool = False) -> Dict[str, Any]:
        """Dictionary for API responses with live metrics."""
        return {
            "id": self.id,
            "name": self.name,
            "pattern": self.pattern,
            "upstream_url": self.upstream_url,
            "api_key": mask_api_key(self.api_key) if mask_keys else self.api_key,
            "has_api_key": bool(self.api_key),
            "enabled": self.enabled,
            "priority": self.priority,
            "strategy": self.strategy,
            "max_concurrent": self.max_concurrent,
            "slot_cooldown_ms": self.slot_cooldown_ms,
            "max_rpm": self.max_rpm,
            "timeout": self.timeout,
            "fallback_upstream_url": self.fallback_upstream_url,
            "retry_policy": self.retry_policy,
            "circuit_breaker": self.circuit_breaker,
            "exclude_pattern": self.exclude_pattern,
            "is_default": self.is_default,
            "circuit_state": self.get_circuit_state(),
            "is_cooling_down": self.is_cooling_down(),
            "cooldown_remaining_seconds": round(self.cooldown_remaining(), 1),
            "cooldown_reason": self._cooldown_reason if self.is_cooling_down() else None,
            "consecutive_failures": self._consecutive_failures,
            "limiter_stats": self.limiter.get_stats(),
        }


class ModelRouter:
    """
    Thread-safe, microsecond-latency model router and per-upstream concurrency limiter.
    """

    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path
        self.routing_strategy = "priority"
        self._rr_counter = 0
        self.default_upstream_url = DEFAULT_UPSTREAM_URL
        self.default_name = "Default Upstream (e-INFRA)"
        self.default_api_key: Optional[str] = None
        self.default_max_concurrent = 4
        self.default_slot_cooldown_ms = 50
        self.default_max_rpm = -1
        self.default_timeout = normalize_timeout(None)
        self.default_fallback_upstream_url = None
        self.default_retry_policy = normalize_retry_policy(None)
        self.default_circuit_breaker = normalize_circuit_breaker(None)
        self.default_limiter = UpstreamConcurrencyLimiter(
            max_concurrent=self.default_max_concurrent,
            slot_cooldown_seconds=self.default_slot_cooldown_ms / 1000.0,
            max_rpm=self.default_max_rpm,
        )
        self.default_rule = ModelRouteRule(
            id="default",
            name=self.default_name,
            pattern=".*",
            upstream_url=self.default_upstream_url,
            api_key=self.default_api_key,
            enabled=True,
            priority=0,
            strategy="inherit",
            max_concurrent=self.default_max_concurrent,
            slot_cooldown_ms=self.default_slot_cooldown_ms,
            max_rpm=self.default_max_rpm,
            timeout=self.default_timeout,
            fallback_upstream_url=self.default_fallback_upstream_url,
            retry_policy=self.default_retry_policy,
            circuit_breaker=self.default_circuit_breaker,
            limiter=self.default_limiter,
            is_default=True,
        )
        self.rules: List[ModelRouteRule] = [self.default_rule]

        if self.config_path and self.config_path.exists():
            self.load()

    def load(self, path: Optional[Path] = None) -> bool:
        target_path = path or self.config_path
        if not target_path or not target_path.exists():
            return False

        try:
            with open(target_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.routing_strategy = str(data.get("routing_strategy", "priority")).strip().lower()
            def_route = data.get("default_route", {})

            # Check if routes array contains an explicit default rule
            routes_data = data.get("routes", [])
            explicit_def_rule_data = None
            for r in routes_data:
                if r.get("is_default") or r.get("id") == "default":
                    explicit_def_rule_data = r
                    break

            if explicit_def_rule_data:
                def_source = dict(def_route)
                def_source.update(explicit_def_rule_data)
            else:
                def_source = def_route

            self.default_upstream_url = def_source.get("upstream_url", DEFAULT_UPSTREAM_URL).rstrip("/")
            self.default_name = def_source.get("name", "Default Upstream")
            self.default_api_key = str(def_source["api_key"]).strip() if def_source.get("api_key") else None
            self.default_max_concurrent = max(1, int(def_source.get("max_concurrent", 4)))
            self.default_slot_cooldown_ms = max(0, int(def_source.get("slot_cooldown_ms", 50)))
            self.default_max_rpm = int(def_source.get("max_rpm", -1)) if def_source.get("max_rpm") is not None else -1
            self.default_timeout = normalize_timeout(def_source.get("timeout"))
            self.default_fallback_upstream_url = def_source.get("fallback_upstream_url")
            self.default_retry_policy = normalize_retry_policy(def_source.get("retry_policy"))
            def_enabled = bool(def_source.get("enabled", True))
            def_exclude_pattern = def_source.get("exclude_pattern")
            def_priority = int(def_source.get("priority", 0))

            self.default_limiter.max_concurrent = self.default_max_concurrent
            self.default_limiter.slot_cooldown_seconds = self.default_slot_cooldown_ms / 1000.0
            self.default_limiter.max_rpm = self.default_max_rpm

            self.default_rule = ModelRouteRule(
                id="default",
                name=self.default_name,
                pattern=def_source.get("pattern", ".*"),
                upstream_url=self.default_upstream_url,
                api_key=self.default_api_key,
                enabled=def_enabled,
                priority=def_priority,
                strategy=def_source.get("strategy", "inherit"),
                max_concurrent=self.default_max_concurrent,
                slot_cooldown_ms=self.default_slot_cooldown_ms,
                max_rpm=self.default_max_rpm,
                timeout=self.default_timeout,
                fallback_upstream_url=self.default_fallback_upstream_url,
                retry_policy=self.default_retry_policy,
                circuit_breaker=def_source.get("circuit_breaker"),
                limiter=self.default_limiter,
                exclude_pattern=def_exclude_pattern,
                is_default=True,
            )

            existing_rules_map = {r.id: r for r in self.rules}
            loaded_rules = []
            for r in routes_data:
                r_id = r.get("id")
                if r_id == "default" or r.get("is_default"):
                    continue
                existing = existing_rules_map.get(r_id) if r_id else None
                rule = ModelRouteRule(
                    id=r_id,
                    name=r.get("name", ""),
                    pattern=r.get("pattern", ""),
                    upstream_url=r.get("upstream_url", DEFAULT_UPSTREAM_URL),
                    api_key=r.get("api_key"),
                    enabled=r.get("enabled", True),
                    priority=r.get("priority", 10),
                    strategy=r.get("strategy", "inherit"),
                    max_concurrent=r.get("max_concurrent", 4),
                    slot_cooldown_ms=r.get("slot_cooldown_ms", 50),
                    max_rpm=r.get("max_rpm", -1),
                    timeout=r.get("timeout"),
                    fallback_upstream_url=r.get("fallback_upstream_url"),
                    retry_policy=r.get("retry_policy"),
                    circuit_breaker=r.get("circuit_breaker"),
                    exclude_pattern=r.get("exclude_pattern"),
                    limiter=existing.limiter if existing else None,
                    is_default=False,
                )
                loaded_rules.append(rule)

            loaded_rules.sort(key=lambda x: x.priority, reverse=True)
            self.rules = loaded_rules
            return True
        except Exception as e:
            print(f"[ModelRouter] Error loading configuration from {target_path}: {e}", file=sys.stderr)
            return False

    def save(self, path: Optional[Path] = None) -> bool:
        target_path = path or self.config_path
        if not target_path:
            return False

        def_dict = self.default_rule.to_config_dict() if self.default_rule else {
            "id": "default",
            "name": self.default_name,
            "pattern": ".*",
            "upstream_url": self.default_upstream_url,
            "api_key": self.default_api_key,
            "enabled": True,
            "priority": 0,
            "strategy": "inherit",
            "max_concurrent": self.default_max_concurrent,
            "slot_cooldown_ms": self.default_slot_cooldown_ms,
            "max_rpm": self.default_max_rpm,
            "timeout": self.default_timeout,
            "fallback_upstream_url": self.default_fallback_upstream_url,
            "retry_policy": self.default_retry_policy,
            "exclude_pattern": None,
            "is_default": True,
        }

        data = {
            "routing_strategy": self.routing_strategy,
            "default_route": def_dict,
            "routes": [r.to_config_dict() for r in self.rules if not r.is_default and r.id != "default"],
        }

        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = target_path.with_name(f"{target_path.stem}.tmp_{os.getpid()}_{time.time_ns()}.json")
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)

            try:
                os.replace(temp_path, target_path)
            except Exception:
                # Direct write fallback if os.replace encounters Windows file lock issues
                with open(target_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                try:
                    if temp_path.exists():
                        temp_path.unlink(missing_ok=True)
                except Exception:
                    pass

            if sys.platform != "win32":
                try:
                    os.chmod(target_path, 0o600)
                except Exception:
                    pass
            return True
        except Exception as e:
            print(f"[ModelRouter] Error saving configuration to {target_path}: {e}", file=sys.stderr)
            try:
                with open(target_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                return True
            except Exception as e2:
                print(f"[ModelRouter] Direct fallback save also failed to {target_path}: {e2}", file=sys.stderr)
                return False

    def _rule_to_result(self, rule: ModelRouteRule) -> RouteResolutionResult:
        return RouteResolutionResult(
            route_id=rule.id,
            route_name=rule.name,
            upstream_url=rule.upstream_url,
            is_default=rule.is_default,
            pattern_matched=rule.pattern,
            exclude_pattern=rule.exclude_pattern,
            max_concurrent=rule.max_concurrent,
            slot_cooldown_ms=rule.slot_cooldown_ms,
            max_rpm=rule.max_rpm,
            timeout=rule.timeout,
            fallback_upstream_url=rule.fallback_upstream_url,
            retry_policy=rule.retry_policy,
            circuit_breaker=rule.circuit_breaker,
            api_key=rule.api_key,
            priority=rule.priority,
            strategy=rule.strategy,
            is_cooling_down=rule.is_cooling_down(),
            cooldown_remaining=rule.cooldown_remaining(),
            circuit_state=rule.get_circuit_state(),
            canary_probe=False,
        )

    def resolve_chain(self, model_name: Optional[str]) -> List[RouteResolutionResult]:
        """
        Resolve all matching candidate routes for the requested model name in priority order.
        Custom rules matching the model are added in descending priority order.
        If no custom rules match, the default rule is returned as fallback (if enabled and not excluded).
        If default rule is disabled/frozen or excluded, returns an empty list.
        """
        matches: List[RouteResolutionResult] = []
        clean_name = str(model_name).strip() if model_name else ""

        # 1. Custom rules matching clean_name
        custom_rules = [r for r in self.rules if not r.is_default and r.id != "default"]
        custom_rules.sort(key=lambda x: x.priority, reverse=True)

        for rule in custom_rules:
            if rule.matches(clean_name):
                matches.append(self._rule_to_result(rule))

        # 2. If no custom rules matched, fall back to default rule if enabled and not excluded
        if not matches:
            def_rule = self.default_rule
            if def_rule and def_rule.enabled and def_rule.matches(clean_name):
                matches.append(self._rule_to_result(def_rule))

        return matches

    def resolve(self, model_name: Optional[str]) -> Optional[RouteResolutionResult]:
        """
        Resolve highest-priority target upstream for the requested model name.
        Executes in microsecond time.
        """
        chain = self.resolve_chain(model_name)
        return chain[0] if chain else None

    async def select_admission_route(
        self, candidates: List[RouteResolutionResult]
    ) -> Tuple[Optional[RouteResolutionResult], Optional[UpstreamConcurrencyLimiter], bool]:
        """
        Select an admission route following priority precedence and circuit breaker availability:
        - Filters candidates based on circuit breaker availability (CLOSED or HALF_OPEN canary).
        - If all routes are cooling down (OPEN or canary in flight), triggers Safety Valve to emergency
          probe the candidate closest to cooldown expiration.
        - Supports 'priority' spillover and 'balanced' (least-conn / round-robin) modes.
        - Atomically marks canary probes in flight on selected HALF_OPEN or emergency candidate.
        """
        if not candidates:
            return None, None, False

        # Refresh candidate circuit breaker states
        for c in candidates:
            r = self.get_rule(c.route_id)
            if r:
                c.circuit_state = r.get_circuit_state()
                c.is_cooling_down = r.is_cooling_down()
                c.cooldown_remaining = r.cooldown_remaining()

        # Eligible candidates: CLOSED, or HALF_OPEN with no canary in flight
        available_candidates = [
            c for c in candidates
            if (self.get_rule(c.route_id).is_available_for_admission() if self.get_rule(c.route_id) else not c.is_cooling_down)
        ]

        if available_candidates:
            pool = available_candidates
        else:
            # Safety Valve: All routes are cooling down. Emergency probe the route closest to expiration!
            min_remaining = min(c.cooldown_remaining for c in candidates)
            pool = [c for c in candidates if c.cooldown_remaining == min_remaining]

        def _finalize_picked(cand: RouteResolutionResult, lim: UpstreamConcurrencyLimiter, acquired: bool):
            r = self.get_rule(cand.route_id)
            if r and r.get_circuit_state() in ("HALF_OPEN", "OPEN"):
                r._canary_in_flight = True
                cand.canary_probe = True
            return cand, lim, acquired

        if len(pool) == 1:
            limiter = self.get_limiter(pool[0].route_id)
            acquired = await limiter.try_acquire()
            return _finalize_picked(pool[0], limiter, acquired)

        # Determine if balanced mode is active
        is_balanced = (self.routing_strategy in ("balanced", "least_conn", "round_robin"))
        if not is_balanced:
            is_balanced = any(c.strategy in ("balanced", "least_conn", "round_robin") for c in pool)

        if not is_balanced:
            # ── Default Priority Spillover ──────────────────────────────────
            for cand in pool:
                limiter = self.get_limiter(cand.route_id)
                if await limiter.try_acquire():
                    return _finalize_picked(cand, limiter, True)

            best_candidate = pool[0]
            best_limiter = self.get_limiter(best_candidate.route_id)
            min_queued = best_limiter.queued

            for cand in pool[1:]:
                lim = self.get_limiter(cand.route_id)
                q_depth = lim.queued
                if q_depth < min_queued:
                    min_queued = q_depth
                    best_candidate = cand
                    best_limiter = lim

            return _finalize_picked(best_candidate, best_limiter, False)

        # ── Balanced Routing Strategy (Equal Priority Pool) ─────────────────
        prio_map: Dict[int, List[RouteResolutionResult]] = {}
        for cand in pool:
            prio_map.setdefault(cand.priority, []).append(cand)

        sorted_prios = sorted(prio_map.keys(), reverse=True)

        for prio in sorted_prios:
            tier_candidates = prio_map[prio]
            tier_len = len(tier_candidates)
            self._rr_counter += 1
            cur_rr = self._rr_counter

            def tier_sort_key(item: Tuple[int, RouteResolutionResult]):
                idx, c = item
                lim = self.get_limiter(c.route_id)
                return (lim.queued > 0, lim.active, (idx - cur_rr) % tier_len)

            indexed_tier = list(enumerate(tier_candidates))
            indexed_tier.sort(key=tier_sort_key)

            for _, cand in indexed_tier:
                limiter = self.get_limiter(cand.route_id)
                if await limiter.try_acquire():
                    return _finalize_picked(cand, limiter, True)

        all_limiters = [(c, self.get_limiter(c.route_id)) for c in pool]
        min_queued = min(lim.queued for _, lim in all_limiters)
        min_q_candidates = [c for c, lim in all_limiters if lim.queued == min_queued]

        max_prio = max(c.priority for c in min_q_candidates)
        top_tier_tied = [c for c in min_q_candidates if c.priority == max_prio]

        self._rr_counter += 1
        picked_cand = top_tier_tied[self._rr_counter % len(top_tier_tied)]
        return _finalize_picked(picked_cand, self.get_limiter(picked_cand.route_id), False)

    def get_rule(self, route_id: Optional[str] = None) -> Optional[ModelRouteRule]:
        """
        Get the ModelRouteRule instance for a specific route ID.
        """
        if not route_id or route_id == "default" or (self.default_rule and self.default_rule.id == route_id):
            return self.default_rule
        for r in self.rules:
            if r.id == route_id:
                return r
        return self.default_rule if (self.default_rule and self.default_rule.id == route_id) else None

    def mark_route_success(self, route_id: Optional[str]):
        """Mark route as successful, resetting consecutive failures and restoring circuit to CLOSED."""
        rule = self.get_rule(route_id)
        if rule:
            rule.mark_success()

    def mark_route_quota_exhaustion(self, route_id: Optional[str], reason: str = "Quota Exceeded"):
        """Instant circuit breaker trip for hard quota limit (default 30m / 1800s)."""
        rule = self.get_rule(route_id)
        if rule:
            rule.mark_quota_exhaustion(reason=reason)

    def mark_route_outage_failure(self, route_id: Optional[str], reason: str = "Upstream failure"):
        """Record outage failure, tripping circuit after consecutive failure threshold or backing off canary."""
        rule = self.get_rule(route_id)
        if rule:
            rule.mark_outage_failure(reason=reason)

    def mark_route_failure(self, route_id: Optional[str], reason: str = "", cooldown_seconds: float = 60.0):
        """Record route failure, automatically entering cooldown if threshold is reached."""
        rule = self.get_rule(route_id)
        if rule:
            rule.mark_failure(reason=reason, cooldown_seconds=cooldown_seconds)

    def mark_route_cooldown(self, route_id: Optional[str], seconds: float = 60.0, reason: str = ""):
        """Immediately place route in cooldown."""
        rule = self.get_rule(route_id)
        if rule:
            rule.mark_cooldown(seconds=seconds, reason=reason)

    def reset_route_circuit(self, route_id: Optional[str]):
        """Manually reset circuit breaker to healthy CLOSED state."""
        rule = self.get_rule(route_id)
        if rule:
            rule.reset_circuit()

    def release_route_canary(self, route_id: Optional[str]):
        """Release canary in-flight flag without changing circuit state."""
        rule = self.get_rule(route_id)
        if rule:
            rule.release_canary()

    def get_limiter(self, route_id: Optional[str] = None) -> UpstreamConcurrencyLimiter:
        """
        Get the concurrency limiter for a specific route, or the default limiter.
        """
        if not route_id or route_id == "default" or (self.default_rule and self.default_rule.id == route_id):
            return self.default_rule.limiter if self.default_rule else self.default_limiter
        for rule in self.rules:
            if rule.id == route_id and rule.enabled:
                return rule.limiter
        return self.default_rule.limiter if self.default_rule else self.default_limiter

    def get_all_limiters_stats(self) -> Dict[str, Any]:
        """
        Return live concurrency, queue, and circuit breaker metrics across all active routes and the default router.
        """
        routes_stats = []
        for r in self.rules:
            if r.is_default or r.id == "default":
                continue
            routes_stats.append({
                "id": r.id,
                "name": r.name,
                "pattern": r.pattern,
                "exclude_pattern": r.exclude_pattern,
                "upstream_url": r.upstream_url,
                "has_api_key": bool(r.api_key),
                "enabled": r.enabled,
                "is_default": False,
                "max_rpm": r.max_rpm,
                "timeout": r.timeout,
                "fallback_upstream_url": r.fallback_upstream_url,
                "retry_policy": r.retry_policy,
                "circuit_breaker": r.circuit_breaker,
                "circuit_state": r.get_circuit_state(),
                "is_cooling_down": r.is_cooling_down(),
                "cooldown_remaining_seconds": round(r.cooldown_remaining(), 1),
                "cooldown_reason": r._cooldown_reason if r.is_cooling_down() else None,
                "consecutive_failures": r._consecutive_failures,
                "stats": r.limiter.get_stats(),
            })

        def_rule = self.default_rule
        def_stats = def_rule.limiter.get_stats() if def_rule else self.default_limiter.get_stats()
        return {
            "default": {
                "id": "default",
                "name": def_rule.name if def_rule else self.default_name,
                "pattern": def_rule.pattern if def_rule else ".*",
                "exclude_pattern": def_rule.exclude_pattern if def_rule else None,
                "upstream_url": def_rule.upstream_url if def_rule else self.default_upstream_url,
                "has_api_key": bool(def_rule.api_key if def_rule else self.default_api_key),
                "enabled": def_rule.enabled if def_rule else True,
                "is_default": True,
                "max_rpm": def_rule.max_rpm if def_rule else self.default_max_rpm,
                "timeout": def_rule.timeout if def_rule else self.default_timeout,
                "fallback_upstream_url": def_rule.fallback_upstream_url if def_rule else self.default_fallback_upstream_url,
                "retry_policy": def_rule.retry_policy if def_rule else self.default_retry_policy,
                "circuit_breaker": def_rule.circuit_breaker if def_rule else DEFAULT_CIRCUIT_BREAKER,
                "circuit_state": def_rule.get_circuit_state() if def_rule else "CLOSED",
                "is_cooling_down": def_rule.is_cooling_down() if def_rule else False,
                "cooldown_remaining_seconds": round(def_rule.cooldown_remaining(), 1) if def_rule else 0.0,
                "cooldown_reason": def_rule._cooldown_reason if (def_rule and def_rule.is_cooling_down()) else None,
                "consecutive_failures": def_rule._consecutive_failures if def_rule else 0,
                "stats": def_stats,
            },
            "routes": routes_stats,
        }

    def to_dict(self, mask_keys: bool = False) -> Dict[str, Any]:
        def_dict = self.default_rule.to_dict(mask_keys=mask_keys) if self.default_rule else {
            "id": "default",
            "name": self.default_name,
            "pattern": ".*",
            "upstream_url": self.default_upstream_url,
            "api_key": mask_api_key(self.default_api_key) if mask_keys else self.default_api_key,
            "has_api_key": bool(self.default_api_key),
            "enabled": True,
            "priority": 0,
            "strategy": "inherit",
            "max_concurrent": self.default_max_concurrent,
            "slot_cooldown_ms": self.default_slot_cooldown_ms,
            "max_rpm": self.default_max_rpm,
            "timeout": self.default_timeout,
            "fallback_upstream_url": self.default_fallback_upstream_url,
            "retry_policy": self.default_retry_policy,
            "exclude_pattern": None,
            "is_default": True,
            "limiter_stats": self.default_limiter.get_stats(),
        }
        custom_dicts = [r.to_dict(mask_keys=mask_keys) for r in self.rules if not r.is_default and r.id != "default"]
        return {
            "routing_strategy": self.routing_strategy,
            "default_route": def_dict,
            "routes": custom_dicts,
            "limiters_summary": self.get_all_limiters_stats(),
        }

    def update_from_dict(self, data: Dict[str, Any]) -> None:
        """
        Update router state from a dict, preserving live limiter instances.
        The default rule is preserved and cannot be removed.
        """
        if "routing_strategy" in data:
            self.routing_strategy = str(data["routing_strategy"]).strip().lower()

        routes_data = data.get("routes", [])
        def_rule_data = None
        for r in routes_data:
            if r.get("id") == "default" or r.get("is_default"):
                def_rule_data = r
                break
        if not def_rule_data and "default_route" in data:
            def_rule_data = data["default_route"]

        if def_rule_data:
            if "upstream_url" in def_rule_data:
                self.default_upstream_url = str(def_rule_data["upstream_url"]).strip().rstrip("/")
                self.default_rule.upstream_url = self.default_upstream_url
            if "name" in def_rule_data:
                self.default_name = str(def_rule_data["name"]).strip()
                self.default_rule.name = self.default_name
            if "pattern" in def_rule_data:
                self.default_rule.pattern = str(def_rule_data["pattern"]).strip() or ".*"
            if "exclude_pattern" in def_rule_data:
                self.default_rule.exclude_pattern = str(def_rule_data["exclude_pattern"]).strip() if def_rule_data["exclude_pattern"] else None
            if "enabled" in def_rule_data:
                self.default_rule.enabled = bool(def_rule_data["enabled"])
            if "priority" in def_rule_data:
                self.default_rule.priority = int(def_rule_data["priority"])
            if "strategy" in def_rule_data:
                self.default_rule.strategy = str(def_rule_data["strategy"]).strip().lower()
            if "api_key" in def_rule_data:
                incoming_def_key = def_rule_data["api_key"]
                if incoming_def_key is not None:
                    incoming_def_str = str(incoming_def_key).strip()
                    if not incoming_def_str:
                        self.default_api_key = None
                    elif self.default_api_key and incoming_def_str == mask_api_key(self.default_api_key):
                        pass
                    else:
                        self.default_api_key = incoming_def_str
                else:
                    self.default_api_key = None
                self.default_rule.api_key = self.default_api_key
            if "max_concurrent" in def_rule_data:
                self.default_max_concurrent = max(1, int(def_rule_data["max_concurrent"]))
                self.default_rule.max_concurrent = self.default_max_concurrent
                self.default_limiter.max_concurrent = self.default_max_concurrent
                self.default_rule.limiter.max_concurrent = self.default_max_concurrent
            if "slot_cooldown_ms" in def_rule_data:
                self.default_slot_cooldown_ms = max(0, int(def_rule_data["slot_cooldown_ms"]))
                self.default_rule.slot_cooldown_ms = self.default_slot_cooldown_ms
                self.default_limiter.slot_cooldown_seconds = self.default_slot_cooldown_ms / 1000.0
                self.default_rule.limiter.slot_cooldown_seconds = self.default_slot_cooldown_ms / 1000.0
            if "max_rpm" in def_rule_data:
                self.default_max_rpm = int(def_rule_data["max_rpm"]) if def_rule_data["max_rpm"] is not None else -1
                self.default_rule.max_rpm = self.default_max_rpm
                self.default_limiter.max_rpm = self.default_max_rpm
                self.default_rule.limiter.max_rpm = self.default_max_rpm
            if "timeout" in def_rule_data:
                self.default_timeout = normalize_timeout(def_rule_data["timeout"])
                self.default_rule.timeout = self.default_timeout
            if "fallback_upstream_url" in def_rule_data:
                self.default_fallback_upstream_url = def_rule_data["fallback_upstream_url"]
                self.default_rule.fallback_upstream_url = self.default_fallback_upstream_url
            if "retry_policy" in def_rule_data:
                self.default_retry_policy = normalize_retry_policy(def_rule_data["retry_policy"])
                self.default_rule.retry_policy = self.default_retry_policy
            if "circuit_breaker" in def_rule_data:
                self.default_circuit_breaker = normalize_circuit_breaker(def_rule_data["circuit_breaker"])
                self.default_rule.circuit_breaker = self.default_circuit_breaker

            self.default_rule._compile_regex()

        existing_rules_map = {r.id: r for r in self.rules}

        new_custom_rules = []
        for i, r in enumerate(routes_data):
            r_id = r.get("id")
            if r_id == "default" or r.get("is_default"):
                continue

            r_id = r_id or f"route_{uuid.uuid4().hex[:8]}"
            existing = existing_rules_map.get(r_id)

            priority_val = r.get("priority", 100 - i * 10)
            max_c = max(1, int(r.get("max_concurrent", 4)))
            slot_cd = max(0, int(r.get("slot_cooldown_ms", 50)))
            rpm_val = int(r.get("max_rpm", -1)) if r.get("max_rpm") is not None else -1
            to_val = r.get("timeout")
            fb_val = r.get("fallback_upstream_url")
            r_policy = r.get("retry_policy")
            cb_val = r.get("circuit_breaker")

            incoming_key = r.get("api_key")
            if incoming_key is not None:
                incoming_key_str = str(incoming_key).strip()
                if not incoming_key_str:
                    api_key_val = None
                elif existing and existing.api_key and incoming_key_str == mask_api_key(existing.api_key):
                    api_key_val = existing.api_key
                else:
                    api_key_val = incoming_key_str
            else:
                api_key_val = existing.api_key if existing else None

            rule = ModelRouteRule(
                id=r_id,
                name=r.get("name", ""),
                pattern=r.get("pattern", ""),
                upstream_url=r.get("upstream_url", self.default_upstream_url),
                api_key=api_key_val,
                enabled=r.get("enabled", True),
                priority=priority_val,
                strategy=r.get("strategy", existing.strategy if existing else "inherit"),
                max_concurrent=max_c,
                slot_cooldown_ms=slot_cd,
                max_rpm=rpm_val,
                timeout=to_val,
                fallback_upstream_url=fb_val,
                retry_policy=r_policy,
                circuit_breaker=cb_val,
                exclude_pattern=r.get("exclude_pattern"),
                limiter=existing.limiter if existing else None,
                is_default=False,
            )
            new_custom_rules.append(rule)

        new_custom_rules.sort(key=lambda x: x.priority, reverse=True)
        self.rules = new_custom_rules

