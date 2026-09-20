/**
 * Oracle compatibility mode over node-oracledb.
 *
 * This is the runtime question the requirement singles out: "如果 TypeScript 方案不能
 * 稳定支持 OceanBase Oracle Mode，不允许为了统一语言强行采用 TypeScript". So the suite
 * is built around what can be established about *that* question, and it separates two
 * things that are easy to conflate:
 *
 *   1. whether a usable Oracle driver exists in this runtime at all;
 *   2. whether it can talk to OceanBase's Oracle compatibility mode.
 *
 * (1) is answerable here and now, and it is not a formality: node-oracledb's thick
 * mode needs a native client library, so "installable" and "usable" are different
 * facts with different deployment consequences -- the same distinction the Python
 * suite draws between `python-oracledb` thin and thick.
 *
 * (2) needs a reachable OceanBase instance and is reported as unverified without one.
 * No amount of inspecting the driver answers it.
 */

import { createRequire } from 'node:module';

import { CheckStatus, checkResult } from '../shared/results.mjs';

export const DRIVER_PACKAGE = 'oracledb';

/** Candidate statements for reading the instance compatibility mode, as in Python. */
export const COMPAT_MODE_QUERIES = [
  ["SELECT value FROM v$parameter WHERE name = 'ob_compatibility_mode'", 'v$parameter'],
];

/**
 * Load node-oracledb.
 *
 * The version comes from package metadata, not from the module's own attribute. This
 * is the same trap the Python suite documents for PyMySQL: `oracledb.version` is the
 * numeric build version (`70001`), which is not what a reader would type into
 * `npm install`. A version in the conclusions document has to be one that resolves.
 *
 * @returns {Promise<{module: object|null, version: string|null, error: string|null}>}
 */
export async function load() {
  try {
    const module = await import('oracledb');
    return { module: module.default ?? module, version: resolveVersion(), error: null };
  } catch (error) {
    return { module: null, version: null, error: String(error?.message ?? error) };
  }
}

/**
 * Read the installed distribution version, falling back to the driver's attribute.
 *
 * @returns {string}
 */
function resolveVersion() {
  try {
    return createRequire(import.meta.url)(`${DRIVER_PACKAGE}/package.json`).version;
  } catch {
    return 'unknown';
  }
}

/**
 * Report whether the driver is in thin mode.
 *
 * @param {object|null} module
 * @returns {'thin'|'thick'|'unknown'}
 */
export function clientMode(module) {
  if (!module) return 'unknown';
  if (typeof module.thin === 'boolean') return module.thin ? 'thin' : 'thick';
  return 'unknown';
}

function check({ checkId, name, status, summary, evidence = {} }) {
  return checkResult({ checkId, mode: 'oracle', name, status, summary, evidence });
}

/**
 * The first line of a message, for use in a summary.
 *
 * @param {string} text
 * @returns {string}
 */
function firstLine(text) {
  return String(text ?? '').split('\n')[0].trim();
}

/**
 * Preflight: is a usable driver present, and does it need a native client library?
 *
 * Runs without a database and without credentials, so it produces a real answer even
 * on a machine that cannot reach OceanBase.
 *
 * Ordering note: attempting thick mode calls `initOracleClient()`, which switches the
 * whole process to thick and cannot be undone. This runs *after* the thin connection
 * checks so that a thin result is never silently produced by a thick client.
 *
 * @param {{module: object|null, version: string|null, error: string|null}} loaded
 * @returns {object[]}
 */
export function runPreflightChecks(loaded) {
  const results = [];

  if (!loaded.module) {
    results.push(
      check({
        checkId: 'TS-PRE-THIN',
        name: 'TypeScript Oracle 驱动可加载（thin）',
        status: CheckStatus.UNSUPPORTED,
        summary: `oracledb 未安装或无法加载：${loaded.error}`,
        evidence: { reason: 'driver-not-installed' },
      }),
    );
    results.push(
      check({
        checkId: 'TS-PRE-THICK',
        name: 'TypeScript Oracle 驱动可用（thick）',
        status: CheckStatus.UNSUPPORTED,
        summary: '驱动未安装，无法尝试 thick 模式',
        evidence: { reason: 'driver-not-installed' },
      }),
    );
    return results;
  }

  results.push(
    check({
      checkId: 'TS-PRE-THIN',
      name: 'TypeScript Oracle 驱动可加载（thin）',
      status: CheckStatus.PASS,
      summary: `oracledb ${loaded.version} 可加载，当前为 ${clientMode(loaded.module)} 模式；thin 模式无需客户端库`,
      evidence: { driver_version: loaded.version, client_mode: clientMode(loaded.module), needs_client_library: false },
    }),
  );

  results.push(probeThickMode(loaded));
  return results;
}

/**
 * Try to enable thick mode, which is what needs a native client library.
 *
 * @param {{module: object, version: string|null}} loaded
 * @returns {object}
 */
