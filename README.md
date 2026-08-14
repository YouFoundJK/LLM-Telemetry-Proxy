<div align="center">

# ⚡ LLM Telemetry Proxy & Dashboard

**A lightweight, zero-intrusive reverse proxy and full control dashboard for your LLM workflows.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=flat-square)](LICENSE)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Dashboard: Built-in](https://img.shields.io/badge/Dashboard-Web%20UI-emerald?style=flat-square)](http://localhost:9118)
[![Docs: MkDocs](https://img.shields.io/badge/Docs-Material-purple?style=flat-square)](docs/)

<br/>

<p align="center">
  Intercept, monitor, and optimize LLM API calls in real time.<br/>
  <b>Zero code changes to your prompts</b> • <b>100% manageable from the web UI</b> • <b>Easy setup</b>
</p>

<p align="center">
  <img src="docs/assets/cover-image.png" alt="LLM Telemetry Dashboard Preview" width="85%" style="border-radius: 8px; box-shadow: 0 4px 20px rgba(0,0,0,0.15);" />
</p>

</div>

---

## 💡 What is this?

**LLM Telemetry Proxy** is a lightweight local telemetry server that sits transparently between your client applications and upstream LLM providers. It captures detailed performance metrics, latency breakdowns (TTFB, total RTT, tokens/sec), and token usage without modifying or slowing down your requests.

Setup is as simple as pointing your API calls to this local proxy (`http://localhost:9090/v1`) and managing your upstream provider directly from the dashboard web UI.


<table>
  <tr>
    <td width="50%">
      <h3>🎯 Zero-Config Telemetry</h3>
      <p>Passively measures Time-to-First-Byte (TTFB), total round-trip latency, generation speed (tokens/sec), and token counts without slowing down or modifying your API calls.</p>
    </td>
    <td width="50%">
      <h3>🎛 100% Web-Managed Control</h3>
      <p>Start, stop, and configure proxy ports, upstreams, database cleanup, price updates, and rate limits directly from your browser—no terminal required.</p>
    </td>
  </tr>
  <tr>
    <td width="50%">
      <h3>🛡 Token Budget & Rate Guard</h3>
      <p>Enforces a 24-hour rolling token budget to prevent runaway agent loops and queues concurrent requests to avoid upstream <code>429 Too Many Requests</code> errors.</p>
    </td>
    <td width="50%">
      <h3>🔍 Live Payload Inspector</h3>
      <p>Inspect real-time prompt schemas, tool calls, model reasoning tokens, and completions in a dedicated live streaming view with automatic credential masking.</p>
    </td>
  </tr>
</table>

---

## 🚀 Quick Start in 3 Steps

### 1. Install Dependencies

```bash
git clone https://github.com/your-org/llm-proxy.git
cd llm-proxy

# Set up virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install requirements
pip install -r requirements.txt
```

### 2. Start the Dashboard & Proxy

Launch the entire suite with a single command:

```bash
./start.sh start --with-proxy
```

> Open **[`http://localhost:9118`](http://localhost:9118)** in your browser. Check the control panel to update your upstream URL or restart the proxy if necessary. You're ready to go!

---

### 3. Point Your LLM Agents to the Proxy

Point your client's base URL to `http://localhost:9090/v1` and pass your regular API key:

#### Python (OpenAI SDK / Agent Frameworks)
```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:9090/v1",
    api_key="your-api-key"
)

response = client.chat.completions.create(
    model="deepseek-v4-flash",
    messages=[{"role": "user", "content": "Hello world!"}],
    stream=True
)

for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

#### Environment Variable (Works with most CLI & Agent tools)
```bash
export OPENAI_BASE_URL="http://localhost:9090/v1"
export OPENAI_API_KEY="your-api-key"
```

#### cURL
```bash
curl http://localhost:9090/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key" \
  -d '{
    "model": "deepseek-v4-flash",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

---

## 🎛 All Managed from the Dashboard

You do not need to memorize CLI flags. Once the dashboard at **`http://localhost:9118`** is open, you can:

- **Start / Stop / Restart the Proxy Gateway** with custom ports or upstream URLs.
- **Inspect Live Payloads & Reasoning Tokens** with the built-in real-time inspector.
- **Track Spending & Costs** with date-aware pricing tiers and 1-click price auto-sync.
- **Monitor Token Budgets** across a rolling 24-hour supervisor window.
- **Run Database Maintenance & Compression** in one click to reclaim disk space.

---

## 📚 Detailed Documentation

Looking for advanced architecture details, SQLite schemas, REST API specs, or CLI commands? 

Check out the full **MkDocs Material** documentation site:

```bash
mkdocs serve
```
Then visit **`http://127.0.0.1:8000`** in your browser.

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
