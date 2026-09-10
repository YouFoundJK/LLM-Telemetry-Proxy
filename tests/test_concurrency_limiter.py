#!/usr/bin/env python3
"""
Test Suite: Upstream Concurrency Limiter, Slot Cooldown Enforcement,
Cancellation Safety, and 429 Automatic Retry with Exponential Backoff.
"""

import sys
import os
import json
import time
import asyncio
import unittest
from pathlib import Path
from collections import deque
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import proxy.llm_telemetry_proxy as proxy_mod
from proxy.llm_telemetry_proxy import (
    UpstreamConcurrencyLimiter,
    create_app as create_proxy_app,
)


class TestUpstreamConcurrencyLimiterUnit(unittest.IsolatedAsyncioTestCase):
    """Unit tests for the UpstreamConcurrencyLimiter class."""

    async def test_strict_concurrency_cap(self):
        """Verify that active concurrency never exceeds max_concurrent under heavy load."""
        limiter = UpstreamConcurrencyLimiter(max_concurrent=3, slot_cooldown_seconds=0.01)
        current_active = 0
        peak_observed = 0
        completed = 0
        lock = asyncio.Lock()

        async def worker(worker_id: int):
            nonlocal current_active, peak_observed, completed
            async with limiter.slot():
                async with lock:
                    current_active += 1
                    peak_observed = max(peak_observed, current_active)
                    self.assertLessEqual(current_active, 3, f"Worker {worker_id} exceeded concurrency cap of 3!")

                await asyncio.sleep(0.03)  # simulate work

                async with lock:
                    current_active -= 1
                    completed += 1

        tasks = [asyncio.create_task(worker(i)) for i in range(15)]
        await asyncio.gather(*tasks)

        self.assertEqual(completed, 15)
        self.assertEqual(current_active, 0)
        self.assertEqual(peak_observed, 3)
        self.assertEqual(limiter.active, 0)
        self.assertEqual(limiter.queued, 0)
        self.assertEqual(limiter.get_stats()["total_admitted"], 15)

    async def test_zero_delay_when_not_maxed(self):
        """Verify that when active < max_concurrent and queue is empty, requests are admitted instantly with 0ms delay."""
        limiter = UpstreamConcurrencyLimiter(max_concurrent=4, slot_cooldown_seconds=0.1)  # 100ms cooldown if maxed

        t0 = time.monotonic()
        async with limiter.slot():
            pass
        t1 = time.monotonic()

        duration_ms = (t1 - t0) * 1000
        self.assertLess(duration_ms, 30, f"Unsaturated acquire took {duration_ms:.2f}ms, expected ~0ms")

    async def test_slot_cooldown_enforced_when_maxed(self):
        """Verify that when queue is maxed out, the slot cooldown gap (50ms) is strictly enforced between slot release and next dispatch."""
        limiter = UpstreamConcurrencyLimiter(max_concurrent=1, slot_cooldown_seconds=0.06)  # 60ms cooldown
        slot1_released_at = 0.0
        slot2_acquired_at = 0.0

        async def job1():
            nonlocal slot1_released_at
            async with limiter.slot():
                await asyncio.sleep(0.02)
            slot1_released_at = time.monotonic()

        async def job2():
            nonlocal slot2_acquired_at
            # Starts while job1 is in-flight, will be queued
            async with limiter.slot():
                slot2_acquired_at = time.monotonic()

        t_job1 = asyncio.create_task(job1())
        await asyncio.sleep(0.005)  # Ensure job1 acquires first
        t_job2 = asyncio.create_task(job2())

        await asyncio.gather(t_job1, t_job2)

        gap_ms = (slot2_acquired_at - slot1_released_at) * 1000
        # The cooldown is 60ms; allow Windows timer scheduler tolerance (e.g. >= 40ms)
        self.assertGreaterEqual(gap_ms, 40.0, f"Cooldown gap was {gap_ms:.2f}ms, expected >= 40ms")

    async def test_cancellation_safety(self):
        """Verify that cancelling waiting or active requests does not leak slots or stall the limiter."""
        limiter = UpstreamConcurrencyLimiter(max_concurrent=2, slot_cooldown_seconds=0.01)

        # Fill capacity
        async def slow_holder(cancel_event, done_event):
            async with limiter.slot():
                done_event.set()
                await cancel_event.wait()

        c_ev = asyncio.Event()
        d_ev1 = asyncio.Event()
        d_ev2 = asyncio.Event()

        t1 = asyncio.create_task(slow_holder(c_ev, d_ev1))
        t2 = asyncio.create_task(slow_holder(c_ev, d_ev2))
        await d_ev1.wait()
        await d_ev2.wait()

        self.assertEqual(limiter.active, 2)

        # Queue a third request and cancel it while queued
        async def waiter_to_cancel():
            async with limiter.slot():
                pass

        t_waiter = asyncio.create_task(waiter_to_cancel())
        await asyncio.sleep(0.01)
        self.assertEqual(limiter.queued, 1)

        t_waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await t_waiter

        self.assertEqual(limiter.queued, 0)

        # Release holding tasks
        c_ev.set()
        await asyncio.gather(t1, t2)

        # Now test that a new task can acquire normally
        acquired = False
        async with limiter.slot():
            acquired = True

        self.assertTrue(acquired)
        self.assertEqual(limiter.active, 0)
        self.assertEqual(limiter.queued, 0)

    async def test_max_rpm_disabled_by_default(self):
        """Verify max_rpm is -1 by default and does not throttle calls."""
        limiter = UpstreamConcurrencyLimiter(max_concurrent=10)
        self.assertEqual(limiter.max_rpm, -1)

        t_start = time.monotonic()
        for _ in range(15):
            async with limiter.slot():
                pass
        t_elapsed = time.monotonic() - t_start
        self.assertLess(t_elapsed, 0.5)

    async def test_max_rpm_rate_limiting(self):
        """Verify max_rpm enforces rolling window pacing."""
        # Setup limiter with max_rpm = 3, max_concurrent = 5
        limiter = UpstreamConcurrencyLimiter(max_concurrent=5, max_rpm=3)
        self.assertEqual(limiter.max_rpm, 3)

        # First 3 should acquire immediately
        t_start = time.monotonic()
        for _ in range(3):
            async with limiter.slot():
                pass
        t_first_three = time.monotonic() - t_start
        self.assertLess(t_first_three, 0.2)
        self.assertEqual(len(limiter._rpm_history), 3)

        # Manually shift the oldest timestamp in _rpm_history to simulate 59.9s ago (expiring in 0.1s)
        now = time.monotonic()
        limiter._rpm_history[0] = now - 59.9

        # The 4th slot acquisition should wait for the 1st call to slide out of the 60s window (~0.1s wait)
        t_wait_start = time.monotonic()
        async with limiter.slot():
            pass
        t_waited = time.monotonic() - t_wait_start
        self.assertGreaterEqual(t_waited, 0.08)
        self.assertEqual(len(limiter._rpm_history), 3)



