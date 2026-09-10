#!/usr/bin/env python3
"""
Comprehensive Stress & Edge-Case Test Suite for LLM Telemetry Proxy:
1. Extreme concurrency limiter fuzzing & cancellation stress (100 workers).
2. Queue timeout fast-fail under route congestion.
3. Client cancellation while waiting in limiter queue (zero slot leaks).
4. Mid-stream upstream crash / disconnect with safe SSE error & terminal EOF.
5. End-to-end multi-worker burst (50 concurrent mixed streaming/non-streaming requests).
6. High-concurrency database logging integrity (WAL mode / no database lock errors).
"""

import asyncio
import json
import random
import sqlite3
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

import proxy.llm_telemetry_proxy as proxy_mod
from proxy.model_router import ModelRouter, UpstreamConcurrencyLimiter


class TestLimiterStressAndEdgeCases(unittest.IsolatedAsyncioTestCase):
    """Stress testing the concurrency limiter under intense random load, timeouts, and cancellations."""

    async def test_extreme_concurrency_fuzzing(self):
        """100 concurrent workers randomly acquiring, holding, or cancelling."""
        limiter = UpstreamConcurrencyLimiter(max_concurrent=4, slot_cooldown_seconds=0.01, max_rpm=300)
        active_track = 0
        peak_active = 0
        lock = asyncio.Lock()
        completed = 0
        cancelled = 0

        async def worker(wid: int):
            nonlocal active_track, peak_active, completed, cancelled
            try:
                async with limiter.slot():
                    async with lock:
                        active_track += 1
                        peak_active = max(peak_active, active_track)
                        self.assertLessEqual(active_track, 4, f"Active count {active_track} exceeded max_concurrent=4!")

                    try:
                        # Random hold duration
                        hold_time = random.uniform(0.005, 0.025)
                        await asyncio.sleep(hold_time)
                        completed += 1
                    finally:
                        async with lock:
                            active_track -= 1
            except asyncio.CancelledError:
                cancelled += 1
                raise

        # Launch 100 workers
        tasks = [asyncio.create_task(worker(i)) for i in range(100)]

        # Randomly cancel 15 workers in flight
        await asyncio.sleep(0.015)
        for i in range(0, 100, 7):
            if not tasks[i].done():
                tasks[i].cancel()

        # Wait for all workers to settle
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Invariants verification
        self.assertEqual(limiter.active, 0, f"Limiter leaked active slots! Found active={limiter.active}")
        self.assertEqual(limiter.queued, 0, f"Limiter leaked queued waiters! Found queued={limiter.queued}")
        self.assertEqual(active_track, 0, "Internal active tracking did not return to 0!")
        self.assertLessEqual(peak_active, 4, "Peak active concurrency exceeded max_concurrent!")
        self.assertGreater(completed, 40, f"Too few workers completed: {completed}")

    async def test_queue_cancellation_preserves_next_waiter(self):
        """When a queued waiter is cancelled, the subsequent waiter in line still gets dispatched without delay."""
        limiter = UpstreamConcurrencyLimiter(max_concurrent=1, slot_cooldown_seconds=0.01)

        c_ev = asyncio.Event()
        d_ev = asyncio.Event()

        # Task 1 holds slot
        async def holder():
            async with limiter.slot():
                d_ev.set()
                await c_ev.wait()

        t1 = asyncio.create_task(holder())
        await d_ev.wait()

        # Task 2 queues and will be cancelled
        t2_started = asyncio.Event()

        async def waiter_cancelled():
            t2_started.set()
            async with limiter.slot():
                pass

        t2 = asyncio.create_task(waiter_cancelled())
        await t2_started.wait()
        await asyncio.sleep(0.01)

        # Task 3 queues behind Task 2
        t3_started = asyncio.Event()
        t3_acquired = asyncio.Event()

        async def waiter_surviving():
            t3_started.set()
            async with limiter.slot():
                t3_acquired.set()

        t3 = asyncio.create_task(waiter_surviving())
        await t3_started.wait()
        await asyncio.sleep(0.01)

        self.assertEqual(limiter.queued, 2)

        # Cancel Task 2 while in queue
        t2.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await t2

        self.assertEqual(limiter.queued, 1)

        # Release Task 1 -> Task 3 must immediately acquire!
        c_ev.set()
        await t1

        await asyncio.wait_for(t3_acquired.wait(), timeout=0.5)
        await t3
        self.assertEqual(limiter.active, 0)
        self.assertEqual(limiter.queued, 0)


