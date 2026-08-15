/**
 * Telemetry Dashboard App Controller
 * Orchestrates API calls, UI interactions, and Chart rendering.
 */

const App = (() => {
  // Global Application State
  const State = {
    currentData: null,
    currentFilteredCalls: null,
    currentGroupBy: '',
    currentErrorsFilter: false,
    currentSort: { col: 'timestamp', dir: 'desc' },
    activeTab: 'overview',
    liveUpdatesEnabled: true,
    refreshRateSeconds: 30,
    tokenMetricType: 'input', // 'input', 'output', 'total'
    modelCosts: null,
    eInfraEnabled: true,
    liveNodesConfig: null,
    allLiveModels: [],
    allLiveModelsData: [],
    
    // Interval References
    intervals: {
      refresh: null,
      serverStatus: null,
      crossCheck: null,
      proxyStatus: null,
      proxyLogs: null
    },
    
    // Proxy Gateway State
    proxyStatus: null,
    runningProxyConfig: null,
    isProxyStatusLoading: false,
    healthData: null,
    
    // Dropdown Instance
    modelDropdownInstance: null,
    
    // Flatpickr Instance
    datePickerInstance: null,
    
    // Abort controller for network query throttling
    activeAbortController: null
  };

  // Browser IndexedDB Cache Management & UI synchronizer
  async function updateCacheStatsUI() {
    try {
      if (typeof TelemetryStore === 'undefined') return;
      const stats = await TelemetryStore.getStorageStats();
      const countEl = document.getElementById('browserCacheCountVal');
      const sizeEl = document.getElementById('browserCacheSizeVal');
      const ttlEl = document.getElementById('browserCacheTtlVal');

      if (countEl) countEl.textContent = stats.count ? stats.count.toLocaleString() : '0';
      if (sizeEl) sizeEl.textContent = `${stats.estimatedSizeMb} MB`;
      if (ttlEl) ttlEl.textContent = `Auto-evicts in ${stats.remainingDays}d`;
    } catch (e) {
      console.warn('Failed to update cache stats UI:', e);
    }
  }

  function setupCacheControl() {
    const clearBtn = document.getElementById('clearBrowserCacheBtn');
    const resyncBtn = document.getElementById('resyncBrowserCacheBtn');
    const alertEl = document.getElementById('browserCacheAlert');

    if (clearBtn) {
      clearBtn.addEventListener('click', async () => {
        try {
          clearBtn.disabled = true;
          clearBtn.textContent = 'Clearing...';
          await TelemetryStore.clearAll();
          await updateCacheStatsUI();
          if (alertEl) {
            alertEl.style.display = 'block';
            alertEl.className = 'proxy-alert success';
            alertEl.textContent = 'Browser IndexedDB cache cleared successfully.';
            setTimeout(() => { alertEl.style.display = 'none'; }, 4000);
          }
          await refresh(true);
        } catch (e) {
          if (alertEl) {
            alertEl.style.display = 'block';
            alertEl.className = 'proxy-alert error';
            alertEl.textContent = `Failed to clear cache: ${e.message}`;
          }
        } finally {
          clearBtn.disabled = false;
          clearBtn.innerHTML = '🗑 Clear Cache';
        }
      });
    }

    if (resyncBtn) {
      resyncBtn.addEventListener('click', async () => {
        try {
          resyncBtn.disabled = true;
          resyncBtn.textContent = 'Syncing...';
          await TelemetryStore.clearAll();
          if (alertEl) {
            alertEl.style.display = 'block';
            alertEl.className = 'proxy-alert success';
            alertEl.textContent = 'Re-syncing complete dataset from server...';
          }
          await refresh(true);
          await updateCacheStatsUI();
          if (alertEl) {
            alertEl.textContent = 'Data lake synchronized successfully.';
            setTimeout(() => { alertEl.style.display = 'none'; }, 4000);
          }
        } catch (e) {
          if (alertEl) {
            alertEl.style.display = 'block';
            alertEl.className = 'proxy-alert error';
            alertEl.textContent = `Sync failed: ${e.message}`;
          }
        } finally {
          resyncBtn.disabled = false;
          resyncBtn.innerHTML = '🔄 Full Re-sync';
        }
      });
    }
  }

  function getSelectedDateRange() {
    let fromVal = '';
    let toVal = '';
    if (State.datePickerInstance && State.datePickerInstance.selectedDates.length === 2) {
      fromVal = UI.toLocalISOString(State.datePickerInstance.selectedDates[0]);
      toVal = UI.toLocalISOString(State.datePickerInstance.selectedDates[1]);
    }
    return { from: fromVal, to: toVal };
  }

  function setQuickRange(range, triggerRefresh = true) {
    const now = new Date();
    let fromDateVal = null;
    if (range === '1h') fromDateVal = new Date(now.getTime() - 3600000);
    else if (range === '6h') fromDateVal = new Date(now.getTime() - 6 * 3600000);
    else if (range === '24h') fromDateVal = new Date(now.getTime() - 24 * 3600000);
    else if (range === '7d') fromDateVal = new Date(now.getTime() - 7 * 86400000);

    if (State.datePickerInstance) {
      State.datePickerInstance.setDate([fromDateVal, now], false);
    }
    
    document.querySelectorAll('[data-range]').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.range === range);
    });
    
    saveFiltersToLocalStorage();
    if (triggerRefresh) {
      refresh(true);
    }
  }

  function saveFiltersToLocalStorage() {
    const selectedModels = State.modelDropdownInstance ? State.modelDropdownInstance.getSelectedModels() : [];
    const dateRange = getSelectedDateRange();
    
    const activeRangeBtn = document.querySelector('[data-range].active');
    const quickRange = activeRangeBtn ? activeRangeBtn.dataset.range : '';

    const filters = {
      activeTab: State.activeTab,
      selectedModels: selectedModels,
      fromDate: dateRange.from,
      toDate: dateRange.to,
      quickRange: quickRange,
      errorsFilter: State.currentErrorsFilter,
      groupBy: State.currentGroupBy,
      liveUpdatesEnabled: State.liveUpdatesEnabled,
      eInfraEnabled: State.eInfraEnabled
    };
    
    localStorage.setItem('telemetry_dashboard_filters', JSON.stringify(filters));
  }

  function loadFiltersFromLocalStorage() {
    try {
      const saved = localStorage.getItem('telemetry_dashboard_filters');
      if (!saved) return null;
      return JSON.parse(saved);
    } catch (e) {
      console.error('Failed to load filters from localStorage', e);
      return null;
    }
  }

  function applyFilters(savedFilters) {
    if (!savedFilters) return;
    
    State.currentErrorsFilter = !!savedFilters.errorsFilter;
    document.querySelectorAll('[data-errors]').forEach(btn => {
      const isErrorsOnlyBtn = btn.dataset.errors === '1';
      if (isErrorsOnlyBtn === State.currentErrorsFilter) {
        btn.classList.add('active');
      } else {
        btn.classList.remove('active');
      }
    });

    State.currentGroupBy = savedFilters.groupBy || '';
    document.querySelectorAll('[data-group]').forEach(btn => {
      if (btn.dataset.group === State.currentGroupBy) {
        btn.classList.add('active');
      } else {
        btn.classList.remove('active');
      }
    });
    
    const rawTableCard = document.getElementById('rawCallsTableCard');
    const groupedTableCard = document.getElementById('groupedCallsTableCard');
    if (State.currentGroupBy) {
      if (rawTableCard) rawTableCard.style.display = 'none';
      if (groupedTableCard) groupedTableCard.style.display = 'block';
      const titleText = document.getElementById('groupedTableTitle');
      if (titleText) {
        titleText.textContent = `Grouped Summary: By ${State.currentGroupBy.charAt(0).toUpperCase() + State.currentGroupBy.slice(1)}`;
      }
    } else {
      if (rawTableCard) rawTableCard.style.display = 'block';
      if (groupedTableCard) groupedTableCard.style.display = 'none';
    }

    if (savedFilters.liveUpdatesEnabled !== undefined) {
      State.liveUpdatesEnabled = !!savedFilters.liveUpdatesEnabled;
      const liveToggle = document.getElementById('liveUpdatesToggle');
      if (liveToggle) {
        liveToggle.checked = State.liveUpdatesEnabled;
      }
      const statusText = document.getElementById('liveStatusText');
      const dot = document.getElementById('liveStatusDot');
      if (State.liveUpdatesEnabled) {
        if (statusText) statusText.textContent = 'Auto updates active';
        if (dot) dot.classList.remove('paused');
      } else {
        if (statusText) statusText.textContent = 'Auto updates paused';
        if (dot) dot.classList.add('paused');
      }
    }

    if (savedFilters.eInfraEnabled !== undefined) {
      State.eInfraEnabled = !!savedFilters.eInfraEnabled;
      const eInfraToggle = document.getElementById('eInfraToggle');
      if (eInfraToggle) {
        eInfraToggle.checked = State.eInfraEnabled;
      }
    }

    if (State.datePickerInstance) {
      if (savedFilters.quickRange) {
        const range = savedFilters.quickRange;
        const now = new Date();
        let fromDateVal = null;
        if (range === '1h') fromDateVal = new Date(now.getTime() - 3600000);
        else if (range === '6h') fromDateVal = new Date(now.getTime() - 6 * 3600000);
        else if (range === '24h') fromDateVal = new Date(now.getTime() - 24 * 3600000);
        else if (range === '7d') fromDateVal = new Date(now.getTime() - 7 * 86400000);
        
        State.datePickerInstance.setDate([fromDateVal, now], false);
        document.querySelectorAll('[data-range]').forEach(btn => {
          btn.classList.toggle('active', btn.dataset.range === range);
        });
      } else if (savedFilters.fromDate && savedFilters.toDate) {
        document.querySelectorAll('[data-range]').forEach(btn => btn.classList.remove('active'));
        State.datePickerInstance.setDate([new Date(savedFilters.fromDate), new Date(savedFilters.toDate)], false);
      }
    }

    if (savedFilters.activeTab) {
      switchTab(savedFilters.activeTab);
    } else {
      switchTab('overviewTab');
    }
  }

  function computeSummary(calls) {
    let totalCalls = 0;
    let totalInput = 0;
    let totalOutput = 0;
    let ttfbSum = 0;
    let ttfbCount = 0;
    let rttSum = 0;
    let rttCount = 0;
    let tpsOutputTokens = 0;
    let tpsTotalMs = 0;
    let errors = 0;
    let firstCall = null;
    let lastCall = null;

    calls.forEach(c => {
      const cnt = c.calls_count !== undefined && c.calls_count !== null ? c.calls_count : 1;
      totalCalls += cnt;
      totalInput += (c.input_tokens || 0);
      totalOutput += (c.output_tokens || 0);
      
      if (c.ttfb_ms !== null && c.ttfb_ms !== undefined) {
        ttfbSum += c.ttfb_ms * cnt;
        ttfbCount += cnt;
      }
      if (c.total_ms !== null && c.total_ms !== undefined) {
        rttSum += c.total_ms * cnt;
        rttCount += cnt;
      }
      if ((c.output_tokens || 0) > 0 && (c.total_ms || 0) > 0) {
        tpsOutputTokens += (c.output_tokens || 0);
        tpsTotalMs += (c.total_ms || 0) * cnt;
      }
      if (c.error || (c.status_code && (c.status_code < 200 || c.status_code >= 300))) {
        errors += cnt;
      }
      if (!firstCall || c.timestamp < firstCall) firstCall = c.timestamp;
      if (!lastCall || c.timestamp > lastCall) lastCall = c.timestamp;
    });

    return {
      calls: totalCalls,
      total_input: totalInput,
      total_output: totalOutput,
      avg_ttfb: ttfbCount ? (ttfbSum / ttfbCount) : 0,
      avg_rtt: rttCount ? (rttSum / rttCount) : 0,
      avg_tps: tpsTotalMs > 0 ? (tpsOutputTokens / (tpsTotalMs / 1000)) : 0,
      errors: errors,
      first_call: firstCall,
      last_call: lastCall
    };
  }

  function aggregateCalls(calls, groupBy) {
    if (!groupBy) return [];

    const groups = {};
    calls.forEach(c => {
      let key = '';
      if (groupBy === 'model') {
        key = c.model || 'System / Non-Inference';
      } else if (groupBy === 'hour') {
        const d = new Date(c.timestamp);
        const yr = d.getFullYear();
        const mo = String(d.getMonth() + 1).padStart(2, '0');
        const dy = String(d.getDate()).padStart(2, '0');
        const hr = String(d.getHours()).padStart(2, '0');
        key = `${yr}-${mo}-${dy}T${hr}:00:00`;
      } else if (groupBy === 'day') {
        const d = new Date(c.timestamp);
        const yr = d.getFullYear();
        const mo = String(d.getMonth() + 1).padStart(2, '0');
        const dy = String(d.getDate()).padStart(2, '0');
        key = `${yr}-${mo}-${dy}`;
      } else if (groupBy === 'call_type') {
        key = c.call_type || 'chat';
      }

      if (!groups[key]) {
        groups[key] = {
          key: key,
          calls: 0,
          total_input: 0,
          total_output: 0,
          ttfbSum: 0,
          ttfbCount: 0,
          ttfbMax: 0,
          rttSum: 0,
          rttCount: 0,
          rttMax: 0,
          tpsOutputTokens: 0,
          tpsTotalMs: 0,
          loadSum: 0,
          loadCount: 0,
          errors: 0
        };
      }

      const g = groups[key];
      const cnt = c.calls_count !== undefined && c.calls_count !== null ? c.calls_count : 1;
      g.calls += cnt;
      g.total_input += (c.input_tokens || 0);
      g.total_output += (c.output_tokens || 0);

      if (c.ttfb_ms !== null && c.ttfb_ms !== undefined) {
        g.ttfbSum += c.ttfb_ms * cnt;
        g.ttfbCount += cnt;
        if (c.ttfb_ms > g.ttfbMax) g.ttfbMax = c.ttfb_ms;
      }
      if (c.total_ms !== null && c.total_ms !== undefined) {
        g.rttSum += c.total_ms * cnt;
        g.rttCount += cnt;
        if (c.total_ms > g.rttMax) g.rttMax = c.total_ms;
      }
      if ((c.output_tokens || 0) > 0 && (c.total_ms || 0) > 0) {
        g.tpsOutputTokens += (c.output_tokens || 0);
        g.tpsTotalMs += (c.total_ms || 0) * cnt;
      }
      if (c.server_running !== null && c.server_running !== undefined) {
        g.loadSum += c.server_running * cnt;
        g.loadCount += cnt;
      }
      if (c.error || (c.status_code && (c.status_code < 200 || c.status_code >= 300))) {
        g.errors += cnt;
      }
    });

    const resultList = Object.values(groups).map(g => {
      const res = {
        calls: g.calls,
        total_input: g.total_input,
        total_output: g.total_output,
        avg_ttfb: g.ttfbCount ? (g.ttfbSum / g.ttfbCount) : 0,
        max_ttfb: g.ttfbMax,
        avg_rtt: g.rttCount ? (g.rttSum / g.rttCount) : 0,
        max_rtt: g.rttMax,
        avg_tps: g.tpsTotalMs > 0 ? (g.tpsOutputTokens / (g.tpsTotalMs / 1000)) : 0,
        avg_load: g.loadCount ? (g.loadSum / g.loadCount) : 0,
        errors: g.errors
      };

      if (groupBy === 'model') res.model = g.key;
      else if (groupBy === 'hour') res.hour = g.key;
      else if (groupBy === 'day') res.day = g.key;
      else if (groupBy === 'call_type') res.call_type = g.key;

      return res;
    });

    if (groupBy === 'model') {
      resultList.sort((a, b) => (b.total_input || 0) - (a.total_input || 0));
    }

    return resultList;
  }

  function renderTelemetry(data) {
    if (!data) return;

    const selectedModels = State.modelDropdownInstance ? State.modelDropdownInstance.getSelectedModels() : [];
    
    let filteredCalls = data.calls || [];
    if (selectedModels.length > 0) {
      filteredCalls = filteredCalls.filter(c => c.model && selectedModels.includes(c.model));
    }
    if (State.currentErrorsFilter) {
      filteredCalls = filteredCalls.filter(c => Boolean(c.error || (c.status_code && (c.status_code < 200 || c.status_code >= 300))));
    }

    const summary = computeSummary(filteredCalls);
    UI.renderSummary(summary);

    let groups = [];
    if (State.currentGroupBy) {
      groups = aggregateCalls(filteredCalls, State.currentGroupBy);
      UI.renderGroupedTable(groups, State.currentGroupBy);
    } else {
      UI.renderCallsTable(filteredCalls, State.currentSort.col, State.currentSort.dir);
    }

    enrichCallsWithCosts(filteredCalls);
    State.currentFilteredCalls = filteredCalls;

    const dateRange = getSelectedDateRange();
    const minTime = dateRange.from ? new Date(dateRange.from).getTime() : null;
    const maxTime = dateRange.to ? new Date(dateRange.to).getTime() : new Date().getTime();
    const timeRange = { minTime, maxTime };

    TelemetryCharts.renderAll(filteredCalls, State.tokenMetricType, timeRange);

    UI.renderCostSummary(filteredCalls, State.modelCosts);
    UI.renderCostTables(filteredCalls, State.modelCosts, State.currentGroupBy);
    TelemetryCharts.renderCostCharts(filteredCalls, State.modelCosts, timeRange);

    const hourData = UI.renderPerformanceAnalyzer(filteredCalls);
    if (hourData) {
      TelemetryCharts.renderAnalyzerCharts(hourData, filteredCalls, timeRange);
    }

    const modelStatsData = {
      groups: aggregateCalls(filteredCalls, 'model'),
      available_models: data.available_models || []
    };
    UI.renderModelTable(modelStatsData, State.currentGroupBy, filteredCalls);

    UI.renderCrossCheck(data);

    const statusText = document.getElementById('liveStatusText');
    if (statusText && State.liveUpdatesEnabled) {
      statusText.textContent = `Last updated: ${new Date().toLocaleTimeString([], { hour12: false })}`;
    }
  }

  function showLoadingOverlays() {
    const spinner = `<div class="loading-overlay" style="display: flex;"><div class="spinner"></div><span>Retrieving data...</span></div>`;
    
    const summaryBar = document.getElementById('summaryBar');
    if (summaryBar) summaryBar.innerHTML = spinner;
    
    const costSummaryBar = document.getElementById('costSummaryBar');
    if (costSummaryBar) costSummaryBar.innerHTML = spinner;
    
    const duelContainer = document.getElementById('modelDuelContainer');
    if (duelContainer) duelContainer.innerHTML = spinner;

    const crossCheck = document.getElementById('crossCheckSection');
    if (crossCheck) crossCheck.innerHTML = spinner;

    const callsBody = document.querySelector('#callsTable tbody');
    if (callsBody) callsBody.innerHTML = '<tr><td colspan="11" class="loading" style="text-align:center;">Retrieving telemetry data...</td></tr>';
  }

  function showErrorState(err) {
    const errorMsg = `<div class="tag tag-error" style="padding: 12px; margin: 10px; width: 100%; text-align: center;">Sync Error: ${err.message || err}</div>`;
    
    const summaryBar = document.getElementById('summaryBar');
    if (summaryBar) summaryBar.innerHTML = errorMsg;

    const statusText = document.getElementById('liveStatusText');
    if (statusText) statusText.textContent = 'Sync Error';
  }

  /**
   * Initialize Dashboard
   */
  async function init() {
    setupTabNavigation();
    setupProxyControl();
    setupFilters();
    setupToggleSwitches();
    setupCacheControl();

    try {
      if (typeof TelemetryStore !== 'undefined') {
        await TelemetryStore.init();
        updateCacheStatsUI();
      }
    } catch (e) {
      console.warn('TelemetryStore initialization failed, falling back:', e);
    }
    
    try {
      const savedNodes = localStorage.getItem('telemetry_dashboard_live_nodes_config');
      if (savedNodes) {
        State.liveNodesConfig = JSON.parse(savedNodes);
      }
    } catch (e) {
      console.warn('Failed to load live nodes config', e);
    }
    
    const savedFilters = loadFiltersFromLocalStorage();
    
    if (savedFilters) {
      applyFilters(savedFilters);
    } else {
      setQuickRange('24h', false);
      switchTab('overviewTab');
    }

    const savedSelectedModels = savedFilters ? (savedFilters.selectedModels || []) : null;

    await loadAvailableModels(savedSelectedModels);
    await performInitialLoad();
    startIntervals();
  }

  /**
   * Set Up Tabs Switcher (with support for programmatic switching)
   */
  function switchTab(tabId) {
    // Map legacy tab IDs to controlPanelTab if needed
    if (tabId === 'proxyTab' || tabId === 'diagnosticsTab' || tabId === 'crossCheckTab') {
      tabId = 'controlPanelTab';
    }

    const previousTab = State.activeTab;
    // If moving away from controlPanelTab without restarting, discard unapplied edits
    if (previousTab === 'controlPanelTab' && tabId !== 'controlPanelTab') {
      resetProxyConfigInputs();
    }

    const btn = document.querySelector(`.tab-btn[data-tab="${tabId}"]`);
    if (!btn) return;

    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));

    btn.classList.add('active');
    const contentEl = document.getElementById(tabId);
    if (contentEl) contentEl.classList.add('active');

    State.activeTab = tabId;
    saveFiltersToLocalStorage();

    // Only show telemetry filter controls and summary KPI cards on telemetry tabs
    const isTelemetryTab = ['overviewTab', 'tablesTab', 'analyzerTab', 'costsTab'].includes(tabId);
    const controlsCard = document.querySelector('.controls-card');
    const summaryBar = document.getElementById('summaryBar');

    if (controlsCard) {
      controlsCard.style.display = isTelemetryTab ? '' : 'none';
    }
    if (summaryBar) {
      summaryBar.style.display = isTelemetryTab ? '' : 'none';
    }

    if (tabId === 'controlPanelTab') {
      loadHealth();
      loadProxyStatus();
      loadProxyLogs();
      loadRawLogStatus();
      loadCrossCheck();
    }
  }

  function setupTabNavigation() {
    document.querySelectorAll('.tab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const tabId = btn.dataset.tab;
        if (tabId) switchTab(tabId);
      });
    });

    // Mobile Navigation Sidebar Toggle
    const hamburger = document.getElementById('hamburgerMenuBtn');
    const navTabs = document.querySelector('.nav-tabs');
    const backdrop = document.getElementById('sidebarBackdrop');
    
    if (hamburger && navTabs && backdrop) {
      const toggleSidebar = () => {
        const isOpen = navTabs.classList.toggle('open');
        hamburger.classList.toggle('open', isOpen);
        backdrop.classList.toggle('open', isOpen);
      };
      
      const closeSidebar = () => {
        navTabs.classList.remove('open');
        hamburger.classList.remove('open');
        backdrop.classList.remove('open');
      };
      
      hamburger.addEventListener('click', toggleSidebar);
      backdrop.addEventListener('click', closeSidebar);
      
      // Close sidebar when any tab button is clicked
      document.querySelectorAll('.nav-tabs .tab-btn').forEach(btn => {
        btn.addEventListener('click', closeSidebar);
      });
    }
  }

  /**
   * Initial models fetch to populate checkbox filter
   */
  async function loadAvailableModels(savedSelectedModels = []) {
    try {
      const data = await TelemetryAPI.query({ limit: 1 });
      const availableModels = data.available_models || [];
      State.modelDropdownInstance = UI.setupCustomDropdown(availableModels, () => {
        saveFiltersToLocalStorage();
        renderTelemetry(State.currentData);
      }, savedSelectedModels);
    } catch (e) {
      console.error('Failed to load initial models filter', e);
    }
  }

  /**
   * Normalizes model costs config into a lookup structure with pre-sorted, epoch-parsed tiers.
   */
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
        // Legacy single-tier object support
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

  /**
   * Resolves the active model cost tier for a given model and call timestamp based on intervals.
   */
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

  /**
   * Performs the initial full loading pipeline
   */
  async function loadCosts() {
    try {
      State.modelCosts = await TelemetryAPI.getCosts();
      State.normalizedModelCosts = normalizeModelCostsConfig(State.modelCosts);
    } catch (e) {
      console.error('Failed to load model costs config', e);
    }
  }

  function enrichCallsWithCosts(calls) {
    if (!calls || !State.modelCosts) return;
    if (!State.normalizedModelCosts) {
      State.normalizedModelCosts = normalizeModelCostsConfig(State.modelCosts);
    }

    calls.forEach(c => {
      const costConfig = getModelCostForCall(State.normalizedModelCosts, c.model, c.timestamp);
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

  async function performInitialLoad() {
    await loadCosts();
    await refresh();
    await loadServerStatus();
    await loadCrossCheck();
    await loadHealth();
    await loadProxyStatus();
    await loadRawLogStatus();
  }

  /**
   * Setup filter listeners (Group By, Date, Errors, Token Metrics)
   */
  function setupFilters() {
    // Initialize Flatpickr range picker
    State.datePickerInstance = flatpickr("#dateRangePicker", {
      mode: "range",
      enableTime: true,
      dateFormat: "Y-m-d H:i",
      time_24hr: true,
      onChange: function(selectedDates, dateStr, instance) {
        if (selectedDates.length === 2) {
          // Clear active class from quick range buttons since dates were custom-selected
          document.querySelectorAll('[data-range]').forEach(b => b.classList.remove('active'));
          saveFiltersToLocalStorage();
          refresh(true);
        }
      }
    });

    document.querySelectorAll('[data-range]').forEach(btn => {
      btn.addEventListener('click', () => {
        const range = btn.dataset.range;
        setQuickRange(range);
      });
    });

    document.querySelectorAll('[data-group]').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('[data-group]').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        
        State.currentGroupBy = btn.dataset.group;
        saveFiltersToLocalStorage();

        const rawTableCard = document.getElementById('rawCallsTableCard');
        const groupedTableCard = document.getElementById('groupedCallsTableCard');
        
        if (State.currentGroupBy) {
          if (rawTableCard) rawTableCard.style.display = 'none';
          if (groupedTableCard) groupedTableCard.style.display = 'block';
          const titleText = document.getElementById('groupedTableTitle');
          if (titleText) {
            titleText.textContent = `Grouped Summary: By ${State.currentGroupBy.charAt(0).toUpperCase() + State.currentGroupBy.slice(1)}`;
          }
          if (State.activeTab === 'overviewTab') {
            switchTab('tablesTab');
          }
        } else {
          if (rawTableCard) rawTableCard.style.display = 'block';
          if (groupedTableCard) groupedTableCard.style.display = 'none';
          if (State.activeTab === 'tablesTab') {
            switchTab('overviewTab');
          }
        }

        renderTelemetry(State.currentData);
      });
    });

    document.querySelectorAll('[data-errors]').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('[data-errors]').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        
        State.currentErrorsFilter = btn.dataset.errors === '1';
        saveFiltersToLocalStorage();
        renderTelemetry(State.currentData);
      });
    });

    document.querySelectorAll('[data-token-metric]').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('[data-token-metric]').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        
        State.tokenMetricType = btn.dataset.tokenMetric;
        saveFiltersToLocalStorage();
        if (State.currentFilteredCalls) {
          const dateRange = getSelectedDateRange();
          const minTime = dateRange.from ? new Date(dateRange.from).getTime() : null;
          const maxTime = dateRange.to ? new Date(dateRange.to).getTime() : new Date().getTime();
          const timeRange = { minTime, maxTime };
          TelemetryCharts.renderTokenChart(State.currentFilteredCalls, State.tokenMetricType, timeRange);
        }
      });
    });

    document.querySelectorAll('#callsTable th[data-sort]').forEach(th => {
      th.addEventListener('click', () => {
        const col = th.dataset.sort;
        if (State.currentSort.col === col) {
          State.currentSort.dir = State.currentSort.dir === 'asc' ? 'desc' : 'asc';
        } else {
          State.currentSort.col = col;
          State.currentSort.dir = 'desc';
        }
        
        document.querySelectorAll('#callsTable th[data-sort]').forEach(header => {
          let text = header.textContent.replace(/[▲▼]/g, '').trim();
          if (header.dataset.sort === State.currentSort.col) {
            text += State.currentSort.dir === 'asc' ? ' ▲' : ' ▼';
          }
          header.textContent = text;
        });

        if (State.currentFilteredCalls) {
          UI.renderCallsTable(State.currentFilteredCalls, State.currentSort.col, State.currentSort.dir);
        }
      });
    });

    const refreshBtn = document.getElementById('manualRefreshBtn');
    if (refreshBtn) {
      refreshBtn.addEventListener('click', () => {
        performInitialLoad();
      });
    }
  }

  /**
   * Set Up Toggle Switches (Live Updates, Refresh Interval rate)
   */
  function setupToggleSwitches() {
    const liveToggle = document.getElementById('liveUpdatesToggle');
    if (liveToggle) {
      liveToggle.addEventListener('change', (e) => {
        State.liveUpdatesEnabled = e.target.checked;
        saveFiltersToLocalStorage();
        const statusText = document.getElementById('liveStatusText');
        const dot = document.getElementById('liveStatusDot');
        
        if (State.liveUpdatesEnabled) {
          if (statusText) statusText.textContent = 'Auto updates active';
          if (dot) dot.classList.remove('paused');
          startIntervals();
          refresh(true);
        } else {
          if (statusText) statusText.textContent = 'Auto updates paused';
          if (dot) dot.classList.add('paused');
          stopIntervals();
        }
      });
    }

    const eInfraToggle = document.getElementById('eInfraToggle');
    if (eInfraToggle) {
      eInfraToggle.addEventListener('change', (e) => {
        State.eInfraEnabled = e.target.checked;
        saveFiltersToLocalStorage();
        loadServerStatus();
        startIntervals();
      });
    }

    const openEditLiveNodesBtn = document.getElementById('openEditLiveNodesBtn');
    const modal = document.getElementById('liveNodesModal');
    const closeBtn = document.getElementById('closeLiveNodesModalBtn');

    if (openEditLiveNodesBtn && modal) {
      openEditLiveNodesBtn.addEventListener('click', () => {
        ensureLiveNodesConfig(State.allLiveModels);
        UI.renderLiveNodesModal(State.allLiveModels, State.liveNodesConfig, (newConfig) => {
          State.liveNodesConfig = newConfig;
          localStorage.setItem('telemetry_dashboard_live_nodes_config', JSON.stringify(newConfig));
          loadServerStatus();
          modal.classList.remove('open');
        });
        modal.classList.add('open');
      });
    }

    if (closeBtn && modal) {
      closeBtn.addEventListener('click', () => {
        modal.classList.remove('open');
      });
    }

    if (modal) {
      window.addEventListener('click', (e) => {
        if (e.target === modal) {
          modal.classList.remove('open');
        }
      });
    }
  }

  /**
   * Gather inputs and query API
   */
  async function refresh(forceFetch = false) {
    // If a quick range button is selected, update date range dynamically based on current time
    const activeRangeBtn = document.querySelector('[data-range].active');
    if (activeRangeBtn) {
      const range = activeRangeBtn.dataset.range;
      const now = new Date();
      let fromDateVal = null;
      if (range === '1h') fromDateVal = new Date(now.getTime() - 3600000);
      else if (range === '6h') fromDateVal = new Date(now.getTime() - 6 * 3600000);
      else if (range === '24h') fromDateVal = new Date(now.getTime() - 24 * 3600000);
      else if (range === '7d') fromDateVal = new Date(now.getTime() - 7 * 86400000);

      if (State.datePickerInstance && fromDateVal) {
        State.datePickerInstance.setDate([fromDateVal, now], false);
      }
    }

    const dateRange = getSelectedDateRange();
    const fromVal = dateRange.from ? new Date(dateRange.from).toISOString() : '';
    const toVal = dateRange.to ? new Date(dateRange.to).toISOString() : '';

    // Automatically refresh live model status nodes from infra in sync with telemetry
    loadServerStatus();

    // 1. Instantaneous Local Rendering from IndexedDB Data Lake
    let cachedCalls = [];
    if (typeof TelemetryStore !== 'undefined') {
      try {
        cachedCalls = await TelemetryStore.getRange(fromVal, toVal);
      } catch (e) {
        console.warn('Failed to query local IndexedDB cache range:', e);
      }
    }

    if (cachedCalls.length > 0 && !forceFetch) {
      const initialPayload = {
        calls: cachedCalls,
        available_models: State.allAvailableModels || [],
        available_types: State.allAvailableTypes || []
      };
      State.currentData = initialPayload;
      renderTelemetry(initialPayload);
      // Run delta sync in background
      fetchTelemetryDelta(fromVal, toVal, true, false);
    } else {
      fetchTelemetryDelta(fromVal, toVal, false, forceFetch);
    }
  }

  /**
   * Delta-Sync Engine: Resilient fetcher with fallback and offline-first guarantees.
   */
  async function fetchTelemetryDelta(fromVal, toVal, isBackground = false, forceFetch = false) {
    if (State.activeAbortController) {
      State.activeAbortController.abort();
    }
    
    const controller = new AbortController();
    State.activeAbortController = controller;
    const signal = controller.signal;

    if (!isBackground) {
      showLoadingOverlays();
    }

    try {
      if (typeof TelemetryStore === 'undefined') {
        // Direct query if IndexedDB is unavailable
        const data = await TelemetryAPI.query({ from: fromVal, to: toVal, limit: 50000 }, { signal });
        State.currentData = data;
        renderTelemetry(data);
        return;
      }

      const watermarks = await TelemetryStore.getWatermarks();

      if (forceFetch || watermarks.count === 0) {
        // Cold fetch for requested range
        const filters = {
          from: fromVal,
          to: toVal,
          limit: 500000
        };
        try {
          const data = await TelemetryAPI.queryBulk(filters, { signal });
          if (data.calls && data.calls.length > 0) {
            await TelemetryStore.putBatch(data.calls);
          }
          if (data.db_fingerprint) {
            await TelemetryStore.setMeta('db_fingerprint', data.db_fingerprint);
          }
          if (data.available_models && data.available_models.length > 0) {
            State.allAvailableModels = data.available_models;
          }
          if (data.available_types && data.available_types.length > 0) {
            State.allAvailableTypes = data.available_types;
          }
        } catch (bulkErr) {
          console.warn('[TelemetryStore] Bulk query failed, trying standard query fallback:', bulkErr);
          // Fallback to standard query with smaller limits
          const fallbackData = await TelemetryAPI.query({ from: fromVal, to: toVal, limit: 10000 }, { signal });
          if (fallbackData.calls && fallbackData.calls.length > 0) {
            await TelemetryStore.putBatch(fallbackData.calls);
          }
        }
      } else {
        // Smart Delta Sync:
        // A. Fetch missing older historical window if fromVal is earlier than cached earliestTs
        if (fromVal && (!watermarks.earliestTs || fromVal < watermarks.earliestTs)) {
          const histFilters = {
            from: fromVal,
            to: watermarks.earliestTs,
            limit: 500000
          };
          try {
            const histData = await TelemetryAPI.queryBulk(histFilters, { signal });
            if (histData.calls && histData.calls.length > 0) {
              await TelemetryStore.putBatch(histData.calls);
            }
          } catch (e) {
            console.warn('[TelemetryStore] Historical gap sync deferred:', e);
          }
        }

        // B. Fetch live tail (new calls recorded since maxId)
        if (watermarks.maxId > 0) {
          const tailFilters = {
            since_id: watermarks.maxId,
            limit: 500000
          };
          try {
            const tailData = await TelemetryAPI.queryBulk(tailFilters, { signal });
            if (tailData.calls && tailData.calls.length > 0) {
              await TelemetryStore.putBatch(tailData.calls);
            }
            if (tailData.available_models && tailData.available_models.length > 0) {
              State.allAvailableModels = tailData.available_models;
            }
            if (tailData.available_types && tailData.available_types.length > 0) {
              State.allAvailableTypes = tailData.available_types;
            }
          } catch (e) {
            console.warn('[TelemetryStore] Live tail sync deferred:', e);
          }
        }
      }

      // Query the complete updated range from IndexedDB
      const finalCalls = await TelemetryStore.getRange(fromVal, toVal);

      const finalDataResponse = {
        calls: finalCalls,
        available_models: State.allAvailableModels || [],
        available_types: State.allAvailableTypes || []
      };

      State.currentData = finalDataResponse;
      renderTelemetry(finalDataResponse);
      updateCacheStatsUI();
    } catch (e) {
      if (e.name === 'AbortError') {
        console.log('Query delta fetch aborted.');
        return;
      }
      console.error('Delta sync failed:', e);

      // Attempt recovery from existing cached records before showing error state
      if (typeof TelemetryStore !== 'undefined') {
        try {
          const fallbackCached = await TelemetryStore.getRange(fromVal, toVal);
          if (fallbackCached && fallbackCached.length > 0) {
            const recoveryPayload = {
              calls: fallbackCached,
              available_models: State.allAvailableModels || [],
              available_types: State.allAvailableTypes || []
            };
            State.currentData = recoveryPayload;
            renderTelemetry(recoveryPayload);
            updateCacheStatsUI();
            return;
          }
        } catch (dbErr) {
          console.warn('Fallback cache read failed:', dbErr);
        }
      }

      if (!isBackground) {
        showErrorState(e);
      }
    } finally {
      if (State.activeAbortController === controller) {
        State.activeAbortController = null;
      }
    }
  }

  /**
   * Live Server Status Node fetcher
   */
  async function loadServerStatus() {
    if (!State.eInfraEnabled) {
      const el = document.getElementById('serverStatus');
      if (el) {
        el.innerHTML = `
          <div class="loading-overlay" style="position: static; padding: 20px; text-align: center; color: var(--text-dim); background: transparent; display: flex;">
            <span>e-INFRA live sync disabled. Toggle 'Sync' on to monitor nodes.</span>
          </div>
        `;
      }
      return;
    }
    
    try {
      const data = await TelemetryAPI.getServerStatus();
      State.allLiveModelsData = data.models || [];
      State.allLiveModels = (data.models || []).map(m => m.name);
      
      ensureLiveNodesConfig(State.allLiveModels);
      
      UI.renderServerStatus(data, State.liveNodesConfig);
    } catch (e) {
      console.error('Failed to load server status node data', e);
      const el = document.getElementById('serverStatus');
      if (el) el.innerHTML = `<div class="tag tag-error" style="padding:10px;">Failed to query server status node: ${e.message}</div>`;
    }
  }

  /**
   * Proxy cross-check database comparisons fetcher
   */
  async function loadCrossCheck() {
    try {
      const dateRange = getSelectedDateRange();
      const fromVal = dateRange.from ? new Date(dateRange.from).toISOString() : '';
      const toVal = dateRange.to ? new Date(dateRange.to).toISOString() : '';
      const selectedModels = State.modelDropdownInstance ? State.modelDropdownInstance.getSelectedModels() : [];
      
      const filters = {
        from: fromVal,
        to: toVal,
        models: selectedModels,
        errors_only: State.currentErrorsFilter,
        limit: 1
      };
      
      const data = await TelemetryAPI.query(filters);
      UI.renderCrossCheck(data);
    } catch (e) {
      console.error('Failed to load cross check statistics', e);
    }
  }

  /**
   * Database details and status
   */
  async function loadHealth() {
    try {
      const data = await TelemetryAPI.getHealth();
      State.healthData = data;
      if (UI.renderHealth) UI.renderHealth(data);
      
      const dbPathEl = document.getElementById('proxyDbPathVal');
      const dbSizeEl = document.getElementById('proxyDbSizeVal');
      if (dbPathEl && data.db_path) dbPathEl.textContent = data.db_path;
      if (dbSizeEl && data.db_size_mb !== undefined) {
        dbSizeEl.textContent = data.db_exists ? `${data.db_size_mb} MB` : 'Not Found';
      }

      // Check DB fingerprint to detect server DB compressions or resets
      if (data.db_fingerprint && typeof TelemetryStore !== 'undefined') {
        const storedFp = await TelemetryStore.getMeta('db_fingerprint');
        if (storedFp && storedFp !== data.db_fingerprint) {
          console.log('[TelemetryStore] Server database fingerprint changed. Invalidating stale browser cache.');
          await TelemetryStore.clearAll();
          await TelemetryStore.setMeta('db_fingerprint', data.db_fingerprint);
          await refresh(true);
        } else if (!storedFp) {
          await TelemetryStore.setMeta('db_fingerprint', data.db_fingerprint);
        }
      }
      updateCacheStatsUI();
    } catch (e) {
      console.error('Database health check failed', e);
    }
  }

  // Upstream Target LocalStorage Cache (stores only what the user has actually used)
  const UPSTREAM_HISTORY_KEY = 'llm_proxy_user_upstream_history';

  function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function normalizeUpstreamUrl(url) {
    if (!url || typeof url !== 'string') return '';
    return url.trim().replace(/\/+$/, '');
  }

  function getUpstreamHistory() {
    try {
      const stored = localStorage.getItem(UPSTREAM_HISTORY_KEY);
      if (stored) {
        const parsed = JSON.parse(stored);
        if (Array.isArray(parsed)) {
          const unique = [];
          parsed.forEach(item => {
            const norm = normalizeUpstreamUrl(item);
            if (norm && !unique.includes(norm)) {
              unique.push(norm);
            }
          });
          return unique;
        }
      }
    } catch (e) {
      console.warn('Failed to parse upstream history from localStorage', e);
    }
    return [];
  }

  function saveUpstreamToHistory(url) {
    const normalized = normalizeUpstreamUrl(url);
    if (!normalized) return;
    try {
      const current = getUpstreamHistory();
      // If already at the top of the history list, avoid redundant write and render
      if (current.length > 0 && current[0] === normalized) {
        return;
      }
      const filtered = current.filter(item => item !== normalized);
      filtered.unshift(normalized);
      const limited = filtered.slice(0, 30);
      localStorage.setItem(UPSTREAM_HISTORY_KEY, JSON.stringify(limited));
      renderUpstreamHistoryOptions();
    } catch (e) {
      console.warn('Failed to save upstream history to localStorage', e);
    }
  }

  function renderUpstreamHistoryOptions() {
    const list = getUpstreamHistory();
    const datalist = document.getElementById('proxyUpstreamDatalist');
    const select = document.getElementById('proxyUpstreamQuickSelect');
    const countEl = document.getElementById('proxyUpstreamHistoryCount');
    
    if (datalist) {
      datalist.innerHTML = list.map(u => `<option value="${escapeHtml(u)}"></option>`).join('');
    }
    
    if (select) {
      if (list.length === 0) {
        select.innerHTML = '<option value="" disabled selected>▼ No History</option>';
      } else {
        select.innerHTML = `<option value="" disabled selected>▼ Previous Targets (${list.length})</option>` +
          list.map(u => `<option value="${escapeHtml(u)}">${escapeHtml(u)}</option>`).join('');
      }
    }

    if (countEl) {
      countEl.textContent = list.length === 1 ? '1 previous target' : `${list.length} previous targets`;
    }
  }

  function formatTokenLimitToMillion(tokenLimit) {
    if (tokenLimit === null || tokenLimit === undefined || isNaN(tokenLimit)) return '480';
    const num = Number(tokenLimit);
    if (num <= 0) return '0';
    // If it's a raw token count (e.g. 480000000), convert to millions (480)
    const valInM = num >= 1000 ? num / 1_000_000 : num;
    return Number(valInM.toFixed(4)).toString();
  }

  function parseTokenLimitFromInput(val) {
    if (val === null || val === undefined || String(val).trim() === '') return 480000000;
    const num = parseFloat(String(val).trim().replace(/,/g, ''));
    if (isNaN(num) || num < 0) return 480000000;
    // Input is in Millions (e.g. 480 -> 480,000,000)
    return Math.round(num * 1_000_000);
  }

  function isProxyConfigDirty() {
    if (!State.runningProxyConfig) return false;
    const portInput = document.getElementById('proxyConfigPort');
    const hostInput = document.getElementById('proxyConfigHost');
    const tokenLimitInput = document.getElementById('proxyConfigTokenLimit');
    const upstreamInput = document.getElementById('proxyConfigUpstream');
    
    const currPort = portInput ? parseInt(portInput.value, 10) : 9090;
    const currHost = hostInput ? hostInput.value.trim() : '0.0.0.0';
    const currTokenLimit = tokenLimitInput ? parseTokenLimitFromInput(tokenLimitInput.value) : 480000000;
    const currUpstream = upstreamInput ? normalizeUpstreamUrl(upstreamInput.value) : '';
    const runningUpstream = normalizeUpstreamUrl(State.runningProxyConfig.upstream);

    return currPort !== State.runningProxyConfig.port ||
           currHost !== State.runningProxyConfig.host ||
           currTokenLimit !== State.runningProxyConfig.token_limit ||
           currUpstream !== runningUpstream;
  }

  function updateProxyConfigDirtyState() {
    const isDirty = isProxyConfigDirty();
    const noticeEl = document.getElementById('proxyConfigStatusNotice');
    const restartBtn = document.getElementById('proxyRestartBtn');
    const resetBtn = document.getElementById('proxyResetConfigBtn');

    if (noticeEl) {
      noticeEl.style.display = isDirty ? 'inline-flex' : 'none';
    }
    if (restartBtn) {
      if (isDirty) {
        restartBtn.classList.add('pending-restart');
      } else {
        restartBtn.classList.remove('pending-restart');
      }
    }
    if (resetBtn) {
      resetBtn.style.display = isDirty ? 'inline-flex' : 'none';
    }
  }

  function resetProxyConfigInputs() {
    if (!State.runningProxyConfig) return;
    const portInput = document.getElementById('proxyConfigPort');
    const hostInput = document.getElementById('proxyConfigHost');
    const tokenLimitInput = document.getElementById('proxyConfigTokenLimit');
    const upstreamInput = document.getElementById('proxyConfigUpstream');

    if (portInput) portInput.value = State.runningProxyConfig.port;
    if (hostInput) hostInput.value = State.runningProxyConfig.host;
    if (tokenLimitInput) tokenLimitInput.value = formatTokenLimitToMillion(State.runningProxyConfig.token_limit ?? 480000000);
    if (upstreamInput) upstreamInput.value = State.runningProxyConfig.upstream;

    updateProxyConfigDirtyState();
  }

  /**
   * Load live Proxy Gateway Status
   */
  async function loadProxyStatus() {
    // If the browser tab or window is hidden / not visible, completely skip network requests
    if (document.hidden) return;
    if (State.isProxyStatusLoading) return;
    State.isProxyStatusLoading = true;

    try {
      const port = State.runningProxyConfig ? State.runningProxyConfig.port : (parseInt(document.getElementById('proxyConfigPort')?.value || '9090', 10));
      const data = await TelemetryAPI.getProxyStatus(port);
      State.proxyStatus = data;

      const activePort = data.port || 9090;
      const activeHost = data.host || '0.0.0.0';
      const activeTokenLimit = data.token_limit || (data.token_budget && data.token_budget.daily_limit) || (data.health && data.health.token_budget && data.health.token_budget.daily_limit) || 480000000;
      const activeUpstream = data.upstream || (data.health && data.health.upstream) || (State.runningProxyConfig ? State.runningProxyConfig.upstream : 'https://llm.ai.e-infra.cz/v1');

      State.runningProxyConfig = {
        port: activePort,
        host: activeHost,
        token_limit: activeTokenLimit,
        upstream: activeUpstream
      };

      if (activeUpstream) {
        saveUpstreamToHistory(activeUpstream);
      }

      // Only synchronize input values if user is NOT currently editing/dirty
      if (!isProxyConfigDirty()) {
        const portInput = document.getElementById('proxyConfigPort');
        const hostInput = document.getElementById('proxyConfigHost');
        const tokenLimitInput = document.getElementById('proxyConfigTokenLimit');
        const upstreamInput = document.getElementById('proxyConfigUpstream');
        if (portInput && document.activeElement !== portInput) portInput.value = activePort;
        if (hostInput && document.activeElement !== hostInput) hostInput.value = activeHost;
        if (tokenLimitInput && document.activeElement !== tokenLimitInput) {
          tokenLimitInput.value = formatTokenLimitToMillion(activeTokenLimit);
        }
        if (upstreamInput && document.activeElement !== upstreamInput) upstreamInput.value = activeUpstream;
      }

      UI.renderProxyStatus(data);
      updateProxyConfigDirtyState();
    } catch (e) {
      console.warn('Failed to load proxy status', e);
    } finally {
      State.isProxyStatusLoading = false;
    }
  }

  /**
   * Load live Proxy Execution Logs
   */
  async function loadProxyLogs() {
    if (document.hidden) return;
    try {
      const linesSelect = document.getElementById('proxyLogLinesSelect');
      const lines = linesSelect ? parseInt(linesSelect.value, 10) : 200;
      const autoScrollChk = document.getElementById('proxyLogAutoScroll');
      const autoScroll = autoScrollChk ? autoScrollChk.checked : true;
      const data = await TelemetryAPI.getProxyLogs(lines);
      UI.renderProxyLogs(data, autoScroll);
    } catch (e) {
      console.warn('Failed to load proxy logs', e);
    }
  }

  /**
   * Setup Gateway Control Listeners and Actions
   */
  function setupProxyControl() {
    // Render stored upstream history options
    renderUpstreamHistoryOptions();

    // Top header badge click -> switch to control panel tab
    const headerBadge = document.getElementById('proxyHeaderBadge');
    if (headerBadge) {
      headerBadge.addEventListener('click', () => {
        switchTab('controlPanelTab');
      });
    }

    const headerConcurrencyBadge = document.getElementById('headerConcurrencyBadge');
    if (headerConcurrencyBadge) {
      headerConcurrencyBadge.addEventListener('click', () => {
        switchTab('controlPanelTab');
      });
    }

    // Proxy configuration input listeners for dirty state tracking
    const portInput = document.getElementById('proxyConfigPort');
    const hostInput = document.getElementById('proxyConfigHost');
    const tokenLimitInput = document.getElementById('proxyConfigTokenLimit');
    const upstreamInput = document.getElementById('proxyConfigUpstream');

    if (portInput) {
      portInput.addEventListener('input', updateProxyConfigDirtyState);
      portInput.addEventListener('change', updateProxyConfigDirtyState);
    }
    if (hostInput) {
      hostInput.addEventListener('input', updateProxyConfigDirtyState);
      hostInput.addEventListener('change', updateProxyConfigDirtyState);
    }
    if (tokenLimitInput) {
      tokenLimitInput.addEventListener('input', updateProxyConfigDirtyState);
      tokenLimitInput.addEventListener('change', updateProxyConfigDirtyState);
    }
    if (upstreamInput) {
      upstreamInput.addEventListener('input', updateProxyConfigDirtyState);
      upstreamInput.addEventListener('change', updateProxyConfigDirtyState);
    }

    // Quick select history dropdown
    const upstreamQuickSelect = document.getElementById('proxyUpstreamQuickSelect');
    if (upstreamQuickSelect) {
      upstreamQuickSelect.addEventListener('change', () => {
        if (upstreamInput && upstreamQuickSelect.value) {
          upstreamInput.value = upstreamQuickSelect.value;
          upstreamQuickSelect.selectedIndex = 0;
          updateProxyConfigDirtyState();
        }
      });
    }

    // Discard / Reset button
    const resetBtn = document.getElementById('proxyResetConfigBtn');
    if (resetBtn) {
      resetBtn.addEventListener('click', () => {
        resetProxyConfigInputs();
        UI.showProxyAlert('Configuration edits discarded.', 'info', 2500);
      });
    }

    // Start Gateway button
    const startBtn = document.getElementById('proxyStartBtn');
    if (startBtn) {
      startBtn.addEventListener('click', async () => {
        const port = parseInt(document.getElementById('proxyConfigPort')?.value || '9090', 10);
        const host = document.getElementById('proxyConfigHost')?.value || '0.0.0.0';
        const tokenLimit = parseTokenLimitFromInput(document.getElementById('proxyConfigTokenLimit')?.value);
        const upstream = document.getElementById('proxyConfigUpstream')?.value || 'https://llm.ai.e-infra.cz/v1';

        startBtn.disabled = true;
        saveUpstreamToHistory(upstream);
        UI.showProxyAlert(`Starting proxy gateway on port ${port}...`, 'info', 0);
        try {
          const res = await TelemetryAPI.startProxy({ port, host, upstream, token_limit: tokenLimit });
          if (res.success) {
            UI.showProxyAlert(res.message || 'Proxy started successfully.', 'success', 5000);
          } else {
            UI.showProxyAlert(res.error || 'Failed to start proxy.', 'error', 8000);
          }
        } catch (err) {
          UI.showProxyAlert(`Start error: ${err.message}`, 'error', 8000);
        } finally {
          await loadProxyStatus();
          await loadProxyLogs();
        }
      });
    }

    // Stop Gateway button
    const stopBtn = document.getElementById('proxyStopBtn');
    if (stopBtn) {
      stopBtn.addEventListener('click', async () => {
        if (!confirm('Are you sure you want to stop the LLM Telemetry Proxy gateway?')) return;
        stopBtn.disabled = true;
        UI.showProxyAlert('Stopping proxy gateway...', 'info', 0);
        try {
          const res = await TelemetryAPI.stopProxy({ force: true });
          if (res.success) {
            UI.showProxyAlert(res.message || 'Proxy stopped successfully.', 'success', 5000);
          } else {
            UI.showProxyAlert(res.error || 'Failed to stop proxy.', 'error', 8000);
          }
        } catch (err) {
          UI.showProxyAlert(`Stop error: ${err.message}`, 'error', 8000);
        } finally {
          await loadProxyStatus();
          await loadProxyLogs();
        }
      });
    }

    // Restart Gateway button
    const restartBtn = document.getElementById('proxyRestartBtn');
    if (restartBtn) {
      restartBtn.addEventListener('click', async () => {
        const port = parseInt(document.getElementById('proxyConfigPort')?.value || '9090', 10);
        const host = document.getElementById('proxyConfigHost')?.value || '0.0.0.0';
        const tokenLimit = parseTokenLimitFromInput(document.getElementById('proxyConfigTokenLimit')?.value);
        const upstream = document.getElementById('proxyConfigUpstream')?.value || 'https://llm.ai.e-infra.cz/v1';

        restartBtn.disabled = true;
        saveUpstreamToHistory(upstream);
        UI.showProxyAlert(`Restarting proxy gateway on port ${port}...`, 'info', 0);
        try {
          const res = await TelemetryAPI.restartProxy({ port, host, upstream, token_limit: tokenLimit });
          if (res.success) {
            UI.showProxyAlert(res.message || 'Proxy restarted successfully.', 'success', 5000);
          } else {
            UI.showProxyAlert(res.error || 'Failed to restart proxy.', 'error', 8000);
          }
        } catch (err) {
          UI.showProxyAlert(`Restart error: ${err.message}`, 'error', 8000);
        } finally {
          await loadProxyStatus();
          await loadProxyLogs();
        }
      });
    }

    // Log Refresh button
    const logRefreshBtn = document.getElementById('proxyLogRefreshBtn');
    if (logRefreshBtn) {
      logRefreshBtn.addEventListener('click', async () => {
        await loadProxyLogs();
      });
    }

    // Log Lines select change
    const logLinesSelect = document.getElementById('proxyLogLinesSelect');
    if (logLinesSelect) {
      logLinesSelect.addEventListener('change', () => {
        loadProxyLogs();
      });
    }

    // Log Clear button
    const logClearBtn = document.getElementById('proxyLogClearBtn');
    if (logClearBtn) {
      logClearBtn.addEventListener('click', async () => {
        if (!confirm('Clear the proxy log file?')) return;
        try {
          await TelemetryAPI.clearProxyLogs();
          await loadProxyLogs();
          UI.showProxyAlert('Proxy logs cleared.', 'info', 3000);
        } catch (err) {
          UI.showProxyAlert(`Failed to clear logs: ${err.message}`, 'error', 5000);
        }
      });
    }

    // Tab/window activation listeners: immediately refresh proxy status without waiting for the 3s cycle
    const onTabActivated = () => {
      if (!document.hidden && document.visibilityState === 'visible') {
        loadProxyStatus();
        if (State.activeTab === 'controlPanelTab') {
          loadProxyLogs();
          loadRawLogStatus();
        }
      }
    };
    document.addEventListener('visibilitychange', onTabActivated);
    window.addEventListener('focus', onTabActivated);

    // Run DB Compress button
    const runDbCompressBtn = document.getElementById('runDbCompressBtn');
    if (runDbCompressBtn) {
      runDbCompressBtn.addEventListener('click', async () => {
        if (!confirm('Run database compression? This aggregates historical data older than 14 days and creates an automatic backup (.db.bak).')) return;
        runDbCompressBtn.disabled = true;
        runDbCompressBtn.textContent = '⏳ Compressing Database...';
        UI.showProxyAlert('Running database compression in background...', 'info', 0);
        try {
          const res = await TelemetryAPI.runDbCompress();
          if (res.success) {
            UI.showProxyAlert('Database compression completed successfully!', 'success', 6000);
            alert(`Database Compression Result:\n\n${res.output}`);
          } else {
            UI.showProxyAlert(`Database compression finished with warnings/errors.`, 'error', 8000);
            alert(`Database Compression Output:\n\n${res.output || res.error}`);
          }
        } catch (err) {
          UI.showProxyAlert(`Compression error: ${err.message}`, 'error', 8000);
        } finally {
          runDbCompressBtn.disabled = false;
          runDbCompressBtn.textContent = '⚡ Run Database Compression (db_compress.py)';
          await loadHealth();
          await refresh();
        }
      });
    }

    // Raw Payload Logging Toggle button
    const rawLogToggleBtn = document.getElementById('rawLogToggleBtn');
    if (rawLogToggleBtn) {
      rawLogToggleBtn.addEventListener('click', async () => {
        try {
          rawLogToggleBtn.disabled = true;
          const res = await TelemetryAPI.toggleRawLog();
          UI.showRawLogAlert(res.message || 'Raw logging toggled.', 'info', 3500);
          await loadRawLogStatus();
        } catch (err) {
          UI.showRawLogAlert(`Toggle error: ${err.message}`, 'error', 5000);
        } finally {
          rawLogToggleBtn.disabled = false;
        }
      });
    }

    // Open Standalone Inspector Window button
    const openInspectorBtn = document.getElementById('openInspectorBtn');
    if (openInspectorBtn) {
      openInspectorBtn.addEventListener('click', () => {
        const url = (TelemetryAPI.BASE_URL || '') + '/inspector';
        window.open(url, '_blank', 'noopener,noreferrer');
      });
    }

    // Clear Raw Log File button
    const rawLogClearBtn = document.getElementById('rawLogClearBtn');
    if (rawLogClearBtn) {
      rawLogClearBtn.addEventListener('click', async () => {
        if (!confirm('Are you sure you want to clear the raw JSON payloads file (logger/payloads.jsonl)?')) return;
        try {
          rawLogClearBtn.disabled = true;
          const res = await TelemetryAPI.clearRawLogs();
          UI.showRawLogAlert(res.message || 'Logger file cleared.', 'success', 3500);
          await loadRawLogStatus();
        } catch (err) {
          UI.showRawLogAlert(`Clear error: ${err.message}`, 'error', 5000);
        } finally {
          rawLogClearBtn.disabled = false;
        }
      });
    }

    // Auto-Sync Model Prices buttons (Control Panel & Cost Analyzer Tab)
    async function triggerCostSync(btn, alertEl) {
      if (!btn) return;
      const origHtml = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = '<span class="btn-icon">⏳</span> Syncing with LiteLLM...';

      if (alertEl) {
        alertEl.className = 'proxy-alert info';
        alertEl.textContent = 'Fetching latest model pricing from GitHub (BerriAI/litellm)...';
        alertEl.style.display = 'block';
      }

      try {
        const res = await TelemetryAPI.syncCosts();
        if (res.status === 'ok') {
          const msg = res.message || 'Cost sync completed.';
          if (alertEl) {
            alertEl.className = 'proxy-alert success';
            alertEl.textContent = `✅ ${msg}`;
            alertEl.style.display = 'block';
            setTimeout(() => {
              if (alertEl) alertEl.style.display = 'none';
            }, 8000);
          }
          await loadCosts();
          await refresh();
        } else {
          if (alertEl) {
            alertEl.className = 'proxy-alert error';
            alertEl.textContent = `❌ Sync failed: ${res.error || 'Unknown error'}`;
            alertEl.style.display = 'block';
          }
        }
      } catch (err) {
        if (alertEl) {
          alertEl.className = 'proxy-alert error';
          alertEl.textContent = `❌ Sync error: ${err.message}`;
          alertEl.style.display = 'block';
        }
      } finally {
        btn.disabled = false;
        btn.innerHTML = origHtml;
      }
    }

    const syncCostsBtnControlPanel = document.getElementById('syncCostsBtnControlPanel');
    const costSyncAlertControlPanel = document.getElementById('costSyncAlertControlPanel');
    if (syncCostsBtnControlPanel) {
      syncCostsBtnControlPanel.addEventListener('click', () => {
        triggerCostSync(syncCostsBtnControlPanel, costSyncAlertControlPanel);
      });
    }

    const syncCostsBtnCostsTab = document.getElementById('syncCostsBtnCostsTab');
    const costSyncAlertCostsTab = document.getElementById('costSyncAlertCostsTab');
    if (syncCostsBtnCostsTab) {
      syncCostsBtnCostsTab.addEventListener('click', () => {
        triggerCostSync(syncCostsBtnCostsTab, costSyncAlertCostsTab);
      });
    }
  }

  /**
   * Load Raw Payload Logging Status
   */
  async function loadRawLogStatus() {
    if (document.hidden) return;
    try {
      const data = await TelemetryAPI.getRawLogStatus();
      UI.renderRawLogStatus(data);
    } catch (e) {
      console.warn('Failed to load raw log status', e);
    }
  }

  /**
   * Start interval polling loops
   */
  function startIntervals() {
    stopIntervals();

    // 1. Live Proxy Gateway status & Active Concurrency Heartbeat — ALWAYS active across all tabs
    State.intervals.proxyStatus = setInterval(() => {
      if (document.visibilityState === 'visible') {
        loadProxyStatus();
        if (State.activeTab === 'controlPanelTab') {
          loadProxyLogs();
          loadRawLogStatus();
        }
      }
    }, 3000);

    // 2. Telemetry query intervals (respects liveUpdatesEnabled toggle)
    if (!State.liveUpdatesEnabled) return;

    State.intervals.refresh = setInterval(refresh, State.refreshRateSeconds * 1000);
    if (State.eInfraEnabled) {
      State.intervals.serverStatus = setInterval(loadServerStatus, 15000);
    }
    State.intervals.crossCheck = setInterval(loadCrossCheck, 30000);
  }

  /**
   * Stop interval polling loops
   */
  function stopIntervals() {
    if (State.intervals.refresh) clearInterval(State.intervals.refresh);
    if (State.intervals.serverStatus) clearInterval(State.intervals.serverStatus);
    if (State.intervals.crossCheck) clearInterval(State.intervals.crossCheck);
    if (State.intervals.proxyStatus) clearInterval(State.intervals.proxyStatus);
    if (State.intervals.rawLogStatus) clearInterval(State.intervals.rawLogStatus);
    if (State.intervals.proxyLogs) clearInterval(State.intervals.proxyLogs);
    
    State.intervals.refresh = null;
    State.intervals.serverStatus = null;
    State.intervals.crossCheck = null;
    State.intervals.proxyStatus = null;
    State.intervals.rawLogStatus = null;
    State.intervals.proxyLogs = null;
  }

  function ensureLiveNodesConfig(allFetchedNames) {
    if (!State.liveNodesConfig) {
      State.liveNodesConfig = [];
    }
    
    const config = State.liveNodesConfig;
    const existingNames = new Set(config.map(n => n.name));
    
    let changed = false;
    allFetchedNames.forEach(name => {
      if (!existingNames.has(name)) {
        config.push({
          name: name,
          visible: true
        });
        changed = true;
      }
    });
    
    if (changed) {
      saveLiveNodesConfig();
    }
  }

  function saveLiveNodesConfig() {
    localStorage.setItem('telemetry_dashboard_live_nodes_config', JSON.stringify(State.liveNodesConfig));
  }

  async function openLiveNodesModal() {
    const modal = document.getElementById('liveNodesModal');
    if (!modal) return;
    
    modal.classList.add('open');
    
    const container = document.getElementById('liveNodesListContainer');
    if (!container) return;
    
    if (!State.eInfraEnabled) {
      container.innerHTML = '<div style="color:var(--text-muted); text-align:center; padding: 20px;">e-INFRA live sync is toggled off. Please toggle Sync on in the sidebar first.</div>';
      return;
    }
    
    container.innerHTML = `
      <div style="display:flex; flex-direction:column; align-items:center; justify-content:center; padding: 30px; gap: 10px; color: var(--text-muted);">
        <div class="spinner"></div>
        <span>Syncing latest infrastructure nodes...</span>
      </div>
    `;
    
    try {
      const data = await TelemetryAPI.getServerStatus();
      State.allLiveModelsData = data.models || [];
      State.allLiveModels = (data.models || []).map(m => m.name);
      
      ensureLiveNodesConfig(State.allLiveModels);
      
      // Update background dashboard nodes list
      UI.renderServerStatus(data, State.liveNodesConfig);
      
      rebuildLiveNodesEditList();
    } catch (e) {
      console.error('Failed to sync nodes for edit list', e);
      container.innerHTML = `<div class="tag tag-error" style="padding:10px; text-align:center;">Failed to sync online nodes: ${e.message}</div>`;
    }
  }

  function rebuildLiveNodesEditList() {
    const container = document.getElementById('liveNodesListContainer');
    if (!container) return;
    
    const allFetched = State.allLiveModels || [];
    if (!allFetched.length) {
      container.innerHTML = '<div style="color:var(--text-muted); text-align:center; padding: 20px;">No active nodes fetched yet. Toggle Sync on to retrieve them.</div>';
      return;
    }
    
    ensureLiveNodesConfig(allFetched);
    const config = State.liveNodesConfig || [];
    
    container.innerHTML = config.map((node, index) => {
      return `
        <div class="node-edit-item" data-index="${index}">
          <input type="checkbox" id="chk_node_${index}" ${node.visible ? 'checked' : ''}>
          <label class="node-name-label" for="chk_node_${index}">
            <span class="tag ${UI.getModelClass(node.name)}">${node.name}</span>
          </label>
          <div class="node-order-actions">
            <button class="btn-order btn-up" ${index === 0 ? 'disabled' : ''} title="Move Up">▲</button>
            <button class="btn-order btn-down" ${index === config.length - 1 ? 'disabled' : ''} title="Move Down">▼</button>
          </div>
        </div>
      `;
    }).join('');
    
    // Attach listeners
    container.querySelectorAll('.node-edit-item').forEach(item => {
      const idx = parseInt(item.dataset.index);
      const chk = item.querySelector('input[type="checkbox"]');
      const btnUp = item.querySelector('.btn-up');
      const btnDown = item.querySelector('.btn-down');
      
      chk.addEventListener('change', (e) => {
        config[idx].visible = e.target.checked;
        saveLiveNodesConfig();
        UI.renderServerStatus({ models: State.allLiveModelsData }, State.liveNodesConfig);
      });
      
      btnUp.addEventListener('click', () => {
        if (idx > 0) {
          const temp = config[idx];
          config[idx] = config[idx - 1];
          config[idx - 1] = temp;
          saveLiveNodesConfig();
          rebuildLiveNodesEditList();
          UI.renderServerStatus({ models: State.allLiveModelsData }, State.liveNodesConfig);
        }
      });
      
      btnDown.addEventListener('click', () => {
        if (idx < config.length - 1) {
          const temp = config[idx];
          config[idx] = config[idx + 1];
          config[idx + 1] = temp;
          saveLiveNodesConfig();
          rebuildLiveNodesEditList();
          UI.renderServerStatus({ models: State.allLiveModelsData }, State.liveNodesConfig);
        }
      });
    });
  }

  return {
    init,
    refresh,
    loadServerStatus,
    loadCrossCheck,
    loadHealth,
    loadProxyStatus,
    loadProxyLogs,
    switchTab
  };
})();

// Document Ready Bootstrap
document.addEventListener('DOMContentLoaded', () => {
  App.init();
});
