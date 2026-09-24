/**
 * Unit tests for the admin UI theme tokens.
 *
 * These guard the two invariants documented at the top of `tailwind.config.js`:
 *
 *   1. `gray` / `indigo` are declared through `theme.extend.colors`, and each
 *      ramp is complete (50-950). A partial ramp declared through `theme.colors`
 *      would silently stop generating the utility classes for the omitted
 *      shades, with no build error.
 *   2. Every value in those ramps is mirrored as a CSS custom property in
 *      `mcpgateway/static/admin.css` (`:root`, `--cf-gray-*` / `--cf-indigo-*`),
 *      which the hand-written rules (sidebar, chat bubbles, markdown body,
 *      scrollbars) consume through `var()`. The two must not drift.
 *
 * Plus a contrast floor for the primary action colours: a solid indigo button
 * is white-on-indigo, so indigo-500/600 must stay at or above WCAG AA (4.5:1).
 * The non-gray/indigo families are asserted to be untouched, because red /
 * green / yellow / amber / orange carry error / success / warning meaning here.
 */

import { describe, test, expect } from "vitest";
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, "../../..");
const require = createRequire(import.meta.url);

const config = require(path.join(repoRoot, "tailwind.config.js"));
const adminCss = fs.readFileSync(
  path.join(repoRoot, "mcpgateway/static/admin.css"),
  "utf8"
);

const SHADES = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950];
const FAMILIES = ["gray", "indigo"];

// --- tiny colour helpers (WCAG 2.x relative luminance) ---------------------

function toRgb(value) {
  const hex = value.trim().replace(/^#/, "");
  if (!/^[0-9a-fA-F]{6}$/.test(hex)) return null;
  return [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16));
}

function luminance(value) {
  const rgb = toRgb(value);
  return (
    0.2126 * channel(rgb[0]) + 0.7152 * channel(rgb[1]) + 0.0722 * channel(rgb[2])
  );
}

function channel(c) {
  const s = c / 255;
  return s <= 0.04045 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
}

function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/** Parse the `:root` token block out of admin.css into { "gray-700": "#344054" }. */
function readTokens(css) {
  const tokens = {};
  const re = /--cf-(gray|indigo)-(\d+):\s*(\d+)\s+(\d+)\s+(\d+)\s*;/g;
  let match;
  while ((match = re.exec(css)) !== null) {
    const hex =
      "#" +
      [match[3], match[4], match[5]]
        .map((n) => Number(n).toString(16).padStart(2, "0"))
        .join("");
    tokens[`${match[1]}-${match[2]}`] = hex;
  }
  return tokens;
}

const tokens = readTokens(adminCss);

// --- tests -----------------------------------------------------------------

