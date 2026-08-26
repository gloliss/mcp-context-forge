/**
 * @vitest-environment jsdom
 */

import Alpine from "@alpinejs/csp";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import {
  executeObservabilityScriptsAndStrip,
  observabilityDashboard,
} from "../../../mcpgateway/admin_ui/components/observability-dashboard.js";
import {
  afterAll,
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const TEMPLATE_DIR = path.resolve(__dirname, "../../../mcpgateway/templates");
const SUBVIEW_CONTROLLERS = [
  [
    "observability_metrics.html",
    "createMetricsController",
    "metrics-container",
  ],
  ["observability_tools.html", "createToolsController", "tools-container"],
  [
    "observability_prompts.html",
    "createPromptsController",
    "prompts-container",
  ],
  [
    "observability_resources.html",
    "createResourcesController",
    "resources-container",
  ],
];

function templateBody(name, marker) {
  const source = fs.readFileSync(path.join(TEMPLATE_DIR, name), "utf8");
  return source.slice(source.indexOf(marker));
}

function installTemplateController(name, factoryName) {
  const source = fs.readFileSync(path.join(TEMPLATE_DIR, name), "utf8");
  const script = source.match(/<script[^>]*>([\s\S]*?)<\/script>/);
  if (!script) throw new Error(`No controller script found in ${name}`);

  const body = script[1].replace(/\{\{[\s\S]*?\}\}/g, "");
  // eslint-disable-next-line no-new-func
  new Function(body)();
  return window[factoryName];
}

function response({ json, text, status = 200 }) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: vi.fn().mockResolvedValue(json),
    text: vi.fn().mockResolvedValue(text),
  };
}

function buttonWithText(text) {
  return Array.from(document.querySelectorAll("button")).find(
    (button) => button.textContent.trim() === text
  );
}

