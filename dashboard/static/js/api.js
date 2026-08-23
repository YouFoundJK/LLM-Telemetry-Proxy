/**
 * Telemetry Dashboard API Wrapper
 * Communicates with the telemetry backend server.
 */

const TelemetryAPI = (() => {
  // If double-clicked as a file:/// URL, direct requests to localhost:9118.
  // Otherwise, derive the base path from the current URL so the dashboard
  // works behind a reverse proxy at any sub-path (e.g. /llm/).
  const _detectBaseUrl = () => {
    if (typeof window === 'undefined') return 'http://127.0.0.1:9118';
    if (window.location && window.location.protocol === 'file:') return 'http://127.0.0.1:9118';
    // Extract the directory portion of the path (everything up to and including the last /)
    const path = (window.location && window.location.pathname) || '';
    const lastSlash = path.lastIndexOf('/');
    return lastSlash > 0 ? path.substring(0, lastSlash) : '';
  };
  const BASE_URL = _detectBaseUrl();

  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

  // Concurrency Gate: Restricts concurrent in-flight requests to prevent reverse proxy burst 429 errors
  const MAX_CONCURRENT_REQUESTS = 3;
  let _activeRequests = 0;
  const _requestQueue = [];

  // Global Rate-Limit (429) Circuit Breaker
  let _rateLimitUntil = 0;
  let _consecutive429s = 0;

  function isRateLimited() {
    return Date.now() < _rateLimitUntil;
  }

  function _acquireSlot() {
    if (_activeRequests < MAX_CONCURRENT_REQUESTS) {
      _activeRequests++;
      return Promise.resolve();
    }
    return new Promise(resolve => _requestQueue.push(resolve));
  }

  function _releaseSlot() {
    _activeRequests = Math.max(0, _activeRequests - 1);
    if (_requestQueue.length > 0 && _activeRequests < MAX_CONCURRENT_REQUESTS) {
      _activeRequests++;
      const next = _requestQueue.shift();
      setTimeout(next, 20);
    }
  }

  /**
   * Helper to format fetch errors.
   */
  async function handleResponse(response) {
    if (!response || !response.ok) {
      if (!response) {
        throw new Error('No response received (request may have been throttled)');
      }
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
   * Robust fetch wrapper with automatic rate-limit (429) circuit breaking and transient error backoff.
   */
  async function fetchWithRetry(url, options = {}, maxRetries = null) {
    const isBackground = Boolean(options.isBackground);
    // Background polling requests should not compound 429s with retries
    const retries = maxRetries !== null ? maxRetries : (isBackground ? 0 : 2);

    // If global rate limit is active, skip background requests immediately without touching network
    if (isBackground && isRateLimited()) {
      const waitRemaining = Math.max(0, Math.round((_rateLimitUntil - Date.now()) / 1000));
      const throttledErr = new Error(`Rate limit active (${waitRemaining}s remaining). Background poll skipped.`);
      throttledErr.status = 429;
      throttledErr.isThrottled = true;
      throw throttledErr;
    }

    await _acquireSlot();
    let attempt = 0;
    let delay = 1000;

    try {
      while (true) {
        try {
          const response = await fetch(url, options);

          if (response.status === 429) {
            _consecutive429s++;
            const retryAfter = response.headers.get('Retry-After');
            let backoffMs = Math.min(30000, 3000 * Math.pow(1.5, Math.min(_consecutive429s - 1, 4)));
            if (retryAfter) {
              const parsedSec = parseFloat(retryAfter);
              if (!isNaN(parsedSec) && parsedSec > 0) {
                backoffMs = Math.max(parsedSec * 1000, backoffMs);
              }
            }
            _rateLimitUntil = Date.now() + backoffMs;

            if (attempt < retries) {
              console.warn(`[TelemetryAPI] Received HTTP 429 from ${url}. Pausing & backing off ${Math.round(backoffMs)}ms (attempt ${attempt + 1}/${retries})...`);
              // Release slot during sleep to prevent stalling other non-retry tasks
              _releaseSlot();
              await sleep(backoffMs);
              await _acquireSlot();
              attempt++;
              continue;
            } else {
              console.warn(`[TelemetryAPI] Rate limit (429) active from ${url}. Pausing background requests for ${Math.round(backoffMs / 1000)}s.`);
            }
          } else if (response.status === 503 && attempt < retries) {
            let waitMs = delay + Math.random() * 300;
            console.warn(`[TelemetryAPI] Received HTTP 503 from ${url}. Backing off ${Math.round(waitMs)}ms (attempt ${attempt + 1}/${retries})...`);
            _releaseSlot();
            await sleep(waitMs);
            await _acquireSlot();
            delay *= 2;
            attempt++;
            continue;
          } else if (response.ok) {
            // Reset consecutive 429 counter on successful response
            _consecutive429s = 0;
          }

          return response;
        } catch (err) {
          if (err.name === 'AbortError') {
            throw err;
          }
          if (attempt < retries) {
            const waitMs = delay + Math.random() * 300;
            console.warn(`[TelemetryAPI] Network fetch error (${err.message}) on ${url}. Retrying in ${Math.round(waitMs)}ms...`);
            _releaseSlot();
            await sleep(waitMs);
            await _acquireSlot();
            delay *= 2;
            attempt++;
            continue;
          }
          throw err;
        }
      }
    } finally {
      _releaseSlot();
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
  async function getServerStatus(options = {}) {
    const url = `${BASE_URL}/api/server-status`;
    const response = await fetchWithRetry(url, { isBackground: true, ...options });
    return handleResponse(response);
  }

  /**
   * GET /api/costs — retrieves pricing structures.
   */
  async function getCosts(options = {}) {
    const url = `${BASE_URL}/api/costs`;
    const response = await fetchWithRetry(url, options);
    return handleResponse(response);
  }

  /**
   * GET /api/model-mapping — retrieves alias to canonical model mapping configuration.
   */
  async function getModelMapping(options = {}) {
    const url = `${BASE_URL}/api/model-mapping`;
    const response = await fetchWithRetry(url, options);
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
  async function getHealth(options = {}) {
    const url = `${BASE_URL}/health`;
    const response = await fetchWithRetry(url, { isBackground: true, ...options });
    return handleResponse(response);
  }

  /**
   * GET /api/proxy/status — retrieves proxy gateway live state and health.
   */
  async function getProxyStatus(port, options = {}) {
    const url = port ? `${BASE_URL}/api/proxy/status?port=${encodeURIComponent(port)}` : `${BASE_URL}/api/proxy/status`;
    const response = await fetchWithRetry(url, { isBackground: true, ...options });
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
  async function getProxyLogs(lines = 150, options = {}) {
    const url = `${BASE_URL}/api/proxy/logs?lines=${lines}`;
    const response = await fetchWithRetry(url, { isBackground: true, ...options });
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
   * GET /api/proxy/routes — retrieves model routing configuration with masked API keys.
   */
  async function getProxyRoutes(options = {}) {
    const url = `${BASE_URL}/api/proxy/routes`;
    const response = await fetchWithRetry(url, options);
    return handleResponse(response);
  }

  /**
   * POST /api/proxy/routes — updates model routing configuration persistently.
   */
  async function saveProxyRoutes(data) {
    const url = `${BASE_URL}/api/proxy/routes`;
    const response = await fetchWithRetry(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    return handleResponse(response);
  }

  /**
   * POST /api/proxy/routes/test — tests route resolution against a model string.
   */
  async function testProxyRoute(modelName) {
    const url = `${BASE_URL}/api/proxy/routes/test`;
    const response = await fetchWithRetry(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: modelName }),
    });
    return handleResponse(response);
  }

  /**
   * GET /api/raw-log/status — retrieves raw payload logging status & file metadata.
   */
  async function getRawLogStatus(options = {}) {
    const url = `${BASE_URL}/api/raw-log/status`;
    const response = await fetchWithRetry(url, { isBackground: true, ...options });
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
  async function getRecentRawLogs(limit = 50, options = {}) {
    const url = `${BASE_URL}/api/raw-log/recent?limit=${limit}`;
    const response = await fetchWithRetry(url, options);
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
   * GET /api/control-panel/bundle — retrieves unified proxy status, health, routes, logs, and raw-log status in ONE request.
   */
  async function getControlPanelBundle(port, lines = 150, options = {}) {
    const params = new URLSearchParams();
    if (port) params.append('port', port.toString());
    if (lines) params.append('lines', lines.toString());
    if (options.includeLogs !== undefined) {
      params.append('include_logs', options.includeLogs ? '1' : '0');
    }
    const url = `${BASE_URL}/api/control-panel/bundle?${params.toString()}`;
    const response = await fetchWithRetry(url, { isBackground: true, ...options });
    return handleResponse(response);
  }

  /**
   * GET /api/dashboard/bundle — retrieves model mapping, costs, health, proxy status, and routes in ONE request.
   */
  async function getDashboardBundle(options = {}) {
    const url = `${BASE_URL}/api/dashboard/bundle`;
    const response = await fetchWithRetry(url, { isBackground: true, ...options });
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
    getModelMapping,
    syncCosts,
    getHealth,
    getProxyStatus,
    startProxy,
    stopProxy,
    restartProxy,
    getProxyLogs,
    clearProxyLogs,
    getProxyRoutes,
    saveProxyRoutes,
    testProxyRoute,
    runDbCompress,
    getRawLogStatus,
    toggleRawLog,
    getRecentRawLogs,
    clearRawLogs,
    getControlPanelBundle,
    getDashboardBundle,
    getRawLogStreamUrl,
    isRateLimited,
    BASE_URL
  };
})();

if (typeof module !== 'undefined' && module.exports) {
  module.exports = TelemetryAPI;
}


