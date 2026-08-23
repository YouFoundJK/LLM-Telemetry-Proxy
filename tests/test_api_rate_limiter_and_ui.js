const assert = require('assert');
const UI = require('../dashboard/static/js/ui.js');
const TelemetryAPI = require('../dashboard/static/js/api.js');

console.log('=== Running UI & API Rate Limiting Tests ===\n');

// --- Test 1: UI.escapeHtml ---
console.log('--- Test 1: UI.escapeHtml sanitization ---');
assert.strictEqual(UI.escapeHtml(null), '', 'null should return empty string');
assert.strictEqual(UI.escapeHtml(undefined), '', 'undefined should return empty string');
assert.strictEqual(UI.escapeHtml('hello world'), 'hello world', 'plain text unchanged');
assert.strictEqual(
  UI.escapeHtml('<script>alert("xss")</script>'),
  '&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;',
  'script tags escaped'
);
assert.strictEqual(
  UI.escapeHtml("Tom & Jerry's \"Favorite\""),
  'Tom &amp; Jerry&#039;s &quot;Favorite&quot;',
  'ampersand, quotes, apostrophe escaped'
);
console.log('PASS: UI.escapeHtml sanitizes correctly.\n');

// --- Test 2: TelemetryAPI exports & mock 429 handling ---
console.log('--- Test 2: TelemetryAPI API surface & rate limiter ---');
assert.strictEqual(typeof TelemetryAPI.isRateLimited, 'function', 'isRateLimited should be a function');
assert.strictEqual(TelemetryAPI.isRateLimited(), false, 'initial rate limit should be false');

async function testRateLimiterCircuitBreaker() {
  let fetchCount = 0;
  global.fetch = async (url, opts) => {
    fetchCount++;
    return {
      ok: false,
      status: 429,
      statusText: 'Too Many Requests',
      headers: {
        get: (h) => (h.toLowerCase() === 'retry-after' ? '2' : null)
      },
      json: async () => ({ error: 'Too Many Requests' })
    };
  };

  try {
    await TelemetryAPI.getProxyStatus(9090);
    assert.fail('Expected getProxyStatus to throw on 429');
  } catch (err) {
    assert.strictEqual(err.status, 429, 'Error status should be 429');
  }

  assert.strictEqual(TelemetryAPI.isRateLimited(), true, 'isRateLimited() should be true after receiving 429');
  assert.strictEqual(fetchCount, 1, 'Background poll should NOT retry on 429 (fetch count must be 1)');

  // Second background poll during active rate limit should immediately skip without fetch
  try {
    await TelemetryAPI.getProxyLogs(100);
    assert.fail('Expected getProxyLogs to be throttled');
  } catch (err) {
    assert.strictEqual(err.isThrottled, true, 'Second background poll should be throttled');
    assert.strictEqual(fetchCount, 1, 'Fetch count should remain 1 (no network call made)');
  }

  console.log('PASS: TelemetryAPI 429 circuit breaker safely throttles background requests without retries.\n');
}

testRateLimiterCircuitBreaker().then(() => {
  console.log('==============================================');
  console.log('ALL UI & API RATE LIMITER TESTS PASSED! 🚀');
  console.log('==============================================');
}).catch((err) => {
  console.error('Test failed:', err);
  process.exit(1);
});

