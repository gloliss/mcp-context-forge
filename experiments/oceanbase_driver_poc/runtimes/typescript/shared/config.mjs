/**
 * Environment-backed configuration, mirroring `common/config.py`.
 *
 * The variable names are identical to the Python side on purpose: one environment
 * drives both runtimes, so a TS result and a Python result for the same target are
 * comparable without reconfiguring anything.
 *
 * A missing required variable is not an error. It is the normal state of a machine
 * with no OceanBase attached, and it becomes SKIP_NO_ENV for every check.
 */

import { Redactor } from './redact.mjs';

const ENV_PREFIX = { mysql: 'OB_MYSQL', oracle: 'OB_ORACLE' };

/** Port has no default: OBProxy and direct connections use different ones. */
const REQUIRED_FIELDS = ['HOST', 'PORT', 'USER', 'PASSWORD'];

/**
 * The environment variable name for each field of a mode.
 *
 * @param {'mysql'|'oracle'} mode
 * @returns {Record<string, string>}
 */
export function envVarNames(mode) {
  const prefix = ENV_PREFIX[mode];
  return {
    host: `${prefix}_HOST`,
    port: `${prefix}_PORT`,
    user: `${prefix}_USER`,
    password: `${prefix}_PASSWORD`,
    database: `${prefix}_DATABASE`,
    serviceName: `${prefix}_SERVICE_NAME`,
    schema: `${prefix}_SCHEMA`,
    connectTimeoutS: `${prefix}_CONNECT_TIMEOUT_S`,
    queryTimeoutS: `${prefix}_QUERY_TIMEOUT_S`,
    poolMax: `${prefix}_POOL_MAX`,
  };
}

/**
 * List the required variables that are absent or blank.
 *
 * @param {'mysql'|'oracle'} mode
 * @param {Record<string, string|undefined>} [env]
 * @returns {string[]}
 */
export function missingRequired(mode, env = process.env) {
  const names = envVarNames(mode);
  return REQUIRED_FIELDS.map((field) => names[field.toLowerCase()]).filter((name) => !clean(env[name]));
}

/**
 * Load one mode's configuration.
 *
 * @param {'mysql'|'oracle'} mode
 * @param {Record<string, string|undefined>} [env]
 * @returns {object|null} The configuration, or null when a required variable is
 *   missing -- which the harness turns into SKIP_NO_ENV for every check.
 */
export function loadConfig(mode, env = process.env) {
  if (!ENV_PREFIX[mode]) throw new Error(`unknown compatibility mode: ${mode}`);
  if (missingRequired(mode, env).length > 0) return null;

  const names = envVarNames(mode);
  return {
    mode,
    host: clean(env[names.host]),
    port: Number.parseInt(clean(env[names.port]), 10),
    user: clean(env[names.user]),
    password: clean(env[names.password]),
    database: clean(env[names.database]),
    serviceName: clean(env[names.serviceName]),
    schema: clean(env[names.schema]),
    connectTimeoutS: asFloat(env[names.connectTimeoutS], 5),
    queryTimeoutS: asFloat(env[names.queryTimeoutS], 30),
    poolMax: asInt(env[names.poolMax], 5),
  };
}

/**
 * Render a target for a result file with credentials removed.
 *
 * @param {object} config
 * @param {import('./redact.mjs').Redactor} redactor
 * @returns {object}
 */
export function redactedTarget(config, redactor) {
  return redactor.redactDeep({
    host: config.host,
    port: config.port,
    user: config.user,
    password: config.password,
    database: config.database,
    serviceName: config.serviceName,
    schema: config.schema,
    connectTimeoutS: config.connectTimeoutS,
    queryTimeoutS: config.queryTimeoutS,
    poolMax: config.poolMax,
  });
}

/**
 * Build a redactor holding every configured password.
 *
 * @param {string[]} modes
 * @param {Record<string, string|undefined>} [env]
 * @returns {import('./redact.mjs').Redactor}
 */
export function buildRedactor(modes, env = process.env) {
  const secrets = [];
  for (const mode of modes) {
    const config = loadConfig(mode, env);
    if (config?.password) secrets.push(config.password);
  }
  return new Redactor(secrets);
}

function clean(value) {
  if (value === undefined || value === null) return null;
  const trimmed = String(value).trim();
  return trimmed === '' ? null : trimmed;
}

function asFloat(value, fallback) {
  const cleaned = clean(value);
  if (cleaned === null) return fallback;
  const parsed = Number.parseFloat(cleaned);
  return Number.isNaN(parsed) ? fallback : parsed;
}

function asInt(value, fallback) {
  const cleaned = clean(value);
  if (cleaned === null) return fallback;
  const parsed = Number.parseInt(cleaned, 10);
  return Number.isNaN(parsed) ? fallback : parsed;
}