describe("Observability dashboard Alpine CSP integration", () => {
  let fetchMock;
  let intervalId;
  let subviewDestroySpies;

  beforeAll(() => {
    Alpine.data("observabilityDashboard", observabilityDashboard);
    window.Alpine = Alpine;
    Alpine.start();
  });

  beforeEach(async () => {
    intervalId = 0;
    subviewDestroySpies = {};
    const consoleError = vi
      .spyOn(console, "error")
      .mockImplementation(() => {});
    window.ROOT_PATH = "/forge";
    window.htmx = {
      ajax: vi.fn().mockResolvedValue({}),
    };
    vi.spyOn(window, "setInterval").mockImplementation(() => ++intervalId);
    vi.spyOn(window, "clearInterval").mockImplementation(() => {});
    vi.spyOn(window, "alert").mockImplementation(() => {});
    vi.spyOn(window, "confirm").mockReturnValue(true);

    const subviews = {
      metrics: ["createMetricsController", "observabilityMetrics"],
      tools: ["createToolsController", "observabilityTools"],
      prompts: ["createPromptsController", "observabilityPrompts"],
      resources: ["createResourcesController", "observabilityResources"],
    };
    Object.entries(subviews).forEach(([mode, [factoryName, providerName]]) => {
      subviewDestroySpies[mode] = vi.fn();
      window[factoryName] = () => ({
        initialized: false,
        init() {
          this.initialized = true;
        },
        destroy() {
          subviewDestroySpies[mode]();
        },
      });
      subviews[mode].markup =
        `<div data-testid="${mode}-subview" x-data="${providerName}"><span x-text="'loaded'"></span></div>`;
    });

    fetchMock = vi.fn().mockImplementation((url, options = {}) => {
      const requestUrl = String(url);
      if (
        requestUrl.endsWith("/admin/observability/queries") &&
        options.method === "POST"
      ) {
        return Promise.resolve(response({ json: { id: 9, name: "My query" } }));
      }
      if (requestUrl.endsWith("/admin/observability/queries")) {
        return Promise.resolve(response({ json: [] }));
      }
      const mode = Object.keys(subviews).find((name) =>
        requestUrl.endsWith(
          `/admin/observability/${name === "metrics" ? "metrics" : name}/partial`
        )
      );
      if (mode) {
        return Promise.resolve(response({ text: subviews[mode].markup }));
      }
      return Promise.resolve(response({ json: {} }));
    });
    vi.stubGlobal("fetch", fetchMock);

    Alpine.mutateDom(() => {
      document.body.innerHTML = templateBody(
        "observability_partial.html",
        '<div class="observability-container"'
      );
    });
    Alpine.initTree(document.querySelector(".observability-container"));
    await Alpine.nextTick();
    expect(
      document.querySelector(".observability-container")._x_dataStack
    ).toHaveLength(1);
    expect(
      document
        .querySelector(".observability-container")
        .contains(document.querySelector('[x-show="showSaveQueryModal"]'))
    ).toBe(true);
    expect(consoleError.mock.calls).toEqual([]);
  });

  afterEach(async () => {
    Alpine.destroyTree(document.body);
    Alpine.mutateDom(() => {
      document.body.innerHTML = "";
    });
    await Alpine.nextTick();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    delete window.ROOT_PATH;
    delete window.htmx;
    delete window.createMetricsController;
    delete window.createToolsController;
    delete window.createPromptsController;
    delete window.createResourcesController;
  });

  afterAll(() => {
    Alpine.stopObservingMutations();
    delete window.Alpine;
  });

  it("initializes polling and keeps the Save Query modal closed", async () => {
    await vi.waitFor(() => {
      expect(window.htmx.ajax).toHaveBeenCalledWith(
        "GET",
        expect.stringContaining("/admin/observability/traces?"),
        expect.objectContaining({ target: "#traces-list" })
      );
      expect(window.htmx.ajax).toHaveBeenCalledWith(
        "GET",
        "/forge/admin/observability/stats",
        expect.objectContaining({ target: "#stats-container" })
      );
    });

    const modal = document.querySelector('[x-show="showSaveQueryModal"]');
    expect(modal.style.display).toBe("none");
    expect(modal.hasAttribute("x-cloak")).toBe(false);
    expect(fetchMock).toHaveBeenCalledWith(
      "/forge/admin/observability/queries"
    );
  });

  it("stops polling when hidden and resumes it when revisited", async () => {
    const dashboard = document.querySelector(".observability-container")
      ._x_dataStack[0];
    expect(dashboard.tracesInterval).not.toBeNull();
    expect(dashboard.statsInterval).not.toBeNull();

    document.dispatchEvent(new CustomEvent("observability:leave"));
    await Alpine.nextTick();

    expect(dashboard.tracesInterval).toBeNull();
    expect(dashboard.statsInterval).toBeNull();
    expect(window.clearInterval).toHaveBeenCalledTimes(2);

    const requestsBeforeEnter = window.htmx.ajax.mock.calls.length;
    document.dispatchEvent(new CustomEvent("observability:enter"));
    await Alpine.nextTick();

    expect(dashboard.tracesInterval).not.toBeNull();
    expect(dashboard.statsInterval).not.toBeNull();
    expect(window.htmx.ajax.mock.calls.length).toBe(requestsBeforeEnter + 2);
  });

  it.each(["metrics", "tools", "prompts", "resources"])(
    "switches to and initializes the %s view",
    async (mode) => {
      const labels = {
        metrics: "📊 Advanced Metrics",
        tools: "🔧 MCP Tools",
        prompts: "💬 Prompts",
        resources: "📦 Resources",
      };
      buttonWithText(labels[mode]).click();

      await vi.waitFor(() => {
        expect(
          document.querySelector(".observability-container")._x_dataStack[0]
            .viewMode
        ).toBe(mode);
        expect(
          document.querySelector(`[data-testid="${mode}-subview"]`)
        ).not.toBeNull();
      });
    }
  );

  it("destroys the previous sub-view tree when leaving and re-entering", async () => {
    buttonWithText("📊 Advanced Metrics").click();

    await vi.waitFor(() => {
      expect(
        document.querySelector('[data-testid="metrics-subview"]')
      ).not.toBeNull();
    });
    const firstSubview = document.querySelector(
      '[data-testid="metrics-subview"]'
    );

    document.dispatchEvent(new CustomEvent("observability:leave"));
    await Alpine.nextTick();
    document.dispatchEvent(new CustomEvent("observability:enter"));
    await Alpine.nextTick();
    buttonWithText("📊 Advanced Metrics").click();

    await vi.waitFor(() => {
      const currentSubview = document.querySelector(
        '[data-testid="metrics-subview"]'
      );
      expect(currentSubview).not.toBe(firstSubview);
      expect(subviewDestroySpies.metrics).toHaveBeenCalledTimes(1);
    });

    expect(firstSubview.isConnected).toBe(false);
    expect(
      fetchMock.mock.calls.filter(([url]) =>
        String(url).endsWith("/admin/observability/metrics/partial")
      )
    ).toHaveLength(2);
  });

  it("submits a saved query and closes the modal", async () => {
    document
      .querySelector('button[title="Save current filters as a query"]')
      .click();
    expect(
      document.querySelector(".observability-container")._x_dataStack[0]
        .showSaveQueryModal
    ).toBe(true);

    const name = document.querySelector('[x-model="saveQueryName"]');
    name.value = "  My query  ";
    name.dispatchEvent(new Event("input", { bubbles: true }));
    document.getElementById("save-query-shared").click();
    buttonWithText("Save Query").click();

    await vi.waitFor(() => {
      const saveCall = fetchMock.mock.calls.find(
        ([url, options]) =>
          String(url).endsWith("/admin/observability/queries") &&
          options?.method === "POST"
      );
      expect(saveCall).toBeTruthy();
      expect(JSON.parse(saveCall[1].body)).toMatchObject({
        name: "My query",
        is_shared: true,
        filter_config: { timeRange: "24h", statusFilter: "all" },
      });
      expect(
        document.querySelector(".observability-container")._x_dataStack[0]
          .showSaveQueryModal
      ).toBe(false);
    });
    expect(window.alert).toHaveBeenCalledWith("Query saved successfully!");
  });
});

