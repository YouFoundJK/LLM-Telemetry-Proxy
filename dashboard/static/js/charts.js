/**
 * Telemetry Dashboard Charts Module
 * Manages Chart.js instances and rendering configurations.
 */

const TelemetryCharts = (() => {
  // Force Chart.js to use en-US locale globally for tooltips and ticks
  if (typeof Chart !== 'undefined') {
    Chart.defaults.locale = 'en-US';
  }

  // Store chart instances to destroy them before reloading
  const instances = {};

  // Theme-compliant colors
  const COLORS = {
    grid: '#21262d',
    text: '#8b949e',
    accent: '#58a6ff',  // Blue
    cyan: '#39d2c0',    // Cyan
    purple: '#bc8cff',  // Purple
    red: '#f85149',     // Red
    green: '#3fb950',   // Green
    orange: '#d29922',  // Orange
    pink: '#ff7b72',    // Pink/Coral
    yellow: '#f1e05a',  // Yellow/Gold
    teal: '#00a3a6',    // Teal
    magenta: '#d75fbf'  // Magenta
  };

  // Dynamic model-to-color assignment registry
  const modelColorRegistry = {};

  // Standard model prefix mapping to preferred colors
  const PREFERRED_MODEL_COLORS = [
    { pattern: 'glm', colorName: 'accent' },
    { pattern: 'qwen', colorName: 'cyan' },
    { pattern: 'gemma', colorName: 'purple' },
    { pattern: 'deepseek', colorName: 'orange' },
    { pattern: 'gpt', colorName: 'pink' }
  ];

  // List of colors to cycle through for unique models
  const PALETTE = [
    'accent',
    'cyan',
    'purple',
    'orange',
    'pink',
    'red',
    'green',
    'yellow',
    'teal',
    'magenta'
  ];

  /**
   * Helper to map model names to theme colors uniquely and dynamically.
   */
  function getModelColor(model) {
    if (!model) return COLORS.text;
    const m = model.toLowerCase().trim();

    // Check if already registered
    if (modelColorRegistry[m]) {
      return COLORS[modelColorRegistry[m]] || modelColorRegistry[m];
    }

    let assignedColor = null;

    // Check preferred prefix mappings first, but only if that color is not already in use
    for (const item of PREFERRED_MODEL_COLORS) {
      if (m.includes(item.pattern)) {
        const usedColors = new Set(Object.values(modelColorRegistry));
        if (!usedColors.has(item.colorName)) {
          assignedColor = item.colorName;
        }
        break;
      }
    }

    // If no preferred color or it was already used, find the first unused color from the PALETTE
    if (!assignedColor) {
      const usedColors = new Set(Object.values(modelColorRegistry));
      for (const colName of PALETTE) {
        if (!usedColors.has(colName)) {
          assignedColor = colName;
          break;
        }
      }
    }

    // If all colors in PALETTE are used, cycle/recycle them using the index
    if (!assignedColor) {
      const index = Object.keys(modelColorRegistry).length % PALETTE.length;
      assignedColor = PALETTE[index];
    }

    modelColorRegistry[m] = assignedColor;
    return COLORS[assignedColor];
  }

  /**
   * Destroy all active chart instances.
   */
  function destroyAll() {
    Object.keys(instances).forEach(key => {
      if (instances[key]) {
        instances[key].destroy();
        instances[key] = null;
      }
    });
  }

  /**
   * Helper to format numbers.
   */
  function fmtNum(n) {
    if (n === null || n === undefined) return '—';
    if (n >= 1e9) return (n/1e9).toFixed(2) + 'B';
    if (n >= 1e6) return (n/1e6).toFixed(2) + 'M';
    if (n >= 1e3) return (n/1e3).toFixed(1) + 'K';
    return n.toFixed(0);
  }

  /**
   * Render all dashboard charts with filtered data.
   */
  function renderAll(calls, tokenMetricType = 'input', timeRange = null) {
    destroyAll();
    if (!calls || !calls.length) return;

    // 1. Token Usage Over Time (Stacked by Model)
    renderTokenChart(calls, tokenMetricType, timeRange);

    // 2. RTT Distribution (Histogram)
    renderRttChart(calls);

    // 3. TTFB Distribution (Histogram)
    renderTtfbChart(calls);

    // 4. Server Load vs RTT (Scatter)
    renderLoadChart(calls);

    // 5. Throughput (tokens/sec) Over Time (Scatter)
    renderThroughputChart(calls, timeRange);

    // 6. Input Token Size Distribution (Histogram)
    renderInputSizeChart(calls);

    // 7. Errors Timeline (Bar)
    renderErrorChart(calls, timeRange);
  }

  /**
   * Helper to calculate bucket interval in minutes based on total timespan of data.
   */
  function getIntervalMinutes(calls, timeRange = null) {
    let minTime, maxTime;
    if (timeRange && timeRange.minTime && timeRange.maxTime) {
      minTime = timeRange.minTime;
      maxTime = timeRange.maxTime;
    } else {
      if (!calls || calls.length <= 1) return 60;
      const timestamps = calls.map(c => new Date(c.timestamp).getTime());
      minTime = Math.min(...timestamps);
      maxTime = Math.max(...timestamps);
    }
    const spanMs = maxTime - minTime;
    const spanHours = spanMs / (3600 * 1000);

    if (spanHours <= 1.5) return 15;       // 1 hour range -> 15 min resolution
    if (spanHours <= 6.5) return 30;       // 6 hour range -> 30 min resolution
    if (spanHours <= 25) return 60;        // 24 hour range -> 1 hour resolution
    if (spanHours <= 24 * 7.5) return 360; // 7 day range -> 6 hour resolution
    return 1440;                           // larger -> 1 day resolution
  }

  /**
   * Helper to round a timestamp down to the nearest bucket interval.
   */
  function getBucketKey(timestamp, intervalMinutes) {
    const d = new Date(timestamp);
    if (intervalMinutes < 60) {
      const minutes = d.getMinutes();
      const roundedMinutes = Math.floor(minutes / intervalMinutes) * intervalMinutes;
      d.setMinutes(roundedMinutes);
      d.setSeconds(0);
      d.setMilliseconds(0);
    } else if (intervalMinutes < 1440) {
      const hours = d.getHours();
      const intervalHours = intervalMinutes / 60;
      const roundedHours = Math.floor(hours / intervalHours) * intervalHours;
      d.setHours(roundedHours);
      d.setMinutes(0);
      d.setSeconds(0);
      d.setMilliseconds(0);
    } else {
      d.setHours(0);
      d.setMinutes(0);
      d.setSeconds(0);
      d.setMilliseconds(0);
    }
    return d.getTime();
  }

  /**
   * Helper to generate a complete list of bucket keys (timestamps) spanning min to max calls.
   */
  function getTimeBuckets(calls, intervalMinutes, timeRange = null) {
    let minTime, maxTime;
    if (timeRange && timeRange.minTime && timeRange.maxTime) {
      minTime = timeRange.minTime;
      maxTime = timeRange.maxTime;
    } else {
      if (!calls || calls.length === 0) return [];
      const timestamps = calls.map(c => new Date(c.timestamp).getTime());
      minTime = Math.min(...timestamps);
      maxTime = Math.max(...timestamps);
    }
    
    const buckets = [];
    let current = getBucketKey(minTime, intervalMinutes);
    const end = getBucketKey(maxTime, intervalMinutes);
    
    const maxSteps = 1000;
    let steps = 0;
    while (current <= end && steps < maxSteps) {
      buckets.push(current);
      current += intervalMinutes * 60 * 1000;
      steps++;
    }
    return buckets;
  }

  /**
   * Token Usage Chart
   */
  function renderTokenChart(calls, tokenMetricType, timeRange = null) {
    const intervalMinutes = getIntervalMinutes(calls, timeRange);
    const buckets = getTimeBuckets(calls, intervalMinutes, timeRange);

    const byBucket = {};
    buckets.forEach(b => {
      byBucket[b] = {};
    });

    calls.forEach(c => {
      let tokens = 0;
      if (tokenMetricType === 'input') tokens = c.input_tokens || 0;
      else if (tokenMetricType === 'output') tokens = c.output_tokens || 0;
      else tokens = (c.input_tokens || 0) + (c.output_tokens || 0);

      if (tokens <= 0) return;
      if (!c.model || c.model === 'unknown') return; // Exclude unknown/missing model

      const bucketKey = getBucketKey(c.timestamp, intervalMinutes);
      const m = c.model;

      if (!byBucket[bucketKey]) byBucket[bucketKey] = {};
      byBucket[bucketKey][m] = (byBucket[bucketKey][m] || 0) + tokens;
    });

    const sortedBuckets = Object.keys(byBucket).map(Number).sort((a, b) => a - b);
    const models = [...new Set(calls.map(c => c.model).filter(m => m && m !== 'unknown'))].sort();
    
    const datasets = models.map(m => ({
      label: m,
      data: sortedBuckets.map(b => byBucket[b][m] || 0),
      backgroundColor: getModelColor(m) + '30',
      borderColor: getModelColor(m),
      borderWidth: 1.5,
      fill: true,
      tension: 0.2
    }));

    const ctx = document.getElementById('tokenChart');
    if (!ctx) return;

    if (instances.token) {
      instances.token.destroy();
    }

    instances.token = new Chart(ctx, {
      type: 'line',
      data: {
        labels: sortedBuckets.map(b => UI.formatShortDate(b)),
        datasets
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { labels: { color: COLORS.text, font: { family: 'Outfit' } } }
        },
        scales: {
          x: {
            ticks: { color: COLORS.text, maxRotation: 45, autoSkip: true, maxTicksLimit: 12 },
            grid: { color: COLORS.grid }
          },
          y: {
            ticks: { color: COLORS.text, callback: v => fmtNum(v) },
            grid: { color: COLORS.grid }
          }
        },
        interaction: { mode: 'index', intersect: false }
      }
    });
  }

  /**
   * RTT Distribution Chart
   */
  function renderRttChart(calls) {
    const rttBuckets = { '<1s': 0, '1-3s': 0, '3-10s': 0, '10-30s': 0, '30-60s': 0, '60-120s': 0, '>120s': 0 };
    calls.forEach(c => {
      if (!c.total_ms) return;
      const cnt = c.calls_count !== undefined && c.calls_count !== null ? c.calls_count : 1;
      const s = c.total_ms / 1000;
      if (s < 1) rttBuckets['<1s'] += cnt;
      else if (s < 3) rttBuckets['1-3s'] += cnt;
      else if (s < 10) rttBuckets['3-10s'] += cnt;
      else if (s < 30) rttBuckets['10-30s'] += cnt;
      else if (s < 60) rttBuckets['30-60s'] += cnt;
      else if (s < 120) rttBuckets['60-120s'] += cnt;
      else rttBuckets['>120s'] += cnt;
    });

    const ctx = document.getElementById('rttChart');
    if (!ctx) return;

    instances.rtt = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: Object.keys(rttBuckets),
        datasets: [{
          label: 'Calls',
          data: Object.values(rttBuckets),
          backgroundColor: COLORS.accent + '80',
          borderColor: COLORS.accent,
          borderWidth: 1,
          borderRadius: 4
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: COLORS.text }, grid: { color: COLORS.grid } },
          y: { ticks: { color: COLORS.text, precision: 0 }, grid: { color: COLORS.grid } }
        }
      }
    });
  }

  /**
   * TTFB Distribution Chart
   */
  function renderTtfbChart(calls) {
    const ttfbBuckets = { '<500ms': 0, '500ms-1s': 0, '1-3s': 0, '3-10s': 0, '10-30s': 0, '>30s': 0 };
    calls.forEach(c => {
      if (!c.ttfb_ms) return;
      const cnt = c.calls_count !== undefined && c.calls_count !== null ? c.calls_count : 1;
      const ms = c.ttfb_ms;
      if (ms < 500) ttfbBuckets['<500ms'] += cnt;
      else if (ms < 1000) ttfbBuckets['500ms-1s'] += cnt;
      else if (ms < 3000) ttfbBuckets['1-3s'] += cnt;
      else if (ms < 10000) ttfbBuckets['3-10s'] += cnt;
      else if (ms < 30000) ttfbBuckets['10-30s'] += cnt;
      else ttfbBuckets['>30s'] += cnt;
    });

    const ctx = document.getElementById('ttfbChart');
    if (!ctx) return;

    instances.ttfb = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: Object.keys(ttfbBuckets),
        datasets: [{
          label: 'Calls',
          data: Object.values(ttfbBuckets),
          backgroundColor: COLORS.cyan + '80',
          borderColor: COLORS.cyan,
          borderWidth: 1,
          borderRadius: 4
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: COLORS.text }, grid: { color: COLORS.grid } },
          y: { ticks: { color: COLORS.text, precision: 0 }, grid: { color: COLORS.grid } }
        }
      }
    });
  }

  /**
   * Server Load vs RTT Scatter Chart
   */
  function renderLoadChart(calls) {
    const loadPoints = calls
      .filter(c => c.server_running !== null && c.total_ms)
      .map(c => ({
        x: c.server_running,
        y: c.total_ms / 1000,
        model: c.model || 'unknown'
      }));

    const ctx = document.getElementById('loadChart');
    if (!ctx) return;

    // Resolve color mapping for individual data points
    const pointBackgroundColors = loadPoints.map(p => getModelColor(p.model) + '60');
    const pointBorderColors = loadPoints.map(p => getModelColor(p.model));

    instances.load = new Chart(ctx, {
      type: 'scatter',
      data: {
        datasets: [{
          label: 'Calls',
          data: loadPoints,
          backgroundColor: pointBackgroundColors,
          borderColor: pointBorderColors,
          pointRadius: 4,
          pointHoverRadius: 6
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: ctx => {
                const p = ctx.raw;
                return `${p.model}: Load=${p.x}, RTT=${p.y.toFixed(2)}s`;
              }
            }
          }
        },
        scales: {
          x: {
            title: { display: true, text: 'Server Load (running tasks)', color: COLORS.text },
            ticks: { color: COLORS.text },
            grid: { color: COLORS.grid }
          },
          y: {
            title: { display: true, text: 'RTT (seconds)', color: COLORS.text },
            ticks: { color: COLORS.text },
            grid: { color: COLORS.grid }
          }
        }
      }
    });
  }

  /**
   * Throughput Scatter Chart
   */
  function renderThroughputChart(calls, timeRange = null) {
    const tpsData = calls
      .filter(c => c.tokens_per_s && c.tokens_per_s > 0)
      .map(c => ({
        x: new Date(c.timestamp).getTime(),
        y: c.tokens_per_s,
        model: c.model || 'unknown'
      }));

    const ctx = document.getElementById('tpsChart');
    if (!ctx) return;

    // Correctly resolve background & border colors for the scatter points using mapped array!
    const pointBackgroundColors = tpsData.map(d => getModelColor(d.model) + '60');
    const pointBorderColors = tpsData.map(d => getModelColor(d.model));

    instances.throughput = new Chart(ctx, {
      type: 'scatter',
      data: {
        datasets: [{
          label: 'Throughput',
          data: tpsData,
          backgroundColor: pointBackgroundColors,
          borderColor: pointBorderColors,
          pointRadius: 3,
          pointHoverRadius: 5
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: ctx => {
                const p = ctx.raw;
                return `${p.model}: ${p.y.toFixed(1)} tok/s`;
              }
            }
          }
        },
        scales: {
          x: {
            type: 'linear',
            title: { display: true, text: 'Time', color: COLORS.text },
            min: timeRange ? timeRange.minTime : undefined,
            max: timeRange ? timeRange.maxTime : undefined,
            ticks: {
              color: COLORS.text,
              callback: v => UI.formatShortTime(v)
            },
            grid: { color: COLORS.grid }
          },
          y: {
            title: { display: true, text: 'tokens/sec', color: COLORS.text },
            ticks: { color: COLORS.text },
            grid: { color: COLORS.grid }
          }
        }
      }
    });
  }

  /**
   * Input Size Distribution Chart
   */
  function renderInputSizeChart(calls) {
    const sizeBuckets = { '<1K': 0, '1-10K': 0, '10-50K': 0, '50-100K': 0, '100-150K': 0, '150-200K': 0, '>200K': 0 };
    calls.forEach(c => {
      if (!c.input_tokens || c.input_tokens <= 0) return;
      const cnt = c.calls_count !== undefined && c.calls_count !== null ? c.calls_count : 1;
      const t = c.input_tokens / cnt; // Token size per call
      if (t < 1000) sizeBuckets['<1K'] += cnt;
      else if (t < 10000) sizeBuckets['1-10K'] += cnt;
      else if (t < 50000) sizeBuckets['10-50K'] += cnt;
      else if (t < 100000) sizeBuckets['50-100K'] += cnt;
      else if (t < 150000) sizeBuckets['100-150K'] += cnt;
      else if (t < 200000) sizeBuckets['150-200K'] += cnt;
      else sizeBuckets['>200K'] += cnt;
    });

    const ctx = document.getElementById('inputSizeChart');
    if (!ctx) return;

    instances.inputSize = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: Object.keys(sizeBuckets),
        datasets: [{
          label: 'Calls',
          data: Object.values(sizeBuckets),
          backgroundColor: COLORS.purple + '80',
          borderColor: COLORS.purple,
          borderWidth: 1,
          borderRadius: 4
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: COLORS.text }, grid: { color: COLORS.grid } },
          y: { ticks: { color: COLORS.text, precision: 0 }, grid: { color: COLORS.grid } }
        }
      }
    });
  }

  /**
   * Errors Timeline Chart
   */
  function renderErrorChart(calls, timeRange = null) {
    const intervalMinutes = getIntervalMinutes(calls, timeRange);
    const buckets = getTimeBuckets(calls, intervalMinutes, timeRange);

    const byBucket = {};
    buckets.forEach(b => {
      byBucket[b] = 0;
    });

    const errors = calls.filter(c => c.error);
    errors.forEach(c => {
      const cnt = c.calls_count !== undefined && c.calls_count !== null ? c.calls_count : 1;
      const bucketKey = getBucketKey(c.timestamp, intervalMinutes);
      if (byBucket[bucketKey] !== undefined) {
        byBucket[bucketKey] += cnt;
      } else {
        byBucket[bucketKey] = cnt;
      }
    });

    const sortedBuckets = Object.keys(byBucket).map(Number).sort((a, b) => a - b);

    const ctx = document.getElementById('errorChart');
    if (!ctx) return;

    instances.error = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: sortedBuckets.map(b => UI.formatShortDate(b)),
        datasets: [{
          label: 'Errors',
          data: sortedBuckets.map(b => byBucket[b]),
          backgroundColor: COLORS.red + '80',
          borderColor: COLORS.red,
          borderWidth: 1,
          borderRadius: 4
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: COLORS.text, maxRotation: 45 }, grid: { color: COLORS.grid } },
          y: { ticks: { color: COLORS.text, stepSize: 1, precision: 0 }, grid: { color: COLORS.grid }, beginAtZero: true }
        }
      }
    });
  }

  /**
   * Render Analyzer Token Trend Chart (Input vs Output vs Total)
   */
  function renderAnalyzerTokenTrendChart(calls, timeRange = null) {
    const intervalMinutes = getIntervalMinutes(calls, timeRange);
    const buckets = getTimeBuckets(calls, intervalMinutes, timeRange);

    const byBucket = {};
    buckets.forEach(b => {
      byBucket[b] = { input: 0, output: 0, total: 0 };
    });

    calls.forEach(c => {
      const bucketKey = getBucketKey(c.timestamp, intervalMinutes);
      if (byBucket[bucketKey] === undefined) return;
      const inp = c.input_tokens || 0;
      const out = c.output_tokens || 0;
      const callsCount = (c.calls_count !== undefined && c.calls_count !== null ? c.calls_count : 1);
      byBucket[bucketKey].input += inp;
      byBucket[bucketKey].output += out;
      byBucket[bucketKey].total += (inp + out);
    });

    const sortedBuckets = Object.keys(byBucket).map(Number).sort((a, b) => a - b);
    const labels = sortedBuckets.map(b => UI.formatShortDate(b));

    const datasets = [
      {
        label: 'Input Tokens',
        data: sortedBuckets.map(b => byBucket[b].input),
        borderColor: COLORS.accent,
        backgroundColor: COLORS.accent + '15',
        borderWidth: 2,
        tension: 0.2,
        fill: true
      },
      {
        label: 'Output Tokens',
        data: sortedBuckets.map(b => byBucket[b].output),
        borderColor: COLORS.purple,
        backgroundColor: COLORS.purple + '15',
        borderWidth: 2,
        tension: 0.2,
        fill: true
      },
      {
        label: 'Total Tokens',
        data: sortedBuckets.map(b => byBucket[b].total),
        borderColor: COLORS.green,
        backgroundColor: COLORS.green + '10',
        borderWidth: 2.5,
        borderDash: [5, 5],
        tension: 0.2,
        fill: false
      }
    ];

    const ctx = document.getElementById('analyzerTokenTrendChart');
    if (!ctx) return;

    if (instances.analyzerTokenTrend) {
      instances.analyzerTokenTrend.destroy();
    }

    instances.analyzerTokenTrend = new Chart(ctx, {
      type: 'line',
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { labels: { color: COLORS.text, font: { family: 'Outfit' } } }
        },
        scales: {
          x: { ticks: { color: COLORS.text, maxRotation: 45 }, grid: { color: COLORS.grid } },
          y: { ticks: { color: COLORS.text, callback: v => fmtNum(v) }, grid: { color: COLORS.grid }, beginAtZero: true }
        }
      }
    });
  }

  /**
   * Render Average Tokens per Call Chart by Model
   */
  function renderAnalyzerAvgTokenChart(calls, timeRange = null) {
    const intervalMinutes = getIntervalMinutes(calls, timeRange);
    const buckets = getTimeBuckets(calls, intervalMinutes, timeRange);

    const byBucket = {};
    buckets.forEach(b => {
      byBucket[b] = {};
    });

    calls.forEach(c => {
      if (!c.model || c.model === 'unknown') return;
      const bucketKey = getBucketKey(c.timestamp, intervalMinutes);
      if (byBucket[bucketKey] === undefined) return;
      const m = c.model;
      const totalTok = (c.input_tokens || 0) + (c.output_tokens || 0);
      const callsCount = (c.calls_count !== undefined && c.calls_count !== null ? c.calls_count : 1);
      
      if (!byBucket[bucketKey][m]) {
        byBucket[bucketKey][m] = { sum: 0, count: 0 };
      }
      byBucket[bucketKey][m].sum += totalTok;
      byBucket[bucketKey][m].count += callsCount;
    });

    const sortedBuckets = Object.keys(byBucket).map(Number).sort((a, b) => a - b);
    const labels = sortedBuckets.map(b => UI.formatShortDate(b));
    const models = [...new Set(calls.map(c => c.model).filter(m => m && m !== 'unknown'))].sort();

    const datasets = models.map(m => {
      return {
        label: m,
        data: sortedBuckets.map(b => {
          const entry = byBucket[b][m];
          return entry && entry.count > 0 ? (entry.sum / entry.count) : null;
        }),
        borderColor: getModelColor(m),
        backgroundColor: 'transparent',
        borderWidth: 2,
        tension: 0.2,
        spanGaps: true
      };
    });

    const ctx = document.getElementById('analyzerAvgTokenChart');
    if (!ctx) return;

    if (instances.analyzerAvgToken) {
      instances.analyzerAvgToken.destroy();
    }

    instances.analyzerAvgToken = new Chart(ctx, {
      type: 'line',
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { labels: { color: COLORS.text, font: { family: 'Outfit' } } }
        },
        scales: {
          x: { ticks: { color: COLORS.text, maxRotation: 45 }, grid: { color: COLORS.grid } },
          y: {
            title: { display: true, text: 'Tokens per Call', color: COLORS.text, font: { family: 'Outfit' } },
            ticks: { color: COLORS.text, callback: v => fmtNum(v) },
            grid: { color: COLORS.grid },
            beginAtZero: true
          }
        }
      }
    });
  }

  /**
   * Render Hourly Analysis Charts
   */
  function renderAnalyzerCharts(hourData, calls, timeRange = null) {
    if (!hourData) return;

    // Destroy existing instances if any to prevent memory leaks
    if (instances.hourlyVolume) { instances.hourlyVolume.destroy(); instances.hourlyVolume = null; }
    if (instances.hourlyLatency) { instances.hourlyLatency.destroy(); instances.hourlyLatency = null; }
    if (instances.analyzerTokenTrend) { instances.analyzerTokenTrend.destroy(); instances.analyzerTokenTrend = null; }
    if (instances.analyzerAvgToken) { instances.analyzerAvgToken.destroy(); instances.analyzerAvgToken = null; }

    const labels = hourData.map(d => `${String(d.hour).padStart(2, '0')}:00`);
    const callsData = hourData.map(d => d.calls);
    const errorsData = hourData.map(d => d.errors);
    const rttData = hourData.map(d => d.rttCount ? (d.rttSum / d.rttCount) : 0);
    const ttfbData = hourData.map(d => d.ttfbCount ? ((d.ttfbSum / d.ttfbCount) / 1000) : 0); // Convert to seconds for shared axis

    // 1. Volume & Errors Chart
    const ctxVol = document.getElementById('hourlyVolumeChart');
    if (ctxVol) {
      instances.hourlyVolume = new Chart(ctxVol, {
        type: 'bar',
        data: {
          labels,
          datasets: [
            {
              label: 'Total Calls',
              data: callsData,
              backgroundColor: COLORS.accent + '80',
              borderColor: COLORS.accent,
              borderWidth: 1,
              borderRadius: 4
            },
            {
              label: 'Errors (Rate Limiting)',
              data: errorsData,
              backgroundColor: COLORS.red + '80',
              borderColor: COLORS.red,
              borderWidth: 1,
              borderRadius: 4
            }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { labels: { color: COLORS.text, font: { family: 'Outfit' } } }
          },
          scales: {
            x: { ticks: { color: COLORS.text }, grid: { color: COLORS.grid } },
            y: { ticks: { color: COLORS.text, precision: 0 }, grid: { color: COLORS.grid }, beginAtZero: true }
          }
        }
      });
    }

    // 2. Latency Chart (RTT & TTFT in seconds)
    const ctxLat = document.getElementById('hourlyLatencyChart');
    if (ctxLat) {
      instances.hourlyLatency = new Chart(ctxLat, {
        type: 'line',
        data: {
          labels,
          datasets: [
            {
              label: 'Total Latency (Avg RTT)',
              data: rttData,
              borderColor: COLORS.accent,
              backgroundColor: COLORS.accent + '20',
              borderWidth: 2,
              tension: 0.3,
              fill: false
            },
            {
              label: 'Startup Latency (Avg TTFT)',
              data: ttfbData,
              borderColor: COLORS.cyan,
              backgroundColor: COLORS.cyan + '20',
              borderWidth: 2,
              tension: 0.3,
              fill: false
            }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { labels: { color: COLORS.text, font: { family: 'Outfit' } } }
          },
          scales: {
            x: { ticks: { color: COLORS.text }, grid: { color: COLORS.grid } },
            y: {
              title: { display: true, text: 'Time (Seconds)', color: COLORS.text, font: { family: 'Outfit' } },
              ticks: { color: COLORS.text },
              grid: { color: COLORS.grid },
              beginAtZero: true
            }
          }
        }
      });
    }

    // 3. Token Trend & Avg Token per Call Charts
    if (calls) {
      renderAnalyzerTokenTrendChart(calls, timeRange);
      renderAnalyzerAvgTokenChart(calls, timeRange);
    }
  }

  function renderCostCharts(calls, modelCosts, timeRange = null) {
    if (!calls || !calls.length) return;
    renderCostOverTimeChart(calls, timeRange);
    renderCostShareChart(calls);
  }

  function renderCostOverTimeChart(calls, timeRange = null) {
    const intervalMinutes = getIntervalMinutes(calls, timeRange);
    const buckets = getTimeBuckets(calls, intervalMinutes, timeRange);

    const byBucket = {};
    buckets.forEach(b => {
      byBucket[b] = {};
    });

    calls.forEach(c => {
      if (!c.model || c.model === 'unknown') return;
      const bucketKey = getBucketKey(c.timestamp, intervalMinutes);
      const m = c.model;
      const cost = c.total_cost || 0;
      byBucket[bucketKey][m] = (byBucket[bucketKey][m] || 0) + cost;
    });

    const sortedBuckets = Object.keys(byBucket).map(Number).sort((a, b) => a - b);
    const models = [...new Set(calls.map(c => c.model).filter(m => m && m !== 'unknown'))].sort();

    const datasets = models.map(m => {
      const dataPoints = sortedBuckets.map(b => byBucket[b][m] || 0);

      return {
        label: m,
        data: dataPoints,
        backgroundColor: getModelColor(m) + '20',
        borderColor: getModelColor(m),
        borderWidth: 2,
        fill: true,
        tension: 0.2
      };
    });

    const labels = sortedBuckets.map(b => UI.formatShortDate(b));

    const ctx = document.getElementById('costOverTimeChart');
    if (!ctx) return;

    if (instances.costOverTime) {
      instances.costOverTime.destroy();
    }

    instances.costOverTime = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: {
            position: 'top',
            labels: { color: COLORS.text, font: { family: 'Outfit' } }
          },
          tooltip: {
            callbacks: {
              label: function(context) {
                return `${context.dataset.label}: $${context.raw.toFixed(4)}`;
              }
            }
          }
        },
        scales: {
          x: {
            grid: { color: COLORS.grid },
            ticks: { color: COLORS.text, maxRotation: 45, minRotation: 45 }
          },
          y: {
            grid: { color: COLORS.grid },
            ticks: {
              color: COLORS.text,
              callback: function(value) {
                return '$' + value.toFixed(2);
              }
            },
            title: {
              display: true,
              text: 'Inference Cost (USD)',
              color: COLORS.text,
              font: { family: 'Outfit' }
            },
            beginAtZero: true
          }
        }
      }
    });
  }

  function renderCostShareChart(calls) {
    const byModel = {};
    calls.forEach(c => {
      if (!c.model || c.model === 'unknown') return;
      const m = c.model;
      byModel[m] = (byModel[m] || 0) + (c.total_cost || 0);
    });

    const sortedModels = Object.keys(byModel).sort((a, b) => byModel[b] - byModel[a]);
    const data = sortedModels.map(m => byModel[m]);
    const backgroundColors = sortedModels.map(m => getModelColor(m));

    const ctx = document.getElementById('costShareChart');
    if (!ctx) return;

    if (instances.costShare) {
      instances.costShare.destroy();
    }

    const totalCost = data.reduce((a, b) => a + b, 0);
    if (totalCost === 0) {
      instances.costShare = new Chart(ctx, {
        type: 'doughnut',
        data: {
          labels: ['No cost data'],
          datasets: [{
            data: [1],
            backgroundColor: ['#21262d'],
            borderColor: ['#30363d']
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false }
          }
        }
      });
      return;
    }

    instances.costShare = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: sortedModels,
        datasets: [{
          data,
          backgroundColor: backgroundColors.map(c => c + 'cc'),
          borderColor: backgroundColors,
          borderWidth: 1
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: {
            position: 'right',
            labels: { color: COLORS.text, font: { family: 'Outfit' } }
          },
          tooltip: {
            callbacks: {
              label: function(context) {
                const val = context.raw;
                const pct = ((val / totalCost) * 100).toFixed(1);
                return ` ${context.label}: $${val.toFixed(4)} (${pct}%)`;
              }
            }
          }
        }
      }
    });
  }

  return {
    renderAll,
    destroyAll,
    getModelColor,
    COLORS,
    renderAnalyzerCharts,
    renderCostCharts,
    renderTokenChart
  };
})();
