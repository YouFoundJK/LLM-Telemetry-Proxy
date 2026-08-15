#!/usr/bin/env python3
"""
Comprehensive E2E Workflow Test: Validates all dashboard workflows and API endpoints
under heavy load, concurrent requests, and large date ranges without errors.
"""

import sys
import os
import json
import gzip
import time
import asyncio
import unittest
from pathlib import Path
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

# Add project root to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dashboard.server import create_app


def safe_json_decode(raw_bytes):
    try:
        return json.loads(gzip.decompress(raw_bytes).decode("utf-8"))
    except Exception:
        return json.loads(raw_bytes.decode("utf-8"))


class TestEndToEndDashboardWorkflows(AioHTTPTestCase):
    async def get_application(self):
        return create_app()

    @unittest_run_loop
    async def test_workflow_1_initial_page_load_burst(self):
        """Simulate browser initial page load (firing all startup endpoints concurrently)."""
        endpoints = [
            "/api/costs",
            "/api/query/bulk?limit=1000",
            "/health",
            "/api/proxy/status",
            "/api/raw-log/status"
        ]
        
        async def fetch_ep(ep):
            resp = await self.client.request("GET", ep)
            return resp

        tasks = [fetch_ep(ep) for ep in endpoints]
        responses = await asyncio.gather(*tasks)

        for resp in responses:
            self.assertEqual(resp.status, 200, f"Endpoint {resp.url} returned unexpected status {resp.status}")

    @unittest_run_loop
    async def test_workflow_2_large_two_month_query(self):
        """Simulate querying a massive 2-month range (all 145k+ calls)."""
        t0 = time.time()
        resp = await self.client.request(
            "GET",
            "/api/query/bulk?from=2026-06-01T00:00:00Z&to=2026-08-31T23:59:59Z&limit=500000",
            headers={"Accept-Encoding": "gzip"}
        )
        duration = time.time() - t0
        self.assertEqual(resp.status, 200)

        raw = await resp.read()
        data = safe_json_decode(raw)

        self.assertGreater(data["count"], 100000, "Expected large record set for 2-month range")
        self.assertLess(duration, 2.5, f"Bulk query took {duration:.3f}s, exceeding SLA")
        print(f"\n[E2E] Massive 2-month bulk query ({data['count']} rows) served in {duration:.3f}s")

    @unittest_run_loop
    async def test_workflow_3_filtered_queries(self):
        """Simulate model filtering and error filtering."""
        # Query with model filter
        resp_model = await self.client.request("GET", "/api/query/bulk?model=glm-4&limit=500")
        self.assertEqual(resp_model.status, 200)
        raw_m = await resp_model.read()
        data_m = safe_json_decode(raw_m)
        for row in data_m["rows"]:
            self.assertIn("glm", row[2].lower())

        # Query with errors_only filter
        resp_err = await self.client.request("GET", "/api/query/bulk?errors_only=1&limit=500")
        self.assertEqual(resp_err.status, 200)
        raw_e = await resp_err.read()
        data_e = safe_json_decode(raw_e)
        self.assertIsInstance(data_e["rows"], list)

    @unittest_run_loop
    async def test_workflow_4_rapid_delta_sync_burst(self):
        """Simulate rapid successive delta sync pings (testing rate resilience)."""
        for i in range(10):
            resp = await self.client.request("GET", f"/api/query/bulk?since_id=140000&limit=50")
            self.assertEqual(resp.status, 200, f"Rapid request {i} failed with status {resp.status}")


if __name__ == "__main__":
    unittest.main()
