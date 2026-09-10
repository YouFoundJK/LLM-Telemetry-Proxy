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

## ⚡ 24/7 High-Performance Server Deployment & Native Binary Pre-Compilation

For servers running the proxy permanently, maximum throughput, minimal CPU usage, and zero memory creep are achieved via native machine-code compilation (`Nuitka`) and anti-fragmentation memory allocators (`jemalloc`).

### 1. Server Prerequisites (Debian/Ubuntu)

```bash
# Install GCC compiler, Python C-headers, jemalloc, and patchelf (one-time sudo)
sudo apt-get update && sudo apt-get install -y gcc python3-dev libjemalloc2 patchelf
```

### 2. Install Accelerators

In your Python virtual environment on the server:

```bash
pip install -r requirements.txt
```

This installs `orjson` (SIMD Rust JSON parser), `uvloop` (C/libuv event loop), and `nuitka` (native C compiler).

### 3. Compile Standalone Native Binary

Compile the proxy directly on the server to ensure exact matching with the host's `glibc` and CPU architecture:

```bash
./dashboard.sh build
# or: python scripts/build_binaries.py --target proxy
```

The compiled binary will be placed at `dist/llm_telemetry_proxy.bin` (standalone ELF executable with Link-Time Optimization).

### 4. Start & Supervise

```bash
./dashboard.sh start --with-proxy
```

`dashboard.sh` and `ProxyManager` automatically:
- Detect the pre-compiled binary in `dist/` and launch it directly.
- Detect `libjemalloc.so.2` and preload it via `LD_PRELOAD` to permanently prevent heap memory fragmentation.
- Activate `gc.freeze()` at startup to eliminate cyclic garbage collector overhead.
- Fall back gracefully to the Python script with runtime accelerators if the binary is absent.

### 5. Resource Profiling & Comparison Tool (`benchmark_monitor.py`)

A standalone profiler is available in `scripts/benchmark_monitor.py` to measure and compare real-world performance:

```bash
# 1. Profile current running proxy for 30 minutes (1800s)
python scripts/benchmark_monitor.py record --output python_run.csv --duration 1800

# 2. Restart proxy with native binary
./dashboard.sh proxy restart

# 3. Profile native binary for 30 minutes
python scripts/benchmark_monitor.py record --output native_run.csv --duration 1800

# 4. Generate side-by-side comparison report
python scripts/benchmark_monitor.py compare python_run.csv native_run.csv
```


