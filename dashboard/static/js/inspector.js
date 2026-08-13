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
        if (Array.isArray(parsed)) return parsed;
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
  }

  async function initInspector() {
    await updateStatusMeta();
    await loadRecentEvents();
    connectSSE();

    // Periodic poll for status & file size
    setInterval(updateStatusMeta, 5000);
  }

  /**
   * Fetch current logging state & file size from server
   */
  async function updateStatusMeta() {
    try {
      const status = await TelemetryAPI.getRawLogStatus();
      State.isLoggingEnabled = Boolean(status.enabled);

      if (els.metaFilePath) {
        els.metaFilePath.textContent = status.rel_path || 'logger/payloads.jsonl';
      }
      if (els.metaFileSize) {
        els.metaFileSize.textContent = status.file_size_formatted || '0 B';
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
    } catch (e) {
      console.warn('Failed to update status meta', e);
    }
  }

  /**
   * Toggle raw logging state
   */
  async function handleToggleLogging() {
    try {
      const res = await TelemetryAPI.toggleRawLog();
      await updateStatusMeta();
    } catch (e) {
      alert('Error toggling logging: ' + e.message);
    }
  }

  /**
   * Load recent events from disk
   */
  async function loadRecentEvents() {
    try {
      const data = await TelemetryAPI.getRecentRawLogs(80);
      if (data && data.entries && data.entries.length > 0) {
        // data.entries is already sorted newest first
        for (const entry of data.entries.reverse()) {
          appendEvent(entry, false);
        }
        updateModelDropdown();
        renderFeed();
      }
    } catch (e) {
      console.warn('Failed to load recent logs', e);
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
   * Append a single raw payload event to the feed without destroying existing DOM nodes
   */
  function appendEvent(record, renderImmediately = true) {
    if (!record || !record.id) return;

    // Deduplicate if already present
    if (State.events.some(e => e.id === record.id)) return;

    let hasNewModel = false;
    // Track model for dropdown filter
    if (record.model) {
      if (!State.models.has(record.model)) {
        State.models.add(record.model);
        hasNewModel = true;
      }
    }

    State.events.push(record);
    if (State.events.length > State.maxDisplayed) {
      const removed = State.events.shift();
      if (removed && els.feed) {
        const oldCard = els.feed.querySelector(`[data-event-id="${removed.id}"]`);
        if (oldCard) oldCard.remove();
      }
    }

    if (hasNewModel) {
      updateModelDropdown();
    }

    if (renderImmediately && els.feed) {
      // Remove empty state placeholder if present
      const emptyStateEl = els.feed.querySelector('.empty-state');
      if (emptyStateEl) emptyStateEl.remove();

      // Create and append ONLY the new card to preserve state of existing cards
      const cardEl = createPayloadCard(record);
      cardEl.setAttribute('data-event-id', record.id);
      const isVisible = matchesFilters(record);
      if (!isVisible) {
        cardEl.style.display = 'none';
      }
      els.feed.appendChild(cardEl);

      updateDisplayedCount();

      if (State.autoScroll && isVisible) {
        window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
      }
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
      els.feed.innerHTML = `
        <div class="empty-state" id="initialEmptyState">
          <h3>Awaiting Live Telemetry Payload Logs</h3>
          <p>Ensure Raw Payload Logging is toggled <strong>ON</strong>. As requests pass through the proxy, complete incoming & outgoing JSON schemas will stream here in real-time.</p>
          <button id="emptyStateEnableBtn" class="btn-mini btn-mini-primary" style="padding: 6px 16px; font-size: 13px;">
            ⚡ Turn ON Raw Payload Logging
          </button>
        </div>
      `;
      els.feed.querySelector('#emptyStateEnableBtn')?.addEventListener('click', handleToggleLogging);
      if (els.metaTotalCalls) els.metaTotalCalls.textContent = '0';
      return;
    }

    els.feed.innerHTML = '';
    let visibleCount = 0;

    // Render events in chronological order
    for (const record of State.events) {
      const isVisible = matchesFilters(record);
      const cardEl = createPayloadCard(record);
      cardEl.setAttribute('data-event-id', record.id);
      if (!isVisible) {
        cardEl.style.display = 'none';
      } else {
        visibleCount++;
      }
      els.feed.appendChild(cardEl);
    }

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

    // Status filter
    const status = record.response?.status_code;
    if (State.filterStatus === 'success' && status !== 200) {
      return false;
    }
    if (State.filterStatus === 'errors' && (!status || status === 200)) {
      return false;
    }

    // Search query filter
    if (State.filterText) {
      const q = State.filterText;
      const modelMatch = record.model && record.model.toLowerCase().includes(q);
      const endpointMatch = record.endpoint && record.endpoint.toLowerCase().includes(q);
      const contentMatch = record.response?.content?.text && record.response.content.text.toLowerCase().includes(q);
      const promptMatch = record.request?.prompt && String(record.request.prompt).toLowerCase().includes(q);
      
      let msgsMatch = false;
      if (record.request?.messages && Array.isArray(record.request.messages)) {
        msgsMatch = record.request.messages.some(m => m.content && String(m.content).toLowerCase().includes(q));
      }

      if (!modelMatch && !endpointMatch && !contentMatch && !promptMatch && !msgsMatch) {
        return false;
      }
    }

    return true;
  }

  /**
   * Build the complete DOM element for a payload card
   */
  function createPayloadCard(record) {
    const card = document.createElement('div');
    const isError = Boolean(record.response?.error || (record.response?.status_code && record.response.status_code >= 400));
    card.className = `payload-card ${isError ? 'error-call' : 'success-call'}`;

    const usage = record.response?.usage || {};
    const inTokens = usage.prompt_tokens ?? (record.request?.payload?.messages ? '—' : 0);
    const outTokens = usage.completion_tokens ?? (isError ? 0 : '—');
    const reasoningTokens = usage.reasoning_tokens ?? 0;
    const totalTokens = usage.total_tokens ?? (typeof inTokens === 'number' && typeof outTokens === 'number' ? inTokens + outTokens : '—');

    const totalMs = record.response?.total_ms ? `${(record.response.total_ms / 1000).toFixed(2)}s` : '—';
    const ttfbMs = record.response?.ttfb_ms ? `${Math.round(record.response.ttfb_ms)}ms` : '—';
    const tps = record.response?.tokens_per_s ? `${Math.round(record.response.tokens_per_s)} tok/s` : '';

    const timestamp = record.timestamp ? new Date(record.timestamp).toLocaleTimeString() : '';
    const status = record.response?.status_code || (isError ? 'ERR' : 200);

    // Card Header
    const header = document.createElement('div');
    header.className = 'payload-card-header';
    header.innerHTML = `
      <div class="card-title-left">
        <span class="card-expand-icon" style="font-size: 11px; color: #8b949e;">▼</span>
        <span class="method-badge">${escapeHtml(record.method || 'POST')}</span>
        <span class="model-badge-lg">${escapeHtml(record.model || 'Unknown Model')}</span>
        <span class="status-pill ${status === 200 ? 'status-200' : 'status-err'}">${status}</span>
        <span style="font-size: 11px; color: #8b949e; font-family: var(--font-mono);">${timestamp}</span>
        <span style="font-size: 11px; color: #6e7681; font-family: var(--font-mono);">${escapeHtml(record.endpoint || '')}</span>
      </div>

      <div class="token-pills-row">
        <span class="tok-in" title="Prompt Tokens">In: <strong>${inTokens}</strong></span>
        <span>•</span>
        <span class="tok-out" title="Completion Tokens">Out: <strong>${outTokens}</strong></span>
        ${reasoningTokens > 0 ? `<span>•</span><span class="tok-reasoning" title="Reasoning Tokens">Reasoning: <strong>${reasoningTokens}</strong></span>` : ''}
        <span>•</span>
        <span class="tok-total" title="Total Tokens">Total: <strong>${totalTokens}</strong></span>
        ${tps ? `<span>•</span><span style="color: #39d2c0;">${tps}</span>` : ''}
        <span>•</span>
        <span style="color: #8b949e;" title="TTFB / Total RTT">⏱ ${ttfbMs} / ${totalMs}</span>
      </div>
    `;

    // Card Body
    const body = document.createElement('div');
    body.className = 'card-body';

    // Toggle card body on header click
    header.addEventListener('click', (e) => {
      // Don't toggle if clicking a button inside header
      if (e.target.tagName === 'BUTTON') return;
      const isHidden = body.style.display === 'none';
      body.style.display = isHidden ? 'flex' : 'none';
      const icon = header.querySelector('.card-expand-icon');
      if (icon) icon.textContent = isHidden ? '▼' : '▶';
    });

    // 1. Prompts & Messages Section
    const msgSection = buildMessagesSection(record);
    if (msgSection) body.appendChild(msgSection);

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
   * Build Messages / Conversation History Section
   */
  function buildMessagesSection(record) {
    const messages = record.request?.messages || [];
    const prompt = record.request?.prompt;

    if (!messages.length && !prompt) return null;

    const container = document.createElement('div');
    container.className = 'foldable-section';

    const header = document.createElement('div');
    header.className = 'foldable-header';
    header.innerHTML = `
      <span>💬 Incoming Prompt & Messages (${messages.length || 1})</span>
      <span class="fold-icon">▼</span>
    `;

    const content = document.createElement('div');
    content.className = 'foldable-content';

    if (prompt) {
      const pCard = document.createElement('div');
      pCard.className = 'msg-card msg-user';
      pCard.innerHTML = `
        <div class="msg-header">
          <span style="color: #58a6ff;">Prompt</span>
          <button class="btn-mini" onclick="navigator.clipboard.writeText(this.nextElementSibling?.textContent || '')">Copy</button>
        </div>
        <div class="msg-text">${escapeHtml(String(prompt))}</div>
      `;
      content.appendChild(pCard);
    }

    messages.forEach((msg, idx) => {
      const role = (msg.role || 'user').toLowerCase();
      let roleClass = 'msg-user';
      let roleColor = '#58a6ff';

      if (role === 'system') {
        roleClass = 'msg-system';
        roleColor = '#bc8cff';
      } else if (role === 'assistant') {
        roleClass = 'msg-assistant';
        roleColor = '#3fb950';
      } else if (role === 'tool' || role === 'function') {
        roleClass = 'msg-tool';
        roleColor = '#d29922';
      }

      const msgCard = document.createElement('div');
      msgCard.className = `msg-card ${roleClass}`;
      
      const text = typeof msg.content === 'string' ? msg.content : JSON.stringify(msg.content, null, 2);
      
      msgCard.innerHTML = `
        <div class="msg-header">
          <span style="color: ${roleColor}; font-weight: 700;">#${idx + 1} ${role.toUpperCase()}</span>
          <button class="btn-mini btn-copy-msg">Copy</button>
        </div>
        <div class="msg-text">${escapeHtml(text || '')}</div>
      `;

      msgCard.querySelector('.btn-copy-msg')?.addEventListener('click', () => {
        navigator.clipboard.writeText(text || '');
      });

      content.appendChild(msgCard);
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
   * Build Output Response, Reasoning & Thinking Section
   */
  function buildResponseSection(record) {
    const text = record.response?.content?.text;
    const reasoning = record.response?.content?.reasoning_content;
    const toolCalls = record.response?.content?.tool_calls;
    const isErr = Boolean(record.response?.error || (record.response?.status_code && record.response.status_code >= 400));
    const error = record.response?.error || (isErr && record.response?.status_code ? `HTTP ${record.response.status_code}` : null);

    if (!text && !reasoning && !toolCalls && !error) return null;

    const container = document.createElement('div');
    container.className = 'foldable-section';

    const header = document.createElement('div');
    header.className = 'foldable-header';
    header.innerHTML = `
      <span>✨ Generated Response & Thinking Process</span>
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

    // Tool Calls
    if (toolCalls && Array.isArray(toolCalls) && toolCalls.length > 0) {
      const toolCard = document.createElement('div');
      toolCard.className = 'msg-card msg-tool';
      toolCard.innerHTML = `
        <div class="msg-header">
          <span style="color: #d29922; font-weight: 700;">Tool / Function Calls (${toolCalls.length})</span>
        </div>
        <div class="msg-text">${escapeHtml(JSON.stringify(toolCalls, null, 2))}</div>
      `;
      content.appendChild(toolCard);
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
