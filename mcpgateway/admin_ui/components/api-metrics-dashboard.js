const REQUEST_TIMEOUT_MS = 15_000;

/**
 * Alpine component for the API Metrics dashboard.
 *
 * A single AbortController is shared by the four requests in each refresh. A
 * newer refresh invalidates the previous request so late responses cannot
 * overwrite current dashboard state.
 */
export function apiMetricsDashboard() {
  return {
    timeRange: 24,
    loading: false,
    error: null,
    stats: { total_traces: 0, success_count: 0, error_count: 0, avg_duration_ms: 0 },
    percentileData: { timestamps: [], p50: [], p90: [], p95: [], p99: [] },
    topErrors: [],
    topSlow: [],
    _requestController: null,
    _requestSequence: 0,

    async init() {
      await this.loadAll();
    },

    async loadAll() {
      if (this._requestController) {
        this._requestController.abort();
      }

      const requestSequence = ++this._requestSequence;
      const controller = new AbortController();
      this._requestController = controller;
      this.loading = true;
      this.error = null;

      let timedOut = false;
      const timeoutId = globalThis.setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, REQUEST_TIMEOUT_MS);

      try {
        const rootPath = window.ROOT_PATH || '';
        const requestOptions = { signal: controller.signal };
        const responses = await Promise.all([
          fetch(`${rootPath}/admin/observability/stats?hours=${this.timeRange}`, requestOptions),
          fetch(`${rootPath}/admin/observability/metrics/percentiles?hours=${this.timeRange}&interval_minutes=${this.intervalForRange()}`, requestOptions),
          fetch(`${rootPath}/admin/observability/metrics/top-errors?hours=${this.timeRange}&limit=10`, requestOptions),
          fetch(`${rootPath}/admin/observability/metrics/top-slow?hours=${this.timeRange}&limit=10`, requestOptions),
        ]);

        if (requestSequence !== this._requestSequence) return;

        const namedResponses = [
          ['Stats', responses[0]],
          ['Percentiles', responses[1]],
          ['Top errors', responses[2]],
          ['Top slow', responses[3]],
        ];
        const failed = namedResponses.find(([, response]) => !response.ok);
        if (failed) {
          throw new Error(`${failed[0]} endpoint returned ${failed[1].status}`);
        }

        const [statsData, pctData, errData, slowData] = await Promise.all(namedResponses.map(([, response]) => response.json()));
        if (requestSequence !== this._requestSequence) return;

        this.stats = statsData.stats || statsData;
        this.percentileData = pctData;
        this.topErrors = errData.endpoints || [];
        this.topSlow = slowData.endpoints || [];
      } catch (error) {
        // Promise.all rejects on the first failure. Abort sibling requests so
        // none can remain in flight after this refresh has finished.
        controller.abort();
        if (requestSequence !== this._requestSequence) return;

        let message;
        if (timedOut) {
          message = 'Request timed out after 15 seconds';
        } else if (error instanceof Error) {
          message = error.message;
        } else {
          message = String(error);
        }
        console.error('API Metrics Dashboard error:', error);
        this.error = `Failed to load metrics: ${message}`;
      } finally {
        globalThis.clearTimeout(timeoutId);
        if (requestSequence === this._requestSequence) {
          this.loading = false;
          this._requestController = null;
        }
      }
    },

    destroy() {
      ++this._requestSequence;
      if (this._requestController) {
        this._requestController.abort();
        this._requestController = null;
      }
      this.loading = false;
    },

    intervalForRange() {
      if (this.timeRange <= 1) return 5;
      if (this.timeRange <= 6) return 15;
      if (this.timeRange <= 24) return 60;
      return 360;
    },

    successRate() {
      if (!this.stats.total_traces) return 0;
      return (this.stats.success_count / this.stats.total_traces) * 100;
    },

    errorRate() {
      if (!this.stats.total_traces) return 0;
      return (this.stats.error_count / this.stats.total_traces) * 100;
    },

    successRateColor() {
      const rate = this.successRate();
      if (rate >= 99) return 'text-green-600 dark:text-green-400';
      if (rate >= 95) return 'text-yellow-600 dark:text-yellow-400';
      return 'text-red-600 dark:text-red-400';
    },

    errorRateColor() {
      const rate = this.errorRate();
      if (rate <= 1) return 'text-green-600 dark:text-green-400';
      if (rate <= 5) return 'text-yellow-600 dark:text-yellow-400';
      return 'text-red-600 dark:text-red-400';
    },

    latestPercentile(percentile) {
      const values = this.percentileData[`p${percentile}`];
      return Array.isArray(values) && values.length ? values[values.length - 1] : null;
    },

    timeRangeLabel() {
      if (this.timeRange <= 1) return 'last hour';
      if (this.timeRange <= 6) return 'last 6 hours';
      if (this.timeRange <= 24) return 'last 24 hours';
      return 'last 7 days';
    },

    fmtCount(value) {
      if (value == null) return '—';
      if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
      if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
      return String(value);
    },

    fmtPct(value) {
      if (value == null) return '—';
      return `${value.toFixed(1)}%`;
    },

    fmtMs(value) {
      if (value == null) return '—';
      if (value >= 1_000) return `${(value / 1_000).toFixed(1)}s`;
      return `${Math.round(value)} ms`;
    },

    truncateUrl(url, max) {
      if (!url) return '';
      return url.length > max ? `${url.substring(0, max)}…` : url;
    },

    methodBadge(method) {
      const normalizedMethod = (method || '').toUpperCase();
      if (normalizedMethod === 'GET') return 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400';
      if (normalizedMethod === 'POST') return 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400';
      if (normalizedMethod === 'PUT' || normalizedMethod === 'PATCH') return 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400';
      if (normalizedMethod === 'DELETE') return 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400';
      return 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300';
    },

    durationColor(milliseconds) {
      if (milliseconds == null) return 'text-gray-600 dark:text-gray-400';
      if (milliseconds >= 1_000) return 'text-red-600 dark:text-red-400';
      if (milliseconds >= 500) return 'text-yellow-600 dark:text-yellow-400';
      return 'text-green-600 dark:text-green-400';
    },
  };
}
