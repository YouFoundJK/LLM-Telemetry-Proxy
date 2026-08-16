#!/usr/bin/env python3
"""
Test Suite: Real-Time Raw Payload Logging, Streaming, Inspector Integration, and Fast Tail Reader.
"""

import sys
import os
import json
import time
import asyncio
import tempfile
import unittest
from pathlib import Path
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

# Add project root to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import proxy.llm_telemetry_proxy as proxy_mod
from proxy.llm_telemetry_proxy import (
    create_app as create_proxy_app,
    make_raw_payload_start_record,
    make_raw_payload_record,
    read_recent_jsonl_lines,
    append_raw_payload,
    broadcast_raw_payload,
    format_size,
)
from dashboard.server import (
    create_app as create_dashboard_app,
    read_recent_jsonl_lines as server_read_recent,
)


class TestRawPayloadHelpers(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.log_file = Path(self.temp_dir.name) / "test_payloads.jsonl"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_start_record_schema_and_masking(self):
        """Verify start record has in_progress status and sensitive headers are masked."""
        record = make_raw_payload_start_record(
            req_id="req_test_123",
            path="/v1/chat/completions",
            method="POST",
            call_type="chat",
            model="DeepSeek-V3",
            client_ip="127.0.0.1",
            req_headers={
                "Authorization": "Bearer sk-1234567890abcdef12345678",
                "Content-Type": "application/json",
                "User-Agent": "curl/7.68.0",
            },
            payload_obj={
                "model": "DeepSeek-V3",
                "messages": [{"role": "user", "content": "Hello world!"}],
                "stream": True,
            },
            is_stream=True,
        )

        self.assertEqual(record["id"], "req_test_123")
        self.assertEqual(record["status"], "in_progress")
        self.assertEqual(record["endpoint"], "/v1/chat/completions")
        self.assertEqual(record["model"], "DeepSeek-V3")
        self.assertEqual(record["request"]["messages"][0]["content"], "Hello world!")
        # Sensitive header masking check
        auth_header = record["request"]["headers"]["Authorization"]
        self.assertTrue(auth_header.startswith("Bearer sk-1..."))
        self.assertNotIn("sk-1234567890abcdef12345678", auth_header)

    def test_completed_record_schema(self):
        """Verify completed record has completed status, metrics, and reasoning tokens."""
        record = make_raw_payload_record(
            req_id="req_test_123",
            path="/v1/chat/completions",
            method="POST",
            call_type="chat",
            model="DeepSeek-V3",
            client_ip="127.0.0.1",
            req_headers={"Content-Type": "application/json"},
            payload_obj={"messages": [{"role": "user", "content": "Explain quantum physics"}]},
            status_code=200,
            resp_headers={"content-type": "application/json"},
            is_stream=True,
            ttfb_ms=150.5,
            total_ms=1200.0,
            tokens_per_s=45.2,
            input_tokens=15,
            output_tokens=54,
            reasoning_tokens=20,
            content_text="Quantum physics is the study of matter and energy...",
            reasoning_text="First analyze quantum state...",
            tool_calls=None,
            raw_resp_json=None,
            error=None,
        )

        self.assertEqual(record["id"], "req_test_123")
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["response"]["status_code"], 200)
        self.assertEqual(record["response"]["usage"]["prompt_tokens"], 15)
        self.assertEqual(record["response"]["usage"]["completion_tokens"], 54)
        self.assertEqual(record["response"]["usage"]["reasoning_tokens"], 20)
        self.assertEqual(record["response"]["usage"]["total_tokens"], 69)
        self.assertEqual(record["response"]["content"]["reasoning_content"], "First analyze quantum state...")

    def test_sequence_numbering_in_records(self):
        """Verify seq field is properly assigned in start and completed records."""
        start_rec = make_raw_payload_start_record(
            req_id="req_seq_1",
            path="/v1/chat/completions",
            method="POST",
            call_type="chat",
            model="DeepSeek-V3",
            client_ip="127.0.0.1",
            req_headers={},
            payload_obj={"messages": []},
            is_stream=True,
            seq=42,
        )
        self.assertEqual(start_rec["seq"], 42)

        comp_rec = make_raw_payload_record(
            req_id="req_seq_1",
            path="/v1/chat/completions",
            method="POST",
            call_type="chat",
            model="DeepSeek-V3",
            client_ip="127.0.0.1",
            req_headers={},
            payload_obj={},
            status_code=200,
            resp_headers={},
            is_stream=True,
            ttfb_ms=10.0,
            total_ms=50.0,
            tokens_per_s=20.0,
            input_tokens=10,
            output_tokens=10,
            reasoning_tokens=0,
            content_text="test",
            reasoning_text="",
            tool_calls=None,
            raw_resp_json=None,
            error=None,
            seq=42,
        )
        self.assertEqual(comp_rec["seq"], 42)

    def test_fast_tail_reader_small_file(self):
        """Test read_recent_jsonl_lines on small files."""
        # Create 10 lines
        with open(self.log_file, "w", encoding="utf-8") as f:
            for i in range(10):
                f.write(json.dumps({"id": f"id_{i}", "num": i}) + "\n")

        entries, total = read_recent_jsonl_lines(self.log_file, limit=5)
        self.assertEqual(len(entries), 5)
        self.assertEqual(total, 10)
        # Newest first
        self.assertEqual(entries[0]["id"], "id_9")
        self.assertEqual(entries[4]["id"], "id_5")

    def test_fast_tail_reader_large_file(self):
        """Test read_recent_jsonl_lines on large files (>512KB) using backward seek."""
        # Generate 1000 large entries
        with open(self.log_file, "w", encoding="utf-8") as f:
            for i in range(1000):
                large_prompt = f"Prompt number {i} " + ("x" * 600)
                large_resp = f"Response number {i} " + ("y" * 600)
                f.write(json.dumps({"id": f"large_id_{i}", "prompt": large_prompt, "response": large_resp}) + "\n")

        self.assertGreater(self.log_file.stat().st_size, 512 * 1024)

        t0 = time.time()
        entries, total = read_recent_jsonl_lines(self.log_file, limit=20)
        duration = time.time() - t0

        self.assertEqual(len(entries), 20)
        self.assertEqual(entries[0]["id"], "large_id_999")
        self.assertEqual(entries[19]["id"], "large_id_980")
        self.assertLess(duration, 0.05, f"Large tail read took too long: {duration:.4f}s")


