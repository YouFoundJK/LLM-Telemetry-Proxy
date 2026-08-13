#!/usr/bin/env python3
"""
LLM Telemetry Database Compressor.
Aggregates historical telemetry data (older than 14 days) into 2-week intervals.
Grouped by model, endpoint, call_type, status_code, and error.
"""

import argparse
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys

env_db_path = os.environ.get("TELEMETRY_DB_PATH")
DB_PATH = Path(env_db_path) if env_db_path else (Path(__file__).resolve().parent.parent / "data" / "llm_telemetry.db")
BASE_EPOCH = datetime(2026, 1, 5, 0, 0, 0, tzinfo=timezone.utc) # Monday

def backup_db(db_path):
    if db_path.exists():
        backup_path = db_path.with_suffix(".db.bak")
        shutil.copy2(db_path, backup_path)
        print(f"[OK] Database backup created at: {backup_path}")
        return backup_path
    return None

def restore_db(backup_path, db_path):
    if backup_path and backup_path.exists():
        try:
            shutil.copy2(backup_path, db_path)
            print(f"[WARNING] Database successfully restored from backup: {backup_path}")
        except Exception as e:
            print(f"[ERROR] Critical failure: Could not restore backup database: {e}", file=sys.stderr)

def is_bucket_closed(ts_str, cutoff_dt):
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        dt_utc = dt.astimezone(timezone.utc)
        delta = dt_utc - BASE_EPOCH
        two_week_index = delta.days // 14
        bucket_start = BASE_EPOCH + timedelta(days=two_week_index * 14)
        bucket_end = bucket_start + timedelta(days=14)
        return bucket_end < cutoff_dt
    except Exception:
        return False

