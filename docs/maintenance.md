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

## ⚡ 24/7 High-Performance Server Deployment & Native C-Extension Compilation

For servers running the proxy permanently, maximum throughput, minimal CPU usage, and zero memory creep are achieved via Native C-Extension compilation (`Nuitka --module`), async accelerators (`uvloop`, `orjson`), and anti-fragmentation memory allocators (`jemalloc`).

### 1. Server Prerequisites (Debian/Ubuntu)

```bash
# Install GCC compiler, Python C-headers, and jemalloc (one-time sudo)
sudo apt-get update && sudo apt-get install -y gcc g++ python3-dev libjemalloc2 ccache
```

### 2. Install Accelerators & Compiler

In your Python virtual environment on the server:

```bash
pip install -r requirements.txt
# Installs: aiohttp, orjson (SIMD Rust JSON), uvloop (libuv), nuitka (C compiler)
```

### 3. Compile Native C-Extension Modules

Compile the performance-critical proxy modules (`model_router`, `proxy_forwarder`, `proxy_stream`, `fast_json`, `telemetry_db`, `payload_inspector`) into native `.so` shared libraries:

```bash
./start.sh build
# or: python scripts/build_binaries.py --mode modules
```

The compiled native modules (`*.so`) are generated exclusively in `dist/modules/`, keeping the source tree 100% pure `.py`. When the proxy starts in accelerated mode, `setup_native_modules_path()` loads the compiled `.so` C-extensions from `dist/modules/`.

### 4. Start & Supervise

```bash
./start.sh start --with-proxy
# or: ./dashboard.sh proxy start
```

`dashboard.sh` automatically:
- Detects the compiled native `.so` modules and loads them with native C speed.
- Detects `libjemalloc.so.2` and preloads it via `LD_PRELOAD` to permanently prevent heap memory fragmentation.
- Falls back gracefully to `.py` source files if `.so` modules are absent.

### 5. Resource Profiling & Benchmark Comparison Tool (`benchmark_monitor.py`)

A standalone profiler is available in `scripts/benchmark_monitor.py` to measure and compare real-world performance:

```bash
# Profile running proxy for 30 minutes (1800s)
python scripts/benchmark_monitor.py record --output native_modules_run.csv --duration 1800

# Compare against a baseline recording
python scripts/benchmark_monitor.py compare baseline.csv native_modules_run.csv
```



