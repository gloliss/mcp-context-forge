/**
 * Regression tests for SVG structure in the Admin UI.
 *
 * HTML parsing has a foreign-content insertion mode: inside an `<svg>`, a start
 * tag whose name is one of the HTML tags (the "breakout" list — `i`, `span`,
 * `div`, `b`, `p`, `br`, …) is a parse error that pops the SVG off the open
 * element stack and reprocesses the tag as HTML. Everything after it is then
 * parsed as HTML too, so the remaining `<g>`/`<rect>`/`<text>` become unknown
 * inline elements: the drawing vanishes and the labels collapse into a run of
 * plain text.
 *
 * The overview diagram hit exactly this when a decorative `<i class="fa-solid
 * fa-link">` was placed inside `<text>` — the input section still drew, and all
 * of GATEWAY / INFRASTRUCTURE / OUTPUTS rendered as text. Icons therefore
 * cannot be reused inside SVG markup; a `<path>` is the shape that belongs
 * there.
 */

import { describe, expect, test } from "vitest";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, "../../..");
const SVG_NS = "http://www.w3.org/2000/svg";

function read(relativePath) {
  return fs.readFileSync(path.join(repoRoot, relativePath), "utf8");
}

/**
 * Tag names that terminate an SVG (foreign-content breakout). `font` is in the
 * spec's list only with a colour/face/size attribute and `image` is a real SVG
 * element, so neither appears here.
 */
const BREAKOUT = new Set(
  "b big blockquote body br center code dd div dl dt em embed h1 h2 h3 h4 h5 h6 head hr i img li listing menu meta nobr ol p pre ruby s small span strong strike sub sup table tt u ul var".split(
    " "
  )
);

const TOKEN = /<\/?([a-zA-Z][a-zA-Z0-9]*)(?=[\s/>])[^>]*>/g;

/**
 * Blank HTML comments, keeping newlines so reported line numbers stay real. A
 * comment cannot open an element, so prose that names a tag (`a <text><i> here
 * …`) must not be read as markup. Only applied to `.html`: in JS a `<!--`
 * without a matching `-->` would blank real code instead.
 */
function blankHtmlComments(source) {
  return source.replace(/<!--[\s\S]*?-->/g, (comment) =>
    comment.replace(/[^\n]/g, " ")
  );
}

function listFiles(dir, extensions) {
  const found = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      found.push(...listFiles(full, extensions));
    } else if (extensions.some((extension) => entry.name.endsWith(extension))) {
      found.push(full);
    }
  }
  return found;
}

/** Vite output, rebuilt from the sources these tests already scan. */
function isGeneratedBundle(file) {
  return /^(?:bundle|chunk|i18n)-/.test(path.basename(file));
}

/**
 * Tokenise the whole file, not line by line, so a tag whose attributes wrap is
 * still seen. Depth is carried across the file, which is what lets the same
 * check cover JS that builds SVG in template literals.
 */
function breakoutTags(source) {
  const hits = [];
  let depth = 0;
  for (const match of source.matchAll(TOKEN)) {
    const name = match[1].toLowerCase();
    if (match[0].startsWith("</")) {
      if (name === "svg" && depth > 0) depth -= 1;
      continue;
    }
    if (name === "svg") {
      depth += 1;
    } else if (depth > 0 && BREAKOUT.has(name)) {
      const line = source.slice(0, match.index).split("\n").length;
      hits.push(`${line}: <${name}> inside svg — ${match[0].slice(0, 80)}`);
    }
  }
  return hits;
}

describe("Admin UI SVG structure", () => {
  test("the overview diagram keeps its sections inside the svg element", () => {
    const holder = document.createElement("div");
    holder.innerHTML = read("mcpgateway/templates/overview_partial.html");

    const svg = holder.querySelector("#overview-architecture");
    expect(svg).not.toBeNull();
    expect(svg.namespaceURI).toBe(SVG_NS);

    // A breakout tag ejects these groups out of the svg, where they parse as
    // generic HTML elements — drawn as nothing, read as a wall of text.
    for (const section of [
      "input-section",
      "center-section",
      "infrastructure-section",
      "output-section",
    ]) {
      const group = svg.querySelector(`.${section}`);
      expect(group, `.${section} must remain inside #overview-architecture`).not.toBeNull();
      expect(group.namespaceURI).toBe(SVG_NS);
    }
  });

  test("no HTML breakout tag is used inside an svg, in any template or script", () => {
    const offenders = [];
    for (const directory of ["mcpgateway/templates", "mcpgateway/admin_ui", "mcpgateway/static"]) {
      for (const file of listFiles(path.join(repoRoot, directory), [".html", ".js"])) {
        if (isGeneratedBundle(file)) continue;
        const source = fs.readFileSync(file, "utf8");
        const text = file.endsWith(".html") ? blankHtmlComments(source) : source;
        for (const hit of breakoutTags(text)) {
          offenders.push(`${path.relative(repoRoot, file)}:${hit}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});
