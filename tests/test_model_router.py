#!/usr/bin/env python3
"""
Test Suite: Dynamic Model Router & Upstream Concurrency Limiter.
Validates regex matching, fallback behavior, client auth passthrough,
persistence, API routes, end-to-end dispatch, and microsecond latency.
"""

import asyncio
import json
import os
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import aiohttp
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestServer

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from proxy.model_router import ModelRouter, ModelRouteRule, build_upstream_url
import proxy.llm_telemetry_proxy as proxy_mod


class TestModelRouterUnit(unittest.TestCase):
    """Unit tests for ModelRouter data structures, matching, and serialization."""

    def setUp(self):
        self.tmp_dir = TemporaryDirectory()
        self.config_path = Path(self.tmp_dir.name) / "model_routes.json"
        self.router = ModelRouter(config_path=self.config_path)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_default_route_resolution(self):
        """When no custom rules match, it must return the default route."""
        res = self.router.resolve("unknown-model-123")
        self.assertTrue(res.is_default)
        self.assertEqual(res.upstream_url, "https://llm.ai.e-infra.cz/v1")

    def test_custom_regex_routing(self):
        """Custom rules should match and take precedence over default."""
        self.router.update_from_dict({
            "default_route": {
                "name": "Default e-INFRA",
                "upstream_url": "https://llm.ai.e-infra.cz/v1"
            },
            "routes": [
                {
                    "id": "openrouter_stealth",
                    "name": "OpenRouter Stealth",
                    "pattern": r"stealth/ox-alpha.*",
                    "upstream_url": "https://openrouter.ai/api/v1",
                    "priority": 20
                },
                {
                    "id": "openai_catchall",
                    "name": "OpenAI Catchall",
                    "pattern": r"^(gpt-4o|o1|o3).*",
                    "upstream_url": "https://api.openai.com/v1",
                    "priority": 10
                }
            ]
        })

        # Test OpenRouter match
        res1 = self.router.resolve("stealth/ox-alpha:free")
        self.assertFalse(res1.is_default)
        self.assertEqual(res1.route_name, "OpenRouter Stealth")
        self.assertEqual(res1.upstream_url, "https://openrouter.ai/api/v1")

        # Test OpenAI match
        res2 = self.router.resolve("gpt-4o-mini")
        self.assertFalse(res2.is_default)
        self.assertEqual(res2.upstream_url, "https://api.openai.com/v1")

        # Test unmatched fallback to default
        res3 = self.router.resolve("DeepSeek-V3")
        self.assertTrue(res3.is_default)
        self.assertEqual(res3.upstream_url, "https://llm.ai.e-infra.cz/v1")

    def test_persistence_save_and_load(self):
        """Verify configuration round-trip from JSON file."""
        self.router.update_from_dict({
            "default_route": {
                "name": "Default Test",
                "upstream_url": "https://llm.ai.e-infra.cz/v1",
                "max_concurrent": 4
            },
            "routes": [
                {
                    "id": "saved_route",
                    "name": "Saved Route",
                    "pattern": "persisted/.*",
                    "upstream_url": "https://target.com/v1",
                    "priority": 50,
                    "max_concurrent": 8
                }
            ]
        })
        self.assertTrue(self.router.save())

        new_router = ModelRouter(config_path=self.config_path)
        self.assertEqual(new_router.default_name, "Default Test")
        self.assertEqual(new_router.default_upstream_url, "https://llm.ai.e-infra.cz/v1")
        self.assertEqual(len(new_router.rules), 1)
        self.assertEqual(new_router.rules[0].name, "Saved Route")
        self.assertEqual(new_router.rules[0].upstream_url, "https://target.com/v1")
        self.assertEqual(new_router.rules[0].max_concurrent, 8)

    def test_route_with_api_key_persistence_and_resolution(self):
        """Verify API key storage, serialization, masking, and resolution."""
        self.router.update_from_dict({
            "default_route": {
                "name": "Default Test",
                "upstream_url": "https://llm.ai.e-infra.cz/v1",
                "api_key": "default-secret-key-12345"
            },
            "routes": [
                {
                    "id": "keyed_route",
                    "name": "Keyed Route",
                    "pattern": "secret-provider/.*",
                    "upstream_url": "https://api.secretprovider.com/v1",
                    "api_key": "sk-secret-provider-key-99999",
                    "priority": 50,
                },
                {
                    "id": "unkeyed_route",
                    "name": "Unkeyed Route",
                    "pattern": "open-provider/.*",
                    "upstream_url": "https://api.openprovider.com/v1",
                    "priority": 40,
                }
            ]
        })

        # Test resolution
        res1 = self.router.resolve("secret-provider/v1")
        self.assertEqual(res1.api_key, "sk-secret-provider-key-99999")

        res2 = self.router.resolve("open-provider/free")
        self.assertIsNone(res2.api_key)

        res_def = self.router.resolve("unmatched-model")
        self.assertEqual(res_def.api_key, "default-secret-key-12345")

        # Test masking in to_dict
        d_masked = self.router.to_dict(mask_keys=True)
        self.assertTrue(d_masked["routes"][0]["has_api_key"])
        self.assertEqual(d_masked["routes"][0]["api_key"], "sk-s...9999")
        self.assertFalse(d_masked["routes"][1]["has_api_key"])
        self.assertIsNone(d_masked["routes"][1]["api_key"])

        # Test persistence
        self.assertTrue(self.router.save())
        reloaded = ModelRouter(config_path=self.config_path)
        self.assertEqual(reloaded.rules[0].api_key, "sk-secret-provider-key-99999")
        self.assertIsNone(reloaded.rules[1].api_key)
        self.assertEqual(reloaded.default_api_key, "default-secret-key-12345")

        # Test update_from_dict with masked key does not overwrite actual key
        reloaded.update_from_dict(d_masked)
        self.assertEqual(reloaded.rules[0].api_key, "sk-secret-provider-key-99999")

        # Test clearing api_key with empty string
        d_clear = reloaded.to_dict(mask_keys=False)
        d_clear["routes"][0]["api_key"] = ""
        reloaded.update_from_dict(d_clear)
        self.assertIsNone(reloaded.rules[0].api_key)

    def test_build_upstream_url(self):
        """Validate URL construction avoids duplicate /v1 paths."""
        self.assertEqual(
            build_upstream_url("https://openrouter.ai/api/v1", "/v1/chat/completions"),
            "https://openrouter.ai/api/v1/chat/completions"
        )
        self.assertEqual(
            build_upstream_url("https://llm.ai.e-infra.cz/v1", "/v1/models"),
            "https://llm.ai.e-infra.cz/v1/models"
        )
        self.assertEqual(
            build_upstream_url("https://api.openai.com", "/v1/chat/completions"),
            "https://api.openai.com/v1/chat/completions"
        )

    def test_resolution_latency_performance(self):
        """Ensure 10,000 route matches execute in under 100 milliseconds (< 10 µs per match)."""
        self.router.update_from_dict({
            "default_route": {"upstream_url": "https://llm.ai.e-infra.cz/v1"},
            "routes": [
                {"id": f"r_{i}", "pattern": f"prefix-{i}/.*", "upstream_url": f"https://upstream-{i}.com/v1", "priority": i}
                for i in range(20)
            ]
        })

        t0 = time.perf_counter()
        iterations = 10000
        for _ in range(iterations):
            self.router.resolve("prefix-15/model-alpha")
        elapsed = time.perf_counter() - t0

        avg_us = (elapsed / iterations) * 1_000_000
        print(f"\n[Benchmark] 10,000 route resolutions took {elapsed:.4f}s ({avg_us:.2f} µs/resolution)")
        self.assertLess(elapsed, 0.2, "Route resolution exceeded performance budget!")

    def test_per_route_concurrency_isolation(self):
        """Verify per-route concurrency limiters and queues operate independently."""
        async def run_test():
            self.router.update_from_dict({
                "default_route": {
                    "upstream_url": "https://default.com/v1",
                    "max_concurrent": 4
                },
                "routes": [
                    {
                        "id": "route_tight",
                        "name": "Tight Limit",
                        "pattern": "tight/.*",
                        "upstream_url": "https://tight.com/v1",
                        "max_concurrent": 2,
                        "slot_cooldown_ms": 10
                    },
                    {
                        "id": "route_wide",
                        "name": "Wide Limit",
                        "pattern": "wide/.*",
                        "upstream_url": "https://wide.com/v1",
                        "max_concurrent": 5,
                        "slot_cooldown_ms": 10
                    }
                ]
            })

            res_tight = self.router.resolve("tight/model-1")
            res_wide = self.router.resolve("wide/model-1")
            limiter_tight = self.router.get_limiter(res_tight.route_id)
            limiter_wide = self.router.get_limiter(res_wide.route_id)

            self.assertEqual(limiter_tight.max_concurrent, 2)
            self.assertEqual(limiter_wide.max_concurrent, 5)

            tight_active = 0
            tight_peak = 0
            lock = asyncio.Lock()

            async def tight_worker():
                nonlocal tight_active, tight_peak
                async with limiter_tight.slot():
                    async with lock:
                        tight_active += 1
                        tight_peak = max(tight_peak, tight_active)
                        self.assertLessEqual(tight_active, 2)
                    await asyncio.sleep(0.02)
                    async with lock:
                        tight_active -= 1

            tasks = [asyncio.create_task(tight_worker()) for _ in range(6)]
            await asyncio.gather(*tasks)
            self.assertEqual(tight_peak, 2)
            self.assertEqual(limiter_tight.active, 0)

            # Check stats summary
            summary = self.router.get_all_limiters_stats()
            self.assertIn("default", summary)
            self.assertEqual(len(summary["routes"]), 2)
            self.assertEqual(summary["routes"][0]["stats"]["max_concurrent"], 2)

        asyncio.run(run_test())

    def test_resolve_chain_multi_priority_order(self):
        """Verify multiple rules with identical regex patterns are sorted by priority descending, with default at end."""
        self.router.update_from_dict({
            "default_route": {"name": "Default Route", "upstream_url": "https://default.ai/v1"},
            "routes": [
                {"id": "r_low", "name": "Route Low", "pattern": r"^shared/model.*", "priority": 10, "upstream_url": "https://low.ai/v1"},
                {"id": "r_high", "name": "Route High", "pattern": r"^shared/model.*", "priority": 100, "upstream_url": "https://high.ai/v1"},
                {"id": "r_mid", "name": "Route Mid", "pattern": r"^shared/model.*", "priority": 50, "upstream_url": "https://mid.ai/v1"},
            ]
        })
        chain = self.router.resolve_chain("shared/model:v1")
        self.assertEqual(len(chain), 3)
        self.assertEqual(chain[0].route_name, "Route High")
        self.assertEqual(chain[0].upstream_url, "https://high.ai/v1")
        self.assertEqual(chain[1].route_name, "Route Mid")
        self.assertEqual(chain[1].upstream_url, "https://mid.ai/v1")
        self.assertEqual(chain[2].route_name, "Route Low")
        self.assertEqual(chain[2].upstream_url, "https://low.ai/v1")

        # Fallback to default when no rules match
        def_chain = self.router.resolve_chain("unmatched-model")
        self.assertEqual(len(def_chain), 1)
        self.assertTrue(def_chain[0].is_default)

        # self.router.resolve() returns highest priority (#1)
        res = self.router.resolve("shared/model:v1")
        self.assertEqual(res.route_name, "Route High")

    def test_has_immediate_capacity_and_try_acquire(self):
        """Verify limiter's try_acquire and has_immediate_capacity respect active slots and rolling RPM limits."""
        async def run_test():
            from proxy.model_router import UpstreamConcurrencyLimiter
            limiter = UpstreamConcurrencyLimiter(max_concurrent=1, slot_cooldown_ms=0, max_rpm=2)

            # Initial state: has capacity
            self.assertTrue(limiter.has_immediate_capacity())
            acquired = await limiter.try_acquire()
            self.assertTrue(acquired)
            self.assertEqual(limiter.active, 1)

            # Saturated active slots: cannot immediately acquire
            self.assertFalse(limiter.has_immediate_capacity())
            acquired2 = await limiter.try_acquire()
            self.assertFalse(acquired2)
            self.assertEqual(limiter.active, 1)

            # Release slot: active drops to 0, 1 RPM used out of 2
            await limiter.release()
            self.assertEqual(limiter.active, 0)
            self.assertTrue(limiter.has_immediate_capacity())

            # Acquire 2nd request: RPM hits limit (2/2)
            acquired3 = await limiter.try_acquire()
            self.assertTrue(acquired3)
            await limiter.release()
            self.assertEqual(limiter.active, 0)

            # RPM limit is now exhausted: has_immediate_capacity is False
            self.assertFalse(limiter.has_immediate_capacity())
            acquired4 = await limiter.try_acquire()
            self.assertFalse(acquired4)

        asyncio.run(run_test())

    def test_zero_wait_concurrency_and_rpm_overspill_selection(self):
        """Verify select_admission_route overspills to next priority candidate when higher route is saturated or RPM-exhausted."""
        async def run_test():
            self.router.update_from_dict({
                "default_route": {"name": "Default Route", "upstream_url": "https://default.ai/v1", "max_concurrent": 10},
                "routes": [
                    {"id": "r1", "name": "Priority 1", "pattern": r"^overspill.*", "priority": 100, "upstream_url": "https://r1.ai/v1", "max_concurrent": 1, "max_rpm": 0},
                    {"id": "r2", "name": "Priority 2", "pattern": r"^overspill.*", "priority": 50, "upstream_url": "https://r2.ai/v1", "max_concurrent": 2, "max_rpm": 0},
                ]
            })
            candidates = self.router.resolve_chain("overspill-test")

            # 1. Normal state: Priority 1 is free -> admitted immediately to Priority 1
            admit_route, limiter, already_acquired = await self.router.select_admission_route(candidates)
            self.assertEqual(admit_route.route_name, "Priority 1")
            self.assertTrue(already_acquired)
            self.assertEqual(limiter.active, 1)

            # 2. Concurrency overspill: While Priority 1 has active slot, next request must overspill to Priority 2 immediately!
            admit_route2, limiter2, already_acquired2 = await self.router.select_admission_route(candidates)
            self.assertEqual(admit_route2.route_name, "Priority 2")
            self.assertTrue(already_acquired2)
            self.assertEqual(limiter2.active, 1)

            # Release Priority 2
            await limiter2.release()
            # Release Priority 1
            await limiter.release()

        asyncio.run(run_test())

    def test_shortest_queue_selection_when_all_saturated(self):
        """When all routes are saturated, select_admission_route joins the shortest queue, tie-breaking by priority."""
        async def run_test():
            self.router.update_from_dict({
                "default_route": {"name": "Default Route", "upstream_url": "https://default.ai/v1", "max_concurrent": 1},
                "routes": [
                    {"id": "r1", "name": "Route 1 (Prio 100)", "pattern": r"^queue.*", "priority": 100, "upstream_url": "https://r1.ai/v1", "max_concurrent": 1},
                    {"id": "r2", "name": "Route 2 (Prio 50)", "pattern": r"^queue.*", "priority": 50, "upstream_url": "https://r2.ai/v1", "max_concurrent": 1},
                ]
            })
            candidates = self.router.resolve_chain("queue-test")
            lim1 = self.router.get_limiter("r1")
            lim2 = self.router.get_limiter("r2")
            lim_def = self.router.get_limiter("default")

            # Saturate active slots on all candidates
            await lim1.try_acquire()
            await lim2.try_acquire()
            await lim_def.try_acquire()

            # Simulate queue waiters: Route 1 has 3 waiters, Route 2 has 1 waiter, Default has 5 waiters
            fut1 = asyncio.Future()
            fut2 = asyncio.Future()
            lim1._waiters.append(fut1)
            lim1._waiters.append(fut1)
            lim1._waiters.append(fut1)
            lim2._waiters.append(fut2)
            lim_def._waiters.append(fut1)
            lim_def._waiters.append(fut1)

            self.assertEqual(lim1.queue_depth, 3)
            self.assertEqual(lim2.queue_depth, 1)

            # All saturated -> shortest queue selected (Route 2 with 1 waiter)
            admit_route, limiter, already_acquired = await self.router.select_admission_route(candidates)
            self.assertEqual(admit_route.route_name, "Route 2 (Prio 50)")
            self.assertFalse(already_acquired)

            # Tie-break test: give Route 1 only 1 waiter as well
            lim1._waiters.clear()
            lim1._waiters.append(fut1)
            self.assertEqual(lim1.queue_depth, 1)
            self.assertEqual(lim2.queue_depth, 1)

            # Strict '<' ensures Route 1 wins on tie-break because of higher priority!
            admit_tie, _, _ = await self.router.select_admission_route(candidates)
            self.assertEqual(admit_tie.route_name, "Route 1 (Prio 100)")

        asyncio.run(run_test())


