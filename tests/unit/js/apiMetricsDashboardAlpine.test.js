/**
 * @vitest-environment jsdom
 *
 * Integration regression for an API Metrics partial inserted after Alpine CSP
 * has already started, matching the HTMX dashboard-loading lifecycle.
 */

import Alpine from "@alpinejs/csp";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
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

function enabledTemplateMarkup() {
  const template = fs.readFileSync(TEMPLATE_PATH, "utf8");
  return template.split("{% if observability_enabled and trace_http_requests and observability_sample_rate > 0 %}", 2)[1].split("{% elif", 1)[0];
}

function jsonResponse(payload) {
  return {
    ok: true,
    status: 200,
    json: vi.fn().mockResolvedValue(payload),
  };
}

describe("API Metrics dashboard Alpine CSP integration", () => {
  beforeAll(() => {
    Alpine.data("apiMetricsDashboard", apiMetricsDashboard);
    Alpine.start();
  });

  beforeEach(() => {
    window.ROOT_PATH = "/forge";
    document.body.innerHTML = '<div id="api-metrics-panel"></div>';
  });

  afterEach(async () => {
    document.body.innerHTML = "";
    await Alpine.nextTick();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    delete window.ROOT_PATH;
  });

  afterAll(() => {
    Alpine.stopObservingMutations();
  });

  it("initializes a dynamically inserted provider once and makes four requests", async () => {
    const responses = [
      jsonResponse({ total_traces: 7, success_count: 7, error_count: 0 }),
      jsonResponse({ timestamps: [], p50: [], p90: [], p95: [], p99: [] }),
      jsonResponse({ endpoints: [] }),
      jsonResponse({ endpoints: [] }),
    ];
    const fetchMock = vi.fn();
    responses.forEach((response) => fetchMock.mockResolvedValueOnce(response));
    vi.stubGlobal("fetch", fetchMock);

    document.getElementById("api-metrics-panel").innerHTML = enabledTemplateMarkup();

    await vi.waitFor(() => {
      expect(document.querySelector('[x-text="fmtCount(stats.total_traces)"]').textContent).toBe("7");
      expect(document.querySelector('[x-show="loading"]').style.display).toBe("none");
    });

    expect(fetchMock).toHaveBeenCalledTimes(4);
  });
});
