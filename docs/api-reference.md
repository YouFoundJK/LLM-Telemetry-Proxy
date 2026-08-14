# API Reference

Complete reference for all HTTP and SSE endpoints provided by the **LLM Telemetry Proxy** (`:9090`) and the **Dashboard Server** (`:9118`).

---

## ⚡ Proxy Endpoints (Port `9090`)

### Inference Gateway
| Method | Path | Description |
| :--- | :--- | :--- |
| `POST` | `/v1/chat/completions` | Proxies OpenAI chat completion requests with stream timing. |
| `POST` | `/v1/completions` | Proxies legacy text completions. |
| `POST` | `/v1/embeddings` | Proxies text embeddings. |
| `GET` | `/v1/models` | Lists upstream available models (passed through). |
| `GET` | `/v1/models/{model}` | Fetches model metadata. |

---

### Proxy Health & Token Budget
#### `GET /health`
Returns proxy health status, semaphore utilization, rolling 24-hour token budget, and upstream server status.

**Example Response**:
```json
{
  "status": "ok",
  "upstream": "https://llm.ai.e-infra.cz/v1",
  "db": "/path/to/data/llm_telemetry.db",
  "rate_limiter": {
    "max_concurrent": 4,
    "active": 1,
    "queued": 0
  },
  "token_budget": {
    "daily_limit": 480000000,
    "total_used": 1450230,
    "remaining": 478549770,
    "percentage_used": 0.3
  }
}
```

---

### Raw Payload Inspector Endpoints
| Method | Path | Description |
| :--- | :--- | :--- |
| `GET` | `/v1/raw-log/status` | Returns whether raw payload logging is enabled and file size. |
| `POST` | `/v1/raw-log/toggle` | Toggles raw logging ON/OFF. Body: `{"enabled": true}`. |
| `GET` | `/v1/raw-log/recent` | Returns the last N raw payload JSON records. Query: `?limit=50`. |
| `POST` | `/v1/raw-log/clear` | Truncates the `logger/payloads.jsonl` file. |
| `GET` | `/v1/raw-log/stream` | Server-Sent Events (SSE) live feed of raw incoming/outgoing payloads. |

---

## 📊 Dashboard API Endpoints (Port `9118`)

### Telemetry Query API
#### `GET /api/query` (or `/api/stats`)
Executes parameterized aggregations against the telemetry SQLite database.

**Query Parameters**:
- `from` *(string, ISO-8601)*: Start timestamp filter.
- `to` *(string, ISO-8601)*: End timestamp filter.
- `model` *(string, repeatable)*: Filter by one or more model names.
- `call_type` *(string, repeatable)*: `chat`, `embedding`, `rerank`, `props`.
- `group_by` *(string)*: `model`, `hour`, `day`, `call_type`.
- `errors_only` *(boolean)*: `true` to filter only failed requests.
- `limit` *(integer)*: Maximum records when returning raw call lists (default `1000`).

---

### Cost & Model Configuration
#### `GET /api/costs`
Returns the active `data/model_costs.json` pricing tier configuration.

#### `POST /api/costs/sync`
Triggers an automated background pull from the LiteLLM pricing dataset, updating `data/model_costs.json` with dynamic tiers.

---

### Process Lifecycle Control
| Method | Path | Description |
| :--- | :--- | :--- |
| `GET` | `/api/proxy/status` | Returns running state, PID, port, and health check of the proxy gateway. |
| `POST` | `/api/proxy/start` | Spawns the proxy gateway as a background process. |
| `POST` | `/api/proxy/stop` | Terminates the running proxy process. |
| `POST` | `/api/proxy/restart` | Gracefully restarts the proxy process. |
| `GET` | `/api/proxy/logs` | Reads recent stdout/stderr output lines from `data/proxy.log`. |
| `POST` | `/api/proxy/clear-logs` | Clears `data/proxy.log`. |
| `POST` | `/api/db/compress` | Triggers the database compression script `proxy/db_compress.py`. |
