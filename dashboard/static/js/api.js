/**
 * Telemetry Dashboard API Wrapper
 * Communicates with the telemetry backend server.
 */

const TelemetryAPI = (() => {
  // If double-clicked as a file:/// URL, direct requests to localhost:9118.
  // Otherwise, derive the base path from the current URL so the dashboard
  // works behind a reverse proxy at any sub-path (e.g. /llm/).
  const _detectBaseUrl = () => {
    if (window.location.protocol === 'file:') return 'http://127.0.0.1:9118';
    // Extract the directory portion of the path (everything up to and including the last /)
    const path = window.location.pathname;
    const lastSlash = path.lastIndexOf('/');
    return lastSlash > 0 ? path.substring(0, lastSlash) : '';
  };
  const BASE_URL = _detectBaseUrl();

  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

  /**
   * Helper to format fetch errors.
   */
  async function handleResponse(response) {
    if (!response.ok) {
      let errDetails = '';
      try {
        const errJson = await response.json();
        errDetails = errJson.error?.message || errJson.error || errJson.details || '';
      } catch (e) {
        errDetails = response.statusText;
      }
      const err = new Error(errDetails ? `${response.status}: ${errDetails}` : `HTTP Error ${response.status}`);
      err.status = response.status;
      throw err;
    }
    return await response.json();
  }

  /**
   * Robust fetch wrapper with automatic rate-limit (429) and transient error (503) retry with exponential backoff.
   */
  async function fetchWithRetry(url, options = {}, maxRetries = 3) {
    let attempt = 0;
    let delay = 350;

    while (true) {
      try {
        const response = await fetch(url, options);

        if ((response.status === 429 || response.status === 503) && attempt < maxRetries) {
          const retryAfter = response.headers.get('Retry-After');
          let waitMs = delay + Math.random() * 150;
          if (retryAfter) {
            const parsedSec = parseFloat(retryAfter);
            if (!isNaN(parsedSec)) {
              waitMs = Math.max(parsedSec * 1000, waitMs);
            }
          }
          console.warn(`[TelemetryAPI] Received HTTP ${response.status} from ${url}. Retrying in ${Math.round(waitMs)}ms (attempt ${attempt + 1}/${maxRetries})...`);
          await sleep(waitMs);
          delay *= 2;
          attempt++;
          continue;
        }

        return response;
      } catch (err) {
        if (err.name === 'AbortError') {
          throw err;
        }
        if (attempt < maxRetries) {
          const waitMs = delay + Math.random() * 150;
          console.warn(`[TelemetryAPI] Network fetch error (${err.message}) on ${url}. Retrying in ${Math.round(waitMs)}ms...`);
          await sleep(waitMs);
          delay *= 2;
          attempt++;
          continue;
        }
        throw err;
      }
    }
  }

  /**
   * GET /api/query — retrieves telemetry logs and summaries based on filters.
   */
  async function query(filters = {}, options = {}) {
    const params = new URLSearchParams();
    
    // Add models (support array or single value)
    if (filters.models) {
      const models = Array.isArray(filters.models) ? filters.models : [filters.models];
      models.forEach(m => {
        if (m) params.append('model', m);
      });
    }

    // Add call types
    if (filters.call_types) {
      const types = Array.isArray(filters.call_types) ? filters.call_types : [filters.call_types];
      types.forEach(t => {
        if (t) params.append('call_type', t);
      });
    }

    if (filters.from) params.append('from', filters.from);
    if (filters.to) params.append('to', filters.to);
    if (filters.errors_only) params.append('errors_only', '1');
    if (filters.group_by) params.append('group_by', filters.group_by);
    if (filters.limit) params.append('limit', filters.limit.toString());

    const url = `${BASE_URL}/api/query?${params.toString()}`;
    const fetchOptions = {};
    if (options.signal) {
      fetchOptions.signal = options.signal;
    }
    const response = await fetchWithRetry(url, fetchOptions);
    return handleResponse(response);
  }

  /**
   * GET /api/query/bulk — retrieves high-throughput telemetry logs in columnar matrix format.
   */
  async function queryBulk(filters = {}, options = {}) {
    const params = new URLSearchParams();
    
    if (filters.models) {
      const models = Array.isArray(filters.models) ? filters.models : [filters.models];
      models.forEach(m => {
        if (m) params.append('model', m);
      });
    }

    if (filters.call_types) {
      const types = Array.isArray(filters.call_types) ? filters.call_types : [filters.call_types];
      types.forEach(t => {
        if (t) params.append('call_type', t);
      });
    }

    if (filters.from) params.append('from', filters.from);
    if (filters.to) params.append('to', filters.to);
    if (filters.since_id) params.append('since_id', filters.since_id.toString());
    if (filters.since_ts) params.append('since_ts', filters.since_ts);
    if (filters.errors_only) params.append('errors_only', '1');
    if (filters.limit) params.append('limit', filters.limit.toString());

    const url = `${BASE_URL}/api/query/bulk?${params.toString()}`;
    const fetchOptions = {
      headers: {
        'Accept-Encoding': 'gzip, deflate, br'
      }
    };
    if (options.signal) {
      fetchOptions.signal = options.signal;
    }
    const response = await fetchWithRetry(url, fetchOptions);
    const data = await handleResponse(response);

    // Convert columnar rows matrix into call objects for seamless consumer usage
    const cols = data.columns || [];
    const rows = data.rows || [];
    const calls = new Array(rows.length);

    for (let i = 0; i < rows.length; i++) {
      const row = rows[i];
      const obj = {};
      for (let c = 0; c < cols.length; c++) {
        const val = row[c];
        if (val !== null && val !== undefined) {
          obj[cols[c]] = val;
        }
      }
      calls[i] = obj;
    }

    return {
      calls: calls,
      count: data.count || calls.length,
      db_fingerprint: data.db_fingerprint,
      available_models: data.available_models || [],
      available_types: data.available_types || []
    };
  }

  /**
   * GET /api/server-status — retrieves live server node telemetry from e-INFRA API.
   */
  async function getServerStatus() {
    const url = `${BASE_URL}/api/server-status`;
    const response = await fetchWithRetry(url);
    return handleResponse(response);
  }

  /**
   * GET /api/costs — retrieves pricing structures.
   */
  async function getCosts() {
    const url = `${BASE_URL}/api/costs`;
    const response = await fetchWithRetry(url);
    return handleResponse(response);
  }

  /**
   * POST /api/costs/sync — automatically fetches latest rates from LiteLLM and updates model_costs.json.
   */
  async function syncCosts() {
    const url = `${BASE_URL}/api/costs/sync`;
    const response = await fetchWithRetry(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' }
    });
    return handleResponse(response);
  }

  /**
   * GET /health — retrieves SQLite database size, presence, and HTML state.
   */
  async function getHealth() {
    const url = `${BASE_URL}/health`;
    const response = await fetchWithRetry(url);
    return handleResponse(response);
  }

  /**
   * GET /api/proxy/status — retrieves proxy gateway live state and health.
   */
  async function getProxyStatus(port) {
    const url = port ? `${BASE_URL}/api/proxy/status?port=${encodeURIComponent(port)}` : `${BASE_URL}/api/proxy/status`;
    const response = await fetchWithRetry(url);
    return handleResponse(response);
  }

  /**
   * POST /api/proxy/start — starts the proxy gateway process.
   */
  async function startProxy(params = {}) {
    const url = `${BASE_URL}/api/proxy/start`;
    const response = await fetchWithRetry(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
    });
    return handleResponse(response);
  }

  /**
   * POST /api/proxy/stop — stops / kills the proxy gateway process.
   */
  async function stopProxy(params = {}) {
    const url = `${BASE_URL}/api/proxy/stop`;
    const response = await fetchWithRetry(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
    });
    return handleResponse(response);
  }

  /**
   * POST /api/proxy/restart — restarts the proxy gateway process.
   */
  async function restartProxy(params = {}) {
    const url = `${BASE_URL}/api/proxy/restart`;
    const response = await fetchWithRetry(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
    });
    return handleResponse(response);
  }

  /**
   * GET /api/proxy/logs — retrieves tail of proxy log output.
   */
  async function getProxyLogs(lines = 150) {
    const url = `${BASE_URL}/api/proxy/logs?lines=${lines}`;
    const response = await fetchWithRetry(url);
    return handleResponse(response);
  }

  /**
   * POST /api/clear-logs — clears proxy log output.
   */
  async function clearProxyLogs() {
    const url = `${BASE_URL}/api/proxy/clear-logs`;
    const response = await fetchWithRetry(url, { method: 'POST' });
    return handleResponse(response);
  }

  /**
   * POST /api/db/compress — triggers historical database compression.
   */
  async function runDbCompress() {
    const url = `${BASE_URL}/api/db/compress`;
    const response = await fetchWithRetry(url, { method: 'POST' });
    return handleResponse(response);
  }

  /**
   * GET /api/raw-log/status — retrieves raw payload logging status & file metadata.
   */
  async function getRawLogStatus() {
    const url = `${BASE_URL}/api/raw-log/status`;
    const response = await fetchWithRetry(url);
    return handleResponse(response);
  }

  /**
   * POST /api/raw-log/toggle — toggle raw logging state.
   */
  async function toggleRawLog(enabled) {
    const url = `${BASE_URL}/api/raw-log/toggle`;
    const payload = enabled !== undefined ? { enabled: Boolean(enabled) } : {};
    const response = await fetchWithRetry(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    return handleResponse(response);
  }

  /**
   * GET /api/raw-log/recent — retrieve the latest N raw logged calls.
   */
  async function getRecentRawLogs(limit = 50) {
    const url = `${BASE_URL}/api/raw-log/recent?limit=${limit}`;
    const response = await fetchWithRetry(url);
    return handleResponse(response);
  }

  /**
   * POST /api/raw-log/clear — clear logger file on disk.
   */
  async function clearRawLogs() {
    const url = `${BASE_URL}/api/raw-log/clear`;
    const response = await fetchWithRetry(url, { method: 'POST' });
    return handleResponse(response);
  }

  /**
   * Returns the SSE stream URL for live raw logs.
   */
  function getRawLogStreamUrl() {
    return `${BASE_URL}/api/raw-log/stream`;
  }

  return {
    query,
    queryBulk,
    getServerStatus,
    getCosts,
    syncCosts,
    getHealth,
    getProxyStatus,
    startProxy,
    stopProxy,
    restartProxy,
    getProxyLogs,
    clearProxyLogs,
    runDbCompress,
    getRawLogStatus,
    toggleRawLog,
    getRecentRawLogs,
    clearRawLogs,
    getRawLogStreamUrl,
    BASE_URL
  };
})();