class TestProxyRateLimitingIntegration(AioHTTPTestCase):
    """Integration test suite: Proxy against mock upstream with 429 auto-retry and concurrency limiting."""

    async def setUpAsync(self):
        self.upstream_calls = 0
        self.upstream_429_count = 0
        self.mock_upstream_app = web.Application()

        async def mock_chat(req):
            self.upstream_calls += 1
            body = await req.json()

            # Return 429 for the first 2 calls if model is 'test-retry-429'
            if body.get("model") == "test-retry-429" and self.upstream_429_count < 2:
                self.upstream_429_count += 1
                return web.json_response(
                    {"error": {"message": "Rate limit exceeded: max concurrent requests is 4", "type": "rate_limit_exceeded"}},
                    status=429,
                    headers={"Retry-After": "0.1"}
                )

            return web.json_response({
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello world from upstream!"},
                    "finish_reason": "stop"
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
            })

        async def mock_models(req):
            return web.json_response({"data": [{"id": "deepseek-v4"}, {"id": "glm-5.2"}]})

        self.mock_upstream_app.router.add_post("/v1/chat/completions", mock_chat)
        self.mock_upstream_app.router.add_get("/v1/models", mock_models)

        from aiohttp.test_utils import TestServer
        self.mock_server = TestServer(self.mock_upstream_app)
        await self.mock_server.start_server()

        # Point proxy to mock upstream
        self.orig_upstream = proxy_mod.UPSTREAM
        proxy_mod.UPSTREAM = str(self.mock_server.make_url("/v1"))
        proxy_mod.RETRY_429_MAX = 3
        proxy_mod.SLOT_COOLDOWN_MS = 20
        proxy_mod._concurrency_limiter.max_concurrent = 2
        proxy_mod._concurrency_limiter.slot_cooldown_seconds = 0.02
        await super().setUpAsync()

    async def tearDownAsync(self):
        proxy_mod.UPSTREAM = self.orig_upstream
        await self.mock_server.close()
        await super().tearDownAsync()

    async def get_application(self):
        return create_proxy_app()

    async def test_429_immediate_passthrough_when_disabled(self):
        """Verify that with default RETRY_429_MAX = 0, upstream 429 is passed immediately to client."""
        proxy_mod.RETRY_429_MAX = 0
        payload = {
            "model": "test-retry-429",
            "messages": [{"role": "user", "content": "Ping"}]
        }
        resp = await self.client.request("POST", "/v1/chat/completions", json=payload)
        self.assertEqual(resp.status, 429)
        data = await resp.json()
        self.assertIn("error", data)
        self.assertEqual(self.upstream_calls, 1)

    async def test_429_auto_retry_success(self):
        """Verify that when opted-in (RETRY_429_MAX > 0), upstream 429 responses are retried and succeed."""
        proxy_mod.RETRY_429_MAX = 3
        payload = {
            "model": "test-retry-429",
            "messages": [{"role": "user", "content": "Ping"}]
        }
        resp = await self.client.request("POST", "/v1/chat/completions", json=payload)
        self.assertEqual(resp.status, 200, f"Expected 200 OK after retry, got {resp.status}")

        data = await resp.json()
        self.assertIn("choices", data)
        self.assertEqual(data["choices"][0]["message"]["content"], "Hello world from upstream!")
        self.assertEqual(self.upstream_429_count, 2, "Expected 2 upstream 429s encountered")
        self.assertEqual(self.upstream_calls, 3, "Expected 3 total upstream requests (2 retries + 1 success)")
        self.assertGreaterEqual(proxy_mod._concurrency_limiter.get_stats()["total_retries_429"], 2)

    async def test_health_concurrency_metrics(self):
        """Verify that /health reports all rich concurrency statistics."""
        resp = await self.client.request("GET", "/health")
        self.assertEqual(resp.status, 200)
        data = await resp.json()

        self.assertIn("rate_limiter", data)
        rl = data["rate_limiter"]
        self.assertIn("max_concurrent", rl)
        self.assertIn("active", rl)
        self.assertIn("queued", rl)
        self.assertIn("slot_cooldown_ms", rl)
        self.assertIn("total_admitted", rl)
        self.assertIn("total_queued", rl)
        self.assertIn("total_retries_429", rl)
        self.assertIn("peak_active", rl)
        self.assertEqual(rl["max_concurrent"], 2)

    async def test_concurrent_burst_through_proxy(self):
        """Send a burst of 6 concurrent requests through proxy and verify all succeed without 429s."""
        payload = {
            "model": "deepseek-v4",
            "messages": [{"role": "user", "content": "Concurrent test"}]
        }

        async def send_one():
            r = await self.client.request("POST", "/v1/chat/completions", json=payload)
            self.assertEqual(r.status, 200)
            return await r.json()

        tasks = [send_one() for _ in range(6)]
        results = await asyncio.gather(*tasks)

        self.assertEqual(len(results), 6)
        for res in results:
            self.assertEqual(res["choices"][0]["message"]["content"], "Hello world from upstream!")


if __name__ == "__main__":
    unittest.main()
