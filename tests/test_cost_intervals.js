const fs = require('fs');
const path = require('path');
const assert = require('assert');

// 1. Load actual model_costs.json
const candidatePaths = [
  path.join(__dirname, '..', 'data', 'model_costs.json'),
  path.join(__dirname, 'data', 'model_costs.json'),
  path.join(__dirname, 'model_costs.json'),
];
const modelCostsPath = candidatePaths.find(p => fs.existsSync(p)) || candidatePaths[0];
const rawModelCosts = JSON.parse(fs.readFileSync(modelCostsPath, 'utf8'));

// 2. Exact functions from app.js
function normalizeModelCostsConfig(rawCosts) {
  if (!rawCosts || typeof rawCosts !== 'object') return {};
  const normalized = {};
  for (const [k, val] of Object.entries(rawCosts)) {
    if (Array.isArray(val)) {
      const tiers = val.map(entry => {
        const dateStr = entry.effective_date || entry.last_updated || '1970-01-01';
        const fullIso = dateStr.includes('T') ? dateStr : `${dateStr}T00:00:00Z`;
        let epoch = new Date(fullIso).getTime();
        if (isNaN(epoch)) epoch = 0;
        return {
          epoch,
          effective_date: dateStr,
          input_cost_per_million: Number(entry.input_cost_per_million) || 0,
          output_cost_per_million: Number(entry.output_cost_per_million) || 0,
          provider_source: entry.provider_source || 'Unknown',
          last_updated: entry.last_updated || dateStr
        };
      }).sort((a, b) => a.epoch - b.epoch);
      normalized[k] = tiers;
    } else if (val && typeof val === 'object') {
      const dateStr = val.effective_date || val.last_updated || '1970-01-01';
      const fullIso = dateStr.includes('T') ? dateStr : `${dateStr}T00:00:00Z`;
      let epoch = new Date(fullIso).getTime();
      if (isNaN(epoch)) epoch = 0;
      normalized[k] = [{
        epoch,
        effective_date: dateStr,
        input_cost_per_million: Number(val.input_cost_per_million) || 0,
        output_cost_per_million: Number(val.output_cost_per_million) || 0,
        provider_source: val.provider_source || 'Unknown',
        last_updated: val.last_updated || dateStr
      }];
    }
  }
  return normalized;
}

function getModelCostForCall(normalizedCosts, rawModel, callTimestamp) {
  if (!normalizedCosts || !rawModel) return null;
  const modelKey = String(rawModel).trim().toLowerCase();
  const costKeys = Object.keys(normalizedCosts);
  if (!costKeys.length) return null;

  let matchedKey = null;
  // 1. Exact match
  for (const k of costKeys) {
    if (modelKey === k.toLowerCase()) {
      matchedKey = k;
      break;
    }
  }
  // 2. Longest substring match
  if (!matchedKey) {
    const sortedKeys = [...costKeys].sort((a, b) => b.length - a.length);
    for (const k of sortedKeys) {
      if (modelKey.includes(k.toLowerCase())) {
        matchedKey = k;
        break;
      }
    }
  }
  // 3. Reverse substring match
  if (!matchedKey) {
    for (const k of costKeys) {
      if (k.toLowerCase().includes(modelKey)) {
        matchedKey = k;
        break;
      }
    }
  }

  if (!matchedKey) return null;
  const tiers = normalizedCosts[matchedKey];
  if (!tiers || !tiers.length) return null;

  // Parse call timestamp
  let callEpoch = NaN;
  if (callTimestamp) {
    callEpoch = new Date(callTimestamp).getTime();
  }

  // If timestamp is invalid or missing, default to latest tier
  if (isNaN(callEpoch)) {
    return tiers[tiers.length - 1];
  }

  // If call happened before earliest tier, clamp to earliest tier
  if (callEpoch < tiers[0].epoch) {
    return tiers[0];
  }

  // Find the latest tier where tier.epoch <= callEpoch
  for (let i = tiers.length - 1; i >= 0; i--) {
    if (callEpoch >= tiers[i].epoch) {
      return tiers[i];
    }
  }

  return tiers[0];
}

