/**
 * Regression tests for the Admin UI icon and motion system.
 *
 * The sidebar used platform-dependent emoji before these checks were added.
 * Keep navigation and runtime branding on the bundled vector icon set so the
 * layout stays consistent in browsers, containers, and air-gapped installs.
 */

import { describe, expect, test } from "vitest";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, "../../..");

function read(relativePath) {
  return fs.readFileSync(path.join(repoRoot, relativePath), "utf8");
}

const adminHtml = read("mcpgateway/templates/admin.html");
const overviewHtml = read("mcpgateway/templates/overview_partial.html");
const versionHtml = read("mcpgateway/templates/version_info_partial.html");
const adminCss = read("mcpgateway/static/admin.css");

describe("Admin UI vector icon system", () => {
  test("every sidebar link has one decorative Font Awesome icon", () => {
    const sidebar = adminHtml.match(
      /<!-- Sidebar Navigation -->([\s\S]*?)<!-- Sidebar Footer -->/
    )?.[1];

    expect(sidebar).toBeDefined();
    const linkCount = (sidebar.match(/class="sidebar-link\b/g) || []).length;
    const iconCount = (sidebar.match(/class="sidebar-icon"/g) || []).length;
    expect(linkCount).toBeGreaterThan(20);
    expect(iconCount).toBe(linkCount);
    expect(sidebar).not.toMatch(/[📊🖥️🔗🛠️⚙️💬📁🌳📦🤖🔌🌐🗄️🧪👨‍💻⚡🔍📈🧩🗃️👥👤🎫📤📋ℹ️🔧]/u);
    expect((sidebar.match(/aria-hidden="true"><i class="fa-/g) || []).length).toBe(
      linkCount
    );
  });

  test("Python and Rust runtime labels use bundled brand icons", () => {
    for (const template of [overviewHtml, versionHtml]) {
      expect(template).toContain("fa-brands fa-python");
      expect(template).toContain("fa-brands fa-rust");
      expect(template).not.toMatch(/[🐍🦀]/u);
    }
  });
});

describe("Admin UI motion accessibility", () => {
  test("motion effects include a reduced-motion fallback", () => {
    expect(adminCss).toContain("@keyframes cf-panel-enter");
    expect(adminCss).toContain("@keyframes cf-icon-pop");
    expect(adminCss).toContain("@keyframes cf-runtime-breathe");
    expect(adminCss).toContain("@media (prefers-reduced-motion: reduce)");
    expect(adminCss).toMatch(/\.runtime-brand-mark--hero,[\s\S]*animation:\s*none;/);
  });
});
