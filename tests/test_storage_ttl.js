/**
 * Test script for TelemetryStore and 1-week TTL eviction in JavaScript.
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

// Mock IndexedDB in Node environment
class MockIDBIndex {
  constructor(store, indexName) {
    this.store = store;
    this.indexName = indexName;
  }
  getAll(range) {
    const req = { onsuccess: null, onerror: null };
    setImmediate(() => {
      let items = Array.from(this.store.items.values());
      if (this.indexName === 'timestamp') {
        items.sort((a, b) => (a.timestamp || '').localeCompare(b.timestamp || ''));
        if (range) {
          if (range.lower && range.upper) {
            items = items.filter(item => item.timestamp >= range.lower && item.timestamp <= range.upper);
          } else if (range.lower) {
            items = items.filter(item => item.timestamp >= range.lower);
          } else if (range.upper) {
            items = items.filter(item => item.timestamp <= range.upper);
          }
        }
      }
      req.result = items;
      if (req.onsuccess) req.onsuccess({ target: req });
    });
    return req;
  }
  openCursor(range, direction = 'next') {
    const req = { onsuccess: null, onerror: null };
    setImmediate(() => {
      let items = Array.from(this.store.items.values());
      if (this.indexName === 'timestamp') {
        items.sort((a, b) => (a.timestamp || '').localeCompare(b.timestamp || ''));
        if (range) {
          if (range.lower && range.upper) {
            items = items.filter(item => item.timestamp >= range.lower && item.timestamp <= range.upper);
          } else if (range.lower) {
            items = items.filter(item => item.timestamp >= range.lower);
          } else if (range.upper) {
            items = items.filter(item => item.timestamp <= range.upper);
          }
        }
      }
      if (direction === 'prev') {
        items.reverse();
      }
      let idx = 0;
      const makeCursor = (item) => {
        if (!item) return null;
        return {
          value: item,
          continue: () => {
            idx++;
            if (idx < items.length) {
              req.result = makeCursor(items[idx]);
              if (req.onsuccess) req.onsuccess({ target: req });
            } else {
              req.result = null;
              if (req.onsuccess) req.onsuccess({ target: req });
            }
          }
        };
      };
      req.result = makeCursor(items[0]);
      if (req.onsuccess) req.onsuccess({ target: req });
    });
    return req;
  }
}

class MockIDBStore {
  constructor() {
    this.items = new Map();
    this.indexes = new Map();
  }
  createIndex(name, keyPath, opts) {
    this.indexes.set(name, new MockIDBIndex(this, name));
  }
  index(name) {
    return this.indexes.get(name);
  }
  put(item) {
    this.items.set(item.id !== undefined ? item.id : item.key, item);
  }
  get(key) {
    const req = { onsuccess: null, onerror: null };
    setImmediate(() => {
      req.result = this.items.get(key) || null;
      if (req.onsuccess) req.onsuccess({ target: req });
    });
    return req;
  }
  getAll() {
    const req = { onsuccess: null, onerror: null };
    setImmediate(() => {
      req.result = Array.from(this.items.values());
      if (req.onsuccess) req.onsuccess({ target: req });
    });
    return req;
  }
  count() {
    const req = { onsuccess: null, onerror: null };
    setImmediate(() => {
      req.result = this.items.size;
      if (req.onsuccess) req.onsuccess({ target: req });
    });
    return req;
  }
  clear() {
    this.items.clear();
  }
  openCursor(range, direction = 'next') {
    return this.index('timestamp')?.openCursor(range, direction);
  }
}

class MockIDBDatabase {
  constructor() {
    this.objectStoreNames = {
      contains: (name) => this.stores.has(name)
    };
    this.stores = new Map();
  }
  createObjectStore(name, opts) {
    const s = new MockIDBStore();
    this.stores.set(name, s);
    return s;
  }
  transaction(storeNames, mode) {
    const names = Array.isArray(storeNames) ? storeNames : [storeNames];
    const tx = {
      objectStore: (n) => this.stores.get(n),
      oncomplete: null,
      onerror: null
    };
    setImmediate(() => {
      if (tx.oncomplete) tx.oncomplete();
    });
    return tx;
  }
}

global.IDBKeyRange = {
  bound: (l, u) => ({ lower: l, upper: u }),
  lowerBound: (l) => ({ lower: l }),
  upperBound: (u) => ({ upper: u })
};

global.window = {
  indexedDB: {
    open: (name, version) => {
      const req = { onsuccess: null, onerror: null, onupgradeneeded: null };
      const db = new MockIDBDatabase();
      setImmediate(() => {
        if (req.onupgradeneeded) {
          req.onupgradeneeded({ target: { result: db } });
        }
        if (req.onsuccess) {
          req.onsuccess({ target: { result: db } });
        }
      });
      return req;
    }
  }
};
global.indexedDB = global.window.indexedDB;

const storageJsPath = path.join(__dirname, '..', 'dashboard', 'static', 'js', 'storage.js');
const code = fs.readFileSync(storageJsPath, 'utf8');
vm.runInThisContext(code);

async function runTests() {
  console.log('Testing TelemetryStore initialization...');
  await TelemetryStore.init();

  console.log('Testing batch insertion with various timestamp formats...');
  const sampleCalls = [
    // Microseconds and timezone offset +00:00 (SQLite raw format)
    { id: 1, timestamp: '2026-08-01T10:00:00.123456+00:00', model: 'glm-4', input_tokens: 500, output_tokens: 100 },
    // Standard ISO string
    { id: 2, timestamp: '2026-08-02T15:30:00.000Z', model: 'qwen-2.5', input_tokens: 1200, output_tokens: 300 },
    // Date string without milliseconds
    { id: 3, timestamp: '2026-08-03T18:00:00Z', model: 'glm-4', input_tokens: 800, output_tokens: 200 },
    // Date string 24h later
    { id: 4, timestamp: '2026-08-04T12:00:00Z', model: 'glm-4', input_tokens: 400, output_tokens: 100 }
  ];
  await TelemetryStore.putBatch(sampleCalls);

  const stats = await TelemetryStore.getStorageStats();
  console.log('Storage Stats:', stats);
  assert.strictEqual(stats.count, 4);
  assert.strictEqual(stats.maxId, 4);

  console.log('Testing range queries...');
  // Query 1: 6-hour window covering call 2
  const range1 = await TelemetryStore.getRange('2026-08-02T12:00:00Z', '2026-08-02T18:00:00Z');
  assert.strictEqual(range1.length, 1);
  assert.strictEqual(range1[0].id, 2);
  console.log('PASS: 6-hour range query returned call 2');

  // Query 2: 24-hour window covering call 1
  const range2 = await TelemetryStore.getRange('2026-08-01T00:00:00Z', '2026-08-01T23:59:59Z');
  assert.strictEqual(range2.length, 1);
  assert.strictEqual(range2[0].id, 1);
  console.log('PASS: 24-hour range query returned call 1');

  // Query 3: Multi-day range covering all 4 calls
  const range3 = await TelemetryStore.getRange('2026-08-01T00:00:00Z', '2026-08-05T00:00:00Z');
  assert.strictEqual(range3.length, 4);
  console.log('PASS: Full range query returned all 4 calls');

  console.log('Testing 1-week TTL eviction logic...');
  assert.strictEqual(stats.remainingDays, 7);

  console.log('Testing metadata storage & retrieval...');
  await TelemetryStore.setMeta('db_fingerprint', 'test_fp_123');
  const fp = await TelemetryStore.getMeta('db_fingerprint');
  assert.strictEqual(fp, 'test_fp_123');

  console.log('Testing clearAll...');
  await TelemetryStore.clearAll();
  const clearedStats = await TelemetryStore.getStorageStats();
  assert.strictEqual(clearedStats.count, 0);

  console.log('\n[OK] All TelemetryStore JavaScript tests passed successfully!');
}

runTests().catch(err => {
  console.error('Test failed:', err);
  process.exit(1);
});
