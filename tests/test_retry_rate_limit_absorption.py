#!/usr/bin/env python3
"""
Test Suite: Rate-Limit Absorption & Transparent Retry Engine.
Validates:
1. Immediate replay on upstream 429 status (fast-track 0s delay).
2. SSE streaming deferred handshake & error chunk interception.
3. Body-level rate-limit text pattern detection.
4. Institutional route zero-retry pass-through enforcement (max_retries = 0).
5. Downstream header injection (X-Proxy-Retries-Attempted, X-Proxy-Rate-Limit-Absorbed).
6. SQLite telemetry and metric recording (retries_attempted, absorbed_429).
"""

import asyncio
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import aiohttp
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestServer

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from proxy.model_router import ModelRouter, ModelRouteRule, normalize_retry_policy
import proxy.llm_telemetry_proxy as proxy_mod


class TestRateLimitAbsorptionE2E(AioHTTPTestCase):
    """End-to-end integration tests for transparent rate-limit absorption."""

    async def setUpAsync(self):
        self.tmp_dir = TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_telemetry.db"
        self.config_path = Path(self.tmp_dir.name) / "routes.json"

        # Mock Upstream Counters
        self.open_provider_calls = 0
        self.open_stream_calls = 0
        self.body_rate_limit_calls = 0
        self.institutional_calls = 0

        # Create Mock Upstream App
        mock_app = web.Application()
        mock_app.router.add_post("/v1/chat/completions", self._handle_mock_completions)
        mock_app.router.add_get("/v1/models", self._handle_mock_models)

        self.mock_server = TestServer(mock_app)
        await self.mock_server.start_server()

        # Configure Router
        self.test_router = ModelRouter(config_path=self.config_path)
        self.test_router.update_from_dict({
            "default_route": {
                "name": "Institutional Provider (No Retry)",
                "upstream_url": f"http://127.0.0.1:{self.mock_server.port}/v1",
                "max_concurrent": 4,
                "retry_policy": {
                    "enabled": False,
                    "max_retries": 0,
                    "mode": "immediate"
                }
            },
            "routes": [
                {
                    "id": "open_free_provider",
                    "name": "Open Free Provider (Fast Retries)",
                    "pattern": r"^open-free/.*",
                    "upstream_url": f"http://127.0.0.1:{self.mock_server.port}/v1",
                    "priority": 100,
                    "max_concurrent": 4,
                    "retry_policy": {
                        "enabled": True,
                        "max_retries": 3,
                        "mode": "immediate",
                        "retry_on_status": [429, 503, 529],
                        "retry_on_body_patterns": ["rate limit", "try again", "overloaded"],
                        "max_retry_after_seconds": 2.0
                    }
                },
                {
                    "id": "body_limit_provider",
                    "name": "Body Level Limit Provider",
                    "pattern": r"^body-limit/.*",
                    "upstream_url": f"http://127.0.0.1:{self.mock_server.port}/v1",
                    "priority": 90,
                    "max_concurrent": 4,
                    "retry_policy": {
                        "enabled": True,
                        "max_retries": 3,
                        "mode": "immediate",
                        "retry_on_status": [429, 503],
                        "retry_on_body_patterns": ["rate limit reached", "capacity full"],
                        "max_retry_after_seconds": 2.0
                    }
                }
            ]
        })

        # Wire global proxy module state
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

    async def _handle_mock_completions(self, request: web.Request) -> web.StreamResponse:
        data = await request.json()
        model = data.get("model", "")
        stream = data.get("stream", False)

        # 1. Open Free Provider (Non-streaming 429 then 200)
        if model.startswith("open-free/") and not stream:
            self.open_provider_calls += 1
            if self.open_provider_calls <= 2:
                # Return 429 on first 2 calls
                return web.json_response(
                    {"error": {"message": "Rate limit exceeded. Try again."}},
                    status=429
                )
            # Success on 3rd call
            return web.json_response({
                "id": "chatcmpl-success",
                "object": "chat.completion",
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "Success after rate-limit absorption!"}
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18}
            })

        # 2. Open Free Provider (Streaming SSE)
        if model.startswith("open-free/") and stream:
            self.open_stream_calls += 1
            if model == "open-free/empty-stream-test" and self.open_stream_calls == 1:
                # Upstream sends HTTP 200 SSE with empty role/null chunks and immediate [DONE]
                response = web.StreamResponse(
                    status=200,
                    headers={"Content-Type": "text/event-stream"}
                )
                await response.prepare(request)
                c_role = json.dumps({"choices": [{"delta": {"role": "assistant"}}]})
                c_null = json.dumps({"choices": [{"delta": {"content": None}}]})
                await response.write(f"data: {c_role}\n\n".encode("utf-8"))
                await response.write(f"data: {c_null}\n\n".encode("utf-8"))
                await response.write(b"data: [DONE]\n\n")
                await response.write_eof()
                return response
            elif self.open_stream_calls == 1 and model != "open-free/empty-stream-test":
                # Upstream sends HTTP 200 SSE, but first data chunk is an error!
                response = web.StreamResponse(
                    status=200,
                    headers={"Content-Type": "text/event-stream"}
                )
                await response.prepare(request)
                err_chunk = json.dumps({"error": {"message": "rate limit: global pool busy, try again"}})
                await response.write(f"data: {err_chunk}\n\n".encode("utf-8"))
                await response.write_eof()
                return response
            else:
                # Success stream on attempt 2
                response = web.StreamResponse(
                    status=200,
                    headers={"Content-Type": "text/event-stream"}
                )
                await response.prepare(request)
                c1 = json.dumps({"choices": [{"delta": {"content": "Streamed "}}]})
                c2 = json.dumps({"choices": [{"delta": {"content": "successfully!"}}]})
                c3 = json.dumps({"usage": {"prompt_tokens": 8, "completion_tokens": 6}})
                await response.write(f"data: {c1}\n\n".encode("utf-8"))
                await response.write(f"data: {c2}\n\n".encode("utf-8"))
                await response.write(f"data: {c3}\n\n".encode("utf-8"))
                await response.write(b"data: [DONE]\n\n")
                await response.write_eof()
                return response

        # 3. Body-level Rate Limit Provider (HTTP 200 with rate-limit body text)
        if model.startswith("body-limit/"):
            self.body_rate_limit_calls += 1
            if self.body_rate_limit_calls == 1:
                return web.json_response({
                    "error": "rate limit reached, please retry"
                }, status=200)
            return web.json_response({
                "choices": [{"message": {"content": "Body limit absorbed!"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5}
            })

        # 4. Institutional Provider (Strict No-Retry)
        if model.startswith("institutional/"):
            self.institutional_calls += 1
            return web.json_response({
                "error": {"message": "429 Too Many Requests - Institutional quota"}
            }, status=429)

        return web.json_response({"choices": [{"message": {"content": "Default ok"}}]})

    async def _handle_mock_models(self, request: web.Request) -> web.Response:
        return web.json_response({"data": [{"id": "open-free/model-1"}]})

    async def test_non_streaming_rate_limit_absorbed_and_replayed(self):
        """Proxy should silently retry 429s immediately and return 200 to client."""
        payload = {
            "model": "open-free/fast-llm",
            "messages": [{"role": "user", "content": "Hello free model!"}]
        }
        resp = await self.client.post("/v1/chat/completions", json=payload)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers.get("X-Proxy-Retries-Attempted"), "2")
        self.assertEqual(resp.headers.get("X-Proxy-Rate-Limit-Absorbed"), "1")

        data = await resp.json()
        self.assertIn("Success after rate-limit absorption!", data["choices"][0]["message"]["content"])
        self.assertEqual(self.open_provider_calls, 3)

        # Check DB log for metrics
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT retries_attempted, absorbed_429, status_code FROM api_calls WHERE model = ?",
                ("open-free/fast-llm",)
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], 2)  # 2 retries attempted
            self.assertEqual(row[1], 1)  # 1 absorbed
            self.assertEqual(row[2], 200)

    async def test_streaming_deferred_handshake_absorbs_error_chunk(self):
        """Proxy should buffer initial SSE chunks, intercept error without sending to client, and replay cleanly."""
        payload = {
            "model": "open-free/streaming-llm",
            "messages": [{"role": "user", "content": "Stream me"}],
            "stream": True
        }
        resp = await self.client.post("/v1/chat/completions", json=payload)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers.get("X-Proxy-Retries-Attempted"), "1")
        self.assertEqual(resp.headers.get("X-Proxy-Rate-Limit-Absorbed"), "1")

        content = await resp.text()
        self.assertIn("Streamed", content)
        self.assertIn("successfully!", content)
        self.assertNotIn("rate limit: global pool busy", content)
        self.assertEqual(self.open_stream_calls, 2)
        self.assertNotIn("rate limit: global pool busy", content)
        self.assertEqual(self.open_stream_calls, 2)

    async def test_body_level_rate_limit_detection_and_absorption(self):
        """Proxy should inspect JSON response body for rate-limit signatures and replay."""
        payload = {
            "model": "body-limit/test-model",
            "messages": [{"role": "user", "content": "Hello body limit"}]
        }
        resp = await self.client.post("/v1/chat/completions", json=payload)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers.get("X-Proxy-Retries-Attempted"), "1")
        self.assertEqual(resp.headers.get("X-Proxy-Rate-Limit-Absorbed"), "1")

        data = await resp.json()
        self.assertEqual(data["choices"][0]["message"]["content"], "Body limit absorbed!")
        self.assertEqual(self.body_rate_limit_calls, 2)

    async def test_upstream_empty_body_absorbed_and_replayed(self):
        """When upstream returns an empty body or empty choices, proxy immediately replays and returns valid response."""
        payload = {
            "model": "body-limit/empty-first-attempt",
            "messages": [{"role": "user", "content": "Hello empty body test"}]
        }
        resp = await self.client.post("/v1/chat/completions", json=payload)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers.get("X-Proxy-Retries-Attempted"), "1")
        self.assertEqual(resp.headers.get("X-Proxy-Rate-Limit-Absorbed"), "1")
        data = await resp.json()
        self.assertIn("Body limit absorbed!", data["choices"][0]["message"]["content"])

    async def test_upstream_empty_streaming_sse_absorbed_and_replayed(self):
        """When upstream sends an empty SSE stream (0 tokens before [DONE]), proxy intercepts before client handshake and replays."""
        payload = {
            "model": "open-free/empty-stream-test",
            "messages": [{"role": "user", "content": "Test stream"}],
            "stream": True
        }
        resp = await self.client.post("/v1/chat/completions", json=payload)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers.get("X-Proxy-Retries-Attempted"), "1")
        self.assertEqual(resp.headers.get("X-Proxy-Rate-Limit-Absorbed"), "1")
        content = await resp.text()
        self.assertIn("Streamed", content)
        self.assertIn("successfully!", content)

    async def test_institutional_provider_strictly_zero_retries(self):
        """For institutional providers with max_retries=0 / enabled=False, exactly 1 attempt must be made."""
        payload = {
            "model": "institutional/e-infra-llama",
            "messages": [{"role": "user", "content": "Strict test"}]
        }
        resp = await self.client.post("/v1/chat/completions", json=payload)
        self.assertEqual(resp.status, 429)
        self.assertEqual(resp.headers.get("X-Proxy-Retries-Attempted"), "0")
        self.assertEqual(resp.headers.get("X-Proxy-Rate-Limit-Absorbed"), "0")

        # Crucial check: exactly 1 call was made to upstream, zero retries
        self.assertEqual(self.institutional_calls, 1)

        # Check DB log
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT retries_attempted, absorbed_429, status_code FROM api_calls WHERE model = ?",
                ("institutional/e-infra-llama",)
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], 0)
            self.assertEqual(row[1], 0)
            self.assertEqual(row[2], 429)


if __name__ == "__main__":
    unittest.main()
