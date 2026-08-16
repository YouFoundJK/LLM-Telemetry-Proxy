/**
 * Comprehensive Unit Tests for Chart Model Aggregation & Zero-Noise Filtering
 */

const assert = require('assert');

// Mock browser globals required by charts.js in Node environment
global.window = global;
global.document = {
  getElementById: () => null,
  querySelector: () => null,
  querySelectorAll: () => []
};
global.Chart = function(ctx, config) {
  this.ctx = ctx;
  this.config = config;
  this.destroy = () => {};
};
global.Chart.defaults = {};
global.UI = {
  formatShortDate: (ts) => new Date(ts).toISOString(),
  formatShortTime: (ts) => new Date(ts).toLocaleTimeString()
};

const TelemetryCharts = require('../dashboard/static/js/charts.js');

console.log('=== Starting Telemetry Charts Smart Model Aggregation Tests ===\n');

// -------------------------------------------------------------
// Test 1: Zero-Metric Model Elimination
// -------------------------------------------------------------
{
  console.log('--- Test 1: Zero-metric models are completely eliminated ---');
  const buckets = [1000, 2000, 3000];
  const byBucket = {
    1000: { 'glm-5.2': 100, 'dead-model': 0, 'unused-model': 0 },
    2000: { 'glm-5.2': 200, 'dead-model': 0 },
    3000: { 'glm-5.2': 300 }
  };

  const result = TelemetryCharts.prepareModelSeries(byBucket, buckets, false, 6, 0.01);
  assert.deepStrictEqual(result.models, ['glm-5.2'], 'Only active models should be included');
  assert.strictEqual(result.modelTotals['glm-5.2'], 600);
  assert.strictEqual(result.models.includes('dead-model'), false);
  assert.strictEqual(result.models.includes('unused-model'), false);
  console.log('PASS: 0-usage models are completely omitted.');
}

// -------------------------------------------------------------
// Test 2: Models under limit preserved individually
// -------------------------------------------------------------
{
  console.log('\n--- Test 2: Models within max limit (<= 6) are preserved individually ---');
  const buckets = [1000];
  const byBucket = {
    1000: {
      'glm-5.2': 500000,
      'kimi-k3': 300000,
      'deepseek-v4-flash': 200000,
      'deepseek-v4-flash-thinking': 100000
    }
  };

  const result = TelemetryCharts.prepareModelSeries(byBucket, buckets, false, 6, 0.01);
  assert.strictEqual(result.models.length, 4);
  assert.deepStrictEqual(result.models, [
    'glm-5.2',
    'kimi-k3',
    'deepseek-v4-flash',
    'deepseek-v4-flash-thinking'
  ]);
  assert.strictEqual(result.models.includes('Other Models'), false);
  console.log('PASS: Prominent active models within threshold kept individually.');
}

// -------------------------------------------------------------
// Test 3: Large model list sliced into Top-N + 'Other Models'
// -------------------------------------------------------------
{
  console.log('\n--- Test 3: More than 6 models sliced into Top 5 + Other Models ---');
  const buckets = [1000, 2000];
  const byBucket = {
    1000: {
      'glm-5.2': 40000000,
      'kimi-k3': 30000000,
      'deepseek-v4-flash-thinking': 20000000,
      'deepseek-v4-flash': 10000000,
      'gemma4': 500000,
      'qwen3.5-int4': 200000,
      'command-a': 307,
      'gpt-oss-120b': 82,
      'mistral-medium-3.5': 707
    },
    2000: {
      'glm-5.2': 45000000,
      'kimi-k3': 32000000,
      'deepseek-v4-flash-thinking': 22000000,
      'deepseek-v4-flash': 11000000,
      'gemma4': 550000,
      'qwen3.5-int4': 250000,
      'command-a': 0,
      'gpt-oss-120b': 0,
      'mistral-medium-3.5': 0
    }
  };

  const result = TelemetryCharts.prepareModelSeries(byBucket, buckets, false, 6, 0.005);
  
  // Top 5 models
  assert.strictEqual(result.models[0], 'glm-5.2');
  assert.strictEqual(result.models[1], 'kimi-k3');
  assert.strictEqual(result.models[2], 'deepseek-v4-flash-thinking');
  assert.strictEqual(result.models[3], 'deepseek-v4-flash');
  assert.strictEqual(result.models[4], 'gemma4');
  assert.strictEqual(result.models[5], 'Other Models');
  assert.strictEqual(result.models.length, 6);

  // Check Other Models bucket aggregation
  const expectedOtherB1 = 200000 + 307 + 82 + 707;
  const expectedOtherB2 = 250000;
  assert.strictEqual(result.byBucket[1000]['Other Models'], expectedOtherB1);
  assert.strictEqual(result.byBucket[2000]['Other Models'], expectedOtherB2);
  console.log('PASS: Minor and noise models cleanly aggregated into Other Models.');
}