def load_model_mapping() -> dict:
    mapping_path = Path(__file__).resolve().parent.parent / "model_mapping.json"
    if mapping_path.exists():
        try:
            with open(mapping_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[compressor] Error loading model_mapping.json: {e}", file=sys.stderr)
    return {}

def get_resolved_model(model_name: str, mapping: dict) -> str:
    if not model_name:
        return model_name
    model_lower = model_name.lower().strip()
    for main_model, aliases in mapping.items():
        if model_lower == main_model.lower().strip():
            return main_model
        for alias in aliases:
            if model_lower == alias.lower().strip():
                return main_model
    return model_name

def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def get_bucket_timestamp(ts_str):
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        dt_utc = dt.astimezone(timezone.utc)
        delta = dt_utc - BASE_EPOCH
        two_week_index = delta.days // 14
        bucket_start = BASE_EPOCH + timedelta(days=two_week_index * 14)
        return bucket_start.isoformat()
    except Exception as e:
        print(f"Error parsing timestamp {ts_str}: {e}", file=sys.stderr)
        return ts_str

def run_migrations(conn):
    try:
        conn.execute("ALTER TABLE api_calls ADD COLUMN calls_count INTEGER DEFAULT 1")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE proxy_calls ADD COLUMN calls_count INTEGER DEFAULT 1")
    except sqlite3.OperationalError:
        pass

def compress(dry_run=False):
    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    conn = get_db()
    run_migrations(conn)
    conn.commit()
    
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    cutoff_str = cutoff.isoformat()
    print(f"Compressing data older than: {cutoff_str}")

    api_candidates = conn.execute(
        "SELECT * FROM api_calls WHERE timestamp < ? ORDER BY timestamp ASC",
        (cutoff_str,)
    ).fetchall()
    
    proxy_candidates = conn.execute(
        "SELECT * FROM proxy_calls WHERE timestamp < ? ORDER BY timestamp ASC",
        (cutoff_str,)
    ).fetchall()

    api_rows = [r for r in api_candidates if is_bucket_closed(r["timestamp"], cutoff)]
    api_skipped = len(api_candidates) - len(api_rows)
    
    proxy_rows = [r for r in proxy_candidates if is_bucket_closed(r["timestamp"], cutoff)]
    proxy_skipped = len(proxy_candidates) - len(proxy_rows)

    print(f"Found {len(api_rows)} closed-bucket rows in api_calls to compress (skipped {api_skipped} active-bucket rows).")
    print(f"Found {len(proxy_rows)} closed-bucket rows in proxy_calls to compress (skipped {proxy_skipped} active-bucket rows).")

    if not api_rows and not proxy_rows:
        print("No historical closed-bucket data to compress.")
        conn.close()
        return

    mapping = load_model_mapping()

    # Process api_calls
    api_groups = {}
    for r in api_rows:
        resolved_model = get_resolved_model(r["model"], mapping)
        bucket_ts = get_bucket_timestamp(r["timestamp"])
        key = (
            bucket_ts,
            resolved_model,
            r["endpoint"],
            r["call_type"],
            r["status_code"],
            r["error"]
        )
        if key not in api_groups:
            api_groups[key] = []
        api_groups[key].append(r)

    # Process proxy_calls
    proxy_groups = {}
    for r in proxy_rows:
        resolved_model = get_resolved_model(r["model"], mapping)
        bucket_ts = get_bucket_timestamp(r["timestamp"])
        key = (
            bucket_ts,
            r["endpoint"],
            r["method"],
            r["call_type"],
            resolved_model,
            r["status_code"],
            r["error"],
            r["logged"]
        )
        if key not in proxy_groups:
            proxy_groups[key] = []
        proxy_groups[key].append(r)

    # Compile api_calls aggregated rows
    aggregated_api = []
    for key, group in api_groups.items():
        bucket_ts, model, endpoint, call_type, status_code, error = key
        
        total_calls = 0
        total_input = 0
        total_output = 0
        total_ttfb = 0.0
        total_rtt = 0.0
        sum_tps = 0.0
        sum_running = 0.0
        sum_tok_s = 0.0
        
        tps_count = 0
        running_count = 0
        tok_s_count = 0
        server_model = None

        for r in group:
            c_count = r["calls_count"] if (r["calls_count"] is not None) else 1
            total_calls += c_count
            total_input += r["input_tokens"] if r["input_tokens"] else 0
            total_output += r["output_tokens"] if r["output_tokens"] else 0
            total_ttfb += r["ttfb_ms"] if r["ttfb_ms"] else 0.0
            total_rtt += r["total_ms"] if r["total_ms"] else 0.0
            
            if r["tokens_per_s"] is not None:
                sum_tps += r["tokens_per_s"] * c_count
                tps_count += c_count
            if r["server_running"] is not None:
                sum_running += r["server_running"] * c_count
                running_count += c_count
            if r["server_tok_s"] is not None:
                sum_tok_s += r["server_tok_s"] * c_count
                tok_s_count += c_count
            
            if r["server_model"] and not server_model:
                server_model = r["server_model"]

        avg_tps = sum_tps / tps_count if tps_count > 0 else None
        avg_running = sum_running / running_count if running_count > 0 else None
        avg_tok_s = sum_tok_s / tok_s_count if tok_s_count > 0 else None

        aggregated_api.append({
            "timestamp": bucket_ts,
            "model": model,
            "endpoint": endpoint,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "ttfb_ms": total_ttfb,
            "total_ms": total_rtt,
            "tokens_per_s": avg_tps,
            "server_running": avg_running,
            "server_tok_s": avg_tok_s,
            "server_model": server_model,
            "status_code": status_code,
            "error": error,
            "call_type": call_type,
            "calls_count": total_calls
        })

    # Compile proxy_calls aggregated rows
    aggregated_proxy = []
    for key, group in proxy_groups.items():
        bucket_ts, endpoint, method, call_type, model, status_code, error, logged = key
        
        total_calls = 0
        total_ttfb = 0.0
        total_rtt = 0.0
        ttfb_count = 0
        rtt_count = 0

        for r in group:
            c_count = r["calls_count"] if (r["calls_count"] is not None) else 1
            total_calls += c_count
            
            if r["ttfb_ms"] is not None:
                total_ttfb += r["ttfb_ms"]
            if r["total_ms"] is not None:
                total_rtt += r["total_ms"]

        avg_ttfb = total_ttfb if total_calls > 0 else None
        avg_rtt = total_rtt if total_calls > 0 else None

        aggregated_proxy.append({
            "timestamp": bucket_ts,
            "endpoint": endpoint,
            "method": method,
            "call_type": call_type,
            "model": model,
            "status_code": status_code,
            "error": error,
            "logged": logged,
            "ttfb_ms": avg_ttfb,
            "total_ms": avg_rtt,
            "calls_count": total_calls
        })

    if dry_run:
        print("\n--- DRY RUN SUMMARY ---")
        print(f"Would compress {len(api_rows)} rows in api_calls into {len(aggregated_api)} aggregated rows:")
        for idx, row in enumerate(aggregated_api[:10]):
            print(f"  {idx+1}. timestamp={row['timestamp']} model={row['model']} calls={row['calls_count']} input={row['input_tokens']} output={row['output_tokens']}")
        if len(aggregated_api) > 10:
            print(f"  ... and {len(aggregated_api) - 10} more rows.")
            
        print(f"Would compress {len(proxy_rows)} rows in proxy_calls into {len(aggregated_proxy)} aggregated rows:")
        for idx, row in enumerate(aggregated_proxy[:10]):
            print(f"  {idx+1}. timestamp={row['timestamp']} endpoint={row['endpoint']} calls={row['calls_count']} logged={row['logged']}")
        if len(aggregated_proxy) > 10:
            print(f"  ... and {len(aggregated_proxy) - 10} more rows.")
        conn.close()
        return

    backup_path = None
    if not dry_run:
        backup_path = backup_db(DB_PATH)

    try:
        conn.execute("BEGIN TRANSACTION")
        
        if api_rows:
            api_ids = [(r["id"],) for r in api_rows]
            conn.executemany("DELETE FROM api_calls WHERE id = ?", api_ids)
            conn.executemany("""
                INSERT INTO api_calls
                    (timestamp, model, endpoint, input_tokens, output_tokens,
                     ttfb_ms, total_ms, tokens_per_s, server_running, server_tok_s,
                     server_model, status_code, error, call_type, calls_count)
                VALUES
                    (:timestamp, :model, :endpoint, :input_tokens, :output_tokens,
                     :ttfb_ms, :total_ms, :tokens_per_s, :server_running, :server_tok_s,
                     :server_model, :status_code, :error, :call_type, :calls_count)
            """, aggregated_api)

        if proxy_rows:
            proxy_ids = [(r["id"],) for r in proxy_rows]
            conn.executemany("DELETE FROM proxy_calls WHERE id = ?", proxy_ids)
            conn.executemany("""
                INSERT INTO proxy_calls
                    (timestamp, endpoint, method, call_type, model, status_code, error, logged, ttfb_ms, total_ms, calls_count)
                VALUES
                    (:timestamp, :endpoint, :method, :call_type, :model, :status_code, :error, :logged, :ttfb_ms, :total_ms, :calls_count)
            """, aggregated_proxy)

        conn.commit()
        print("[OK] Data successfully compressed.")
        
        print("Vacuuming database to reclaim disk space...")
        conn.execute("VACUUM")
        print("[OK] Database vacuumed successfully.")

    except Exception as e:
        if not dry_run:
            try:
                conn.rollback()
            except Exception:
                pass
            restore_db(backup_path, DB_PATH)
        print(f"[ERROR] Error during compression: {e}", file=sys.stderr)
        raise e
    finally:
        conn.close()
        # Clean up backup file if successful
        if not dry_run and backup_path and backup_path.exists():
            try:
                os.remove(backup_path)
                print(f"Cleaned up backup file: {backup_path}")
            except Exception as ex:
                print(f"Warning: could not clean up backup file: {ex}", file=sys.stderr)

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="LLM Telemetry Database Compressor")
    p.add_argument("--dry-run", action="store_true", help="Perform a dry run without modifying the database")
    args = p.parse_args()
    
    compress(dry_run=args.dry_run)
