# Proxy Gateway

The **LLM Telemetry Proxy** is an asynchronous reverse proxy built with Python `aiohttp`. It forwards requests transparently while calculating metrics, managing concurrency, and enforcing safety policies.

---

## ⚙ Core Capabilities

### 1. Adaptive Rate Limiting & Concurrency Control

Many upstream LLM providers (or private clusters) enforce strict concurrent request limits per API key or tenant (e.g., maximum 4 parallel requests). When client code fires parallel async queries, this often results in `429 Too Many Requests` errors.

The proxy utilizes an async semaphore to throttle in-flight calls:

```python
MAX_CONCURRENT = 4
_upstream_semaphore = asyncio.Semaphore(MAX_CONCURRENT)
```

- When all 4 slots are busy, subsequent incoming requests wait gracefully in an async queue.
- As soon as an active stream completes (or fails), waiting requests are immediately dispatched without hitting upstream rate limits.

---

### 2. Rolling 24-Hour Token Budget Supervisor

The proxy provides hard token consumption caps to prevent runaway agent loops from exhausting monthly quotas or incurring unexpected bills.

- **Default Cap**: 480,000,000 tokens / 24 hours (configurable via `DAILY_TOKEN_LIMIT`).
- **Rolling Window**: Token usage is calculated across a continuous 86,400-second window.
- **Persistent State Across Restarts**: 
  - Restores active usage from `data/llm_telemetry.db` and fallback snapshots in `data/token_budget.json`.
- **Response Headers**:
  ```http
  X-Token-Budget-Used: 12450000
  X-Token-Budget-Remaining: 467550000
  X-Token-Budget-Percentage: 2.59
  X-Token-Budget-Limit: 480000000
  ```
- **Rejection Behavior**: If the cap is reached, requests are safely rejected with HTTP 429 and a descriptive JSON payload:
  ```json
  {
    "error": {
      "type": "token_budget_exceeded",
      "message": "🚫 DAILY TOKEN BUDGET EXCEEDED\nUsed: 480,120,000 tokens (100.0% of daily limit)..."
    }
  }
  ```

---

### 3. Canonical Model Name Resolution

To avoid fragmentation in dashboards where clients pass aliases (e.g. `glm`, `glm-5`, or `coder` instead of canonical names), the proxy utilizes `data/model_mapping.json` to canonicalize names in RAM before persisting to disk.

```json
{
  "glm-5.2": ["glm", "glm-5", "glm-5.2"],
  "deepseek-v4-flash": ["deepseek", "deepseek-v4-flash"],
  "qwen3.5-122b": ["coder", "agentic", "qwen3.5-122b"]
}
```

---

### 4. Real-Time Raw Payload Logging & Privacy Sanitizer

When debugging prompts, formatting errors, tool calling schemas, or model reasoning tokens, developers can enable **Raw Payload Logging** via the dashboard or REST API:

```bash
curl -X POST http://localhost:9090/v1/raw-log/toggle -H "Content-Type: application/json" -d '{"enabled": true}'
```

#### Privacy-Preserving Header Masking
To prevent accidental credential leaks into logs, `make_raw_payload_record` automatically sanitizes authorization headers:
- `Authorization: Bearer sk-proj-...` &rarr; `Bearer sk-p...cdef`
- `api-key`, `x-api-key`, `x-auth-token` &rarr; Masked safely

#### SSE Live Streaming
The proxy offers a Server-Sent Events (SSE) feed at `/v1/raw-log/stream` that transmits structured JSON payloads to the connected Payload Inspector UI with zero polling overhead.
