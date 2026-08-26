const SUBVIEW_CONFIG = {
  metrics: {
    path: "/admin/observability/metrics/partial",
    containerId: "metrics-container",
    factoryName: "createMetricsController",
    providerName: "observabilityMetrics",
  },
  tools: {
    path: "/admin/observability/tools/partial",
    containerId: "tools-container",
    factoryName: "createToolsController",
    providerName: "observabilityTools",
  },
  prompts: {
    path: "/admin/observability/prompts/partial",
    containerId: "prompts-container",
    factoryName: "createPromptsController",
    providerName: "observabilityPrompts",
  },
  resources: {
    path: "/admin/observability/resources/partial",
    containerId: "resources-container",
    factoryName: "createResourcesController",
    providerName: "observabilityResources",
  },
};

const FILTER_FIELDS = [
  "timeRange",
  "statusFilter",
  "minDuration",
  "maxDuration",
  "httpMethod",
  "userEmail",
  "nameSearch",
  "attributeSearch",
  "toolName",
];

function rootPath() {
  return window.ROOT_PATH || "";
}

function showRequestError(targetId, message) {
  const target = document.getElementById(targetId);
  if (!target) return;

  target.replaceChildren();
  const error = document.createElement("div");
  error.className = "p-4 text-red-600 dark:text-red-400";
  error.textContent = message;
  target.appendChild(error);
}

/**
 * Execute trusted inline scripts from an Observability sub-view, then return
 * its script-free markup. The scripts come from authenticated, same-origin
 * server templates and inherit the current page's CSP nonce.
 */
export function executeObservabilityScriptsAndStrip(html) {
  const scriptPattern = new RegExp(
    "<" + "script\\b([^>]*)>([\\s\\S]*?)</" + "script>",
    "gi"
  );
  let match;

  while ((match = scriptPattern.exec(html)) !== null) {
    const attributes = match[1];
    const code = match[2].trim();
    if (/(?:^|\s)src\s*=/i.test(attributes)) {
      console.warn(
        "[observability] external scripts are not supported in sub-views"
      );
      continue;
    }
    if (!code) continue;

    const script = document.createElement("script");
    script.textContent = code;
    const nonce = window.htmxConfig?.inlineScriptNonce;
    if (nonce) script.setAttribute("nonce", nonce);
    document.head.appendChild(script);
    script.remove();
  }

  return html.replace(
    new RegExp("<" + "script\\b[^>]*>[\\s\\S]*?</" + "script>", "gi"),
    ""
  );
}

function registerSubviewProvider(config) {
  const factory = window[config.factoryName];
  if (typeof factory !== "function") {
    throw new Error(`Controller ${config.factoryName} was not loaded`);
  }
  if (!window.Alpine || typeof window.Alpine.data !== "function") {
    throw new Error("Alpine is not available");
  }
  window.Alpine.data(config.providerName, factory);
}

function initializeSubview(container, html) {
  if (
    window.Alpine &&
    typeof window.Alpine.mutateDom === "function" &&
    typeof window.Alpine.initTree === "function" &&
    typeof window.Alpine.destroyTree === "function"
  ) {
    window.Alpine.mutateDom(() => {
      // Mutation observation is paused here, so Alpine cannot discover removed
      // component roots on its own. Destroy them explicitly before replacement.
      Array.from(container.children).forEach((child) => {
        window.Alpine.destroyTree(child);
      });
      container.innerHTML = html;
    });
    // The container already belongs to the dashboard's Alpine tree and owns
    // its x-show directive. Re-initializing that node detaches the directive
    // from the parent scope, leaving the selected view hidden. Only initialize
    // the newly inserted sub-view roots.
    Array.from(container.children).forEach((child) => {
      window.Alpine.initTree(child);
    });
    return;
  }
  container.innerHTML = html;
}

