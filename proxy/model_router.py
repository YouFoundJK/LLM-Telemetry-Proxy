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
from typing import Any, Dict, List, Optional

DEFAULT_UPSTREAM_URL = "https://llm.ai.e-infra.cz/v1"


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


# ── Concurrency Limiter ───────────────────────────────────────────────────────
class _SlotContextManager:
    """Async context manager helper for limiter.slot()."""
    def __init__(self, limiter: 'UpstreamConcurrencyLimiter'):
        self.limiter = limiter

    async def __aenter__(self):
        await self.limiter.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.limiter.release()


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

    @property
    def active(self) -> int:
        return self._active_count

    @property
    def queued(self) -> int:
        return len(self._waiters)

    def record_429_retry(self):
        self._total_retries_429 += 1

    def get_stats(self) -> dict:
        return {
            "max_concurrent": self.max_concurrent,
            "active": self._active_count,
            "queued": len(self._waiters),
            "slot_cooldown_ms": int(round(self.slot_cooldown_seconds * 1000)),
            "total_admitted": self._total_admitted,
            "total_queued": self._total_queued,
            "total_retries_429": self._total_retries_429,
            "peak_active": self._peak_active,
        }

    def slot(self):
        """Returns an async context manager for acquiring and releasing a concurrency slot."""
        return _SlotContextManager(self)

    async def acquire(self):
        """Acquire a concurrency slot, waiting in FIFO order if max_concurrent is reached."""
        async with self._lock:
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
            while self._waiters:
                fut = self._waiters.popleft()
                if not fut.done() and not fut.cancelled():
                    self._total_admitted += 1
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