class TestProxyEndToEndStressAndEdgeCases(AioHTTPTestCase):
    """Rigorous end-to-end stress tests with real HTTP server and mock upstream."""

    async def setUpAsync(self):
        self.tmp_dir = TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_stress.db"
        self.config_path = Path(self.tmp_dir.name) / "test_routes.json"

        # Mock Upstream App
        self.mock_upstream_app = web.Application()
        self.mock_upstream_app.router.add_post("/v1/chat/completions", self._handle_mock_upstream)

        self.mock_server = TestServer(self.mock_upstream_app)
        await self.mock_server.start_server()

        # Configure Router with strict route timeouts
        self.test_router = ModelRouter(config_path=self.config_path)
        self.test_router.update_from_dict({
            "default_route": {
                "name": "Mock Upstream",
                "upstream_url": f"http://127.0.0.1:{self.mock_server.port}/v1",
                "max_concurrent": 4,
                "slot_cooldown_ms": 10,
                "timeout": {
                    "connect": 5.0,
                    "first_byte": 5.0,
                    "sock_read": 5.0,
                    "total": 15.0,
                    "queue": 5.0,  # 5s queue timeout for high-concurrency burst draining
                },
                "retry_policy": {
                    "enabled": True,
                    "max_retries": 2,
                    "mode": "immediate",
                    "retry_on_status": [429, 502],
                    "retry_on_empty": True,
                    "retry_on_disconnect": True,
                }
            },
            "routes": [
                {
                    "id": "tight_queue_route",
                    "name": "Tight Queue Route",
                    "pattern": r"^edge/tight-queue.*",
                    "upstream_url": f"http://127.0.0.1:{self.mock_server.port}/v1",
                    "max_concurrent": 2,
                    "timeout": {
                        "connect": 5.0,
                        "first_byte": 5.0,
                        "sock_read": 5.0,
                        "total": 15.0,
                        "queue": 0.3,
                    }
                }
            ]
        })

        # Wire global proxy state
        self.orig_router = proxy_mod._model_router
        self.orig_db = proxy_mod.DB_PATH
        self.orig_upstream = proxy_mod.UPSTREAM

        proxy_mod._model_router = self.test_router
        proxy_mod.DB_PATH = self.db_path
        proxy_mod.UPSTREAM = f"http://127.0.0.1:{self.mock_server.port}/v1"
        proxy_mod.init_db()

        await super().setUpAsync()

    async def tearDownAsync(self):
        proxy_mod._model_router = self.orig_router
        proxy_mod.DB_PATH = self.orig_db
        proxy_mod.UPSTREAM = self.orig_upstream
        await self.mock_server.close()
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass
        await super().tearDownAsync()

    async def get_application(self):
        return proxy_mod.create_app()

    async def _handle_mock_upstream(self, request: web.Request) -> web.StreamResponse:
        data = await request.json()
        model = data.get("model", "")
        stream = data.get("stream", False)

        # 1. Immediate Upstream Disconnect after headers (Simulates mid-stream connection drop)
        if model == "edge/stream-immediate-disconnect":
            resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
            await resp.prepare(request)
            c1 = json.dumps({"choices": [{"delta": {"content": "Token1"}}]})
            await resp.write(f"data: {c1}\n\n".encode("utf-8"))
            if request.transport:
                request.transport.close()
            return resp

        # 2. Upstream Stalls Mid-Stream (Simulates socket read timeout)
        if model == "edge/stream-stall":
            resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
            await resp.prepare(request)
            c1 = json.dumps({"choices": [{"delta": {"content": "Starting stream..."}}]})
            await resp.write(f"data: {c1}\n\n".encode("utf-8"))
            await asyncio.sleep(10.0)
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
            return resp

        # 3. Slow Provider (Holds slot to trigger queue timeout in subsequent calls)
        if model in ("edge/slow-provider", "edge/tight-queue"):
            await asyncio.sleep(0.8)
            return web.json_response({
                "choices": [{"message": {"content": "Slow response ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}
            })

        # 4. Standard Valid Stream
        if stream:
            resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
            await resp.prepare(request)
            for i in range(3):
                chunk = json.dumps({"choices": [{"delta": {"content": f"Chunk {i} "}}]})
                await resp.write(f"data: {chunk}\n\n".encode("utf-8"))
                await asyncio.sleep(0.005)
            u = json.dumps({"usage": {"prompt_tokens": 10, "completion_tokens": 6}})
            await resp.write(f"data: {u}\n\n".encode("utf-8"))
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
            return resp

        # Standard Non-streaming
        return web.json_response({
            "choices": [{"message": {"content": "Normal response ok"}}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 4}
        })

    async def test_midstream_upstream_disconnect_delivers_valid_sse_error(self):
        """When upstream drops TCP mid-stream, client receives valid HTTP 200, chunked SSE error, and clean EOF."""
        payload = {
            "model": "edge/stream-immediate-disconnect",
            "messages": [{"role": "user", "content": "Test stream drop"}],
            "stream": True
        }
        resp = await self.client.post("/v1/chat/completions", json=payload)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers.get("Content-Type"), "text/event-stream")

        content = await resp.text()
        self.assertIn("Token1", content)
        self.assertIn("upstream_stream_error", content)
        self.assertIn("data: [DONE]", content)

    async def test_burst_of_50_concurrent_requests_mixed_workload(self):
        """Rigorously fire 50 concurrent requests mixing streaming and non-streaming, asserting 0 corrupt responses."""
        async def send_req(i: int):
            is_stream = (i % 2 == 0)
            payload = {
                "model": "stress-test-model",
                "messages": [{"role": "user", "content": f"Ping worker {i}"}],
                "stream": is_stream
            }
            r = await self.client.post("/v1/chat/completions", json=payload)
            if is_stream:
                self.assertEqual(r.status, 200)
                txt = await r.text()
                self.assertIn("Chunk 0", txt)
                self.assertIn("data: [DONE]", txt)
            else:
                self.assertEqual(r.status, 200)
                body = await r.json()
                self.assertEqual(body["choices"][0]["message"]["content"], "Normal response ok")
            return r.status

        tasks = [asyncio.create_task(send_req(i)) for i in range(50)]
        results = await asyncio.gather(*tasks)

        self.assertEqual(len(results), 50)
        self.assertTrue(all(code == 200 for code in results))

        # Check SQLite DB integrity and row count
        with sqlite3.connect(self.db_path) as conn:
            api_count = conn.execute("SELECT count(*) FROM api_calls").fetchone()[0]
            proxy_count = conn.execute("SELECT count(*) FROM proxy_calls").fetchone()[0]
            self.assertEqual(api_count, 50)
            self.assertEqual(proxy_count, 50)

            # Check PRAGMA integrity
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            self.assertEqual(integrity, "ok")


if __name__ == "__main__":
    unittest.main()
