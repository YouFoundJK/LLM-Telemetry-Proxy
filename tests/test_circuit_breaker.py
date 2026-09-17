# -*- coding: utf-8 -*-
"""
Test Suite: Adaptive Route Circuit Breaker, Cooldown, and Canary Probing

Tests:
1. Quota exhaustion (429 free-models-per-day, billing) triggers instant 30-minute trip.
2. Outage failures (502, 503, timeouts) require consecutive failure threshold (default 3) before tripping 5-minute cooldown.
3. Half-open transition after cooldown expiration admitting exactly 1 canary request.
4. Canary probe success restores circuit to CLOSED with zero consecutive failures.
5. Canary probe failure trips circuit back to OPEN with exponential backoff.
6. Safety valve: when all routes in a pool are OPEN, emergency probes the candidate closest to expiration.
7. Manual reset via router resets circuit to CLOSED.
8. Detection helper is_quota_exhaustion correctly identifies quota errors vs transient concurrency bursts.
9. End-to-end integration with mock servers: Route 1 quota exhaustion causes instant failover to Route 2,
   Route 1 enters 30m cooldown, subsequent requests immediately bypass Route 1 without calling it,
   and manual reset restores Route 1.
"""

import asyncio
import json
import time
import unittest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestServer

from proxy.model_router import (
    ModelRouter,
    ModelRouteRule,
    RouteResolutionResult,
    is_quota_exhaustion,
    DEFAULT_CIRCUIT_BREAKER,
)
from proxy.proxy_forwarder import handle_proxy, set_forwarder_globals
import proxy.telemetry_db as telemetry_db


