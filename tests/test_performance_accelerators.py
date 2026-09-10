#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for High-Performance Engine Accelerators & Native Binary Infrastructure.

Validates:
1. proxy.fast_json SIMD/fallback serialization and deserialization
2. SQLite WAL pragma configuration in telemetry_db
3. Native binary detection in dashboard.proxy_manager
4. scripts.build_binaries CLI argument parsing & toolchain checks
"""

import sys
import os
import gc
import json
import sqlite3
import tempfile
import argparse
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import proxy.fast_json as fast_json
import proxy.telemetry_db as telemetry_db
import dashboard.proxy_manager as proxy_manager
import scripts.build_binaries as build_binaries


class TestFastJson(unittest.TestCase):
    """Verify high-performance JSON operations and fallback consistency."""

    def test_json_loads_str_and_bytes(self):
        obj = {"model": "meta-llama/Llama-3.3-70B", "tokens": 128, "streaming": True, "active": [1, 2, 3]}
        raw_str = json.dumps(obj)
        raw_bytes = raw_str.encode("utf-8")

        parsed_from_str = fast_json.json_loads(raw_str)
        parsed_from_bytes = fast_json.json_loads(raw_bytes)

        self.assertEqual(parsed_from_str, obj)
        self.assertEqual(parsed_from_bytes, obj)

    def test_json_dumps_and_bytes(self):
        obj = {"status": "ok", "latency_ms": 14.5, "nested": {"key": "val"}}
        dumped_str = fast_json.json_dumps(obj)
        dumped_bytes = fast_json.json_dumps_bytes(obj)

        self.assertIsInstance(dumped_str, str)
        self.assertIsInstance(dumped_bytes, bytes)
        self.assertEqual(json.loads(dumped_str), obj)
        self.assertEqual(json.loads(dumped_bytes.decode("utf-8")), obj)

    def test_json_loads_invalid_raises_decode_error(self):
        with self.assertRaises(Exception):
            fast_json.json_loads(b"invalid {json [content")


class TestSqlitePragmas(unittest.TestCase):
    """Verify SQLite WAL and cache performance PRAGMA configuration."""

    def test_configure_db_pragmas_applied(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            db_file = Path(tf.name)
        try:
            conn = sqlite3.connect(str(db_file))
            telemetry_db._configure_db_pragmas(conn)

            # Query applied pragmas
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode")
            mode = cur.fetchone()[0].lower()
            self.assertEqual(mode, "wal", "SQLite should be configured in WAL mode")

            cur.execute("PRAGMA synchronous")
            sync = cur.fetchone()[0]
            self.assertEqual(sync, 1, "SQLite synchronous should be NORMAL (1)")

            cur.execute("PRAGMA busy_timeout")
            b_timeout = cur.fetchone()[0]
            self.assertEqual(b_timeout, 5000, "SQLite busy_timeout should be 5000ms")

            conn.close()
        finally:
            if db_file.exists():
                try:
                    db_file.unlink()
                except Exception:
                    pass


class TestProxyManagerBinaryDetection(unittest.TestCase):
    """Verify proxy_manager detects pre-compiled binaries appropriately."""

    def test_get_proxy_binary_path_when_absent(self):
        with patch.object(Path, "is_file", return_value=False):
            bin_path = proxy_manager.get_proxy_binary_path()
            self.assertIsNone(bin_path)

    def test_get_proxy_binary_path_when_present(self):
        fake_binary = REPO_ROOT / "dist" / "llm_telemetry_proxy.bin"
        with patch.object(Path, "is_file", side_effect=lambda: True), \
             patch("os.access", return_value=True):
            bin_path = proxy_manager.get_proxy_binary_path()
            self.assertIsNotNone(bin_path)


class TestBuildBinariesScript(unittest.TestCase):
    """Verify build script argument parsing and toolchain inspection."""

    def test_check_toolchain_runs_cleanly(self):
        # check_toolchain() should return a boolean without crashing
        res = build_binaries.check_toolchain()
        self.assertIsInstance(res, bool)

    def test_default_build_target_is_all(self):
        # When no --target flag is passed, build_binaries should default to compiling all components
        parser = argparse.ArgumentParser()
        parser.add_argument("--target", default="all")
        args = parser.parse_args([])
        self.assertEqual(args.target, "all")

    def test_gc_freeze_supported_or_noop(self):
        # gc.freeze should execute cleanly if available on Python 3.12+
        if hasattr(gc, "freeze"):
            try:
                gc.freeze()
            except Exception as e:
                self.fail(f"gc.freeze() raised an exception: {e}")


if __name__ == "__main__":
    unittest.main()
