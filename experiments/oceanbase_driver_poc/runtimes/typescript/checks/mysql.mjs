/**
 * MySQL compatibility mode over mysql2.
 *
 * Scope is deliberately bounded (design D5): the TypeScript evaluation exists to
 * answer the Oracle-mode question the requirement singles out, so the MySQL suite
 * here covers only basic connectivity, a simple query, and parameter binding. It is
 * not a second full matrix.
 *
 * One difference from the PyMySQL suite is worth recording rather than hiding:
 * mysql2 offers two distinct execution paths, and they bind differently.
 *
 *   - `query()` escapes values client-side and interpolates them into the SQL, which
 *     is what PyMySQL's `cursor.execute(sql, args)` does too;
 *   - `execute()` uses a real server-side prepared statement (COM_STMT_PREPARE).
 *
 * The second is a strictly stronger binding mode and its availability on OceanBase is
 * a compatibility question this check answers rather than assumes, so both are run.
 */

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import mysql from 'mysql2/promise';

import { CheckStatus, checkResult } from '../shared/results.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const BIND_CASES = JSON.parse(readFileSync(join(HERE, '..', '..', '..', 'common', 'bind_cases.json'), 'utf8'));

export const DRIVER_PACKAGE = 'mysql2';

/** Candidate statements for reading the instance compatibility mode, as in Python. */
export const COMPAT_MODE_QUERIES = [
  ['SELECT @@ob_compatibility_mode', 'variable'],
  ["SHOW VARIABLES LIKE 'ob_compatibility_mode'", 'show-variables'],
];

/** MySQL server errno values, mirroring the Python map for the codes seen here. */
const ERRNO_MAP = new Map([
  [1044, 'DB_PERMISSION_DENIED'],
  [1045, 'DB_AUTH_FAILED'],
  [1049, 'DB_OBJECT_NOT_FOUND'],
  [1064, 'DB_SYNTAX_ERROR'],
  [1142, 'DB_PERMISSION_DENIED'],
  [1146, 'DB_OBJECT_NOT_FOUND'],
  [1205, 'DB_TIMEOUT'],
  [1317, 'DB_TIMEOUT'],
  [2003, 'DB_UNREACHABLE'],
  [2005, 'DB_UNREACHABLE'],
  [2013, 'DB_UNREACHABLE'],
  [3024, 'DB_TIMEOUT'],
]);

/**
 * Classify a driver failure into the shared vocabulary.
 *
 * @param {Error & {errno?: number}} error
 * @returns {{code: string, native: string|null}}
 */
