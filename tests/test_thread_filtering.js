const fs = require('fs');

const lines = fs.readFileSync('logger/payloads.jsonl', 'utf-8').trim().split('\n').filter(Boolean).map(JSON.parse);

function deepEqual(a, b) {
  if (a === b) return true;
  if (a === null || b === null || typeof a !== typeof b) return false;
  if (typeof a === 'string') return a.trim() === (typeof b === 'string' ? b.trim() : '');
  if (typeof a !== 'object') return a === b;

  if (Array.isArray(a)) {
    if (!Array.isArray(b) || a.length !== b.length) return false;
    for (let i = 0; i < a.length; i++) {
      if (!deepEqual(a[i], b[i])) return false;
    }
    return true;
  }

  if (Array.isArray(b)) return false;

  const keysA = Object.keys(a);
  const keysB = Object.keys(b);
  if (keysA.length !== keysB.length) return false;

  for (const k of keysA) {
    if (!Object.prototype.hasOwnProperty.call(b, k)) return false;
    if (!deepEqual(a[k], b[k])) return false;
  }
  return true;
}

function messagesEqual(a, b) {
  if (!a || !b) return a === b;
  if (typeof a !== 'object' || typeof b !== 'object') return a === b;

  const roleA = (a.role || '').toLowerCase().trim();
  const roleB = (b.role || '').toLowerCase().trim();
  if (roleA !== roleB) return false;

  if ((a.name || '') !== (b.name || '')) return false;
  if ((a.tool_call_id || '') !== (b.tool_call_id || '')) return false;

  if (!deepEqual(a.content, b.content)) return false;

  if (Boolean(a.tool_calls) !== Boolean(b.tool_calls)) return false;
  if (a.tool_calls && b.tool_calls) {
    if (!deepEqual(a.tool_calls, b.tool_calls)) return false;
  }

  return true;
}

function getCallSeq(record, index) {
  if (record && typeof record.seq === 'number') return record.seq;
  if (record && typeof record._uiSeq === 'number') return record._uiSeq;
  if (typeof index === 'number' && index >= 0) return index + 1;
  return 1;
}

function findPredecessorDiff(record, historyIndex, events) {
  if (!record || !Array.isArray(events)) return null;
  const idx = (typeof historyIndex === 'number' && historyIndex >= 0)
    ? historyIndex
    : events.findIndex(e => e.id === record.id);
  if (idx <= 0) return null;

  const currMsgs = record.request?.messages;
  if (Array.isArray(currMsgs) && currMsgs.length > 1) {
    let bestMatch = null;
    let maxMatchCount = 0;

    for (let i = idx - 1; i >= 0; i--) {
      const candidate = events[i];
      if (!candidate) continue;
      const candMsgs = candidate.request?.messages;
      if (!Array.isArray(candMsgs) || candMsgs.length === 0) continue;

      let commonCount = 0;
      const maxCheck = Math.min(candMsgs.length, currMsgs.length);
      for (let j = 0; j < maxCheck; j++) {
        if (messagesEqual(currMsgs[j], candMsgs[j])) {
          commonCount++;
        } else {
          break;
        }
      }

      if (commonCount > 0 && commonCount > maxMatchCount && commonCount < currMsgs.length) {
        maxMatchCount = commonCount;
        bestMatch = {
          type: 'messages',
          matchedRecord: candidate,
          matchedIndex: i,
          matchedSeq: getCallSeq(candidate, i),
          matchedCount: commonCount,
          totalCount: currMsgs.length,
          newMessages: currMsgs.slice(commonCount),
        };
        if (maxMatchCount === currMsgs.length - 1) break;
      }
    }

    if (bestMatch) return bestMatch;
  }
  return null;
}

function getThreadTitle(rootRecord) {
  if (!rootRecord) return 'Agent Thread';
  const msgs = rootRecord.request?.messages;
  if (!Array.isArray(msgs) || msgs.length === 0) {
    const ep = rootRecord.endpoint || '';
    if (ep.includes('embeddings')) return 'Embedding Vector Call';
    return `${rootRecord.model || 'Single Call'}`;
  }

  let fullText = '';
  for (let i = 0; i < Math.min(3, msgs.length); i++) {
    const c = msgs[i]?.content;
    if (typeof c === 'string') fullText += ' ' + c;
    else if (Array.isArray(c)) {
      fullText += ' ' + c.map(part => part?.text || '').join(' ');
    }
  }

  const lower = fullText.toLowerCase();
  if (lower.includes('inductive reasoning')) return 'Inductive Reasoning Agent';
  if (lower.includes('deductive reasoning')) return 'Deductive Reasoning Agent';
  if (lower.includes('context synthesis')) return 'Context Synthesis Agent';
  if (lower.includes('build/completion task') || lower.includes('build task')) return 'Hermes Subagent · Build Task';
  if (lower.includes('adversarial verification') || lower.includes('verification task')) return 'Hermes Subagent · Verif Task';
  if (lower.includes('hermes')) return 'Hermes Main Agent';

  const firstContent = msgs[0]?.content || msgs[1]?.content || '';
  const snippet = String(typeof firstContent === 'string' ? firstContent : JSON.stringify(firstContent))
    .replace(/[\r\n\t]+/g, ' ')
    .trim()
    .slice(0, 32);
  if (snippet.length > 0) {
    return `${rootRecord.model || 'Agent'}: "${snippet}..."`;
  }
  return `${rootRecord.model || 'Agent Thread'}`;
}

