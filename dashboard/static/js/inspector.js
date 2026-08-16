/**
 * Real-Time LLM Payload Inspector Client
 * Streams raw incoming/outgoing JSON schemas from the proxy and renders foldable interactive cards.
 */

(() => {
  const STORAGE_KEY = 'inspector_selected_models';

  function getStoredModels() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed) && parsed.length > 0) return parsed;
      }
    } catch (_) {}
    return null; // null represents "All Models"
  }

  function saveStoredModels(models) {
    try {
      if (models === null) {
        localStorage.removeItem(STORAGE_KEY);
      } else {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(models));
      }
    } catch (_) {}
  }

  // State
  const State = {
    events: [],
    models: new Set(),
    selectedModels: getStoredModels(), // null = All Models, or Array of strings
    sse: null,
    isLoggingEnabled: false,
    autoScroll: true,
    filterText: '',
    filterStatus: 'all',
    maxDisplayed: 200,
    globalDiffMode: true,
    cardDiffOverrides: new Map(), // Map<string|number, 'diff' | 'full'>
  };

  // DOM Elements
  let els = {};

  document.addEventListener('DOMContentLoaded', () => {
    cacheElements();
    setupListeners();
    initInspector();
  });

  function cacheElements() {
    els = {
      feed: document.getElementById('payloadFeed'),
      emptyState: document.getElementById('initialEmptyState'),
      liveConnDot: document.getElementById('liveConnDot'),
      liveConnText: document.getElementById('liveConnText'),
      toggleBtn: document.getElementById('inspectorToggleLoggingBtn'),
      toggleBtnText: document.getElementById('inspectorToggleLoggingText'),
      toggleAllDiffsBtn: document.getElementById('toggleAllDiffsBtn'),
      toggleAllDiffsText: document.getElementById('toggleAllDiffsText'),
      autoScrollCheck: document.getElementById('autoScrollCheck'),
      expandAllBtn: document.getElementById('expandAllBtn'),
      collapseAllBtn: document.getElementById('collapseAllBtn'),
      clearScreenBtn: document.getElementById('clearScreenBtn'),
      clearDiskFileBtn: document.getElementById('clearDiskFileBtn'),
      emptyStateEnableBtn: document.getElementById('emptyStateEnableBtn'),
      searchInput: document.getElementById('searchInput'),
      modelSelectWrapper: document.getElementById('inspectorModelSelectWrapper'),
      modelSelectTrigger: document.getElementById('inspectorModelSelectTrigger'),
      modelDropdown: document.getElementById('inspectorModelDropdown'),
      statusFilterSelect: document.getElementById('statusFilterSelect'),
      metaFilePath: document.getElementById('metaFilePath'),
      metaFileSize: document.getElementById('metaFileSize'),
      metaTotalCalls: document.getElementById('metaTotalCalls'),
    };
  }

  function setupListeners() {
    // Logging toggle
    if (els.toggleBtn) {
      els.toggleBtn.addEventListener('click', handleToggleLogging);
    }
    if (els.emptyStateEnableBtn) {
      els.emptyStateEnableBtn.addEventListener('click', handleToggleLogging);
    }

    // Global Diff Mode Toggle
    if (els.toggleAllDiffsBtn) {
      els.toggleAllDiffsBtn.addEventListener('click', () => {
        State.globalDiffMode = !State.globalDiffMode;
        State.cardDiffOverrides.clear();
        updateGlobalDiffBtnDisplay();
        renderFeed();
      });
    }

    // Auto-scroll checkbox
    if (els.autoScrollCheck) {
      els.autoScrollCheck.addEventListener('change', (e) => {
        State.autoScroll = e.target.checked;
      });
    }

    // Expand / Collapse All
    if (els.expandAllBtn) {
      els.expandAllBtn.addEventListener('click', () => {
        document.querySelectorAll('.payload-card .card-body').forEach(b => b.style.display = 'flex');
        document.querySelectorAll('.payload-card .card-expand-icon').forEach(i => i.textContent = '▼');
      });
    }
    if (els.collapseAllBtn) {
      els.collapseAllBtn.addEventListener('click', () => {
        document.querySelectorAll('.payload-card .card-body').forEach(b => b.style.display = 'none');
        document.querySelectorAll('.payload-card .card-expand-icon').forEach(i => i.textContent = '▶');
      });
    }

    // Clear Feed (Screen only)
    if (els.clearScreenBtn) {
      els.clearScreenBtn.addEventListener('click', () => {
        State.events = [];
        renderFeed();
      });
    }

    // Truncate File on Disk
    if (els.clearDiskFileBtn) {
      els.clearDiskFileBtn.addEventListener('click', async () => {
        if (!confirm('Truncate the raw payload log file on disk (logger/payloads.jsonl)?')) return;
        try {
          await TelemetryAPI.clearRawLogs();
          State.events = [];
          renderFeed();
          await updateStatusMeta();
        } catch (err) {
          alert('Failed to clear log file: ' + err.message);
        }
      });
    }

    // Search and Filters
    if (els.searchInput) {
      els.searchInput.addEventListener('input', (e) => {
        State.filterText = e.target.value.toLowerCase().trim();
        applyClientFilters();
      });
    }

    // Custom Model Dropdown Trigger Open / Close
    if (els.modelSelectTrigger && els.modelSelectWrapper) {
      els.modelSelectTrigger.addEventListener('click', (e) => {
        e.stopPropagation();
        els.modelSelectWrapper.classList.toggle('open');
      });
    }

    // Close Dropdown when clicking outside
    document.addEventListener('click', (e) => {
      if (els.modelSelectWrapper && !els.modelSelectWrapper.contains(e.target)) {
        els.modelSelectWrapper.classList.remove('open');
      }
    });

    if (els.statusFilterSelect) {
      els.statusFilterSelect.addEventListener('change', (e) => {
        State.filterStatus = e.target.value;
        applyClientFilters();
      });
    }

    window.addEventListener('resize', updateHeaderHeight);
  }

  function updateHeaderHeight() {
    const nav = document.querySelector('.inspector-header');
    if (nav) {
      document.documentElement.style.setProperty('--inspector-header-height', `${nav.offsetHeight}px`);
    }
  }

  async function initInspector() {
    updateHeaderHeight();
    updateGlobalDiffBtnDisplay();
    await updateStatusMeta();
    await loadRecentEvents();
    connectSSE();

    // Periodic poll for status & file size
    setInterval(updateStatusMeta, 5000);
  }

  function updateGlobalDiffBtnDisplay() {
    if (!els.toggleAllDiffsBtn || !els.toggleAllDiffsText) return;
    if (State.globalDiffMode) {
      els.toggleAllDiffsBtn.className = 'btn-mini btn-mini-primary';
      els.toggleAllDiffsText.textContent = 'Diff Mode: ON';
    } else {
      els.toggleAllDiffsBtn.className = 'btn-mini';
      els.toggleAllDiffsText.textContent = 'Diff Mode: OFF';
    }
  }

  function updateStatusMetaDisplay() {
    updateGlobalDiffBtnDisplay();
    if (els.metaFilePath && State.metaFilePath) {
      els.metaFilePath.textContent = State.metaFilePath;
    }
    if (els.metaFileSize && State.metaFileSize) {
      els.metaFileSize.textContent = State.metaFileSize;
    }

    if (els.toggleBtn && els.toggleBtnText) {
      if (State.isLoggingEnabled) {
        els.toggleBtn.className = 'btn-mini';
        els.toggleBtn.style.color = '#f85149';
        els.toggleBtn.style.borderColor = 'rgba(248, 81, 73, 0.4)';
        els.toggleBtnText.textContent = 'Disable Logging';
      } else {
        els.toggleBtn.className = 'btn-mini btn-mini-primary';
        els.toggleBtn.style.color = '';
        els.toggleBtn.style.borderColor = '';
        els.toggleBtnText.textContent = 'Enable Logging';
      }
    }
  }

  /**
   * Fetch current logging state & file size from server
   */
  async function updateStatusMeta() {
    try {
      const status = await TelemetryAPI.getRawLogStatus();
      const prevLoggingState = State.isLoggingEnabled;
      State.isLoggingEnabled = Boolean(status.enabled);
      State.metaFilePath = status.rel_path || 'logger/payloads.jsonl';
      State.metaFileSize = status.file_size_formatted || '0 B';

      updateStatusMetaDisplay();

      // Re-render empty state if logging state changed and no events exist yet
      if (State.events.length === 0 && prevLoggingState !== State.isLoggingEnabled) {
        renderFeed();
      }
    } catch (e) {
      console.warn('Failed to update status meta', e);
    }
  }

  /**
   * Toggle raw logging state
   */
  async function handleToggleLogging() {
    try {
      if (els.toggleBtn) els.toggleBtn.disabled = true;
      const res = await TelemetryAPI.toggleRawLog();
      State.isLoggingEnabled = Boolean(res.enabled);
      State.metaFilePath = res.rel_path || State.metaFilePath || 'logger/payloads.jsonl';
      State.metaFileSize = res.file_size_formatted || State.metaFileSize || '0 B';
      updateStatusMetaDisplay();
      if (State.events.length === 0) {
        renderFeed();
      }
    } catch (e) {
      alert('Error toggling logging: ' + e.message);
    } finally {
      if (els.toggleBtn) els.toggleBtn.disabled = false;
    }
  }

  /**
   * Load recent events from disk
   */
  async function loadRecentEvents() {
    try {
      const data = await TelemetryAPI.getRecentRawLogs(200);
      if (data && data.entries && data.entries.length > 0) {
        // data.entries is sorted newest first by server
        for (const entry of data.entries.reverse()) {
          appendEvent(entry, false);
        }
        updateModelDropdown();
      }
    } catch (e) {
      console.warn('Failed to load recent logs', e);
    } finally {
      renderFeed();
    }
  }

  let sseReconnectTimer = null;

  /**
   * Connect to Server-Sent Events (SSE) live feed
   */
  function connectSSE() {
    if (State.sse) {
      try { State.sse.close(); } catch (_) {}
      State.sse = null;
    }
    if (sseReconnectTimer) {
      clearTimeout(sseReconnectTimer);
      sseReconnectTimer = null;
    }

    setConnStatus('connecting', 'Connecting...');
    const streamUrl = TelemetryAPI.getRawLogStreamUrl();
    const sse = new EventSource(streamUrl);
    State.sse = sse;

    sse.onopen = () => {
      setConnStatus('connected', 'Live (Connected)');
    };

    sse.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data);
        if (payload.type === 'connected') {
          setConnStatus('connected', 'Live (Connected)');
          if (typeof payload.enabled === 'boolean') {
            const prev = State.isLoggingEnabled;
            State.isLoggingEnabled = payload.enabled;
            updateStatusMetaDisplay();
            if (State.events.length === 0 && prev !== State.isLoggingEnabled) {
              renderFeed();
            }
          }
          return;
        }
        if (payload.type === 'status_update') {
          const prev = State.isLoggingEnabled;
          State.isLoggingEnabled = Boolean(payload.enabled);
          updateStatusMetaDisplay();
          if (State.events.length === 0 && prev !== State.isLoggingEnabled) {
            renderFeed();
          }
          return;
        }
        appendEvent(payload, true);
      } catch (err) {
        console.warn('Error parsing SSE event:', err);
      }
    };

    sse.onerror = () => {
      setConnStatus('error', 'Reconnecting...');
      if (!sseReconnectTimer) {
        sseReconnectTimer = setTimeout(() => {
          connectSSE();
        }, 3000);
      }
    };
  }

  function setConnStatus(state, text) {
    if (!els.liveConnDot || !els.liveConnText) return;
    els.liveConnText.textContent = text;

    if (state === 'connected') {
      els.liveConnDot.style.background = '#3fb950';
      els.liveConnDot.style.boxShadow = '0 0 8px rgba(63, 185, 80, 0.6)';
    } else if (state === 'connecting') {
      els.liveConnDot.style.background = '#d29922';
      els.liveConnDot.style.boxShadow = 'none';
    } else {
      els.liveConnDot.style.background = '#f85149';
      els.liveConnDot.style.boxShadow = 'none';
    }
  }

  function getModelBadgeClass(model) {
    if (!model) return 'tag-other';
    const m = model.toLowerCase();
    if (m.includes('glm')) return 'tag-glm';
    if (m.includes('qwen')) return 'tag-qwen';
    if (m.includes('gemma')) return 'tag-gemma';
    if (m.includes('deepseek')) return 'tag-deepseek';
    if (m.includes('gpt')) return 'tag-gpt';
    if (m.includes('claude')) return 'tag-claude';
    return 'tag-other';
  }

  /**
   * Append or update a raw payload event in the feed without destroying existing DOM nodes
   */
  function appendEvent(record, renderImmediately = true) {
    if (!record || !record.id) return;

    // Check if event already exists in State (e.g. updating in-progress to completed)
    const existingIndex = State.events.findIndex(e => e.id === record.id);
    let isUpdate = false;
    if (existingIndex >= 0) {
      State.events[existingIndex] = record;
      isUpdate = true;
    } else {
      State.events.push(record);
      if (State.events.length > State.maxDisplayed) {
        const removed = State.events.shift();
        if (removed && els.feed) {
          const oldCard = els.feed.querySelector(`[data-event-id="${removed.id}"]`);
          if (oldCard) oldCard.remove();
        }
      }
    }

    let hasNewModel = false;
    if (record.model && !State.models.has(record.model)) {
      State.models.add(record.model);
      hasNewModel = true;
    }

    if (hasNewModel) {
      updateModelDropdown();
    }

    if (renderImmediately && els.feed) {
      // Remove empty state placeholder if present
      const emptyStateEl = els.feed.querySelector('.empty-state');
      if (emptyStateEl) emptyStateEl.remove();

      const eventIdx = existingIndex >= 0 ? existingIndex : State.events.length - 1;
      const cardEl = createPayloadCard(record, eventIdx);
      cardEl.setAttribute('data-event-id', record.id);

      const isVisible = matchesFilters(record);
      if (!isVisible) {
        cardEl.style.display = 'none';
      }

      if (existingCard) {
        // Preserve open/collapsed state of existing card
        const oldBody = existingCard.querySelector('.card-body');
        const newBody = cardEl.querySelector('.card-body');
        if (oldBody && newBody && oldBody.style.display === 'none') {
          newBody.style.display = 'none';
          const newIcon = cardEl.querySelector('.card-expand-icon');
          if (newIcon) newIcon.textContent = '▶';
        }
        existingCard.replaceWith(cardEl);
      } else {
        els.feed.appendChild(cardEl);
        if (State.autoScroll && isVisible) {
          window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
        }
      }

      updateDisplayedCount();
    }
  }

  /**
   * Rebuild or update the multi-select model dropdown with checkboxes
   */
  function updateModelDropdown() {
    if (!els.modelDropdown || !els.modelSelectTrigger) return;

    const availableModels = Array.from(State.models).sort();
    if (availableModels.length === 0) {
      els.modelSelectTrigger.textContent = 'All Models';
      els.modelDropdown.innerHTML = '<div class="custom-option" style="color: #8b949e; cursor: default; padding: 8px 12px;">No models detected yet</div>';
      return;
    }

    // If State.selectedModels is null, all available models are checked
    const isAllChecked = State.selectedModels === null || (
      Array.isArray(State.selectedModels) && availableModels.every(m => State.selectedModels.includes(m))
    );

    let html = `
      <div class="custom-option select-all-btn" id="inspectorSelectAllModelsBtn">
        <input type="checkbox" id="chk_inspector_all_models" ${isAllChecked ? 'checked' : ''}>
        <span style="font-weight: 600;">All Models</span>
      </div>
    `;

    html += availableModels.map(m => {
      const isChecked = isAllChecked || (Array.isArray(State.selectedModels) && State.selectedModels.includes(m));
      const badgeCls = getModelBadgeClass(m);
      return `
        <div class="custom-option model-option" data-model="${escapeHtml(m)}">
          <input type="checkbox" value="${escapeHtml(m)}" ${isChecked ? 'checked' : ''}>
          <span class="tag ${badgeCls}">${escapeHtml(m)}</span>
        </div>
      `;
    }).join('');

    els.modelDropdown.innerHTML = html;

    const checkAll = document.getElementById('chk_inspector_all_models');
    const itemChecks = els.modelDropdown.querySelectorAll('.model-option input[type="checkbox"]');

    function syncTriggerAndState() {
      const checkedInputs = els.modelDropdown.querySelectorAll('.model-option input[type="checkbox"]:checked');
      const checkedCount = checkedInputs.length;

      if (checkedCount === itemChecks.length) {
        els.modelSelectTrigger.textContent = 'All Models';
        if (checkAll) checkAll.checked = true;
        State.selectedModels = null;
        saveStoredModels(null);
      } else if (checkedCount === 0) {
        els.modelSelectTrigger.textContent = '0 Models Selected';
        if (checkAll) checkAll.checked = false;
        State.selectedModels = [];
        saveStoredModels([]);
      } else {
        if (checkedCount === 1) {
          els.modelSelectTrigger.textContent = checkedInputs[0].value;
        } else {
          els.modelSelectTrigger.textContent = `${checkedCount} Models Selected`;
        }
        if (checkAll) checkAll.checked = false;
        State.selectedModels = Array.from(checkedInputs).map(i => i.value);
        saveStoredModels(State.selectedModels);
      }
    }

    // Attach click events on option rows
    els.modelDropdown.querySelectorAll('.custom-option').forEach(opt => {
      opt.addEventListener('click', (e) => {
        e.stopPropagation();
        const chk = opt.querySelector('input[type="checkbox"]');
        if (!chk) return;

        if (e.target !== chk) {
          chk.checked = !chk.checked;
        }

        if (opt.id === 'inspectorSelectAllModelsBtn') {
          itemChecks.forEach(i => i.checked = chk.checked);
        } else {
          const checkedCount = els.modelDropdown.querySelectorAll('.model-option input[type="checkbox"]:checked').length;
          if (checkAll) checkAll.checked = (checkedCount === itemChecks.length);
        }

        syncTriggerAndState();
        applyClientFilters();
      });
    });

    // Initialize trigger text display
    const checkedInputs = els.modelDropdown.querySelectorAll('.model-option input[type="checkbox"]:checked');
    const checkedCount = checkedInputs.length;
    if (checkedCount === itemChecks.length || State.selectedModels === null) {
      els.modelSelectTrigger.textContent = 'All Models';
      if (checkAll) checkAll.checked = (checkedCount === itemChecks.length);
    } else if (checkedCount === 0) {
      els.modelSelectTrigger.textContent = '0 Models Selected';
      if (checkAll) checkAll.checked = false;
    } else if (checkedCount === 1) {
      els.modelSelectTrigger.textContent = checkedInputs[0].value;
      if (checkAll) checkAll.checked = false;
    } else {
      els.modelSelectTrigger.textContent = `${checkedCount} Models Selected`;
      if (checkAll) checkAll.checked = false;
    }
  }

  /**
   * Render or re-filter all events in the feed
   */
  function renderFeed() {
    if (!els.feed) return;

    if (State.events.length === 0) {
      if (State.isLoggingEnabled) {
        els.feed.innerHTML = `
          <div class="empty-state" id="initialEmptyState">
            <div style="font-size: 32px; margin-bottom: 10px;">📡</div>
            <h3 style="color: #3fb950; margin-bottom: 8px;">Live Payload Logging is Active</h3>
            <p style="color: #8b949e; max-width: 540px; margin: 0 auto 16px; line-height: 1.6;">
              The proxy is actively recording incoming & outgoing LLM payloads. Send requests through the proxy (e.g. <code>/v1/chat/completions</code>) to stream payloads here live.
            </p>
            <div style="display: inline-flex; align-items: center; gap: 8px; padding: 6px 16px; background: rgba(63, 185, 80, 0.1); border: 1px solid rgba(63, 185, 80, 0.3); border-radius: 20px; font-size: 12px; color: #3fb950;">
              <span class="live-dot" style="width: 8px; height: 8px; border-radius: 50%; background: #3fb950; display: inline-block; box-shadow: 0 0 8px rgba(63, 185, 80, 0.8);"></span>
              <span>Awaiting incoming requests...</span>
            </div>
          </div>
        `;
      } else {
        els.feed.innerHTML = `
          <div class="empty-state" id="initialEmptyState">
            <div style="font-size: 32px; margin-bottom: 10px;">⏸️</div>
            <h3 style="margin-bottom: 8px;">Raw Payload Logging is Paused</h3>
            <p style="color: #8b949e; max-width: 540px; margin: 0 auto 16px; line-height: 1.6;">
              Ensure Raw Payload Logging is toggled <strong>ON</strong>. As requests pass through the proxy, complete incoming & outgoing JSON schemas will stream here in real-time.
            </p>
            <button id="emptyStateEnableBtn" class="btn-mini btn-mini-primary" style="padding: 7px 18px; font-size: 13px;">
              ⚡ Turn ON Raw Payload Logging
            </button>
          </div>
        `;
        els.feed.querySelector('#emptyStateEnableBtn')?.addEventListener('click', handleToggleLogging);
      }

      if (els.metaTotalCalls) els.metaTotalCalls.textContent = '0';
      return;
    }

    els.feed.innerHTML = '';
    let visibleCount = 0;

    // Render events in chronological order
    State.events.forEach((record, index) => {
      const isVisible = matchesFilters(record);
      const cardEl = createPayloadCard(record, index);
      cardEl.setAttribute('data-event-id', record.id);
      if (!isVisible) {
        cardEl.style.display = 'none';
      } else {
        visibleCount++;
      }
      els.feed.appendChild(cardEl);
    });

    if (els.metaTotalCalls) {
      els.metaTotalCalls.textContent = `${visibleCount} / ${State.events.length}`;
    }
  }

  function applyClientFilters() {
    if (!els.feed) return;
    let visibleCount = 0;

    State.events.forEach((record) => {
      const card = els.feed.querySelector(`[data-event-id="${record.id}"]`);
      if (!card) return;
      const match = matchesFilters(record);
      card.style.display = match ? '' : 'none';
      if (match) visibleCount++;
    });

    if (els.metaTotalCalls) {
      els.metaTotalCalls.textContent = `${visibleCount} / ${State.events.length}`;
    }
  }

  function updateDisplayedCount() {
    if (!els.metaTotalCalls || !els.feed) return;
    const cards = els.feed.querySelectorAll('.payload-card');
    let visibleCount = 0;
    cards.forEach(c => {
      if (c.style.display !== 'none') visibleCount++;
    });
    els.metaTotalCalls.textContent = `${visibleCount} / ${State.events.length}`;
  }

  function matchesFilters(record) {
    // Multi-model filter
    if (State.selectedModels !== null && Array.isArray(State.selectedModels)) {
      if (State.selectedModels.length === 0) {
        return false;
      }
      if (!record.model || !State.selectedModels.includes(record.model)) {
        return false;
      }
    }

    const isInProgress = record.status === 'in_progress';
    const status = record.response?.status_code;

    // Status filter
    if (State.filterStatus === 'success') {
      if (isInProgress || status !== 200) return false;
    }
    if (State.filterStatus === 'errors') {
      if (isInProgress || (!status || status === 200)) return false;
    }

    // Search query filter
    if (State.filterText) {
      const q = State.filterText;
      const modelMatch = record.model && record.model.toLowerCase().includes(q);
      const endpointMatch = record.endpoint && record.endpoint.toLowerCase().includes(q);
      const contentMatch = record.response?.content?.text && record.response.content.text.toLowerCase().includes(q);
      const reasoningMatch = record.response?.content?.reasoning_content && record.response.content.reasoning_content.toLowerCase().includes(q);
      const promptMatch = record.request?.prompt && String(record.request.prompt).toLowerCase().includes(q);
      const inputMatch = record.request?.input && JSON.stringify(record.request.input).toLowerCase().includes(q);
      
      let msgsMatch = false;
      if (record.request?.messages && Array.isArray(record.request.messages)) {
        msgsMatch = record.request.messages.some(m => {
          const content = m.content ? String(m.content).toLowerCase() : '';
          const role = m.role ? String(m.role).toLowerCase() : '';
          return content.includes(q) || role.includes(q);
        });
      }

      if (!modelMatch && !endpointMatch && !contentMatch && !reasoningMatch && !promptMatch && !inputMatch && !msgsMatch) {
        return false;
      }
    }

    return true;
  }

  // ── Call Numbering, Diffing & Jump Navigation Helpers ───────────────────────

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

  /**
   * Find the longest prefix predecessor across earlier calls (handles multi-agent interleaved logs)
   */
  function findPredecessorDiff(record, historyIndex) {
    if (!record || !Array.isArray(State.events)) return null;
    const idx = (typeof historyIndex === 'number' && historyIndex >= 0)
      ? historyIndex
      : State.events.findIndex(e => e.id === record.id);
    if (idx <= 0) return null;

    const currMsgs = record.request?.messages;
    if (Array.isArray(currMsgs) && currMsgs.length > 1) {
      let bestMatch = null;
      let maxMatchCount = 0;

      for (let i = idx - 1; i >= 0; i--) {
        const candidate = State.events[i];
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

    // Fallback for prompt strings (e.g. /v1/completions)
    const currPrompt = record.request?.prompt;
    if (typeof currPrompt === 'string' && currPrompt.length > 30) {
      let bestPromptMatch = null;
      let maxPromptLen = 0;

      for (let i = idx - 1; i >= 0; i--) {
        const candidate = State.events[i];
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

  let returnToastEl = null;
  let returnToastTimer = null;

  function jumpToCall(targetSeq, fromSeq) {
    const targetCard = els.feed?.querySelector(`[data-call-seq="${targetSeq}"]`);
    if (!targetCard) {
      alert(`Call #${targetSeq} is no longer in the active screen buffer.`);
      return;
    }

    // Expand target card body if collapsed
    const body = targetCard.querySelector('.card-body');
    if (body) body.style.display = 'flex';
    const icon = targetCard.querySelector('.card-expand-icon');
    if (icon) icon.textContent = '▼';

    // Expand target card messages section if collapsed
    const msgFold = targetCard.querySelector('.messages-foldable-content');
    if (msgFold) msgFold.style.display = 'flex';
    const msgFoldIcon = targetCard.querySelector('.messages-fold-icon');
    if (msgFoldIcon) msgFoldIcon.textContent = '▼';

    targetCard.scrollIntoView({ behavior: 'smooth', block: 'center' });

    targetCard.classList.remove('call-highlight-pulse');
    void targetCard.offsetWidth; // trigger reflow
    targetCard.classList.add('call-highlight-pulse');
    setTimeout(() => targetCard.classList.remove('call-highlight-pulse'), 2200);

    if (fromSeq && fromSeq !== targetSeq) {
      showReturnToast(targetSeq, fromSeq);
    }
  }

  function showReturnToast(targetSeq, fromSeq) {
    if (returnToastEl) {
      returnToastEl.remove();
      returnToastEl = null;
    }
    if (returnToastTimer) {
      clearTimeout(returnToastTimer);
      returnToastTimer = null;
    }

    const toast = document.createElement('div');
    toast.className = 'jump-return-toast';
    toast.innerHTML = `
      <span>Jumped to <strong>Call #${targetSeq}</strong></span>
      <button class="btn-mini btn-mini-primary btn-return-jump" style="padding: 4px 10px; font-weight: 600;">
        ↩ Return to Call #${fromSeq}
      </button>
      <button class="btn-mini btn-close-toast" style="padding: 2px 6px;">✕</button>
    `;

    toast.querySelector('.btn-return-jump')?.addEventListener('click', () => {
      jumpToCall(fromSeq, null);
      toast.remove();
      returnToastEl = null;
    });

    toast.querySelector('.btn-close-toast')?.addEventListener('click', () => {
      toast.remove();
      returnToastEl = null;
    });

    document.body.appendChild(toast);
    returnToastEl = toast;

    returnToastTimer = setTimeout(() => {
      if (returnToastEl) {
        returnToastEl.remove();
        returnToastEl = null;
      }
    }, 12000);
  }

  /**
   * Build the complete DOM element for a payload card
   */
  function createPayloadCard(record, historyIndex) {
    const card = document.createElement('div');
    const isInProgress = record.status === 'in_progress';
    const isError = !isInProgress && Boolean(record.response?.error || (record.response?.status_code && record.response.status_code >= 400));
    
    card.className = `payload-card ${isInProgress ? 'in-progress-call' : (isError ? 'error-call' : 'success-call')}`;

    const seq = getCallSeq(record, historyIndex);
    card.setAttribute('data-event-id', record.id);
    card.setAttribute('data-call-seq', String(seq));
    card.id = `call-${seq}`;

    const diffInfo = findPredecessorDiff(record, historyIndex);
    const override = State.cardDiffOverrides.get(seq) || State.cardDiffOverrides.get(record.id);
    const isDiffActive = override ? (override === 'diff') : (State.globalDiffMode && Boolean(diffInfo));

    const usage = record.response?.usage || {};
    const inTokens = usage.prompt_tokens ?? (record.request?.messages?.length ? `${record.request.messages.length} msgs` : (record.request?.prompt ? '1 prompt' : '—'));
    const outTokens = usage.completion_tokens ?? (isInProgress ? '...' : (isError ? 0 : '—'));
    const reasoningTokens = usage.reasoning_tokens ?? 0;
    const totalTokens = usage.total_tokens ?? (typeof inTokens === 'number' && typeof outTokens === 'number' ? inTokens + outTokens : '—');

    const totalMs = record.response?.total_ms ? `${(record.response.total_ms / 1000).toFixed(2)}s` : (isInProgress ? 'Streaming...' : '—');
    const ttfbMs = record.response?.ttfb_ms ? `${Math.round(record.response.ttfb_ms)}ms` : '—';
    const tps = record.response?.tokens_per_s ? `${Math.round(record.response.tokens_per_s)} tok/s` : '';

    const timestamp = record.timestamp ? new Date(record.timestamp).toLocaleTimeString() : '';
    const status = record.response?.status_code || (isInProgress ? 'IN-FLIGHT' : (isError ? 'ERR' : 200));

    let statusPillHtml;
    if (isInProgress) {
      statusPillHtml = `<span class="status-pill status-inflight"><span class="spin">⚡</span> IN-FLIGHT</span>`;
    } else if (status === 200) {
      statusPillHtml = `<span class="status-pill status-200">200 OK</span>`;
    } else {
      statusPillHtml = `<span class="status-pill status-err">${escapeHtml(String(status))}</span>`;
    }

    let tokenPillsHtml;
    if (isInProgress) {
      tokenPillsHtml = `
        <span class="tok-in" title="Input">In: <strong>${inTokens}</strong></span>
        <span>•</span>
        <span style="color: #d29922; font-style: italic; font-size: 11px;">⏱ Active stream in-flight...</span>
      `;
    } else {
      tokenPillsHtml = `
        <span class="tok-in" title="Prompt Tokens">In: <strong>${inTokens}</strong></span>
        <span>•</span>
        <span class="tok-out" title="Completion Tokens">Out: <strong>${outTokens}</strong></span>
        ${reasoningTokens > 0 ? `<span>•</span><span class="tok-reasoning" title="Reasoning Tokens">Reasoning: <strong>${reasoningTokens}</strong></span>` : ''}
        <span>•</span>
        <span class="tok-total" title="Total Tokens">Total: <strong>${totalTokens}</strong></span>
        ${tps ? `<span>•</span><span style="color: #39d2c0;">${tps}</span>` : ''}
        <span>•</span>
        <span style="color: #8b949e;" title="TTFB / Total RTT">⏱ ${ttfbMs} / ${totalMs}</span>
      `;
    }

    // Toggle Diff button inside card header if diffInfo exists
    let diffToggleHtml = '';
    if (diffInfo) {
      diffToggleHtml = `
        <button class="btn-toggle-diff ${isDiffActive ? '' : 'viewing-full'}" data-action="toggle-card-diff" title="Click to toggle between Diff View and Full View">
          ${isDiffActive ? '⚡ Diff View' : '📄 Full View'}
        </button>
      `;
    }

    // Card Header
    const header = document.createElement('div');
    header.className = 'payload-card-header';
    header.innerHTML = `
      <div class="card-title-left">
        <span class="card-expand-icon" style="font-size: 11px; color: #8b949e;">▼</span>
        <span class="call-seq-badge">#${seq}</span>
        <span class="method-badge">${escapeHtml(record.method || 'POST')}</span>
        ${diffToggleHtml}
        <span class="model-badge-lg">${escapeHtml(record.model || 'Unknown Model')}</span>
        ${statusPillHtml}
        <span style="font-size: 11px; color: #8b949e; font-family: var(--font-mono);">${timestamp}</span>
        <span style="font-size: 11px; color: #6e7681; font-family: var(--font-mono);">${escapeHtml(record.endpoint || '')}</span>
      </div>

      <div class="token-pills-row">
        ${tokenPillsHtml}
      </div>
    `;

    // Card Body
    const body = document.createElement('div');
    body.className = 'card-body';

    // Toggle card body on header click
    header.addEventListener('click', (e) => {
      // Don't toggle if clicking a button or link inside header
      if (e.target.tagName === 'BUTTON' || e.target.closest('button') || e.target.closest('a')) return;
      const isHidden = body.style.display === 'none';
      body.style.display = isHidden ? 'flex' : 'none';
      const icon = header.querySelector('.card-expand-icon');
      if (icon) icon.textContent = isHidden ? '▼' : '▶';
    });

    // 1. Prompts & Messages Section
    const msgSection = buildMessagesSection(record, diffInfo, isDiffActive, seq);
    if (msgSection) body.appendChild(msgSection);

    // Toggle button handler
    const diffBtn = header.querySelector('[data-action="toggle-card-diff"]');
    if (diffBtn) {
      diffBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        const currentMode = diffBtn.classList.contains('viewing-full') ? 'full' : 'diff';
        const newMode = currentMode === 'diff' ? 'full' : 'diff';
        State.cardDiffOverrides.set(seq, newMode);
        State.cardDiffOverrides.set(record.id, newMode);

        if (newMode === 'diff') {
          diffBtn.className = 'btn-toggle-diff';
          diffBtn.textContent = '⚡ Diff View';
        } else {
          diffBtn.className = 'btn-toggle-diff viewing-full';
          diffBtn.textContent = '📄 Full View';
        }

        const existingSection = body.querySelector('.messages-foldable-section');
        const newSection = buildMessagesSection(record, diffInfo, newMode === 'diff', seq);
        if (existingSection && newSection) {
          existingSection.replaceWith(newSection);
        } else if (!existingSection && newSection) {
          body.insertBefore(newSection, body.firstChild);
        }
      });
    }

    // 2. Generated Response & Reasoning Section
    const respSection = buildResponseSection(record);
    if (respSection) body.appendChild(respSection);

    // 3. Metadata & Request Headers Section
    const metaSection = buildMetadataSection(record);
    if (metaSection) body.appendChild(metaSection);

    // 4. Interactive Full Raw JSON Schema Section
    const jsonSection = buildJsonSchemaSection(record);
    if (jsonSection) body.appendChild(jsonSection);

    card.appendChild(header);
    card.appendChild(body);
    return card;
  }

  /**
   * Build Messages / Conversation History Section with Diff Support
   */
  function buildMessagesSection(record, diffInfo = null, isDiffMode = false, callSeq = 1) {
    const messages = record.request?.messages || [];
    const prompt = record.request?.prompt;
    const input = record.request?.input;

    if (!messages.length && !prompt && !input) return null;

    const container = document.createElement('div');
    container.className = 'foldable-section messages-foldable-section';

    const header = document.createElement('div');
    header.className = 'foldable-header';

    let headerLabel = `💬 Incoming Prompt & Input (${messages.length || (Array.isArray(input) ? input.length : 1)})`;
    if (isDiffMode && diffInfo) {
      if (diffInfo.type === 'messages') {
        headerLabel = `💬 Incoming Messages (${diffInfo.newMessages.length} new / ${diffInfo.totalCount} total)`;
      } else if (diffInfo.type === 'prompt') {
        headerLabel = `💬 Incoming Prompt (Diff / Delta View)`;
      }
    }

    header.innerHTML = `
      <span>${escapeHtml(headerLabel)}</span>
      <span class="fold-icon messages-fold-icon">▼</span>
    `;

    const content = document.createElement('div');
    content.className = 'foldable-content messages-foldable-content';

    if (input) {
      const inputStr = typeof input === 'string' ? input : JSON.stringify(input, null, 2);
      const isArr = Array.isArray(input);
      const inputLabel = isArr ? `Embedding Input (${input.length} items)` : 'Embedding Input';
      const pCard = document.createElement('div');
      pCard.className = 'msg-card msg-user';
      pCard.innerHTML = `
        <div class="msg-header">
          <span style="color: #58a6ff; font-weight: 700;">${escapeHtml(inputLabel)}</span>
          <button class="btn-mini btn-copy-input">Copy</button>
        </div>
        <div class="msg-text">${escapeHtml(inputStr)}</div>
      `;
      pCard.querySelector('.btn-copy-input')?.addEventListener('click', (e) => {
        e.stopPropagation();
        navigator.clipboard.writeText(inputStr);
        const btn = e.target;
        const orig = btn.textContent;
        btn.textContent = '✓ Copied';
        setTimeout(() => btn.textContent = orig, 1500);
      });
      content.appendChild(pCard);
    }

    if (prompt) {
      if (isDiffMode && diffInfo && diffInfo.type === 'prompt') {
        const banner = document.createElement('div');
        banner.className = 'diff-banner';
        banner.innerHTML = `
          <div class="diff-banner-left">
            <span style="font-size: 13px;">⚡</span>
            <span>Prompt prefix (${diffInfo.matchedCount.toLocaleString()} chars) is identical to <a class="diff-jump-link" href="#call-${diffInfo.matchedSeq}">Call #${diffInfo.matchedSeq}</a></span>
          </div>
          <div class="diff-badge-delta">+${diffInfo.newPromptSuffix.length.toLocaleString()} new chars</div>
        `;
        banner.querySelector('.diff-jump-link')?.addEventListener('click', (e) => {
          e.preventDefault();
          e.stopPropagation();
          jumpToCall(diffInfo.matchedSeq, callSeq);
        });
        content.appendChild(banner);

        const pCard = document.createElement('div');
        pCard.className = 'msg-card msg-user';
        pCard.innerHTML = `
          <div class="msg-header">
            <span style="color: #58a6ff;">New Prompt Suffix / Delta</span>
            <button class="btn-mini btn-copy-prompt">Copy Delta</button>
          </div>
          <div class="msg-text">${escapeHtml(diffInfo.newPromptSuffix)}</div>
        `;
        pCard.querySelector('.btn-copy-prompt')?.addEventListener('click', (e) => {
          e.stopPropagation();
          navigator.clipboard.writeText(diffInfo.newPromptSuffix);
          const btn = e.target;
          const orig = btn.textContent;
          btn.textContent = '✓ Copied';
          setTimeout(() => btn.textContent = orig, 1500);
        });
        content.appendChild(pCard);
      } else {
        const pCard = document.createElement('div');
        pCard.className = 'msg-card msg-user';
        pCard.innerHTML = `
          <div class="msg-header">
            <span style="color: #58a6ff;">Prompt</span>
            <button class="btn-mini btn-copy-prompt">Copy</button>
          </div>
          <div class="msg-text">${escapeHtml(String(prompt))}</div>
        `;
        pCard.querySelector('.btn-copy-prompt')?.addEventListener('click', (e) => {
          e.stopPropagation();
          navigator.clipboard.writeText(String(prompt));
          const btn = e.target;
          const orig = btn.textContent;
          btn.textContent = '✓ Copied';
          setTimeout(() => btn.textContent = orig, 1500);
        });
        content.appendChild(pCard);
      }
    }

    if (messages.length > 0) {
      let msgsToRender = messages;
      let startIndex = 0;

      if (isDiffMode && diffInfo && diffInfo.type === 'messages') {
        const banner = document.createElement('div');
        banner.className = 'diff-banner';
        const modelName = diffInfo.matchedRecord.model ? ` (${diffInfo.matchedRecord.model})` : '';
        banner.innerHTML = `
          <div class="diff-banner-left">
            <span style="font-size: 13px;">⚡</span>
            <span>Messages <strong>1–${diffInfo.matchedCount}</strong> are identical to <a class="diff-jump-link" href="#call-${diffInfo.matchedSeq}">Call #${diffInfo.matchedSeq}${escapeHtml(modelName)}</a></span>
          </div>
          <div class="diff-badge-delta">+${diffInfo.newMessages.length} new message${diffInfo.newMessages.length > 1 ? 's' : ''}</div>
        `;
        banner.querySelector('.diff-jump-link')?.addEventListener('click', (e) => {
          e.preventDefault();
          e.stopPropagation();
          jumpToCall(diffInfo.matchedSeq, callSeq);
        });
        content.appendChild(banner);

        msgsToRender = diffInfo.newMessages;
        startIndex = diffInfo.matchedCount;
      }

      msgsToRender.forEach((msg, relIdx) => {
        const idx = startIndex + relIdx;
        const role = (msg.role || 'user').toLowerCase();
        let roleClass = 'msg-user';
        let roleColor = '#58a6ff';

        const isToolMessage = (role === 'tool' || role === 'function');
        const hasToolCalls = Array.isArray(msg.tool_calls) && msg.tool_calls.length > 0;

        if (role === 'system') {
          roleClass = 'msg-system';
          roleColor = '#bc8cff';
        } else if (role === 'assistant') {
          roleClass = 'msg-assistant';
          roleColor = '#3fb950';
        } else if (isToolMessage) {
          roleClass = 'msg-tool';
          roleColor = '#d29922';
        }

        const msgCard = document.createElement('div');
        
        const contentText = msg.content !== null && msg.content !== undefined
          ? (typeof msg.content === 'string' ? msg.content : JSON.stringify(msg.content, null, 2))
          : '';

        if (isToolMessage) {
          // Tool Message Turn (collapsed by default so inspector stays concise)
          msgCard.className = `msg-card ${roleClass} msg-card-collapsible`;
          const toolName = msg.name || msg.tool_call_id || '';
          const toolLabel = toolName ? `#${idx + 1} ${role.toUpperCase()} [${escapeHtml(toolName)}]` : `#${idx + 1} ${role.toUpperCase()}`;
          const charCount = contentText.length;

          msgCard.innerHTML = `
            <div class="msg-header" style="cursor: pointer; user-select: none; margin-bottom: 0;">
              <div style="display: flex; align-items: center; gap: 8px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
                <span class="msg-fold-icon" style="font-size: 10px; color: #8b949e; flex-shrink: 0;">▶</span>
                <span style="color: ${roleColor}; font-weight: 700; flex-shrink: 0;">${toolLabel}</span>
                <span style="font-size: 11px; color: #8b949e; font-weight: normal; text-transform: none;">(${charCount.toLocaleString()} chars)</span>
              </div>
              <button class="btn-mini btn-copy-msg" style="flex-shrink: 0; margin-left: 8px;">Copy</button>
            </div>
            <div class="msg-text" style="display: none; margin-top: 8px;">${escapeHtml(contentText)}</div>
          `;

          const mHeader = msgCard.querySelector('.msg-header');
          const mText = msgCard.querySelector('.msg-text');
          const mFoldIcon = msgCard.querySelector('.msg-fold-icon');

          mHeader.addEventListener('click', (e) => {
            if (e.target.closest('.btn-copy-msg')) return;
            const isHidden = mText.style.display === 'none';
            mText.style.display = isHidden ? 'block' : 'none';
            mFoldIcon.textContent = isHidden ? '▼' : '▶';
            mHeader.style.marginBottom = isHidden ? '6px' : '0';
          });

          msgCard.querySelector('.btn-copy-msg')?.addEventListener('click', (e) => {
            e.stopPropagation();
            navigator.clipboard.writeText(contentText);
            const btn = e.target;
            const orig = btn.textContent;
            btn.textContent = '✓ Copied';
            setTimeout(() => btn.textContent = orig, 1500);
          });

          content.appendChild(msgCard);
        } else {
          // Normal message turn (user, system, assistant)
          msgCard.className = `msg-card ${roleClass}`;
          
          let headerHtml = `
            <div class="msg-header">
              <span style="color: ${roleColor}; font-weight: 700;">#${idx + 1} ${role.toUpperCase()}</span>
              <button class="btn-mini btn-copy-msg">Copy</button>
            </div>
          `;

          let bodyHtml = '';
          if (contentText) {
            bodyHtml += `<div class="msg-text">${escapeHtml(contentText)}</div>`;
          }

          msgCard.innerHTML = headerHtml + bodyHtml;

          msgCard.querySelector('.btn-copy-msg')?.addEventListener('click', (e) => {
            e.stopPropagation();
            const copyText = contentText || (hasToolCalls ? JSON.stringify(msg.tool_calls, null, 2) : '');
            navigator.clipboard.writeText(copyText);
            const btn = e.target;
            const orig = btn.textContent;
            btn.textContent = '✓ Copied';
            setTimeout(() => btn.textContent = orig, 1500);
          });

          // If message has historical tool_calls, render them as a collapsed sub-box
          if (hasToolCalls) {
            const toolJson = JSON.stringify(msg.tool_calls, null, 2);
            const toolNames = msg.tool_calls.map(t => t?.function?.name || t?.name || t?.type).filter(Boolean).join(', ');
            const toolLabel = toolNames ? `Tool Calls: ${toolNames} (${msg.tool_calls.length})` : `Tool Calls (${msg.tool_calls.length})`;

            const toolCallsBox = document.createElement('div');
            toolCallsBox.className = 'msg-card msg-tool msg-card-collapsible';
            toolCallsBox.style.marginTop = contentText ? '8px' : '0';
            toolCallsBox.innerHTML = `
              <div class="msg-header" style="cursor: pointer; user-select: none; margin-bottom: 0;">
                <div style="display: flex; align-items: center; gap: 8px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
                  <span class="msg-fold-icon" style="font-size: 10px; color: #8b949e; flex-shrink: 0;">▶</span>
                  <span style="color: #d29922; font-weight: 700; flex-shrink: 0;">🔧 ${escapeHtml(toolLabel)}</span>
                  <span style="font-size: 11px; color: #8b949e; font-weight: normal; text-transform: none;">(${toolJson.length.toLocaleString()} chars)</span>
                </div>
                <button class="btn-mini btn-copy-nested-tool" style="flex-shrink: 0; margin-left: 8px;">Copy</button>
              </div>
              <div class="msg-text" style="display: none; margin-top: 8px;">${escapeHtml(toolJson)}</div>
            `;

            const tcHeader = toolCallsBox.querySelector('.msg-header');
            const tcText = toolCallsBox.querySelector('.msg-text');
            const tcFoldIcon = toolCallsBox.querySelector('.msg-fold-icon');

            tcHeader.addEventListener('click', (e) => {
              if (e.target.closest('.btn-copy-nested-tool')) return;
              const isHidden = tcText.style.display === 'none';
              tcText.style.display = isHidden ? 'block' : 'none';
              tcFoldIcon.textContent = isHidden ? '▼' : '▶';
              tcHeader.style.marginBottom = isHidden ? '6px' : '0';
            });

            toolCallsBox.querySelector('.btn-copy-nested-tool')?.addEventListener('click', (e) => {
              e.stopPropagation();
              navigator.clipboard.writeText(toolJson);
              const btn = e.target;
              const orig = btn.textContent;
              btn.textContent = '✓ Copied';
              setTimeout(() => btn.textContent = orig, 1500);
            });

            msgCard.appendChild(toolCallsBox);
          }

          content.appendChild(msgCard);
        }
      });
    }

    header.addEventListener('click', () => {
      const hidden = content.style.display === 'none';
      content.style.display = hidden ? 'flex' : 'none';
      header.querySelector('.fold-icon').textContent = hidden ? '▼' : '▶';
    });

    container.appendChild(header);
    container.appendChild(content);
    return container;
  }

  /**
   * Build Output Response, Reasoning & Thinking Section
   */
  function buildResponseSection(record) {
    const text = record.response?.content?.text;
    const reasoning = record.response?.content?.reasoning_content;
    const toolCalls = record.response?.content?.tool_calls;
    const rawData = record.response?.raw_json?.data;
    const isEmbeddingResp = Array.isArray(rawData) && rawData.length > 0 && Boolean(rawData[0]?.embedding);
    const isErr = Boolean(record.response?.error || (record.response?.status_code && record.response.status_code >= 400));
    const error = record.response?.error || (isErr && record.response?.status_code ? `HTTP ${record.response.status_code}` : null);

    if (!text && !reasoning && !toolCalls && !error && !isEmbeddingResp) return null;

    const container = document.createElement('div');
    container.className = 'foldable-section';

    const header = document.createElement('div');
    header.className = 'foldable-header';
    const sectionTitle = isEmbeddingResp ? '✨ Generated Embeddings & Output' : '✨ Generated Response & Thinking Process';
    header.innerHTML = `
      <span>${escapeHtml(sectionTitle)}</span>
      <span class="fold-icon">▼</span>
    `;

    const content = document.createElement('div');
    content.className = 'foldable-content';

    // Error Alert if any
    if (error) {
      const errBox = document.createElement('div');
      errBox.className = 'reasoning-box';
      errBox.style.borderColor = 'rgba(248, 81, 73, 0.4)';
      errBox.style.background = 'rgba(248, 81, 73, 0.08)';
      errBox.innerHTML = `
        <div class="reasoning-header" style="color: #f85149;">
          <span>⚠️ Error Details</span>
        </div>
        <div class="reasoning-text" style="color: #f85149; font-style: normal;">${escapeHtml(error)}</div>
      `;
      content.appendChild(errBox);
    }

    // Reasoning / Thinking Box (Collapsed by default to keep the UI clean)
    if (reasoning) {
      const rBox = document.createElement('div');
      rBox.className = 'reasoning-box';
      rBox.innerHTML = `
        <div class="reasoning-header" style="cursor: pointer; user-select: none;">
          <div style="display: flex; align-items: center; gap: 8px;">
            <span class="reasoning-fold-icon" style="font-size: 10px; color: #8b949e;">▶</span>
            <span>🧠 Reasoning / Thinking Process</span>
            <span style="font-size: 11px; color: #8b949e; font-weight: normal;">(${reasoning.length.toLocaleString()} chars)</span>
          </div>
          <button class="btn-mini btn-copy-reasoning" style="margin-left: auto;">Copy Reasoning</button>
        </div>
        <div class="reasoning-text" style="display: none;">${escapeHtml(reasoning)}</div>
      `;

      const rHeader = rBox.querySelector('.reasoning-header');
      const rText = rBox.querySelector('.reasoning-text');
      const rFoldIcon = rBox.querySelector('.reasoning-fold-icon');

      rHeader.addEventListener('click', (e) => {
        if (e.target.closest('.btn-copy-reasoning')) return;
        const isHidden = rText.style.display === 'none';
        rText.style.display = isHidden ? 'block' : 'none';
        rFoldIcon.textContent = isHidden ? '▼' : '▶';
      });

      rBox.querySelector('.btn-copy-reasoning')?.addEventListener('click', (e) => {
        e.stopPropagation();
        navigator.clipboard.writeText(reasoning);
        const btn = e.target;
        const orig = btn.textContent;
        btn.textContent = '✓ Copied';
        setTimeout(() => btn.textContent = orig, 1500);
      });

      content.appendChild(rBox);
    }

    // Generated Text Output
    if (text) {
      const outCard = document.createElement('div');
      outCard.className = 'msg-card msg-assistant';
      outCard.innerHTML = `
        <div class="msg-header">
          <span style="color: #3fb950; font-weight: 700;">Assistant Output</span>
          <button class="btn-mini btn-copy-out">Copy Text</button>
        </div>
        <div class="msg-text">${escapeHtml(text)}</div>
      `;
      outCard.querySelector('.btn-copy-out')?.addEventListener('click', () => {
        navigator.clipboard.writeText(text);
      });
      content.appendChild(outCard);
    }

    // Tool Calls (Collapsed by default so inspector stays concise)
    if (toolCalls && Array.isArray(toolCalls) && toolCalls.length > 0) {
      const toolCard = document.createElement('div');
      toolCard.className = 'msg-card msg-tool msg-card-collapsible';
      const toolJson = JSON.stringify(toolCalls, null, 2);
      const toolNames = toolCalls.map(t => t?.function?.name || t?.name || t?.type).filter(Boolean).join(', ');
      const toolLabel = toolNames ? `Tool Calls: ${toolNames} (${toolCalls.length})` : `Tool / Function Calls (${toolCalls.length})`;

      toolCard.innerHTML = `
        <div class="msg-header" style="cursor: pointer; user-select: none; margin-bottom: 0;">
          <div style="display: flex; align-items: center; gap: 8px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
            <span class="msg-fold-icon" style="font-size: 10px; color: #8b949e; flex-shrink: 0;">▶</span>
            <span style="color: #d29922; font-weight: 700; flex-shrink: 0;">🔧 ${escapeHtml(toolLabel)}</span>
            <span style="font-size: 11px; color: #8b949e; font-weight: normal; text-transform: none;">(${toolJson.length.toLocaleString()} chars)</span>
          </div>
          <button class="btn-mini btn-copy-tool-resp" style="flex-shrink: 0; margin-left: 8px;">Copy</button>
        </div>
        <div class="msg-text" style="display: none; margin-top: 8px;">${escapeHtml(toolJson)}</div>
      `;

      const tHeader = toolCard.querySelector('.msg-header');
      const tText = toolCard.querySelector('.msg-text');
      const tFoldIcon = toolCard.querySelector('.msg-fold-icon');

      tHeader.addEventListener('click', (e) => {
        if (e.target.closest('.btn-copy-tool-resp')) return;
        const isHidden = tText.style.display === 'none';
        tText.style.display = isHidden ? 'block' : 'none';
        tFoldIcon.textContent = isHidden ? '▼' : '▶';
        tHeader.style.marginBottom = isHidden ? '6px' : '0';
      });

      toolCard.querySelector('.btn-copy-tool-resp')?.addEventListener('click', (e) => {
        e.stopPropagation();
        navigator.clipboard.writeText(toolJson);
        const btn = e.target;
        const orig = btn.textContent;
        btn.textContent = '✓ Copied';
        setTimeout(() => btn.textContent = orig, 1500);
      });

      content.appendChild(toolCard);
    }

    // Generated Embeddings Output (Collapsed by default so inspector stays concise)
    if (isEmbeddingResp) {
      const embCard = document.createElement('div');
      embCard.className = 'msg-card msg-assistant msg-card-collapsible';
      const vectorCount = rawData.length;
      const dim = Array.isArray(rawData[0]?.embedding) ? rawData[0].embedding.length : '?';
      const embLabel = `Generated Embeddings (${vectorCount} vector${vectorCount > 1 ? 's' : ''}, dimension: ${dim})`;
      const fullJson = JSON.stringify(rawData, null, 2);

      embCard.innerHTML = `
        <div class="msg-header" style="cursor: pointer; user-select: none; margin-bottom: 0;">
          <div style="display: flex; align-items: center; gap: 8px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
            <span class="msg-fold-icon" style="font-size: 10px; color: #8b949e; flex-shrink: 0;">▶</span>
            <span style="color: #3fb950; font-weight: 700; flex-shrink: 0;">🧬 ${escapeHtml(embLabel)}</span>
            <span style="font-size: 11px; color: #8b949e; font-weight: normal; text-transform: none;">(${fullJson.length.toLocaleString()} chars)</span>
          </div>
          <button class="btn-mini btn-copy-emb" style="flex-shrink: 0; margin-left: 8px;">Copy Vectors</button>
        </div>
        <div class="msg-text" style="display: none; margin-top: 8px;">${escapeHtml(fullJson)}</div>
      `;

      const eHeader = embCard.querySelector('.msg-header');
      const eText = embCard.querySelector('.msg-text');
      const eFoldIcon = embCard.querySelector('.msg-fold-icon');

      eHeader.addEventListener('click', (e) => {
        if (e.target.closest('.btn-copy-emb')) return;
        const isHidden = eText.style.display === 'none';
        eText.style.display = isHidden ? 'block' : 'none';
        eFoldIcon.textContent = isHidden ? '▼' : '▶';
        eHeader.style.marginBottom = isHidden ? '6px' : '0';
      });

      embCard.querySelector('.btn-copy-emb')?.addEventListener('click', (e) => {
        e.stopPropagation();
        navigator.clipboard.writeText(fullJson);
        const btn = e.target;
        const orig = btn.textContent;
        btn.textContent = '✓ Copied';
        setTimeout(() => btn.textContent = orig, 1500);
      });

      content.appendChild(embCard);
    }

    header.addEventListener('click', () => {
      const hidden = content.style.display === 'none';
      content.style.display = hidden ? 'flex' : 'none';
      header.querySelector('.fold-icon').textContent = hidden ? '▼' : '▶';
    });

    container.appendChild(header);
    container.appendChild(content);
    return container;
  }

  /**
   * Build Client Metadata, Origin & Parameters Section
   */
  function buildMetadataSection(record) {
    const container = document.createElement('div');
    container.className = 'foldable-section';

    const header = document.createElement('div');
    header.className = 'foldable-header';
    header.innerHTML = `
      <span>⚙️ Request Metadata & Orchestrator Headers</span>
      <span class="fold-icon">▶</span>
    `;

    const content = document.createElement('div');
    content.className = 'foldable-content';
    content.style.display = 'none'; // collapsed by default

    const kvGrid = document.createElement('div');
    kvGrid.className = 'kv-grid';

    // Client IP & User Agent
    const client = record.client || {};
    addKv(kvGrid, 'Client IP', client.ip || '—');
    addKv(kvGrid, 'User-Agent', client.user_agent || '—');
    addKv(kvGrid, 'Streaming Mode', record.response?.is_stream ? 'Yes (SSE)' : 'No (Batch)');

    // Model Parameters
    const params = record.request?.parameters || {};
    for (const [k, v] of Object.entries(params)) {
      addKv(kvGrid, `Param: ${k}`, typeof v === 'object' ? JSON.stringify(v) : String(v));
    }

    // Headers
    const headers = record.request?.headers || {};
    for (const [k, v] of Object.entries(headers)) {
      if (['host', 'content-length'].includes(k.toLowerCase())) continue;
      addKv(kvGrid, `Header: ${k}`, String(v));
    }

    content.appendChild(kvGrid);

    header.addEventListener('click', () => {
      const hidden = content.style.display === 'none';
      content.style.display = hidden ? 'flex' : 'none';
      header.querySelector('.fold-icon').textContent = hidden ? '▼' : '▶';
    });

    container.appendChild(header);
    container.appendChild(content);
    return container;
  }

  function addKv(grid, key, val) {
    const div = document.createElement('div');
    div.className = 'kv-item';
    div.innerHTML = `
      <span class="kv-key">${escapeHtml(key)}</span>
      <span class="kv-val">${escapeHtml(val)}</span>
    `;
    grid.appendChild(div);
  }

  /**
   * Build Interactive Foldable JSON Tree Viewer Section
   */
  function buildJsonSchemaSection(record) {
    const container = document.createElement('div');
    container.className = 'foldable-section';

    const header = document.createElement('div');
    header.className = 'foldable-header';
    header.innerHTML = `
      <span style="display: flex; align-items: center; gap: 8px;">
        <span>📋 Full Raw JSON Schema</span>
        <button class="btn-mini btn-copy-json" style="padding: 2px 6px;">Copy Full JSON</button>
      </span>
      <span class="fold-icon">▶</span>
    `;

    const content = document.createElement('div');
    content.className = 'foldable-content';
    content.style.display = 'none'; // collapsed by default

    const jsonViewer = document.createElement('div');
    jsonViewer.className = 'json-tree-container';
    jsonViewer.appendChild(renderJsonTree(record));
    content.appendChild(jsonViewer);

    // Copy JSON button
    header.querySelector('.btn-copy-json')?.addEventListener('click', (e) => {
      e.stopPropagation();
      navigator.clipboard.writeText(JSON.stringify(record, null, 2));
      const btn = e.target;
      const orig = btn.textContent;
      btn.textContent = '✓ Copied!';
      setTimeout(() => btn.textContent = orig, 2000);
    });

    header.addEventListener('click', () => {
      const hidden = content.style.display === 'none';
      content.style.display = hidden ? 'flex' : 'none';
      header.querySelector('.fold-icon').textContent = hidden ? '▼' : '▶';
    });

    container.appendChild(header);
    container.appendChild(content);
    return container;
  }

  /**
   * Recursive Interactive JSON Tree Renderer
   */
  function renderJsonTree(data, depth = 0) {
    if (data === null) {
      const span = document.createElement('span');
      span.className = 'json-null';
      span.textContent = 'null';
      return span;
    }

    if (typeof data === 'boolean') {
      const span = document.createElement('span');
      span.className = 'json-boolean';
      span.textContent = String(data);
      return span;
    }

    if (typeof data === 'number') {
      const span = document.createElement('span');
      span.className = 'json-number';
      span.textContent = String(data);
      return span;
    }

    if (typeof data === 'string') {
      const span = document.createElement('span');
      span.className = 'json-string';
      span.textContent = JSON.stringify(data);
      return span;
    }

    if (Array.isArray(data)) {
      const container = document.createElement('span');
      if (data.length === 0) {
        container.textContent = '[]';
        return container;
      }

      const toggle = document.createElement('span');
      toggle.className = 'json-toggle';
      toggle.textContent = '▼';

      const openBracket = document.createTextNode('[');
      const closeBracket = document.createTextNode(']');

      const childrenContainer = document.createElement('div');
      childrenContainer.style.paddingLeft = '18px';

      data.forEach((item, index) => {
        const itemLine = document.createElement('div');
        itemLine.appendChild(renderJsonTree(item, depth + 1));
        if (index < data.length - 1) {
          itemLine.appendChild(document.createTextNode(','));
        }
        childrenContainer.appendChild(itemLine);
      });

      const collapsedSpan = document.createElement('span');
      collapsedSpan.className = 'json-collapsed-text';
      collapsedSpan.textContent = ` Array(${data.length}) `;
      collapsedSpan.style.display = 'none';

      toggle.addEventListener('click', () => {
        const isCollapsed = childrenContainer.style.display === 'none';
        childrenContainer.style.display = isCollapsed ? 'block' : 'none';
        collapsedSpan.style.display = isCollapsed ? 'none' : 'inline';
        toggle.textContent = isCollapsed ? '▼' : '▶';
      });

      container.appendChild(toggle);
      container.appendChild(openBracket);
      container.appendChild(collapsedSpan);
      container.appendChild(childrenContainer);
      container.appendChild(closeBracket);
      return container;
    }

    if (typeof data === 'object') {
      const container = document.createElement('span');
      const keys = Object.keys(data);
      if (keys.length === 0) {
        container.textContent = '{}';
        return container;
      }

      const toggle = document.createElement('span');
      toggle.className = 'json-toggle';
      toggle.textContent = '▼';

      const openBrace = document.createTextNode('{');
      const closeBrace = document.createTextNode('}');

      const childrenContainer = document.createElement('div');
      childrenContainer.style.paddingLeft = '18px';

      keys.forEach((key, index) => {
        const itemLine = document.createElement('div');
        const keySpan = document.createElement('span');
        keySpan.className = 'json-key';
        keySpan.textContent = `"${key}": `;

        itemLine.appendChild(keySpan);
        itemLine.appendChild(renderJsonTree(data[key], depth + 1));
        if (index < keys.length - 1) {
          itemLine.appendChild(document.createTextNode(','));
        }
        childrenContainer.appendChild(itemLine);
      });

      const collapsedSpan = document.createElement('span');
      collapsedSpan.className = 'json-collapsed-text';
      collapsedSpan.textContent = ` {...} `;
      collapsedSpan.style.display = 'none';

      // Auto-collapse deep objects
      if (depth > 2) {
        childrenContainer.style.display = 'none';
        collapsedSpan.style.display = 'inline';
        toggle.textContent = '▶';
      }

      toggle.addEventListener('click', () => {
        const isCollapsed = childrenContainer.style.display === 'none';
        childrenContainer.style.display = isCollapsed ? 'block' : 'none';
        collapsedSpan.style.display = isCollapsed ? 'none' : 'inline';
        toggle.textContent = isCollapsed ? '▼' : '▶';
      });

      container.appendChild(toggle);
      container.appendChild(openBrace);
      container.appendChild(collapsedSpan);
      container.appendChild(childrenContainer);
      container.appendChild(closeBrace);
      return container;
    }

    const span = document.createElement('span');
    span.textContent = String(data);
    return span;
  }

  function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

})();
