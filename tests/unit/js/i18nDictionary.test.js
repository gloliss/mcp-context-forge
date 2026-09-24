/**
 * Unit tests for the ContextForge Admin UI Chinese dictionary (i18n)
 *
 * 词典是运行时文字覆盖层的查表数据：key 是界面上逐字出现的英文原文
 * （已 trim、已把连续空白折叠为单个半角空格），value 是简体中文。
 * 这里校验的是「数据本身」的完整性，不依赖任何 DOM 或 translator 实现。
 */

import { describe, test, expect } from "vitest";
import { readFileSync, readdirSync, statSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";

import zhCN, { dictionaries } from "../../../mcpgateway/admin_ui/i18n/zh-CN.js";
import common from "../../../mcpgateway/admin_ui/i18n/dict/common.js";
import forms from "../../../mcpgateway/admin_ui/i18n/dict/forms.js";
import tables from "../../../mcpgateway/admin_ui/i18n/dict/tables.js";
import toasts from "../../../mcpgateway/admin_ui/i18n/dict/toasts.js";
import errors from "../../../mcpgateway/admin_ui/i18n/dict/errors.js";
import auth from "../../../mcpgateway/admin_ui/i18n/dict/auth.js";
import metrics from "../../../mcpgateway/admin_ui/i18n/dict/metrics.js";
import admin from "../../../mcpgateway/admin_ui/i18n/dict/admin.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, "../../..");
const UI_DIR = join(REPO_ROOT, "mcpgateway", "admin_ui");
const TEMPLATE_DIR = join(REPO_ROOT, "mcpgateway", "templates");

const DOMAINS = { common, forms, tables, toasts, errors, auth, metrics, admin };
const DOMAIN_NAMES = Object.keys(DOMAINS);

/** 刻意保留英文原文、value 与 key 完全相同的条目（协议字段，不是实体）。 */
const ALLOW_IDENTICAL = new Set([
  "Bearer Token",
  "Token Endpoint",
  "Access Token",
  "Refresh Token",
  "Idempotency-Key",
  "Content-Type",
  "X-CSRF-Token",
]);

/**
 * value 里不含中文但属刻意为之的条目：保留英文的协议标签只做全角标点归一化，
 * 以及被 <span> 切开的 `0 of 0 calls` 里的连接词。
 */
const ALLOW_NO_CJK = new Set([
  "Token Endpoint:", // 保留英文协议标签 + 全角冒号
  "Correlation ID:", // 同上
  "IP:", // 同上
  "User Agent:", // 同上
  "of", // `0 of 0 calls` 被 span 切开，渲染为 `0 / 0 次调用`
]);

/**
 * 「服务器」在这里指后端 / OAuth 协议里的 server，不是被保留的 MCP `Server` 实体，
 * 故译中文是正确的（RFC 6749 中文版即「授权服务器」）。
 */
const GENERIC_SERVER_OK = new Set([
  "5xx Server Error",
  "The OAuth Authorization Server issuer URL. Required for DCR.",
  "The OAuth Authorization Server issuer URL. Required for DCR (Dynamic Client Registration).",
  "Server returned an empty response. This endpoint may not be implemented yet or the server crashed.",
]);

/**
 * 保留英文的专有名词：译文里出现对应中文即为误译。
 * 例外按「英文原文」白名单，逐条说明原因。
 */
const PRESERVED_TERMS = [
  [/服务器/, "Server"],
  [/网关/, "Gateway"],
  [/插件/, "Plugin"],
  [/提示词/, "Prompt"],
  [/工具(?!栏|箱)/, "Tool"],
  [/资源(?!组)/, "Resource"],
];

/**
 * key -> 允许被译成中文保留词的例外。
 * `System Resources`（version_info 卡片的宿主 CPU/内存）里的 Resources 是普通名词，
 * 不是 MCP Resource 实体，故译「系统资源」。
 */
const TERM_EXCEPTIONS = { "System Resources": "host CPU/RAM card, not an MCP Resource" };

const CJK = /[一-鿿]/;

/**
 * 抽取器读的是渲染后的 DOM 文本，所以 HTML 实体是解码后的形态
 * （`&mdash;` 变 `—`、`&amp;` 变 `&`）。语料必须同样解码，
 * 否则这些 key 会被误判成凭空捏造。
 */
const ENTITIES = {
  amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " ",
  mdash: "—", ndash: "–", hellip: "…", times: "×",
  lsquo: "‘", rsquo: "’", ldquo: "“", rdquo: "”",
  middot: "·", bull: "•", rarr: "→", larr: "←",
  copy: "©", reg: "®", deg: "°", check: "✓",
};

function decodeEntities(text) {
  return text.replace(/&(#x[0-9a-fA-F]+|#\d+|[A-Za-z][A-Za-z0-9]*);/g, (whole, body) => {
    if (body.slice(0, 2) === "#x" || body.slice(0, 2) === "#X") {
      return String.fromCodePoint(parseInt(body.slice(2), 16));
    }
    if (body[0] === "#") return String.fromCodePoint(parseInt(body.slice(1), 10));
    return Object.prototype.hasOwnProperty.call(ENTITIES, body) ? ENTITIES[body] : whole;
  });
}

/**
 * JS 字符串字面量到达 DOM 时转义已被处理，`'Loading teams…'` 渲染成
 * `Loading teams…`。语料必须同时索引解码后的形态，否则正确取键反而像捏造。
 */
function decodeJsEscapes(text) {
  return text.replace(/\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})|\\([ntr])/g, (_m, u, x) => {
    if (u) return String.fromCodePoint(parseInt(u, 16));
    if (x) return String.fromCodePoint(parseInt(x, 16));
    return " ";
  });
}

