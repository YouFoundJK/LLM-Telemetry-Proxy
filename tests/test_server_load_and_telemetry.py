#!/usr/bin/env python3
"""
Unit and Integration Tests for Server Load Telemetry,
Model Resolution, and Metric Aggregation Integrity.
"""

import sys
import json
import asyncio
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add project root to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from proxy.llm_telemetry_proxy import (
    fetch_server_load,
    _load_cache,
    resolve_canonical_model,
    log_call,
)


class MockResponse:
    def __init__(self, data, status=200):
        self._data = data
        self.status = status

    async def json(self):
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class MockSession:
    def __init__(self, mock_data):
        self.mock_data = mock_data

    def get(self, url, timeout=None):
        return MockResponse(self.mock_data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class TestServerLoadTelemetry(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Reset cache before each test
        _load_cache["data"] = None
        _load_cache["ts"] = 0

        # Sample status API response containing online, archived, and embedding models
        self.sample_status_api = [
            {
                "model_name": "Deepseek-v4",
                "status": "archived",
                "latest": {
                    "num_requests_running": 6,
                    "generation_tokens_rate": 280.41,
                },
            },
            {
                "model_name": "Deepseek-v4-flash",
                "status": "online",
                "latest": {
                    "num_requests_running": 3,
                    "generation_tokens_rate": 150.2,
                },
            },
            {
                "model_name": "Glm-5.2",
                "status": "online",
                "latest": {
                    "num_requests_running": 8,
                    "generation_tokens_rate": 310.0,
                },
            },
            {
                "model_name": "Qwen3.5-122b",
                "status": "online",
                "latest": {
                    "num_requests_running": 0,
                    "generation_tokens_rate": 0.0,
                },
            },
            {
                "model_name": "Qwen3.5-int4",
                "status": "online",
                "latest": {
                    "num_requests_running": 2,
                    "generation_tokens_rate": 120.0,
                },
            },
            {
                "model_name": "qwen3-embedding-4b",
                "status": "online",
                "latest": {
                    "num_requests_running": 0,
                    "generation_tokens_rate": 0.0,
                },
            },
        ]

    async def test_unmonitored_or_routed_model_returns_none(self):
        """Unmonitored / third-party models must return (None, None, None) without fallback."""
        with patch("aiohttp.ClientSession", return_value=MockSession(self.sample_status_api)):
            running, tok_s, model_name = await fetch_server_load("stealth/ox-alpha")
            self.assertIsNone(running)
            self.assertIsNone(tok_s)
            self.assertIsNone(model_name)

            running, tok_s, model_name = await fetch_server_load("gpt-4o")
            self.assertIsNone(running)
            self.assertIsNone(tok_s)
            self.assertIsNone(model_name)

    async def test_none_or_empty_hint_returns_none(self):
        """Empty or None model hint returns (None, None, None) immediately."""
        running, tok_s, model_name = await fetch_server_load(None)
        self.assertIsNone(running)
        self.assertIsNone(tok_s)
        self.assertIsNone(model_name)

        running, tok_s, model_name = await fetch_server_load("")
        self.assertIsNone(running)
        self.assertIsNone(tok_s)
        self.assertIsNone(model_name)

    async def test_archived_models_filtered_out(self):
        """Archived models (like the decommissioned Deepseek-v4 with load 6) must never be returned."""
        with patch("aiohttp.ClientSession", return_value=MockSession(self.sample_status_api)):
            # Archived model Deepseek-v4 is filtered out; resolves to online Deepseek-v4-flash via canonical mapping
            running, tok_s, model_name = await fetch_server_load("Deepseek-v4")
            if running is not None:
                self.assertEqual(running, 3)
                self.assertEqual(model_name, "Deepseek-v4-flash")
            else:
                self.assertIsNone(running)

    async def test_exact_and_canonical_matching(self):
        """Active online models are matched accurately."""
        with patch("aiohttp.ClientSession", return_value=MockSession(self.sample_status_api)):
            # Direct match
            running, tok_s, model_name = await fetch_server_load("Glm-5.2")
            self.assertEqual(running, 8)
            self.assertEqual(model_name, "Glm-5.2")

            # Canonical alias match ('glm' -> 'glm-5.2')
            running, tok_s, model_name = await fetch_server_load("glm")
            self.assertEqual(running, 8)
            self.assertEqual(model_name, "Glm-5.2")

            # Idle model with 0 running requests
            running, tok_s, model_name = await fetch_server_load("qwen3.5-122b")
            self.assertEqual(running, 0)
            self.assertEqual(model_name, "Qwen3.5-122b")

    async def test_thinking_suffix_resolution(self):
        """Thinking model requests resolve to the underlying active container."""
        with patch("aiohttp.ClientSession", return_value=MockSession(self.sample_status_api)):
            running, tok_s, model_name = await fetch_server_load("deepseek-v4-flash-thinking")
            self.assertEqual(running, 3)
            self.assertEqual(model_name, "Deepseek-v4-flash")

    async def test_embedding_model_matches_its_own_container(self):
        """Embedding model matches embedding container, not unrelated LLM container."""
        with patch("aiohttp.ClientSession", return_value=MockSession(self.sample_status_api)):
            running, tok_s, model_name = await fetch_server_load("qwen3-embedding-4b")
            self.assertEqual(running, 0)
            self.assertEqual(model_name, "qwen3-embedding-4b")


class TestMetricAggregationIntegrity(unittest.TestCase):
    def test_zero_load_aggregation_in_sql(self):
        """Verify that load=0 (idle) is counted in averages, and NULL load is not counted."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE api_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                model TEXT,
                ttfb_ms REAL,
                total_ms REAL,
                tokens_per_s REAL,
                server_running REAL,
                calls_count INTEGER
            )
        """)

        # 2 calls with load=0, 2 calls with load=6, 2 calls with load=NULL
        conn.execute("INSERT INTO api_calls (model, server_running, calls_count) VALUES ('model-a', 0.0, 1)")
        conn.execute("INSERT INTO api_calls (model, server_running, calls_count) VALUES ('model-a', 0.0, 1)")
        conn.execute("INSERT INTO api_calls (model, server_running, calls_count) VALUES ('model-a', 6.0, 1)")
        conn.execute("INSERT INTO api_calls (model, server_running, calls_count) VALUES ('model-a', 6.0, 1)")
        conn.execute("INSERT INTO api_calls (model, server_running, calls_count) VALUES ('model-a', NULL, 1)")
        conn.execute("INSERT INTO api_calls (model, server_running, calls_count) VALUES ('model-a', NULL, 1)")

        # Query using the fixed logic
        row = conn.execute("""
            SELECT
                SUM(server_running * COALESCE(calls_count, 1)) as sum_load,
                SUM(CASE WHEN server_running IS NOT NULL THEN COALESCE(calls_count, 1) ELSE 0 END) as load_count,
                SUM(server_running * COALESCE(calls_count, 1)) / NULLIF(SUM(CASE WHEN server_running IS NOT NULL THEN COALESCE(calls_count, 1) ELSE 0 END), 0) as avg_load
            FROM api_calls
        """).fetchone()

        # Expected: sum_load = 0+0+6+6 = 12. load_count = 4 (the two 0s and two 6s). avg_load = 12 / 4 = 3.0
        self.assertEqual(row["sum_load"], 12.0)
        self.assertEqual(row["load_count"], 4)
        self.assertEqual(row["avg_load"], 3.0)

        conn.close()


if __name__ == "__main__":
    unittest.main()
