/**
 * Unit Test Suite for Smart Multi-Agent Payload Diffing & Call Numbering Logic
 */

const assert = require('assert');

// ── Re-implementing / Extracting logic to test ──
function getCallSeq(record, index) {
  if (record && typeof record.seq === 'number') return record.seq;
  if (record && typeof record._uiSeq === 'number') return record._uiSeq;
  if (typeof index === 'number' && index >= 0) {
    const seq = index + 1;
    if (record) record._uiSeq = seq;
    return seq;
  }
  return 1;
}

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

  const currPrompt = record.request?.prompt;
  if (typeof currPrompt === 'string' && currPrompt.length > 30) {
    let bestPromptMatch = null;
    let maxPromptLen = 0;

    for (let i = idx - 1; i >= 0; i--) {
      const candidate = events[i];
      if (!candidate) continue;
      const candPrompt = candidate.request?.prompt;
      if (typeof candPrompt !== 'string' || candPrompt.length < 20) continue;
      if (candPrompt.length >= currPrompt.length) continue;

      if (currPrompt.startsWith(candPrompt) && candPrompt.length > maxPromptLen) {
        maxPromptLen = candPrompt.length;
        bestPromptMatch = {
          type: 'prompt',
          matchedRecord: candidate,
          matchedSeq: getCallSeq(candidate, i),
          matchedCount: candPrompt.length,
          newPromptSuffix: currPrompt.slice(candPrompt.length),
        };
      }
    }

    if (bestPromptMatch) return bestPromptMatch;
  }

  return null;
}

// ── Test Execution ──
console.log('=== Running Smart Payload Diffing & Sequential Numbering Tests ===\n');

// Test 1: Single conversation sequence
console.log('--- Test 1: Sequential Single-Agent Conversation Diffing ---');
const msgSystem = { role: 'system', content: 'You are a code assistant.' };
const msgUser1 = { role: 'user', content: 'Write a quicksort in python.' };
const msgAssistant1 = { role: 'assistant', content: 'def quicksort(arr): ...' };
const msgUser2 = { role: 'user', content: 'Now optimize it in-place.' };
const msgAssistant2 = { role: 'assistant', content: 'def partition(arr, low, high): ...' };

const events1 = [
  { id: 'call_1', seq: 1, model: 'DeepSeek-V3', request: { messages: [msgSystem, msgUser1] } },
  { id: 'call_2', seq: 2, model: 'DeepSeek-V3', request: { messages: [msgSystem, msgUser1, msgAssistant1, msgUser2] } },
  { id: 'call_3', seq: 3, model: 'DeepSeek-V3', request: { messages: [msgSystem, msgUser1, msgAssistant1, msgUser2, msgAssistant2] } },
];

const diffCall1 = findPredecessorDiff(events1[0], 0, events1);
assert.strictEqual(diffCall1, null, 'Call #1 has no predecessor');

const diffCall2 = findPredecessorDiff(events1[1], 1, events1);
assert.notStrictEqual(diffCall2, null, 'Call #2 matches Call #1');
assert.strictEqual(diffCall2.matchedSeq, 1);
assert.strictEqual(diffCall2.matchedCount, 2);
assert.strictEqual(diffCall2.newMessages.length, 2);
assert.strictEqual(diffCall2.newMessages[0].content, 'def quicksort(arr): ...');
assert.strictEqual(diffCall2.newMessages[1].content, 'Now optimize it in-place.');

const diffCall3 = findPredecessorDiff(events1[2], 2, events1);
assert.notStrictEqual(diffCall3, null, 'Call #3 matches Call #2 as longest prefix');
assert.strictEqual(diffCall3.matchedSeq, 2);
assert.strictEqual(diffCall3.matchedCount, 4);
assert.strictEqual(diffCall3.newMessages.length, 1);
assert.strictEqual(diffCall3.newMessages[0].content, 'def partition(arr, low, high): ...');
console.log('PASS: Single agent conversation turns correctly diff against immediate parent.\n');


// Test 2: Multi-agent interleaved conversation calls
console.log('--- Test 2: Multi-Agent Interleaved Conversation Matching ---');
// Agent A: Coding Agent
const agentA_call1 = { id: 'a1', seq: 1, model: 'DeepSeek-V3', request: { messages: [{ role: 'system', content: 'Agent A' }, { role: 'user', content: 'Task A' }] } };
// Agent B: Research Agent
const agentB_call1 = { id: 'b1', seq: 2, model: 'Qwen-2.5-72B', request: { messages: [{ role: 'system', content: 'Agent B' }, { role: 'user', content: 'Research B' }] } };
// Agent A: Turn 2 (interleaved after Agent B!)
const agentA_call2 = { id: 'a2', seq: 3, model: 'DeepSeek-V3', request: { messages: [{ role: 'system', content: 'Agent A' }, { role: 'user', content: 'Task A' }, { role: 'assistant', content: 'Doing A' }] } };
// Agent B: Turn 2 (interleaved after Agent A!)
const agentB_call2 = { id: 'b2', seq: 4, model: 'Qwen-2.5-72B', request: { messages: [{ role: 'system', content: 'Agent B' }, { role: 'user', content: 'Research B' }, { role: 'assistant', content: 'Doing B' }] } };