class TestProxyRawLogEndpoints(AioHTTPTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.orig_file = proxy_mod.LOGGER_FILE
        self.test_log_file = Path(self.temp_dir.name) / "payloads.jsonl"
        proxy_mod.LOGGER_FILE = self.test_log_file
        proxy_mod.LOGGER_DIR = Path(self.temp_dir.name)
        proxy_mod._raw_logging_enabled = False
        super().setUp()

    def tearDown(self):
        proxy_mod.LOGGER_FILE = self.orig_file
        self.temp_dir.cleanup()
        super().tearDown()

    async def get_application(self):
        return create_proxy_app()

    @unittest_run_loop
    async def test_status_endpoint(self):
        """GET /v1/raw-log/status returns current disabled state."""
        resp = await self.client.request("GET", "/v1/raw-log/status")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertFalse(data["enabled"])
        self.assertEqual(data["file_size_bytes"], 0)

    @unittest_run_loop
    async def test_toggle_endpoint_and_state_persistence(self):
        """POST /v1/raw-log/toggle enables and disables logging cleanly."""
        # Toggle ON
        resp = await self.client.request("POST", "/v1/raw-log/toggle", json={"enabled": True})
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["enabled"])
        self.assertTrue(proxy_mod._raw_logging_enabled)

        # Toggle OFF
        resp2 = await self.client.request("POST", "/v1/raw-log/toggle", json={"enabled": False})
        self.assertEqual(resp2.status, 200)
        data2 = await resp2.json()
        self.assertFalse(data2["enabled"])
        self.assertFalse(proxy_mod._raw_logging_enabled)

    @unittest_run_loop
    async def test_logging_enforcement_disk_and_stream(self):
        """Verify that when logging is disabled, append_raw_payload does not write to disk."""
        proxy_mod._raw_logging_enabled = False
        record = make_raw_payload_record(
            req_id="test_req", path="/v1", method="POST", call_type="chat", model="test",
            client_ip="127.0.0.1", req_headers={}, payload_obj={}, status_code=200, resp_headers={},
            is_stream=False, ttfb_ms=10, total_ms=20, tokens_per_s=10, input_tokens=5, output_tokens=5,
            reasoning_tokens=0, content_text="hi", reasoning_text="", tool_calls=None, raw_resp_json=None, error=None
        )

        append_raw_payload(record)
        self.assertFalse(self.test_log_file.exists())

        # Enable and write
        proxy_mod._raw_logging_enabled = True
        append_raw_payload(record)
        self.assertTrue(self.test_log_file.exists())
        self.assertGreater(self.test_log_file.stat().st_size, 0)

    @unittest_run_loop
    async def test_clear_endpoint(self):
        """POST /v1/raw-log/clear clears the log file."""
        proxy_mod._raw_logging_enabled = True
        record = make_raw_payload_record(
            req_id="test_req", path="/v1", method="POST", call_type="chat", model="test",
            client_ip="127.0.0.1", req_headers={}, payload_obj={}, status_code=200, resp_headers={},
            is_stream=False, ttfb_ms=10, total_ms=20, tokens_per_s=10, input_tokens=5, output_tokens=5,
            reasoning_tokens=0, content_text="hi", reasoning_text="", tool_calls=None, raw_resp_json=None, error=None
        )
        append_raw_payload(record)
        self.assertGreater(self.test_log_file.stat().st_size, 0)

        resp = await self.client.request("POST", "/v1/raw-log/clear")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["success"])
        self.assertEqual(self.test_log_file.stat().st_size, 0)


class TestDashboardRawLogEndpoints(AioHTTPTestCase):
    async def get_application(self):
        return create_dashboard_app()

    @unittest_run_loop
    async def test_dashboard_raw_log_status(self):
        """GET /api/raw-log/status returns status structure."""
        resp = await self.client.request("GET", "/api/raw-log/status")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertIn("enabled", data)
        self.assertIn("file_path", data)
        self.assertIn("file_size_formatted", data)

    @unittest_run_loop
    async def test_dashboard_raw_log_recent(self):
        """GET /api/raw-log/recent returns list of entries."""
        resp = await self.client.request("GET", "/api/raw-log/recent?limit=10")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertIn("entries", data)
        self.assertIsInstance(data["entries"], list)


if __name__ == "__main__":
    unittest.main()