// -------------------------------------------------------------
// Test 4: Explicit User Model Filtering Preserved
// -------------------------------------------------------------
{
  console.log('\n--- Test 4: Explicit user filtering (isUserFiltered = true) disables "Other Models" ---');
  const buckets = [1000];
  const byBucket = {
    1000: {
      'command-a': 307,
      'gpt-oss-120b': 82,
      'mistral-medium-3.5': 707,
      'model-4': 100,
      'model-5': 200,
      'model-6': 300,
      'model-7': 400
    }
  };

  const result = TelemetryCharts.prepareModelSeries(byBucket, buckets, true, 6, 0.01);
  assert.strictEqual(result.models.includes('Other Models'), false);
  assert.strictEqual(result.models.length, 7);
  console.log('PASS: Explicitly selected models preserved without grouping.');
}

// -------------------------------------------------------------
// Test 5: Color Assignment for Other Models & Registered Models
// -------------------------------------------------------------
{
  console.log('\n--- Test 5: Color assignment logic ---');
  const otherColor = TelemetryCharts.getModelColor('Other Models');
  assert.strictEqual(otherColor, '#6e7681', 'Other Models must receive neutral slate color');

  const otherLowerColor = TelemetryCharts.getModelColor('other');
  assert.strictEqual(otherLowerColor, '#6e7681');

  const glmColor = TelemetryCharts.getModelColor('glm-5.2');
  assert.strictEqual(typeof glmColor, 'string');
  assert.strictEqual(glmColor.startsWith('#'), true);
  console.log('PASS: Model color assignment operates correctly.');
}

// -------------------------------------------------------------
// Test 6: Metric-Sensitive Filtering (e.g. Embedding models 0 output tokens)
// -------------------------------------------------------------
{
  console.log('\n--- Test 6: Metric sensitivity (Input vs Output) ---');
  const calls = [
    { timestamp: '2026-08-16T12:00:00Z', model: 'glm-5.2', input_tokens: 1000, output_tokens: 500 },
    { timestamp: '2026-08-16T12:00:00Z', model: 'qwen3-embedding-4b', input_tokens: 500, output_tokens: 0 }
  ];

  // In Output metric mode, qwen3-embedding-4b has 0 output tokens and must NOT be in the chart
  let chartConfig = null;
  global.document.getElementById = (id) => {
    if (id === 'tokenChart') return {};
    return null;
  };
  global.Chart = function(ctx, config) {
    chartConfig = config;
    this.destroy = () => {};
  };

  TelemetryCharts.renderTokenChart(calls, 'output');
  assert.notStrictEqual(chartConfig, null);
  const outputDatasets = chartConfig.data.datasets.map(d => d.label);
  assert.deepStrictEqual(outputDatasets, ['glm-5.2'], 'Embedding model with 0 output tokens must be excluded');
  
  // In Input metric mode, both should appear
  TelemetryCharts.renderTokenChart(calls, 'input');
  const inputDatasets = chartConfig.data.datasets.map(d => d.label);
  assert.strictEqual(inputDatasets.includes('glm-5.2'), true);
  assert.strictEqual(inputDatasets.includes('qwen3-embedding-4b'), true);
  console.log('PASS: Metric toggling dynamically includes/excludes models based on active usage.');
}

console.log('\n=========================================');
console.log('ALL CHART MODEL AGGREGATION TESTS PASSED!');
console.log('=========================================');