const multiAgentEvents = [agentA_call1, agentB_call1, agentA_call2, agentB_call2];

const diffA2 = findPredecessorDiff(agentA_call2, 2, multiAgentEvents);
assert.notStrictEqual(diffA2, null);
assert.strictEqual(diffA2.matchedSeq, 1, 'Agent A Turn 2 matches Agent A Turn 1 (skipping Agent B Call #2)');
assert.strictEqual(diffA2.matchedCount, 2);
assert.strictEqual(diffA2.newMessages.length, 1);

const diffB2 = findPredecessorDiff(agentB_call2, 3, multiAgentEvents);
assert.notStrictEqual(diffB2, null);
assert.strictEqual(diffB2.matchedSeq, 2, 'Agent B Turn 2 matches Agent B Turn 1 (skipping Agent A Call #3)');
assert.strictEqual(diffB2.matchedCount, 2);
assert.strictEqual(diffB2.newMessages.length, 1);
console.log('PASS: Interleaved multi-agent conversations accurately locate their specific thread predecessor.\n');


// Test 3: Complex message equality (tool_calls, multimodal, whitespace)
console.log('--- Test 3: Complex Message Equality & Tool Calls ---');
const msgWithTools1 = {
  role: 'assistant',
  content: null,
  tool_calls: [
    { id: 'call_abc', type: 'function', function: { name: 'read_file', arguments: '{"path": "foo.py"}' } }
  ]
};
const msgWithTools2 = {
  role: 'assistant',
  content: null,
  tool_calls: [
    { id: 'call_abc', type: 'function', function: { name: 'read_file', arguments: '{"path": "foo.py"}' } }
  ]
};
const msgWithToolsDifferent = {
  role: 'assistant',
  content: null,
  tool_calls: [
    { id: 'call_xyz', type: 'function', function: { name: 'write_file', arguments: '{"path": "bar.py"}' } }
  ]
};

assert.strictEqual(messagesEqual(msgWithTools1, msgWithTools2), true, 'Identical tool calls match');
assert.strictEqual(messagesEqual(msgWithTools1, msgWithToolsDifferent), false, 'Different tool calls do not match');

const toolResp1 = { role: 'tool', tool_call_id: 'call_abc', name: 'read_file', content: 'print("hello")' };
const toolResp2 = { role: 'tool', tool_call_id: 'call_abc', name: 'read_file', content: 'print("hello")' };
assert.strictEqual(messagesEqual(toolResp1, toolResp2), true, 'Identical tool responses match');
console.log('PASS: Deep message comparator correctly matches tool calls, tool responses, and content.\n');


// Test 4: Raw prompt prefix diffing
console.log('--- Test 4: Raw Prompt Prefix Diffing ---');
const prompt1 = 'Translate the following English document into Spanish with formal tone: Line 1';
const prompt2 = 'Translate the following English document into Spanish with formal tone: Line 1\nLine 2\nLine 3';
const promptEvents = [
  { id: 'p1', seq: 1, request: { prompt: prompt1 } },
  { id: 'p2', seq: 2, request: { prompt: prompt2 } },
];
const diffP2 = findPredecessorDiff(promptEvents[1], 1, promptEvents);
assert.notStrictEqual(diffP2, null);
assert.strictEqual(diffP2.type, 'prompt');
assert.strictEqual(diffP2.matchedSeq, 1);
assert.strictEqual(diffP2.newPromptSuffix, '\nLine 2\nLine 3');
console.log('PASS: Prompt prefix diffing detects string deltas.\n');


// Test 5: Sequential call numbering fallback
console.log('--- Test 5: Call Numbering Fallback ---');
const unsequencedEvent = { id: 'unseq_1' };
assert.strictEqual(getCallSeq(unsequencedEvent, 4), 5, 'Fallback index + 1 is assigned');
assert.strictEqual(unsequencedEvent._uiSeq, 5, '_uiSeq is cached on event');
console.log('PASS: Call numbering handles unsequenced historical logs seamlessly.\n');

console.log('=====================================================');
console.log('ALL SMART PAYLOAD DIFFING & NUMBERING TESTS PASSED! 🚀');
console.log('=====================================================');