export function classifyError(error) {
  const message = String(error?.message ?? '').toLowerCase();
  const errno = typeof error?.errno === 'number' ? error.errno : null;

  if (errno !== null) {
    // Same overload as PyMySQL: 2013 covers both a dropped connection and a socket
    // read timeout, and only the wording separates them.
    if (errno === 2013 && /timed out|timeout/.test(message)) return { code: 'DB_TIMEOUT', native: String(errno) };
    const mapped = ERRNO_MAP.get(errno);
    if (mapped) return { code: mapped, native: String(errno) };
  }
  // Reachability wording before timeout wording: an endpoint that never answered is
  // unreachable, not a query that exceeded its deadline.
  if (/can't connect|connection refused|unknown host|econnrefused|etimedout/.test(message)) return { code: 'DB_UNREACHABLE', native: errno === null ? null : String(errno) };
  if (/access denied/.test(message)) return { code: 'DB_AUTH_FAILED', native: errno === null ? null : String(errno) };
  if (/timed out|timeout/.test(message)) return { code: 'DB_TIMEOUT', native: errno === null ? null : String(errno) };
  return { code: 'DB_DRIVER_ERROR', native: errno === null ? null : String(errno) };
}

/**
 * Connect parameters for mysql2.
 *
 * @param {object} config
 * @returns {object}
 */
export function connectOptions(config) {
  return {
    host: config.host,
    port: config.port,
    user: config.user,
    password: config.password,
    database: config.database ?? undefined,
    connectTimeout: Math.round(config.connectTimeoutS * 1000),
    charset: 'utf8mb4',
    // Keep the driver quiet about a target it cannot reach; the check reports it.
    disableEval: true,
  };
}

/**
 * Extract the compatibility mode from whichever probe answered.
 *
 * @param {string} source
 * @param {Array} row
 * @returns {string|null}
 */
export function normaliseCompatMode(source, row) {
  if (!row) return null;
  const values = Array.isArray(row) ? row : Object.values(row);
  if (source === 'show-variables') return values.length > 1 && values[1] != null ? String(values[1]) : null;
  return values.length > 0 && values[0] != null ? String(values[0]) : null;
}

function skipped(checkId, name, missingEnv) {
  return checkResult({
    checkId,
    mode: 'mysql',
    name,
    status: CheckStatus.SKIP_NO_ENV,
    summary: `未执行：缺少连接环境变量 ${missingEnv.join(', ')}`,
    evidence: { missing_env: missingEnv },
  });
}

function errored(checkId, name, error, redactor) {
  const { code, native } = classifyError(error);
  return checkResult({
    checkId,
    mode: 'mysql',
    name,
    status: CheckStatus.ERROR,
    summary: redactor.redact(String(error?.message ?? error)),
    evidence: { error_code: code, native_code: native, exception: error?.constructor?.name ?? 'Error' },
  });
}

/**
 * Run the bounded MySQL suite.
 *
 * @param {object|null} config
 * @param {string[]} missingEnv
 * @param {import('../shared/redact.mjs').Redactor} redactor
 * @returns {Promise<object[]>}
 */
export async function runMysqlChecks(config, missingEnv, redactor) {
  const specs = [
    ['M1', '建立连接'],
    ['M2', 'SELECT 1 / 简单查询'],
    ['M3', '参数绑定'],
  ];

  if (config === null) return specs.map(([id, name]) => skipped(id, name, missingEnv));

  const results = [];
  let connection = null;
  try {
    connection = await mysql.createConnection(connectOptions(config));
  } catch (error) {
    // A failed connection is reported per check, so the shape of the result does not
    // depend on which failure happened first.
    return specs.map(([id, name]) => errored(id, name, error, redactor));
  }

  try {
    results.push(await checkConnect(connection, config, redactor));
    results.push(await checkSimpleQuery(connection));
    results.push(await checkParamBinding(connection));
  } finally {
    await connection.end().catch(() => {});
  }
  return results;
}

async function checkConnect(connection, config, redactor) {
  const [versionRows] = await connection.query('SELECT VERSION()');
  const version = versionRows?.[0] ? Object.values(versionRows[0])[0] : null;

  const attempts = [];
  let compatMode = null;
  let source = null;
  for (const [statement, label] of COMPAT_MODE_QUERIES) {
    try {
      const [rows] = await connection.query(statement);
      const candidate = normaliseCompatMode(label, rows?.[0] ? Object.values(rows[0]) : null);
      if (candidate) {
        compatMode = candidate;
        source = label;
        break;
      }
      attempts.push(`${label}: 未返回兼容模式`);
    } catch (error) {
      attempts.push(`${label}: ${error.message}`);
    }
  }
  if (compatMode === null && attempts.length === 0) attempts.push('no-candidate-answered');

  return checkResult({
    checkId: 'M1',
    mode: 'mysql',
    name: '建立连接',
    status: CheckStatus.PASS,
    summary: `连接成功，实例版本 ${version ?? 'unknown'}${compatMode ? `，兼容模式 ${compatMode}` : '，兼容模式未读到'}`,
    evidence: {
      instance: { version, compat_mode: compatMode, compat_mode_source: source ?? 'unavailable', probe_attempts: attempts },
      connect_options: redactor.redactDeep(connectOptions(config)),
    },
  });
}

async function checkSimpleQuery(connection) {
  const [scalarRows] = await connection.query('SELECT 1 AS n');
  const [mixedRows] = await connection.query("SELECT 1 AS n, 'text' AS s, NULL AS n_null");
  const scalar = scalarRows?.[0] ? Object.values(scalarRows[0])[0] : null;
  const mixed = mixedRows?.[0] ? Object.values(mixedRows[0]) : null;

  if (scalar === null || mixed === null) {
    return checkResult({ checkId: 'M2', mode: 'mysql', name: 'SELECT 1 / 简单查询', status: CheckStatus.FAIL, summary: '简单查询未返回结果' });
  }
  if (scalar !== 1 || mixed[0] !== 1) {
    return checkResult({ checkId: 'M2', mode: 'mysql', name: 'SELECT 1 / 简单查询', status: CheckStatus.FAIL, summary: `简单查询返回了非预期结果：${JSON.stringify([scalar, mixed])}` });
  }
  return checkResult({
    checkId: 'M2',
    mode: 'mysql',
    name: 'SELECT 1 / 简单查询',
    status: CheckStatus.PASS,
    summary: '简单查询返回预期结果',
    evidence: { select_1: scalar, row: mixed },
  });
}

async function checkParamBinding(connection) {
  const mismatches = [];
  const modes = {};

  for (const [execution, runner] of [
    ['execute(prepared)', (sql, params) => connection.execute(sql, params)],
    ['query(client-escaped)', (sql, params) => connection.query(sql, params)],
  ]) {
    let ok = true;
    for (const [label, value] of BIND_CASES) {
      try {
        const [rows] = await runner('SELECT ? AS v', [value]);
        const row = rows?.[0];
        if (!row) {
          mismatches.push(`${execution}/${label}: 未返回结果`);
          ok = false;
          continue;
        }
        const got = Object.values(row)[0];
        if (got !== value) {
          mismatches.push(`${execution}/${label}: 期望 ${JSON.stringify(value)}，得到 ${JSON.stringify(got)}`);
          ok = false;
        }
      } catch (error) {
        mismatches.push(`${execution}/${label}: ${error.message}`);
        ok = false;
      }
    }
    modes[execution] = ok;
  }

  const total = BIND_CASES.length;
  const evidence = { modes, cases: total, mismatches };
  if (Object.values(modes).some((ok) => !ok)) {
    return checkResult({
      checkId: 'M3',
      mode: 'mysql',
      name: '参数绑定',
      status: CheckStatus.FAIL,
      summary: `${mismatches.length} 处绑定结果不符（prepared=${modes['execute(prepared)']}，client-escaped=${modes['query(client-escaped)']}）`,
      evidence,
    });
  }
  return checkResult({
    checkId: 'M3',
    mode: 'mysql',
    name: '参数绑定',
    status: CheckStatus.PASS,
    summary: `${total} 类参数在服务端预编译与客户端转义两条路径下取值均不变`,
    evidence,
  });
}
