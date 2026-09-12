# Maintenance & Operations

This document covers operational best practices for maintaining the LLM Telemetry Proxy and Dashboard in production.

---

## 🗄 Database Compaction (`db_compress.py`)

As millions of LLM requests are processed, the SQLite database grows. The built-in database compressor aggregates historical calls older than 14 days into 2-week summary buckets.

### Manual CLI Execution

```bash
# 1. Preview changes without modifying data
python proxy/db_compress.py --dry-run

# 2. Execute database compression
python proxy/db_compress.py
```

### Triggering via Dashboard API

You can also trigger database compression from the Dashboard UI or via HTTP:

```bash
curl -X POST http://localhost:9118/api/db/compress
```

### Automation via Cron

To automate database compaction every Sunday at 3:00 AM:

```bash
0 3 * * 0 /path/to/llm-proxy/.venv/bin/python /path/to/llm-proxy/proxy/db_compress.py >> /path/to/llm-proxy/data/db_compress.log 2>&1
```

---

## 💲 Model Cost Synchronization (`update_model_costs.py`)

Pricing for LLM models frequently drops across competitive providers. To keep cost metrics accurate over time without manual JSON editing:

```bash
python dashboard/update_model_costs.py
```

- Fetches latest pricing from [LiteLLM Pricing Dataset](https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json).
- Checks if the new price differs from the current active tier.
- If changed, creates a new entry with the current `effective_date` while preserving past historical tiers.
- Does **not** overwrite past dates, ensuring past telemetry calculations remain accurate.

---

## 🧹 Log Rotation & Maintenance

Runtime logs and process metadata are stored in `data/`:

| Path | Purpose |
| :--- | :--- |
| `data/proxy.log` | Standard output and error logs from the proxy gateway. |
| `data/dashboard.log` | Standard output and error logs from the dashboard server. |
| `logger/payloads.jsonl` | Raw request/response payloads (when raw logging is active). |
| `data/.proxy.pid` | Process ID of the active proxy process. |
| `data/.dashboard.pid` | Process ID of the active dashboard process. |

### Clearing Logs

To clear logs safely without stopping running processes:

```bash
# Clear proxy logs
curl -X POST http://localhost:9118/api/proxy/clear-logs

# Clear raw inspector payloads
curl -X POST http://localhost:9118/api/raw-log/clear
```

---

## 🔄 Service Lifecycle Management

Use `dashboard/dashboard.sh` or `start.sh` for reliable service control:

```bash
# Graceful restart with port configuration
./start.sh restart 9118

# Stop all processes (including proxy)
./start.sh stop --all
```

---

## ⚡ 24/7 High-Performance Server Deployment

For servers running the proxy permanently, maximum throughput, minimal CPU usage, and stable memory are achieved via runtime accelerators and anti-fragmentation memory allocators.

### 1. Server Prerequisites (Debian/Ubuntu)

```bash
# Install jemalloc for anti-fragmentation memory allocation (one-time sudo)
sudo apt-get update && sudo apt-get install -y libjemalloc2
```

### 2. Install Accelerators

In your Python virtual environment on the server:

```bash
pip install aiohttp orjson uvloop
# or: pip install -r requirements.txt
```

This installs `orjson` (SIMD Rust JSON parser) and `uvloop` (C/libuv event loop). Both are auto-detected at startup.

### 3. Start & Supervise

```bash
./dashboard.sh start --with-proxy
```

`dashboard.sh` automatically:
- Detects `libjemalloc.so.2` and preloads it via `LD_PRELOAD` to permanently prevent heap memory fragmentation.
- Activates `gc.freeze()` at startup to eliminate cyclic garbage collector overhead.
- Falls back gracefully if accelerators are not installed.

### 4. Resource Profiling Tool (`benchmark_monitor.py`)

A standalone profiler is available in `scripts/benchmark_monitor.py` to measure real-world performance:

```bash
# Profile current running proxy for 30 minutes (1800s)
python scripts/benchmark_monitor.py record --output baseline.csv --duration 1800
```

### 5. Experimental: Native Binary Compilation (Nuitka)

An experimental Nuitka compilation pipeline is available for environments that require distributing a single binary without a Python interpreter.

> **⚠️ Not recommended for memory-sensitive deployments.** Benchmarking shows the compiled binary uses
> significantly more RAM (~85–145 MB vs ~70 MB) and exhibits higher memory creep (~128 MB/hour vs ~5 MB/hour)
> than the Python script with accelerators. The proxy is I/O-bound; its CPU-intensive hot paths (`orjson`,
> `uvloop`) are already native C/Rust extensions, so compilation provides no measurable speed benefit.

```bash
# Prerequisites for building
sudo apt-get install -y gcc python3-dev patchelf ccache
pip install nuitka

# Build (optional)
./dashboard.sh build
```