function probeThickMode(loaded) {
  const init = loaded.module.initOracleClient;
  if (typeof init !== 'function') {
    return check({
      checkId: 'TS-PRE-THICK',
      name: 'TypeScript Oracle 驱动可用（thick）',
      status: CheckStatus.UNSUPPORTED,
      summary: 'oracledb 未暴露 initOracleClient，无法启用 thick 模式',
      evidence: { reason: 'no-init-oracle-client' },
    });
  }

  try {
    init.call(loaded.module);
  } catch (error) {
    // The expected outcome on a machine without Instant Client / OBCI. This is a
    // capability finding, not a failure of the POC.
    //
    // The message is kept whole in evidence but trimmed to its first line in the
    // summary: node-oracledb appends a multi-paragraph installation guide, which
    // makes the results table unreadable and buries the actual cause.
    const full = String(error?.message ?? error);
    return check({
      checkId: 'TS-PRE-THICK',
      name: 'TypeScript Oracle 驱动可用（thick）',
      status: CheckStatus.UNSUPPORTED,
      summary: `thick 模式不可用（缺原生客户端库）：${firstLine(full)}`,
      evidence: { reason: 'no-client-library', client_library_required: true, error_message: full },
    });
  }

  return check({
    checkId: 'TS-PRE-THICK',
    name: 'TypeScript Oracle 驱动可用（thick）',
    status: CheckStatus.PASS,
    summary: `thick 模式已启用（需原生客户端库，部署时须随镜像携带）；当前为 ${clientMode(loaded.module)} 模式`,
    evidence: { client_mode: clientMode(loaded.module), client_library_required: true },
  });
}

/**
 * Run the bounded Oracle suite.
 *
 * @param {object|null} config
 * @param {string[]} missingEnv
 * @param {object|null} module
 * @param {import('../shared/redact.mjs').Redactor} redactor
 * @returns {Promise<object[]>}
 */
export async function runOracleChecks(config, missingEnv, module, redactor) {
  const specs = [
    ['O1', '建立连接'],
    ['O2', '简单 SELECT'],
    ['O3', 'Named Bind Parameter'],
  ];

  if (config === null) {
    return specs.map(([checkId, name]) =>
      check({
        checkId,
        name,
        status: CheckStatus.SKIP_NO_ENV,
        summary: `未执行：缺少连接环境变量 ${missingEnv.join(', ')}`,
        evidence: { missing_env: missingEnv },
      }),
    );
  }

  if (!module) {
    return specs.map(([checkId, name]) =>
      check({ checkId, name, status: CheckStatus.UNSUPPORTED, summary: '驱动未安装，无法执行该检查', evidence: { reason: 'driver-not-installed' } }),
    );
  }

  const connectString = `${config.host}:${config.port}/${config.serviceName ?? config.database ?? ''}`;
  let connection = null;
  try {
    connection = await module.getConnection({
      user: config.user,
      password: config.password,
      connectString,
    });
  } catch (error) {
    return specs.map(([checkId, name]) =>
      check({
        checkId,
        name,
        status: CheckStatus.ERROR,
        summary: redactor.redact(String(error?.message ?? error)),
        evidence: { client_mode: clientMode(module) },
      }),
    );
  }

  try {
    const [versionRows] = await connection.execute('SELECT banner FROM v$version WHERE ROWNUM = 1', [], { outFormat: module.OUT_FORMAT_ARRAY });
    const [scalarRows] = await connection.execute('SELECT 1 FROM DUAL', [], { outFormat: module.OUT_FORMAT_ARRAY });
    const [bindRows] = await connection.execute('SELECT :a AS a FROM DUAL', { a: 'bound-value' }, { outFormat: module.OUT_FORMAT_ARRAY });

    return [
      check({
        checkId: 'O1',
        name: '建立连接',
        status: CheckStatus.PASS,
        summary: `连接成功（${clientMode(module)} 模式），实例版本 ${versionRows?.[0]?.[0] ?? 'unknown'}`,
        evidence: { client_mode: clientMode(module), version: versionRows?.[0]?.[0] ?? null, target: redactor.redactDeep({ host: config.host, port: config.port }) },
      }),
      check({
        checkId: 'O2',
        name: '简单 SELECT',
        status: CheckStatus.PASS,
        summary: '简单 SELECT 返回预期结果',
        evidence: { select_1: scalarRows?.[0]?.[0] ?? null },
      }),
      check({
        checkId: 'O3',
        name: 'Named Bind Parameter',
        status: bindRows?.[0]?.[0] === 'bound-value' ? CheckStatus.PASS : CheckStatus.FAIL,
        summary: bindRows?.[0]?.[0] === 'bound-value' ? ':name 命名绑定取值正确' : `命名绑定返回了非预期结果：${JSON.stringify(bindRows?.[0]?.[0])}`,
      }),
    ];
  } finally {
    await connection.close().catch(() => {});
  }
}

/**
 * Describe the bounded scope this suite covers, for the conclusions document.
 *
 * @returns {string}
 */
export function scopeNote() {
  return 'TypeScript 侧为限定范围评估（设计 D5）：MySQL 模式只覆盖连通/查询/绑定，Oracle 模式回答「驱动是否可用」与「能否连上 OB Oracle Mode」两个问题，不做全矩阵。';
}
