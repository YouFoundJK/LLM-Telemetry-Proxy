/**
 * Telemetry Dashboard Charts Module
 * Manages Chart.js instances and rendering configurations with smart model aggregation.
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
    { pattern: 'kimi', colorName: 'teal' },
    { pattern: 'deepseek', colorName: 'orange' },
    { pattern: 'gpt', colorName: 'pink' },
    { pattern: 'qwen', colorName: 'cyan' },
    { pattern: 'gemma', colorName: 'purple' }
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

    if (m === 'other' || m === 'other models' || m.startsWith('other')) {
      return '#6e7681'; // Neutral slate grey for aggregated other models
    }
    if (m === 'system / non-inference') {
      return '#8b949e';
    }

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
   * Smart Model Aggregator for Time Series & Breakdown Charts:
   * - Eliminates models with zero/negative totals.
   * - Orders models descending by total contribution.
   * - If not explicitly user-filtered and there are > maxModels (default 6) or long-tail noise (< minSharePct):
   *   retains top (maxModels - 1) models and aggregates the remaining minor models into 'Other Models'.
   * - Returns: { models: string[], byBucket: object, modelTotals: object }
   */
  function prepareModelSeries(byBucket, sortedBuckets, isUserFiltered = false, maxModels = 6, minSharePct = 0.005) {
    const modelTotals = {};

    // 1. Calculate sum of metric for each model across all buckets
    sortedBuckets.forEach(b => {
      const bucketObj = byBucket[b] || {};
      Object.keys(bucketObj).forEach(m => {
        const val = bucketObj[m] || 0;
        if (val > 0) {
          modelTotals[m] = (modelTotals[m] || 0) + val;
        }
      });
    });

    // 2. Filter out models with total <= 0 and sort descending
    const activeModels = Object.keys(modelTotals)
      .filter(m => (modelTotals[m] || 0) > 0)
      .sort((a, b) => modelTotals[b] - modelTotals[a]);

    if (activeModels.length === 0) {
      return { models: [], byBucket, modelTotals: {} };
    }

    // If user explicitly selected specific models in the dropdown filter, or <= maxModels:
    // keep all active selected models directly without grouping into "Other Models"
    if (isUserFiltered || activeModels.length <= maxModels) {
      return { models: activeModels, byBucket, modelTotals };
    }

    // 3. Smart Top-N + "Other Models" aggregation
    const topLimit = maxModels - 1; // e.g. top 5
    const topModels = [];
    const otherModels = [];

    activeModels.forEach((m, idx) => {
      if (idx < topLimit) {
        topModels.push(m);
      } else {
        otherModels.push(m);
      }
    });

    if (otherModels.length === 0) {
      return { models: topModels, byBucket, modelTotals };
    }

    // Clone byBucket to avoid mutating caller reference
    const newByBucket = {};
    sortedBuckets.forEach(b => {
      newByBucket[b] = {};
      const bucketObj = byBucket[b] || {};

      topModels.forEach(m => {
        if (bucketObj[m] !== undefined) {
          newByBucket[b][m] = bucketObj[m];
        }
      });

      let otherSum = 0;
      otherModels.forEach(m => {
        otherSum += (bucketObj[m] || 0);
      });
      if (otherSum > 0) {
        newByBucket[b]['Other Models'] = otherSum;
      }
    });

    const otherTotal = otherModels.reduce((acc, m) => acc + (modelTotals[m] || 0), 0);
    if (otherTotal > 0) {
      modelTotals['Other Models'] = otherTotal;
      topModels.push('Other Models');
    }

    return {
      models: topModels,
      byBucket: newByBucket,
      modelTotals
    };
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
  function renderAll(calls, tokenMetricType = 'input', timeRange = null, isUserFiltered = false) {
    destroyAll();
    if (!calls || !calls.length) return;

    // 1. Token Usage Over Time (Stacked by Model)
    renderTokenChart(calls, tokenMetricType, timeRange, isUserFiltered);

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
  function renderTokenChart(calls, tokenMetricType = 'input', timeRange = null, isUserFiltered = false) {
    const intervalMinutes = getIntervalMinutes(calls, timeRange);
    const buckets = getTimeBuckets(calls, intervalMinutes, timeRange);
    const bucketSet = new Set(buckets);

    const rawByBucket = {};
    buckets.forEach(b => {
      rawByBucket[b] = {};
    });

    (calls || []).forEach(c => {
      let tokens = 0;
      if (tokenMetricType === 'input') tokens = c.input_tokens || 0;
      else if (tokenMetricType === 'output') tokens = c.output_tokens || 0;
      else tokens = (c.input_tokens || 0) + (c.output_tokens || 0);

      if (tokens <= 0) return;
      if (!c.model || c.model === 'unknown') return;

      const bucketKey = getBucketKey(c.timestamp, intervalMinutes);
      if (!bucketSet.has(bucketKey)) return;

      const m = c.model;
      rawByBucket[bucketKey][m] = (rawByBucket[bucketKey][m] || 0) + tokens;
    });

    const sortedBuckets = buckets;
    const { models, byBucket } = prepareModelSeries(rawByBucket, sortedBuckets, isUserFiltered, 6, 0.005);

    const datasets = models.map(m => ({
      label: m,
      data: sortedBuckets.map(b => byBucket[b][m] || 0),
      backgroundColor: m === 'Other Models' ? '#6e768120' : getModelColor(m) + '30',
      borderColor: getModelColor(m),
      borderWidth: m === 'Other Models' ? 1.5 : 2,
      borderDash: m === 'Other Models' ? [4, 4] : undefined,
      fill: true,
      tension: 0.2,
      pointRadius: (ctx) => (Number(ctx.raw) > 0 ? 3 : 0),
      pointHoverRadius: (ctx) => (Number(ctx.raw) > 0 ? 5 : 0),
      pointHitRadius: 10
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
          legend: {
            display: datasets.length > 0,
            labels: { color: COLORS.text, font: { family: 'Outfit' } }
          },
          tooltip: {
            filter: (tooltipItem) => (Number(tooltipItem.raw) || 0) > 0,
            itemSort: (a, b) => (Number(b.raw) || 0) - (Number(a.raw) || 0),
            callbacks: {
              label: function(context) {
                let label = context.dataset.label || '';
                if (label) label += ': ';
                const val = Number(context.raw) || 0;
                return label + val.toLocaleString('en-US');
              }
            }
          }
        },
        scales: {
          x: {
            ticks: { color: COLORS.text, maxRotation: 45, autoSkip: true, maxTicksLimit: 12 },
            grid: { color: COLORS.grid }
          },
          y: {
            ticks: { color: COLORS.text, callback: v => fmtNum(v) },
            grid: { color: COLORS.grid },
            beginAtZero: true
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
    (calls || []).forEach(c => {
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
          y: { ticks: { color: COLORS.text, precision: 0 }, grid: { color: COLORS.grid }, beginAtZero: true }
        }
      }
    });
  }

  /**
   * TTFB Distribution Chart
   */
  function renderTtfbChart(calls) {
    const ttfbBuckets = { '<500ms': 0, '500ms-1s': 0, '1-3s': 0, '3-10s': 0, '10-30s': 0, '>30s': 0 };
    (calls || []).forEach(c => {
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
          y: { ticks: { color: COLORS.text, precision: 0 }, grid: { color: COLORS.grid }, beginAtZero: true }
        }
      }
    });
  }

  /**
   * Fast downsampler for scatter plots to maintain 60 FPS rendering on massive datasets.
   * If points exceed targetMax, samples points preserving min, max, outliers, and density.
   */
  function downsamplePoints(points, targetMax = 2500) {
    if (!points || points.length <= targetMax) return points;

    const n = points.length;
    const bucketSize = n / (targetMax / 2);
    const sampled = [];

    for (let i = 0; i < n; i += bucketSize) {
      const chunkEnd = Math.min(n, Math.floor(i + bucketSize));
      let minPt = points[Math.floor(i)];
      let maxPt = points[Math.floor(i)];

      for (let j = Math.floor(i); j < chunkEnd; j++) {
        const pt = points[j];
        if (pt.y < minPt.y) minPt = pt;
        if (pt.y > maxPt.y) maxPt = pt;
      }

      sampled.push(minPt);
      if (maxPt !== minPt) {
        sampled.push(maxPt);
      }
    }

    return sampled;
  }

  /**
   * Server Load vs RTT Scatter Chart
   */
  function renderLoadChart(calls) {
    const rawLoadPoints = (calls || [])
      .filter(c => c.server_running !== null && c.server_running !== undefined && c.total_ms && c.total_ms > 0)
      .map(c => ({
        x: c.server_running,
        y: c.total_ms / 1000,
        model: c.model || 'unknown'
      }));

    const loadPoints = downsamplePoints(rawLoadPoints, 2500);

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
            grid: { color: COLORS.grid },
            beginAtZero: true
          },
          y: {
            title: { display: true, text: 'RTT (seconds)', color: COLORS.text },
            ticks: { color: COLORS.text },
            grid: { color: COLORS.grid },
            beginAtZero: true
          }
        }
      }
    });
  }

  /**
   * Throughput Scatter Chart
   */
  function renderThroughputChart(calls, timeRange = null) {
    const rawTpsData = (calls || [])
      .filter(c => c.tokens_per_s && c.tokens_per_s > 0)
      .map(c => ({
        x: new Date(c.timestamp).getTime(),
        y: c.tokens_per_s,
        model: c.model || 'unknown'
      }));

    const tpsData = downsamplePoints(rawTpsData, 2500);

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
            grid: { color: COLORS.grid },
            beginAtZero: true
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
    (calls || []).forEach(c => {
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
          y: { ticks: { color: COLORS.text, precision: 0 }, grid: { color: COLORS.grid }, beginAtZero: true }
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
    const bucketSet = new Set(buckets);

    const byBucket = {};
    buckets.forEach(b => {
      byBucket[b] = 0;
    });

    const errors = (calls || []).filter(c => Boolean(c.error || (c.status_code && (c.status_code < 200 || c.status_code >= 300))));
    errors.forEach(c => {
      const cnt = c.calls_count !== undefined && c.calls_count !== null ? c.calls_count : 1;
      const bucketKey = getBucketKey(c.timestamp, intervalMinutes);
      if (!bucketSet.has(bucketKey)) return;
      byBucket[bucketKey] += cnt;
    });

    const sortedBuckets = buckets;

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
          x: { ticks: { color: COLORS.text, maxRotation: 45, autoSkip: true, maxTicksLimit: 12 }, grid: { color: COLORS.grid } },
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
    const bucketSet = new Set(buckets);

    const byBucket = {};
    buckets.forEach(b => {
      byBucket[b] = { input: 0, output: 0, total: 0 };
    });

    (calls || []).forEach(c => {
      const bucketKey = getBucketKey(c.timestamp, intervalMinutes);
      if (!bucketSet.has(bucketKey)) return;
      const inp = c.input_tokens || 0;
      const out = c.output_tokens || 0;
      byBucket[bucketKey].input += inp;
      byBucket[bucketKey].output += out;
      byBucket[bucketKey].total += (inp + out);
    });

    const sortedBuckets = buckets;
    const labels = sortedBuckets.map(b => UI.formatShortDate(b));

    const datasets = [
      {
        label: 'Input Tokens',
        data: sortedBuckets.map(b => byBucket[b].input),
        borderColor: COLORS.accent,
        backgroundColor: COLORS.accent + '15',
        borderWidth: 2,
        tension: 0.2,
        fill: true,
        pointRadius: (ctx) => (Number(ctx.raw) > 0 ? 3 : 0),
        pointHoverRadius: (ctx) => (Number(ctx.raw) > 0 ? 5 : 0)
      },
      {
        label: 'Output Tokens',
        data: sortedBuckets.map(b => byBucket[b].output),
        borderColor: COLORS.purple,
        backgroundColor: COLORS.purple + '15',
        borderWidth: 2,
        tension: 0.2,
        fill: true,
        pointRadius: (ctx) => (Number(ctx.raw) > 0 ? 3 : 0),
        pointHoverRadius: (ctx) => (Number(ctx.raw) > 0 ? 5 : 0)
      },
      {
        label: 'Total Tokens',
        data: sortedBuckets.map(b => byBucket[b].total),
        borderColor: COLORS.green,
        backgroundColor: COLORS.green + '10',
        borderWidth: 2.5,
        borderDash: [5, 5],
        tension: 0.2,
        fill: false,
        pointRadius: (ctx) => (Number(ctx.raw) > 0 ? 3 : 0),
        pointHoverRadius: (ctx) => (Number(ctx.raw) > 0 ? 5 : 0)
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
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { labels: { color: COLORS.text, font: { family: 'Outfit' } } },
          tooltip: {
            filter: (tooltipItem) => (Number(tooltipItem.raw) || 0) > 0,
            itemSort: (a, b) => (Number(b.raw) || 0) - (Number(a.raw) || 0),
            callbacks: {
              label: function(context) {
                let label = context.dataset.label || '';
                if (label) label += ': ';
                const val = Number(context.raw) || 0;
                return label + val.toLocaleString('en-US');
              }
            }
          }
        },
        scales: {
          x: { ticks: { color: COLORS.text, maxRotation: 45, autoSkip: true, maxTicksLimit: 12 }, grid: { color: COLORS.grid } },
          y: { ticks: { color: COLORS.text, callback: v => fmtNum(v) }, grid: { color: COLORS.grid }, beginAtZero: true }
        }
      }
    });
  }

  /**
   * Render Average Tokens per Call Chart by Model
   */
  function renderAnalyzerAvgTokenChart(calls, timeRange = null, isUserFiltered = false) {
    const intervalMinutes = getIntervalMinutes(calls, timeRange);
    const buckets = getTimeBuckets(calls, intervalMinutes, timeRange);
    const bucketSet = new Set(buckets);

    const byBucket = {};
    buckets.forEach(b => {
      byBucket[b] = {};
    });

    const modelTotals = {};
    const modelCallsCount = {};

    (calls || []).forEach(c => {
      if (!c.model || c.model === 'unknown') return;
      const totalTok = (c.input_tokens || 0) + (c.output_tokens || 0);
      if (totalTok <= 0) return;
      const bucketKey = getBucketKey(c.timestamp, intervalMinutes);
      if (!bucketSet.has(bucketKey)) return;
      const m = c.model;
      const callsCount = (c.calls_count !== undefined && c.calls_count !== null ? c.calls_count : 1);

      if (!byBucket[bucketKey][m]) {
        byBucket[bucketKey][m] = { sum: 0, count: 0 };
      }
      byBucket[bucketKey][m].sum += totalTok;
      byBucket[bucketKey][m].count += callsCount;

      modelTotals[m] = (modelTotals[m] || 0) + totalTok;
      modelCallsCount[m] = (modelCallsCount[m] || 0) + callsCount;
    });

    const sortedBuckets = buckets;
    const labels = sortedBuckets.map(b => UI.formatShortDate(b));

    // Filter models with real calls and sort descending by call volume
    const activeModels = Object.keys(modelCallsCount)
      .filter(m => (modelCallsCount[m] || 0) > 0 && (modelTotals[m] || 0) > 0)
      .sort((a, b) => (modelCallsCount[b] || 0) - (modelCallsCount[a] || 0));

    // Limit to top active models (e.g. top 5) if unfiltered
    const models = (isUserFiltered || activeModels.length <= 5) ? activeModels : activeModels.slice(0, 5);

    const datasets = models.map(m => {
      return {
        label: m,
        data: sortedBuckets.map(b => {
          const entry = byBucket[b][m];
          return entry && entry.count > 0 && entry.sum > 0 ? (entry.sum / entry.count) : null;
        }),
        borderColor: getModelColor(m),
        backgroundColor: 'transparent',
        borderWidth: 2,
        tension: 0.2,
        spanGaps: false,
        pointRadius: (ctx) => (ctx.raw !== null && Number(ctx.raw) > 0 ? 3 : 0),
        pointHoverRadius: (ctx) => (ctx.raw !== null && Number(ctx.raw) > 0 ? 5 : 0),
        pointHitRadius: 10
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
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: {
            display: datasets.length > 0,
            labels: { color: COLORS.text, font: { family: 'Outfit' } }
          },
          tooltip: {
            filter: (tooltipItem) => tooltipItem.raw !== null && (Number(tooltipItem.raw) || 0) > 0,
            itemSort: (a, b) => (Number(b.raw) || 0) - (Number(a.raw) || 0),
            callbacks: {
              label: function(context) {
                let label = context.dataset.label || '';
                if (label) label += ': ';
                const val = Number(context.raw) || 0;
                return label + Math.round(val).toLocaleString('en-US') + ' tok/call';
              }
            }
          }
        },
        scales: {
          x: { ticks: { color: COLORS.text, maxRotation: 45, autoSkip: true, maxTicksLimit: 12 }, grid: { color: COLORS.grid } },
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
  function renderAnalyzerCharts(hourData, calls, timeRange = null, isUserFiltered = false) {
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
            legend: { labels: { color: COLORS.text, font: { family: 'Outfit' } } },
            tooltip: {
              filter: (tooltipItem) => (Number(tooltipItem.raw) || 0) > 0
            }
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
              fill: false,
              pointRadius: (ctx) => (Number(ctx.raw) > 0 ? 3 : 0),
              pointHoverRadius: (ctx) => (Number(ctx.raw) > 0 ? 5 : 0)
            },
            {
              label: 'Startup Latency (Avg TTFT)',
              data: ttfbData,
              borderColor: COLORS.cyan,
              backgroundColor: COLORS.cyan + '20',
              borderWidth: 2,
              tension: 0.3,
              fill: false,
              pointRadius: (ctx) => (Number(ctx.raw) > 0 ? 3 : 0),
              pointHoverRadius: (ctx) => (Number(ctx.raw) > 0 ? 5 : 0)
            }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: 'index', intersect: false },
          plugins: {
            legend: { labels: { color: COLORS.text, font: { family: 'Outfit' } } },
            tooltip: {
              filter: (tooltipItem) => (Number(tooltipItem.raw) || 0) > 0
            }
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
      renderAnalyzerAvgTokenChart(calls, timeRange, isUserFiltered);
    }
  }

  function renderCostCharts(calls, modelCosts, timeRange = null, isUserFiltered = false) {
    if (!calls || !calls.length) return;
    renderCostOverTimeChart(calls, timeRange, isUserFiltered);
    renderCostShareChart(calls, isUserFiltered);
  }

  function renderCostOverTimeChart(calls, timeRange = null, isUserFiltered = false) {
    const intervalMinutes = getIntervalMinutes(calls, timeRange);
    const buckets = getTimeBuckets(calls, intervalMinutes, timeRange);
    const bucketSet = new Set(buckets);

    const rawByBucket = {};
    buckets.forEach(b => {
      rawByBucket[b] = {};
    });

    (calls || []).forEach(c => {
      if (!c.model || c.model === 'unknown') return;
      const cost = c.total_cost || 0;
      if (cost <= 0) return;
      const bucketKey = getBucketKey(c.timestamp, intervalMinutes);
      if (!bucketSet.has(bucketKey)) return;
      const m = c.model;
      rawByBucket[bucketKey][m] = (rawByBucket[bucketKey][m] || 0) + cost;
    });

    const sortedBuckets = buckets;
    const { models, byBucket } = prepareModelSeries(rawByBucket, sortedBuckets, isUserFiltered, 6, 0.005);

    const datasets = models.map(m => {
      const dataPoints = sortedBuckets.map(b => byBucket[b][m] || 0);

      return {
        label: m,
        data: dataPoints,
        backgroundColor: m === 'Other Models' ? '#6e768115' : getModelColor(m) + '20',
        borderColor: getModelColor(m),
        borderWidth: m === 'Other Models' ? 1.5 : 2,
        borderDash: m === 'Other Models' ? [4, 4] : undefined,
        fill: true,
        tension: 0.2,
        pointRadius: (ctx) => (Number(ctx.raw) > 0 ? 3 : 0),
        pointHoverRadius: (ctx) => (Number(ctx.raw) > 0 ? 5 : 0),
        pointHitRadius: 10
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
            display: datasets.length > 0,
            position: 'top',
            labels: { color: COLORS.text, font: { family: 'Outfit' } }
          },
          tooltip: {
            filter: (tooltipItem) => (Number(tooltipItem.raw) || 0) > 0,
            itemSort: (a, b) => (Number(b.raw) || 0) - (Number(a.raw) || 0),
            callbacks: {
              label: function(context) {
                const val = Number(context.raw) || 0;
                return `${context.dataset.label}: $${val < 0.01 ? val.toFixed(4) : val.toFixed(2)}`;
              }
            }
          }
        },
        scales: {
          x: {
            grid: { color: COLORS.grid },
            ticks: { color: COLORS.text, maxRotation: 45, minRotation: 45, autoSkip: true, maxTicksLimit: 12 }
          },
          y: {
            grid: { color: COLORS.grid },
            ticks: {
              color: COLORS.text,
              callback: function(value) {
                return '$' + (value < 1 && value > 0 ? value.toFixed(3) : value.toFixed(2));
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

  function renderCostShareChart(calls, isUserFiltered = false) {
    const byModel = {};
    (calls || []).forEach(c => {
      if (!c.model || c.model === 'unknown') return;
      const cost = c.total_cost || 0;
      if (cost <= 0) return;
      const m = c.model;
      byModel[m] = (byModel[m] || 0) + cost;
    });

    const sortedModels = Object.keys(byModel).filter(m => (byModel[m] || 0) > 0).sort((a, b) => byModel[b] - byModel[a]);

    const ctx = document.getElementById('costShareChart');
    if (!ctx) return;

    if (instances.costShare) {
      instances.costShare.destroy();
    }

    const totalCost = sortedModels.reduce((sum, m) => sum + byModel[m], 0);
    if (totalCost === 0 || sortedModels.length === 0) {
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

    // Top-N + Other Models for Doughnut
    let displayModels = sortedModels;
    let displayData = [];
    if (!isUserFiltered && sortedModels.length > 6) {
      const top5 = sortedModels.slice(0, 5);
      const otherModels = sortedModels.slice(5);
      const otherCost = otherModels.reduce((sum, m) => sum + byModel[m], 0);
      displayModels = [...top5, 'Other Models'];
      displayData = [...top5.map(m => byModel[m]), otherCost];
    } else {
      displayData = displayModels.map(m => byModel[m]);
    }

    const backgroundColors = displayModels.map(m => getModelColor(m));

    instances.costShare = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: displayModels,
        datasets: [{
          data: displayData,
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
                return ` ${context.label}: $${val < 0.01 ? val.toFixed(4) : val.toFixed(2)} (${pct}%)`;
              }
            }
          }
        }
      }
    });
  }

  const moduleExports = {
    renderAll,
    destroyAll,
    getModelColor,
    COLORS,
    renderAnalyzerCharts,
    renderCostCharts,
    renderTokenChart,
    prepareModelSeries
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = moduleExports;
  }

  return moduleExports;
})();
