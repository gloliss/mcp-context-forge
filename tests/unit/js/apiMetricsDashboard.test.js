/**
 * @vitest-environment jsdom
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

import { apiMetricsDashboard } from "../../../mcpgateway/admin_ui/components/api-metrics-dashboard.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const TEMPLATE_PATH = path.resolve(
  __dirname,
  "../../../mcpgateway/templates/api_metrics_dashboard.html",
);
const ALPINE_SETUP_PATH = path.resolve(
  __dirname,
  "../../../mcpgateway/admin_ui/alpine-setup.js",
);

function templateSource() {
  return fs.readFileSync(TEMPLATE_PATH, "utf8");
}

function jsonResponse(payload, { ok = true, status = 200 } = {}) {
  return {
    ok,
    status,
    json: vi.fn().mockResolvedValue(payload),
  };
}

function successfulResponses(totalTraces = 20) {
  return [
    jsonResponse({
      total_traces: totalTraces,
      success_count: totalTraces - 2,
      error_count: 2,
      avg_duration_ms: 45,
    }),
    jsonResponse({
      timestamps: ["2026-08-18T10:00:00Z", "2026-08-18T11:00:00Z"],
      p50: [10, 12],
      p90: [20, 22],
      p95: [30, 34],
      p99: [40, 45],
    }),
    jsonResponse({ endpoints: [] }),
    jsonResponse({ endpoints: [] }),
  ];
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

describe("API Metrics dashboard", () => {
  beforeEach(() => {
    window.ROOT_PATH = "/forge";
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    delete window.ROOT_PATH;
    document.body.innerHTML = "";
  });

  it("registers a CSP-safe Alpine provider and removes the inline controller", () => {
    const markup = templateSource();
    const alpineSetup = fs.readFileSync(ALPINE_SETUP_PATH, "utf8");

    expect(markup).toContain('x-data="apiMetricsDashboard"');
    expect(markup).not.toContain('x-data="apiMetricsDashboard()"');
    expect(markup).not.toContain("x-init=");
    expect(markup).not.toContain("<script");
    expect(alpineSetup).toContain("Alpine.data('apiMetricsDashboard', apiMetricsDashboard);");
  });

  it("shows an explicit disabled-state notice with no history backfill promise", () => {
    const markup = templateSource();

    expect(markup).toContain("{% if observability_enabled and trace_http_requests and observability_sample_rate > 0 %}");
    expect(markup).toContain("API Metrics are disabled");
    expect(markup).toContain("OBSERVABILITY_ENABLED=true");
    expect(markup).toContain("API request tracing is disabled");
    expect(markup).toContain("OBSERVABILITY_TRACE_HTTP_REQUESTS=true");
    expect(markup).toContain("API trace sampling is disabled");
    expect(markup).toContain("OBSERVABILITY_SAMPLE_RATE");
    expect(markup).toContain("earlier requests are not backfilled");
  });

  it("formats dashboard values", () => {
    const dashboard = apiMetricsDashboard();

    expect(dashboard.fmtCount(1250)).toBe("1.3K");
    expect(dashboard.fmtPct(12.34)).toBe("12.3%");
    expect(dashboard.fmtMs(1200)).toBe("1.2s");
    expect(dashboard.methodBadge("get")).toContain("bg-green-100");
  });

  it("init makes exactly four requests and consumes each endpoint contract", async () => {
    const [stats, percentiles, errors, slow] = successfulResponses();
    errors.json.mockResolvedValue({
      endpoints: [
        {
          endpoint: "GET /failed",
          method: "GET",
          url: "/failed",
          total_count: 4,
          error_count: 2,
          error_rate: 50,
        },
      ],
    });
    slow.json.mockResolvedValue({
      endpoints: [
        {
          endpoint: "POST /slow",
          method: "POST",
          url: "/slow",
          count: 3,
          avg_duration_ms: 750,
          max_duration_ms: 1200,
        },
      ],
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(stats)
      .mockResolvedValueOnce(percentiles)
      .mockResolvedValueOnce(errors)
      .mockResolvedValueOnce(slow);
    vi.stubGlobal("fetch", fetchMock);

    const dashboard = apiMetricsDashboard();
    await dashboard.init();

    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/forge/admin/observability/stats?hours=24",
      { signal: expect.any(AbortSignal) },
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/forge/admin/observability/metrics/percentiles?hours=24&interval_minutes=60",
      { signal: expect.any(AbortSignal) },
    );
    const requestSignals = fetchMock.mock.calls.map(([, options]) => options.signal);
    expect(new Set(requestSignals).size).toBe(1);
    expect(dashboard.stats.total_traces).toBe(20);
    expect(dashboard.successRate()).toBe(90);
    expect(dashboard.latestPercentile(95)).toBe(34);
    expect(dashboard.latestPercentile(99)).toBe(45);
    expect(dashboard.topErrors).toHaveLength(1);
    expect(dashboard.topSlow).toHaveLength(1);
    expect(dashboard.error).toBeNull();
    expect(dashboard.loading).toBe(false);
  });

  it("shows a useful error and exits loading when an endpoint fails", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({}))
      .mockResolvedValueOnce(jsonResponse({}, { ok: false, status: 503 }))
      .mockResolvedValueOnce(jsonResponse({ endpoints: [] }))
      .mockResolvedValueOnce(jsonResponse({ endpoints: [] }));
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(console, "error").mockImplementation(() => {});

    const dashboard = apiMetricsDashboard();
    await dashboard.loadAll();

    expect(dashboard.error).toBe(
      "Failed to load metrics: Percentiles endpoint returned 503",
    );
    expect(dashboard.loading).toBe(false);
  });

  it("aborts sibling requests when one request rejects early", async () => {
    const failure = deferred();
    const siblings = Array.from({ length: 3 }, () => deferred());
    const fetchMock = vi
      .fn()
      .mockReturnValueOnce(failure.promise)
      .mockReturnValueOnce(siblings[0].promise)
      .mockReturnValueOnce(siblings[1].promise)
      .mockReturnValueOnce(siblings[2].promise);
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(console, "error").mockImplementation(() => {});

    const dashboard = apiMetricsDashboard();
    const loading = dashboard.loadAll();
    const signal = fetchMock.mock.calls[0][1].signal;
    failure.reject(new TypeError("Network unavailable"));
    await loading;

    expect(signal.aborted).toBe(true);
    expect(dashboard.error).toBe("Failed to load metrics: Network unavailable");
    expect(dashboard.loading).toBe(false);
  });

  it("aborts the previous refresh and ignores its stale response", async () => {
    const oldRequests = Array.from({ length: 4 }, () => deferred());
    const newResponses = successfulResponses(40);
    const fetchMock = vi.fn();
    oldRequests.forEach(({ promise }) => fetchMock.mockReturnValueOnce(promise));
    newResponses.forEach((response) => fetchMock.mockResolvedValueOnce(response));
    vi.stubGlobal("fetch", fetchMock);

    const dashboard = apiMetricsDashboard();
    const oldLoad = dashboard.loadAll();
    const oldSignal = fetchMock.mock.calls[0][1].signal;
    const newLoad = dashboard.loadAll();

    expect(oldSignal.aborted).toBe(true);
    await newLoad;
    expect(dashboard.stats.total_traces).toBe(40);
    expect(dashboard.loading).toBe(false);

    successfulResponses(5).forEach((response, index) => {
      oldRequests[index].resolve(response);
    });
    await oldLoad;

    expect(dashboard.stats.total_traces).toBe(40);
    expect(dashboard.error).toBeNull();
    expect(dashboard.loading).toBe(false);
  });

  it("aborts after 15 seconds, reports a timeout, and exits loading", async () => {
    vi.useFakeTimers();
    vi.spyOn(console, "error").mockImplementation(() => {});
    const fetchMock = vi.fn((...args) => {
      const signal = args[1].signal;
      const request = deferred();
      signal.addEventListener("abort", () => {
        request.reject(new DOMException("The operation was aborted", "AbortError"));
      }, { once: true });
      return request.promise;
    });
    vi.stubGlobal("fetch", fetchMock);

    const dashboard = apiMetricsDashboard();
    const loading = dashboard.loadAll();
    await vi.advanceTimersByTimeAsync(15_000);
    await loading;

    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(dashboard.error).toBe(
      "Failed to load metrics: Request timed out after 15 seconds",
    );
    expect(dashboard.loading).toBe(false);
  });

  it("destroy aborts in-flight requests and clears loading state", async () => {
    const fetchMock = vi.fn((...args) => {
      const signal = args[1].signal;
      const request = deferred();
      signal.addEventListener("abort", () => {
        request.reject(new DOMException("The operation was aborted", "AbortError"));
      }, { once: true });
      return request.promise;
    });
    vi.stubGlobal("fetch", fetchMock);
    const dashboard = apiMetricsDashboard();

    const loading = dashboard.loadAll();
    const signal = fetchMock.mock.calls[0][1].signal;
    dashboard.destroy();
    await loading;

    expect(signal.aborted).toBe(true);
    expect(dashboard.loading).toBe(false);
    expect(dashboard._requestController).toBeNull();
  });

  it("keeps Alpine error-rate expressions valid JavaScript", () => {
    document.body.innerHTML = templateSource();

    const errorRows = document.querySelector('template[x-for="ep in topErrors"]');
    const rateText = errorRows.content.querySelector('[x-text*="error_rate"]');
    const textExpression = rateText.getAttribute("x-text");
    const classExpression = rateText.getAttribute(":class");

    // eslint-disable-next-line no-new-func
    expect(new Function("ep", `return ${textExpression};`)({ error_rate: 60 })).toBe(
      "60%",
    );
    // eslint-disable-next-line no-new-func
    expect(new Function("ep", `return ${classExpression};`)({ error_rate: 60 })).toContain(
      "bg-red-100",
    );
  });
});
