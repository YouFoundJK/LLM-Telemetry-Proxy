/**
 * Telemetry Dashboard UI Module
 * Handles DOM rendering, formatting, and user interaction states.
 */

const UI = (() => {
  
  // Formatters
  function formatNum(n) {
    if (n === null || n === undefined) return '—';
    if (n >= 1e9) return (n/1e9).toFixed(2) + 'B';
    if (n >= 1e6) return (n/1e6).toFixed(2) + 'M';
    if (n >= 1e3) return (n/1e3).toFixed(1) + 'K';
    return n.toLocaleString('en-US');
  }

  function formatMs(ms) {
    if (ms === null || ms === undefined || ms === 0) return '—';
    if (ms >= 1000) return (ms/1000).toFixed(2) + 's';
    return ms.toFixed(0) + 'ms';
  }

  function formatTps(tps) {
    if (tps === null || tps === undefined || tps === 0 || isNaN(tps) || !isFinite(tps)) return '—';
    return tps.toFixed(1);
  }

  function getModelClass(model) {
    if (!model) return 'tag-other';
    const m = model.toLowerCase();
    if (m.includes('glm')) return 'tag-glm';
    if (m.includes('qwen')) return 'tag-qwen';
    if (m.includes('gemma')) return 'tag-gemma';
    if (m.includes('deepseek')) return 'tag-deepseek';
    if (m.includes('gpt')) return 'tag-gpt';
    return 'tag-other';
  }

  // Timezone-safe formatting
  function toLocalISOString(date) {
    const tzOffset = date.getTimezoneOffset() * 60000;
    return new Date(date.getTime() - tzOffset).toISOString().slice(0, 16);
  }

  function formatShortTime(ts) {
    const d = new Date(ts);
    return d.toLocaleTimeString([], { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  function formatShortDate(ts) {
    const d = new Date(ts);
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + 
           d.toLocaleTimeString([], { hour12: false, hour: '2-digit', minute: '2-digit' });
  }

  /**
   * Render Top Summary Stat Cards
   */
  function renderSummary(summaryData) {
    const el = document.getElementById('summaryBar');
    if (!el) return;

    const s = summaryData || {};
    const calls = s.calls || 0;
    const input = s.total_input || 0;
    const output = s.total_output || 0;
    const errors = s.errors || 0;
    const avgTtfb = s.avg_ttfb || 0;
    const avgRtt = s.avg_rtt || 0;
    const avgTps = s.avg_tps || 0;

    const errorRate = calls ? ((errors / calls) * 100) : 0;
    const avgInput = calls ? (input / calls) : 0;
    const avgOutput = calls ? (output / calls) : 0;

    const cards = [
      { 
        label: 'Total Calls', 
        value: formatNum(calls), 
        sub: errors ? `${errors} failed` : 'No errors', 
        cls: errors ? 'red' : 'green',
        tooltip: `Exact total: ${calls.toLocaleString('en-US')} calls\nErrors: ${errors.toLocaleString('en-US')} failed`
      },
      { 
        label: 'Input Tokens', 
        value: formatNum(input), 
        sub: `Avg: ${formatNum(Math.round(avgInput))} / call`, 
        cls: 'accent',
        tooltip: `Exact total: ${input.toLocaleString('en-US')} tokens\nExact average: ${avgInput.toLocaleString('en-US', {maximumFractionDigits: 2})} tokens per call`
      },
      { 
        label: 'Output Tokens', 
        value: formatNum(output), 
        sub: `Avg: ${formatNum(Math.round(avgOutput))} / call`, 
        cls: 'accent',
        tooltip: `Exact total: ${output.toLocaleString('en-US')} tokens\nExact average: ${avgOutput.toLocaleString('en-US', {maximumFractionDigits: 2})} tokens per call`
      },
      { 
        label: 'Avg TTFB', 
        value: formatMs(avgTtfb), 
        sub: 'Time to first byte', 
        cls: avgTtfb > 3000 ? 'orange' : 'green',
        tooltip: `Exact average: ${avgTtfb.toFixed(2)} ms`
      },
      { 
        label: 'Avg RTT (Latency)', 
        value: formatMs(avgRtt), 
        sub: 'Round-trip duration', 
        cls: avgRtt > 8000 ? 'red' : avgRtt > 3000 ? 'orange' : 'green',
        tooltip: `Exact average: ${avgRtt.toFixed(2)} ms`
      },
      { 
        label: 'Avg Throughput', 
        value: formatTps(avgTps), 
        sub: 'Tokens per second', 
        cls: avgTps > 40 ? 'green' : avgTps > 15 ? 'accent' : 'red',
        tooltip: `Exact average: ${avgTps.toFixed(2)} tokens/sec`
      },
      { 
        label: 'Error Rate', 
        value: errorRate.toFixed(1) + '%', 
        sub: errors ? `${errors} requests failed` : 'All requests OK', 
        cls: errorRate > 5 ? 'red' : errorRate > 0 ? 'orange' : 'green',
        tooltip: `Exact failure rate: ${(calls ? (errors / calls * 100) : 0).toFixed(4)}%\n(${errors} errors out of ${calls} calls)`
      },
    ];

    el.innerHTML = cards.map(c => `
      <div class="stat-card ${c.cls}" title="${c.tooltip || ''}">
        <div class="label">${c.label}</div>
        <div class="value ${c.cls}">${c.value}</div>
        <div class="subtext">${c.sub}</div>
      </div>
    `).join('');
  }

  /**
   * Render Live e-INFRA Model Nodes
   */
  function renderServerStatus(data, liveNodesConfig = null) {
    const el = document.getElementById('serverStatus');
    if (!el) return;

    const allModels = data.models || [];
    let models = [];

    if (liveNodesConfig && liveNodesConfig.length > 0) {
      const configMap = {};
      liveNodesConfig.forEach((n, idx) => {
        configMap[n.name] = { visible: n.visible, index: idx };
      });

      const visibleModels = allModels.filter(m => {
        const conf = configMap[m.name];
        return conf ? conf.visible : false;
      });

      visibleModels.sort((a, b) => {
        const idxA = configMap[a.name] ? configMap[a.name].index : 999;
        const idxB = configMap[b.name] ? configMap[b.name].index : 999;
        return idxA - idxB;
      });
      models = visibleModels;
    } else {
      models = allModels;
    }
    
    if (!models.length) {
      el.innerHTML = '<div class="loading-overlay" style="position:static; padding:20px; color:var(--text-dim); text-align:center;">No active online nodes visible. Click Edit to customize nodes.</div>';
      return;
    }

    el.innerHTML = models.map(m => {
      const runningVal = m.running || 0;
      const loadColor = runningVal > 8 ? 'var(--red)' : runningVal > 4 ? 'var(--orange)' : 'var(--green)';
      
      const kvPct = (m.kv_cache * 100).toFixed(0);
      const kvColor = m.kv_cache > 0.8 ? 'var(--red)' : m.kv_cache > 0.5 ? 'var(--orange)' : 'var(--green)';

      const nameColor = TelemetryCharts.getModelColor(m.name);

      return `
        <div class="server-node-card">
          <div class="node-name" style="color: ${nameColor}; border-bottom: 1px solid rgba(255,255,255,0.05); padding-bottom: 6px; margin-bottom: 8px;" title="${m.name}">
            ${m.name}
          </div>
          <div class="metric-row">
            <span class="metric-label">Running Requests</span>
            <span class="metric-val" style="color: ${loadColor}">${runningVal}</span>
          </div>
          <div class="metric-row">
            <span class="metric-label">Waiting in Queue</span>
            <span class="metric-val">${m.waiting || 0}</span>
          </div>
          <div class="metric-row">
            <span class="metric-label">Throughput</span>
            <span class="metric-val">${formatTps(m.tokens_per_s)} tok/s</span>
          </div>
          <div class="metric-row" style="margin-top: 8px;">
            <span class="metric-label">KV Cache Allocation</span>
            <span class="metric-val" style="color: ${kvColor}">${kvPct}%</span>
          </div>
          <div class="progress-bar-bg">
            <div class="progress-bar-fill" style="width: ${kvPct}%; background: ${kvColor}"></div>
          </div>
        </div>
      `;
    }).join('');
  }

  /**
   * Render Cross-Check Comparison & Warning Banner
   */
  function renderCrossCheck(data) {
    const el = document.getElementById('crossCheckSection');
    if (!el) return;

    const proxyStats = data.proxy_stats;
    const breakdown = data.proxy_breakdown || [];
    const s = data.summary;

    let html = '';

    if (proxyStats && proxyStats.total_calls !== null) {
      const loggedCalls = s ? s.calls : 0;
      const proxyTotal = proxyStats.total_calls || 0;
      const proxyErrors = proxyStats.total_errors || 0;
      const matchPctVal = proxyTotal > 0 ? ((proxyStats.logged_calls || 0) / proxyTotal * 100) : 0;
      const matchPct = proxyTotal > 0 ? matchPctVal.toFixed(1) : '0.0';

      // Discrepancy warning banner if match rate is low
      if (proxyTotal > 10 && matchPctVal < 90.0) {
        html += `
          <div class="warning-banner">
            <svg width="20" height="20" fill="currentColor" viewBox="0 0 16 16">
              <path d="M7.938 2.016A.13.13 0 0 1 8.002 2a.13.13 0 0 1 .063.016.146.146 0 0 1 .054.057l6.857 11.667c.036.06.035.124.002.183a.163.163 0 0 1-.054.06.116.116 0 0 1-.066.017H1.146a.115.115 0 0 1-.066-.017.163.163 0 0 1-.054-.06.176.176 0 0 1 .002-.183L7.884 2.073a.147.147 0 0 1 .054-.057zm1.044-.45a1.13 1.13 0 0 0-1.96 0L.165 13.233c-.457.778.091 1.767.98 1.767h13.713c.889 0 1.438-.99.98-1.767L8.982 1.566z"/>
              <path d="M7.002 12a1 1 0 1 1 2 0 1 1 0 0 1-2 0zM7.1 5.995a.905.905 0 1 1 1.8 0l-.35 3.507a.552.552 0 0 1-1.1 0L7.1 5.995z"/>
            </svg>
            <div>
              <strong>Low Cross-check Logging Rate (${matchPct}%):</strong> There is a significant discrepancy between total proxy connections and logged inference calls. This could indicate model list requests, aborts, or system errors on the API side.
            </div>
          </div>
        `;
      }

      html += `
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 24px;">
          <div>
            <h3 style="font-size:12px; text-transform:uppercase; color:var(--text-muted); margin-bottom:12px;">Global Statistics</h3>
            <table style="width:100%;">
              <thead>
                <tr>
                  <th>Metric Name</th>
                  <th class="num">Count</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>Proxy total connections (raw requests)</td>
                  <td class="num"><b>${proxyTotal}</b></td>
                </tr>
                <tr>
                  <td>  └ Logged inference calls (api_calls)</td>
                  <td class="num">${proxyStats.logged_calls || 0}</td>
                </tr>
                <tr>
                  <td>  └ Filtered requests (model_list, properties, metadata)</td>
                  <td class="num">${proxyStats.unlogged_calls || 0}</td>
                </tr>
                <tr>
                  <td>  └ Proxy failure states</td>
                  <td class="num" style="color:var(--red)">${proxyErrors}</td>
                </tr>
                <tr>
                  <td>Database synchronization rate</td>
                  <td class="num" style="font-weight:600; color:${matchPctVal > 95 ? 'var(--green)' : matchPctVal > 80 ? 'var(--orange)' : 'var(--red)'}">
                    ${matchPct}%
                  </td>
                </tr>
                <tr>
                  <td>Proxy tracking start timestamp</td>
                  <td>${proxyStats.started_at ? formatShortDate(proxyStats.started_at) : '—'}</td>
                </tr>
              </tbody>
            </table>
          </div>
      `;
    } else {
      html += '<div class="loading-overlay">No proxy_calls logs available. Enable logging on the main proxy and restart.</div>';
    }

    if (breakdown.length) {
      html += `
        <div>
          <h3 style="font-size:12px; text-transform:uppercase; color:var(--text-muted); margin-bottom:12px;">Breakdown by Call Type</h3>
          <table style="width:100%;">
            <thead>
              <tr>
                <th>Call Type</th>
                <th class="num">Total Requests</th>
                <th class="num">Logged</th>
                <th class="num">Errors</th>
              </tr>
            </thead>
            <tbody>
              ${breakdown.map(b => `
                <tr>
                  <td><span class="tag tag-other">${b.call_type || 'unclassified'}</span></td>
                  <td class="num">${b.calls}</td>
                  <td class="num">${b.logged || 0}</td>
                  <td class="num" style="color:${b.errors > 0 ? 'var(--red)' : 'inherit'}">${b.errors || 0}</td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        </div>
      </div>`;
    } else {
      html += '</div>';
    }

    el.innerHTML = html;
  }

  /**
   * Render System Diagnostics
   */
  function renderHealth(health) {
    const el = document.getElementById('diagnosticsPanel');
    if (!el) return;

    el.innerHTML = `
      <div class="diag-grid">
        <div class="diag-card">
          <h3>Database Info</h3>
          <div class="diag-value-row">
            <span class="diag-label">SQLite File Path</span>
            <span class="diag-val code">${health.db_path || 'unknown'}</span>
          </div>
          <div class="diag-value-row">
            <span class="diag-label">Database Exists</span>
            <span class="diag-val" style="color: ${health.db_exists ? 'var(--green)' : 'var(--red)'}">
              ${health.db_exists ? 'YES' : 'NO'}
            </span>
          </div>
          <div class="diag-value-row">
            <span class="diag-label">Database File Size</span>
            <span class="diag-val">${health.db_size_mb || 0} MB</span>
          </div>
        </div>
        <div class="diag-card">
          <h3>Server Configuration</h3>
          <div class="diag-value-row">
            <span class="diag-label">Backend Host URL</span>
            <span class="diag-val code">${TelemetryAPI.BASE_URL || 'Local (relative)'}</span>
          </div>
          <div class="diag-value-row">
            <span class="diag-label">Static Assets Served</span>
            <span class="diag-val" style="color: var(--green)">YES</span>
          </div>
          <div class="diag-value-row">
            <span class="diag-label">CORS Enabled</span>
            <span class="diag-val" style="color: var(--green)">YES (Access-Control-Allow-Origin: *)</span>
          </div>
        </div>
      </div>
    `;
  }

  /**
   * Render Recent Calls Table
   */
  function renderCallsTable(calls, sortCol, sortDir) {
    const tbody = document.querySelector('#callsTable tbody');
    if (!tbody) return;

    if (!calls || !calls.length) {
      tbody.innerHTML = '<tr><td colspan="11" class="loading" style="text-align:center;">No recent telemetry entries match current filters.</td></tr>';
      return;
    }

    // Sort calls data array on client side for immediate interaction
    const sorted = [...calls].sort((a, b) => {
      let av = a[sortCol];
      let bv = b[sortCol];
      if (av === null || av === undefined) return sortDir === 'asc' ? -1 : 1;
      if (bv === null || bv === undefined) return sortDir === 'asc' ? 1 : -1;
      
      if (typeof av === 'number') {
        return sortDir === 'asc' ? av - bv : bv - av;
      }
      return sortDir === 'asc' ? String(av).localeCompare(String(bv)) : String(bv).localeCompare(String(av));
    });

    const httpStatusMap = {
      400: 'HTTP 400 Bad Request',
      401: 'HTTP 401 Unauthorized',
      403: 'HTTP 403 Forbidden',
      404: 'HTTP 404 Not Found',
      408: 'HTTP 408 Request Timeout',
      429: 'HTTP 429 Rate Limit Exceeded',
      500: 'HTTP 500 Internal Server Error',
      502: 'HTTP 502 Bad Gateway',
      503: 'HTTP 503 Service Unavailable',
      504: 'HTTP 504 Gateway Timeout'
    };

    tbody.innerHTML = sorted.slice(0, 500).map(c => {
      const isErr = Boolean(c.error || (c.status_code && (c.status_code < 200 || c.status_code >= 300)));
      const errorMsg = c.error || (isErr && c.status_code ? (httpStatusMap[c.status_code] || `HTTP ${c.status_code}`) : (isErr ? 'Error' : ''));
      const statusTagClass = isErr ? 'tag-error' : (c.status_code >= 200 && c.status_code < 300) ? 'tag-success' : 'tag-warning';
      const statusText = c.status_code || (isErr ? 'ERR' : '?');

      return `
      <tr>
        <td>${formatShortDate(c.timestamp)}</td>
        <td><span class="tag ${getModelClass(c.model)}">${c.model || 'System / Non-Inference'}</span></td>
        <td><span class="tag tag-other">${c.call_type || 'chat'}</span></td>
        <td class="num">${formatNum(c.input_tokens)}</td>
        <td class="num">${formatNum(c.output_tokens)}</td>
        <td class="num">${formatMs(c.ttfb_ms)}</td>
        <td class="num">${formatMs(c.total_ms)}</td>
        <td class="num">${formatTps(c.tokens_per_s)}</td>
        <td class="num">${c.server_running !== null ? c.server_running : '—'}</td>
        <td>
          <span class="tag ${statusTagClass}">
            ${statusText}
          </span>
        </td>
        <td style="color:var(--red); font-size:11px; font-family:var(--font-mono); max-width: 250px; overflow: hidden; text-overflow: ellipsis;" title="${errorMsg}">
          ${errorMsg}
        </td>
      </tr>
    `}).join('');
  }

  /**
   * Render Per-Model Summary Table
   */
  function renderModelTable(data, currentGroupBy, rawCalls) {
    const tbody = document.querySelector('#modelTable tbody');
    if (!tbody) return;

    const isModelGroup = (currentGroupBy === 'model');

    // If server grouped by model, render server stats
    if (isModelGroup && data.groups) {
      const groups = [...data.groups].sort((a, b) => (b.total_input || 0) - (a.total_input || 0));
      tbody.innerHTML = groups.map(g => `
        <tr>
          <td><span class="tag ${getModelClass(g.model)}">${g.model || 'System / Non-Inference'}</span></td>
          <td class="num">${g.calls}</td>
          <td class="num">${formatNum(g.total_input)}</td>
          <td class="num">${formatNum(g.total_output)}</td>
          <td class="num">${formatMs(g.avg_ttfb)}</td>
          <td class="num">${formatMs(g.max_ttfb)}</td>
          <td class="num">${formatMs(g.avg_rtt)}</td>
          <td class="num">${formatMs(g.max_rtt)}</td>
          <td class="num">${formatTps(g.avg_tps)}</td>
          <td class="num">${g.avg_load !== null && g.avg_load !== undefined && g.avg_load > 0 ? Number(g.avg_load).toFixed(1) : '—'}</td>
          <td class="num">
            <span class="tag ${g.errors ? 'tag-error' : 'tag-success'}">
              ${g.errors || '0'}
            </span>
          </td>
        </tr>
      `).join('');
      return;
    }

    // Otherwise, compute client-side model aggregation using resolved raw calls
    const calls = rawCalls || data.calls || [];
    const byModel = {};
    
    calls.forEach(c => {
      const m = c.model || 'System / Non-Inference';
      if (!byModel[m]) {
        byModel[m] = {
          calls: 0,
          input: 0,
          output: 0,
          ttfbSum: 0,
          ttfbCount: 0,
          maxTtfb: 0,
          rttSum: 0,
          rttCount: 0,
          maxRtt: 0,
          tpsOutput: 0,
          tpsTotalMs: 0,
          loadSum: 0,
          loadCount: 0,
          errors: 0
        };
      }
      const b = byModel[m];
      const cnt = c.calls_count !== undefined && c.calls_count !== null ? c.calls_count : 1;
      b.calls += cnt;
      b.input += (c.input_tokens || 0);
      b.output += (c.output_tokens || 0);

      if (c.ttfb_ms !== null && c.ttfb_ms !== undefined) {
        b.ttfbSum += c.ttfb_ms * cnt;
        b.ttfbCount += cnt;
        if (c.ttfb_ms > b.maxTtfb) b.maxTtfb = c.ttfb_ms;
      }
      if (c.total_ms !== null && c.total_ms !== undefined) {
        b.rttSum += c.total_ms * cnt;
        b.rttCount += cnt;
        if (c.total_ms > b.maxRtt) b.maxRtt = c.total_ms;
      }
      if ((c.output_tokens || 0) > 0 && (c.total_ms || 0) > 0) {
        b.tpsOutput += (c.output_tokens || 0);
        b.tpsTotalMs += (c.total_ms || 0) * cnt;
      }
      if (c.server_running !== null && c.server_running !== undefined) {
        b.loadSum += c.server_running * cnt;
        b.loadCount += cnt;
      }
      if (c.error || (c.status_code && (c.status_code < 200 || c.status_code >= 300))) b.errors += cnt;
    });

    tbody.innerHTML = Object.entries(byModel)
      .sort((a, b) => (b[1].input || 0) - (a[1].input || 0))
      .map(([m, b]) => {
        const avgTtfb = b.ttfbCount > 0 ? (b.ttfbSum / b.ttfbCount) : 0;
        const avgRtt = b.rttCount > 0 ? (b.rttSum / b.rttCount) : 0;
        const avgTps = b.tpsTotalMs > 0 ? (b.tpsOutput / (b.tpsTotalMs / 1000)) : 0;
        const avgLoad = b.loadCount > 0 ? (b.loadSum / b.loadCount) : 0;

        return `
          <tr>
            <td><span class="tag ${getModelClass(m)}">${m}</span></td>
            <td class="num">${b.calls}</td>
            <td class="num">${formatNum(b.input)}</td>
            <td class="num">${formatNum(b.output)}</td>
            <td class="num">${formatMs(avgTtfb)}</td>
            <td class="num">${formatMs(b.maxTtfb)}</td>
            <td class="num">${formatMs(avgRtt)}</td>
            <td class="num">${formatMs(b.maxRtt)}</td>
            <td class="num">${formatTps(avgTps)}</td>
            <td class="num">${b.loadCount > 0 ? avgLoad.toFixed(1) : '—'}</td>
            <td class="num">
              <span class="tag ${b.errors ? 'tag-error' : 'tag-success'}">
                ${b.errors || '0'}
              </span>
            </td>
          </tr>
        `;
      }).join('');
  }

  /**
   * Render Aggregated Time Series & Type Tables (Fixes broken Group By views)
   */
  function renderGroupedTable(groups, type) {
    const tableContainer = document.getElementById('groupedTableContainer');
    if (!tableContainer) return;

    if (!groups || !groups.length) {
      tableContainer.innerHTML = '<div class="loading-overlay">No grouped data found for this selection.</div>';
      return;
    }

    let html = '';

    if (type === 'hour' || type === 'day') {
      const timeLabel = type === 'hour' ? 'Hour Timestamp' : 'Day Date';
      html = `
        <table style="width:100%;">
          <thead>
            <tr>
              <th>${timeLabel}</th>
              <th class="num">Calls</th>
              <th class="num">Total Input</th>
              <th class="num">Total Output</th>
              <th class="num">Avg TTFB</th>
              <th class="num">Avg RTT</th>
              <th class="num">Avg Throughput</th>
              <th class="num">Avg Server Load</th>
              <th class="num">Errors</th>
            </tr>
          </thead>
          <tbody>
            ${groups.map(g => {
              const timeVal = g.hour || g.day || '?';
              const formattedTime = type === 'hour' ? formatShortDate(timeVal) : timeVal;
              return `
                <tr>
                  <td><b>${formattedTime}</b></td>
                  <td class="num">${g.calls}</td>
                  <td class="num">${formatNum(g.total_input)}</td>
                  <td class="num">${formatNum(g.total_output)}</td>
                  <td class="num">${formatMs(g.avg_ttfb)}</td>
                  <td class="num">${formatMs(g.avg_rtt)}</td>
                  <td class="num">${formatTps(g.avg_tps)}</td>
                  <td class="num">${(g.avg_load || 0).toFixed(1)}</td>
                  <td class="num">
                    <span class="tag ${g.errors ? 'tag-error' : 'tag-success'}">
                      ${g.errors || '0'}
                    </span>
                  </td>
                </tr>
              `;
            }).join('')}
          </tbody>
        </table>
      `;
    } else if (type === 'call_type') {
      html = `
        <table style="width:100%;">
          <thead>
            <tr>
              <th>Call Type</th>
              <th class="num">Total Calls</th>
              <th class="num">Total Input</th>
              <th class="num">Total Output</th>
              <th class="num">Avg RTT</th>
            </tr>
          </thead>
          <tbody>
            ${groups.map(g => `
              <tr>
                <td><span class="tag tag-other">${g.call_type || 'unclassified'}</span></td>
                <td class="num">${g.calls}</td>
                <td class="num">${formatNum(g.total_input)}</td>
                <td class="num">${formatNum(g.total_output)}</td>
                <td class="num">${formatMs(g.avg_rtt)}</td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      `;
    } else if (type === 'model') {
      // Re-use model table structure, but print it in the grouped section
      html = `
        <table style="width:100%;">
          <thead>
            <tr>
              <th>Model</th>
              <th class="num">Calls</th>
              <th class="num">Input Tok</th>
              <th class="num">Output Tok</th>
              <th class="num">Avg TTFB</th>
              <th class="num">Max TTFB</th>
              <th class="num">Avg RTT</th>
              <th class="num">Max RTT</th>
              <th class="num">Avg tok/s</th>
              <th class="num">Avg Load</th>
              <th class="num">Errors</th>
            </tr>
          </thead>
          <tbody>
            ${groups.map(g => `
              <tr>
                <td><span class="tag ${getModelClass(g.model)}">${g.model || 'unknown'}</span></td>
                <td class="num">${g.calls}</td>
                <td class="num">${formatNum(g.total_input)}</td>
                <td class="num">${formatNum(g.total_output)}</td>
                <td class="num">${formatMs(g.avg_ttfb)}</td>
                <td class="num">${formatMs(g.max_ttfb)}</td>
                <td class="num">${formatMs(g.avg_rtt)}</td>
                <td class="num">${formatMs(g.max_rtt)}</td>
                <td class="num">${formatTps(g.avg_tps)}</td>
                <td class="num">${(g.avg_load || 0).toFixed(1)}</td>
                <td class="num">
                  <span class="tag ${g.errors ? 'tag-error' : 'tag-success'}">
                    ${g.errors || '0'}
                  </span>
                </td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      `;
    }

    tableContainer.innerHTML = html;
  }

  /**
   * Set Up Custom Checkbox Dropdown for Model Selections
   */
  function setupCustomDropdown(availableModels, onSelectionChange, initialSelected = null) {
    const trigger = document.getElementById('modelSelectTrigger');
    const dropdown = document.getElementById('modelDropdown');
    const wrapper = document.querySelector('.custom-select-wrapper');
    if (!trigger || !dropdown || !wrapper) return;

    // Toggle dropdown open/close
    trigger.addEventListener('click', (e) => {
      e.stopPropagation();
      wrapper.classList.toggle('open');
    });

    // Close when clicking outside
    document.addEventListener('click', (e) => {
      if (!wrapper.contains(e.target)) {
        wrapper.classList.remove('open');
      }
    });

    // Populates options list
    function rebuild() {
      const checkAllDefault = (initialSelected === null);
      
      let optionsHtml = `
        <div class="custom-option select-all-btn" id="selectAllModelsBtn">
          <input type="checkbox" id="chk_all_models">
          <span>All Models</span>
        </div>
      `;

      optionsHtml += availableModels.map(m => {
        const isChecked = (checkAllDefault || (Array.isArray(initialSelected) && initialSelected.includes(m))) ? 'checked' : '';
        return `
          <div class="custom-option model-option" data-model="${m}">
            <input type="checkbox" id="chk_${m}" value="${m}" ${isChecked}>
            <span class="tag ${getModelClass(m)}">${m}</span>
          </div>
        `;
      }).join('');

      dropdown.innerHTML = optionsHtml;

      // Event listener inside options
      const checkAll = document.getElementById('chk_all_models');
      const itemChecks = dropdown.querySelectorAll('.model-option input[type="checkbox"]');

      // Sync visual trigger text
      function updateTriggerText() {
        const checkedOptions = Array.from(itemChecks).filter(i => i.checked);
        if (checkedOptions.length === 0 || checkedOptions.length === itemChecks.length) {
          trigger.textContent = 'All Models';
          checkAll.checked = (checkedOptions.length === itemChecks.length);
        } else {
          trigger.textContent = `${checkedOptions.length} Model${checkedOptions.length > 1 ? 's' : ''}`;
          checkAll.checked = false;
        }
      }

      // Option row click
      dropdown.querySelectorAll('.custom-option').forEach(opt => {
        opt.addEventListener('click', (e) => {
          e.stopPropagation();
          const chk = opt.querySelector('input[type="checkbox"]');
          
          // If they didn't click the checkbox directly, toggle it
          if (e.target !== chk) {
            chk.checked = !chk.checked;
          }

          if (opt.id === 'selectAllModelsBtn') {
            itemChecks.forEach(i => i.checked = chk.checked);
          }
          
          updateTriggerText();
          onSelectionChange(getSelectedModels());
        });
      });

      updateTriggerText();
    }

    function getSelectedModels() {
      const checkedInputs = dropdown.querySelectorAll('.model-option input[type="checkbox"]:checked');
      return Array.from(checkedInputs).map(i => i.value);
    }

    rebuild();

    return {
      getSelectedModels
    };
  }
  /**
   * Render Performance Analyzer Tab
   * Aggregates stats by hour-of-day and dynamically compares top 2 most active models
   */
  function renderPerformanceAnalyzer(calls) {
    const duelEl = document.getElementById('modelDuelContainer');
    if (!duelEl) return null;

    const duelTitle = document.getElementById('modelDuelTitle');

    if (!calls || !calls.length) {
      if (duelTitle) duelTitle.textContent = 'Model Performance Duel';
      duelEl.innerHTML = '<div class="loading-overlay">No telemetry data available for analysis. Adjust your date filters.</div>';
      return null;
    }

    // 1. Determine top 2 most-used models dynamically by input tokens
    const modelInputTokens = {};
    calls.forEach(c => {
      if (!c.model || c.model === 'unknown' || c.model === 'System / Non-Inference') return;
      const inp = c.input_tokens || 0;
      modelInputTokens[c.model] = (modelInputTokens[c.model] || 0) + inp;
    });

    const sortedModels = Object.keys(modelInputTokens).sort((a, b) => modelInputTokens[b] - modelInputTokens[a]);

    if (sortedModels.length < 2) {
      const singleModel = sortedModels[0] || 'Unknown';
      if (duelTitle) duelTitle.textContent = `Model Performance: ${singleModel}`;
      duelEl.innerHTML = `<div class="loading-overlay" style="padding: 24px; text-align: center; color: var(--text-muted);">A performance duel requires at least 2 distinct active models. Currently only <b>${singleModel}</b> is active in this time filter. Select additional models in the filter to compare head-to-head.</div>`;
    } else {
      const modelA = sortedModels[0];
      const modelB = sortedModels[1];

      if (duelTitle) duelTitle.textContent = `Model Performance Duel: ${modelA} vs ${modelB}`;

      const duelData = {
        modelA: { name: modelA, calls: 0, inputTok: 0, outputTok: 0, ttfb: [], rtt: [], tpsOutput: 0, tpsTotalMs: 0, errors: 0 },
        modelB: { name: modelB, calls: 0, inputTok: 0, outputTok: 0, ttfb: [], rtt: [], tpsOutput: 0, tpsTotalMs: 0, errors: 0 }
      };

      calls.forEach(c => {
        if (!c.model) return;
        let grp = null;
        if (c.model === modelA) grp = duelData.modelA;
        else if (c.model === modelB) grp = duelData.modelB;

        if (grp) {
          const cnt = c.calls_count !== undefined && c.calls_count !== null ? c.calls_count : 1;
          grp.calls += cnt;
          grp.inputTok += (c.input_tokens || 0);
          grp.outputTok += (c.output_tokens || 0);
          if (c.ttfb_ms !== null && c.ttfb_ms !== undefined) grp.ttfb.push(c.ttfb_ms);
          if (c.total_ms !== null && c.total_ms !== undefined) grp.rtt.push(c.total_ms);
          if ((c.output_tokens || 0) > 0 && (c.total_ms || 0) > 0) {
            grp.tpsOutput += (c.output_tokens || 0);
            grp.tpsTotalMs += (c.total_ms || 0) * cnt;
          }
          if (c.error || (c.status_code && (c.status_code < 200 || c.status_code >= 300))) grp.errors += cnt;
        }
      });

      const avg = arr => arr.length ? arr.reduce((a,b) => a+b, 0) / arr.length : 0;
      const calcPct = (part, total) => total ? (part / total * 100) : 100;

      const summaryA = {
        calls: duelData.modelA.calls,
        avgTtfb: avg(duelData.modelA.ttfb),
        avgRtt: avg(duelData.modelA.rtt),
        avgTps: duelData.modelA.tpsTotalMs > 0 ? (duelData.modelA.tpsOutput / (duelData.modelA.tpsTotalMs / 1000)) : 0,
        successRate: 100 - calcPct(duelData.modelA.errors, duelData.modelA.calls),
        totalTokens: duelData.modelA.inputTok + duelData.modelA.outputTok
      };

      const summaryB = {
        calls: duelData.modelB.calls,
        avgTtfb: avg(duelData.modelB.ttfb),
        avgRtt: avg(duelData.modelB.rtt),
        avgTps: duelData.modelB.tpsTotalMs > 0 ? (duelData.modelB.tpsOutput / (duelData.modelB.tpsTotalMs / 1000)) : 0,
        successRate: 100 - calcPct(duelData.modelB.errors, duelData.modelB.calls),
        totalTokens: duelData.modelB.inputTok + duelData.modelB.outputTok
      };

      const winner = {};
      if (summaryA.calls && summaryB.calls) {
        if (summaryA.avgTtfb > 0 && summaryB.avgTtfb > 0) {
          winner.ttfb = summaryA.avgTtfb < summaryB.avgTtfb ? 'modelA' : (summaryA.avgTtfb > summaryB.avgTtfb ? 'modelB' : 'draw');
        } else if (summaryA.avgTtfb > 0) {
          winner.ttfb = 'modelA';
        } else if (summaryB.avgTtfb > 0) {
          winner.ttfb = 'modelB';
        } else {
          winner.ttfb = 'draw';
        }

        if (summaryA.avgRtt > 0 && summaryB.avgRtt > 0) {
          winner.rtt = summaryA.avgRtt < summaryB.avgRtt ? 'modelA' : (summaryA.avgRtt > summaryB.avgRtt ? 'modelB' : 'draw');
        } else if (summaryA.avgRtt > 0) {
          winner.rtt = 'modelA';
        } else if (summaryB.avgRtt > 0) {
          winner.rtt = 'modelB';
        } else {
          winner.rtt = 'draw';
        }

        if (summaryA.avgTps > 0 || summaryB.avgTps > 0) {
          winner.tps = summaryA.avgTps > summaryB.avgTps ? 'modelA' : (summaryA.avgTps < summaryB.avgTps ? 'modelB' : 'draw');
        } else {
          winner.tps = 'draw';
        }

        winner.success = summaryA.successRate > summaryB.successRate ? 'modelA' : (summaryA.successRate === summaryB.successRate ? 'draw' : 'modelB');
      }

      const colorA = TelemetryCharts.getModelColor(modelA);
      const colorB = TelemetryCharts.getModelColor(modelB);

      let html = `
        <div class="duel-layout">
          <div>
            <table style="width: 100%; font-size: 13px;">
              <thead>
                <tr>
                  <th>Performance Metric</th>
                  <th style="color:${colorA};">${modelA}</th>
                  <th style="color:${colorB};">${modelB}</th>
                  <th>Winner</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td><b>Total Requests</b></td>
                  <td class="num">${formatNum(summaryA.calls)}</td>
                  <td class="num">${formatNum(summaryB.calls)}</td>
                  <td>${summaryA.calls > summaryB.calls ? `🔥 ${modelA} (More data)` : summaryB.calls > summaryA.calls ? `🔥 ${modelB} (More data)` : 'Draw'}</td>
                </tr>
                <tr>
                  <td><b>Avg Time to First Token (TTFT)</b></td>
                  <td class="num" style="${winner.ttfb === 'modelA' ? 'color:var(--green); font-weight:600;' : ''}">${formatMs(summaryA.avgTtfb)}</td>
                  <td class="num" style="${winner.ttfb === 'modelB' ? 'color:var(--green); font-weight:600;' : ''}">${formatMs(summaryB.avgTtfb)}</td>
                  <td>${winner.ttfb === 'modelA' ? `🏆 ${modelA}` : winner.ttfb === 'modelB' ? `🏆 ${modelB}` : '—'}</td>
                </tr>
                <tr>
                  <td><b>Avg Latency (RTT)</b></td>
                  <td class="num" style="${winner.rtt === 'modelA' ? 'color:var(--green); font-weight:600;' : ''}">${formatMs(summaryA.avgRtt)}</td>
                  <td class="num" style="${winner.rtt === 'modelB' ? 'color:var(--green); font-weight:600;' : ''}">${formatMs(summaryB.avgRtt)}</td>
                  <td>${winner.rtt === 'modelA' ? `🏆 ${modelA}` : winner.rtt === 'modelB' ? `🏆 ${modelB}` : '—'}</td>
                </tr>
                <tr>
                  <td><b>Avg Generation Speed (tok/s)</b></td>
                  <td class="num" style="${winner.tps === 'modelA' ? 'color:var(--green); font-weight:600;' : ''}">${formatTps(summaryA.avgTps)}</td>
                  <td class="num" style="${winner.tps === 'modelB' ? 'color:var(--green); font-weight:600;' : ''}">${formatTps(summaryB.avgTps)}</td>
                  <td>${winner.tps === 'modelA' ? `🏆 ${modelA}` : winner.tps === 'modelB' ? `🏆 ${modelB}` : '—'}</td>
                </tr>
                <tr>
                  <td><b>Success Rate (Reliability)</b></td>
                  <td class="num" style="${winner.success === 'modelA' ? 'color:var(--green); font-weight:600;' : ''}">${summaryA.successRate.toFixed(1)}%</td>
                  <td class="num" style="${winner.success === 'modelB' ? 'color:var(--green); font-weight:600;' : ''}">${summaryB.successRate.toFixed(1)}%</td>
                  <td>${winner.success === 'modelA' ? `🏆 ${modelA}` : winner.success === 'modelB' ? `🏆 ${modelB}` : winner.success === 'draw' ? '🤝 Draw' : '—'}</td>
                </tr>
              </tbody>
            </table>
          </div>
      `;

      const speedWinner = winner.ttfb === 'modelA' ? modelA : (winner.ttfb === 'modelB' ? modelB : null);
      let speedText = 'Both models show comparable startup latency.';
      if (speedWinner) {
        if (summaryA.avgTtfb > 0 && summaryB.avgTtfb > 0) {
          const higherTtfb = Math.max(summaryA.avgTtfb, summaryB.avgTtfb);
          const lowerTtfb = Math.min(summaryA.avgTtfb, summaryB.avgTtfb);
          const speedPct = ((higherTtfb - lowerTtfb) / higherTtfb * 100).toFixed(0);
          speedText = `<b>${speedWinner}</b> starts generating text faster, leading by <b>${speedPct}%</b> in average Time to First Token. Use ${speedWinner} for interactive UI prompts or real-time autocomplete tasks where immediate response is critical.`;
        } else {
          speedText = `<b>${speedWinner}</b> has streaming TTFT recorded (avg <b>${formatMs(summaryA.avgTtfb || summaryB.avgTtfb)}</b>). Use ${speedWinner} for interactive UI prompts or real-time tasks.`;
        }
      }

      const genWinner = winner.tps === 'modelA' ? modelA : (winner.tps === 'modelB' ? modelB : null);
      let genText = 'Both models show comparable token generation speeds.';
      if (genWinner) {
        if (Math.min(summaryA.avgTps, summaryB.avgTps) > 0) {
          const higherTps = Math.max(summaryA.avgTps, summaryB.avgTps);
          const lowerTps = Math.min(summaryA.avgTps, summaryB.avgTps);
          const genPct = ((higherTps - lowerTps) / lowerTps * 100).toFixed(0);
          genText = `<b>${genWinner}</b> prints tokens at a higher velocity, winning by <b>${genPct}%</b> in raw generation throughput (${formatTps(higherTps)} vs ${formatTps(lowerTps)} tok/s). Use ${genWinner} for bulk agent summaries, code generation, or long reasoning tasks.`;
        } else {
          const maxTps = Math.max(summaryA.avgTps, summaryB.avgTps);
          genText = `<b>${genWinner}</b> is generating streaming tokens at <b>${formatTps(maxTps)} tok/s</b>. Use ${genWinner} for bulk agent summaries or code generation tasks.`;
        }
      }

      const recommendation = `
        <div style="background: rgba(255,255,255,0.02); border: 1px solid var(--border); border-radius: 8px; padding: 16px; display:flex; flex-direction:column; gap:12px;">
          <h4 style="font-size:13px; text-transform:uppercase; color:var(--accent); margin-bottom:4px;">💡 Telemetry Recommendation Report</h4>
          <p style="font-size:13px; line-height: 1.6;">
            • <b>Latency & Startup (TTFT)</b>: ${speedText}
          </p>
          <p style="font-size:13px; line-height: 1.6;">
            • <b>Generation Speed</b>: ${genText}
          </p>
          <p style="font-size:13px; line-height: 1.5; color:var(--text-muted); font-size:12px; margin-top:8px;">
            * Analysis is computed dynamically comparing the top 2 models by input token volume over the selected time range (${formatNum(summaryA.calls)} ${modelA} calls vs ${formatNum(summaryB.calls)} ${modelB} calls).
          </p>
        </div>
      `;

      html += recommendation + '</div>';
      duelEl.innerHTML = html;
    }

    // Compute Token Efficiency by Model
    const modelTokenStats = {};
    calls.forEach(c => {
      if (!c.model || c.model === 'unknown') return;
      const m = c.model;
      const cnt = c.calls_count !== undefined && c.calls_count !== null ? c.calls_count : 1;
      
      if (!modelTokenStats[m]) {
        modelTokenStats[m] = { name: m, calls: 0, input: 0, output: 0, total: 0 };
      }
      const s = modelTokenStats[m];
      s.calls += cnt;
      s.input += (c.input_tokens || 0);
      s.output += (c.output_tokens || 0);
      s.total += (c.input_tokens || 0) + (c.output_tokens || 0);
    });

    const efficiencyRows = Object.values(modelTokenStats).map(s => {
      const avgInput = s.calls > 0 ? (s.input / s.calls) : 0;
      const avgOutput = s.calls > 0 ? (s.output / s.calls) : 0;
      const ratio = avgOutput > 0 ? (avgInput / avgOutput) : 0;
      return {
        ...s,
        avgInput,
        avgOutput,
        ratio
      };
    });

    const tableBody = document.getElementById('tokenEfficiencyTableBody');
    if (tableBody) {
      if (efficiencyRows.length === 0) {
        tableBody.innerHTML = '<tr><td colspan="8" style="text-align:center; color:var(--text-dim); padding:12px;">No token data available.</td></tr>';
      } else {
        tableBody.innerHTML = efficiencyRows.map(r => `
          <tr style="border-bottom:1px solid rgba(255,255,255,0.02);">
            <td style="padding:8px 12px; font-weight:600;"><span class="tag ${getModelClass(r.name)}">${r.name}</span></td>
            <td class="num">${formatNum(r.calls)}</td>
            <td class="num">${formatNum(r.input)}</td>
            <td class="num">${formatNum(r.output)}</td>
            <td class="num" style="color:var(--accent); font-weight:600;">${formatNum(r.total)}</td>
            <td class="num">${formatNum(Math.round(r.avgInput))}</td>
            <td class="num">${formatNum(Math.round(r.avgOutput))}</td>
            <td class="num">${r.ratio ? `${r.ratio.toFixed(2)}x` : '—'}</td>
          </tr>
        `).join('');
      }
    }

    const reportEl = document.getElementById('tokenOptimizationReport');
    if (reportEl) {
      if (efficiencyRows.length === 0) {
        reportEl.innerHTML = '<p style="color:var(--text-muted)">Awaiting telemetry data to calculate recommendations...</p>';
      } else {
        // Find top token consumer
        const topConsumer = [...efficiencyRows].sort((a,b) => b.total - a.total)[0];
        const totalTokensAcrossAll = efficiencyRows.reduce((sum, r) => sum + r.total, 0);
        const consumerShare = totalTokensAcrossAll > 0 ? (topConsumer.total / totalTokensAcrossAll * 100).toFixed(0) : 0;
        
        // Find highest prompt ratio
        const highestRatioModel = [...efficiencyRows].filter(r => r.ratio > 0).sort((a,b) => b.ratio - a.ratio)[0];
        
        // Find highest avg tokens per call
        const highestAvgTokensModel = [...efficiencyRows].sort((a,b) => (b.avgInput + b.avgOutput) - (a.avgInput + a.avgOutput))[0];
        
        let reportHtml = `
          <h4 style="font-size:13px; text-transform:uppercase; color:var(--green); margin-bottom:4px;">💡 Token Optimization Recommendations</h4>
        `;
        
        if (topConsumer) {
          reportHtml += `
            <p style="font-size:13px; line-height:1.6;">
              • <b>Major Consumer</b>: Model <b>${topConsumer.name}</b> is responsible for <b>${consumerShare}%</b> of all tokens used (<b>${formatNum(topConsumer.total)}</b> tokens). 
              Focus optimization efforts here first to achieve maximum token reduction.
            </p>
          `;
        }
        
        if (highestRatioModel && highestRatioModel.ratio > 1.5) {
          reportHtml += `
            <p style="font-size:13px; line-height:1.6;">
              • <b>Prompt Bloat Alert</b>: <b>${highestRatioModel.name}</b> has a high Prompt-to-Completion ratio of <b>${highestRatioModel.ratio.toFixed(2)}x</b> 
              (avg prompt size <b>${formatNum(Math.round(highestRatioModel.avgInput))}</b> tokens vs completion size <b>${formatNum(Math.round(highestRatioModel.avgOutput))}</b> tokens). 
              Consider implementing <b>context caching</b>, shortening system prompts, or compressing few-shot examples.
            </p>
          `;
        } else {
          reportHtml += `
            <p style="font-size:13px; line-height:1.6;">
              • <b>Input/Output Balance</b>: Prompt-to-completion ratios are healthy. Output tokens dominate or are balanced with input tokens, indicating efficient prompt designs.
            </p>
          `;
        }
        
        if (highestAvgTokensModel && (highestAvgTokensModel.avgInput + highestAvgTokensModel.avgOutput) > 2000) {
          const avgTotal = Math.round(highestAvgTokensModel.avgInput + highestAvgTokensModel.avgOutput);
          reportHtml += `
            <p style="font-size:13px; line-height:1.6;">
              • <b>Heavy Request Overhead</b>: <b>${highestAvgTokensModel.name}</b> has a high average token density of <b>${formatNum(avgTotal)}</b> tokens per call. 
              Review application flows using this model to verify if large text blocks are being re-sent redundantly.
            </p>
          `;
        }
        
        reportHtml += `
          <p style="font-size:11px; line-height:1.5; color:var(--text-muted); margin-top:8px; border-top: 1px solid rgba(255,255,255,0.05); padding-top:8px;">
            <b>Suggested Strategy</b>: To minimize total token usage over time:
            <br>1. Shorten high-frequency prompts for ${topConsumer ? topConsumer.name : 'top models'}.
            <br>2. Set max_tokens limits on completions to avoid runaway generation outputs.
            <br>3. Avoid sending complete chat histories if older messages are not relevant to context.
          </p>
        `;
        
        reportEl.innerHTML = reportHtml;
      }
    }

    // Aggregate Hour of Day stats (local time)
    const hourData = Array.from({ length: 24 }, (_, i) => ({
      hour: i,
      calls: 0,
      errors: 0,
      rttSum: 0,
      rttCount: 0,
      ttfbSum: 0,
      ttfbCount: 0
    }));

    calls.forEach(c => {
      const d = new Date(c.timestamp);
      const h = d.getHours(); // Local hour of day!
      const cnt = c.calls_count !== undefined && c.calls_count !== null ? c.calls_count : 1;
      
      hourData[h].calls += cnt;
      if (c.error || (c.status_code && (c.status_code < 200 || c.status_code >= 300))) {
        hourData[h].errors += cnt;
      }
      if (c.total_ms) {
        hourData[h].rttSum += (c.total_ms / 1000) * cnt; // in seconds
        hourData[h].rttCount += cnt;
      }
      if (c.ttfb_ms) {
        hourData[h].ttfbSum += c.ttfb_ms * cnt; // in milliseconds
        hourData[h].ttfbCount += cnt;
      }
    });

    // ──────── Actionable Insights & Matrix Heatmaps ────────
    const insightsEl = document.getElementById('performanceInsightsContainer');
    if (insightsEl) {
      const activeModelsSet = new Set();
      calls.forEach(c => {
        if (c.model) activeModelsSet.add(c.model);
      });
      const activeModels = Array.from(activeModelsSet).sort();

      const modelStats = {};
      activeModels.forEach(m => {
        modelStats[m] = {
          name: m,
          periods: {
            Night: { calls: 0, rttSum: 0, rttCount: 0, errors: 0, tpsSum: 0, tpsCount: 0 },
            Morning: { calls: 0, rttSum: 0, rttCount: 0, errors: 0, tpsSum: 0, tpsCount: 0 },
            Afternoon: { calls: 0, rttSum: 0, rttCount: 0, errors: 0, tpsSum: 0, tpsCount: 0 },
            Evening: { calls: 0, rttSum: 0, rttCount: 0, errors: 0, tpsSum: 0, tpsCount: 0 }
          },
          days: Array.from({ length: 7 }, () => ({
            calls: 0, rttSum: 0, rttCount: 0, errors: 0, tpsSum: 0, tpsCount: 0
          }))
        };
      });

      const getPeriod = (hour) => {
        if (hour >= 0 && hour < 6) return 'Night';
        if (hour >= 6 && hour < 12) return 'Morning';
        if (hour >= 12 && hour < 18) return 'Afternoon';
        return 'Evening';
      };

      calls.forEach(c => {
        if (!c.model) return;
        const m = c.model;
        const stats = modelStats[m];
        if (!stats) return;

        const d = new Date(c.timestamp);
        const hour = d.getHours();
        const day = d.getDay(); // 0 = Sunday, 1 = Monday, etc.
        const cnt = c.calls_count !== undefined && c.calls_count !== null ? c.calls_count : 1;
        const period = getPeriod(hour);

        // Period stats
        stats.periods[period].calls += cnt;
        if (c.error || (c.status_code && (c.status_code < 200 || c.status_code >= 300))) stats.periods[period].errors += cnt;
        if (c.total_ms) {
          stats.periods[period].rttSum += c.total_ms * cnt;
          stats.periods[period].rttCount += cnt;
        }
        if (c.tokens_per_s) {
          stats.periods[period].tpsSum += c.tokens_per_s * cnt;
          stats.periods[period].tpsCount += cnt;
        }

        // Day stats
        stats.days[day].calls += cnt;
        if (c.error || (c.status_code && (c.status_code < 200 || c.status_code >= 300))) stats.days[day].errors += cnt;
        if (c.total_ms) {
          stats.days[day].rttSum += c.total_ms * cnt;
          stats.days[day].rttCount += cnt;
        }
        if (c.tokens_per_s) {
          stats.days[day].tpsSum += c.tokens_per_s * cnt;
          stats.days[day].tpsCount += cnt;
        }
      });

      if (activeModels.length === 0) {
        insightsEl.innerHTML = '<div style="color:var(--text-muted); padding:20px; text-align:center;">No telemetry data available for performance insights. Adjust your date filters.</div>';
      } else {
        const bestPeriodPerModel = {};
        const bestDayPerModel = {};

        Object.keys(modelStats).forEach(m => {
          const stats = modelStats[m];
          
          let minRtt = Infinity;
          let bestP = null;
          Object.keys(stats.periods).forEach(p => {
            const periodStats = stats.periods[p];
            if (periodStats.rttCount > 0) {
              const avgRtt = periodStats.rttSum / periodStats.rttCount;
              if (avgRtt < minRtt) {
                minRtt = avgRtt;
                bestP = p;
              }
            }
          });
          bestPeriodPerModel[m] = { period: bestP, rtt: minRtt };

          let minDayRtt = Infinity;
          let bestD = null;
          for (let d = 0; d < 7; d++) {
            const dayStats = stats.days[d];
            if (dayStats.rttCount > 0) {
              const avgRtt = dayStats.rttSum / dayStats.rttCount;
              if (avgRtt < minDayRtt) {
                minDayRtt = avgRtt;
                bestD = d;
              }
            }
          }
          bestDayPerModel[m] = { day: bestD, rtt: minDayRtt };
        });

        // Overall period stats
        const overallPeriods = {
          Night: { rttSum: 0, rttCount: 0 },
          Morning: { rttSum: 0, rttCount: 0 },
          Afternoon: { rttSum: 0, rttCount: 0 },
          Evening: { rttSum: 0, rttCount: 0 }
        };
        const overallDays = Array.from({ length: 7 }, () => ({ rttSum: 0, rttCount: 0 }));

        calls.forEach(c => {
          if (!c.total_ms) return;
          const cnt = c.calls_count !== undefined && c.calls_count !== null ? c.calls_count : 1;
          const d = new Date(c.timestamp);
          const hour = d.getHours();
          const day = d.getDay();

          const period = getPeriod(hour);
          overallPeriods[period].rttSum += c.total_ms * cnt;
          overallPeriods[period].rttCount += cnt;

          overallDays[day].rttSum += c.total_ms * cnt;
          overallDays[day].rttCount += cnt;
        });

        let bestOverallPeriod = null, worstOverallPeriod = null;
        let minOverallRtt = Infinity, maxOverallRtt = -Infinity;
        Object.keys(overallPeriods).forEach(p => {
          const stats = overallPeriods[p];
          if (stats.rttCount > 0) {
            const avg = stats.rttSum / stats.rttCount;
            if (avg < minOverallRtt) {
              minOverallRtt = avg;
              bestOverallPeriod = p;
            }
            if (avg > maxOverallRtt) {
              maxOverallRtt = avg;
              worstOverallPeriod = p;
            }
          }
        });

        let weekdayRttSum = 0, weekdayCount = 0;
        let weekendRttSum = 0, weekendCount = 0;
        for (let d = 0; d < 7; d++) {
          const stats = overallDays[d];
          if (stats.rttCount > 0) {
            if (d === 0 || d === 6) {
              weekendRttSum += stats.rttSum;
              weekendCount += stats.rttCount;
            } else {
              weekdayRttSum += stats.rttSum;
              weekdayCount += stats.rttCount;
            }
          }
        }
        const avgWeekday = weekdayCount ? (weekdayRttSum / weekdayCount) : 0;
        const avgWeekend = weekendCount ? (weekendRttSum / weekendCount) : 0;

        let bestOverallDay = null, worstOverallDay = null;
        let minOverallDayRtt = Infinity, maxOverallDayRtt = -Infinity;
        for (let d = 0; d < 7; d++) {
          const stats = overallDays[d];
          if (stats.rttCount > 0) {
            const avg = stats.rttSum / stats.rttCount;
            if (avg < minOverallDayRtt) {
              minOverallDayRtt = avg;
              bestOverallDay = d;
            }
            if (avg > maxOverallDayRtt) {
              maxOverallDayRtt = avg;
              worstOverallDay = d;
            }
          }
        }

        let recommendationsHtml = '';

        if (bestOverallPeriod && worstOverallPeriod && minOverallRtt !== Infinity && maxOverallRtt !== -Infinity) {
          const slowdown = minOverallRtt > 0 ? Math.round(((maxOverallRtt - minOverallRtt) / minOverallRtt) * 100) : 0;
          recommendationsHtml += `
            <p style="font-size:13px; line-height:1.6; margin-bottom:8px;">
              • <b>Optimal Time of Day</b>: Across all models, response times are fastest during the <b>${bestOverallPeriod}</b> period (average latency <b>${formatMs(minOverallRtt)}</b>) and slowest during the <b>${worstOverallPeriod}</b> period (average latency <b>${formatMs(maxOverallRtt)}</b>), representing a <b>${slowdown}%</b> average latency penalty during peak periods.
            </p>
          `;
        }

        if (avgWeekday && avgWeekend) {
          const diffPct = avgWeekday > 0 ? Math.round(((avgWeekday - avgWeekend) / avgWeekday) * 100) : 0;
          const directionWord = avgWeekend < avgWeekday ? 'faster' : 'slower';
          const absDiffPct = Math.abs(diffPct);
          recommendationsHtml += `
            <p style="font-size:13px; line-height:1.6; margin-bottom:8px;">
              • <b>Weekday vs. Weekend Load</b>: Weekend API latency averages <b>${formatMs(avgWeekend)}</b> compared to <b>${formatMs(avgWeekday)}</b> on weekdays. Weekend calls are <b>${absDiffPct}% ${directionWord}</b>, likely due to lower upstream corporate/academia usage of the e-INFRA servers.
            </p>
          `;
        }

        if (bestOverallDay !== null && worstOverallDay !== null && minOverallDayRtt !== Infinity && maxOverallDayRtt !== -Infinity) {
          const dayNamesList = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
          const bestDayName = dayNamesList[bestOverallDay];
          const worstDayName = dayNamesList[worstOverallDay];
          const dayDiffPct = minOverallDayRtt > 0 ? Math.round(((maxOverallDayRtt - minOverallDayRtt) / minOverallDayRtt) * 100) : 0;
          recommendationsHtml += `
            <p style="font-size:13px; line-height:1.6; margin-bottom:8px;">
              • <b>Optimal Day of the Week</b>: Overall telemetry shows <b>${bestDayName}</b> is the best performing day of the week (average latency <b>${formatMs(minOverallDayRtt)}</b>), while <b>${worstDayName}</b> experiences the highest congestion (average latency <b>${formatMs(maxOverallDayRtt)}</b>, a <b>${dayDiffPct}%</b> slowdown compared to the optimal day).
            </p>
          `;
        }

        let modelDispatchText = '';
        Object.keys(bestPeriodPerModel).forEach(m => {
          const b = bestPeriodPerModel[m];
          if (b.period) {
            modelDispatchText += `<b>${m}</b> performs best during the <b>${b.period}</b> (avg ${formatMs(b.rtt)}), `;
          }
        });
        if (modelDispatchText) {
          modelDispatchText = modelDispatchText.replace(/,\s*$/, '');
          recommendationsHtml += `
            <p style="font-size:13px; line-height:1.6; margin-bottom:8px;">
              • <b>Model Performance Hotspots</b>: ${modelDispatchText}. Consider scheduling bulk offline tasks (like embeddings or document synthesis) during these optimal hours.
            </p>
          `;
        }

        let errorHotspots = [];
        Object.keys(modelStats).forEach(m => {
          const stats = modelStats[m];
          Object.keys(stats.periods).forEach(p => {
            const periodStats = stats.periods[p];
            if (periodStats.calls > 10) {
              const errRate = (periodStats.errors / periodStats.calls) * 100;
              if (errRate > 5.0) {
                errorHotspots.push(`<b>${m}</b> during <b>${p}</b> (${errRate.toFixed(1)}% error rate)`);
              }
            }
          });
        });

        if (errorHotspots.length > 0) {
          recommendationsHtml += `
            <p style="font-size:13px; line-height:1.6; margin-bottom:8px; color: var(--orange);">
              • <b>⚠️ High-Error Risk Windows</b>: Telemetry shows elevated failure/timeout rates for: ${errorHotspots.join(', ')}. Use alternative models during these specific windows to maintain service availability.
            </p>
          `;
        } else {
          recommendationsHtml += `
            <p style="font-size:13px; line-height:1.6; margin-bottom:8px; color: var(--green);">
              • <b>Stability Check</b>: No significant hour-of-day or day-of-week rate limit error hotspots detected (>5% failure rate). Service reliability remains uniform across the operational hours.
            </p>
          `;
        }

        const renderCell = (cellStats, isBest) => {
          if (!cellStats || cellStats.calls === 0) return `<td class="num" style="color:var(--text-dim); text-align:right;">—</td>`;
          const avgRtt = cellStats.rttCount ? (cellStats.rttSum / cellStats.rttCount) : 0;
          const successRate = 100 - (cellStats.calls ? (cellStats.errors / cellStats.calls * 100) : 0);
          const avgTps = cellStats.tpsCount ? (cellStats.tpsSum / cellStats.tpsCount) : 0;
          
          let style = 'padding: 8px 12px; border-bottom: 1px solid rgba(255,255,255,0.02); text-align:right;';
          let badge = '';
          if (isBest) {
            style = 'background: rgba(63, 185, 80, 0.08); border: 1px solid rgba(63, 185, 80, 0.2); font-weight:600; padding: 8px 12px; text-align:right;';
            badge = ' 🌟';
          }
          
          const rttText = avgRtt ? formatMs(avgRtt) : '—';
          const tpsText = avgTps ? `${avgTps.toFixed(0)} t/s` : '';
          
          return `
            <td class="num" style="${style}" title="Calls: ${cellStats.calls}\nErrors: ${cellStats.errors}\nSuccess: ${successRate.toFixed(1)}%">
              <div style="font-size:12px;">${rttText}${badge}</div>
              <div style="font-size:10px; color:var(--text-muted);">${successRate.toFixed(0)}% succ${tpsText ? ` • ${tpsText}` : ''}</div>
            </td>
          `;
        };

        const periodsList = ['Night', 'Morning', 'Afternoon', 'Evening'];
        let tablesHtml = `
          <div style="background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin-bottom: 16px;">
            <h4 style="font-size:13px; text-transform:uppercase; color:var(--accent); margin-bottom:12px;">📊 Model Performance Matrix: Hour of Day</h4>
            <div class="table-responsive">
              <table style="width:100%; border-collapse: collapse; font-size:12px;">
                <thead>
                  <tr style="border-bottom:1px solid var(--border);">
                    <th style="text-align:left; padding:8px 12px;">Model</th>
                    <th class="num" style="padding:8px 12px; text-align:right;">Night (00-06)</th>
                    <th class="num" style="padding:8px 12px; text-align:right;">Morning (06-12)</th>
                    <th class="num" style="padding:8px 12px; text-align:right;">Afternoon (12-18)</th>
                    <th class="num" style="padding:8px 12px; text-align:right;">Evening (18-24)</th>
                  </tr>
                </thead>
                <tbody>
                  ${Object.keys(modelStats).map(m => {
                    const stats = modelStats[m];
                    const bestP = bestPeriodPerModel[m].period;
                    return `
                      <tr style="border-bottom:1px solid rgba(255,255,255,0.02);">
                        <td style="padding:8px 12px; text-align:left; font-weight:600;"><span class="tag ${getModelClass(m)}">${m}</span></td>
                        ${periodsList.map(p => renderCell(stats.periods[p], p === bestP)).join('')}
                      </tr>
                    `;
                  }).join('')}
                </tbody>
              </table>
            </div>
          </div>

          <div style="background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin-bottom: 16px;">
            <h4 style="font-size:13px; text-transform:uppercase; color:var(--purple); margin-bottom:12px;">📅 Model Performance Matrix: Day of Week</h4>
            <div class="table-responsive">
              <table style="width:100%; border-collapse: collapse; font-size:12px;">
                <thead>
                  <tr style="border-bottom:1px solid var(--border);">
                    <th style="text-align:left; padding:8px 12px;">Model</th>
                    <th class="num" style="padding:8px 12px; text-align:right;">Mon</th>
                    <th class="num" style="padding:8px 12px; text-align:right;">Tue</th>
                    <th class="num" style="padding:8px 12px; text-align:right;">Wed</th>
                    <th class="num" style="padding:8px 12px; text-align:right;">Thu</th>
                    <th class="num" style="padding:8px 12px; text-align:right;">Fri</th>
                    <th class="num" style="padding:8px 12px; text-align:right;">Sat</th>
                    <th class="num" style="padding:8px 12px; text-align:right;">Sun</th>
                  </tr>
                </thead>
                <tbody>
                  ${Object.keys(modelStats).map(m => {
                    const stats = modelStats[m];
                    const bestD = bestDayPerModel[m].day;
                    const daysOrder = [1, 2, 3, 4, 5, 6, 0];
                    return `
                      <tr style="border-bottom:1px solid rgba(255,255,255,0.02);">
                        <td style="padding:8px 12px; text-align:left; font-weight:600;"><span class="tag ${getModelClass(m)}">${m}</span></td>
                        ${daysOrder.map(d => renderCell(stats.days[d], d === bestD)).join('')}
                      </tr>
                    `;
                  }).join('')}
                </tbody>
              </table>
            </div>
          </div>
        `;

        insightsEl.innerHTML = `
          <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 24px;">
            ${tablesHtml}
          </div>
          <div style="background: rgba(255,255,255,0.02); border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin-top: 16px;">
            <h4 style="font-size:13px; text-transform:uppercase; color:var(--cyan); margin-bottom:12px;">💡 Actions & Recommendations</h4>
            ${recommendationsHtml}
          </div>
        `;
      }
    }

    return hourData;
  }

  function formatCost(val) {
    if (val === null || val === undefined) return '—';
    if (val === 0) return '$0.00';
    if (val > 0 && val < 0.00001) return '$' + val.toFixed(6);
    if (val < 0.01) return '$' + val.toFixed(5);
    if (val < 1) return '$' + val.toFixed(4);
    return '$' + val.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function renderCostSummary(calls, modelCosts) {
    const el = document.getElementById('costSummaryBar');
    if (!el) return;

    if (!calls || !calls.length) {
      el.innerHTML = '<div class="loading-overlay">No cost data available for current selection.</div>';
      return;
    }

    let totalCost = 0;
    let inputCost = 0;
    let outputCost = 0;
    let totalCalls = 0;

    calls.forEach(c => {
      totalCost += c.total_cost || 0;
      inputCost += c.input_cost || 0;
      outputCost += c.output_cost || 0;
      totalCalls += (c.calls_count !== undefined && c.calls_count !== null ? c.calls_count : 1);
    });

    const avgCostPerCall = totalCalls > 0 ? (totalCost / totalCalls) : 0;
    const costPer1kCalls = avgCostPerCall * 1000;

    const cards = [
      { label: 'Total API Cost', value: formatCost(totalCost), sub: `Across all filtered calls`, cls: totalCost > 10.0 ? 'orange' : 'green' },
      { label: 'Input Token Cost', value: formatCost(inputCost), sub: `Prompt / context spend`, cls: 'accent' },
      { label: 'Output Token Cost', value: formatCost(outputCost), sub: `Completion / generation spend`, cls: 'accent' },
      { label: 'Avg Cost / Call', value: formatCost(avgCostPerCall), sub: `Per single inference request`, cls: avgCostPerCall > 0.05 ? 'orange' : 'green' },
      { label: 'Cost / 1K Calls', value: formatCost(costPer1kCalls), sub: `Estimate for 1,000 requests`, cls: costPer1kCalls > 50.0 ? 'orange' : 'green' }
    ];

    el.innerHTML = cards.map(c => `
      <div class="stat-card ${c.cls}">
        <div class="label">${c.label}</div>
        <div class="value ${c.cls}">${c.value}</div>
        <div class="subtext">${c.sub}</div>
      </div>
    `).join('');
  }

  function renderCostTables(calls, modelCosts, currentGroupBy) {
    const tbody = document.querySelector('#costModelTable tbody');
    if (!tbody) return;

    if (!calls || !calls.length) {
      tbody.innerHTML = '<tr><td colspan="10" class="loading" style="text-align:center;">No data matching current filters.</td></tr>';
      return;
    }

    // 1. Model Breakdown
    const byModel = {};
    calls.forEach(c => {
      const m = c.model || 'System / Non-Inference';
      if (!byModel[m]) {
        byModel[m] = {
          calls: 0,
          inputTokens: 0,
          outputTokens: 0,
          inputCost: 0,
          outputCost: 0,
          totalCost: 0,
          providerSource: c.provider_source || 'Unknown',
          lastUpdated: c.last_updated || '—'
        };
      }
      const b = byModel[m];
      const callsCount = (c.calls_count !== undefined && c.calls_count !== null ? c.calls_count : 1);
      b.calls += callsCount;
      b.inputTokens += c.input_tokens || 0;
      b.outputTokens += c.output_tokens || 0;
      b.inputCost += c.input_cost || 0;
      b.outputCost += c.output_cost || 0;
      b.totalCost += c.total_cost || 0;
      if (c.provider_source && c.provider_source !== 'Unknown') {
        b.providerSource = c.provider_source;
      }
      if (c.last_updated && (!b.lastUpdated || b.lastUpdated === '—' || c.last_updated > b.lastUpdated)) {
        b.lastUpdated = c.last_updated;
      }
    });

    tbody.innerHTML = Object.entries(byModel)
      .sort((a, b) => b[1].totalCost - a[1].totalCost)
      .map(([m, b]) => {
        const avgCost = b.calls > 0 ? (b.totalCost / b.calls) : 0;
        return `
          <tr>
            <td><span class="tag ${getModelClass(m)}">${m}</span></td>
            <td class="num">${formatNum(b.calls)}</td>
            <td class="num">${formatNum(b.inputTokens)}</td>
            <td class="num">${formatNum(b.outputTokens)}</td>
            <td class="num">${formatCost(b.inputCost)}</td>
            <td class="num">${formatCost(b.outputCost)}</td>
            <td class="num" style="font-weight:600; color:var(--text-white)">${formatCost(b.totalCost)}</td>
            <td class="num">${formatCost(avgCost)}</td>
            <td><span class="tag tag-other" style="font-size: 11px;">${b.providerSource}</span></td>
            <td style="color:var(--text-dim); font-size:11px;">${b.lastUpdated}</td>
          </tr>
        `;
      }).join('');

    // 2. Grouped Cost Table (Hour/Day)
    const groupedCard = document.getElementById('groupedCostTableCard');
    const groupedContainer = document.getElementById('groupedCostTableContainer');
    
    if (currentGroupBy === 'hour' || currentGroupBy === 'day') {
      if (groupedCard) groupedCard.style.display = 'block';
      const titleText = document.getElementById('groupedCostTableTitle');
      if (titleText) {
        titleText.textContent = `Grouped Cost Summary: By ${currentGroupBy.charAt(0).toUpperCase() + currentGroupBy.slice(1)}`;
      }

      // Group calls by date key
      const byTime = {};
      calls.forEach(c => {
        const d = new Date(c.timestamp);
        let key = '';
        if (currentGroupBy === 'hour') {
          const yr = d.getFullYear();
          const mo = String(d.getMonth() + 1).padStart(2, '0');
          const dy = String(d.getDate()).padStart(2, '0');
          const hr = String(d.getHours()).padStart(2, '0');
          key = `${yr}-${mo}-${dy}T${hr}:00:00`;
        } else {
          const yr = d.getFullYear();
          const mo = String(d.getMonth() + 1).padStart(2, '0');
          const dy = String(d.getDate()).padStart(2, '0');
          key = `${yr}-${mo}-${dy}`;
        }

        if (!byTime[key]) {
          byTime[key] = {
            calls: 0,
            inputTokens: 0,
            outputTokens: 0,
            inputCost: 0,
            outputCost: 0,
            totalCost: 0
          };
        }
        const b = byTime[key];
        const callsCount = (c.calls_count !== undefined && c.calls_count !== null ? c.calls_count : 1);
        b.calls += callsCount;
        b.inputTokens += c.input_tokens || 0;
        b.outputTokens += c.output_tokens || 0;
        b.inputCost += c.input_cost || 0;
        b.outputCost += c.output_cost || 0;
        b.totalCost += c.total_cost || 0;
      });

      const sortedTimeKeys = Object.keys(byTime).sort();

      if (groupedContainer) {
        const timeLabel = currentGroupBy === 'hour' ? 'Hour Timestamp' : 'Day Date';
        groupedContainer.innerHTML = `
          <table style="width:100%;">
            <thead>
              <tr>
                <th>${timeLabel}</th>
                <th class="num">Calls</th>
                <th class="num">Input Tokens</th>
                <th class="num">Output Tokens</th>
                <th class="num">Input Cost ($)</th>
                <th class="num">Output Cost ($)</th>
                <th class="num">Total Cost ($)</th>
                <th class="num">Avg Cost/Call ($)</th>
              </tr>
            </thead>
            <tbody>
              ${sortedTimeKeys.map(k => {
                const b = byTime[k];
                const formattedTime = currentGroupBy === 'hour' ? formatShortDate(k) : k;
                const avgCost = b.calls > 0 ? (b.totalCost / b.calls) : 0;
                return `
                  <tr>
                    <td><b>${formattedTime}</b></td>
                    <td class="num">${formatNum(b.calls)}</td>
                    <td class="num">${formatNum(b.inputTokens)}</td>
                    <td class="num">${formatNum(b.outputTokens)}</td>
                    <td class="num">${formatCost(b.inputCost)}</td>
                    <td class="num">${formatCost(b.outputCost)}</td>
                    <td class="num" style="font-weight:600; color:var(--text-white)">${formatCost(b.totalCost)}</td>
                    <td class="num">${formatCost(avgCost)}</td>
                  </tr>
                `;
              }).join('')}
            </tbody>
          </table>
        `;
      }
    } else {
      if (groupedCard) groupedCard.style.display = 'none';
    }
  }

  /**
   * Render Gateway Status & Metrics
   */
  function renderProxyStatus(status) {
    if (!status) return;

    const isRunning = Boolean(status.running || status.health_ok);
    const pid = status.pid;
    const port = status.port || 9090;
    const upstream = status.upstream || (status.health && status.health.upstream) || 'https://llm.ai.e-infra.cz/v1';

    // 1. Header Proxy Status badge
    const headerDot = document.getElementById('proxyHeaderDot');
    const headerText = document.getElementById('proxyHeaderText');
    if (headerDot && headerText) {
      headerDot.className = `proxy-dot ${isRunning ? 'online' : 'offline'}`;
      headerText.textContent = isRunning ? `Proxy: ${port}` : 'Proxy: Offline';
    }

    // 2. Compute Active Concurrency & Queue
    let concurrencyStr = isRunning ? '0 / 4 (0)' : '—';
    let activeCount = 0;
    let queuedCount = 0;
    if (status.health && status.health.rate_limiter) {
      const rl = status.health.rate_limiter;
      activeCount = rl.active || 0;
      const maxCount = rl.max_concurrent || 4;
      queuedCount = rl.queued || 0;
      concurrencyStr = `${activeCount} / ${maxCount} (${queuedCount})`;
    }

    // 3. Header Active Concurrency badge
    const headerConcurrencyBadge = document.getElementById('headerConcurrencyBadge');
    const headerConcurrencyVal = document.getElementById('headerConcurrencyVal');
    if (headerConcurrencyVal) {
      headerConcurrencyVal.textContent = concurrencyStr;
    }
    if (headerConcurrencyBadge) {
      if (!isRunning) {
        headerConcurrencyBadge.className = 'header-concurrency-badge';
      } else if (queuedCount > 0) {
        headerConcurrencyBadge.className = 'header-concurrency-badge high-load';
      } else if (activeCount > 0) {
        headerConcurrencyBadge.className = 'header-concurrency-badge active-load';
      } else {
        headerConcurrencyBadge.className = 'header-concurrency-badge';
      }
    }

    // 4. Main Control Panel status badge
    const stateBadge = document.getElementById('proxyStateBadge');
    const stateText = document.getElementById('proxyStateText');
    if (stateBadge && stateText) {
      stateBadge.className = `proxy-badge ${isRunning ? 'online' : 'offline'}`;
      stateText.textContent = isRunning ? 'ONLINE' : 'STOPPED';
    }

    // 5. Metric items in Control Panel
    const pidEl = document.getElementById('proxyPidVal');
    if (pidEl) pidEl.textContent = isRunning && pid ? pid : (isRunning ? 'Active' : '—');

    const portEl = document.getElementById('proxyPortVal');
    if (portEl) portEl.textContent = port;

    const upstreamEl = document.getElementById('proxyUpstreamVal');
    if (upstreamEl) upstreamEl.textContent = upstream;

    const concurrencyEl = document.getElementById('proxyConcurrencyVal');
    if (concurrencyEl) {
      concurrencyEl.textContent = concurrencyStr;
    }

    // Token budget
    const budgetFill = document.getElementById('proxyTokenBudgetFill');
    const budgetText = document.getElementById('proxyTokenBudgetText');
    if (budgetFill && budgetText) {
      const tb = (status.health && status.health.token_budget) || status.token_budget;
      if (tb && typeof tb.total_used === 'number') {
        const perc = tb.percentage_used || 0;
        budgetFill.style.width = `${Math.min(perc, 100)}%`;
        if (perc > 85) {
          budgetFill.className = 'token-budget-fill danger';
        } else if (perc > 60) {
          budgetFill.className = 'token-budget-fill warn';
        } else {
          budgetFill.className = 'token-budget-fill';
        }
        const usedM = ((tb.total_used || 0) / 1_000_000).toFixed(1);
        const limitM = ((tb.daily_limit || 480_000_000) / 1_000_000).toFixed(0);
        budgetText.textContent = `${usedM}M / ${limitM}M (${perc}%)`;
      } else {
        budgetFill.style.width = '0%';
        budgetText.textContent = isRunning ? '0 / 480M (0%)' : '—';
      }
    }

    // Button states
    const startBtn = document.getElementById('proxyStartBtn');
    const stopBtn = document.getElementById('proxyStopBtn');
    const restartBtn = document.getElementById('proxyRestartBtn');

    if (startBtn) startBtn.disabled = isRunning;
    if (stopBtn) stopBtn.disabled = !isRunning;
    if (restartBtn) restartBtn.disabled = !isRunning;
  }

  /**
   * Render Proxy Execution Logs in Terminal
   */
  function renderProxyLogs(logsData, autoScroll = true) {
    const terminal = document.getElementById('proxyLogTerminal');
    if (!terminal) return;

    if (!logsData) {
      terminal.textContent = 'No logs available.';
      return;
    }

    if (logsData.error) {
      terminal.textContent = `Error reading logs: ${logsData.error}`;
      return;
    }

    terminal.textContent = logsData.logs || 'No output recorded yet.';
    if (autoScroll) {
      terminal.scrollTop = terminal.scrollHeight;
    }
  }

  /**
   * Show Notification Alert on Gateway Tab
   */
  function showProxyAlert(message, type = 'info', durationMs = 5000) {
    const alertEl = document.getElementById('proxyActionAlert');
    if (!alertEl) return;

    alertEl.className = `proxy-alert ${type}`;
    alertEl.textContent = message;
    alertEl.style.display = 'flex';

    if (durationMs > 0) {
      setTimeout(() => {
        if (alertEl.textContent === message) {
          alertEl.style.display = 'none';
        }
      }, durationMs);
    }
  }

  /**
   * Render Raw Log Status in Control Panel
   */
  function renderRawLogStatus(statusData) {
    const badge = document.getElementById('rawLogStateBadge');
    const stateText = document.getElementById('rawLogStateText');
    const filePathVal = document.getElementById('rawLogFilePathVal');
    const fileSizeVal = document.getElementById('rawLogFileSizeVal');
    const toggleBtn = document.getElementById('rawLogToggleBtn');
    const toggleBtnText = document.getElementById('rawLogToggleBtnText');

    if (!statusData) return;

    const isEnabled = Boolean(statusData.enabled);

    if (badge) {
      badge.className = `proxy-badge ${isEnabled ? 'online' : 'offline'}`;
    }
    if (stateText) {
      stateText.textContent = isEnabled ? 'RECORDING ACTIVE' : 'LOGGING OFF';
    }
    if (filePathVal) {
      filePathVal.textContent = statusData.rel_path || 'logger/payloads.jsonl';
    }
    if (fileSizeVal) {
      fileSizeVal.textContent = statusData.file_size_formatted || '0 B';
    }
    if (toggleBtn && toggleBtnText) {
      if (isEnabled) {
        toggleBtn.className = 'btn-proxy btn-stop';
        toggleBtnText.textContent = 'Disable Raw Logging';
      } else {
        toggleBtn.className = 'btn-proxy btn-start';
        toggleBtnText.textContent = 'Enable Raw Logging';
      }
    }
  }

  /**
   * Show Raw Log Action Alert in Control Panel
   */
  function showRawLogAlert(message, type = 'info', durationMs = 5000) {
    const alertEl = document.getElementById('rawLogActionAlert');
    if (!alertEl) return;

    alertEl.className = `proxy-alert ${type}`;
    alertEl.textContent = message;
    alertEl.style.display = 'flex';

    if (durationMs > 0) {
      setTimeout(() => {
        if (alertEl.textContent === message) {
          alertEl.style.display = 'none';
        }
      }, durationMs);
    }
  }

  return {
    formatNum,
    formatMs,
    formatTps,
    toLocalISOString,
    formatShortTime,
    formatShortDate,
    renderSummary,
    renderServerStatus,
    renderCrossCheck,
    renderHealth,
    renderCallsTable,
    renderModelTable,
    renderGroupedTable,
    setupCustomDropdown,
    renderPerformanceAnalyzer,
    formatCost,
    renderCostSummary,
    renderCostTables,
    renderProxyStatus,
    renderProxyLogs,
    showProxyAlert,
    renderRawLogStatus,
    showRawLogAlert,
    getModelClass
  };
})();


