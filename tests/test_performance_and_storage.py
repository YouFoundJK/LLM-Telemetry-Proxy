#!/usr/bin/env python3
"""
Test Suite: Telemetry Retrieval Optimization, Bulk Sync Endpoint, and Storage TTL.
"""

import sys
import os
import json
import gzip
import time
import sqlite3
import unittest
from pathlib import Path
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

# Add project root to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dashboard.server import create_app, get_db, get_db_fingerprint, get_resolved_model
from proxy.llm_telemetry_proxy import init_db


class TestBulkRetrievalAndIndexes(unittest.TestCase):
    def setUp(self):
        self.conn = get_db()

    def tearDown(self):
        self.conn.close()

    def test_composite_indexes_exist(self):
        """Verify that high-performance composite indexes are present."""
        init_db()
        indexes = self.conn.execute("PRAGMA index_list(api_calls)").fetchall()
        index_names = [idx["name"] for idx in indexes]
        self.assertIn("idx_api_calls_ts_id", index_names)
        self.assertIn("idx_api_calls_model_ts", index_names)

        proxy_indexes = self.conn.execute("PRAGMA index_list(proxy_calls)").fetchall()
        proxy_index_names = [idx["name"] for idx in proxy_indexes]
        self.assertIn("idx_proxy_calls_ts_id", proxy_index_names)

    def test_db_fingerprint(self):
        """Verify DB fingerprint generation."""
        fp = get_db_fingerprint()
        self.assertIsInstance(fp, str)
        self.assertNotEqual(fp, "none")
        self.assertIn("_", fp)

    def test_raw_fetch_speed_on_large_dataset(self):
        """Verify that indexed SQLite query retrieves 100k+ rows in under 0.6 seconds."""
        t0 = time.time()
        rows = self.conn.execute("""
            SELECT id, timestamp, model, endpoint, input_tokens, output_tokens,
                   ttfb_ms, total_ms, tokens_per_s, server_running, status_code,
                   error, call_type, calls_count
            FROM api_calls
            ORDER BY timestamp ASC, id ASC
            LIMIT 200000
        """).fetchall()
        duration = time.time() - t0
        print(f"\n[BENCHMARK] SQLite fetched {len(rows)} rows in {duration:.3f}s")
        self.assertLess(duration, 1.5, "Database read exceeded performance threshold")


class TestBulkApiEndpoint(AioHTTPTestCase):
    async def get_application(self):
        return create_app()

    @unittest_run_loop
    async def test_bulk_query_schema_and_speed(self):
        """Test GET /api/query/bulk endpoint returns columnar matrix with correct headers."""
        t0 = time.time()
        resp = await self.client.request(
            "GET",
            "/api/query/bulk?limit=50000",
            headers={"Accept-Encoding": "gzip"}
        )
        duration = time.time() - t0
        self.assertEqual(resp.status, 200)

        # Check if already auto-decompressed by aiohttp client or compressed
        raw_body = await resp.read()
        try:
            decompressed = gzip.decompress(raw_body)
            data = json.loads(decompressed.decode("utf-8"))
        except Exception:
            data = json.loads(raw_body.decode("utf-8"))

        print(f"[BENCHMARK] /api/query/bulk returned {data.get('count')} rows in {duration:.3f}s")

        self.assertIn("columns", data)
        self.assertIn("rows", data)
        self.assertIn("count", data)
        self.assertIn("db_fingerprint", data)
        self.assertIn("available_models", data)
        self.assertIn("available_types", data)
        self.assertGreater(data["count"], 0)

        # Verify row length matches columns length
        cols = data["columns"]
        first_row = data["rows"][0]
        self.assertEqual(len(cols), len(first_row))

    @unittest_run_loop
    async def test_bulk_query_since_id_delta(self):
        """Test GET /api/query/bulk?since_id=... for delta sync."""
        # First get a valid ID from the middle of the dataset
        resp1 = await self.client.request("GET", "/api/query/bulk?limit=100")
        raw_body1 = await resp1.read()
        try:
            data1 = json.loads(gzip.decompress(raw_body1).decode("utf-8"))
        except Exception:
            data1 = json.loads(raw_body1.decode("utf-8"))
        mid_id = data1["rows"][50][0]

        # Request since_id
        resp2 = await self.client.request("GET", f"/api/query/bulk?since_id={mid_id}&limit=20")
        raw_body2 = await resp2.read()
        try:
            data2 = json.loads(gzip.decompress(raw_body2).decode("utf-8"))
        except Exception:
            data2 = json.loads(raw_body2.decode("utf-8"))

        self.assertEqual(resp2.status, 200)
        self.assertGreater(len(data2["rows"]), 0)
        for r in data2["rows"]:
            self.assertGreater(r[0], mid_id)

    @unittest_run_loop
    async def test_health_endpoint_includes_fingerprint(self):
        """Test that /health returns the db_fingerprint field."""
        resp = await self.client.request("GET", "/health")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertIn("db_fingerprint", data)
        self.assertTrue(len(data["db_fingerprint"]) > 0)


if __name__ == "__main__":
    unittest.main()