function enrichCallsWithCosts(calls, modelCosts) {
  if (!calls || !modelCosts) return;
  const normalizedModelCosts = normalizeModelCostsConfig(modelCosts);

  calls.forEach(c => {
    const costConfig = getModelCostForCall(normalizedModelCosts, c.model, c.timestamp);
    const callsCount = (c.calls_count !== undefined && c.calls_count !== null) ? c.calls_count : 1;

    if (costConfig) {
      c.input_cost = ((c.input_tokens || 0) / 1e6) * (costConfig.input_cost_per_million || 0) * callsCount;
      c.output_cost = ((c.output_tokens || 0) / 1e6) * (costConfig.output_cost_per_million || 0) * callsCount;
      c.total_cost = c.input_cost + c.output_cost;
      c.provider_source = costConfig.provider_source || 'Unknown';
      c.last_updated = costConfig.effective_date || costConfig.last_updated || '';
      c.effective_date = costConfig.effective_date || costConfig.last_updated || '';
    } else {
      c.input_cost = 0;
      c.output_cost = 0;
      c.total_cost = 0;
      c.provider_source = 'Unknown';
      c.last_updated = '';
      c.effective_date = '';
    }
  });
}

console.log('--- Test 1: Real model_costs.json parsing ---');
const normalized = normalizeModelCostsConfig(rawModelCosts);
assert.ok(normalized['deepseek-v4-pro'], 'deepseek-v4-pro must exist');
assert.strictEqual(normalized['deepseek-v4-pro'].length, 1);
assert.strictEqual(normalized['deepseek-v4-pro'][0].input_cost_per_million, 0.435);
console.log('PASS: model_costs.json parsed successfully.');

console.log('\n--- Test 2: Dynamic Multi-Tier Interval Matching ---');
const mockModelCosts = {
  "test-model": [
    {
      "effective_date": "2026-06-01",
      "input_cost_per_million": 1.00,
      "output_cost_per_million": 2.00,
      "provider_source": "Provider v1"
    },
    {
      "effective_date": "2026-08-01",
      "input_cost_per_million": 0.50,
      "output_cost_per_million": 1.00,
      "provider_source": "Provider v2 (Price Cut)"
    },
    {
      "effective_date": "2026-10-01",
      "input_cost_per_million": 0.25,
      "output_cost_per_million": 0.50,
      "provider_source": "Provider v3"
    }
  ],
  "legacy-model": {
    "effective_date": "2026-05-01",
    "input_cost_per_million": 2.00,
    "output_cost_per_million": 4.00,
    "provider_source": "Legacy Provider"
  }
};

const calls = [
  // 1. Call before earliest registered date (2026-05-15 < 2026-06-01) -> clamped to 2026-06-01 tier
  {
    id: 1,
    model: "test-model",
    timestamp: "2026-05-15T12:00:00Z",
    input_tokens: 1000000,
    output_tokens: 1000000,
    calls_count: 1
  },
  // 2. Call between tier 1 and tier 2 (2026-07-15) -> tier 1
  {
    id: 2,
    model: "test-model",
    timestamp: "2026-07-15T10:00:00Z",
    input_tokens: 1000000,
    output_tokens: 1000000,
    calls_count: 1
  },
  // 3. Call right on tier 2 boundary (2026-08-01T00:00:00Z) -> tier 2
  {
    id: 3,
    model: "test-model",
    timestamp: "2026-08-01T00:00:00Z",
    input_tokens: 1000000,
    output_tokens: 1000000,
    calls_count: 1
  },
  // 4. Call in tier 2 (2026-09-10) -> tier 2
  {
    id: 4,
    model: "test-model",
    timestamp: "2026-09-10T14:30:00Z",
    input_tokens: 2000000,
    output_tokens: 1000000,
    calls_count: 2
  },
  // 5. Call in tier 3 (2026-11-01) -> tier 3
  {
    id: 5,
    model: "test-model",
    timestamp: "2026-11-01T08:00:00Z",
    input_tokens: 1000000,
    output_tokens: 1000000,
    calls_count: 1
  },
  // 6. Call with legacy single object format
  {
    id: 6,
    model: "legacy-model",
    timestamp: "2026-09-01T12:00:00Z",
    input_tokens: 1000000,
    output_tokens: 1000000,
    calls_count: 1
  },
  // 7. Call with no timestamp -> latest tier (tier 3)
  {
    id: 7,
    model: "test-model",
    timestamp: null,
    input_tokens: 1000000,
    output_tokens: 1000000,
    calls_count: 1
  }
];