describe("Observability dynamic partial helpers", () => {
  afterEach(() => vi.restoreAllMocks());

  it("strips inline scripts and applies the active CSP nonce", () => {
    window.htmxConfig = { inlineScriptNonce: "nonce-value" };
    const append = vi
      .spyOn(document.head, "appendChild")
      .mockImplementation((node) => node);

    const clean = executeObservabilityScriptsAndStrip(
      "<script>window.exampleController = function () {};</script><div>content</div>"
    );

    expect(clean).toBe("<div>content</div>");
    expect(append).toHaveBeenCalledTimes(1);
    expect(append.mock.calls[0][0].nonce).toBe("nonce-value");
    delete window.htmxConfig;
  });

  it("keeps every sub-view directive compatible with Alpine CSP", async () => {
    const subviews = [
      [
        "observability_metrics.html",
        "metrics-dashboard",
        "observabilityMetrics",
      ],
      ["observability_tools.html", "tools-dashboard", "observabilityTools"],
      [
        "observability_prompts.html",
        "prompts-dashboard",
        "observabilityPrompts",
      ],
      [
        "observability_resources.html",
        "resources-dashboard",
        "observabilityResources",
      ],
    ];
    const consoleError = vi
      .spyOn(console, "error")
      .mockImplementation(() => {});
    const consoleWarn = vi.spyOn(console, "warn").mockImplementation(() => {});

    for (const [name, className, providerName] of subviews) {
      const source = templateBody(name, `<div class="${className}"`);
      expect(source).not.toContain("?.");
      Alpine.data(providerName, () => ({
        loading: false,
        error: null,
        timeRange: 24,
        interval: 60,
        limit: 10,
        summaryCards: {
          slowest: null,
          mostErrorProne: null,
          mostUsed: null,
          overallHealth: "good",
        },
        init() {},
        cleanup() {},
        applyFilters() {},
      }));

      const host = document.createElement("div");
      document.body.appendChild(host);
      Alpine.mutateDom(() => {
        host.innerHTML = source;
      });
      Alpine.initTree(host);
      await Alpine.nextTick();
      Alpine.destroyTree(host);
      host.remove();
    }

    expect(
      consoleError.mock.calls.some(([message]) =>
        String(message).includes("CSP Parser Error")
      )
    ).toBe(false);
    expect(
      consoleWarn.mock.calls.some(([message]) =>
        String(message).includes("CSP Parser Error")
      )
    ).toBe(false);
  });
});

describe("Observability sub-view controller lifecycle", () => {
  afterEach(() => {
    document.body.innerHTML = "";
    vi.restoreAllMocks();
    delete window.Admin;
    SUBVIEW_CONTROLLERS.forEach(([, factoryName]) => {
      delete window[factoryName];
    });
  });

  it.each(SUBVIEW_CONTROLLERS)(
    "cleans up %s when leaving during the initial request",
    async (templateName, factoryName, containerId) => {
      document.body.innerHTML = `
        <div id="observability-panel">
          <div id="${containerId}"></div>
        </div>
      `;
      window.Admin = { chartRegistry: { destroyByPrefix: vi.fn() } };
      const factory = installTemplateController(templateName, factoryName);
      const controller = factory();
      let finishInitialLoad;
      const initialLoad = new Promise((resolve) => {
        finishInitialLoad = resolve;
      });

      controller.destroyAllCharts = vi.fn();
      controller.loadAllMetrics = vi.fn(() => initialLoad);
      controller.startAutoRefresh = vi.fn();
      controller.stopAutoRefresh = vi.fn();
      const cleanup = vi.spyOn(controller, "cleanup");
      const initializing = controller.init();

      document.dispatchEvent(new CustomEvent("observability:leave"));
      document.getElementById("observability-panel").classList.add("hidden");
      finishInitialLoad();
      await initializing;

      expect(cleanup).toHaveBeenCalledTimes(1);
      expect(controller.startAutoRefresh).not.toHaveBeenCalled();
      expect(controller.leaveHandler).toBeNull();
      expect(controller.beforeUnloadHandler).toBeNull();
    }
  );

  it.each(SUBVIEW_CONTROLLERS)(
    "does not start %s polling after its sub-view becomes inactive",
    async (templateName, factoryName, containerId) => {
      document.body.innerHTML = `
        <div id="observability-panel">
          <div id="${containerId}"></div>
        </div>
      `;
      window.Admin = { chartRegistry: { destroyByPrefix: vi.fn() } };
      const factory = installTemplateController(templateName, factoryName);
      const controller = factory();
      let finishInitialLoad;
      const initialLoad = new Promise((resolve) => {
        finishInitialLoad = resolve;
      });

      controller.destroyAllCharts = vi.fn();
      controller.loadAllMetrics = vi.fn(() => initialLoad);
      controller.startAutoRefresh = vi.fn();
      controller.stopAutoRefresh = vi.fn();
      const initializing = controller.init();

      document.getElementById(containerId).style.display = "none";
      finishInitialLoad();
      await initializing;

      expect(controller.startAutoRefresh).not.toHaveBeenCalled();
      controller.destroy();
    }
  );
});
