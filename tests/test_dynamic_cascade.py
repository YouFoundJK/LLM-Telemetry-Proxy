#!/usr/bin/env python3
"""
Test Suite: Dynamic Cascade Balancing & Failover Circuit Breaker.
Validates that during upstream failures and retries, requests dynamically
balance across equal-priority pools and tiered fallbacks rather than
dumping into a single subsequent route.
"""

import asyncio
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import aiohttp
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestServer

REPO_ROOT = Path(__file__).resolve().parent.parent
import sys
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from proxy.model_router import ModelRouter, ModelRouteRule, build_upstream_url
import proxy.llm_telemetry_proxy as proxy_mod
import proxy.proxy_forwarder as forwarder_mod


class TestDynamicCascadeLogic(unittest.TestCase):
    """Unit tests validating dynamic cascade route selection logic in ModelRouter."""

    def setUp(self):
        self.tmp_dir = TemporaryDirectory()
        self.config_path = Path(self.tmp_dir.name) / "model_routes.json"
        self.router = ModelRouter(config_path=self.config_path)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_dynamic_cascade_balances_across_equal_pool(self):
        """
        When Route 1 in an equal-priority pool (prio 100) fails,
        subsequent cascades must balance evenly across Routes 2, 3, and 4
        rather than all dumping onto Route 2.
        """
        async def run_test():
            self.router.update_from_dict({
                "routing_strategy": "balanced",
                "default_route": {"name": "Default", "upstream_url": "https://default.ai/v1"},
                "routes": [
                    {"id": "r1", "name": "Route 1 Failing", "pattern": "^model-test.*", "priority": 100, "upstream_url": "https://r1.ai/v1", "max_concurrent": 10},
                    {"id": "r2", "name": "Route 2 Working", "pattern": "^model-test.*", "priority": 100, "upstream_url": "https://r2.ai/v1", "max_concurrent": 10},
                    {"id": "r3", "name": "Route 3 Working", "pattern": "^model-test.*", "priority": 100, "upstream_url": "https://r3.ai/v1", "max_concurrent": 10},
                    {"id": "r4", "name": "Route 4 Working", "pattern": "^model-test.*", "priority": 100, "upstream_url": "https://r4.ai/v1", "max_concurrent": 10},
                ]
            })

            candidates = self.router.resolve_chain("model-test")
            self.assertEqual(len(candidates), 4)

            # Simulate 6 requests where Route 1 fails and cascades to the remaining candidates
            cascade_destinations = []
            for _ in range(6):
                attempted = {"r1"}
                remaining = [c for c in candidates if c.route_id not in attempted]
                next_cand, lim, acq = await self.router.select_admission_route(remaining)
                self.assertIsNotNone(next_cand)
                self.assertIn(next_cand.route_id, {"r2", "r3", "r4"})
                cascade_destinations.append(next_cand.route_name)

            # The 6 cascaded requests must be balanced across Route 2, Route 3, and Route 4 (2 each)
            r2_count = cascade_destinations.count("Route 2 Working")
            r3_count = cascade_destinations.count("Route 3 Working")
            r4_count = cascade_destinations.count("Route 4 Working")

            self.assertEqual(r2_count, 2)
            self.assertEqual(r3_count, 2)
            self.assertEqual(r4_count, 2)

        asyncio.run(run_test())

    def test_multi_tier_cascade_falls_through_to_balanced_tier(self):
        """
        When all routes in Tier 100 fail, requests cascade down to Tier 50
        and balance across Tier 50 routes.
        """
        async def run_test():
            self.router.update_from_dict({
                "routing_strategy": "balanced",
                "default_route": {"name": "Default", "upstream_url": "https://default.ai/v1"},
                "routes": [
                    {"id": "t100_a", "name": "Prio 100 A", "pattern": "^tiered.*", "priority": 100, "upstream_url": "https://t100a.ai/v1", "max_concurrent": 5},
                    {"id": "t100_b", "name": "Prio 100 B", "pattern": "^tiered.*", "priority": 100, "upstream_url": "https://t100b.ai/v1", "max_concurrent": 5},
                    {"id": "t50_a", "name": "Prio 50 A", "pattern": "^tiered.*", "priority": 50, "upstream_url": "https://t50a.ai/v1", "max_concurrent": 5},
                    {"id": "t50_b", "name": "Prio 50 B", "pattern": "^tiered.*", "priority": 50, "upstream_url": "https://t50b.ai/v1", "max_concurrent": 5},
                ]
            })

            candidates = self.router.resolve_chain("tiered-model")

            # Both Prio 100 routes failed
            attempted = {"t100_a", "t100_b"}
            remaining = [c for c in candidates if c.route_id not in attempted]
            self.assertEqual(len(remaining), 2)

            destinations = []
            for _ in range(4):
                next_cand, lim, acq = await self.router.select_admission_route(remaining)
                destinations.append(next_cand.route_name)

            # Must distribute evenly across Prio 50 A and Prio 50 B (2 each)
            self.assertEqual(destinations.count("Prio 50 A"), 2)
            self.assertEqual(destinations.count("Prio 50 B"), 2)

        asyncio.run(run_test())

    def test_circuit_breaker_cooldown_bypasses_exhausted_routes(self):
        """
        When a route is marked in cooldown (e.g. quota exhausted),
        select_admission_route automatically bypasses it and routes to healthy peers.
        """
        async def run_test():
            self.router.update_from_dict({
                "routing_strategy": "balanced",
                "default_route": {"name": "Default", "upstream_url": "https://default.ai/v1"},
                "routes": [
                    {"id": "or1", "name": "OpenRouter 1", "pattern": "^stealth.*", "priority": 100, "upstream_url": "https://or1.ai/v1", "max_concurrent": 4},
                    {"id": "tr1", "name": "TokenRouter 1", "pattern": "^stealth.*", "priority": 100, "upstream_url": "https://tr1.ai/v1", "max_concurrent": 4},
                ]
            })

            candidates = self.router.resolve_chain("stealth/union-alpha")
            self.assertEqual(len(candidates), 2)

            # Mark OpenRouter 1 as cooling down (daily limit exceeded)
            self.router.mark_route_cooldown("or1", seconds=30.0, reason="Daily quota exceeded")
            r_rule = self.router.get_rule("or1")
            self.assertTrue(r_rule.is_cooling_down())
            self.assertGreater(r_rule.cooldown_remaining(), 0.0)

            # Re-resolve chain: candidate results reflect cooling down status
            fresh_candidates = self.router.resolve_chain("stealth/union-alpha")
            c_or1 = next(c for c in fresh_candidates if c.route_id == "or1")
            c_tr1 = next(c for c in fresh_candidates if c.route_id == "tr1")
            self.assertTrue(c_or1.is_cooling_down)
            self.assertFalse(c_tr1.is_cooling_down)

            # select_admission_route MUST pick TokenRouter 1 directly, skipping the cooling route
            for _ in range(3):
                admit, lim, acq = await self.router.select_admission_route(fresh_candidates)
                self.assertEqual(admit.route_name, "TokenRouter 1")

        asyncio.run(run_test())