class ModelRouteRule:
    def __init__(
        self,
        id: str,
        name: str,
        pattern: str,
        upstream_url: str,
        enabled: bool = True,
        priority: int = 10,
        max_concurrent: int = 4,
        slot_cooldown_ms: int = 50,
        limiter: Optional[UpstreamConcurrencyLimiter] = None,
    ):
        self.id = str(id) if id else f"route_{uuid.uuid4().hex[:8]}"
        self.name = str(name).strip() if name else "Custom Route"
        self.pattern = str(pattern).strip()
        self.upstream_url = str(upstream_url).strip().rstrip("/")
        self.enabled = bool(enabled)
        self.priority = int(priority)
        self.max_concurrent = max(1, int(max_concurrent))
        self.slot_cooldown_ms = max(0, int(slot_cooldown_ms))
        
        self.limiter = limiter or UpstreamConcurrencyLimiter(
            max_concurrent=self.max_concurrent,
            slot_cooldown_seconds=self.slot_cooldown_ms / 1000.0,
        )
        self.limiter.max_concurrent = self.max_concurrent
        self.limiter.slot_cooldown_seconds = self.slot_cooldown_ms / 1000.0

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
            "enabled": self.enabled,
            "priority": self.priority,
            "max_concurrent": self.max_concurrent,
            "slot_cooldown_ms": self.slot_cooldown_ms,
        }

    def to_dict(self, mask_keys: bool = False) -> Dict[str, Any]:
        """Dictionary for API responses with live metrics."""
        return {
            "id": self.id,
            "name": self.name,
            "pattern": self.pattern,
            "upstream_url": self.upstream_url,
            "enabled": self.enabled,
            "priority": self.priority,
            "max_concurrent": self.max_concurrent,
            "slot_cooldown_ms": self.slot_cooldown_ms,
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
        self.default_max_concurrent = 4
        self.default_slot_cooldown_ms = 50
        self.default_limiter = UpstreamConcurrencyLimiter(
            max_concurrent=self.default_max_concurrent,
            slot_cooldown_seconds=self.default_slot_cooldown_ms / 1000.0,
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
            self.default_max_concurrent = max(1, int(def_route.get("max_concurrent", 4)))
            self.default_slot_cooldown_ms = max(0, int(def_route.get("slot_cooldown_ms", 50)))
            self.default_limiter.max_concurrent = self.default_max_concurrent
            self.default_limiter.slot_cooldown_seconds = self.default_slot_cooldown_ms / 1000.0

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
                    enabled=r.get("enabled", True),
                    priority=r.get("priority", 10),
                    max_concurrent=r.get("max_concurrent", 4),
                    slot_cooldown_ms=r.get("slot_cooldown_ms", 50),
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
                "max_concurrent": self.default_max_concurrent,
                "slot_cooldown_ms": self.default_slot_cooldown_ms,
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

    def resolve(self, model_name: Optional[str]) -> RouteResolutionResult:
        """
        Resolve target upstream for the requested model name.
        Executes in microsecond time.
        """
        if model_name:
            clean_name = str(model_name).strip()
            for rule in self.rules:
                if rule.matches(clean_name):
                    return RouteResolutionResult(
                        route_id=rule.id,
                        route_name=rule.name,
                        upstream_url=rule.upstream_url,
                        is_default=False,
                        pattern_matched=rule.pattern,
                        max_concurrent=rule.max_concurrent,
                        slot_cooldown_ms=rule.slot_cooldown_ms,
                    )

        # Fallback to default route
        return RouteResolutionResult(
            route_id=None,
            route_name=self.default_name,
            upstream_url=self.default_upstream_url,
            is_default=True,
            pattern_matched=None,
            max_concurrent=self.default_max_concurrent,
            slot_cooldown_ms=self.default_slot_cooldown_ms,
        )

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
                "enabled": r.enabled,
                "stats": r.limiter.get_stats(),
            })

        return {
            "default": {
                "name": self.default_name,
                "upstream_url": self.default_upstream_url,
                "stats": self.default_limiter.get_stats(),
            },
            "routes": routes_stats,
        }

    def apply_auth_and_headers(self, headers: Dict[str, str], resolution: RouteResolutionResult) -> Dict[str, str]:
        """
        Passthrough request headers untouched. Client credentials flow directly to upstream.
        """
        return headers

    def to_dict(self, mask_keys: bool = True) -> Dict[str, Any]:
        return {
            "default_route": {
                "name": self.default_name,
                "upstream_url": self.default_upstream_url,
                "max_concurrent": self.default_max_concurrent,
                "slot_cooldown_ms": self.default_slot_cooldown_ms,
                "limiter_stats": self.default_limiter.get_stats(),
            },
            "routes": [r.to_dict() for r in self.rules],
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
        if "max_concurrent" in def_route:
            self.default_max_concurrent = max(1, int(def_route["max_concurrent"]))
            self.default_limiter.max_concurrent = self.default_max_concurrent
        if "slot_cooldown_ms" in def_route:
            self.default_slot_cooldown_ms = max(0, int(def_route["slot_cooldown_ms"]))
            self.default_limiter.slot_cooldown_seconds = self.default_slot_cooldown_ms / 1000.0

        existing_rules_map = {r.id: r for r in self.rules}

        new_rules = []
        for i, r in enumerate(data.get("routes", [])):
            r_id = r.get("id") or f"route_{uuid.uuid4().hex[:8]}"
            existing = existing_rules_map.get(r_id)

            priority_val = r.get("priority", 100 - i * 10)
            max_c = max(1, int(r.get("max_concurrent", 4)))
            slot_cd = max(0, int(r.get("slot_cooldown_ms", 50)))

            rule = ModelRouteRule(
                id=r_id,
                name=r.get("name", ""),
                pattern=r.get("pattern", ""),
                upstream_url=r.get("upstream_url", self.default_upstream_url),
                enabled=r.get("enabled", True),
                priority=priority_val,
                max_concurrent=max_c,
                slot_cooldown_ms=slot_cd,
                limiter=existing.limiter if existing else None,
            )
            new_rules.append(rule)

        new_rules.sort(key=lambda x: x.priority, reverse=True)
        self.rules = new_rules