describe("tailwind.config.js theme", () => {
  test("gray and indigo are extended, not replaced", () => {
    // Replacing (`theme.colors.gray`) would drop every shade not listed, so no
    // `gray-*` utility beyond that list would be generated at all.
    expect(config.theme.colors).toBeUndefined();
    for (const family of FAMILIES) {
      expect(config.theme.extend.colors[family]).toBeTypeOf("object");
    }
  });

  test("semantic colour families are left at Tailwind's defaults", () => {
    const families = Object.keys(config.theme.extend.colors);
    for (const family of [
      "red",
      "green",
      "yellow",
      "amber",
      "orange",
      "blue",
      "purple",
      "teal",
      "cyan",
      "pink",
      "rose",
      "emerald",
    ]) {
      expect(families).not.toContain(family);
    }
  });

  test.each(FAMILIES)("%s ramp is complete 50-950", (family) => {
    const ramp = config.theme.extend.colors[family];
    expect(Object.keys(ramp).map(Number).sort((a, b) => a - b)).toEqual(SHADES);
    for (const shade of SHADES) {
      expect(toRgb(ramp[shade]), `${family}-${shade} is not a hex colour`).not.toBeNull();
    }
  });

  test.each(FAMILIES)("%s ramp gets monotonically darker 50 -> 950", (family) => {
    const ramp = config.theme.extend.colors[family];
    for (let i = 1; i < SHADES.length; i++) {
      const lighter = luminance(ramp[SHADES[i - 1]]);
      const darker = luminance(ramp[SHADES[i]]);
      expect(
        darker,
        `${family}-${SHADES[i - 1]} should be lighter than ${family}-${SHADES[i]}`
      ).toBeLessThan(lighter);
    }
  });

  test("indigo 500/600 keep white text at WCAG AA for solid buttons", () => {
    expect(contrast("#ffffff", config.theme.extend.colors.indigo[500])).toBeGreaterThanOrEqual(4.5);
    expect(contrast("#ffffff", config.theme.extend.colors.indigo[600])).toBeGreaterThanOrEqual(4.5);
  });

  test("sans stack keeps the Latin faces first and adds CJK faces before the generic", () => {
    const sans = config.theme.extend.fontFamily.sans;
    // Latin faces must stay byte-identical to Tailwind's default stack, and
    // must come first, so Latin text renders in exactly the same face as before.
    expect(sans.slice(0, 9)).toEqual([
      "ui-sans-serif",
      "system-ui",
      "-apple-system",
      "BlinkMacSystemFont",
      '"Segoe UI"',
      "Roboto",
      '"Helvetica Neue"',
      "Arial",
      '"Noto Sans"',
    ]);
    // System CJK faces only: the gateway ships to offline RHEL, so no webfont
    // (and no remote @font-face) may be introduced.
    for (const face of [
      '"PingFang SC"',
      '"Hiragino Sans GB"',
      '"Microsoft YaHei"',
      '"Source Han Sans SC"',
      '"Noto Sans CJK SC"',
      '"WenQuanYi Micro Hei"',
    ]) {
      expect(sans).toContain(face);
    }
    // CJK faces resolve before the generic, and the emoji tail is unchanged.
    expect(sans.indexOf('"PingFang SC"')).toBeLessThan(sans.indexOf("sans-serif"));
    expect(sans.slice(-4)).toEqual([
      '"Apple Color Emoji"',
      '"Segoe UI Emoji"',
      '"Segoe UI Symbol"',
      '"Noto Color Emoji"',
    ]);
  });
});

describe("admin.css theme tokens", () => {
  test.each(FAMILIES)(":root mirrors the tailwind %s ramp exactly", (family) => {
    for (const shade of SHADES) {
      expect(
        tokens[`${family}-${shade}`],
        `--cf-${family}-${shade} is missing from admin.css :root`
      ).toBeDefined();
      expect(tokens[`${family}-${shade}`]).toBe(
        config.theme.extend.colors[family][shade].toLowerCase()
      );
    }
  });

  test("no Tailwind-default gray/indigo literal is left outside the token block", () => {
    // Before this change the sidebar and chat rules spelled out raw Tailwind
    // defaults (`rgb(75 85 99)`, `#9ca3af`, ...). Those must all be gone: the
    // palette is defined once, in :root, and every hand-written rule consumes
    // it through var(...), so a leftover literal would silently desync from the
    // config the first time a shade is retuned.
    const withoutTokens = adminCss.replace(/--cf-[a-z]+-\d+:[^;]+;\s*(\/\*[^*]*\*\/)?/g, "");

    const DEFAULTS = {
      gray: ["f9fafb", "f3f4f6", "e5e7eb", "d1d5db", "9ca3af", "6b7280", "4b5563", "374151", "1f2937", "111827", "030712"],
      indigo: ["eef2ff", "e0e7ff", "c7d2fe", "a5b4fc", "818cf8", "6366f1", "4f46e5", "4338ca", "3730a3", "312e81", "1e1b4b"],
    };

    for (const [family, hexes] of Object.entries(DEFAULTS)) {
      for (const hex of hexes) {
        expect(
          withoutTokens.toLowerCase(),
          `found Tailwind-default ${family} literal #${hex} outside the :root token block`
        ).not.toContain(`#${hex}`);
        const [r, g, b] = toRgb(`#${hex}`);
        // ...and the same value in the space-separated RGB form var() consumes,
        // e.g. `rgb(75 85 99)` / `rgb(75 85 99 / 40%)`.
        expect(
          withoutTokens,
          `found Tailwind-default ${family} literal rgb(${r} ${g} ${b}) outside the :root token block`
        ).not.toContain(`rgb(${r} ${g} ${b}`);
      }
    }
  });
});
