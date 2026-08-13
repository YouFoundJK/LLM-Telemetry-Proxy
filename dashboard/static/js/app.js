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
      crossCheck: null
    },
    
    // Dropdown Instance
    modelDropdownInstance: null,
    
    // Flatpickr Instance
    datePickerInstance: null,
    
    // Abort controller for network query throttling
    activeAbortController: null
  };

  // Local browser session cache for telemetry raw responses
  const DataCache = {
    _cache: {},
    
    getKey(fromVal, toVal) {
      return `${fromVal || ''}_${toVal || ''}`;
    },
    
    get(fromVal, toVal) {
      const key = this.getKey(fromVal, toVal);
      return this._cache[key];
    },
    
    set(fromVal, toVal, data) {
      const key = this.getKey(fromVal, toVal);
      this._cache[key] = data;
      try {
        sessionStorage.setItem('telemetry_data_cache_v3_' + key, JSON.stringify(data));
      } catch (e) {
        // Safe quota recovery: clear telemetry cache and retry
        try {
          for (let i = sessionStorage.length - 1; i >= 0; i--) {
            const k = sessionStorage.key(i);
            if (k && k.startsWith('telemetry_data_cache_v3_')) {
              sessionStorage.removeItem(k);
            }
          }
          sessionStorage.setItem('telemetry_data_cache_v3_' + key, JSON.stringify(data));
        } catch (e2) {
          console.warn('Failed to save telemetry cache to sessionStorage:', e2);
        }
      }
    },
    
    loadFromSessionStorage(fromVal, toVal) {
      const key = this.getKey(fromVal, toVal);
      try {
        const cached = sessionStorage.getItem('telemetry_data_cache_v3_' + key);
        if (cached) {
          return JSON.parse(cached);
        }
      } catch (e) {
        console.warn('Failed to load data from sessionStorage', e);
      }
      return null;
    }
  };

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
      State.activeTab = savedFilters.activeTab;
      switchTab(savedFilters.activeTab);
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
      totalInput += (c.input_tokens || 0) * cnt;
      totalOutput += (c.output_tokens || 0) * cnt;
      
      if (c.ttfb_ms !== null && c.ttfb_ms !== undefined) {
        ttfbSum += c.ttfb_ms * cnt;
        ttfbCount += cnt;
      }
      if (c.total_ms !== null && c.total_ms !== undefined) {
        rttSum += c.total_ms * cnt;
        rttCount += cnt;
      }
      if ((c.output_tokens || 0) > 0 && (c.total_ms || 0) > 0) {
        tpsOutputTokens += (c.output_tokens || 0) * cnt;
        tpsTotalMs += (c.total_ms || 0) * cnt;
      }
      if (c.error) {
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
      g.total_input += (c.input_tokens || 0) * cnt;
      g.total_output += (c.output_tokens || 0) * cnt;

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
        g.tpsOutputTokens += (c.output_tokens || 0) * cnt;
        g.tpsTotalMs += (c.total_ms || 0) * cnt;
      }
      if (c.server_running !== null && c.server_running !== undefined) {
        g.loadSum += c.server_running * cnt;
        g.loadCount += cnt;
      }
      if (c.error) {
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
      filteredCalls = filteredCalls.filter(c => c.error);
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
    setupFilters();
    setupToggleSwitches();
    
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
    const btn = document.querySelector(`.tab-btn[data-tab="${tabId}"]`);
    if (!btn) return;

    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));

    btn.classList.add('active');
    const contentEl = document.getElementById(tabId);
    if (contentEl) contentEl.classList.add('active');

    State.activeTab = tabId;
    saveFiltersToLocalStorage();

    if (tabId === 'diagnosticsTab') {
      loadHealth();
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
   * Performs the initial full loading pipeline
   */
  async function loadCosts() {
    try {
      State.modelCosts = await TelemetryAPI.getCosts();
    } catch (e) {
      console.error('Failed to load model costs config', e);
    }
  }

  function enrichCallsWithCosts(calls) {
    if (!calls || !State.modelCosts) return;
    calls.forEach(c => {
      const modelKey = c.model ? c.model.toLowerCase() : '';
      let costConfig = null;
      const costKeys = Object.keys(State.modelCosts);
      
      for (const k of costKeys) {
        if (modelKey === k.toLowerCase()) {
          costConfig = State.modelCosts[k];
          break;
        }
      }
      
      if (!costConfig) {
        const sortedKeys = [...costKeys].sort((a, b) => b.length - a.length);
        for (const k of sortedKeys) {
          if (modelKey.includes(k.toLowerCase())) {
            costConfig = State.modelCosts[k];
            break;
          }
        }
      }
      
      if (!costConfig && modelKey) {
        for (const k of costKeys) {
          if (k.toLowerCase().includes(modelKey)) {
            costConfig = State.modelCosts[k];
            break;
          }
        }
      }
      const callsCount = c.calls_count !== undefined && c.calls_count !== null ? c.calls_count : 1;
      if (costConfig) {
        c.input_cost = ((c.input_tokens || 0) / 1e6) * (costConfig.input_cost_per_million || 0) * callsCount;
        c.output_cost = ((c.output_tokens || 0) / 1e6) * (costConfig.output_cost_per_million || 0) * callsCount;
        c.total_cost = c.input_cost + c.output_cost;
        c.provider_source = costConfig.provider_source || 'Unknown';
        c.last_updated = costConfig.last_updated || '';
      } else {
        c.input_cost = 0;
        c.output_cost = 0;
        c.total_cost = 0;
        c.provider_source = 'Unknown';
        c.last_updated = '';
      }
    });
  }

  async function performInitialLoad() {
    await loadCosts();
    await refresh();
    await loadServerStatus();
    await loadCrossCheck();
    await loadHealth();
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

    const editBtn = document.getElementById('editLiveNodesBtn');
    const modal = document.getElementById('liveNodesModal');
    const closeBtn = document.getElementById('closeLiveNodesModalBtn');

    if (editBtn && modal) {
      editBtn.addEventListener('click', () => {
        openLiveNodesModal();
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

    let cachedData = DataCache.get(fromVal, toVal);
    if (!cachedData) {
      cachedData = DataCache.loadFromSessionStorage(fromVal, toVal);
      if (cachedData) {
        DataCache.set(fromVal, toVal, cachedData);
      }
    }

    if (cachedData && !forceFetch) {
      State.currentData = cachedData;
      renderTelemetry(cachedData);
      fetchAllTelemetryAsynchronously(fromVal, toVal, true);
    } else {
      fetchAllTelemetryAsynchronously(fromVal, toVal, false);
    }
  }

  async function fetchAllTelemetryAsynchronously(fromVal, toVal, isBackground = false) {
    if (State.activeAbortController) {
      State.activeAbortController.abort();
    }
    
    const controller = new AbortController();
    State.activeAbortController = controller;
    const signal = controller.signal;

    let allCalls = isBackground && State.currentData && State.currentData.calls ? [...State.currentData.calls] : [];
    let currentToVal = toVal;
    const limit = 10000;
    
    if (!isBackground) {
      showLoadingOverlays();
    }
    
    const existingIds = new Set(allCalls.map(c => c.id));
    let tempCalls = [];
    const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
    
    try {
      while (true) {
        if (signal.aborted) {
          break;
        }

        const filters = {
          from: fromVal,
          to: currentToVal,
          limit: limit
        };
        const data = await TelemetryAPI.query(filters, { signal });
        const newCalls = data.calls || [];
        
        if (newCalls.length === 0) {
          break;
        }
        
        let foundExisting = false;
        let addedCount = 0;
        
        for (let c of newCalls) {
          if (existingIds.has(c.id)) {
            foundExisting = true;
            if (isBackground) {
              break;
            }
          } else {
            tempCalls.push(c);
            existingIds.add(c.id);
            addedCount++;
          }
        }
        
        if (isBackground && foundExisting) {
          allCalls = [...tempCalls, ...allCalls];
          tempCalls = [];
          
          const mockDataResponse = {
            calls: allCalls,
            available_models: data.available_models,
            available_types: data.available_types,
            proxy_stats: data.proxy_stats,
            proxy_breakdown: data.proxy_breakdown
          };
          State.currentData = mockDataResponse;
          DataCache.set(fromVal, toVal, mockDataResponse);
          renderTelemetry(mockDataResponse);
          break;
        }
        
        allCalls = [...allCalls, ...tempCalls];
        tempCalls = [];

        const mockDataResponse = {
          calls: allCalls,
          available_models: data.available_models,
          available_types: data.available_types,
          proxy_stats: data.proxy_stats,
          proxy_breakdown: data.proxy_breakdown
        };
        
        State.currentData = mockDataResponse;
        DataCache.set(fromVal, toVal, mockDataResponse);
        renderTelemetry(mockDataResponse);
        
        if (newCalls.length < limit || addedCount === 0) {
          break;
        }
        
        const oldestTime = newCalls.reduce((min, c) => {
          const t = new Date(c.timestamp).getTime();
          return t < min ? t : min;
        }, Infinity);
        currentToVal = new Date(oldestTime - 1).toISOString();

        // Throttle sequential loop requests slightly to protect database and stay under rate limiter caps
        await sleep(80);
      }
    } catch (e) {
      if (e.name === 'AbortError') {
        console.log('Query fetch aborted successfully.');
        return;
      }
      console.error('Async pull failed:', e);
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
   * Health and Diagnostics details
   */
  async function loadHealth() {
    try {
      const data = await TelemetryAPI.getHealth();
      UI.renderHealth(data);
    } catch (e) {
      console.error('Diagnostics check failed', e);
    }
  }

  /**
   * Start interval polling loops
   */
  function startIntervals() {
    stopIntervals();
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
    
    State.intervals.refresh = null;
    State.intervals.serverStatus = null;
    State.intervals.crossCheck = null;
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
    loadHealth
  };
})();

// Document Ready Bootstrap
document.addEventListener('DOMContentLoaded', () => {
  App.init();
});