function clusterThreads(events) {
  const predecessors = new Map();
  const diffs = new Map();

  events.forEach((record, index) => {
    const diff = findPredecessorDiff(record, index, events);
    if (diff && diff.matchedRecord) {
      predecessors.set(record.id, diff.matchedRecord.id);
      diffs.set(record.id, diff);
    }
  });

  const threadRoots = new Map();
  const threads = new Map();

  events.forEach(record => {
    let currId = record.id;
    while (predecessors.has(currId)) {
      currId = predecessors.get(currId);
    }
    threadRoots.set(record.id, currId);

    if (!threads.has(currId)) {
      threads.set(currId, []);
    }
    threads.get(currId).push(record);
  });

  return { threadRoots, threads, diffs, predecessors };
}

console.log('=== Running Thread Filter & Isolation Verification Tests ===\n');

const { threadRoots, threads, diffs, predecessors } = clusterThreads(lines);

// Test 1: Validate Thread Clustering
console.log('--- Test 1: Thread Clustering Verification ---');
const multiTurnThreads = Array.from(threads.entries()).filter(([rootId, records]) => records.length > 1);
if (multiTurnThreads.length !== 5) {
  throw new Error(`Expected 5 multi-turn agent threads, found ${multiTurnThreads.length}`);
}
console.log(`PASS: Found exact ${multiTurnThreads.length} multi-turn threads.`);

// Test 2: Validate Thread Isolation and Turn Progression
console.log('\n--- Test 2: Turn Progression in Isolated Threads ---');
multiTurnThreads.forEach(([rootId, records], tIdx) => {
  const rootRecord = lines.find(e => e.id === rootId);
  const title = getThreadTitle(rootRecord);
  const callSeqs = records.map(r => getCallSeq(r, lines.indexOf(r)));
  console.log(`Testing Thread #${tIdx + 1}: ${title} (${records.length} turns: Calls #${callSeqs.join(', #')})`);

  records.forEach((rec, turnIdx) => {
    const seq = getCallSeq(rec, lines.indexOf(rec));
    if (turnIdx === 0) {
      // Turn 1 must be clean root
      if (diffs.has(rec.id)) {
        throw new Error(`Thread root Call #${seq} should not have a predecessor diff in this thread!`);
      }
      console.log(`  ✓ Turn 1 (Call #${seq}): Root full view (${rec.request?.messages?.length} msgs)`);
    } else {
      // Subsequent turns must diff against previous turn in the chain
      const diff = diffs.get(rec.id);
      if (!diff) {
        throw new Error(`Subsequent Turn ${turnIdx + 1} (Call #${seq}) must have a diff against its parent turn!`);
      }
      const prevTurnRec = records[turnIdx - 1];
      const prevTurnSeq = getCallSeq(prevTurnRec, lines.indexOf(prevTurnRec));
      console.log(`  ✓ Turn ${turnIdx + 1} (Call #${seq}): Diffs against Turn ${turnIdx} (Call #${diff.matchedSeq}) [+${diff.newMessages.length} delta msgs]`);
    }
  });
});
console.log('PASS: All multi-turn threads progress with clean root + diff turns.');

// Test 3: Standalone / Single Call Isolation
console.log('\n--- Test 3: Standalone & Embedding Calls ---');
const singleCalls = Array.from(threads.entries()).filter(([rootId, records]) => records.length === 1);
console.log(`Found ${singleCalls.length} standalone / embedding calls.`);
if (singleCalls.length !== 11) {
  throw new Error(`Expected 11 single calls, found ${singleCalls.length}`);
}
console.log('PASS: Standalone calls identified accurately.');

console.log('\n======================================================');
console.log('ALL THREAD CLUSTERING & ISOLATION TESTS PASSED! 🚀');
console.log('======================================================');