enrichCallsWithCosts(calls, mockModelCosts);

// Assertions
// Call 1: clamped to tier 1 ($1 in + $2 out = $3)
assert.strictEqual(calls[0].input_cost, 1.00);
assert.strictEqual(calls[0].output_cost, 2.00);
assert.strictEqual(calls[0].total_cost, 3.00);
assert.strictEqual(calls[0].effective_date, "2026-06-01");
console.log('PASS: Call 1 clamped to earliest tier (2026-06-01)');

// Call 2: tier 1 ($1 in + $2 out = $3)
assert.strictEqual(calls[1].input_cost, 1.00);
assert.strictEqual(calls[1].output_cost, 2.00);
assert.strictEqual(calls[1].total_cost, 3.00);
assert.strictEqual(calls[1].effective_date, "2026-06-01");
console.log('PASS: Call 2 matched tier 1 (2026-06-01)');

// Call 3: boundary tier 2 ($0.50 in + $1.00 out = $1.50)
assert.strictEqual(calls[2].input_cost, 0.50);
assert.strictEqual(calls[2].output_cost, 1.00);
assert.strictEqual(calls[2].total_cost, 1.50);
assert.strictEqual(calls[2].effective_date, "2026-08-01");
console.log('PASS: Call 3 matched tier 2 boundary (2026-08-01)');

// Call 4: tier 2 with calls_count=2 (2M in * 0.50 * 2 = $2.00 in, 1M out * 1.00 * 2 = $2.00 out -> $4.00 total)
assert.strictEqual(calls[3].input_cost, 2.00);
assert.strictEqual(calls[3].output_cost, 2.00);
assert.strictEqual(calls[3].total_cost, 4.00);
assert.strictEqual(calls[3].effective_date, "2026-08-01");
console.log('PASS: Call 4 calculated correctly with calls_count multiplier');

// Call 5: tier 3 ($0.25 in + $0.50 out = $0.75)
assert.strictEqual(calls[4].input_cost, 0.25);
assert.strictEqual(calls[4].output_cost, 0.50);
assert.strictEqual(calls[4].total_cost, 0.75);
assert.strictEqual(calls[4].effective_date, "2026-10-01");
console.log('PASS: Call 5 matched tier 3 (2026-10-01)');

// Call 6: legacy model ($2 in + $4 out = $6)
assert.strictEqual(calls[5].input_cost, 2.00);
assert.strictEqual(calls[5].output_cost, 4.00);
assert.strictEqual(calls[5].total_cost, 6.00);
console.log('PASS: Call 6 legacy single-object backward compatibility');

// Call 7: missing timestamp -> latest tier ($0.25 in + $0.50 out = $0.75)
assert.strictEqual(calls[6].input_cost, 0.25);
assert.strictEqual(calls[6].output_cost, 0.50);
assert.strictEqual(calls[6].total_cost, 0.75);
assert.strictEqual(calls[6].effective_date, "2026-10-01");
console.log('PASS: Call 7 missing timestamp defaulted to latest tier');

console.log('\n=========================================');
console.log('ALL DYNAMIC COST INTERVAL TESTS PASSED!');
console.log('=========================================');
