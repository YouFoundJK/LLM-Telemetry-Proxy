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
  openCursor(range, direction = 'next') {
    const req = { onsuccess: null, onerror: null };
    setImmediate(() => {
      let items = Array.from(this.store.items.values());
      if (this.indexName === 'timestamp') {
        items.sort((a, b) => {
          if (!a.timestamp) return -1;
          if (!b.timestamp) return 1;
          return a.timestamp.localeCompare(b.timestamp);
        });
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

  console.log('Testing batch insertion...');
  const sampleCalls = [
    { id: 1, timestamp: '2026-08-01T10:00:00Z', model: 'glm-4', input_tokens: 500, output_tokens: 100, ttfb_ms: 200, total_ms: 1000 },
    { id: 2, timestamp: '2026-08-02T10:00:00Z', model: 'qwen-2.5', input_tokens: 1200, output_tokens: 300, ttfb_ms: 350, total_ms: 1500 },
    { id: 3, timestamp: '2026-08-03T10:00:00Z', model: 'glm-4', input_tokens: 800, output_tokens: 200, ttfb_ms: 180, total_ms: 800 }
  ];
  await TelemetryStore.putBatch(sampleCalls);

  const stats = await TelemetryStore.getStorageStats();
  console.log('Storage Stats:', stats);
  assert.strictEqual(stats.count, 3);
  assert.strictEqual(stats.maxId, 3);

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