export function observabilityDashboard() {
  return {
    viewMode: "traces",
    selectedTrace: null,
    timeRange: "24h",
    statusFilter: "all",
    minDuration: "",
    maxDuration: "",
    httpMethod: "",
    userEmail: "",
    nameSearch: "",
    attributeSearch: "",
    toolName: "",
    showAdvancedFilters: false,
    savedQueries: [],
    selectedQueryId: "",
    showSaveQueryModal: false,
    saveQueryName: "",
    saveQueryDescription: "",
    saveQueryIsShared: false,
    metricsLoaded: false,
    toolsLoaded: false,
    promptsLoaded: false,
    resourcesLoaded: false,
    metricsLoading: false,
    toolsLoading: false,
    promptsLoading: false,
    resourcesLoading: false,
    tracesInterval: null,
    statsInterval: null,
    filterRefreshTimer: null,

    init() {
      this.startPolling();
      this.loadSavedQueries();

      FILTER_FIELDS.forEach((field) => {
        this.$watch(field, () => this.scheduleFilterRefresh());
      });
      this.$watch("selectedQueryId", (queryId) => {
        if (queryId) this.applySavedQuery();
      });
    },

    destroy() {
      this.stopPolling();
      if (this.filterRefreshTimer) {
        window.clearTimeout(this.filterRefreshTimer);
        this.filterRefreshTimer = null;
      }
    },

    async loadSubview(mode) {
      const config = SUBVIEW_CONFIG[mode];
      if (!config) return;

      const loadedKey = `${mode}Loaded`;
      const loadingKey = `${mode}Loading`;
      if (this[loadedKey] || this[loadingKey]) return;

      this[loadingKey] = true;
      try {
        const response = await fetch(`${rootPath()}${config.path}`);
        if (!response.ok) {
          throw new Error(`Request returned ${response.status}`);
        }
        if (this.viewMode !== mode) return;

        const cleanHtml = executeObservabilityScriptsAndStrip(
          await response.text()
        );
        registerSubviewProvider(config);

        const container = document.getElementById(config.containerId);
        if (!container) throw new Error("Subview container was not found");
        initializeSubview(container, cleanHtml);

        if (this.viewMode === mode) this[loadedKey] = true;
      } catch (error) {
        console.error(`Failed to load Observability ${mode} view:`, error);
        showRequestError(
          config.containerId,
          `Failed to load ${mode} view. Please try again.`
        );
      } finally {
        this[loadingKey] = false;
      }
    },

    setViewMode(mode) {
      this.viewMode = mode;
      if (mode !== "traces") this.loadSubview(mode);
    },

    traceQueryString() {
      const params = new URLSearchParams({
        time_range: this.timeRange,
        status_filter: this.statusFilter,
        limit: "50",
      });
      const optionalFilters = {
        min_duration: this.minDuration,
        max_duration: this.maxDuration,
        http_method: this.httpMethod,
        user_email: this.userEmail,
        name_search: this.nameSearch,
        attribute_search: this.attributeSearch,
        tool_name: this.toolName,
      };
      Object.entries(optionalFilters).forEach(([name, value]) => {
        if (value !== "" && value != null) params.set(name, value);
      });
      return params.toString();
    },

    refreshTraces() {
      const htmx = window.htmx;
      if (!htmx || typeof htmx.ajax !== "function") {
        showRequestError("traces-list", "Unable to load traces.");
        return;
      }
      htmx
        .ajax(
          "GET",
          `${rootPath()}/admin/observability/traces?${this.traceQueryString()}`,
          { target: "#traces-list", swap: "innerHTML" }
        )
        .catch((error) => {
          console.error("Failed to refresh Observability traces:", error);
          showRequestError("traces-list", "Failed to load traces.");
        });
    },

    refreshStats() {
      const htmx = window.htmx;
      if (!htmx || typeof htmx.ajax !== "function") {
        showRequestError("stats-container", "Unable to load statistics.");
        return;
      }
      htmx
        .ajax("GET", `${rootPath()}/admin/observability/stats`, {
          target: "#stats-container",
          swap: "innerHTML",
        })
        .catch((error) => {
          console.error("Failed to refresh Observability statistics:", error);
          showRequestError("stats-container", "Failed to load statistics.");
        });
    },

    startPolling() {
      this.stopPolling();
      this.refreshTraces();
      this.refreshStats();
      this.tracesInterval = window.setInterval(
        () => this.refreshTraces(),
        5000
      );
      this.statsInterval = window.setInterval(() => this.refreshStats(), 30000);
    },

    stopPolling() {
      if (this.tracesInterval) window.clearInterval(this.tracesInterval);
      if (this.statsInterval) window.clearInterval(this.statsInterval);
      this.tracesInterval = null;
      this.statsInterval = null;
    },

    scheduleFilterRefresh() {
      if (this.filterRefreshTimer) {
        window.clearTimeout(this.filterRefreshTimer);
      }
      this.filterRefreshTimer = window.setTimeout(() => {
        this.filterRefreshTimer = null;
        this.applyFilters();
      }, 150);
    },

    applyFilters() {
      this.startPolling();
    },

    refreshAll() {
      this.refreshStats();
      this.refreshTraces();
    },

    clearFilters() {
      this.minDuration = "";
      this.maxDuration = "";
      this.httpMethod = "";
      this.userEmail = "";
      this.nameSearch = "";
      this.attributeSearch = "";
      this.toolName = "";
      this.scheduleFilterRefresh();
    },

    async loadSavedQueries() {
      try {
        const response = await fetch(
          `${rootPath()}/admin/observability/queries`
        );
        if (!response.ok) {
          throw new Error(`Request returned ${response.status}`);
        }
        this.savedQueries = await response.json();
      } catch (error) {
        console.error("Failed to load saved Observability queries:", error);
      }
    },

    async applySavedQuery() {
      if (!this.selectedQueryId) return;
      try {
        const response = await fetch(
          `${rootPath()}/admin/observability/queries/${this.selectedQueryId}`
        );
        if (!response.ok) {
          throw new Error(`Request returned ${response.status}`);
        }

        const query = await response.json();
        const config = query.filter_config || {};
        this.timeRange = config.timeRange || "24h";
        this.statusFilter = config.statusFilter || "all";
        this.minDuration = config.minDuration || "";
        this.maxDuration = config.maxDuration || "";
        this.toolName = config.toolName || "";
        this.httpMethod = config.httpMethod || "";
        this.userEmail = config.userEmail || "";
        this.nameSearch = config.nameSearch || "";
        this.attributeSearch = config.attributeSearch || "";
        this.scheduleFilterRefresh();

        const usageResponse = await fetch(
          `${rootPath()}/admin/observability/queries/${this.selectedQueryId}/use`,
          { method: "POST" }
        );
        if (!usageResponse.ok) {
          console.warn("Failed to update saved query usage count");
        }
      } catch (error) {
        console.error("Failed to apply saved Observability query:", error);
      }
    },

    getCurrentFilterConfig() {
      return {
        timeRange: this.timeRange,
        statusFilter: this.statusFilter,
        minDuration: this.minDuration,
        maxDuration: this.maxDuration,
        httpMethod: this.httpMethod,
        userEmail: this.userEmail,
        nameSearch: this.nameSearch,
        attributeSearch: this.attributeSearch,
        toolName: this.toolName,
      };
    },

    async saveCurrentQuery() {
      const name = this.saveQueryName.trim();
      if (!name) {
        window.alert("Please enter a name for the query");
        return;
      }

      try {
        const response = await fetch(
          `${rootPath()}/admin/observability/queries`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              name,
              description: this.saveQueryDescription.trim() || null,
              filter_config: this.getCurrentFilterConfig(),
              is_shared: this.saveQueryIsShared,
            }),
          }
        );
        if (!response.ok) {
          const payload = await response.json().catch(() => ({}));
          throw new Error(
            payload.detail || `Request returned ${response.status}`
          );
        }

        this.showSaveQueryModal = false;
        this.saveQueryName = "";
        this.saveQueryDescription = "";
        this.saveQueryIsShared = false;
        await this.loadSavedQueries();
        window.alert("Query saved successfully!");
      } catch (error) {
        console.error("Failed to save Observability query:", error);
        window.alert(`Failed to save query: ${error.message}`);
      }
    },

    async deleteSavedQuery(queryId) {
      if (
        !window.confirm("Are you sure you want to delete this saved query?")
      ) {
        return;
      }
      try {
        const response = await fetch(
          `${rootPath()}/admin/observability/queries/${queryId}`,
          { method: "DELETE" }
        );
        if (!response.ok) {
          throw new Error(`Request returned ${response.status}`);
        }
        await this.loadSavedQueries();
        if (String(this.selectedQueryId) === String(queryId)) {
          this.selectedQueryId = "";
        }
      } catch (error) {
        console.error("Failed to delete saved Observability query:", error);
      }
    },

    resetLoadedFlags() {
      this.stopPolling();
      if (this.filterRefreshTimer) {
        window.clearTimeout(this.filterRefreshTimer);
        this.filterRefreshTimer = null;
      }
      this.viewMode = "traces";
      this.metricsLoaded = false;
      this.toolsLoaded = false;
      this.promptsLoaded = false;
      this.resourcesLoaded = false;
    },
  };
}
