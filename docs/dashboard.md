# Analytics Dashboard

The **Telemetry Dashboard** (`http://localhost:9118`) is an interactive web application that provides real-time visibility into LLM usage, performance benchmarks, and cost breakdown.

---

## 🖥 Key Views & Capabilities

### 1. Unified Control Plane
Located at the top of the dashboard, the control header allows quick filtering by:
- **Time Window**: 1 Hour, 6 Hours, 24 Hours, 7 Days, 30 Days, or All Time.
- **Model Filter**: Multi-select filter for individual models.
- **Call Type Filter**: Filter between `chat`, `embedding`, `rerank`, and `props`.
- **Live Sync Toggle**: Automatically polls server load and telemetry updates every 10 seconds.

---

### 2. Metric Summary Cards
Displays high-level KPI aggregations for the selected time window:
- **Total Invocations**: Total call volume and error rates.
- **Total Input / Output Tokens**: Aggregated token consumption.
- **Average Time-To-First-Byte (TTFB)**: Response latency before first token emission.
- **Average Generation Speed**: Tokens generated per second (tok/s).
- **Estimated Total Cost**: Accurate USD pricing based on date-aware model pricing tiers.

---

### 3. Latency & Token Usage Time Series
Interactive Chart.js visualizations:
- **Latency Distribution Over Time**: Visualizes average TTFB vs total RTT with selectable zoom intervals.
- **Token Volume Distribution**: Breaks down prompt input tokens vs generation output tokens over time.
- **Server Load Correlation**: Plots inference latency against upstream queue depth to pinpoint whether latency spikes are caused by model size or server saturation.

---

### 4. Dynamic Model Duel (Head-to-Head Comparison)
Compares the two most frequently invoked models across:
- TTFB Speedup percentage.
- Generation throughput (tok/s) delta.
- Cost efficiency per 1K output tokens.
- Failure/Error frequency comparison.

---

### 5. Multi-Tier Cost Analyzer
The Cost Analyzer evaluates token economics:
- **Date-Aware Tier Matching**: Accurately computes costs even when providers cut pricing mid-month (e.g. comparing calls before and after price cuts).
- **LiteLLM Auto-Sync**: Automatically syncs the latest industry pricing from the [LiteLLM Pricing Dataset](https://github.com/BerriAI/litellm) with one click.
- **Cost Table Breakdown**: Model-by-model cost breakdown with input, output, and total spending.

---

### 6. Standalone Raw Payload Inspector
Access via **`http://localhost:9118/inspector`** or `/raw-logs`:
- Live SSE stream of raw request payloads and response completions.
- Formatted Markdown rendering of user prompts and system instructions.
- Dedicated reasoning trace tab for DeepSeek / o1 / thinking models.
- Structured view of function / tool call arguments.
- Filter by model, status code, or keyword search.
