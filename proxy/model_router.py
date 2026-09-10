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
    "sock_read": 60.0,
    "total": 180.0,
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
    api_key: Optional[str] = None


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
        limiter: Optional[UpstreamConcurrencyLimiter] = None,
    ):
        self.id = str(id) if id else f"route_{uuid.uuid4().hex[:8]}"
        self.name = str(name).strip() if name else "Custom Route"
        self.pattern = str(pattern).strip()
        self.upstream_url = str(upstream_url).strip().rstrip("/")
        self.api_key = str(api_key).strip() if api_key and str(api_key).strip() else None
        self.enabled = bool(enabled)
        self.priority = int(priority)
        self.max_concurrent = max(1, int(max_concurrent))
        self.slot_cooldown_ms = max(0, int(slot_cooldown_ms))
        self.max_rpm = int(max_rpm) if max_rpm is not None else -1
        self.timeout = normalize_timeout(timeout)
        self.fallback_upstream_url = str(fallback_upstream_url).strip().rstrip("/") if fallback_upstream_url else None
        self.retry_policy = normalize_retry_policy(retry_policy)
        
        self.limiter = limiter or UpstreamConcurrencyLimiter(
            max_concurrent=self.max_concurrent,
            slot_cooldown_seconds=self.slot_cooldown_ms / 1000.0,
            max_rpm=self.max_rpm,
        )
        self.limiter.max_concurrent = self.max_concurrent
        self.limiter.slot_cooldown_seconds = self.slot_cooldown_ms / 1000.0
        self.limiter.max_rpm = self.max_rpm

        self._compiled: Optional[re.Pattern] = None
        self._compile_regex()

    def _compile_regex(self):
        if not self.pattern:
            self._compiled = None
            return
        try:
            self._compiled = re.compile(self.pattern, re.IGNORECASE)
        except re.error as e:
            print(f"[ModelRouter] Invalid regex pattern '{self.pattern}' for rule '{self.name}': {e}", file=sys.stderr)
            self._compiled = None

    def matches(self, model_name: str) -> bool:
        if not self.enabled or not self._compiled or not model_name:
            return False
        return bool(self._compiled.search(model_name))

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
            "max_concurrent": self.max_concurrent,
            "slot_cooldown_ms": self.slot_cooldown_ms,
            "max_rpm": self.max_rpm,
            "timeout": self.timeout,
            "fallback_upstream_url": self.fallback_upstream_url,
            "retry_policy": self.retry_policy,
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
            "max_concurrent": self.max_concurrent,
            "slot_cooldown_ms": self.slot_cooldown_ms,
            "max_rpm": self.max_rpm,
            "timeout": self.timeout,
            "fallback_upstream_url": self.fallback_upstream_url,
            "retry_policy": self.retry_policy,
            "limiter_stats": self.limiter.get_stats(),
        }


