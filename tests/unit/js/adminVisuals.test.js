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
const utilsJs = read("mcpgateway/admin_ui/utils.js");

/**
 * Emoji that render as a colour pictograph rather than a font glyph.
 *
 * Deliberately excludes the typographic ranges the UI legitimately uses —
 * arrows (`→`), dashes, ellipsis, middle dot, bullet — so the guard does not
 * fire on prose punctuation.
 */
const EMOJI =
  /[\u{1F000}-\u{1FAFF}\u{2300}-\u{23FF}\u{25B6}\u{25C0}\u{25B2}\u{25BC}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{3030}\u{303D}\u{FE0F}]/u;

/**
 * Blank the two places emoji are allowed: comments and `console.*` output.
 *
 * A scanner rather than a parser — but it still has to tell a comment from a
 * regex from a string, and two constructs here defeat the obvious version. A
 * regex literal such as `/filename="?([^";\n]+)"?/i` carries three quote
 * characters, and a template literal nested inside another's `${}` closes
 * early; either one derails a scanner that only tracks quotes, after which it
 * reads the rest of the file as string content and stops exempting log lines.
 * Both cause the same visible symptom: `✓` in a `console.log` reported as a
 * defect, plus line numbers that drift by however many newlines were dropped.
 *
 * Blanked characters become spaces and newlines survive, so a reported line
 * number is the real line of the real file.
 */
function stripCommentsAndConsole(code) {
  const out = code.split("");
  const n = code.length;
  // A `/` opens a regex only where an expression may begin, so after a word it
  // divides — unless that word is one of these.
  const KEYWORD =
    /^(?:await|case|delete|do|else|in|instanceof|new|of|return|throw|typeof|void|yield)$/;
  // Characters that can end a value: after one, `/` divides.
  const VALUE_END = /[\w$\])"'`}]/;

  const blank = (from, to) => {
    for (let k = from; k < to; k += 1) {
      if (out[k] !== "\n") out[k] = " ";
    }
  };

  // Blank a whole `console.*( … )` call, arguments included, so a multi-line
  // log does not leave a stray glyph behind.
  const skipConsoleCall = (from) => {
    const open = code.indexOf("(", from);
    if (open === -1) {
      return n;
    }
    let depth = 0;
    let inner = "code";
    for (let j = open; j < n; j += 1) {
      const ch = code[j];
      if (inner === "code") {
        if (ch === '"') inner = "double";
        else if (ch === "'") inner = "single";
        else if (ch === "`") inner = "template";
        else if (ch === "(") depth += 1;
        else if (ch === ")" && (depth -= 1) === 0) return j + 1;
      } else if (ch === "\\") {
        j += 1;
      } else if (
        (inner === "double" && ch === '"') ||
        (inner === "single" && ch === "'") ||
        (inner === "template" && ch === "`")
      ) {
        inner = "code";
      }
    }
    return n;
  };

  let i = 0;
  let state = "code";
  let last = ""; // last non-space character seen in code context
  let word = ""; // identifier or keyword ending at the current position
  let braces = 0; // `{`…`}` depth within the current code context
  let inClass = false; // inside a regex `[...]`
  const templates = []; // brace depth to restore at each open template literal

  while (i < n) {
    const ch = code[i];
    const pair = code.slice(i, i + 2);

    if (state === "regex") {
      if (ch === "\\") {
        i += 2;
      } else {
        if (ch === "[") inClass = true;
        else if (ch === "]") inClass = false;
        else if (ch === "/" && !inClass) {
          state = "code";
          last = ")"; // the literal is a value, so the next `/` divides
          word = "";
        }
        i += 1;
      }
    } else if (state !== "code") {
      // A string or template literal holds rendered copy, so its text stays.
      const closer = state === "double" ? '"' : state === "single" ? "'" : "`";
      if (ch === "\\") {
        i += 2;
      } else if (ch === closer) {
        if (state === "template") {
          braces = templates.pop();
        }
        state = "code";
        last = closer;
        word = "";
        i += 1;
      } else if (state === "template" && pair === "${") {
        braces = 0; // the substitution has its own brace context
        state = "code";
        i += 2;
      } else {
        i += 1;
      }
    } else if (pair === "//") {
      const end = code.indexOf("\n", i);
      const stop = end === -1 ? n : end;
      blank(i, stop);
      i = stop;
    } else if (pair === "/*") {
      const end = code.indexOf("*/", i + 2);
      const stop = end === -1 ? n : end + 2;
      blank(i, stop);
      i = stop;
    } else if (
      code.startsWith("console.", i) &&
      !/[\w$.]/.test(code[i - 1] || "")
    ) {
      const end = skipConsoleCall(i);
      blank(i, end);
      last = ")";
      word = "";
      i = end;
    } else if (ch === "/" && (!VALUE_END.test(last) || KEYWORD.test(word))) {
      inClass = false;
      state = "regex";
      i += 1;
    } else if (ch === '"' || ch === "'") {
      state = ch === '"' ? "double" : "single";
      i += 1;
    } else if (ch === "`") {
      templates.push(braces);
      state = "template";
      i += 1;
    } else if (ch === "{") {
      braces += 1;
      last = ch;
      word = "";
      i += 1;
    } else if (ch === "}" && braces > 0) {
      braces -= 1;
      last = ch;
      word = "";
      i += 1;
    } else if (ch === "}" && templates.length > 0) {
      // Closes the `${ … }` of the innermost open template literal.
      state = "template";
      i += 1;
    } else {
      if (!/\s/.test(ch)) {
        last = ch;
        word = /[\w$]/.test(ch) ? word + ch : "";
      }
      i += 1;
    }
  }
  return out.join("");
}

