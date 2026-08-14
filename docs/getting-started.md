# Getting Started

This guide walks you through setting up, configuring, and connecting your applications to the LLM Telemetry Proxy.

---

## 📋 Prerequisites

- **Python**: Version 3.10 or higher.
- **Operating System**: Linux, macOS, or Windows.
- **Upstream API Key**: API key for your target LLM provider (OpenAI, e-INFRA, OpenRouter, Together AI, etc.).

---

## 📦 Installation

Clone the repository and set up a virtual environment:

=== "Linux / macOS"

    ```bash
    git clone https://github.com/your-org/llm-proxy.git
    cd llm-proxy

    python3 -m venv .venv
    source .venv/bin/activate

    pip install -r requirements.txt
    ```

=== "Windows (PowerShell)"

    ```powershell
    git clone https://github.com/your-org/llm-proxy.git
    cd llm-proxy

    python -m venv .venv
    .venv\Scripts\Activate.ps1

    pip install -r requirements.txt
    ```

---

## 🚀 Running the Services

### Unified Service Launcher (`start.sh`)

The repository includes a control script `start.sh` (which proxies to `dashboard/dashboard.sh`) to start, stop, restart, and monitor services.

```bash
# Start both Dashboard (:9118) and Proxy Gateway (:9090)
./start.sh start --with-proxy

# View service health and listening PIDs
./start.sh status

# View the Dashboard web URL
./start.sh url

# View recent Proxy logs
./start.sh proxy logs 50

# Stop all running services
./start.sh stop --all
```

---

### Manual Process Execution

You can run the proxy gateway and dashboard server independently:

#### 1. Start the Proxy Gateway

```bash
python proxy/llm_telemetry_proxy.py \
  --port 9090 \
  --host 0.0.0.0 \
  --upstream https://api.openai.com/v1
```

| Parameter | Default | Description |
| :--- | :--- | :--- |
| `--port` | `9090` | TCP port the proxy listens on. |
| `--host` | `0.0.0.0` | Bind host address. |
| `--upstream` | `https://llm.ai.e-infra.cz/v1` | Upstream OpenAI-compatible API base URL. |
| `--db` | `data/llm_telemetry.db` | Target SQLite database path. |
| `--pid-file` | `data/.proxy.pid` | Process ID tracking file. |

#### 2. Start the Dashboard Server

```bash
python dashboard/server.py --port 9118
```

Navigate to **`http://localhost:9118`** in your browser.

---

## 🔌 Client Integration

Redirect any standard OpenAI-compatible client library to route requests through the proxy gateway at `http://localhost:9090/v1`.

=== "Python (OpenAI SDK)"

    ```python
    from openai import OpenAI

    client = OpenAI(
        base_url="http://localhost:9090/v1",
        api_key="your-upstream-api-key"
    )

    response = client.chat.completions.create(
        model="deepseek-v4-flash",
        messages=[
            {"role": "system", "content": "You are a helpful coding assistant."},
            {"role": "user", "content": "Write a quicksort implementation in Python."}
        ],
        stream=True
    )

    for chunk in response:
        delta = chunk.choices[0].delta.content or ""
        print(delta, end="")
    ```

=== "TypeScript / Node.js"

    ```typescript
    import OpenAI from "openai";

    const openai = new OpenAI({
      baseURL: "http://localhost:9090/v1",
      apiKey: "your-upstream-api-key"
    });

    async function main() {
      const completion = await openai.chat.completions.create({
        messages: [{ role: "user", content: "Hello from TypeScript!" }],
        model: "glm-5.2",
      });
      console.log(completion.choices[0].message.content);
    }

    main();
    ```

=== "LangChain (Python)"

    ```python
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(
        model="deepseek-v4-pro",
        openai_api_base="http://localhost:9090/v1",
        openai_api_key="your-upstream-api-key"
    )

    response = llm.invoke("What are the key architectural tenets of event-driven systems?")
    print(response.content)
    ```

=== "cURL"

    ```bash
    curl http://localhost:9090/v1/chat/completions \
      -H "Content-Type: application/json" \
      -H "Authorization: Bearer your-upstream-api-key" \
      -d '{
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "Hello!"}],
        "stream": false
      }'
    ```

---

## 🧪 Verifying Instrumentation

After sending a test request:

1. Open `http://localhost:9118` to see the new call reflected in the real-time summary cards and latency charts.
2. Or query SQLite directly using the CLI helper:
   ```bash
   python proxy/llm_telemetry_query.py --recent 1
   ```