class TestCircuitBreakerUnit(unittest.TestCase):
    """Unit tests for ModelRouteRule and ModelRouter circuit breaker mechanics."""

    def setUp(self):
        self.router = ModelRouter()
        self.rule = ModelRouteRule(
            id="test_route_1",
            name="Test Upstream 1",
            pattern=r"test/.*",
            upstream_url="http://mock-upstream.local",
            priority=100,
            circuit_breaker={
                "enabled": True,
                "consecutive_failures_threshold": 3,
                "quota_cooldown_seconds": 1800.0,
                "outage_cooldown_seconds": 300.0,
                "backoff_multiplier": 2.0,
                "max_cooldown_seconds": 7200.0,
            },
        )
        self.router.rules = [self.rule]

    def test_quota_exhaustion_instant_trip(self):
        """Hard quota exhaustion should immediately trip circuit to OPEN for 1800s."""
        self.assertEqual(self.rule.get_circuit_state(), "CLOSED")
        self.assertTrue(self.rule.is_available_for_admission())

        self.rule.mark_quota_exhaustion("Rate limit exceeded: free-models-per-day")

        self.assertEqual(self.rule.get_circuit_state(), "OPEN")
        self.assertTrue(self.rule.is_cooling_down())
        self.assertFalse(self.rule.is_available_for_admission())
        self.assertGreater(self.rule.cooldown_remaining(), 1750.0)
        self.assertIn("free-models-per-day", self.rule._cooldown_reason)

    def test_outage_threshold_and_trip(self):
        """Outage failures should only trip to OPEN after threshold (3 consecutive)."""
        self.assertEqual(self.rule.get_circuit_state(), "CLOSED")

        # 1st outage failure
        self.rule.mark_outage_failure("HTTP 502")
        self.assertEqual(self.rule._consecutive_failures, 1)
        self.assertEqual(self.rule.get_circuit_state(), "CLOSED")
        self.assertTrue(self.rule.is_available_for_admission())

        # 2nd outage failure
        self.rule.mark_outage_failure("HTTP 503")
        self.assertEqual(self.rule._consecutive_failures, 2)
        self.assertEqual(self.rule.get_circuit_state(), "CLOSED")
        self.assertTrue(self.rule.is_available_for_admission())

        # Success in between resets counter
        self.rule.mark_success()
        self.assertEqual(self.rule._consecutive_failures, 0)

        # 3 consecutive failures
        self.rule.mark_outage_failure("HTTP 504")
        self.rule.mark_outage_failure("HTTP 504")
        self.rule.mark_outage_failure("HTTP 504")

        self.assertEqual(self.rule.get_circuit_state(), "OPEN")
        self.assertTrue(self.rule.is_cooling_down())
        self.assertFalse(self.rule.is_available_for_admission())
        self.assertGreater(self.rule.cooldown_remaining(), 280.0)
        self.assertLessEqual(self.rule.cooldown_remaining(), 300.0)

    def test_half_open_transition_and_canary_probe(self):
        """Route transitions to HALF_OPEN after cooldown expires; admits exactly 1 canary probe."""
        # Force a quick 0.05s cooldown
        self.rule.mark_cooldown(seconds=0.05, reason="Quick test")
        self.assertEqual(self.rule.get_circuit_state(), "OPEN")
        self.assertFalse(self.rule.is_available_for_admission())

        # Wait for cooldown expiration
        time.sleep(0.06)

        # Now transitions to HALF_OPEN
        self.assertEqual(self.rule.get_circuit_state(), "HALF_OPEN")
        self.assertTrue(self.rule.is_available_for_admission())

        # Simulate canary in flight
        self.rule._canary_in_flight = True
        # While canary is in flight, subsequent requests should be blocked
        self.assertFalse(self.rule.is_available_for_admission())

    def test_canary_success_restores_closed(self):
        """Canary success resets circuit to CLOSED and clears failures."""
        self.rule.mark_cooldown(seconds=0.01, reason="Test")
        time.sleep(0.02)
        self.assertEqual(self.rule.get_circuit_state(), "HALF_OPEN")
        self.rule._canary_in_flight = True

        self.rule.mark_success()
        self.assertEqual(self.rule.get_circuit_state(), "CLOSED")
        self.assertFalse(self.rule.is_cooling_down())
        self.assertEqual(self.rule._consecutive_failures, 0)
        self.assertFalse(self.rule._canary_in_flight)
        self.assertTrue(self.rule.is_available_for_admission())

    def test_canary_failure_exponential_backoff(self):
        """Canary failure in HALF_OPEN trips back to OPEN with multiplied cooldown."""
        self.rule.mark_cooldown(seconds=0.01, reason="Test")
        self.rule._current_cooldown_duration = 300.0
        time.sleep(0.02)
        self.assertEqual(self.rule.get_circuit_state(), "HALF_OPEN")
        self.rule._canary_in_flight = True

        self.rule.mark_outage_failure("Canary probe HTTP 503")
        self.assertEqual(self.rule.get_circuit_state(), "OPEN")
        # Multiplied: 300.0 * 2.0 = 600.0
        self.assertAlmostEqual(self.rule._current_cooldown_duration, 600.0, places=1)
        self.assertGreater(self.rule.cooldown_remaining(), 580.0)

    def test_manual_reset(self):
        """reset_circuit / reset_route_circuit immediately restores CLOSED state."""
        self.rule.mark_quota_exhaustion("Hard limit")
        self.assertEqual(self.rule.get_circuit_state(), "OPEN")

        self.router.reset_route_circuit(self.rule.id)
        self.assertEqual(self.rule.get_circuit_state(), "CLOSED")
        self.assertFalse(self.rule.is_cooling_down())
        self.assertEqual(self.rule.cooldown_remaining(), 0.0)
        self.assertTrue(self.rule.is_available_for_admission())

    def test_is_quota_exhaustion_helper(self):
        """Verify is_quota_exhaustion detects quota limits and ignores non-quota errors."""
        # 1. OpenRouter free tier
        body_openrouter = {"error": {"message": "Rate limit exceeded: free-models-per-day-stealth", "code": 429}}
        is_q, reason = is_quota_exhaustion(status_code=429, body_text_or_json=body_openrouter)
        self.assertTrue(is_q)
        self.assertIn("free-models-per-day", reason)

        # 2. OpenAI insufficient quota
        body_openai = {"error": {"message": "You exceeded your current quota, please check your plan and billing details.", "type": "insufficient_quota"}}
        is_q, reason = is_quota_exhaustion(status_code=429, body_text_or_json=body_openai)
        self.assertTrue(is_q)

        # 3. HTTP 402 Payment Required
        is_q, reason = is_quota_exhaustion(status_code=402)
        self.assertTrue(is_q)
        self.assertIn("402", reason)

        # 4. Normal transient rate spike (concurrency burst, not quota)
        body_transient = {"error": {"message": "Too many requests, please slow down."}}
        is_q, reason = is_quota_exhaustion(status_code=429, body_text_or_json=body_transient)
        self.assertFalse(is_q)

        # 5. Upstream 502/503
        is_q, reason = is_quota_exhaustion(status_code=502, body_text_or_json="Bad Gateway")
        self.assertFalse(is_q)


class TestSafetyValveAndAdmission(unittest.IsolatedAsyncioTestCase):
    """Async tests for select_admission_route, canary selection, and safety valve."""

    async def test_safety_valve_when_all_cooling_down(self):
        """When all routes are cooling down, Safety Valve selects the route closest to expiration."""
        router = ModelRouter()
        r1 = ModelRouteRule(id="r1", name="Route 1", pattern=".*", upstream_url="http://u1", priority=100)
        r2 = ModelRouteRule(id="r2", name="Route 2", pattern=".*", upstream_url="http://u2", priority=100)
        router.rules = [r1, r2]

        # Both routes cooling down, but r2 expires much sooner
        r1.mark_cooldown(seconds=1800.0, reason="30m quota")
        r2.mark_cooldown(seconds=10.0, reason="10s cooldown")

        c1 = router._rule_to_result(r1)
        c2 = router._rule_to_result(r2)

        # Neither is available normally
        self.assertFalse(r1.is_available_for_admission())
        self.assertFalse(r2.is_available_for_admission())

        # Safety valve triggers emergency probe on r2 (closest to expiration)
        picked, limiter, acquired = await router.select_admission_route([c1, c2])
        self.assertIsNotNone(picked)
        self.assertEqual(picked.route_id, "r2")
        self.assertTrue(picked.canary_probe)
        self.assertTrue(r2._canary_in_flight)