/**
 * A template's inline scripts are JavaScript; everything around them is markup,
 * where a `//` is a URL rather than a comment. Scanning the two together would
 * let `https://…` blank the rest of its line, hiding whatever follows it, so
 * the script bodies are exempted from the markup pass and scanned as code.
 */
function stripHtml(html) {
  return html
    .replace(/<!--[\s\S]*?-->/g, (comment) => comment.replace(/[^\n]/g, " "))
    .replace(
      /(<script\b[^>]*>)([\s\S]*?)(<\/script>)/gi,
      (_whole, open, body, close) =>
        open + stripCommentsAndConsole(body) + close
    );
}

function offendersIn(text, label) {
  return stripCommentsAndConsole(text)
    .split("\n")
    .map((line, index) => [line.trim(), index + 1])
    .filter(([line]) => EMOJI.test(line))
    .map(([line, number]) => `${label}:${number}: ${line}`);
}

function listFiles(dir, extension) {
  const found = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      found.push(...listFiles(full, extension));
    } else if (entry.name.endsWith(extension)) {
      found.push(full);
    }
  }
  return found;
}

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

describe("Admin UI has no emoji in rendered copy", () => {
  test("the scanner reads regex literals and nested templates correctly", () => {
    const sample = [
      'const m = name.match(/filename="?([^";\\n]+)"?/i);',
      'console.log("✓ logged, never rendered");',
      "const n = `outer ${list.map((x) => `inner ${x}`).join(\"\")} tail`;",
      'el.textContent = "😀 rendered";',
    ].join("\n");

    const lines = stripCommentsAndConsole(sample).split("\n");

    expect(lines).toHaveLength(4);
    // Three quotes live inside that regex; a scanner that miscounts them reads
    // the rest of the file as a string and stops exempting log lines.
    expect(EMOJI.test(lines[1])).toBe(false);
    // Rendered copy is still caught after both constructs.
    expect(EMOJI.test(lines[3])).toBe(true);
  });

  test("no emoji in any admin_ui module outside comments and console output", () => {
    const offenders = [];
    for (const file of listFiles(
      path.join(repoRoot, "mcpgateway/admin_ui"),
      ".js"
    )) {
      offenders.push(
        ...offendersIn(fs.readFileSync(file, "utf8"), path.relative(repoRoot, file))
      );
    }
    expect(offenders).toEqual([]);
  });

  test("no emoji in admin.html outside comments, scripts and console output", () => {
    expect(offendersIn(stripHtml(adminHtml), "admin.html")).toEqual([]);
  });

  test("message helpers derive their icon from the notification type", () => {
    expect(utilsJs).toContain("export const NOTIFICATION_ICONS");
    expect(utilsJs).toMatch(
      /createIcon\(NOTIFICATION_ICONS\[type\] \|\| ICONS\.info/
    );
    // `danger` used to fall through to the default and render a blue toast.
    expect(utilsJs).toMatch(/type === "danger"/);
  });

  test("icons are built as elements, never injected as markup", () => {
    expect(utilsJs).toMatch(
      /export function createIcon\(name, extraClass = ""\) \{\n {2}const icon = document\.createElement\("i"\);/
    );
    expect(utilsJs).toMatch(
      /export function setIconText\(element, name, text = "", extraClass = ""\) \{/
    );
  });

  test("the user-creation error path stays textContent-based", () => {
    // This node renders server-influenced text; building it with createElement
    // keeps the defence-in-depth that the original textContent assignment had.
    expect(adminHtml).toContain(
      "window.Admin.setIconText(errorContainer, 'fa-circle-xmark', errorMsg);"
    );
    expect(adminHtml).not.toMatch(/errorContainer\.innerHTML/);
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