function walk(dir, filter, acc = []) {
  if (!existsSync(dir)) return acc;
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    const st = statSync(p);
    if (st.isDirectory()) {
      if (name === "node_modules" || name === "i18n" || name === "dist" || name === ".git") continue;
      walk(p, filter, acc);
    } else if (filter(name)) {
      acc.push(p);
    }
  }
  return acc;
}

/**
 * Corpus of English source text the dictionary may key off of.
 * i18n/ is deliberately excluded (it holds the translations themselves).
 * Whitespace is collapsed to a single space and quote escapes dropped,
 * mirroring the normalisation the extractor applied to the keys.
 */
function buildCorpus() {
  const files = [
    ...walk(UI_DIR, (n) => n.endsWith(".js")),
    ...walk(TEMPLATE_DIR, (n) => n.endsWith(".html")),
    ...walk(join(REPO_ROOT, "mcpgateway", "static"),
           (n) => n.endsWith(".js") && !n.startsWith("bundle-")),
  ];
  const parts = [];
  for (const f of files) {
    try {
      const raw = readFileSync(f, "utf8");
      // Index both the raw bytes and the escape-decoded variant: the decoded
      // form is what actually reaches the DOM. Entities are decoded and
      // whitespace collapsed the same way the extractor did it.
      for (const variant of [raw, decodeJsEscapes(raw)]) {
        parts.push(decodeEntities(variant).replace(/\s+/g, " ").replace(/\\(['"`\\])/g, "$1"));
      }
    } catch {
      /* ignore unreadable files */
    }
  }
  return parts.join("\n");
}

const CORPUS = buildCorpus();

describe("i18n dictionary files", () => {
  test("all eight domain modules are plain, non-empty objects", () => {
    for (const name of DOMAIN_NAMES) {
      const d = DOMAINS[name];
      expect(d, `${name}.js should default-export an object`).toBeTypeOf("object");
      expect(Array.isArray(d), `${name}.js must not be an array`).toBe(false);
      expect(d).not.toBeNull();
      expect(Object.keys(d).length, `${name}.js is empty`).toBeGreaterThan(0);
    }
  });

  test("every key is pre-normalised (trimmed, single-space whitespace)", () => {
    const bad = [];
    for (const name of DOMAIN_NAMES) {
      for (const k of Object.keys(DOMAINS[name])) {
        if (k !== k.replace(/\s+/g, " ").trim()) bad.push(`${name}: ${JSON.stringify(k)}`);
      }
    }
    expect(bad).toEqual([]);
  });

  test("every value is a non-empty Chinese string", () => {
    const bad = [];
    for (const name of DOMAIN_NAMES) {
      for (const [k, v] of Object.entries(DOMAINS[name])) {
        if (typeof v !== "string" || v.trim() === "") bad.push(`${name}: empty value for ${k}`);
        else if (v === k && !ALLOW_IDENTICAL.has(k)) bad.push(`${name}: untranslated ${k}`);
        else if (!CJK.test(v) && !ALLOW_NO_CJK.has(k)) bad.push(`${name}: no Chinese in ${k} -> ${v}`);
      }
    }
    expect(bad).toEqual([]);
  });

  test("keys do not collide across domain modules", () => {
    const seen = new Map();
    const dups = [];
    for (const name of DOMAIN_NAMES) {
      for (const k of Object.keys(DOMAINS[name])) {
        if (seen.has(k)) dups.push(`${JSON.stringify(k)}: ${seen.get(k)} + ${name}`);
        else seen.set(k, name);
      }
    }
    expect(dups).toEqual([]);
  });

  test("zh-CN.js merges exactly the eight domain modules", () => {
    expect(Object.keys(dictionaries).sort()).toEqual([...DOMAIN_NAMES].sort());
    for (const name of DOMAIN_NAMES) {
      expect(dictionaries[name]).toBe(DOMAINS[name]);
    }
    const sum = DOMAIN_NAMES.reduce((n, d) => n + Object.keys(DOMAINS[d]).length, 0);
    expect(Object.keys(zhCN).length).toBe(sum);
    const merged = {};
    for (const name of DOMAIN_NAMES) Object.assign(merged, DOMAINS[name]);
    expect(zhCN).toEqual(merged);
  });

  test("dictionary keys are never the empty string or whitespace", () => {
    for (const name of DOMAIN_NAMES) {
      for (const k of Object.keys(DOMAINS[name])) {
        expect(k.trim().length, `${name}: blank key`).toBeGreaterThan(0);
      }
    }
  });
});

describe("i18n dictionary provenance", () => {
  // 抽样校验比全量更快，同时足以发现「凭空捏造 key」这类问题。
  test("every key appears verbatim in templates or admin_ui sources", () => {
    const missing = [];
    for (const name of DOMAIN_NAMES) {
      for (const k of Object.keys(DOMAINS[name])) {
        if (!CORPUS.includes(k)) missing.push(`${name}: ${JSON.stringify(k)}`);
      }
    }
    expect(missing).toEqual([]);
  });

  test("protected domain terms stay in English", () => {
    const bad = [];
    for (const name of DOMAIN_NAMES) {
      for (const [k, v] of Object.entries(DOMAINS[name])) {
        if (TERM_EXCEPTIONS[k]) continue;
        for (const [rx, term] of PRESERVED_TERMS) {
          if (term === "Server" && GENERIC_SERVER_OK.has(k)) continue;
          if (rx.test(v) && k.includes(term)) bad.push(`${name}: ${JSON.stringify(k)} -> ${v} (${term})`);
        }
      }
    }
    expect(bad).toEqual([]);
  });

  test("no key looks like an HTML fragment, selector or code identifier", () => {
    const bad = [];
    for (const name of DOMAIN_NAMES) {
      for (const k of Object.keys(DOMAINS[name])) {
        if (/[<>]/.test(k)) bad.push(`${name}: markup-ish ${JSON.stringify(k)}`);
      }
    }
    expect(bad).toEqual([]);
  });
});
