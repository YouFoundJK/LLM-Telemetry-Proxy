/**
 * Telemetry Dashboard Storage Module (IndexedDB Data Lake)
 * Provides persistent, fast client-side storage for raw telemetry records.
 * Features:
 * - 1-Week TTL auto-eviction (automatically wipes cache if unused for > 7 days)
 * - Indexed range queries on timestamp
 * - Single-transaction bulk insertions
 * - Storage usage estimation & quota safety
 */

const TelemetryStore = (() => {
  const DB_NAME = 'LLM_Telemetry_Lake_v1';
  const DB_VERSION = 1;
  const STORE_CALLS = 'api_calls';
  const STORE_META = 'meta';
  
  // 1-Week Cache Inactivity Cap (7 days in milliseconds)
  const CACHE_TTL_MS = 7 * 24 * 60 * 60 * 1000;

  let _db = null;
  let _initPromise = null;

  /**
   * Open / initialize the IndexedDB database instance.
   */
  function init() {
    if (_db) return Promise.resolve(_db);
    if (_initPromise) return _initPromise;

    _initPromise = new Promise((resolve, reject) => {
      if (!window.indexedDB) {
        console.warn('IndexedDB is not supported by this browser. Running with in-memory fallback.');
        resolve(null);
        return;
      }

      const req = indexedDB.open(DB_NAME, DB_VERSION);

      req.onupgradeneeded = (event) => {
        const db = event.target.result;
        if (!db.objectStoreNames.contains(STORE_CALLS)) {
          const callsStore = db.createObjectStore(STORE_CALLS, { keyPath: 'id' });
          callsStore.createIndex('timestamp', 'timestamp', { unique: false });
          callsStore.createIndex('model', 'model', { unique: false });
          callsStore.createIndex('call_type', 'call_type', { unique: false });
        }
        if (!db.objectStoreNames.contains(STORE_META)) {
          db.createObjectStore(STORE_META, { keyPath: 'key' });
        }
      };

      req.onsuccess = async (event) => {
        _db = event.target.result;
        try {
          await _checkAndEnforceTTL();
        } catch (e) {
          console.warn('Failed to check TTL metadata:', e);
        }
        resolve(_db);
      };

      req.onerror = (event) => {
        console.error('IndexedDB open error:', event.target.error);
        resolve(null); // Resolve with null so app falls back gracefully
      };
    });

    return _initPromise;
  }

  /**
   * Check 1-week TTL cap. If cache was not accessed for > 7 days, wipe all data.
   */
  async function _checkAndEnforceTTL() {
    if (!_db) return;
    const meta = await getMeta('cache_activity');
    const now = Date.now();

    if (meta && meta.last_access_at) {
      const elapsed = now - meta.last_access_at;
      if (elapsed > CACHE_TTL_MS) {
        console.log(`[TelemetryStore] Cache expired (${Math.round(elapsed / 86400000)} days since last access). Auto-evicting entire browser cache.`);
        await clearAll();
        await setMeta('cache_activity', {
          created_at: now,
          last_access_at: now,
          ttl_days: 7
        });
        return;
      }
    }

    // Update last access timestamp
    await setMeta('cache_activity', {
      created_at: meta?.created_at || now,
      last_access_at: now,
      ttl_days: 7
    });
  }

  /**
   * Update the active access timestamp to keep TTL fresh during usage.
   */
  async function touchAccess() {
    if (!_db) return;
    try {
      const meta = await getMeta('cache_activity');
      const now = Date.now();
      await setMeta('cache_activity', {
        created_at: meta?.created_at || now,
        last_access_at: now,
        ttl_days: 7
      });
    } catch (e) {
      // Non-critical
    }
  }

  /**
   * Get metadata record by key.
   */
  function getMeta(key) {
    return new Promise((resolve) => {
      if (!_db) { resolve(null); return; }
      try {
        const tx = _db.transaction(STORE_META, 'readonly');
        const store = tx.objectStore(STORE_META);
        const req = store.get(key);
        req.onsuccess = () => resolve(req.result ? req.result.value : null);
        req.onerror = () => resolve(null);
      } catch (e) {
        resolve(null);
      }
    });
  }

  /**
   * Set metadata record by key.
   */
  function setMeta(key, value) {
    return new Promise((resolve) => {
      if (!_db) { resolve(); return; }
      try {
        const tx = _db.transaction(STORE_META, 'readwrite');
        const store = tx.objectStore(STORE_META);
        store.put({ key, value });
        tx.oncomplete = () => resolve();
        tx.onerror = () => resolve();
      } catch (e) {
        resolve();
      }
    });
  }

  /**
   * Insert or update a batch of call records in a single transaction.
   * Compresses memory by storing lean objects (strips null/undefined properties).
   */
  function putBatch(records) {
    return new Promise((resolve, reject) => {
      if (!_db || !records || records.length === 0) {
        resolve(0);
        return;
      }

      try {
        const tx = _db.transaction([STORE_CALLS, STORE_META], 'readwrite');
        const callsStore = tx.objectStore(STORE_CALLS);

        let maxId = 0;
        let latestTs = '';
        let earliestTs = '';

        for (let i = 0; i < records.length; i++) {
          const r = records[i];
          if (!r || r.id === undefined || r.id === null) continue;

          // Create lean record omitting redundant null/empty fields
          const item = { id: r.id, timestamp: r.timestamp };
          if (r.model) item.model = r.model;
          if (r.endpoint) item.endpoint = r.endpoint;
          if (r.input_tokens) item.input_tokens = r.input_tokens;
          if (r.output_tokens) item.output_tokens = r.output_tokens;
          if (r.ttfb_ms) item.ttfb_ms = r.ttfb_ms;
          if (r.total_ms) item.total_ms = r.total_ms;
          if (r.tokens_per_s) item.tokens_per_s = r.tokens_per_s;
          if (r.server_running !== undefined && r.server_running !== null) item.server_running = r.server_running;
          if (r.status_code) item.status_code = r.status_code;
          if (r.error) item.error = r.error;
          if (r.call_type && r.call_type !== 'chat') item.call_type = r.call_type;
          if (r.calls_count && r.calls_count > 1) item.calls_count = r.calls_count;

          callsStore.put(item);

          if (r.id > maxId) maxId = r.id;
          if (!earliestTs || (r.timestamp && r.timestamp < earliestTs)) earliestTs = r.timestamp;
          if (!latestTs || (r.timestamp && r.timestamp > latestTs)) latestTs = r.timestamp;
        }

        tx.oncomplete = () => {
          touchAccess();
          resolve(records.length);
        };
        tx.onerror = (e) => {
          console.error('Failed to put batch into IndexedDB:', e);
          reject(e);
        };
      } catch (e) {
        console.error('Transaction error in putBatch:', e);
        reject(e);
      }
    });
  }

  /**
   * Query records within a specific ISO timestamp range [fromTs, toTs].
   */
  function getRange(fromTs, toTs) {
    return new Promise((resolve) => {
      if (!_db) { resolve([]); return; }
      try {
        const tx = _db.transaction(STORE_CALLS, 'readonly');
        const store = tx.objectStore(STORE_CALLS);
        const index = store.index('timestamp');

        let keyRange = null;
        if (fromTs && toTs) {
          keyRange = IDBKeyRange.bound(fromTs, toTs);
        } else if (fromTs) {
          keyRange = IDBKeyRange.lowerBound(fromTs);
        } else if (toTs) {
          keyRange = IDBKeyRange.upperBound(toTs);
        }

        const results = [];
        const req = index.openCursor(keyRange);

        req.onsuccess = (event) => {
          const cursor = event.target.result;
          if (cursor) {
            results.push(cursor.value);
            cursor.continue();
          } else {
            touchAccess();
            resolve(results);
          }
        };

        req.onerror = (e) => {
          console.warn('IndexedDB cursor error:', e);
          resolve([]);
        };
      } catch (e) {
        console.warn('IndexedDB getRange error:', e);
        resolve([]);
      }
    });
  }

  /**
   * Get max ID and earliest/latest timestamps currently cached in IndexedDB.
   */
  function getWatermarks() {
    return new Promise((resolve) => {
      if (!_db) { resolve({ count: 0, maxId: 0, earliestTs: null, latestTs: null }); return; }
      try {
        const tx = _db.transaction(STORE_CALLS, 'readonly');
        const store = tx.objectStore(STORE_CALLS);
        const tsIndex = store.index('timestamp');

        let earliestTs = null;
        let latestTs = null;
        let maxId = 0;
        let count = 0;

        const countReq = store.count();
        countReq.onsuccess = () => {
          count = countReq.result;

          // Earliest timestamp (first in index)
          const firstReq = tsIndex.openCursor(null, 'next');
          firstReq.onsuccess = (e) => {
            const cursor = e.target.result;
            if (cursor) earliestTs = cursor.value.timestamp;

            // Latest timestamp (last in index)
            const lastReq = tsIndex.openCursor(null, 'prev');
            lastReq.onsuccess = (e2) => {
              const lastCursor = e2.target.result;
              if (lastCursor) latestTs = lastCursor.value.timestamp;

              // Max ID
              const maxIdReq = store.openCursor(null, 'prev');
              maxIdReq.onsuccess = (e3) => {
                const idCursor = e3.target.result;
                if (idCursor) maxId = idCursor.value.id || 0;

                resolve({ count, maxId, earliestTs, latestTs });
              };
            };
          };
        };

        countReq.onerror = () => resolve({ count: 0, maxId: 0, earliestTs: null, latestTs: null });
      } catch (e) {
        resolve({ count: 0, maxId: 0, earliestTs: null, latestTs: null });
      }
    });
  }

  /**
   * Clear all records and metadata from the database.
   */
  function clearAll() {
    return new Promise((resolve) => {
      if (!_db) { resolve(); return; }
      try {
        const tx = _db.transaction([STORE_CALLS, STORE_META], 'readwrite');
        tx.objectStore(STORE_CALLS).clear();
        tx.objectStore(STORE_META).clear();
        tx.oncomplete = () => {
          console.log('[TelemetryStore] IndexedDB cache completely cleared.');
          resolve();
        };
        tx.onerror = () => resolve();
      } catch (e) {
        resolve();
      }
    });
  }

  /**
   * Get storage stats & TTL status for UI display.
   */
  async function getStorageStats() {
    const watermarks = await getWatermarks();
    const meta = await getMeta('cache_activity');
    const dbFingerprint = await getMeta('db_fingerprint');

    const now = Date.now();
    const lastAccess = meta?.last_access_at || now;
    const expiresAt = lastAccess + CACHE_TTL_MS;
    const remainingDays = Math.max(0, Math.ceil((expiresAt - now) / 86400000));

    // Approximate size in MB (average compact row size in IDB is ~120-150 bytes)
    const estimatedBytes = watermarks.count * 135;
    const estimatedSizeMb = (estimatedBytes / (1024 * 1024)).toFixed(1);

    return {
      count: watermarks.count,
      maxId: watermarks.maxId,
      earliestTs: watermarks.earliestTs,
      latestTs: watermarks.latestTs,
      estimatedSizeMb: Number(estimatedSizeMb),
      lastAccessAt: new Date(lastAccess).toISOString(),
      expiresAt: new Date(expiresAt).toISOString(),
      remainingDays: remainingDays,
      dbFingerprint: dbFingerprint
    };
  }

  return {
    init,
    putBatch,
    getRange,
    getWatermarks,
    getMeta,
    setMeta,
    touchAccess,
    clearAll,
    getStorageStats,
    CACHE_TTL_MS
  };
})();