class ModelRouter:
    """
    Thread-safe, microsecond-latency model router and per-upstream concurrency limiter.
    """

    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path
        self.default_upstream_url = DEFAULT_UPSTREAM_URL
        self.default_name = "Default Upstream (e-INFRA)"
        self.default_api_key: Optional[str] = None
        self.default_max_concurrent = 4
        self.default_slot_cooldown_ms = 50
        self.default_max_rpm = -1
        self.default_timeout = normalize_timeout(None)
        self.default_fallback_upstream_url = None
        self.default_retry_policy = normalize_retry_policy(None)
        self.default_limiter = UpstreamConcurrencyLimiter(
            max_concurrent=self.default_max_concurrent,
            slot_cooldown_seconds=self.default_slot_cooldown_ms / 1000.0,
            max_rpm=self.default_max_rpm,
        )
        self.rules: List[ModelRouteRule] = []

        if self.config_path and self.config_path.exists():
            self.load()

    def load(self, path: Optional[Path] = None) -> bool:
        target_path = path or self.config_path
        if not target_path or not target_path.exists():
            return False

        try:
            with open(target_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            def_route = data.get("default_route", {})
            self.default_upstream_url = def_route.get("upstream_url", DEFAULT_UPSTREAM_URL).rstrip("/")
            self.default_name = def_route.get("name", "Default Upstream")
            self.default_api_key = str(def_route["api_key"]).strip() if def_route.get("api_key") else None
            self.default_max_concurrent = max(1, int(def_route.get("max_concurrent", 4)))
            self.default_slot_cooldown_ms = max(0, int(def_route.get("slot_cooldown_ms", 50)))
            self.default_max_rpm = int(def_route.get("max_rpm", -1)) if def_route.get("max_rpm") is not None else -1
            self.default_timeout = normalize_timeout(def_route.get("timeout"))
            self.default_fallback_upstream_url = def_route.get("fallback_upstream_url")
            self.default_retry_policy = normalize_retry_policy(def_route.get("retry_policy"))
            self.default_limiter.max_concurrent = self.default_max_concurrent
            self.default_limiter.slot_cooldown_seconds = self.default_slot_cooldown_ms / 1000.0
            self.default_limiter.max_rpm = self.default_max_rpm

            existing_rules_map = {r.id: r for r in self.rules}
            loaded_rules = []
            for r in data.get("routes", []):
                r_id = r.get("id")
                existing = existing_rules_map.get(r_id) if r_id else None
                rule = ModelRouteRule(
                    id=r_id,
                    name=r.get("name", ""),
                    pattern=r.get("pattern", ""),
                    upstream_url=r.get("upstream_url", DEFAULT_UPSTREAM_URL),
                    api_key=r.get("api_key"),
                    enabled=r.get("enabled", True),
                    priority=r.get("priority", 10),
                    max_concurrent=r.get("max_concurrent", 4),
                    slot_cooldown_ms=r.get("slot_cooldown_ms", 50),
                    max_rpm=r.get("max_rpm", -1),
                    timeout=r.get("timeout"),
                    fallback_upstream_url=r.get("fallback_upstream_url"),
                    retry_policy=r.get("retry_policy"),
                    limiter=existing.limiter if existing else None,
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

        data = {
            "default_route": {
                "name": self.default_name,
                "upstream_url": self.default_upstream_url,
                "api_key": self.default_api_key,
                "max_concurrent": self.default_max_concurrent,
                "slot_cooldown_ms": self.default_slot_cooldown_ms,
                "max_rpm": self.default_max_rpm,
                "timeout": self.default_timeout,
                "fallback_upstream_url": self.default_fallback_upstream_url,
                "retry_policy": self.default_retry_policy,
            },
            "routes": [r.to_config_dict() for r in self.rules],
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

    def resolve_chain(self, model_name: Optional[str]) -> List[RouteResolutionResult]:
        """
        Resolve all matching candidate routes for the requested model name in priority order.
        If no custom rules match, returns a single-item list containing the default route.
        """
        matches: List[RouteResolutionResult] = []
        if model_name:
            clean_name = str(model_name).strip()
            for rule in self.rules:
                if rule.matches(clean_name):
                    matches.append(
                        RouteResolutionResult(
                            route_id=rule.id,
                            route_name=rule.name,
                            upstream_url=rule.upstream_url,
                            is_default=False,
                            pattern_matched=rule.pattern,
                            max_concurrent=rule.max_concurrent,
                            slot_cooldown_ms=rule.slot_cooldown_ms,
                            max_rpm=rule.max_rpm,
                            timeout=rule.timeout,
                            fallback_upstream_url=rule.fallback_upstream_url,
                            retry_policy=rule.retry_policy,
                            api_key=rule.api_key,
                        )
                    )

        if not matches:
            matches.append(
                RouteResolutionResult(
                    route_id=None,
                    route_name=self.default_name,
                    upstream_url=self.default_upstream_url,
                    is_default=True,
                    pattern_matched=None,
                    max_concurrent=self.default_max_concurrent,
                    slot_cooldown_ms=self.default_slot_cooldown_ms,
                    max_rpm=self.default_max_rpm,
                    timeout=self.default_timeout,
                    fallback_upstream_url=self.default_fallback_upstream_url,
                    retry_policy=self.default_retry_policy,
                    api_key=self.default_api_key,
                )
            )

        return matches

    def resolve(self, model_name: Optional[str]) -> RouteResolutionResult:
        """
        Resolve highest-priority target upstream for the requested model name.
        Executes in microsecond time.
        """
        return self.resolve_chain(model_name)[0]

    async def select_admission_route(
        self, candidates: List[RouteResolutionResult]
    ) -> Tuple[RouteResolutionResult, UpstreamConcurrencyLimiter, bool]:
        """
        Select an admission route following priority precedence:
        1. Checks candidates in priority order; if a route has immediate capacity (no queue,
           active < max_concurrent, and within max_rpm), atomically acquires it (already_acquired=True).
        2. If all candidate routes are currently saturated / queued, selects the candidate
           with the shortest wait queue (minimum limiter.queued, breaking ties by priority).
           Returns (route, limiter, already_acquired=False).
        """
        if not candidates:
            def_route = self.resolve(None)
            limiter = self.get_limiter(def_route.route_id)
            acquired = await limiter.try_acquire()
            return def_route, limiter, acquired

        if len(candidates) == 1:
            limiter = self.get_limiter(candidates[0].route_id)
            acquired = await limiter.try_acquire()
            return candidates[0], limiter, acquired

        # 1. Zero-wait overspill down the priority chain
        for cand in candidates:
            limiter = self.get_limiter(cand.route_id)
            if await limiter.try_acquire():
                return cand, limiter, True

        # 2. All routes are saturated: pick shortest queue (break ties by priority, which is original list order)
        best_candidate = candidates[0]
        best_limiter = self.get_limiter(best_candidate.route_id)
        min_queued = best_limiter.queued

        for cand in candidates[1:]:
            lim = self.get_limiter(cand.route_id)
            q_depth = lim.queued
            if q_depth < min_queued:
                min_queued = q_depth
                best_candidate = cand
                best_limiter = lim

        return best_candidate, best_limiter, False

    def get_limiter(self, route_id: Optional[str] = None) -> UpstreamConcurrencyLimiter:
        """
        Get the concurrency limiter for a specific route, or the default limiter.
        """
        if route_id:
            for rule in self.rules:
                if rule.id == route_id and rule.enabled:
                    return rule.limiter
        return self.default_limiter

    def get_all_limiters_stats(self) -> Dict[str, Any]:
        """
        Return live concurrency and queue metrics across all active routes and the default router.
        """
        routes_stats = []
        for r in self.rules:
            routes_stats.append({
                "id": r.id,
                "name": r.name,
                "pattern": r.pattern,
                "upstream_url": r.upstream_url,
                "has_api_key": bool(r.api_key),
                "enabled": r.enabled,
                "max_rpm": r.max_rpm,
                "timeout": r.timeout,
                "fallback_upstream_url": r.fallback_upstream_url,
                "retry_policy": r.retry_policy,
                "stats": r.limiter.get_stats(),
            })

        return {
            "default": {
                "name": self.default_name,
                "upstream_url": self.default_upstream_url,
                "has_api_key": bool(self.default_api_key),
                "max_rpm": self.default_max_rpm,
                "timeout": self.default_timeout,
                "fallback_upstream_url": self.default_fallback_upstream_url,
                "retry_policy": self.default_retry_policy,
                "stats": self.default_limiter.get_stats(),
            },
            "routes": routes_stats,
        }

    def to_dict(self, mask_keys: bool = False) -> Dict[str, Any]:
        return {
            "default_route": {
                "name": self.default_name,
                "upstream_url": self.default_upstream_url,
                "api_key": mask_api_key(self.default_api_key) if mask_keys else self.default_api_key,
                "has_api_key": bool(self.default_api_key),
                "max_concurrent": self.default_max_concurrent,
                "slot_cooldown_ms": self.default_slot_cooldown_ms,
                "max_rpm": self.default_max_rpm,
                "timeout": self.default_timeout,
                "fallback_upstream_url": self.default_fallback_upstream_url,
                "retry_policy": self.default_retry_policy,
                "limiter_stats": self.default_limiter.get_stats(),
            },
            "routes": [r.to_dict(mask_keys=mask_keys) for r in self.rules],
            "limiters_summary": self.get_all_limiters_stats(),
        }

    def update_from_dict(self, data: Dict[str, Any]) -> None:
        """
        Update router state from a dict, preserving live limiter instances.
        """
        def_route = data.get("default_route", {})
        if "upstream_url" in def_route:
            self.default_upstream_url = def_route["upstream_url"].rstrip("/")
        if "name" in def_route:
            self.default_name = def_route["name"]
        if "api_key" in def_route:
            incoming_def_key = def_route["api_key"]
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
        if "max_concurrent" in def_route:
            self.default_max_concurrent = max(1, int(def_route["max_concurrent"]))
            self.default_limiter.max_concurrent = self.default_max_concurrent
        if "slot_cooldown_ms" in def_route:
            self.default_slot_cooldown_ms = max(0, int(def_route["slot_cooldown_ms"]))
            self.default_limiter.slot_cooldown_seconds = self.default_slot_cooldown_ms / 1000.0
        if "max_rpm" in def_route:
            self.default_max_rpm = int(def_route["max_rpm"]) if def_route["max_rpm"] is not None else -1
            self.default_limiter.max_rpm = self.default_max_rpm
        if "timeout" in def_route:
            self.default_timeout = normalize_timeout(def_route["timeout"])
        if "fallback_upstream_url" in def_route:
            self.default_fallback_upstream_url = def_route["fallback_upstream_url"]
        if "retry_policy" in def_route:
            self.default_retry_policy = normalize_retry_policy(def_route["retry_policy"])

        existing_rules_map = {r.id: r for r in self.rules}

        new_rules = []
        for i, r in enumerate(data.get("routes", [])):
            r_id = r.get("id") or f"route_{uuid.uuid4().hex[:8]}"
            existing = existing_rules_map.get(r_id)

            priority_val = r.get("priority", 100 - i * 10)
            max_c = max(1, int(r.get("max_concurrent", 4)))
            slot_cd = max(0, int(r.get("slot_cooldown_ms", 50)))
            rpm_val = int(r.get("max_rpm", -1)) if r.get("max_rpm") is not None else -1
            to_val = r.get("timeout")
            fb_val = r.get("fallback_upstream_url")
            r_policy = r.get("retry_policy")

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
                max_concurrent=max_c,
                slot_cooldown_ms=slot_cd,
                max_rpm=rpm_val,
                timeout=to_val,
                fallback_upstream_url=fb_val,
                retry_policy=r_policy,
                limiter=existing.limiter if existing else None,
            )
            new_rules.append(rule)

        new_rules.sort(key=lambda x: x.priority, reverse=True)
        self.rules = new_rules

