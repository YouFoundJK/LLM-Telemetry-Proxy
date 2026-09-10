#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM Telemetry Proxy Performance & Resource Benchmark Profiler.

Monitors real-time resource utilization, memory fragmentation, latency,
and throughput for comparing standard Python execution vs Nuitka native binary.

Usage:
    # 1. Profile currently running proxy for 30 minutes (default 1800s):
    python scripts/benchmark_monitor.py record --output python_baseline.csv --duration 1800

    # 2. Restart proxy with native binary, then profile again:
    python scripts/benchmark_monitor.py record --output native_binary.csv --duration 1800

    # 3. Compare the two benchmarks side-by-side:
    python scripts/benchmark_monitor.py compare python_baseline.csv native_binary.csv
"""

import argparse
import csv
import json
import os
import signal
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PID_FILE = REPO_ROOT / "data" / ".proxy.pid"
DEFAULT_DB_FILE = REPO_ROOT / "data" / "llm_telemetry.db"


# ── Process Inspection Helpers (Pure Standard Library /proc & psutil fallback) ──

def get_proxy_pid(pid_file: Path) -> Optional[int]:
    """Retrieve active proxy PID from pid file."""
    if pid_file.exists():
        try:
            content = pid_file.read_text(encoding="utf-8").strip()
            if content.isdigit():
                pid = int(content)
                # Check if alive
                if sys.platform != "win32":
                    os.kill(pid, 0)
                return pid
        except Exception:
            pass
    return None


def get_process_mode(pid: int) -> str:
    """Determine whether process is running as a Python script or pre-compiled native binary."""
    try:
        if sys.platform != "win32" and os.path.exists(f"/proc/{pid}/cmdline"):
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                raw = f.read().replace(b"\0", b" ").decode("utf-8", errors="ignore").strip()
            if ".bin" in raw or "llm_telemetry_proxy.dist" in raw or ("llm_telemetry_proxy" in raw and "python" not in raw):
                return "Native Binary (Nuitka)"
            return "Python Script (CPython)"
    except Exception:
        pass
    return "Unknown / Python"


class ProcessSampler:
    """Samples CPU, RSS/VMS memory, threads, and open handles without external dependencies."""

    def __init__(self, pid: int):
        self.pid = pid
        self.last_cpu_time = 0.0
        self.last_sample_time = time.monotonic()
        self.clk_tck = 100
        self.page_size = 4096
        if sys.platform != "win32":
            try:
                self.clk_tck = os.sysconf("SC_CLK_TCK")
                self.page_size = os.sysconf("SC_PAGE_SIZE")
            except Exception:
                pass
        self._init_cpu()

    def _init_cpu(self):
        t = self._read_proc_cpu()
        if t is not None:
            self.last_cpu_time = t
            self.last_sample_time = time.monotonic()

    def _read_proc_cpu(self) -> Optional[float]:
        try:
            stat_path = f"/proc/{self.pid}/stat"
            if os.path.exists(stat_path):
                with open(stat_path, "r") as f:
                    fields = f.read().split()
                # Field 14 is utime, 15 is stime (0-indexed: 13, 14)
                utime = int(fields[13])
                stime = int(fields[14])
                return (utime + stime) / float(self.clk_tck)
        except Exception:
            pass
        return None

    def sample(self) -> Dict[str, Any]:
        now = time.monotonic()
        delta_time = max(0.001, now - self.last_sample_time)
        cpu_percent = 0.0

        # 1. CPU
        curr_cpu_time = self._read_proc_cpu()
        if curr_cpu_time is not None:
            delta_cpu = max(0.0, curr_cpu_time - self.last_cpu_time)
            cpu_percent = round((delta_cpu / delta_time) * 100.0, 2)
            self.last_cpu_time = curr_cpu_time
            self.last_sample_time = now

        # 2. Memory (RSS & VMS)
        rss_mb = 0.0
        vms_mb = 0.0
        threads = 1
        fds = 0

        if sys.platform != "win32":
            try:
                statm_path = f"/proc/{self.pid}/statm"
                if os.path.exists(statm_path):
                    with open(statm_path, "r") as f:
                        parts = f.read().split()
                    vms_pages = int(parts[0])
                    rss_pages = int(parts[1])
                    rss_mb = round((rss_pages * self.page_size) / (1024 * 1024), 2)
                    vms_mb = round((vms_pages * self.page_size) / (1024 * 1024), 2)
            except Exception:
                pass

            try:
                status_path = f"/proc/{self.pid}/status"
                if os.path.exists(status_path):
                    with open(status_path, "r") as f:
                        for line in f:
                            if line.startswith("Threads:"):
                                threads = int(line.split()[1])
                                break
            except Exception:
                pass

            try:
                fd_path = f"/proc/{self.pid}/fd"
                if os.path.exists(fd_path):
                    fds = len(os.listdir(fd_path))
            except Exception:
                pass
        else:
            # Windows fallback via psutil if present
            try:
                import psutil
                p = psutil.Process(self.pid)
                mem = p.memory_info()
                rss_mb = round(mem.rss / (1024 * 1024), 2)
                vms_mb = round(mem.vms / (1024 * 1024), 2)
                cpu_percent = round(p.cpu_percent(interval=None), 2)
                threads = p.num_threads()
                fds = p.num_handles() if hasattr(p, "num_handles") else 0
            except Exception:
                pass

        return {
            "cpu_percent": cpu_percent,
            "rss_mb": rss_mb,
            "vms_mb": vms_mb,
            "threads": threads,
            "fds": fds,
        }


# ── Telemetry & Traffic Sampler ──────────────────────────────────────────────

def fetch_proxy_metrics(host: str = "127.0.0.1", port: int = 9090) -> Dict[str, Any]:
    """Fetch health and token budget metrics directly from the proxy gateway."""
    url = f"http://{host}:{port}/health"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BenchmarkMonitor/1.0"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                tb = data.get("token_budget", {})
                return {
                    "reachable": True,
                    "tokens_used_24h": tb.get("total_used", 0),
                    "token_limit": tb.get("daily_limit", 0),
                    "upstream": data.get("upstream", ""),
                    "concurrency_limit": data.get("concurrency_limit", 0),
                }
    except Exception:
        pass
    return {"reachable": False, "tokens_used_24h": 0, "token_limit": 0, "upstream": "", "concurrency_limit": 0}


def query_db_recent_window(db_path: Path, start_iso: str) -> Dict[str, Any]:
    """Fetch call count, TTFB, RTT, and token stats accumulated during this benchmark run."""
    if not db_path.exists():
        return {
            "calls_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "avg_ttfb_ms": 0.0,
            "avg_rtt_ms": 0.0,
            "errors": 0,
        }
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True, timeout=2.0)
        cur = conn.cursor()
        cur.execute("""
            SELECT
                COUNT(*) as calls,
                COALESCE(SUM(input_tokens), 0) as in_tok,
                COALESCE(SUM(output_tokens), 0) as out_tok,
                COALESCE(AVG(ttfb_ms), 0.0) as avg_ttfb,
                COALESCE(AVG(total_ms), 0.0) as avg_rtt,
                COALESCE(SUM(CASE WHEN status_code >= 400 OR error IS NOT NULL THEN 1 ELSE 0 END), 0) as errs
            FROM api_calls
            WHERE timestamp >= ?
        """, (start_iso,))
        row = cur.fetchone()
        conn.close()
        if row:
            return {
                "calls_count": row[0] or 0,
                "input_tokens": row[1] or 0,
                "output_tokens": row[2] or 0,
                "avg_ttfb_ms": round(row[3] or 0.0, 2),
                "avg_rtt_ms": round(row[4] or 0.0, 2),
                "errors": row[5] or 0,
            }
    except Exception:
        pass
    return {
        "calls_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "avg_ttfb_ms": 0.0,
        "avg_rtt_ms": 0.0,
        "errors": 0,
    }


# ── Main Commands: Record & Compare ──────────────────────────────────────────

def cmd_record(args):
    pid_file = Path(args.pid_file)
    pid = args.pid or get_proxy_pid(pid_file)
    if not pid:
        print(f"\033[1;31m[ERROR]\033[0m Could not locate running proxy PID from {pid_file}.")
        print("Ensure the proxy is running (`bash dashboard.sh proxy status`) or pass --pid <PID> manually.")
        sys.exit(1)

    mode = get_process_mode(pid)
    out_file = Path(args.output)
    duration_s = int(args.duration)
    interval_s = max(1.0, float(args.interval))
    db_file = Path(args.db)

    print("=" * 70)
    print("  LLM Telemetry Proxy — Real-Time Performance & Resource Profiler")
    print("=" * 70)
    print(f"  Target PID      : {pid}")
    print(f"  Execution Mode  : \033[1;36m{mode}\033[0m")
    print(f"  Sampling Period : {duration_s}s (Every {interval_s}s)")
    print(f"  CSV Log Target  : {out_file.resolve()}")
    print("=" * 70)
    print("Press Ctrl+C at any time to finish early and save the report.\n")

    sampler = ProcessSampler(pid)
    start_time = time.time()
    start_iso = datetime.now(timezone.utc).isoformat()
    end_time = start_time + duration_s

    out_file.parent.mkdir(parents=True, exist_ok=True)
    csv_file = open(out_file, "w", newline="", encoding="utf-8")
    fieldnames = [
        "timestamp", "elapsed_s", "mode", "pid",
        "cpu_percent", "rss_mb", "vms_mb", "threads", "fds",
        "total_calls", "input_tokens", "output_tokens",
        "avg_ttfb_ms", "avg_rtt_ms", "errors",
    ]
    writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
    writer.writeheader()
    csv_file.flush()

    stop_requested = False

    def handle_sigint(sig, frame):
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, handle_sigint)

    sample_count = 0
    init_rss = None
    peak_rss = 0.0
    peak_cpu = 0.0

    try:
        while not stop_requested and time.time() < end_time:
            time.sleep(interval_s)
            elapsed = int(time.time() - start_time)

            proc_stats = sampler.sample()
            db_stats = query_db_recent_window(db_file, start_iso)

            rss = proc_stats["rss_mb"]
            cpu = proc_stats["cpu_percent"]

            if init_rss is None and rss > 0:
                init_rss = rss
            if rss > peak_rss:
                peak_rss = rss
            if cpu > peak_cpu:
                peak_cpu = cpu

            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "elapsed_s": elapsed,
                "mode": mode,
                "pid": pid,
                "cpu_percent": cpu,
                "rss_mb": rss,
                "vms_mb": proc_stats["vms_mb"],
                "threads": proc_stats["threads"],
                "fds": proc_stats["fds"],
                "total_calls": db_stats["calls_count"],
                "input_tokens": db_stats["input_tokens"],
                "output_tokens": db_stats["output_tokens"],
                "avg_ttfb_ms": db_stats["avg_ttfb_ms"],
                "avg_rtt_ms": db_stats["avg_rtt_ms"],
                "errors": db_stats["errors"],
            }
            writer.writerow(record)
            csv_file.flush()
            sample_count += 1

            # Real-time console status line
            rem_s = max(0, int(end_time - time.time()))
            rem_str = f"{rem_s // 60:02d}:{rem_s % 60:02d}"
            elap_str = f"{elapsed // 60:02d}:{elapsed % 60:02d}"

            sys.stdout.write(
                f"\r[\033[1;33m{elap_str}\033[0m / {duration_s // 60:02d}:00] "
                f"RAM: \033[1;32m{rss:5.1f} MB\033[0m (Peak: {peak_rss:5.1f}) | "
                f"CPU: \033[1;34m{cpu:4.1f}%\033[0m | "
                f"Calls: \033[1;35m{db_stats['calls_count']}\033[0m | "
                f"TTFB: {db_stats['avg_ttfb_ms']:5.1f}ms | "
                f"RTT: {db_stats['avg_rtt_ms']:5.1f}ms   "
            )
            sys.stdout.flush()

    finally:
        csv_file.close()
        print("\n\n" + "=" * 70)
        print("  Benchmark Recording Complete!")
        print("=" * 70)
        total_time = int(time.time() - start_time)
        print(f"  Duration Recorded : {total_time} seconds ({sample_count} samples)")
        print(f"  Output CSV File   : {out_file.resolve()}")
        if init_rss is not None:
            growth = round(peak_rss - init_rss, 2)
            print(f"  Initial RAM (RSS) : {init_rss:.1f} MB")
            print(f"  Peak RAM (RSS)    : {peak_rss:.1f} MB (Delta: +{growth:.1f} MB)")
            print(f"  Peak CPU Usage    : {peak_cpu:.1f}%")
        print("=" * 70 + "\n")


def cmd_compare(args):
    file1 = Path(args.file1)
    file2 = Path(args.file2)

    if not file1.exists() or not file2.exists():
        print(f"\033[1;31m[ERROR]\033[0m Both files must exist: '{file1}' and '{file2}'")
        sys.exit(1)

    def parse_csv(path: Path) -> Dict[str, Any]:
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        if not rows:
            return {}

        mode = rows[0].get("mode", "Unknown")
        samples = len(rows)
        duration_s = float(rows[-1].get("elapsed_s", 0))

        rss_vals = [float(r["rss_mb"]) for r in rows if float(r.get("rss_mb", 0)) > 0]
        cpu_vals = [float(r["cpu_percent"]) for r in rows]
        calls_vals = [int(r["total_calls"]) for r in rows]
        ttfb_vals = [float(r["avg_ttfb_ms"]) for r in rows if float(r.get("avg_ttfb_ms", 0)) > 0]
        rtt_vals = [float(r["avg_rtt_ms"]) for r in rows if float(r.get("avg_rtt_ms", 0)) > 0]
        threads_vals = [int(r.get("threads", 1)) for r in rows]
        fds_vals = [int(r.get("fds", 0)) for r in rows]

        init_rss = rss_vals[0] if rss_vals else 0.0
        peak_rss = max(rss_vals) if rss_vals else 0.0
        avg_rss = round(sum(rss_vals) / len(rss_vals), 2) if rss_vals else 0.0
        avg_cpu = round(sum(cpu_vals) / len(cpu_vals), 2) if cpu_vals else 0.0
        peak_cpu = max(cpu_vals) if cpu_vals else 0.0

        growth_mb = round(peak_rss - init_rss, 2)
        growth_per_hr = round((growth_mb / (duration_s / 3600.0)), 2) if duration_s > 60 else 0.0

        total_calls = max(calls_vals) if calls_vals else 0
        avg_ttfb = round(sum(ttfb_vals) / len(ttfb_vals), 2) if ttfb_vals else 0.0
        avg_rtt = round(sum(rtt_vals) / len(rtt_vals), 2) if rtt_vals else 0.0
        max_threads = max(threads_vals) if threads_vals else 1
        max_fds = max(fds_vals) if fds_vals else 0

        return {
            "path": path.name,
            "mode": mode,
            "duration_s": duration_s,
            "samples": samples,
            "init_rss": init_rss,
            "peak_rss": peak_rss,
            "avg_rss": avg_rss,
            "growth_mb": growth_mb,
            "growth_per_hr": growth_per_hr,
            "avg_cpu": avg_cpu,
            "peak_cpu": peak_cpu,
            "total_calls": total_calls,
            "avg_ttfb": avg_ttfb,
            "avg_rtt": avg_rtt,
            "threads": max_threads,
            "fds": max_fds,
        }

    s1 = parse_csv(file1)
    s2 = parse_csv(file2)

    print("=" * 80)
    print("           LLM Telemetry Proxy — Benchmark Comparison Report")
    print("=" * 80)
    print(f"{'Metric':<30} | {s1['mode'][:22]:<22} | {s2['mode'][:22]:<22}")
    print("-" * 80)
    print(f"{'Source File':<30} | {s1['path']:<22} | {s2['path']:<22}")
    print(f"{'Sample Duration':<30} | {int(s1['duration_s'])}s ({s1['samples']} samples)  | {int(s2['duration_s'])}s ({s2['samples']} samples)")
    print(f"{'Total Requests Processed':<30} | {s1['total_calls']:<22} | {s2['total_calls']:<22}")
    print("-" * 80)
    print(f"{'Initial RAM (RSS)':<30} | {s1['init_rss']} MB{'':<14} | {s2['init_rss']} MB")
    print(f"{'Peak RAM (RSS)':<30} | {s1['peak_rss']} MB{'':<14} | {s2['peak_rss']} MB")
    print(f"{'Average RAM (RSS)':<30} | {s1['avg_rss']} MB{'':<14} | {s2['avg_rss']} MB")

    growth_col1 = f"+{s1['growth_mb']} MB" if s1['growth_mb'] >= 0 else f"{s1['growth_mb']} MB"
    growth_col2 = f"+{s2['growth_mb']} MB" if s2['growth_mb'] >= 0 else f"{s2['growth_mb']} MB"
    print(f"{'Memory Growth (Delta)':<30} | {growth_col1:<22} | {growth_col2:<22}")
    print(f"{'Estimated Creep Rate':<30} | {s1['growth_per_hr']} MB/hour{'':<10} | {s2['growth_per_hr']} MB/hour")
    print("-" * 80)
    print(f"{'Average CPU %':<30} | {s1['avg_cpu']}%{'':<17} | {s2['avg_cpu']}%")
    print(f"{'Peak CPU %':<30} | {s1['peak_cpu']}%{'':<17} | {s2['peak_cpu']}%")
    print(f"{'Average TTFB Latency':<30} | {s1['avg_ttfb']} ms{'':<14} | {s2['avg_ttfb']} ms")
    print(f"{'Average Total RTT':<30} | {s1['avg_rtt']} ms{'':<14} | {s2['avg_rtt']} ms")
    print(f"{'Active OS Threads':<30} | {s1['threads']:<22} | {s2['threads']:<22}")
    print(f"{'Open File Descriptors':<30} | {s1['fds']:<22} | {s2['fds']:<22}")
    print("=" * 80)

    # Efficiency calculation
    if s1["avg_rss"] > 0 and s2["avg_rss"] > 0:
        ram_diff = s1["avg_rss"] - s2["avg_rss"]
        if ram_diff > 0:
            pct = (ram_diff / s1["avg_rss"]) * 100
            print(f"👉 \033[1;32mMemory Advantage:\033[0m {s2['mode']} used {ram_diff:.1f} MB less RAM ({pct:.1f}% reduction).")
        elif ram_diff < 0:
            pct = (abs(ram_diff) / s1["avg_rss"]) * 100
            print(f"👉 \033[1;33mMemory Advantage:\033[0m {s1['mode']} used {abs(ram_diff):.1f} MB less RAM ({pct:.1f}% reduction).")

    if s1["avg_ttfb"] > 0 and s2["avg_ttfb"] > 0:
        ttfb_diff = s1["avg_ttfb"] - s2["avg_ttfb"]
        if ttfb_diff > 0:
            print(f"👉 \033[1;32mLatency Advantage:\033[0m {s2['mode']} was {ttfb_diff:.1f} ms faster to first byte.")
        elif ttfb_diff < 0:
            print(f"👉 \033[1;32mLatency Advantage:\033[0m {s1['mode']} was {abs(ttfb_diff):.1f} ms faster to first byte.")
    print("")


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark and compare Python vs Native Binary Proxy performance."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Record Subcommand
    rec = subparsers.add_parser("record", help="Record real-time memory, CPU, and proxy metrics to CSV")
    rec.add_argument("--output", "-o", type=str, default="benchmark_run.csv", help="Output CSV path")
    rec.add_argument("--duration", "-d", type=int, default=1800, help="Benchmark duration in seconds (default: 1800s / 30m)")
    rec.add_argument("--interval", "-i", type=float, default=5.0, help="Sampling interval in seconds (default: 5s)")
    rec.add_argument("--pid", type=int, default=None, help="Explicit PID (defaults to reading data/.proxy.pid)")
    rec.add_argument("--pid-file", type=str, default=str(DEFAULT_PID_FILE), help="Path to .proxy.pid file")
    rec.add_argument("--db", type=str, default=str(DEFAULT_DB_FILE), help="Path to SQLite database")

    # Compare Subcommand
    cmp = subparsers.add_parser("compare", help="Compare two benchmark CSV files side-by-side")
    cmp.add_argument("file1", type=str, help="First benchmark CSV (e.g. python_run.csv)")
    cmp.add_argument("file2", type=str, help="Second benchmark CSV (e.g. native_run.csv)")

    args = parser.parse_args()

    if args.command == "record":
        cmd_record(args)
    elif args.command == "compare":
        cmd_compare(args)


if __name__ == "__main__":
    main()
