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

  /**
   * Helper to format fetch errors.
   */
  async function handleResponse(response) {
    if (!response.ok) {
      let errDetails = '';
      try {
        const errJson = await response.json();
        errDetails = errJson.error || errJson.details || '';
      } catch (e) {
        errDetails = response.statusText;
      }
      throw new Error(errDetails ? `${response.status}: ${errDetails}` : `HTTP Error ${response.status}`);
    }
    return await response.json();
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
    const response = await fetch(url, fetchOptions);
    return handleResponse(response);
  }

  /**
   * GET /api/server-status — retrieves live server node telemetry from e-INFRA API.
   */
  async function getServerStatus() {
    const url = `${BASE_URL}/api/server-status`;
    const response = await fetch(url);
    return handleResponse(response);
  }

  /**
   * GET /api/costs — retrieves pricing structures.
   */
  async function getCosts() {
    const url = `${BASE_URL}/api/costs`;
    const response = await fetch(url);
    return handleResponse(response);
  }

  /**
   * GET /health — retrieves SQLite database size, presence, and HTML state.
   */
  async function getHealth() {
    const url = `${BASE_URL}/health`;
    const response = await fetch(url);
    return handleResponse(response);
  }

  /**
   * GET /api/proxy/status — retrieves proxy gateway live state and health.
   */
  async function getProxyStatus(port) {
    const url = port ? `${BASE_URL}/api/proxy/status?port=${encodeURIComponent(port)}` : `${BASE_URL}/api/proxy/status`;
    const response = await fetch(url);
    return handleResponse(response);
  }

  /**
   * POST /api/proxy/start — starts the proxy gateway process.
   */
  async function startProxy(params = {}) {
    const url = `${BASE_URL}/api/proxy/start`;
    const response = await fetch(url, {
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
    const response = await fetch(url, {
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
    const response = await fetch(url, {
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
    const response = await fetch(url);
    return handleResponse(response);
  }

  /**
   * POST /api/clear-logs — clears proxy log output.
   */
  async function clearProxyLogs() {
    const url = `${BASE_URL}/api/proxy/clear-logs`;
    const response = await fetch(url, { method: 'POST' });
    return handleResponse(response);
  }

  /**
   * POST /api/db/compress — triggers historical database compression.
   */
  async function runDbCompress() {
    const url = `${BASE_URL}/api/db/compress`;
    const response = await fetch(url, { method: 'POST' });
    return handleResponse(response);
  }

  /**
   * GET /api/raw-log/status — retrieves raw payload logging status & file metadata.
   */
  async function getRawLogStatus() {
    const url = `${BASE_URL}/api/raw-log/status`;
    const response = await fetch(url);
    return handleResponse(response);
  }

  /**
   * POST /api/raw-log/toggle — toggle raw logging state.
   */
  async function toggleRawLog(enabled) {
    const url = `${BASE_URL}/api/raw-log/toggle`;
    const payload = enabled !== undefined ? { enabled: Boolean(enabled) } : {};
    const response = await fetch(url, {
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
    const response = await fetch(url);
    return handleResponse(response);
  }

  /**
   * POST /api/raw-log/clear — clear logger file on disk.
   */
  async function clearRawLogs() {
    const url = `${BASE_URL}/api/raw-log/clear`;
    const response = await fetch(url, { method: 'POST' });
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
    getServerStatus,
    getCosts,
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


