#!/usr/bin/env python3
"""
LLM Telemetry Query CLI — read data from the telemetry proxy's SQLite DB.

Usage:
    python3 llm_telemetry_query.py                    # 24h summary per model
    python3 llm_telemetry_query.py --recent 10        # last 10 calls
    python3 llm_telemetry_query.py --model glm-5.2    # filter by model
    python3 llm_telemetry_query.py --correlation      # RTT vs server load
    python3 llm_telemetry_query.py --since 1h         # last 1 hour
    python3 llm_telemetry_query.py --raw              # raw JSON output
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "llm_telemetry.db"


def get_conn():
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}", file=sys.stderr)
        print("Start the proxy first: python3 ~/server/llm_proxy/proxy/llm_telemetry_proxy.py", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def since_clause(args):
    """Build WHERE clause for time filtering."""
    if not args.since:
        return ""
    unit_map = {"m": "minutes", "h": "hours", "d": "days"}
    unit = args.since[-1]
    if unit not in unit_map:
        return ""
    n = args.since[:-1]
    return f"AND timestamp > datetime('now', '-{n} {unit_map[unit]}')"


def print_summary(args):
    conn = get_conn()
    where = since_clause(args)
    model_filter = f"AND model = '{args.model}'" if args.model else ""

    rows = conn.execute(f"""
        SELECT
            model,
            SUM(COALESCE(calls_count, 1)) as calls,
            ROUND(SUM(ttfb_ms) / SUM(COALESCE(calls_count, 1)), 0) as avg_ttfb,
            ROUND(MAX(ttfb_ms), 0) as max_ttfb,
            ROUND(SUM(total_ms) / SUM(COALESCE(calls_count, 1)), 0) as avg_total,
            ROUND(MAX(total_ms), 0) as max_total,
            ROUND(SUM(tokens_per_s * COALESCE(calls_count, 1)) / SUM(COALESCE(calls_count, 1)), 1) as avg_tps,
            ROUND(SUM(server_running * COALESCE(calls_count, 1)) / SUM(COALESCE(calls_count, 1)), 1) as avg_load,
            ROUND(SUM(input_tokens) / SUM(COALESCE(calls_count, 1)), 0) as avg_input,
            ROUND(SUM(output_tokens) / SUM(COALESCE(calls_count, 1)), 0) as avg_output,
            SUM(CASE WHEN error IS NOT NULL THEN COALESCE(calls_count, 1) ELSE 0 END) as errors
        FROM api_calls
        WHERE 1=1 {where} {model_filter}
        GROUP BY model
        ORDER BY calls DESC
    """).fetchall()
    conn.close()

    if not rows:
        print("No data yet.")
        return

    print(f"\n{'Model':<25} {'Calls':>6} {'Avg TTFB':>9} {'Max TTFB':>9} {'Avg RTT':>8} {'Max RTT':>8} {'Avg TPS':>8} {'Avg Load':>9} {'Errors':>7}")
    print("─" * 100)
    for r in rows:
        def fmt_ms(v):
            return f"{v:.0f}ms" if v else "—"
        def fmt_n(v):
            return f"{v:.1f}" if v is not None else "—"
        print(f"{r['model'] or '?':<25} {r['calls']:>6} {fmt_ms(r['avg_ttfb']):>9} {fmt_ms(r['max_ttfb']):>9} {fmt_ms(r['avg_total']):>8} {fmt_ms(r['max_total']):>8} {fmt_n(r['avg_tps']):>8} {fmt_n(r['avg_load']):>9} {r['errors'] or 0:>7}")


def print_recent(args):
    conn = get_conn()
    limit = args.recent
    model_filter = f"AND model = '{args.model}'" if args.model else ""
    where = since_clause(args)

    rows = conn.execute(f"""
        SELECT * FROM api_calls
        WHERE 1=1 {where} {model_filter}
        ORDER BY id DESC LIMIT {limit}
    """).fetchall()
    conn.close()

    if not rows:
        print("No calls logged yet.")
        return

    for r in rows:
        t = r['timestamp'][11:19] if r['timestamp'] else "?"
        model = r['model'] or '?'
        ttfb = f"{r['ttfb_ms']:.0f}ms" if r['ttfb_ms'] else "—"
        total = f"{r['total_ms']:.0f}ms" if r['total_ms'] else "—"
        tps = f"{r['tokens_per_s']:.1f}t/s" if r['tokens_per_s'] else "—"
        load = f"load={r['server_running']:.0f}" if r['server_running'] is not None else "load=—"
        out_tok = f"{r['output_tokens']}tok" if r['output_tokens'] else "—"
        err = f" ❌ {r['error']}" if r['error'] else ""
        status = f" [{r['status_code']}]" if r['status_code'] and r['status_code'] != 200 else ""
        print(f"{t} {model:<25} TTFB={ttfb:<8} RTT={total:<9} {tps:<10} {load:<8} out={out_tok}{status}{err}")


def print_correlation(args):
    """Show how RTT correlates with server load — the key insight."""
    conn = get_conn()
    where = since_clause(args)
    model_filter = f"AND model = '{args.model}'" if args.model else ""

    rows = conn.execute(f"""
        SELECT
            CASE
                WHEN server_running IS NULL THEN 'unknown'
                WHEN server_running = 0 THEN '0 (idle)'
                WHEN server_running <= 2 THEN '1-2'
                WHEN server_running <= 5 THEN '3-5'
                WHEN server_running <= 10 THEN '6-10'
                ELSE '10+'
            END as load_bucket,
            SUM(COALESCE(calls_count, 1)) as calls,
            ROUND(SUM(total_ms) / SUM(COALESCE(calls_count, 1)), 0) as avg_rtt,
            ROUND(MAX(total_ms), 0) as max_rtt,
            ROUND(SUM(ttfb_ms) / SUM(COALESCE(calls_count, 1)), 0) as avg_ttfb,
            ROUND(SUM(tokens_per_s * COALESCE(calls_count, 1)) / SUM(COALESCE(calls_count, 1)), 1) as avg_tps,
            ROUND(SUM(server_tok_s * COALESCE(calls_count, 1)) / SUM(COALESCE(calls_count, 1)), 0) as avg_srv_tps
        FROM api_calls
        WHERE server_running IS NOT NULL {where} {model_filter}
        GROUP BY load_bucket
        ORDER BY MIN(server_running)
    """).fetchall()
    conn.close()

    if not rows:
        print("No server load data yet (need calls where status API was reachable).")
        return

    print(f"\n{'Server Load (running)':<22} {'Calls':>6} {'Avg RTT':>9} {'Max RTT':>9} {'Avg TTFB':>9} {'Avg TPS':>8} {'Server tok/s':>13}")
    print("─" * 80)
    for r in rows:
        avg_rtt = r['avg_rtt'] or 0
        max_rtt = r['max_rtt'] or 0
        avg_ttfb = r['avg_ttfb'] or 0
        avg_tps = r['avg_tps'] or 0
        avg_srv = r['avg_srv_tps'] or 0
        print(f"{r['load_bucket']:<22} {r['calls']:>6} {avg_rtt:.0f}ms   {max_rtt:.0f}ms   {avg_ttfb:.0f}ms   {avg_tps:.1f}    {avg_srv:.0f}")


def print_raw(args):
    conn = get_conn()
    where = since_clause(args)
    model_filter = f"AND model = '{args.model}'" if args.model else ""
    limit = args.recent if args.recent else 50

    rows = conn.execute(f"""
        SELECT * FROM api_calls
        WHERE 1=1 {where} {model_filter}
        ORDER BY id DESC LIMIT {limit}
    """).fetchall()
    conn.close()

    print(json.dumps([dict(r) for r in rows], indent=2))


def main():
    p = argparse.ArgumentParser(description="LLM telemetry query")
    p.add_argument("--recent", type=int, default=0, help="Show N most recent calls")
    p.add_argument("--model", type=str, default="", help="Filter by model name")
    p.add_argument("--since", type=str, default="", help="Time window: 30m, 1h, 1d")
    p.add_argument("--correlation", action="store_true", help="RTT vs server load correlation")
    p.add_argument("--raw", action="store_true", help="Raw JSON output")
    args = p.parse_args()

    if args.raw:
        print_raw(args)
    elif args.correlation:
        print_correlation(args)
    elif args.recent:
        print_recent(args)
    else:
        print_summary(args)


if __name__ == "__main__":
    main()