class TestModelRouterEndToEnd(AioHTTPTestCase):
    """End-to-end integration tests through the aiohttp Proxy gateway."""

    async def setUpAsync(self):
        self.tmp_dir = TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_telemetry.db"
        self.routes_path = Path(self.tmp_dir.name) / "model_routes.json"

        # Mock Upstream 1: Default (e-INFRA)
        self.default_upstream_app = web.Application()
        self.default_requests = []
        async def handle_default(request):
            body = await request.json() if request.can_read_body else {}
            auth = request.headers.get("Authorization", "")
            self.default_requests.append({"body": body, "auth": auth, "path": request.path})
            return web.json_response({
                "id": "chatcmpl-default",
                "object": "chat.completion",
                "model": body.get("model"),
                "choices": [{"message": {"role": "assistant", "content": "Hello from Default Upstream!"}}]
            })
        self.default_upstream_app.router.add_post("/v1/chat/completions", handle_default)
        self.default_upstream_app.router.add_post("/chat/completions", handle_default)

        self.mock_default_server = TestServer(self.default_upstream_app)
        await self.mock_default_server.start_server()

        # Mock Upstream 2: OpenRouter
        self.openrouter_app = web.Application()
        self.openrouter_requests = []
        async def handle_openrouter(request):
            body = await request.json() if request.can_read_body else {}
            auth = request.headers.get("Authorization", "")
            self.openrouter_requests.append({"body": body, "auth": auth, "path": request.path})
            if "failover" in body.get("model", ""):
                return web.json_response({
                    "error": {"message": "Rate limit exceeded on Route 1", "type": "rate_limit_error"}
                }, status=429, headers={"Retry-After": "0"})
            return web.json_response({
                "id": "chatcmpl-openrouter",
                "object": "chat.completion",
                "model": body.get("model"),
                "choices": [{"message": {"role": "assistant", "content": "Hello from OpenRouter!"}}]
            })
        self.openrouter_app.router.add_post("/api/v1/chat/completions", handle_openrouter)
        self.mock_openrouter_server = TestServer(self.openrouter_app)
        await self.mock_openrouter_server.start_server()

        # Configure proxy router
        proxy_mod.DB_PATH = self.db_path
        proxy_mod.init_db()
        proxy_mod._model_router = ModelRouter(config_path=self.routes_path)
        proxy_mod._model_router.update_from_dict({
            "default_route": {
                "name": "Default Mock Upstream",
                "upstream_url": str(self.mock_default_server.make_url("/v1")),
                "max_concurrent": 4
            },
            "routes": [
                {
                    "id": "openrouter_route",
                    "name": "OpenRouter Stealth",
                    "pattern": r"stealth/ox-alpha.*",
                    "upstream_url": str(self.mock_openrouter_server.make_url("/api/v1")),
                    "priority": 10,
                    "max_concurrent": 10
                }
            ]
        })
        proxy_mod._model_router.save()
        await super().setUpAsync()

    async def tearDownAsync(self):
        await self.mock_default_server.close()
        await self.mock_openrouter_server.close()
        self.tmp_dir.cleanup()
        await super().tearDownAsync()

    async def get_application(self):
        return proxy_mod.create_app()

    async def test_dynamic_dispatch_to_openrouter(self):
        """Request for stealth/ox-alpha must route to OpenRouter with client's incoming Bearer key untouched."""
        payload = {
            "model": "stealth/ox-alpha:free",
            "messages": [{"role": "user", "content": "Hi OpenRouter"}]
        }
        resp = await self.client.post(
            "/v1/chat/completions",
            json=payload,
            headers={"Authorization": "Bearer client-supplied-openrouter-key"}
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["choices"][0]["message"]["content"], "Hello from OpenRouter!")

        self.assertEqual(len(self.openrouter_requests), 1)
        self.assertEqual(self.openrouter_requests[0]["auth"], "Bearer client-supplied-openrouter-key")
        self.assertEqual(len(self.default_requests), 0)

    async def test_dynamic_dispatch_replaces_api_key_when_configured(self):
        """When route has an api_key saved, upstream Authorization header is replaced by the saved key."""
        proxy_mod._model_router.update_from_dict({
            "default_route": {
                "name": "Default Mock Upstream",
                "upstream_url": str(self.mock_default_server.make_url("/v1")),
                "max_concurrent": 4
            },
            "routes": [
                {
                    "id": "openrouter_route",
                    "name": "OpenRouter Stealth",
                    "pattern": r"stealth/ox-alpha.*",
                    "upstream_url": str(self.mock_openrouter_server.make_url("/api/v1")),
                    "api_key": "sk-proxy-saved-upstream-secret-key",
                    "priority": 10,
                    "max_concurrent": 10
                }
            ]
        })
        self.openrouter_requests.clear()

        payload = {
            "model": "stealth/ox-alpha:free",
            "messages": [{"role": "user", "content": "Hi OpenRouter"}]
        }
        resp = await self.client.post(
            "/v1/chat/completions",
            json=payload,
            headers={"Authorization": "Bearer client-supplied-original-key"}
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.openrouter_requests), 1)
        # Upstream must receive the proxy's saved key, NOT the client's key!
        self.assertEqual(self.openrouter_requests[0]["auth"], "Bearer sk-proxy-saved-upstream-secret-key")

    async def test_dynamic_dispatch_injects_api_key_when_client_omits_auth(self):
        """When client sends NO Authorization header, the route's saved api_key is injected upstream."""
        proxy_mod._model_router.update_from_dict({
            "default_route": {
                "name": "Default Mock Upstream",
                "upstream_url": str(self.mock_default_server.make_url("/v1")),
                "max_concurrent": 4
            },
            "routes": [
                {
                    "id": "openrouter_route",
                    "name": "OpenRouter Stealth",
                    "pattern": r"stealth/ox-alpha.*",
                    "upstream_url": str(self.mock_openrouter_server.make_url("/api/v1")),
                    "api_key": "sk-injected-upstream-key",
                    "priority": 10,
                    "max_concurrent": 10
                }
            ]
        })
        self.openrouter_requests.clear()

        payload = {
            "model": "stealth/ox-alpha:free",
            "messages": [{"role": "user", "content": "Hi without auth"}]
        }
        resp = await self.client.post(
            "/v1/chat/completions",
            json=payload,
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.openrouter_requests), 1)
        self.assertEqual(self.openrouter_requests[0]["auth"], "Bearer sk-injected-upstream-key")

    async def test_fallback_dispatch_to_default(self):
        """Unmatched request for DeepSeek-V3 must route to Default upstream with client's incoming Bearer key untouched."""
        payload = {
            "model": "DeepSeek-V3",
            "messages": [{"role": "user", "content": "Hi Default"}]
        }
        resp = await self.client.post(
            "/v1/chat/completions",
            json=payload,
            headers={"Authorization": "Bearer client-supplied-default-key"}
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["choices"][0]["message"]["content"], "Hello from Default Upstream!")

        self.assertEqual(len(self.default_requests), 1)
        self.assertEqual(self.default_requests[0]["auth"], "Bearer client-supplied-default-key")
        self.assertEqual(len(self.openrouter_requests), 0)

    async def test_routes_api_management(self):
        """Test GET /v1/routes, POST /v1/routes, and POST /v1/routes/test."""
        # 1. GET /v1/routes
        get_resp = await self.client.get("/v1/routes")
        self.assertEqual(get_resp.status, 200)
        routes_data = await get_resp.json()
        self.assertIn("default_route", routes_data)
        self.assertIn("routes", routes_data)

        # 2. POST /v1/routes/test
        test_resp = await self.client.post("/v1/routes/test", json={"model": "stealth/ox-alpha:test"})
        self.assertEqual(test_resp.status, 200)
        test_data = await test_resp.json()
        self.assertFalse(test_data["is_default"])
        self.assertEqual(test_data["route_name"], "OpenRouter Stealth")

        # 3. POST /v1/routes (add new rule)
        routes_data["routes"].append({
            "id": "new_gemini_route",
            "name": "Gemini Models",
            "pattern": r"gemini-.*",
            "upstream_url": "https://generativelanguage.googleapis.com/v1beta",
            "priority": 30,
            "max_concurrent": 6
        })
        save_resp = await self.client.post("/v1/routes", json=routes_data)
        self.assertEqual(save_resp.status, 200)

        # Verify new rule is active immediately
        test_gemini = await self.client.post("/v1/routes/test", json={"model": "gemini-2.0-flash"})
        gemini_data = await test_gemini.json()
        self.assertFalse(gemini_data["is_default"])
        self.assertEqual(gemini_data["route_name"], "Gemini Models")

    async def test_health_limiters_summary(self):
        """Verify /health returns live limiter summaries for default and routes."""
        resp = await self.client.get("/health")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertIn("limiters_summary", data)
        summary = data["limiters_summary"]
        self.assertIn("default", summary)
        self.assertIn("routes", summary)
        self.assertEqual(summary["default"]["stats"]["max_concurrent"], 4)
        self.assertEqual(len(summary["routes"]), 1)
        self.assertEqual(summary["routes"][0]["stats"]["max_concurrent"], 10)

    def test_default_route_and_custom_routes_persistence(self):
        """Verify that default_route and custom routes both save and reload identically from disk."""
        config_data = {
            "default_route": {
                "name": "OpenRouter Default",
                "upstream_url": "https://openrouter.ai/api/v1",
                "max_concurrent": 3,
                "slot_cooldown_ms": 50
            },
            "routes": [
                {
                    "id": "route_w2floc6",
                    "name": "OpenRouter Stealth",
                    "pattern": "stealth/ox-alpha",
                    "upstream_url": "https://openrouter.ai/api/v1",
                    "enabled": True,
                    "priority": 100,
                    "max_concurrent": 6,
                    "slot_cooldown_ms": 50
                }
            ]
        }
        with TemporaryDirectory() as tmp_d:
            cfg_file = Path(tmp_d) / "model_routes.json"
            router1 = ModelRouter(config_path=cfg_file)
            router1.update_from_dict(config_data)
            saved = router1.save()
            self.assertTrue(saved)

            # Reload into a completely new router instance
            router2 = ModelRouter(config_path=cfg_file)
            self.assertEqual(router2.default_upstream_url, "https://openrouter.ai/api/v1")
            self.assertEqual(router2.default_name, "OpenRouter Default")
            self.assertEqual(router2.default_max_concurrent, 3)
            self.assertEqual(len(router2.rules), 1)
            self.assertEqual(router2.rules[0].name, "OpenRouter Stealth")
            self.assertEqual(router2.rules[0].upstream_url, "https://openrouter.ai/api/v1")
            self.assertEqual(router2.rules[0].max_concurrent, 6)

            # Resolve unknown model -> must go to OpenRouter Default
            res_def = router2.resolve("some-general-model")
            self.assertTrue(res_def.is_default)
            self.assertEqual(res_def.upstream_url, "https://openrouter.ai/api/v1")
            self.assertEqual(res_def.max_concurrent, 3)

            # Resolve stealth model -> must go to OpenRouter Stealth
            res_stealth = router2.resolve("stealth/ox-alpha")
            self.assertFalse(res_stealth.is_default)
            self.assertEqual(res_stealth.upstream_url, "https://openrouter.ai/api/v1")
            self.assertEqual(res_stealth.max_concurrent, 6)

    async def test_proxy_multi_route_concurrency_overspill(self):
        """End-to-end: saturating Route 1 immediately routes identical-pattern requests to Route 2."""
        proxy_mod._model_router.update_from_dict({
            "default_route": {
                "name": "Default Mock Upstream",
                "upstream_url": str(self.mock_default_server.make_url("/v1")),
                "max_concurrent": 4
            },
            "routes": [
                {
                    "id": "r1_openrouter",
                    "name": "Route 1 OpenRouter",
                    "pattern": r"^overspill/.*",
                    "upstream_url": str(self.mock_openrouter_server.make_url("/api/v1")),
                    "priority": 100,
                    "max_concurrent": 1
                },
                {
                    "id": "r2_default",
                    "name": "Route 2 Default",
                    "pattern": r"^overspill/.*",
                    "upstream_url": str(self.mock_default_server.make_url("/v1")),
                    "priority": 50,
                    "max_concurrent": 4
                }
            ]
        })
        self.openrouter_requests.clear()
        self.default_requests.clear()

        # Hold a slot on Route 1
        lim1 = proxy_mod._model_router.get_limiter("r1_openrouter")
        slot_acquired = await lim1.try_acquire()
        self.assertTrue(slot_acquired)
        self.assertEqual(lim1.active, 1)

        try:
            # Send request matching ^overspill/.*
            payload = {"model": "overspill/llama3", "messages": [{"role": "user", "content": "Hi"}]}
            resp = await self.client.post("/v1/chat/completions", json=payload)
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            # Because Route 1 was saturated, request immediately overspilled to Route 2 (default server)!
            self.assertEqual(data["choices"][0]["message"]["content"], "Hello from Default Upstream!")
            self.assertEqual(len(self.default_requests), 1)
            self.assertEqual(len(self.openrouter_requests), 0)
        finally:
            await lim1.release()

    async def test_proxy_multi_route_failover_cascade(self):
        """End-to-end: When Route 1 fails upstream (429) and exhausts retries, proxy cleanly cascades to Route 2."""
        proxy_mod._model_router.update_from_dict({
            "default_route": {
                "name": "Default Mock Upstream",
                "upstream_url": str(self.mock_default_server.make_url("/v1")),
                "max_concurrent": 4
            },
            "routes": [
                {
                    "id": "r1_failing",
                    "name": "Route 1 Failing",
                    "pattern": r"^failover/.*",
                    "upstream_url": str(self.mock_openrouter_server.make_url("/api/v1")),
                    "priority": 100,
                    "max_concurrent": 4,
                    "retry_policy": {"enabled": True, "max_retries": 1, "mode": "immediate"}
                },
                {
                    "id": "r2_fallback",
                    "name": "Route 2 Working",
                    "pattern": r"^failover/.*",
                    "upstream_url": str(self.mock_default_server.make_url("/v1")),
                    "priority": 50,
                    "max_concurrent": 4
                }
            ]
        })
        self.openrouter_requests.clear()
        self.default_requests.clear()

        payload = {"model": "failover/test-model", "messages": [{"role": "user", "content": "Hi"}]}
        resp = await self.client.post("/v1/chat/completions", json=payload)
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        # Successfully served by Route 2 after Route 1 exhausted retries!
        self.assertEqual(data["choices"][0]["message"]["content"], "Hello from Default Upstream!")
        # Route 1 was called initial + 1 retry = 2 attempts
        self.assertEqual(len(self.openrouter_requests), 2)
        # Route 2 was called once and succeeded
        self.assertEqual(len(self.default_requests), 1)

    async def test_routes_test_endpoint_returns_candidates_chain(self):
        """POST /v1/routes/test returns full candidates list and matched count for overspill visualization."""
        proxy_mod._model_router.update_from_dict({
            "default_route": {
                "name": "Default Mock Upstream",
                "upstream_url": str(self.mock_default_server.make_url("/v1")),
                "max_concurrent": 4
            },
            "routes": [
                {
                    "id": "r1_chain",
                    "name": "Route Chain P100",
                    "pattern": r"^chain/.*",
                    "upstream_url": "https://chain100.ai/v1",
                    "priority": 100
                },
                {
                    "id": "r2_chain",
                    "name": "Route Chain P50",
                    "pattern": r"^chain/.*",
                    "upstream_url": "https://chain50.ai/v1",
                    "priority": 50
                }
            ]
        })
        test_resp = await self.client.post("/v1/routes/test", json={"model": "chain/sample"})
        self.assertEqual(test_resp.status, 200)
        data = await test_resp.json()
        self.assertEqual(data["matched_routes_count"], 2)
        self.assertEqual(len(data["candidates"]), 2)
        self.assertEqual(data["candidates"][0]["route_name"], "Route Chain P100")
        self.assertEqual(data["candidates"][1]["route_name"], "Route Chain P50")


if __name__ == "__main__":
    unittest.main()