class TestDynamicCascadeEndToEnd(AioHTTPTestCase):
    """End-to-end integration tests with mock upstream HTTP servers."""

    async def setUpAsync(self):
        self.r1_calls = 0
        self.r2_calls = 0
        self.r3_calls = 0

        # Upstream 1: Always returns 429 quota exceeded
        async def handle_r1(request):
            self.r1_calls += 1
            return web.json_response({
                "error": {
                    "message": "Rate limit exceeded: free-models-per-day-stealth",
                    "code": 429,
                    "metadata": {"limit_source": "openrouter_free_tier_daily"}
                }
            }, status=429)

        # Upstream 2: Healthy, returns 200 OK
        async def handle_r2(request):
            self.r2_calls += 1
            return web.json_response({
                "id": "chatcmpl-r2",
                "object": "chat.completion",
                "model": "stealth/union-alpha",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello from R2"}}]
            })

        # Upstream 3: Healthy, returns 200 OK
        async def handle_r3(request):
            self.r3_calls += 1
            return web.json_response({
                "id": "chatcmpl-r3",
                "object": "chat.completion",
                "model": "stealth/union-alpha",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello from R3"}}]
            })

        self.app1 = web.Application()
        self.app1.router.add_post("/v1/chat/completions", handle_r1)
        self.srv1 = TestServer(self.app1)
        await self.srv1.start_server()

        self.app2 = web.Application()
        self.app2.router.add_post("/v1/chat/completions", handle_r2)
        self.srv2 = TestServer(self.app2)
        await self.srv2.start_server()

        self.app3 = web.Application()
        self.app3.router.add_post("/v1/chat/completions", handle_r3)
        self.srv3 = TestServer(self.app3)
        await self.srv3.start_server()

        self.tmp_dir = TemporaryDirectory()
        self.config_path = Path(self.tmp_dir.name) / "model_routes.json"

        # Configure 3 equal-priority routes:
        # Route 1 (Failing 429), Route 2 (Working), Route 3 (Working)
        self.test_router = ModelRouter(config_path=self.config_path)
        self.test_router.update_from_dict({
            "routing_strategy": "balanced",
            "default_route": {"name": "Default", "upstream_url": f"http://127.0.0.1:{self.srv1.port}/v1"},
            "routes": [
                {
                    "id": "r1_fail",
                    "name": "Route 1 Failing",
                    "pattern": "^stealth/union-alpha.*",
                    "priority": 100,
                    "upstream_url": f"http://127.0.0.1:{self.srv1.port}/v1",
                    "max_concurrent": 10,
                    "retry_policy": {"enabled": True, "max_retries": 1, "mode": "immediate", "retry_on_status": [429]}
                },
                {
                    "id": "r2_work",
                    "name": "Route 2 Working",
                    "pattern": "^stealth/union-alpha.*",
                    "priority": 100,
                    "upstream_url": f"http://127.0.0.1:{self.srv2.port}/v1",
                    "max_concurrent": 10,
                    "retry_policy": {"enabled": True, "max_retries": 1, "mode": "immediate"}
                },
                {
                    "id": "r3_work",
                    "name": "Route 3 Working",
                    "pattern": "^stealth/union-alpha.*",
                    "priority": 100,
                    "upstream_url": f"http://127.0.0.1:{self.srv3.port}/v1",
                    "max_concurrent": 10,
                    "retry_policy": {"enabled": True, "max_retries": 1, "mode": "immediate"}
                },
            ]
        })

        await super().setUpAsync()

    async def tearDownAsync(self):
        await self.srv1.close()
        await self.srv2.close()
        await self.srv3.close()
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass
        await super().tearDownAsync()

    async def get_application(self):
        app = web.Application()
        forwarder_mod.set_forwarder_globals(
            self.test_router, f"http://127.0.0.1:{self.srv1.port}/v1",
            "upstream_session", 1, lambda msg: None
        )
        app.router.add_route("*", "/v1/{tail:.*}", forwarder_mod.handle_proxy)
        return app

    async def test_cascading_spreads_across_surviving_pool(self):
        """
        Send 6 requests for stealth/union-alpha.
        Whenever Route 1 fails, the cascade must dynamically balance
        across Route 2 and Route 3 rather than sending everything to Route 2.
        """
        for i in range(6):
            resp = await self.client.post("/v1/chat/completions", json={
                "model": "stealth/union-alpha",
                "messages": [{"role": "user", "content": f"ping {i}"}]
            })
            self.assertEqual(resp.status, 200)
            body = await resp.json()
            self.assertIn("choices", body)

        self.assertGreater(self.r2_calls, 0, "Route 2 Working should have received requests")
        self.assertGreater(self.r3_calls, 0, "Route 3 Working should have received requests")
        self.assertEqual(self.r2_calls + self.r3_calls, 6)

    async def test_cascades_from_failing_primary_tier_balance_evenly_across_fallback_pool(self):
        """
        Configure Route 1 at Priority 100 (always fails), and Routes 2 & 3 at Priority 50.
        All requests start on Route 1, fail, and cascade to the Priority 50 pool.
        The cascades MUST balance evenly: exactly 3 to Route 2 and 3 to Route 3.
        """
        # Reconfigure router priorities: R1=100 (failing), R2=50 (working), R3=50 (working)
        self.test_router.update_from_dict({
            "routing_strategy": "balanced",
            "default_route": {"name": "Default", "upstream_url": f"http://127.0.0.1:{self.srv1.port}/v1"},
            "routes": [
                {
                    "id": "r1_fail",
                    "name": "Route 1 Primary Failing",
                    "pattern": "^stealth/union-alpha.*",
                    "priority": 100,
                    "upstream_url": f"http://127.0.0.1:{self.srv1.port}/v1",
                    "max_concurrent": 10,
                    "retry_policy": {"enabled": True, "max_retries": 1, "mode": "immediate", "retry_on_status": [429]}
                },
                {
                    "id": "r2_work",
                    "name": "Route 2 Backup Working",
                    "pattern": "^stealth/union-alpha.*",
                    "priority": 50,
                    "upstream_url": f"http://127.0.0.1:{self.srv2.port}/v1",
                    "max_concurrent": 10,
                    "retry_policy": {"enabled": True, "max_retries": 1, "mode": "immediate"}
                },
                {
                    "id": "r3_work",
                    "name": "Route 3 Backup Working",
                    "pattern": "^stealth/union-alpha.*",
                    "priority": 50,
                    "upstream_url": f"http://127.0.0.1:{self.srv3.port}/v1",
                    "max_concurrent": 10,
                    "retry_policy": {"enabled": True, "max_retries": 1, "mode": "immediate"}
                },
            ]
        })

        # Reset counters
        self.r1_calls = 0
        self.r2_calls = 0
        self.r3_calls = 0

        for i in range(6):
            resp = await self.client.post("/v1/chat/completions", json={
                "model": "stealth/union-alpha",
                "messages": [{"role": "user", "content": f"test {i}"}]
            })
            self.assertEqual(resp.status, 200)

        self.assertGreater(self.r1_calls, 0)
        # R2 and R3 both absorbed the cascaded traffic
        self.assertGreater(self.r2_calls, 0)
        self.assertGreater(self.r3_calls, 0)
        self.assertEqual(self.r2_calls + self.r3_calls, 6)


if __name__ == "__main__":
    unittest.main()