class TestCircuitBreakerIntegration(AioHTTPTestCase):
    """End-to-end integration test with live HTTP mock upstreams verifying quota sidelining & cascading."""

    async def setUpAsync(self):
        # Mock 1: Always returns HTTP 429 quota exhaustion (free-models-per-day)
        app1 = web.Application()
        async def mock1_handler(req):
            return web.json_response({
                "error": {
                    "message": "Rate limit exceeded: free-models-per-day-stealth",
                    "type": "free_tier_quota_exhausted",
                    "code": 429
                }
            }, status=429)
        app1.router.add_post("/api/v1/chat/completions", mock1_handler)
        self.server1 = TestServer(app1)
        await self.server1.start_server()

        # Mock 2: Always returns HTTP 200 OK
        app2 = web.Application()
        async def mock2_handler(req):
            return web.json_response({
                "id": "chatcmpl-mock2",
                "object": "chat.completion",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello from Route 2 Working!"}
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 15, "total_tokens": 25}
            })
        app2.router.add_post("/v1/chat/completions", mock2_handler)
        self.server2 = TestServer(app2)
        await self.server2.start_server()

        # Set up ModelRouter: Route 1 at Priority 100 (admitted first), Route 2 at Priority 50
        self.router = ModelRouter()

        self.r1 = ModelRouteRule(
            id="r1_quota_exhausted",
            name="OpenRouter Stealth Quota Exhausted",
            pattern=r"stealth/.*",
            upstream_url=f"http://{self.server1.host}:{self.server1.port}/api/v1",
            priority=100,
            strategy="priority",
            max_concurrent=4,
            retry_policy={"enabled": True, "max_retries": 1},
            circuit_breaker={"enabled": True, "quota_cooldown_seconds": 1800.0},
        )
        self.r2 = ModelRouteRule(
            id="r2_healthy",
            name="TokenRouter Healthy",
            pattern=r"stealth/.*",
            upstream_url=f"http://{self.server2.host}:{self.server2.port}/v1",
            priority=50,
            strategy="priority",
            max_concurrent=4,
            retry_policy={"enabled": True, "max_retries": 1},
            circuit_breaker={"enabled": True, "quota_cooldown_seconds": 1800.0},
        )
        self.router.rules = [self.r1, self.r2]

        set_forwarder_globals(
            router=self.router,
            upstream=f"http://{self.server2.host}:{self.server2.port}/v1",
            session_key="test_session",
            retry_max=1,
            tlog=lambda msg: None,
        )

        await super().setUpAsync()

    async def tearDownAsync(self):
        await self.server1.close()
        await self.server2.close()
        await super().tearDownAsync()

    async def get_application(self):
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", handle_proxy)
        return app

    async def test_end_to_end_quota_trip_and_subsequent_bypass(self):
        """
        1. First request initially targets Route 1 (Priority 100).
        2. Route 1 returns 429 quota exhaustion.
        3. Circuit breaker immediately trips Route 1 to OPEN (30m cooldown) without looping retries.
        4. Forwarder cascades to Route 2 (Priority 50) and returns 200 OK.
        5. Route 1 is confirmed in OPEN state.
        6. Subsequent request for the same model immediately bypasses Route 1 and routes directly to Route 2.
        7. Manual reset restores Route 1 to CLOSED state.
        """
        # Request 1: Initial call
        resp1 = await self.client.post(
            "/v1/chat/completions",
            json={"model": "stealth/union-alpha", "messages": [{"role": "user", "content": "Hi"}]},
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp1.status, 200)
        body1 = await resp1.json()
        self.assertIn("Hello from Route 2 Working!", body1["choices"][0]["message"]["content"])

        # Check Route 1 circuit state
        self.assertEqual(self.r1.get_circuit_state(), "OPEN")
        self.assertTrue(self.r1.is_cooling_down())
        self.assertGreater(self.r1.cooldown_remaining(), 1700.0)

        # Route 2 must be healthy CLOSED
        self.assertEqual(self.r2.get_circuit_state(), "CLOSED")

        # Request 2: Send second request for same model
        # Because Route 1 is OPEN, select_admission_route must immediately admit Route 2 without touching Route 1
        resp2 = await self.client.post(
            "/v1/chat/completions",
            json={"model": "stealth/union-alpha", "messages": [{"role": "user", "content": "Second request"}]},
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp2.status, 200)
        body2 = await resp2.json()
        self.assertIn("Hello from Route 2 Working!", body2["choices"][0]["message"]["content"])

        # Manual reset
        self.router.reset_route_circuit(self.r1.id)
        self.assertEqual(self.r1.get_circuit_state(), "CLOSED")
        self.assertFalse(self.r1.is_cooling_down())


if __name__ == "__main__":
    unittest.main()
